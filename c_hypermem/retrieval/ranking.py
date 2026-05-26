from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class EdgeRRFResult:
    edge_id: str
    score: float
    score_parts: dict[str, object]


def edge_level_rrf(
    *,
    track1_edge_ids: list[str],
    track2_edge_ids: list[str],
    k: int,
) -> list[EdgeRRFResult]:
    scores: dict[str, float] = {}
    score_parts: dict[str, dict[str, object]] = {}

    for rank, edge_id in enumerate(_unique(track1_edge_ids), start=1):
        contribution = 1.0 / (k + rank)
        scores[edge_id] = scores.get(edge_id, 0.0) + contribution
        parts = score_parts.setdefault(edge_id, {})
        parts["rrf_track1"] = contribution
        parts["track1_rank"] = rank

    for rank, edge_id in enumerate(_unique(track2_edge_ids), start=1):
        contribution = 1.0 / (k + rank)
        scores[edge_id] = scores.get(edge_id, 0.0) + contribution
        parts = score_parts.setdefault(edge_id, {})
        parts["rrf_track2"] = contribution
        parts["track2_rank"] = rank

    results = [
        EdgeRRFResult(
            edge_id=edge_id,
            score=score,
            score_parts={
                **score_parts.get(edge_id, {}),
                "edge_rrf_score": score,
            },
        )
        for edge_id, score in scores.items()
    ]
    return sorted(results, key=lambda item: item.score, reverse=True)


def _unique(values: list[str]) -> list[str]:
    return list(dict.fromkeys(value for value in values if value))
