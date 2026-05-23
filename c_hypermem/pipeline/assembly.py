from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from c_hypermem.config import MemoryConfig
from c_hypermem.schema import (
    EdgeCluster,
    EdgeClusterMember,
    EdgeDescriptionVariant,
    EntityAliasIndexEntry,
    ExtractedAssertion,
    ExtractedEntity,
    ExtractedEvent,
    FactPropertyIndexEntry,
    HyperEdge,
    LocalNodeGraph,
    LocalTriple,
    MemoryExtraction,
    MemoryNode,
)
from c_hypermem.stores.base import MemoryStore
from c_hypermem.utils.ids import make_cluster_id, make_edge_id, make_fingerprint, make_member_signature, make_node_id
from c_hypermem.utils.text import compact_key, normalize_text, truncate
from c_hypermem.utils.time import make_time_bundle, touch_node_update, utc_now_iso


@dataclass
class AssemblyContext:
    namespace: str
    metadata: dict[str, Any]
    current_turn: int


@dataclass
class EntityResolution:
    node: MemoryNode
    aliases: set[str] = field(default_factory=set)


class GraphAssembler:
    """Build MemoryNodes, LocalNodeGraphs, HyperEdges, and indexes from candidates."""

    def __init__(self, config: MemoryConfig, store: MemoryStore) -> None:
        self.config = config
        self.store = store

    def assemble(self, extraction: MemoryExtraction, context: AssemblyContext) -> tuple[
        list[MemoryNode],
        list[HyperEdge],
        list[EdgeCluster],
        list[EdgeClusterMember],
        list[EntityAliasIndexEntry],
        list[FactPropertyIndexEntry],
    ]:
        nodes_by_id: dict[str, MemoryNode] = {}
        aliases_by_node_id: dict[str, set[str]] = {}
        entity_types_by_node_id: dict[str, str | None] = {}
        fact_properties: list[FactPropertyIndexEntry] = []
        edges: list[HyperEdge] = []
        clusters: list[EdgeCluster] = []
        cluster_members: list[EdgeClusterMember] = []

        event_node = self._build_event_node(extraction.events, context)
        if event_node is not None:
            nodes_by_id[event_node.node_id] = event_node

        entity_map: dict[str, MemoryNode] = {}
        entity_candidates = self._collect_entities(extraction)
        for entity in entity_candidates:
            resolution = self._resolve_entity(entity, context)
            entity_node = self._merge_node(nodes_by_id.get(resolution.node.node_id), resolution.node, context)
            nodes_by_id[entity_node.node_id] = entity_node
            aliases_by_node_id.setdefault(entity_node.node_id, set()).update(resolution.aliases)
            entity_types_by_node_id.setdefault(entity_node.node_id, entity_node.attributes.get("entity_type"))
            for alias in resolution.aliases:
                entity_map[normalize_text(alias)] = entity_node

        assertion_links: list[tuple[MemoryNode, MemoryNode | None, ExtractedAssertion]] = []
        for assertion in extraction.assertions[: self.config.ingestion.max_facts_per_event]:
            if not assertion.subject or not assertion.object:
                continue
            subject_node = entity_map.get(normalize_text(assertion.subject))
            if subject_node is None:
                subject_resolution = self._resolve_entity(
                    ExtractedEntity(name=assertion.subject, labels=["referent"], aliases=[]),
                    context,
                )
                subject_node = self._merge_node(
                    nodes_by_id.get(subject_resolution.node.node_id),
                    subject_resolution.node,
                    context,
                )
                nodes_by_id[subject_node.node_id] = subject_node
                aliases_by_node_id.setdefault(subject_node.node_id, set()).update(subject_resolution.aliases)
                entity_types_by_node_id.setdefault(subject_node.node_id, subject_node.attributes.get("entity_type"))
                for alias in subject_resolution.aliases:
                    entity_map[normalize_text(alias)] = subject_node

            fact_node = self._build_fact_node(assertion, subject_node, context)
            nodes_by_id[fact_node.node_id] = self._merge_node(nodes_by_id.get(fact_node.node_id), fact_node, context)
            assertion_links.append((nodes_by_id[fact_node.node_id], subject_node, assertion))

            property_key = _property_key(subject_node.node_id, assertion.predicate)
            fact_properties.append(
                FactPropertyIndexEntry(
                    namespace=context.namespace,
                    property_key=property_key,
                    subject_node_id=subject_node.node_id,
                    predicate=assertion.predicate,
                    fact_node_id=fact_node.node_id,
                    updated_at=utc_now_iso(),
                )
            )
            corrections = self._build_corrections(property_key, nodes_by_id[fact_node.node_id], assertion, context)
            for old_fact, correction_edge, retired_property in corrections:
                nodes_by_id[old_fact.node_id] = old_fact
                edges.append(correction_edge)
                fact_properties.append(retired_property)

        if event_node is not None and assertion_links:
            edges.append(self._build_evidence_edge(event_node, [fact for fact, _, _ in assertion_links], context))
        for fact_node, subject_node, assertion in assertion_links:
            if subject_node is not None:
                edges.append(self._build_state_edge(subject_node, fact_node, assertion, event_node, context))

        if self.config.edge_clusters.enabled:
            for edge in edges:
                cluster, member = self._cluster_for_edge(edge, context)
                clusters.append(cluster)
                cluster_members.append(member)

        alias_entries = self._alias_entries(context.namespace, aliases_by_node_id, entity_types_by_node_id)
        return (
            list(nodes_by_id.values()),
            _dedupe_edges(edges),
            _dedupe_clusters(clusters),
            _dedupe_cluster_members(cluster_members),
            alias_entries,
            _dedupe_fact_properties(fact_properties),
        )

    def _build_event_node(self, events: list[ExtractedEvent], context: AssemblyContext) -> MemoryNode | None:
        if events:
            summary = "; ".join(truncate(event.summary, 180) for event in events if event.summary).strip()
            event_time = next((event.time for event in events if event.time), None)
            participants = {
                participant.name: participant.role or "participant"
                for event in events
                for participant in event.participants
                if participant.name
            }
        else:
            summary = truncate(_event_fallback_text(context.metadata), 220)
            event_time = context.metadata.get("date") or context.metadata.get("timestamp")
            participants = {}
        if not summary:
            return None

        session_id = context.metadata.get("session_id") or context.metadata.get("conversation_id")
        canonical = summary
        hint = {"session_id": session_id, "turn": context.current_turn} if session_id else {"turn": context.current_turn}
        fingerprint = make_fingerprint(canonical, hint)
        node = MemoryNode(
            node_id=make_node_id(context.namespace, fingerprint),
            namespace=context.namespace,
            canonical_text=canonical,
            normalized_text=normalize_text(canonical),
            fingerprint=fingerprint,
            node_labels=["event"],
            content=canonical,
            summary=summary,
            attributes={"participants": participants} if participants else {},
            metadata=_source_metadata(context, source_ref="interaction", extra={"event_count": len(events)}),
            time=make_time_bundle(
                current_turn=context.current_turn,
                event_time=event_time,
                source_timestamp=context.metadata.get("timestamp"),
                valid_start=event_time,
            ),
            local_graph=LocalNodeGraph(
                triples=[
                    LocalTriple(subject=name, predicate="participated_as", object=role)
                    for name, role in participants.items()
                ],
                roles=participants,
            ),
        )
        return node

    def _collect_entities(self, extraction: MemoryExtraction) -> list[ExtractedEntity]:
        by_name: dict[str, ExtractedEntity] = {}
        for entity in extraction.entities:
            key = normalize_text(entity.name)
            if not key:
                continue
            by_name.setdefault(key, entity)
        for event in extraction.events:
            for participant in event.participants:
                key = normalize_text(participant.name)
                if key and key not in by_name:
                    by_name[key] = ExtractedEntity(name=participant.name, labels=["participant"], aliases=[])
        for assertion in extraction.assertions:
            key = normalize_text(assertion.subject)
            if key and key not in by_name:
                by_name[key] = ExtractedEntity(name=assertion.subject, labels=["referent"], aliases=[])
        return list(by_name.values())

    def _resolve_entity(self, entity: ExtractedEntity, context: AssemblyContext) -> EntityResolution:
        aliases = _entity_aliases(entity)
        normalized_aliases = [normalize_text(alias) for alias in aliases if normalize_text(alias)]
        existing = self.store.find_entity_alias(context.namespace, normalized_aliases, entity.entity_type)
        if existing is not None:
            existing_nodes = self.store.get_nodes(context.namespace, [existing.node_id])
            if existing_nodes:
                node = existing_nodes[0]
                node = self._update_entity_node(node, entity, aliases, context)
                return EntityResolution(node=node, aliases=set(aliases))

        canonical_name = entity.name.strip()
        hint = {"entity_type": entity.entity_type} if entity.entity_type else None
        fingerprint = make_fingerprint(canonical_name, hint)
        node = MemoryNode(
            node_id=make_node_id(context.namespace, fingerprint),
            namespace=context.namespace,
            canonical_text=canonical_name,
            normalized_text=normalize_text(canonical_name),
            fingerprint=fingerprint,
            node_labels=_dedupe_labels(["entity", *entity.labels]),
            content=canonical_name,
            summary=canonical_name,
            attributes={
                "canonical_name": canonical_name,
                "display_name": canonical_name,
                "entity_type": entity.entity_type,
                "aliases": sorted(set(aliases)),
                **entity.attributes,
            },
            metadata=_source_metadata(context, source_ref=entity.source_ref),
            time=make_time_bundle(current_turn=context.current_turn),
            local_graph=LocalNodeGraph(
                attributes={"entity_type": entity.entity_type} if entity.entity_type else {},
            ),
        )
        return EntityResolution(node=node, aliases=set(aliases))

    def _update_entity_node(
        self,
        node: MemoryNode,
        entity: ExtractedEntity,
        aliases: list[str],
        context: AssemblyContext,
    ) -> MemoryNode:
        node.node_labels = _dedupe_labels([*node.node_labels, "entity", *entity.labels])
        existing_aliases = set(_string_list(node.attributes.get("aliases")))
        node.attributes["aliases"] = sorted(existing_aliases.union(aliases))
        if entity.entity_type and not node.attributes.get("entity_type"):
            node.attributes["entity_type"] = entity.entity_type
            node.local_graph.attributes["entity_type"] = entity.entity_type
        node.metadata.update(_source_metadata(context, source_ref=entity.source_ref))
        return touch_node_update(node, context.current_turn)

    def _build_fact_node(
        self,
        assertion: ExtractedAssertion,
        subject_node: MemoryNode,
        context: AssemblyContext,
    ) -> MemoryNode:
        canonical = _assertion_text(assertion)
        fingerprint = make_fingerprint(
            canonical,
            {
                "subject_node_id": subject_node.node_id,
                "predicate": normalize_text(assertion.predicate),
            },
        )
        labels = _dedupe_labels(["fact", *assertion.labels])
        if _looks_like_preference(assertion):
            labels.append("preference")
        labels = _dedupe_labels(labels)
        triple = LocalTriple(
            subject=assertion.subject,
            predicate=assertion.predicate,
            object=assertion.object,
            qualifiers={"source_ref": assertion.source_ref} if assertion.source_ref else {},
        )
        return MemoryNode(
            node_id=make_node_id(context.namespace, fingerprint),
            namespace=context.namespace,
            canonical_text=canonical,
            normalized_text=normalize_text(canonical),
            fingerprint=fingerprint,
            node_labels=labels,
            content=canonical,
            summary=canonical,
            attributes={
                "subject": assertion.subject,
                "subject_node_id": subject_node.node_id,
                "predicate": assertion.predicate,
                "object": assertion.object,
                "polarity": assertion.polarity,
                **assertion.attributes,
            },
            metadata=_source_metadata(context, source_ref=assertion.source_ref),
            time=make_time_bundle(
                current_turn=context.current_turn,
                event_time=assertion.time or context.metadata.get("date"),
                valid_start=assertion.time or context.metadata.get("date"),
            ),
            local_graph=LocalNodeGraph(
                triples=[triple],
                attributes={"polarity": assertion.polarity},
            ),
        )

    def _build_corrections(
        self,
        property_key: str,
        new_fact: MemoryNode,
        assertion: ExtractedAssertion,
        context: AssemblyContext,
    ) -> list[tuple[MemoryNode, HyperEdge, FactPropertyIndexEntry]]:
        corrections: list[tuple[MemoryNode, HyperEdge, FactPropertyIndexEntry]] = []
        old_properties = self.store.find_fact_properties(context.namespace, property_key, status="active")
        if not old_properties:
            return corrections
        old_fact_ids = [item.fact_node_id for item in old_properties if item.fact_node_id != new_fact.node_id]
        for old_fact in self.store.get_nodes(context.namespace, old_fact_ids):
            if not _is_conflict(old_fact, assertion):
                continue
            old_fact.status = "retired"
            old_fact.superseded_by = new_fact.node_id
            old_fact.invalidated_by = new_fact.node_id
            old_fact.status_reason = "newer conflicting fact for the same subject and predicate"
            old_fact.status_updated_at = utc_now_iso()
            if old_fact.time.world.valid_time and not old_fact.time.world.valid_time.end:
                old_fact.time.world.valid_time.end = assertion.time or context.metadata.get("date")
            touch_node_update(old_fact, context.current_turn)
            retired_property = FactPropertyIndexEntry(
                namespace=context.namespace,
                property_key=property_key,
                subject_node_id=old_fact.attributes.get("subject_node_id"),
                predicate=assertion.predicate,
                fact_node_id=old_fact.node_id,
                status="retired",
                updated_at=utc_now_iso(),
            )
            corrections.append((old_fact, self._build_correction_edge(old_fact, new_fact, context), retired_property))
        return corrections

    def _build_evidence_edge(
        self,
        event_node: MemoryNode,
        fact_nodes: list[MemoryNode],
        context: AssemblyContext,
    ) -> HyperEdge:
        node_ids = [event_node.node_id, *[fact.node_id for fact in fact_nodes]]
        roles = {event_node.node_id: "evidence_event", **{fact.node_id: "derived_fact" for fact in fact_nodes}}
        description = f"{event_node.summary} supports {len(fact_nodes)} extracted fact(s)."
        edge = self._edge(
            edge_type="evidence",
            relation="supports_extracted_facts",
            description=description,
            node_ids=node_ids,
            roles=roles,
            context=context,
        )
        for fact in fact_nodes:
            for triple in fact.local_graph.triples:
                triple.scope_edge_id = edge.edge_id
                triple.role_in_edge = roles.get(fact.node_id)
                triple.edge_relation = edge.relation
        return edge

    def _build_state_edge(
        self,
        subject_node: MemoryNode,
        fact_node: MemoryNode,
        assertion: ExtractedAssertion,
        event_node: MemoryNode | None,
        context: AssemblyContext,
    ) -> HyperEdge:
        node_ids = [subject_node.node_id, fact_node.node_id]
        roles = {subject_node.node_id: "subject", fact_node.node_id: "state_fact"}
        if event_node is not None:
            node_ids.append(event_node.node_id)
            roles[event_node.node_id] = "evidence_event"
        description = f"{assertion.subject} {assertion.predicate} {assertion.object}"
        edge = self._edge(
            edge_type="state",
            relation="describes_entity_state",
            description=description,
            node_ids=node_ids,
            roles=roles,
            polarity=assertion.polarity,
            context=context,
        )
        for triple in fact_node.local_graph.triples:
            triple.scope_edge_id = edge.edge_id
            triple.role_in_edge = roles.get(fact_node.node_id)
            triple.edge_relation = edge.relation
        return edge

    def _build_correction_edge(
        self,
        old_fact: MemoryNode,
        new_fact: MemoryNode,
        context: AssemblyContext,
    ) -> HyperEdge:
        return self._edge(
            edge_type="correction",
            relation="invalidates_previous_fact",
            description=f"{new_fact.content} invalidates {old_fact.content}",
            node_ids=[new_fact.node_id, old_fact.node_id],
            roles={new_fact.node_id: "new_fact", old_fact.node_id: "invalidated_fact"},
            polarity="neutral",
            context=context,
        )

    def _edge(
        self,
        *,
        edge_type: str,
        relation: str,
        description: str,
        node_ids: list[str],
        roles: dict[str, str],
        context: AssemblyContext,
        polarity: str = "positive",
    ) -> HyperEdge:
        source_scope = {
            "session_id": context.metadata.get("session_id"),
            "turn": context.current_turn,
            "date": context.metadata.get("date"),
        }
        edge_fingerprint = make_fingerprint(
            description,
            {
                "edge_type": edge_type,
                "relation": relation,
                "roles": sorted(roles.values()),
                "source_scope": source_scope,
            },
        )
        return HyperEdge(
            edge_id=make_edge_id(context.namespace, edge_fingerprint),
            namespace=context.namespace,
            edge_fingerprint=edge_fingerprint,
            edge_type=edge_type,
            relation=relation,
            description=description,
            polarity=polarity,  # type: ignore[arg-type]
            member_policy=self.config.hyperedges.member_policy_default,  # type: ignore[arg-type]
            member_signature=make_member_signature(node_ids, roles),
            node_ids=list(dict.fromkeys(node_ids)),
            roles=roles,
            weights={node_id: 1.0 for node_id in node_ids},
            metadata=_source_metadata(context, source_ref=edge_type),
            time=make_time_bundle(
                current_turn=context.current_turn,
                event_time=context.metadata.get("date"),
                valid_start=context.metadata.get("date"),
            ),
        )

    def _cluster_for_edge(
        self,
        edge: HyperEdge,
        context: AssemblyContext,
    ) -> tuple[EdgeCluster, EdgeClusterMember]:
        cluster_label = _cluster_label(edge)
        cluster_description = _cluster_description(edge)
        cluster_fingerprint = make_fingerprint(cluster_description, {"cluster_label": cluster_label})
        cluster = EdgeCluster(
            cluster_id=make_cluster_id(context.namespace, cluster_fingerprint),
            namespace=context.namespace,
            cluster_fingerprint=cluster_fingerprint,
            canonical_description=cluster_description,
            cluster_labels=[cluster_label],
            aliases=[compact_key(cluster_description)] if compact_key(cluster_description) else [],
            conflict_state="contains_conflict" if edge.edge_type == "correction" else "none",
            description_variants=[EdgeDescriptionVariant(text=edge.description, source_edge_id=edge.edge_id)],
            metadata=_source_metadata(context, source_ref=edge.edge_type),
        )
        member = EdgeClusterMember(
            namespace=context.namespace,
            cluster_id=cluster.cluster_id,
            edge_id=edge.edge_id,
            relation_to_cluster="updates" if edge.edge_type == "correction" else "supports",
        )
        return cluster, member

    def _merge_node(
        self,
        existing: MemoryNode | None,
        incoming: MemoryNode,
        context: AssemblyContext,
    ) -> MemoryNode:
        if existing is None:
            stored = self.store.get_nodes(context.namespace, [incoming.node_id])
            existing = stored[0] if stored else None
        if existing is None:
            return incoming
        existing.node_labels = _dedupe_labels([*existing.node_labels, *incoming.node_labels])
        existing.attributes = _deep_merge_dict(existing.attributes, incoming.attributes)
        existing.metadata = _deep_merge_dict(existing.metadata, incoming.metadata)
        existing.local_graph = _merge_local_graph(existing.local_graph, incoming.local_graph)
        if not existing.summary and incoming.summary:
            existing.summary = incoming.summary
        if not existing.content and incoming.content:
            existing.content = incoming.content
        return touch_node_update(existing, context.current_turn)

    def _alias_entries(
        self,
        namespace: str,
        aliases_by_node_id: dict[str, set[str]],
        entity_types_by_node_id: dict[str, str | None],
    ) -> list[EntityAliasIndexEntry]:
        entries: list[EntityAliasIndexEntry] = []
        for node_id, aliases in aliases_by_node_id.items():
            entity_type = entity_types_by_node_id.get(node_id)
            for alias in aliases:
                normalized = normalize_text(alias)
                if not normalized:
                    continue
                entries.append(
                    EntityAliasIndexEntry(
                        namespace=namespace,
                        normalized_alias=normalized,
                        entity_type=entity_type,
                        node_id=node_id,
                        updated_at=utc_now_iso(),
                    )
                )
        return entries


