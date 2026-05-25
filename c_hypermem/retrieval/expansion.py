from __future__ import annotations

from dataclasses import dataclass, field

from c_hypermem.config import RetrievalConfig
from c_hypermem.schema import HyperEdge, MemoryNode
from c_hypermem.stores.base import MemoryStore


@dataclass
class Candidate:
    node: MemoryNode
    score: float
    score_parts: dict[str, float] = field(default_factory=dict)
    edge_ids: set[str] = field(default_factory=set)


class EdgeExpansion:
    """Expand lexical candidates through incident HyperEdges."""

    def __init__(self, store: MemoryStore, config: RetrievalConfig) -> None:
        self.store = store
        self.config = config

    def expand(self, namespace: str, candidates: dict[str, Candidate]) -> None:
        seed_ids = list(candidates.keys())
        edges = self.store.get_incident_edges(namespace, seed_ids)[: self.config.edge_top_n]
        expanded_ids: list[str] = []
        for edge in edges:
            self._score_seed_members(edge, seed_ids, candidates)
            expanded_ids.extend(
                node_id
                for node_id in edge.node_ids
                if node_id not in candidates
            )

        expanded_nodes = self.store.get_nodes(namespace, list(dict.fromkeys(expanded_ids)))
        for node in expanded_nodes:
            candidate = candidates.setdefault(node.node_id, Candidate(node=node, score=0.0))
            incident = [edge for edge in edges if node.node_id in edge.node_ids]
            for edge in incident:
                candidate.edge_ids.add(edge.edge_id)
            expansion_score = 0.35 + min(len(candidate.edge_ids), 3) * 0.1
            candidate.score += expansion_score
            candidate.score_parts["edge_expansion"] = candidate.score_parts.get("edge_expansion", 0.0) + expansion_score

    def _score_seed_members(
        self,
        edge: HyperEdge,
        seed_ids: list[str],
        candidates: dict[str, Candidate],
    ) -> None:
        for seed_id in seed_ids:
            if seed_id not in edge.node_ids or seed_id not in candidates:
                continue
            candidates[seed_id].edge_ids.add(edge.edge_id)
            candidates[seed_id].score += 0.15
            candidates[seed_id].score_parts["edge_coherence"] = (
                candidates[seed_id].score_parts.get("edge_coherence", 0.0) + 0.15
            )
