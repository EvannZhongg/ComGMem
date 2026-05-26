from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

from c_hypermem.config import MemoryConfig
from c_hypermem.pipeline.context import AssemblyContext
from c_hypermem.pipeline.graph_utils import deep_merge_dict, source_metadata
from c_hypermem.schema import EdgeCluster, EdgeClusterMember, EdgeDescriptionVariant, HyperEdge, MemoryNode
from c_hypermem.stores.base import MemoryStore
from c_hypermem.utils.ids import make_cluster_id, make_fingerprint
from c_hypermem.utils.text import compact_key, normalize_text


class EdgeClusterBuilder(Protocol):
    """Builds related EdgeClusters without forcing HyperEdge merges."""

    def build(
        self,
        edges: list[HyperEdge],
        *,
        nodes: list[MemoryNode] | None = None,
        namespace: str,
        metadata: dict[str, Any],
        current_turn: int,
    ) -> tuple[list[EdgeCluster], list[EdgeClusterMember]]: ...


class BasicEdgeClusterBuilder:
    """Attach concrete HyperEdges to deterministic anchor clusters."""

    def __init__(self, config: MemoryConfig, store: MemoryStore | None = None) -> None:
        self.config = config
        self.store = store

    def build(
        self,
        edges: list[HyperEdge],
        *,
        nodes: list[MemoryNode] | None = None,
        namespace: str,
        metadata: dict[str, Any],
        current_turn: int,
    ) -> tuple[list[EdgeCluster], list[EdgeClusterMember]]:
        context = AssemblyContext(namespace=namespace, metadata=metadata, current_turn=current_turn)
        edges_by_id = {edge.edge_id: edge for edge in edges}
        occurrences_by_anchor: dict[AnchorKey, list[AnchorOccurrence]] = {}

        self._collect_shared_node_anchor_occurrences(edges, context, occurrences_by_anchor, edges_by_id)
        self._collect_semantic_anchor_occurrences(edges, nodes or [], context, occurrences_by_anchor, edges_by_id)
        self._load_missing_edges(context.namespace, occurrences_by_anchor, edges_by_id)

        clusters_by_fingerprint: dict[str, EdgeCluster] = {}
        changed_clusters_by_id: dict[str, EdgeCluster] = {}
        members_by_key: dict[tuple[str, str], EdgeClusterMember] = {}
        for key in sorted(occurrences_by_anchor):
            occurrences = _unique_anchor_occurrences(occurrences_by_anchor[key])
            edge_ids = sorted({occurrence.edge_id for occurrence in occurrences if occurrence.edge_id in edges_by_id})
            if len(edge_ids) < 2:
                continue
            related_edges = [edges_by_id[edge_id] for edge_id in edge_ids]
            cluster, created_or_changed = self._get_or_create_anchor_cluster(
                key,
                occurrences,
                related_edges,
                context,
                clusters_by_fingerprint,
            )
            if created_or_changed:
                changed_clusters_by_id[cluster.cluster_id] = cluster
            clusters_by_fingerprint[cluster.cluster_fingerprint] = cluster
            for edge_id in edge_ids:
                members_by_key[(cluster.cluster_id, edge_id)] = EdgeClusterMember(
                    namespace=context.namespace,
                    cluster_id=cluster.cluster_id,
                    edge_id=edge_id,
                    metadata={"cluster_basis": key.basis, "anchor_value": key.anchor_value},
                )
        return list(changed_clusters_by_id.values()), list(members_by_key.values())

    def build_for_edge(self, edge: HyperEdge, context: AssemblyContext) -> tuple[EdgeCluster, EdgeClusterMember]:
        key = AnchorKey(basis="shared_node", anchor_value=edge.node_ids[0])
        occurrence = AnchorOccurrence(
            basis=key.basis,
            anchor_value=key.anchor_value,
            edge_id=edge.edge_id,
            node_id=edge.node_ids[0],
        )
        cluster, _ = self._get_or_create_anchor_cluster(key, [occurrence], [edge], context, {})
        member = EdgeClusterMember(
            namespace=context.namespace,
            cluster_id=cluster.cluster_id,
            edge_id=edge.edge_id,
            metadata={"cluster_basis": key.basis, "anchor_value": key.anchor_value},
        )
        return cluster, member

    def _collect_shared_node_anchor_occurrences(
        self,
        edges: list[HyperEdge],
        context: AssemblyContext,
        occurrences_by_anchor: dict["AnchorKey", list["AnchorOccurrence"]],
        edges_by_id: dict[str, HyperEdge],
    ) -> None:
        edge_ids_by_node_id: dict[str, set[str]] = {}
        for edge in edges:
            for node_id in edge.node_ids:
                edge_ids_by_node_id.setdefault(node_id, set()).add(edge.edge_id)

        if self.store is not None and edge_ids_by_node_id:
            incident_edges = self.store.get_incident_edges(context.namespace, sorted(edge_ids_by_node_id))
            edges_by_id.update({edge.edge_id: edge for edge in incident_edges})
            for edge in incident_edges:
                for node_id in edge.node_ids:
                    if node_id in edge_ids_by_node_id:
                        edge_ids_by_node_id[node_id].add(edge.edge_id)

        for node_id in sorted(edge_ids_by_node_id):
            key = AnchorKey(basis="shared_node", anchor_value=node_id)
            for edge_id in sorted(edge_ids_by_node_id[node_id]):
                occurrences_by_anchor.setdefault(key, []).append(
                    AnchorOccurrence(
                        basis=key.basis,
                        anchor_value=key.anchor_value,
                        edge_id=edge_id,
                        node_id=node_id,
                    )
                )

    def _collect_semantic_anchor_occurrences(
        self,
        edges: list[HyperEdge],
        nodes: list[MemoryNode],
        context: AssemblyContext,
        occurrences_by_anchor: dict["AnchorKey", list["AnchorOccurrence"]],
        edges_by_id: dict[str, HyperEdge],
    ) -> None:
        semantic_occurrences: dict[AnchorKey, list[AnchorOccurrence]] = {}
        current_occurrences = _semantic_anchor_occurrences_from_nodes(edges, nodes)
        for occurrence in current_occurrences:
            semantic_occurrences.setdefault(_anchor_key(occurrence), []).append(occurrence)

        endpoint_values = sorted({occurrence.anchor_value for occurrence in current_occurrences})
        if self.store is not None and endpoint_values:
            current_edge_ids = {edge.edge_id for edge in edges}
            endpoint_value_set = set(endpoint_values)
            for record in self.store.find_triples_by_endpoints(context.namespace, endpoint_values):
                for position, value in (("subject", record.subject), ("object", record.object)):
                    anchor_value = normalize_text(value)
                    if not anchor_value or anchor_value not in endpoint_value_set:
                        continue
                    if not record.scope_edge_id or record.scope_edge_id in current_edge_ids:
                        continue
                    occurrence = AnchorOccurrence(
                        basis="semantic_anchor",
                        anchor_value=anchor_value,
                        edge_id=record.scope_edge_id,
                        node_id=record.owner_node_id,
                        triple_id=record.triple_id,
                        position=position,
                        subject=record.subject,
                        object=record.object,
                    )
                    semantic_occurrences.setdefault(_anchor_key(occurrence), []).append(occurrence)

        eligible_by_anchor = _eligible_semantic_anchor_occurrences_by_anchor(semantic_occurrences)
        for key, occurrences in eligible_by_anchor.items():
            eligible = _unique_anchor_occurrences(occurrences)
            if eligible:
                occurrences_by_anchor.setdefault(key, []).extend(eligible)

    def _load_missing_edges(
        self,
        namespace: str,
        occurrences_by_anchor: dict["AnchorKey", list["AnchorOccurrence"]],
        edges_by_id: dict[str, HyperEdge],
    ) -> None:
        if self.store is None:
            return
        missing_edge_ids = sorted(
            {
                occurrence.edge_id
                for occurrences in occurrences_by_anchor.values()
                for occurrence in occurrences
                if occurrence.edge_id not in edges_by_id
            }
        )
        if not missing_edge_ids:
            return
        edges_by_id.update({edge.edge_id: edge for edge in self.store.get_edges(namespace, missing_edge_ids)})

    def _get_or_create_anchor_cluster(
        self,
        key: "AnchorKey",
        occurrences: list["AnchorOccurrence"],
        related_edges: list[HyperEdge],
        context: AssemblyContext,
        batch_clusters: dict[str, EdgeCluster],
    ) -> tuple[EdgeCluster, bool]:
        cluster_fingerprint = cluster_fingerprint_for_anchor(key)
        cluster_description = _anchor_cluster_description(key)
        labels = _anchor_cluster_labels(key)
        occurrence_payloads = [_anchor_occurrence_payload(occurrence) for occurrence in occurrences]
        extra = _anchor_metadata(key, occurrence_payloads, _anchor_reasons(key, occurrences))

        cluster = batch_clusters.get(cluster_fingerprint)
        if cluster is None and self.store is not None:
            cluster = self.store.find_edge_cluster_by_fingerprint(context.namespace, cluster_fingerprint)

        created_or_changed = False
        if cluster is None:
            cluster = EdgeCluster(
                cluster_id=make_cluster_id(context.namespace, cluster_fingerprint),
                namespace=context.namespace,
                cluster_fingerprint=cluster_fingerprint,
                canonical_description=cluster_description,
                cluster_labels=labels,
                aliases=[compact_key(key.anchor_value)],
                conflict_state="none",
                description_variants=[],
                metadata=source_metadata(context, source_ref=None, extra=extra),
            )
            created_or_changed = True
        else:
            before_metadata = dict(cluster.metadata)
            before_labels = list(cluster.cluster_labels)
            before_aliases = list(cluster.aliases)
            cluster.canonical_description = cluster_description
            cluster.cluster_labels = list(dict.fromkeys([*cluster.cluster_labels, *labels]))
            cluster.aliases = list(dict.fromkeys([*cluster.aliases, compact_key(key.anchor_value)]))
            cluster.metadata = _merge_anchor_metadata(
                cluster.metadata,
                source_metadata(context, source_ref=None, extra=extra),
            )
            created_or_changed = (
                before_metadata != cluster.metadata
                or before_labels != cluster.cluster_labels
                or before_aliases != cluster.aliases
            )

        before_variants = [(variant.text, variant.source_edge_id) for variant in cluster.description_variants]
        for edge in related_edges:
            self._append_description_variant(cluster, edge)
        after_variants = [(variant.text, variant.source_edge_id) for variant in cluster.description_variants]
        return cluster, created_or_changed or before_variants != after_variants

    def _append_description_variant(self, cluster: EdgeCluster, edge: HyperEdge) -> None:
        text = edge.description.strip()
        if not text:
            return
        variant_key = (text, edge.edge_id)
        existing_keys = {
            (variant.text.strip(), variant.source_edge_id)
            for variant in cluster.description_variants
        }
        if variant_key in existing_keys:
            return
        cluster.description_variants.append(EdgeDescriptionVariant(text=text, source_edge_id=edge.edge_id))
        limit = max(1, self.config.edge_clusters.description_variants_limit)
        cluster.description_variants = cluster.description_variants[:limit]


