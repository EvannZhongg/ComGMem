from __future__ import annotations

from typing import Any, Protocol

from c_hypermem.config import MemoryConfig
from c_hypermem.pipeline.context import AssemblyContext
from c_hypermem.pipeline.graph_utils import deep_merge_dict, source_metadata
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
    """Attach concrete HyperEdges to shared-node clusters."""

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
        edges_by_shared_node: dict[str, dict[str, HyperEdge]] = {}
        for edge in edges:
            for node_id in edge.node_ids:
                edges_by_shared_node.setdefault(node_id, {})[edge.edge_id] = edge
        if self.store is not None:
            incident_edges = self.store.get_incident_edges(namespace, sorted(edges_by_shared_node))
            for edge in incident_edges:
                for node_id in edge.node_ids:
                    if node_id in edges_by_shared_node:
                        edges_by_shared_node[node_id][edge.edge_id] = edge

        clusters_by_fingerprint: dict[str, EdgeCluster] = {}
        changed_clusters_by_id: dict[str, EdgeCluster] = {}
        members_by_key: dict[tuple[str, str], EdgeClusterMember] = {}
        for shared_node_id in sorted(edges_by_shared_node):
            related_edges = edges_by_shared_node[shared_node_id]
            if len(related_edges) < 2:
                continue
            cluster, created_or_changed = self._get_or_create_shared_node_cluster(
                shared_node_id,
                sorted(related_edges.values(), key=lambda item: item.edge_id),
                context,
                clusters_by_fingerprint,
            )
            if created_or_changed:
                changed_clusters_by_id[cluster.cluster_id] = cluster
            clusters_by_fingerprint[cluster.cluster_fingerprint] = cluster
            for edge_id in sorted(related_edges):
                members_by_key[(cluster.cluster_id, edge_id)] = EdgeClusterMember(
                    namespace=context.namespace,
                    cluster_id=cluster.cluster_id,
                    edge_id=edge_id,
                    relation_to_cluster="shared_node",
                )
        members = list(members_by_key.values())
        return list(changed_clusters_by_id.values()), members

    def build_for_edge(self, edge: HyperEdge, context: AssemblyContext) -> tuple[EdgeCluster, EdgeClusterMember]:
        cluster, _ = self._get_or_create_shared_node_cluster(edge.node_ids[0], [edge], context, {})
        member = EdgeClusterMember(
            namespace=context.namespace,
            cluster_id=cluster.cluster_id,
            edge_id=edge.edge_id,
            relation_to_cluster="shared_node",
        )
        return cluster, member

    def _get_or_create_shared_node_cluster(
        self,
        shared_node_id: str,
        related_edges: list[HyperEdge],
        context: AssemblyContext,
        batch_clusters: dict[str, EdgeCluster],
    ) -> tuple[EdgeCluster, bool]:
        label = "shared_node"
        cluster_fingerprint = cluster_fingerprint_for_shared_node(shared_node_id)
        cluster_description = f"HyperEdges sharing node: {shared_node_id}"
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
                cluster_labels=[label],
                aliases=[compact_key(shared_node_id)],
                conflict_state="none",
                description_variants=[],
                metadata=source_metadata(
                    context,
                    source_ref=None,
                    extra={"shared_node_ids": [shared_node_id]},
                ),
            )
            created_or_changed = True
        else:
            cluster.canonical_description = cluster_description
            cluster.cluster_labels = list(dict.fromkeys([*cluster.cluster_labels, label]))
            cluster.metadata = deep_merge_dict(
                cluster.metadata,
                source_metadata(
                    context,
                    source_ref=None,
                    extra={"shared_node_ids": [shared_node_id]},
                ),
            )
            shared_node_ids = list(cluster.metadata.get("shared_node_ids") or [])
            if shared_node_id not in shared_node_ids:
                shared_node_ids.append(shared_node_id)
                cluster.metadata["shared_node_ids"] = shared_node_ids
                created_or_changed = True
        before = [(variant.text, variant.source_edge_id) for variant in cluster.description_variants]
        for edge in related_edges:
            self._append_description_variant(cluster, edge)
        after = [(variant.text, variant.source_edge_id) for variant in cluster.description_variants]
        return cluster, created_or_changed or before != after

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


def cluster_fingerprint_for_shared_node(shared_node_id: str) -> str:
    return make_fingerprint("shared_node", {"shared_node_id": shared_node_id})
