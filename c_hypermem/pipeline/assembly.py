from __future__ import annotations

from c_hypermem.config import MemoryConfig
from c_hypermem.llms.base import LLMClient
from c_hypermem.pipeline.context import AssemblyContext
from c_hypermem.pipeline.edge_cluster_builder import BasicEdgeClusterBuilder
from c_hypermem.pipeline.entity_resolution import EntityResolver
from c_hypermem.pipeline.graph_utils import (
    dedupe_cluster_members,
    dedupe_clusters,
    dedupe_edges,
    dedupe_fact_properties,
    merge_node,
    property_key,
)
from c_hypermem.pipeline.hyperedge_builder import BasicHyperEdgeBuilder
from c_hypermem.pipeline.maintenance import GraphMaintenance
from c_hypermem.pipeline.node_builder import NodeBuilder, collect_entities
from c_hypermem.schema import (
    EdgeCluster,
    EdgeClusterMember,
    EntityAliasIndexEntry,
    ExtractedEntity,
    FactPropertyIndexEntry,
    HyperEdge,
    MemoryExtraction,
    MemoryNode,
)
from c_hypermem.stores.base import MemoryStore
from c_hypermem.utils.text import normalize_text
from c_hypermem.utils.time import utc_now_iso


class GraphAssembler:
    """Coordinate extracted candidates into graph components.

    Entity resolution, MemoryNode construction, LocalNodeGraph construction and
    semantic graph maintenance live in dedicated helpers; this class keeps
    the write-time assembly order in one place.
    """

    def __init__(self, config: MemoryConfig, store: MemoryStore, *, maintenance_llm: LLMClient | None = None) -> None:
        self.config = config
        self.store = store
        self.entity_resolver = EntityResolver(store)
        self.node_builder = NodeBuilder()
        self.hyperedge_builder = BasicHyperEdgeBuilder(config)
        self.edge_cluster_builder = BasicEdgeClusterBuilder(config, store)
        self.maintenance = GraphMaintenance(store, llm=maintenance_llm)

    def assemble(self, extraction: MemoryExtraction, context: AssemblyContext) -> tuple[
        list[MemoryNode],
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
        retired_nodes: list[MemoryNode] = []
        edges: list[HyperEdge] = []

        event_node = self.node_builder.build_event_node(extraction.events, context)
        if event_node is not None:
            nodes_by_id[event_node.node_id] = self._merge_node(nodes_by_id.get(event_node.node_id), event_node, context)

        entity_map = self._build_entity_nodes(extraction, context, nodes_by_id, aliases_by_node_id, entity_types_by_node_id)
        assertion_links = []

        for assertion in extraction.assertions[: self.config.ingestion.max_facts_per_event]:
            if not assertion.subject or not assertion.object:
                continue
            subject_node = entity_map.get(normalize_text(assertion.subject))
            if subject_node is None:
                subject_node = self._build_subject_entity(
                    assertion.subject,
                    context,
                    nodes_by_id,
                    aliases_by_node_id,
                    entity_types_by_node_id,
                )
                for alias in aliases_by_node_id.get(subject_node.node_id, set()):
                    entity_map[normalize_text(alias)] = subject_node

            fact_node = self.node_builder.build_fact_node(assertion, subject_node, context)
            fact_property_key = property_key(subject_node.node_id, assertion.predicate)
            fact_property = FactPropertyIndexEntry(
                namespace=context.namespace,
                property_key=fact_property_key,
                subject_node_id=subject_node.node_id,
                predicate=assertion.predicate,
                fact_node_id=fact_node.node_id,
                updated_at=utc_now_iso(),
            )
            old_properties = self.store.find_fact_properties(context.namespace, fact_property_key, status="active")
            old_fact_ids = [item.fact_node_id for item in old_properties if item.fact_node_id != fact_node.node_id]
            old_facts = self.store.get_nodes(context.namespace, old_fact_ids)
            if old_facts:
                overlap_decision = self.maintenance.resolve_fact_overlap(
                    new_fact=fact_node,
                    assertion=assertion,
                    old_facts=old_facts,
                    context=context,
                )
                if overlap_decision.should_merge_or_update:
                    for old_fact in _affected_facts(old_facts, overlap_decision.affected_refs):
                        updated_fact = self.maintenance.apply_fact_overlap_update(
                            old_fact=old_fact,
                            new_fact=fact_node,
                            assertion=assertion,
                            decision=overlap_decision,
                            context=context,
                        )
                        nodes_by_id[updated_fact.node_id] = updated_fact
                        fact_properties.append(
                            FactPropertyIndexEntry(
                                namespace=context.namespace,
                                property_key=fact_property_key,
                                subject_node_id=subject_node.node_id,
                                predicate=assertion.predicate,
                                fact_node_id=updated_fact.node_id,
                                updated_at=utc_now_iso(),
                            )
                        )
                    continue
                if overlap_decision.needs_contradiction_check:
                    old_facts = _affected_facts(old_facts, overlap_decision.affected_refs)
                else:
                    old_facts = []

            nodes_by_id[fact_node.node_id] = self._merge_node(nodes_by_id.get(fact_node.node_id), fact_node, context)
            fact_node = nodes_by_id[fact_node.node_id]
            assertion_links.append((fact_node, subject_node, assertion))
            fact_properties.append(fact_property)
            newly_retired_nodes, correction_edges, retired_properties = self.maintenance.retire_conflicting_facts(
                property_key=fact_property.property_key,
                new_fact=fact_node,
                assertion=assertion,
                context=context,
                correction_edge_builder=self.hyperedge_builder.build_correction_edge,
                old_facts=old_facts,
            )
            retired_nodes.extend(newly_retired_nodes)
            for retired_node in newly_retired_nodes:
                nodes_by_id[retired_node.node_id] = retired_node
            edges.extend(correction_edges)
            fact_properties.extend(retired_properties)

        if event_node is not None and assertion_links:
            edges.append(
                self.hyperedge_builder.build_evidence_edge(
                    event_node,
                    [fact for fact, _, _ in assertion_links],
                    context,
                )
            )
        for fact_node, subject_node, assertion in assertion_links:
            edges.append(
                self.hyperedge_builder.build_state_edge(
                    subject_node,
                    fact_node,
                    subject=assertion.subject,
                    predicate=assertion.predicate,
                    object_=assertion.object,
                    polarity=assertion.polarity,
                    event_node=event_node,
                    context=context,
                )
            )

        clusters: list[EdgeCluster] = []
        cluster_members: list[EdgeClusterMember] = []
        if self.config.edge_clusters.enabled:
            clusters, cluster_members = self.edge_cluster_builder.build(
                edges,
                namespace=context.namespace,
                metadata=context.metadata,
                current_turn=context.current_turn,
            )

        alias_entries = self._alias_entries(context.namespace, aliases_by_node_id, entity_types_by_node_id)
        return (
            list(nodes_by_id.values()),
            retired_nodes,
            dedupe_edges(edges),
            dedupe_clusters(clusters),
            dedupe_cluster_members(cluster_members),
            alias_entries,
            dedupe_fact_properties(fact_properties),
        )

    def _build_entity_nodes(
        self,
        extraction: MemoryExtraction,
        context: AssemblyContext,
        nodes_by_id: dict[str, MemoryNode],
        aliases_by_node_id: dict[str, set[str]],
        entity_types_by_node_id: dict[str, str | None],
    ) -> dict[str, MemoryNode]:
        entity_map: dict[str, MemoryNode] = {}
        for entity in collect_entities(extraction.events, extraction.assertions, extraction.entities):
            node = self._build_entity(entity, context, nodes_by_id, aliases_by_node_id, entity_types_by_node_id)
            for alias in aliases_by_node_id.get(node.node_id, set()):
                entity_map[normalize_text(alias)] = node
        return entity_map

    def _build_subject_entity(
        self,
        subject: str,
        context: AssemblyContext,
        nodes_by_id: dict[str, MemoryNode],
        aliases_by_node_id: dict[str, set[str]],
        entity_types_by_node_id: dict[str, str | None],
    ) -> MemoryNode:
        return self._build_entity(
            ExtractedEntity(name=subject, labels=["referent"], aliases=[]),
            context,
            nodes_by_id,
            aliases_by_node_id,
            entity_types_by_node_id,
        )

    def _build_entity(
        self,
        entity: ExtractedEntity,
        context: AssemblyContext,
        nodes_by_id: dict[str, MemoryNode],
        aliases_by_node_id: dict[str, set[str]],
        entity_types_by_node_id: dict[str, str | None],
    ) -> MemoryNode:
        resolution = self.entity_resolver.resolve(entity, context)
        entity_node = self.node_builder.build_or_update_entity_node(entity, resolution, context)
        entity_node = self._merge_node(nodes_by_id.get(entity_node.node_id), entity_node, context)
        nodes_by_id[entity_node.node_id] = entity_node
        aliases_by_node_id.setdefault(entity_node.node_id, set()).update(resolution.aliases)
        entity_types_by_node_id.setdefault(entity_node.node_id, entity_node.attributes.get("entity_type"))
        return entity_node

    def _merge_node(self, existing: MemoryNode | None, incoming: MemoryNode, context: AssemblyContext) -> MemoryNode:
        if existing is None:
            stored = self.store.get_nodes(context.namespace, [incoming.node_id])
            existing = stored[0] if stored else None
        return merge_node(existing, incoming, context)

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


def _affected_facts(old_facts: list[MemoryNode], refs: list[str]) -> list[MemoryNode]:
    by_ref = {f"existing:{index}": fact for index, fact in enumerate(old_facts)}
    return [by_ref[ref] for ref in refs if ref in by_ref]
