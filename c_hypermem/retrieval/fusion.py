from __future__ import annotations

from dataclasses import dataclass

from c_hypermem.schema import MemoryNode


RRF_K = 60


@dataclass
class FusedNode:
    node: MemoryNode
    score: float
    channels: set[str]
    score_parts: dict[str, float]
    vector_hits: list[dict[str, object]]
    edge_ids: set[str]
    cluster_ids: set[str]
    cluster_description_variants: list[dict[str, object]]


def reciprocal_rank_fusion(
    *,
    lexical_nodes: list[MemoryNode],
    vector_nodes: list[MemoryNode],
    vector_hit_payloads: dict[str, list[dict[str, object]]] | None = None,
    k: int = RRF_K,
) -> list[FusedNode]:
    fused: dict[str, FusedNode] = {}
    payloads = vector_hit_payloads or {}

    _add_ranked_list(fused, lexical_nodes, channel="lexical", score_key="rrf_lexical", k=k)
    _add_ranked_list(fused, vector_nodes, channel="vector", score_key="rrf_vector", k=k)

    for node_id, hits in payloads.items():
        if node_id in fused:
            fused[node_id].vector_hits.extend(hits)

    return sorted(fused.values(), key=lambda item: item.score, reverse=True)


def _add_ranked_list(
    fused: dict[str, FusedNode],
    nodes: list[MemoryNode],
    *,
    channel: str,
    score_key: str,
    k: int,
) -> None:
    seen: set[str] = set()
    for rank, node in enumerate(nodes, start=1):
        if node.node_id in seen:
            continue
        seen.add(node.node_id)
        item = fused.setdefault(
            node.node_id,
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
        score = 1.0 / (k + rank)
        item.score += score
        item.channels.add(channel)
        item.score_parts[score_key] = score
