from __future__ import annotations

from dataclasses import dataclass, field

from comgmem.schema import MemoryNode


@dataclass
class FusedNode:
    node: MemoryNode
    score: float
    channels: set[str]
    score_parts: dict[str, float]
    vector_hits: list[dict[str, object]]
    edge_ids: set[str]
    cluster_ids: set[str]


@dataclass(frozen=True)
class RankedNodeEntry:
    node: MemoryNode
    rank: int
    vector_hits: list[dict[str, object]] = field(default_factory=list)
    edge_ids: set[str] = field(default_factory=set)


@dataclass(frozen=True)
class RankedNodeList:
    nodes: list[MemoryNode]
    channel: str
    score_key: str
    entries: list[RankedNodeEntry] | None = None


def reciprocal_rank_fusion_channels(
    *,
    ranked_lists: list[RankedNodeList],
    vector_hit_payloads: dict[str, list[dict[str, object]]] | None = None,
    k: int,
) -> list[FusedNode]:
    fused: dict[str, FusedNode] = {}
    payloads = vector_hit_payloads or {}

    for ranked_list in ranked_lists:
        if ranked_list.entries is None:
            _add_ranked_list(
                fused,
                ranked_list.nodes,
                channel=ranked_list.channel,
                score_key=ranked_list.score_key,
                k=k,
            )
        else:
            _add_ranked_entries(
                fused,
                ranked_list.entries,
                channel=ranked_list.channel,
                score_key=ranked_list.score_key,
                k=k,
            )

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
            ),
        )
        score = 1.0 / (k + rank)
        _add_score(item, score=score, channel=channel, score_key=score_key)


def _add_ranked_entries(
    fused: dict[str, FusedNode],
    entries: list[RankedNodeEntry],
    *,
    channel: str,
    score_key: str,
    k: int,
) -> None:
    best_by_node_id: dict[str, RankedNodeEntry] = {}
    hits_by_node_id: dict[str, list[dict[str, object]]] = {}
    edge_ids_by_node_id: dict[str, set[str]] = {}
    for entry in entries:
        if entry.rank <= 0:
            continue
        node_id = entry.node.node_id
        best = best_by_node_id.get(node_id)
        if best is None or entry.rank < best.rank:
            best_by_node_id[node_id] = entry
        hits_by_node_id.setdefault(node_id, []).extend(entry.vector_hits)
        edge_ids_by_node_id.setdefault(node_id, set()).update(entry.edge_ids)

    for node_id, entry in best_by_node_id.items():
        item = fused.setdefault(
            node_id,
            FusedNode(
                node=entry.node,
                score=0.0,
                channels=set(),
                score_parts={},
                vector_hits=[],
                edge_ids=set(),
                cluster_ids=set(),
            ),
        )
        score = 1.0 / (k + entry.rank)
        _add_score(item, score=score, channel=channel, score_key=score_key)
        item.vector_hits.extend(hits_by_node_id[node_id])
        item.edge_ids.update(edge_ids_by_node_id[node_id])


def _add_score(
    item: FusedNode,
    *,
    score: float,
    channel: str,
    score_key: str,
) -> None:
    item.score += score
    item.channels.add(channel)
    item.score_parts[score_key] = item.score_parts.get(score_key, 0.0) + score
