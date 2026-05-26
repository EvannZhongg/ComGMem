from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field

from c_hypermem.config import RetrievalConfig
from c_hypermem.retrieval.fusion import FusedNode
from c_hypermem.schema import EdgeCluster, EdgeClusterMember, HyperEdge, MemoryNode
from c_hypermem.stores.base import MemoryStore


@dataclass
class Track1RankedEdge:
    edge: HyperEdge
    score: float
    nodes: list[FusedNode]
    score_parts: dict[str, object]
    hit_node_ids: set[str]


@dataclass
class RankedEdge:
    edge: HyperEdge
    score: float
    nodes: list[FusedNode]
    score_parts: dict[str, object]
    cluster_ids: set[str] = field(default_factory=set)
    cluster_edge_descriptions: list[dict[str, object]] = field(default_factory=list)
    hit_node_ids: set[str] = field(default_factory=set)
    edge_vector_hits: list[dict[str, object]] = field(default_factory=list)
    periphery_edges: list[dict[str, object]] = field(default_factory=list)
    periphery_nodes: list[FusedNode] = field(default_factory=list)


class GraphRippleExpansion:
    """Rank node-derived edges and attach bounded cluster periphery."""

    def __init__(self, store: MemoryStore, config: RetrievalConfig) -> None:
        self.store = store
        self.config = config

    def rank_track1_edges(self, *, namespace: str, node_ranking: list[FusedNode]) -> list[Track1RankedEdge]:
        seeds = node_ranking[: max(0, self.config.graph_seed_top_k)]
        if not seeds:
            return []

        seed_scores = {item.node.node_id: item.score for item in seeds}
        seed_ids = set(seed_scores)
        incident_edges = [
            edge
            for edge in self.store.get_incident_edges(namespace, list(seed_ids))
            if edge.status == "active"
        ]
        if not incident_edges:
            return []

        by_node_id = {item.node.node_id: item for item in node_ranking}
        nodes_by_id = self._load_nodes(namespace, incident_edges, by_node_id)
        ranked: list[Track1RankedEdge] = []
        for edge in incident_edges:
            hit_node_ids = seed_ids.intersection(edge.node_ids)
            if not hit_node_ids:
                continue
            base_score = max(seed_scores[node_id] for node_id in hit_node_ids)
            hit_count = len(hit_node_ids)
            multiplier = self._edge_coherence_multiplier(hit_count)
            edge_score = base_score * multiplier
            score_parts: dict[str, object] = {
                "track1_edge_score": edge_score,
                "track1_base_node_score": base_score,
                "track1_hit_node_count": hit_count,
                "edge_coherence_multiplier": multiplier,
            }
            if multiplier > 1.0:
                score_parts["edge_coherence_bonus"] = edge_score - base_score
            ranked.append(
                Track1RankedEdge(
                    edge=edge,
                    score=edge_score,
                    nodes=self._edge_member_nodes(edge, by_node_id=by_node_id, nodes_by_id=nodes_by_id),
                    score_parts=score_parts,
                    hit_node_ids=hit_node_ids,
                )
            )
        return sorted(ranked, key=lambda item: item.score, reverse=True)

    def attach_cluster_periphery(self, *, namespace: str, ranked_edges: list[RankedEdge]) -> list[RankedEdge]:
        core_edge_ids = [item.edge.edge_id for item in ranked_edges]
        clusters = self.store.get_edge_clusters_for_edges(namespace, core_edge_ids)
        if not clusters:
            return ranked_edges

        members = self.store.list_edge_cluster_members(namespace, [cluster.cluster_id for cluster in clusters])
        cluster_by_id = {cluster.cluster_id: cluster for cluster in clusters if cluster.status == "active"}
        clusters_by_core_edge = _clusters_by_core_edge(members, cluster_by_id, set(core_edge_ids))
        all_cluster_edge_ids = list(dict.fromkeys(member.edge_id for member in members if member.status == "active"))
        cluster_edges = [
            edge
            for edge in self.store.get_edges(namespace, all_cluster_edge_ids)
            if edge.status == "active"
        ]
        edges_by_id = {edge.edge_id: edge for edge in [*(item.edge for item in ranked_edges), *cluster_edges]}
        descriptions_by_edge = _cluster_edge_descriptions(members, edges_by_id)
        periphery_edge_payloads = _periphery_edges_by_core_edge(
            members,
            edges_by_id,
            clusters_by_core_edge,
            set(core_edge_ids),
        )
        periphery_nodes_by_core_edge = self._periphery_nodes_by_core_edge(
            namespace,
            ranked_edges,
            periphery_edge_payloads,
            edges_by_id,
        )

        enriched: list[RankedEdge] = []
        for item in ranked_edges:
            attached_clusters = clusters_by_core_edge.get(item.edge.edge_id, [])
            cluster_ids = {cluster.cluster_id for cluster in attached_clusters}
            enriched.append(
                RankedEdge(
                    edge=item.edge,
                    score=item.score,
                    nodes=_with_cluster_ids(item.nodes, cluster_ids),
                    score_parts=item.score_parts,
                    cluster_ids=cluster_ids,
                    cluster_edge_descriptions=descriptions_by_edge.get(item.edge.edge_id, []),
                    hit_node_ids=item.hit_node_ids,
                    edge_vector_hits=item.edge_vector_hits,
                    periphery_edges=periphery_edge_payloads.get(item.edge.edge_id, []),
                    periphery_nodes=periphery_nodes_by_core_edge.get(item.edge.edge_id, []),
                )
            )
        return enriched

    def materialize_edge_nodes(
        self,
        *,
        namespace: str,
        edge: HyperEdge,
        existing_nodes: list[FusedNode] | None = None,
    ) -> list[FusedNode]:
        by_node_id = {item.node.node_id: item for item in existing_nodes or []}
        nodes_by_id = self._load_nodes(namespace, [edge], by_node_id)
        return self._edge_member_nodes(edge, by_node_id=by_node_id, nodes_by_id=nodes_by_id)

    def _edge_coherence_multiplier(self, hit_count: int) -> float:
        if hit_count <= 1:
            return 1.0
        return 1.0 + self.config.edge_coherence_alpha * max(0, hit_count - 1) ** self.config.edge_coherence_beta

    def _edge_member_nodes(
        self,
        edge: HyperEdge,
        *,
        by_node_id: dict[str, FusedNode],
        nodes_by_id: dict[str, MemoryNode],
    ) -> list[FusedNode]:
        members: list[FusedNode] = []
        for node_id in edge.node_ids:
            node = nodes_by_id.get(node_id)
            if node is None or node.status != "active":
                continue
            item = by_node_id.get(node_id)
            if item is None:
                item = FusedNode(
                    node=node,
                    score=0.0,
                    channels=set(),
                    score_parts={},
                    vector_hits=[],
                    edge_ids={edge.edge_id},
                    cluster_ids=set(),
                )
            else:
                item = _clone_fused_node(item)
                item.edge_ids.add(edge.edge_id)
            members.append(item)
        return sorted(members, key=lambda item: item.score, reverse=True)

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

    def _periphery_nodes_by_core_edge(
        self,
        namespace: str,
        ranked_edges: list[RankedEdge],
        periphery_edges_by_core: dict[str, list[dict[str, object]]],
        edges_by_id: dict[str, HyperEdge],
    ) -> dict[str, list[FusedNode]]:
        all_periphery_edge_ids = list(
            dict.fromkeys(
                str(payload["edge_id"])
                for payloads in periphery_edges_by_core.values()
                for payload in payloads
                if payload.get("edge_id")
            )
        )
        periphery_edges = [edges_by_id[edge_id] for edge_id in all_periphery_edge_ids if edge_id in edges_by_id]
        nodes_by_id = self._load_nodes(namespace, periphery_edges, {})
        core_node_ids_by_edge = {item.edge.edge_id: set(item.edge.node_ids) for item in ranked_edges}

        result: dict[str, list[FusedNode]] = {}
        for core_edge_id, payloads in periphery_edges_by_core.items():
            core_node_ids = core_node_ids_by_edge.get(core_edge_id, set())
            seen_nodes: set[str] = set()
            items: list[FusedNode] = []
            for payload in payloads:
                edge = edges_by_id.get(str(payload.get("edge_id") or ""))
                if edge is None:
                    continue
                for node_id in edge.node_ids:
                    if node_id in core_node_ids or node_id in seen_nodes:
                        continue
                    node = nodes_by_id.get(node_id)
                    if node is None or node.status != "active":
                        continue
                    seen_nodes.add(node_id)
                    items.append(
                        FusedNode(
                            node=node,
                            score=0.0,
                            channels={"cluster_periphery"},
                            score_parts={},
                            vector_hits=[],
                            edge_ids={edge.edge_id},
                            cluster_ids=set(),
                        )
                    )
            result[core_edge_id] = items
        return result


