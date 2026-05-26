from __future__ import annotations

import pytest

from c_hypermem.config import RetrievalConfig
from c_hypermem.retrieval.fusion import FusedNode
from c_hypermem.retrieval.graph_ripple import GraphRippleExpansion, RankedEdge
from c_hypermem.retrieval.ranking import edge_level_rrf
from c_hypermem.schema import EdgeCluster, EdgeClusterMember, HyperEdge, MemoryNode


def test_edge_level_rrf_treats_missing_track_as_zero_and_sums_dual_hits():
    results = edge_level_rrf(
        track1_edge_ids=["edge:a"],
        track2_edge_ids=["edge:b", "edge:a"],
        k=10,
    )
    by_id = {item.edge_id: item for item in results}

    assert by_id["edge:a"].score == pytest.approx(1 / 11 + 1 / 12)
    assert by_id["edge:a"].score_parts["rrf_track1"] == pytest.approx(1 / 11)
    assert by_id["edge:a"].score_parts["rrf_track2"] == pytest.approx(1 / 12)
    assert by_id["edge:b"].score == pytest.approx(1 / 11)
    assert by_id["edge:b"].score_parts["rrf_track1"] == 0.0
    assert by_id["edge:b"].score_parts["rrf_track2"] == pytest.approx(1 / 11)


def test_edge_level_rrf_tie_breaks_by_track2_then_track1_then_edge_id():
    track2_first = edge_level_rrf(
        track1_edge_ids=["edge:a"],
        track2_edge_ids=["edge:b"],
        k=10,
        track1_tiebreak_scores={"edge:a": 0.99},
        track2_tiebreak_scores={"edge:b": 0.75},
    )
    assert [item.edge_id for item in track2_first] == ["edge:b", "edge:a"]

    track1_second = edge_level_rrf(
        track1_edge_ids=["edge:a"],
        track2_edge_ids=["edge:b"],
        k=10,
        track1_tiebreak_scores={"edge:a": 0.99, "edge:b": 0.01},
        track2_tiebreak_scores={"edge:a": 0.5, "edge:b": 0.5},
    )
    assert [item.edge_id for item in track1_second] == ["edge:a", "edge:b"]

    deterministic_final = edge_level_rrf(
        track1_edge_ids=["edge:z"],
        track2_edge_ids=["edge:a"],
        k=10,
        track1_tiebreak_scores={"edge:z": 0.0},
        track2_tiebreak_scores={"edge:a": 0.0},
    )
    assert [item.edge_id for item in deterministic_final] == ["edge:a", "edge:z"]


def test_track1_edge_coherence_counts_unique_node_ids():
    node_a = _node("node:andrew", "Andrew")
    node_b = _node("node:project", "Project Atlas")
    edge = HyperEdge(
        edge_id="edge:shared",
        namespace="ns",
        edge_fingerprint="fp:shared",
        description="Andrew and Project Atlas.",
        node_ids=[node_a.node_id, node_b.node_id],
    )
    expansion = GraphRippleExpansion(
        _MemoryStore(nodes=[node_a, node_b], edges=[edge]),
        RetrievalConfig(edge_coherence_alpha=0.5, edge_coherence_beta=2.0),
    )

    ranked = expansion.rank_track1_edges(
        namespace="ns",
        node_ranking=[
            _fused(node_a, 0.50),
            _fused(node_a, 0.40),
            _fused(node_b, 0.30),
        ],
    )

    assert len(ranked) == 1
    assert ranked[0].score_parts["track1_hit_node_count"] == 2
    assert ranked[0].score_parts["track1_base_node_score"] == pytest.approx(0.50)
    assert ranked[0].score_parts["edge_coherence_multiplier"] == pytest.approx(1.5)