@dataclass(frozen=True, order=True)
class AnchorKey:
    basis: str
    anchor_value: str


@dataclass(frozen=True)
class AnchorOccurrence:
    basis: str
    anchor_value: str
    edge_id: str
    node_id: str | None = None
    triple_id: str | None = None
    position: str | None = None
    subject: str | None = None
    object: str | None = None


def cluster_fingerprint_for_anchor(key: AnchorKey) -> str:
    return make_fingerprint(key.basis, {"anchor_value": key.anchor_value})


def cluster_fingerprint_for_shared_node(shared_node_id: str) -> str:
    return cluster_fingerprint_for_anchor(AnchorKey(basis="shared_node", anchor_value=shared_node_id))


def cluster_fingerprint_for_semantic_anchor(anchor_value: str) -> str:
    return cluster_fingerprint_for_anchor(AnchorKey(basis="semantic_anchor", anchor_value=anchor_value))


def _semantic_anchor_occurrences_from_nodes(
    edges: list[HyperEdge],
    nodes: list[MemoryNode],
) -> list[AnchorOccurrence]:
    nodes_by_id = {node.node_id: node for node in nodes}
    occurrences: list[AnchorOccurrence] = []
    for edge in edges:
        for node_id in edge.node_ids:
            node = nodes_by_id.get(node_id)
            if node is None:
                continue
            for triple in node.local_graph.triples:
                if triple.status != "active" or not triple.triple_id:
                    continue
                for position, value in (("subject", triple.subject), ("object", triple.object)):
                    anchor_value = normalize_text(value)
                    if not anchor_value:
                        continue
                    occurrences.append(
                        AnchorOccurrence(
                            basis="semantic_anchor",
                            anchor_value=anchor_value,
                            edge_id=edge.edge_id,
                            node_id=node.node_id,
                            triple_id=triple.triple_id,
                            position=position,
                            subject=triple.subject,
                            object=triple.object,
                        )
                    )
    return occurrences