def _clusters_by_core_edge(
    members: list[EdgeClusterMember],
    cluster_by_id: dict[str, EdgeCluster],
    core_edge_ids: set[str],
) -> dict[str, list[EdgeCluster]]:
    clusters_by_edge: dict[str, list[EdgeCluster]] = {}
    for member in members:
        if member.edge_id not in core_edge_ids or member.status != "active":
            continue
        cluster = cluster_by_id.get(member.cluster_id)
        if cluster is not None:
            clusters_by_edge.setdefault(member.edge_id, []).append(cluster)
    return clusters_by_edge


def _periphery_edges_by_core_edge(
    members: list[EdgeClusterMember],
    edges_by_id: dict[str, HyperEdge],
    clusters_by_core_edge: dict[str, list[EdgeCluster]],
    core_edge_ids: set[str],
) -> dict[str, list[dict[str, object]]]:
    edge_ids_by_cluster: dict[str, list[str]] = {}
    for member in members:
        if member.status != "active":
            continue
        edge_ids_by_cluster.setdefault(member.cluster_id, []).append(member.edge_id)

    result: dict[str, list[dict[str, object]]] = {}
    for core_edge_id, clusters in clusters_by_core_edge.items():
        seen: set[tuple[str, str]] = set()
        payloads: list[dict[str, object]] = []
        for cluster in clusters:
            for related_edge_id in edge_ids_by_cluster.get(cluster.cluster_id, []):
                if related_edge_id in core_edge_ids:
                    continue
                edge = edges_by_id.get(related_edge_id)
                if edge is None or edge.status != "active":
                    continue
                key = (cluster.cluster_id, edge.edge_id)
                if key in seen:
                    continue
                seen.add(key)
                payloads.append(
                    {
                        "cluster_id": cluster.cluster_id,
                        "edge_id": edge.edge_id,
                        "description": edge.description,
                        "node_ids": edge.node_ids,
                        "edge_metadata": edge.metadata,
                    }
                )
        result[core_edge_id] = payloads
    return result