def _event_fallback_text(metadata: dict[str, Any]) -> str:
    session_id = metadata.get("session_id") or metadata.get("conversation_id")
    date = metadata.get("date") or metadata.get("timestamp")
    parts = ["Interaction"]
    if session_id:
        parts.append(f"in {session_id}")
    if date:
        parts.append(f"on {date}")
    return " ".join(parts)


def _source_metadata(context: AssemblyContext, *, source_ref: str | None, extra: dict[str, Any] | None = None) -> dict[str, Any]:
    metadata = {
        "source_ref": source_ref,
        "source_session_id": context.metadata.get("session_id") or context.metadata.get("conversation_id"),
        "date": context.metadata.get("date"),
        "source_turn_ids": context.metadata.get("turn_ids", []),
    }
    if extra:
        metadata.update(extra)
    return {key: value for key, value in metadata.items() if value not in (None, [], {})}


def _entity_aliases(entity: ExtractedEntity) -> list[str]:
    return list(dict.fromkeys([entity.name, *entity.aliases]))


def _assertion_text(assertion: ExtractedAssertion) -> str:
    return " ".join(part for part in [assertion.subject, assertion.predicate, assertion.object] if part).strip()


def _property_key(subject_node_id: str, predicate: str) -> str:
    return f"{subject_node_id}:{compact_key(predicate)}"