def _anchor_key(occurrence: AnchorOccurrence) -> AnchorKey:
    return AnchorKey(basis=occurrence.basis, anchor_value=occurrence.anchor_value)


def _anchor_cluster_description(key: AnchorKey) -> str:
    if key.basis == "shared_node":
        return f"HyperEdges sharing node: {key.anchor_value}"
    if key.basis == "semantic_anchor":
        return f"HyperEdges sharing semantic anchor: {key.anchor_value}"
    return f"HyperEdges sharing anchor: {key.anchor_value}"


def _anchor_cluster_labels(key: AnchorKey) -> list[str]:
    if key.basis == "shared_node":
        return ["shared_node"]
    if key.basis == "semantic_anchor":
        return ["semantic_anchor"]
    return [compact_key(key.basis)]


def _anchor_metadata(
    key: AnchorKey,
    occurrence_payloads: list[dict[str, Any]],
    reasons: list[str],
) -> dict[str, Any]:
    metadata = {
        "cluster_basis": key.basis,
        "anchor_value": key.anchor_value,
        "anchor_occurrences": occurrence_payloads,
        "cluster_reasons": reasons,
    }
    if key.basis == "shared_node":
        metadata["shared_node_ids"] = [key.anchor_value]
    if key.basis == "semantic_anchor":
        metadata["anchor_positions"] = sorted(
            {str(payload["position"]) for payload in occurrence_payloads if payload.get("position")}
        )
    return metadata