def _cluster_edge_descriptions(
    members: list[EdgeClusterMember],
    edges_by_id: dict[str, HyperEdge],
) -> dict[str, list[dict[str, object]]]:
    edge_ids_by_cluster: dict[str, list[str]] = {}
    cluster_ids_by_edge: dict[str, list[str]] = {}
    for member in members:
        if member.status != "active":
            continue
        cluster_id = member.cluster_id
        edge_id = member.edge_id
        edge_ids_by_cluster.setdefault(cluster_id, []).append(edge_id)
        cluster_ids_by_edge.setdefault(edge_id, []).append(cluster_id)

    descriptions_by_edge: dict[str, list[dict[str, object]]] = {}
    for edge_id, cluster_ids in cluster_ids_by_edge.items():
        seen: set[tuple[object, object, object]] = set()
        payloads: list[dict[str, object]] = []
        for cluster_id in cluster_ids:
            for related_edge_id in edge_ids_by_cluster.get(cluster_id, []):
                related_edge = edges_by_id.get(related_edge_id)
                if related_edge is None or related_edge.status != "active" or not related_edge.description.strip():
                    continue
                payload = {
                    "cluster_id": cluster_id,
                    "edge_id": related_edge.edge_id,
                    "description": related_edge.description,
                }
                key = (payload["cluster_id"], payload["edge_id"], payload["description"])
                if key in seen:
                    continue
                seen.add(key)
                payloads.append(payload)
        descriptions_by_edge[edge_id] = payloads
    return descriptions_by_edge


def _clone_fused_node(item: FusedNode) -> FusedNode:
    return FusedNode(
        node=item.node,
        score=item.score,
        channels=set(item.channels),
        score_parts=dict(item.score_parts),
        vector_hits=list(item.vector_hits),
        edge_ids=set(item.edge_ids),
        cluster_ids=set(item.cluster_ids),
    )


def _with_cluster_ids(nodes: list[FusedNode], cluster_ids: set[str]) -> list[FusedNode]:
    if not cluster_ids:
        return nodes
    enriched: list[FusedNode] = []
    for node in nodes:
        item = _clone_fused_node(node)
        item.cluster_ids.update(cluster_ids)
        enriched.append(item)
    return enriched
