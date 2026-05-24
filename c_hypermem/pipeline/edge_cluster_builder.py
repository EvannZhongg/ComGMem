from __future__ import annotations

from typing import Any, Protocol

from c_hypermem.config import MemoryConfig
from c_hypermem.pipeline.context import AssemblyContext
from c_hypermem.pipeline.graph_utils import source_metadata
from c_hypermem.schema import EdgeCluster, EdgeClusterMember, EdgeDescriptionVariant, HyperEdge
from c_hypermem.stores.base import MemoryStore
from c_hypermem.utils.ids import make_cluster_id, make_fingerprint
from c_hypermem.utils.text import compact_key


class EdgeClusterBuilder(Protocol):
    """Builds related EdgeClusters without forcing HyperEdge merges."""

    def build(
        self,
        edges: list[HyperEdge],
        *,
        namespace: str,
        metadata: dict[str, Any],
        current_turn: int,
    ) -> tuple[list[EdgeCluster], list[EdgeClusterMember]]: ...


class BasicEdgeClusterBuilder:
    """Attach concrete HyperEdges to stable topic clusters."""

    def __init__(self, config: MemoryConfig, store: MemoryStore | None = None) -> None:
        self.config = config
        self.store = store

    def build(
        self,
        edges: list[HyperEdge],
        *,
        namespace: str,
        metadata: dict[str, Any],
        current_turn: int,
    ) -> tuple[list[EdgeCluster], list[EdgeClusterMember]]:
        context = AssemblyContext(namespace=namespace, metadata=metadata, current_turn=current_turn)
        clusters_by_fingerprint: dict[str, EdgeCluster] = {}
        new_clusters_by_id: dict[str, EdgeCluster] = {}
        members: list[EdgeClusterMember] = []
        for edge in edges:
            cluster, member, created = self._build_for_edge(edge, context, clusters_by_fingerprint)
            if created:
                new_clusters_by_id[cluster.cluster_id] = cluster
            clusters_by_fingerprint[cluster.cluster_fingerprint] = cluster
            members.append(member)
        return list(new_clusters_by_id.values()), members

    def build_for_edge(self, edge: HyperEdge, context: AssemblyContext) -> tuple[EdgeCluster, EdgeClusterMember]:
        cluster, member, _ = self._build_for_edge(edge, context, {})
        return cluster, member

    def _build_for_edge(
        self,
        edge: HyperEdge,
        context: AssemblyContext,
        batch_clusters: dict[str, EdgeCluster],
    ) -> tuple[EdgeCluster, EdgeClusterMember, bool]:
        label = edge_cluster_label(edge)
        cluster_description = cluster_description_for_edge(edge)
        cluster_fingerprint = cluster_fingerprint_for_edge(edge, label, cluster_description)
        cluster, created = self._get_or_create_cluster(
            edge,
            context,
            label,
            cluster_description,
            cluster_fingerprint,
            batch_clusters,
        )
        member = EdgeClusterMember(
            namespace=context.namespace,
            cluster_id=cluster.cluster_id,
            edge_id=edge.edge_id,
            relation_to_cluster="updates" if edge.edge_type == "correction" else "supports",
        )
        return cluster, member, created

    def _get_or_create_cluster(
        self,
        edge: HyperEdge,
        context: AssemblyContext,
        label: str,
        cluster_description: str,
        cluster_fingerprint: str,
        batch_clusters: dict[str, EdgeCluster],
    ) -> tuple[EdgeCluster, bool]:
        batch_cluster = batch_clusters.get(cluster_fingerprint)
        if batch_cluster is not None:
            self._append_description_variant(batch_cluster, edge)
            return batch_cluster, False
        if self.store is not None:
            existing = self.store.find_edge_cluster_by_fingerprint(context.namespace, cluster_fingerprint)
            if existing is not None:
                self._append_description_variant(existing, edge)
                return existing, True
        cluster = EdgeCluster(
            cluster_id=make_cluster_id(context.namespace, cluster_fingerprint),
            namespace=context.namespace,
            cluster_fingerprint=cluster_fingerprint,
            canonical_description=cluster_description,
            cluster_labels=[label],
            aliases=[compact_key(cluster_description)] if compact_key(cluster_description) else [],
            conflict_state="contains_conflict" if edge.edge_type == "correction" else "none",
            description_variants=[EdgeDescriptionVariant(text=edge.description, source_edge_id=edge.edge_id)],
            metadata=source_metadata(context, source_ref=edge.edge_type),
        )
        return cluster, True

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


def cluster_fingerprint_for_edge(edge: HyperEdge, label: str, cluster_description: str) -> str:
    hint = edge.metadata.get("cluster_hint")
    if isinstance(hint, dict) and hint:
        return make_fingerprint(
            str(hint.get("kind") or label),
            {
                "cluster_label": label,
                "subject_node_id": hint.get("subject_node_id"),
                "predicate": hint.get("predicate"),
            },
        )
    return make_fingerprint(cluster_description, {"cluster_label": label})


def edge_cluster_label(edge: HyperEdge) -> str:
    if edge.edge_type == "state":
        return "entity_state"
    if edge.edge_type == "correction":
        return "conflict_resolution"
    return f"{edge.edge_type}_context"


def cluster_description_for_edge(edge: HyperEdge) -> str:
    if edge.edge_type == "state":
        return edge.description
    return f"{edge.edge_type}: {edge.relation}"