def _unique_anchor_occurrences(occurrences: list[AnchorOccurrence]) -> list[AnchorOccurrence]:
    by_key = {
        (
            occurrence.basis,
            occurrence.anchor_value,
            occurrence.edge_id,
            occurrence.node_id,
            occurrence.triple_id,
            occurrence.position,
        ): occurrence
        for occurrence in occurrences
    }
    return [by_key[key] for key in sorted(by_key)]


def _anchor_occurrence_payload(occurrence: AnchorOccurrence) -> dict[str, Any]:
    payload = {
        "edge_id": occurrence.edge_id,
        "node_id": occurrence.node_id,
        "triple_id": occurrence.triple_id,
        "position": occurrence.position,
        "subject": occurrence.subject,
        "object": occurrence.object,
    }
    return {key: value for key, value in payload.items() if value not in (None, "")}


def _anchor_reasons(key: AnchorKey, occurrences: list[AnchorOccurrence]) -> list[str]:
    if key.basis == "shared_node":
        return ["shared_node"]
    if key.basis != "semantic_anchor":
        return [key.basis]

    positions_by_edge: dict[str, set[str]] = {}
    for occurrence in occurrences:
        if occurrence.position:
            positions_by_edge.setdefault(occurrence.edge_id, set()).add(occurrence.position)
    reasons = set()
    edge_ids = sorted(positions_by_edge)
    for left_index, left_edge_id in enumerate(edge_ids):
        for right_edge_id in edge_ids[left_index + 1:]:
            for left_position in positions_by_edge[left_edge_id]:
                for right_position in positions_by_edge[right_edge_id]:
                    reasons.add(f"{left_position}_{right_position}")
    return sorted(reasons)


