from __future__ import annotations

from collections.abc import Mapping
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
    track1_tiebreak_scores: Mapping[str, float] | None = None,
    track2_tiebreak_scores: Mapping[str, float] | None = None,
) -> list[EdgeRRFResult]:
    scores: dict[str, float] = {}
    score_parts: dict[str, dict[str, object]] = {}
    track1_tiebreak = track1_tiebreak_scores or {}
    track2_tiebreak = track2_tiebreak_scores or {}

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
                "rrf_track1": 0.0,
                "rrf_track2": 0.0,
                **score_parts.get(edge_id, {}),
                "edge_rrf_score": score,
                "edge_rrf_tiebreak_track2_vector_score": float(track2_tiebreak.get(edge_id, 0.0)),
                "edge_rrf_tiebreak_track1_edge_score": float(track1_tiebreak.get(edge_id, 0.0)),
            },
        )
        for edge_id, score in scores.items()
    ]
    return sorted(
        results,
        key=lambda item: (
            -item.score,
            -float(track2_tiebreak.get(item.edge_id, 0.0)),
            -float(track1_tiebreak.get(item.edge_id, 0.0)),
            item.edge_id,
        ),
    )


def _unique(values: list[str]) -> list[str]:
    return list(dict.fromkeys(value for value in values if value))
