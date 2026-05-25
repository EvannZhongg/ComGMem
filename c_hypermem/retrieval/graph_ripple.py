from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from c_hypermem.config import RetrievalConfig
from c_hypermem.retrieval.fusion import FusedNode
from c_hypermem.schema import EdgeCluster, HyperEdge, MemoryNode
from c_hypermem.stores.base import MemoryStore


@dataclass
class RankedEdge:
    edge: HyperEdge
    score: float
    nodes: list[FusedNode]
    score_parts: dict[str, float]
    cluster_ids: set[str]
    cluster_description_variants: list[dict[str, object]]
    hit_node_ids: set[str]


class GraphRippleExpansion:
    """Expand initial retrieval hits through HyperEdges and EdgeClusters."""

    def __init__(self, store: MemoryStore, config: RetrievalConfig) -> None:
        self.store = store
        self.config = config

    def expand(self, *, namespace: str, initial: list[FusedNode]) -> list[RankedEdge]:
        seeds = initial[: max(0, self.config.graph_seed_top_k)]
        if not seeds:
            return []

        by_node_id = {item.node.node_id: item for item in initial}
        seed_scores = {item.node.node_id: item.score for item in seeds}
        seed_ids = set(seed_scores)

        incident_edges = self.store.get_incident_edges(namespace, list(seed_ids))
        cluster_edges, clusters_by_edge = self._cluster_edges(namespace, incident_edges)
        edges_by_id = {edge.edge_id: edge for edge in [*incident_edges, *cluster_edges]}
        nodes_by_id = self._load_nodes(namespace, edges_by_id.values(), by_node_id)

        coherence_by_edge: dict[str, float] = {}
        hit_ids_by_edge: dict[str, set[str]] = {}
        clusters_by_edge_id: dict[str, list[EdgeCluster]] = {}

        for edge in incident_edges:
            hit_node_ids = seed_ids.intersection(edge.node_ids)
            hit_ids_by_edge[edge.edge_id] = hit_node_ids
            clusters = clusters_by_edge.get(edge.edge_id, [])
            clusters_by_edge_id[edge.edge_id] = clusters
            self._add_edge_members(
                edge,
                by_node_id=by_node_id,
                nodes_by_id=nodes_by_id,
                seed_scores=seed_scores,
                hit_node_ids=hit_node_ids,
                clusters=clusters,
            )
            coherence_by_edge[edge.edge_id] = self._edge_coherence(hit_node_ids, seed_scores)

        for edge in cluster_edges:
            hit_node_ids = seed_ids.intersection(edge.node_ids)
            hit_ids_by_edge[edge.edge_id] = hit_node_ids
            clusters = clusters_by_edge.get(edge.edge_id, [])
            clusters_by_edge_id[edge.edge_id] = clusters
            self._add_edge_members(
                edge,
                by_node_id=by_node_id,
                nodes_by_id=nodes_by_id,
                seed_scores=seed_scores,
                hit_node_ids=hit_node_ids,
                clusters=clusters,
            )
            coherence_by_edge[edge.edge_id] = self._edge_coherence(hit_node_ids, seed_scores)

        return self._rank_edges(
            edges=list(edges_by_id.values()),
            by_node_id=by_node_id,
            coherence_by_edge=coherence_by_edge,
            clusters_by_edge=clusters_by_edge_id,
            hit_ids_by_edge=hit_ids_by_edge,
        )

    def _add_edge_members(
        self,
        edge: HyperEdge,
        *,
        by_node_id: dict[str, FusedNode],
        nodes_by_id: dict[str, MemoryNode],
        seed_scores: dict[str, float],
        hit_node_ids: set[str],
        clusters: list[EdgeCluster],
    ) -> None:
        coherence = self._edge_coherence(hit_node_ids, seed_scores)
        for node_id in edge.node_ids:
            node = nodes_by_id.get(node_id)
            if node is None or node.status != "active":
                continue
            item = by_node_id.setdefault(
                node_id,
                FusedNode(
                    node=node,
                    score=0.0,
                    channels=set(),
                    score_parts={},
                    vector_hits=[],
                    edge_ids=set(),
                    cluster_ids=set(),
                    cluster_description_variants=[],
                ),
            )
            item.channels.add("graph")
            item.edge_ids.add(edge.edge_id)
            for cluster in clusters:
                item.cluster_ids.add(cluster.cluster_id)
                self._add_cluster_variants(item, cluster)
            if coherence > 0:
                item.score += coherence
                item.score_parts["edge_coherence"] = item.score_parts.get("edge_coherence", 0.0) + coherence

    def _edge_coherence(self, hit_node_ids: set[str], seed_scores: dict[str, float]) -> float:
        hit_count = len(hit_node_ids)
        if hit_count <= 1:
            return 0.0
        base_avg = sum(seed_scores[node_id] for node_id in hit_node_ids) / hit_count
        return (
            self.config.edge_coherence_alpha
            * max(0, hit_count - 1) ** self.config.edge_coherence_beta
            * base_avg
        )

    def _cluster_edges(
        self,
        namespace: str,
        incident_edges: list[HyperEdge],
    ) -> tuple[list[HyperEdge], dict[str, list[EdgeCluster]]]:
        edge_ids = [edge.edge_id for edge in incident_edges]
        clusters = self.store.get_edge_clusters_for_edges(namespace, edge_ids)
        if not clusters:
            return [], {}

        members = self.store.list_edge_cluster_members(namespace, [cluster.cluster_id for cluster in clusters])
        cluster_by_id = {cluster.cluster_id: cluster for cluster in clusters}
        clusters_by_edge: dict[str, list[EdgeCluster]] = {}
        for member in members:
            cluster = cluster_by_id.get(member.cluster_id)
            if cluster is not None:
                clusters_by_edge.setdefault(member.edge_id, []).append(cluster)

        all_cluster_edge_ids = list(dict.fromkeys(member.edge_id for member in members))
        cluster_edges = self.store.get_edges(namespace, all_cluster_edge_ids)
        incident_edge_ids = set(edge_ids)
        return [edge for edge in cluster_edges if edge.edge_id not in incident_edge_ids], clusters_by_edge

    def _load_nodes(
        self,
        namespace: str,
        edges: Iterable[HyperEdge],
        existing: dict[str, FusedNode],
    ) -> dict[str, MemoryNode]:
        node_ids: list[str] = []
        for edge in edges:
            node_ids.extend(edge.node_ids)
        unique_ids = list(dict.fromkeys(node_ids))
        loaded = {node.node_id: node for node in self.store.get_nodes(namespace, unique_ids)}
        for node_id, item in existing.items():
            loaded.setdefault(node_id, item.node)
        return loaded

    def _add_cluster_variants(self, item: FusedNode, cluster: EdgeCluster) -> None:
        existing = {
            (variant.get("cluster_id"), variant.get("text"), variant.get("source_edge_id"))
            for variant in item.cluster_description_variants
        }
        for variant in cluster.description_variants:
            payload = {
                "cluster_id": cluster.cluster_id,
                "text": variant.text,
                "source_edge_id": variant.source_edge_id,
            }
            key = (payload["cluster_id"], payload["text"], payload["source_edge_id"])
            if key in existing:
                continue
            existing.add(key)
            item.cluster_description_variants.append(payload)

    def _rank_edges(
        self,
        *,
        edges: list[HyperEdge],
        by_node_id: dict[str, FusedNode],
        coherence_by_edge: dict[str, float],
        clusters_by_edge: dict[str, list[EdgeCluster]],
        hit_ids_by_edge: dict[str, set[str]],
    ) -> list[RankedEdge]:
        ranked: list[RankedEdge] = []
        for edge in edges:
            nodes = [by_node_id[node_id] for node_id in edge.node_ids if node_id in by_node_id]
            if not nodes:
                continue
            member_scores = [node.score for node in nodes]
            score = max(member_scores)
            score_parts = {
                "edge_member_max": score,
                "edge_member_avg": sum(member_scores) / len(member_scores),
            }
            coherence = coherence_by_edge.get(edge.edge_id, 0.0)
            if coherence > 0:
                score_parts["edge_coherence"] = coherence
            cluster_ids: set[str] = set()
            variants: list[dict[str, object]] = []
            seen_variants: set[tuple[object, object, object]] = set()
            for cluster in clusters_by_edge.get(edge.edge_id, []):
                cluster_ids.add(cluster.cluster_id)
                for variant in cluster.description_variants:
                    payload = {
                        "cluster_id": cluster.cluster_id,
                        "text": variant.text,
                        "source_edge_id": variant.source_edge_id,
                    }
                    key = (payload["cluster_id"], payload["text"], payload["source_edge_id"])
                    if key in seen_variants:
                        continue
                    seen_variants.add(key)
                    variants.append(payload)
            ranked.append(
                RankedEdge(
                    edge=edge,
                    score=score,
                    nodes=sorted(nodes, key=lambda node: node.score, reverse=True),
                    score_parts=score_parts,
                    cluster_ids=cluster_ids,
                    cluster_description_variants=variants,
                    hit_node_ids=hit_ids_by_edge.get(edge.edge_id, set()),
                )
            )
        return sorted(ranked, key=lambda item: item.score, reverse=True)