def _eligible_semantic_anchor_occurrences_by_anchor(
    occurrences_by_anchor: dict[AnchorKey, list[AnchorOccurrence]],
) -> dict[AnchorKey, list[AnchorOccurrence]]:
    unique_by_anchor = {
        key: _unique_anchor_occurrences(occurrences)
        for key, occurrences in occurrences_by_anchor.items()
    }
    all_occurrences = [
        occurrence
        for occurrences in unique_by_anchor.values()
        for occurrence in occurrences
    ]
    by_pair: dict[tuple[str, str], list[tuple[str, AnchorOccurrence, AnchorOccurrence]]] = {}
    by_edge: dict[str, list[AnchorOccurrence]] = {}
    for occurrence in all_occurrences:
        if occurrence.position in {"subject", "object"}:
            by_edge.setdefault(occurrence.edge_id, []).append(occurrence)

    edge_ids = sorted(by_edge)
    for left_index, left_edge_id in enumerate(edge_ids):
        for right_edge_id in edge_ids[left_index + 1:]:
            pair_key = (left_edge_id, right_edge_id)
            for left_occurrence in by_edge[left_edge_id]:
                for right_occurrence in by_edge[right_edge_id]:
                    reason = f"{left_occurrence.position}_{right_occurrence.position}"
                    by_pair.setdefault(pair_key, []).append((reason, left_occurrence, right_occurrence))

    eligible_occurrences: dict[AnchorKey, dict[tuple[str, str | None, str | None], AnchorOccurrence]] = {}
    for pair_hits in by_pair.values():
        subject_cross_hits = [
            hit for hit in pair_hits if hit[0] in {"subject_object", "object_subject"}
        ]
        subject_subject_hits = [
            hit for hit in pair_hits
            if hit[0] == "subject_subject"
        ]
        subject_subject_anchor_values = {
            hit[1].anchor_value
            for hit in subject_subject_hits
        }
        eligible_hits = []
        if subject_cross_hits:
            eligible_hits.extend(subject_cross_hits)
        if len(subject_subject_anchor_values) >= 2:
            eligible_hits.extend(subject_subject_hits)
        for _, left_occurrence, right_occurrence in eligible_hits:
            for occurrence in (left_occurrence, right_occurrence):
                key = _anchor_key(occurrence)
                eligible_occurrences.setdefault(key, {})[
                    (occurrence.edge_id, occurrence.triple_id, occurrence.position)
                ] = occurrence

    return {
        key: [items[item_key] for item_key in sorted(items)]
        for key, items in eligible_occurrences.items()
    }


def _merge_anchor_metadata(existing: dict[str, Any], incoming: dict[str, Any]) -> dict[str, Any]:
    merged = deep_merge_dict(existing, incoming)
    merged["anchor_occurrences"] = _unique_dicts(
        [*_dicts(existing.get("anchor_occurrences")), *_dicts(incoming.get("anchor_occurrences"))],
        keys=("edge_id", "node_id", "triple_id", "position"),
    )
    merged["cluster_reasons"] = sorted(
        set(_strings(existing.get("cluster_reasons"))) | set(_strings(incoming.get("cluster_reasons")))
    )
    merged["anchor_positions"] = sorted(
        set(_strings(existing.get("anchor_positions"))) | set(_strings(incoming.get("anchor_positions")))
    )
    merged["shared_node_ids"] = list(
        dict.fromkeys([*_strings(existing.get("shared_node_ids")), *_strings(incoming.get("shared_node_ids"))])
    )
    return {key: value for key, value in merged.items() if value not in (None, [], {})}


def _dicts(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [dict(item) for item in value if isinstance(item, dict)]


def _strings(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item) for item in value if str(item)]


def _unique_dicts(items: list[dict[str, Any]], *, keys: tuple[str, ...]) -> list[dict[str, Any]]:
    by_key = {tuple(item.get(key) for key in keys): item for item in items}
    return [by_key[key] for key in sorted(by_key)]