def _looks_like_preference(assertion: ExtractedAssertion) -> bool:
    predicate = normalize_text(assertion.predicate)
    return any(token in predicate for token in ["prefer", "like", "favorite", "favourite"])


def _is_conflict(old_fact: MemoryNode, assertion: ExtractedAssertion) -> bool:
    old_object = normalize_text(str(old_fact.attributes.get("object", "")))
    new_object = normalize_text(assertion.object)
    if not old_object or not new_object:
        return False
    if old_object == new_object:
        return False
    predicate = normalize_text(assertion.predicate)
    multi_value_predicates = {"likes", "enjoys", "has_hobby", "knows", "visited", "uses"}
    if predicate in multi_value_predicates:
        return False
    return True


def _cluster_label(edge: HyperEdge) -> str:
    if edge.edge_type == "state":
        return "entity_state"
    if edge.edge_type == "correction":
        return "conflict_resolution"
    return f"{edge.edge_type}_context"


def _cluster_description(edge: HyperEdge) -> str:
    if edge.edge_type == "state":
        return edge.description
    return f"{edge.edge_type}: {edge.relation}"


def _dedupe_labels(labels: list[str]) -> list[str]:
    cleaned = []
    for label in labels:
        value = compact_key(label)
        if value and value not in cleaned:
            cleaned.append(value)
    return cleaned