def test_cluster_periphery_limits_are_configured():
    core = _node("node:core", "Core")
    sibling_a = _node("node:sibling-a", "Sibling A")
    sibling_b = _node("node:sibling-b", "Sibling B")
    sibling_c = _node("node:sibling-c", "Sibling C")
    core_edge = _edge("edge:core", [core.node_id])
    sibling_edge_1 = _edge("edge:sibling-1", [core.node_id, sibling_a.node_id, sibling_b.node_id])
    sibling_edge_2 = _edge("edge:sibling-2", [core.node_id, sibling_c.node_id])
    cluster = EdgeCluster(
        cluster_id="cluster:shared",
        namespace="ns",
        cluster_fingerprint="fp:cluster",
        canonical_description="Shared core node",
    )
    members = [
        EdgeClusterMember(namespace="ns", cluster_id=cluster.cluster_id, edge_id=edge.edge_id)
        for edge in [core_edge, sibling_edge_1, sibling_edge_2]
    ]
    expansion = GraphRippleExpansion(
        _MemoryStore(
            nodes=[core, sibling_a, sibling_b, sibling_c],
            edges=[core_edge, sibling_edge_1, sibling_edge_2],
            clusters=[cluster],
            members=members,
        ),
        RetrievalConfig(cluster_periphery_edge_limit=1, cluster_periphery_node_limit=1),
    )

    ranked = expansion.attach_cluster_periphery(
        namespace="ns",
        ranked_edges=[
            RankedEdge(
                edge=core_edge,
                score=1.0,
                nodes=[_fused(core, 1.0)],
                score_parts={},
            )
        ],
    )

    assert [edge["edge_id"] for edge in ranked[0].periphery_edges] == ["edge:sibling-1"]
    assert [node.node.node_id for node in ranked[0].periphery_nodes] == ["node:sibling-a"]


def _node(node_id: str, content: str) -> MemoryNode:
    return MemoryNode(
        node_id=node_id,
        namespace="ns",
        canonical_text=content,
        normalized_text=content.lower(),
        fingerprint=f"fp:{node_id}",
        content=content,
    )


def _edge(edge_id: str, node_ids: list[str]) -> HyperEdge:
    return HyperEdge(
        edge_id=edge_id,
        namespace="ns",
        edge_fingerprint=f"fp:{edge_id}",
        description=edge_id,
        node_ids=node_ids,
    )


def _fused(node: MemoryNode, score: float) -> FusedNode:
    return FusedNode(
        node=node,
        score=score,
        channels={"lexical"},
        score_parts={"rrf_lexical": score},
        vector_hits=[],
        edge_ids=set(),
        cluster_ids=set(),
    )


class _MemoryStore:
    def __init__(
        self,
        *,
        nodes: list[MemoryNode],
        edges: list[HyperEdge],
        clusters: list[EdgeCluster] | None = None,
        members: list[EdgeClusterMember] | None = None,
    ) -> None:
        self.nodes = {node.node_id: node for node in nodes}
        self.edges = {edge.edge_id: edge for edge in edges}
        self.clusters = {cluster.cluster_id: cluster for cluster in clusters or []}
        self.members = members or []

    def get_incident_edges(self, namespace: str, node_ids: list[str]) -> list[HyperEdge]:
        node_id_set = set(node_ids)
        return [
            edge
            for edge in self.edges.values()
            if edge.namespace == namespace and node_id_set.intersection(edge.node_ids)
        ]

    def get_nodes(self, namespace: str, node_ids: list[str]) -> list[MemoryNode]:
        return [
            self.nodes[node_id]
            for node_id in node_ids
            if node_id in self.nodes and self.nodes[node_id].namespace == namespace
        ]

    def get_edge_clusters_for_edges(self, namespace: str, edge_ids: list[str]) -> list[EdgeCluster]:
        edge_id_set = set(edge_ids)
        cluster_ids = {
            member.cluster_id
            for member in self.members
            if member.namespace == namespace and member.edge_id in edge_id_set
        }
        return [self.clusters[cluster_id] for cluster_id in cluster_ids if cluster_id in self.clusters]

    def list_edge_cluster_members(
        self,
        namespace: str,
        cluster_ids: list[str] | None = None,
    ) -> list[EdgeClusterMember]:
        cluster_id_set = set(cluster_ids or [])
        return [
            member
            for member in self.members
            if member.namespace == namespace and (not cluster_id_set or member.cluster_id in cluster_id_set)
        ]

    def get_edges(self, namespace: str, edge_ids: list[str]) -> list[HyperEdge]:
        return [
            self.edges[edge_id]
            for edge_id in edge_ids
            if edge_id in self.edges and self.edges[edge_id].namespace == namespace
        ]