def _string_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(item) for item in value]
    return [str(value)]


def _merge_local_graph(existing: LocalNodeGraph, incoming: LocalNodeGraph) -> LocalNodeGraph:
    triple_keys = {
        (normalize_text(triple.subject), normalize_text(triple.predicate), normalize_text(triple.object))
        for triple in existing.triples
    }
    for triple in incoming.triples:
        key = (normalize_text(triple.subject), normalize_text(triple.predicate), normalize_text(triple.object))
        if key not in triple_keys:
            existing.triples.append(triple)
            triple_keys.add(key)
    existing.attributes = _deep_merge_dict(existing.attributes, incoming.attributes)
    existing.roles.update(incoming.roles)
    return existing


def _deep_merge_dict(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    merged = dict(base)
    for key, value in override.items():
        if value in (None, [], {}):
            continue
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge_dict(merged[key], value)
        elif isinstance(value, list) and isinstance(merged.get(key), list):
            merged[key] = list(dict.fromkeys([*merged[key], *value]))
        else:
            merged[key] = value
    return merged


def _dedupe_edges(edges: list[HyperEdge]) -> list[HyperEdge]:
    return list({edge.edge_id: edge for edge in edges}.values())


def _dedupe_clusters(clusters: list[EdgeCluster]) -> list[EdgeCluster]:
    by_id: dict[str, EdgeCluster] = {}
    for cluster in clusters:
        existing = by_id.get(cluster.cluster_id)
        if existing is None:
            by_id[cluster.cluster_id] = cluster
            continue
        existing.description_variants.extend(cluster.description_variants)
        existing.description_variants = existing.description_variants[:8]
        existing.conflict_state = (
            "contains_conflict"
            if "contains_conflict" in {existing.conflict_state, cluster.conflict_state}
            else existing.conflict_state
        )
    return list(by_id.values())


def _dedupe_cluster_members(members: list[EdgeClusterMember]) -> list[EdgeClusterMember]:
    return list({(member.cluster_id, member.edge_id): member for member in members}.values())


def _dedupe_fact_properties(properties: list[FactPropertyIndexEntry]) -> list[FactPropertyIndexEntry]:
    return list({(item.property_key, item.fact_node_id): item for item in properties}.values())
