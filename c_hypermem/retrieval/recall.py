from __future__ import annotations

from dataclasses import dataclass, field

from c_hypermem.config import RetrievalConfig
from c_hypermem.retrieval.context import compose_result_content
from c_hypermem.retrieval.query_analysis import QueryAnalyzer, QueryAnalysis
from c_hypermem.schema import HyperEdge, MemoryNode, SearchResult
from c_hypermem.stores.base import MemoryStore
from c_hypermem.stores.lexical_store import LexicalScorer
from c_hypermem.utils.time import decay_weight


@dataclass
class Candidate:
    node: MemoryNode
    score: float
    score_parts: dict[str, float] = field(default_factory=dict)
    edge_ids: set[str] = field(default_factory=set)
    edge_types: set[str] = field(default_factory=set)


class Retriever:
    def __init__(self, store: MemoryStore, config: RetrievalConfig) -> None:
        self.store = store
        self.config = config
        self.analyzer = QueryAnalyzer()
        self.lexical = LexicalScorer()

    def search(
        self,
        query: str,
        *,
        namespace: str,
        top_k: int,
        current_turn: int | None = None,
    ) -> list[SearchResult]:
        analysis = self.analyzer.analyze(query)
        nodes = self.store.list_nodes(namespace)
        scored = self.lexical.score(query, nodes)[: self.config.lexical_top_n]
        candidates: dict[str, Candidate] = {}

        for node, lexical_score, parts in scored:
            candidate = candidates.setdefault(node.node_id, Candidate(node=node, score=0.0))
            candidate.score += lexical_score
            candidate.score_parts.update(parts)
            self._apply_structural_scores(candidate, analysis, current_turn)

        if self.config.use_hyperedge_expansion and candidates:
            self._expand_candidates(namespace, candidates)

        preferred = self._prefer_answer_nodes(candidates.values(), analysis)
        ranked = sorted(preferred, key=lambda item: item.score, reverse=True)[:top_k]
        return [self._to_result(candidate) for candidate in ranked]

    def _expand_candidates(self, namespace: str, candidates: dict[str, Candidate]) -> None:
        seed_ids = list(candidates.keys())
        edges = self.store.get_incident_edges(namespace, seed_ids)[: self.config.edge_top_n]
        expanded_ids: list[str] = []
        for edge in edges:
            for seed_id in seed_ids:
                if seed_id in edge.node_ids and seed_id in candidates:
                    candidates[seed_id].edge_ids.add(edge.edge_id)
                    candidates[seed_id].edge_types.add(edge.edge_type)
                    candidates[seed_id].score += 0.15
                    candidates[seed_id].score_parts["edge_coherence"] = (
                        candidates[seed_id].score_parts.get("edge_coherence", 0.0) + 0.15
                    )
            expanded_ids.extend(
                node_id
                for node_id in edge.node_ids
                if node_id not in candidates and _expandable_role(edge, node_id)
            )

        expanded_nodes = self.store.get_nodes(namespace, list(dict.fromkeys(expanded_ids)))
        for node in expanded_nodes:
            candidate = candidates.setdefault(node.node_id, Candidate(node=node, score=0.0))
            incident = [edge for edge in edges if node.node_id in edge.node_ids]
            for edge in incident:
                candidate.edge_ids.add(edge.edge_id)
                candidate.edge_types.add(edge.edge_type)
            expansion_score = 0.35 + min(len(candidate.edge_types), 3) * 0.1
            candidate.score += expansion_score
            candidate.score_parts["edge_expansion"] = candidate.score_parts.get("edge_expansion", 0.0) + expansion_score

    def _apply_structural_scores(
        self,
        candidate: Candidate,
        analysis: QueryAnalysis,
        current_turn: int | None,
    ) -> None:
        node = candidate.node
        if analysis.asks_preference and _has_label(node, "preference"):
            candidate.score += 0.8
            candidate.score_parts["preference_match"] = 0.8
        if analysis.asks_task and _has_label(node, "task"):
            candidate.score += 0.8
            candidate.score_parts["task_match"] = 0.8
        if _has_label(node, "entity") and any(hint.lower() == node.content.lower() for hint in analysis.entity_hints):
            candidate.score += 0.5
            candidate.score_parts["entity_match"] = 0.5
        if analysis.time_hints:
            world = node.time.world
            haystack = " ".join(filter(None, [world.event_time, world.source_timestamp, node.metadata.get("date")]))
            if any(hint in haystack for hint in analysis.time_hints):
                candidate.score += 0.5
                candidate.score_parts["temporal_match"] = 0.5
        if self.config.use_recency_decay:
            decay = decay_weight(
                node.time.activation.inserted_turn,
                current_turn,
                self.config.recency_decay_lambda,
            )
            recency_bonus = 0.1 * decay
            candidate.score += recency_bonus
            candidate.score_parts["recency_bonus"] = recency_bonus
        if node.time.activation.access_count:
            access_bonus = min(0.3, self.config.access_boost * node.time.activation.access_count)
            candidate.score += access_bonus
            candidate.score_parts["access_boost"] = access_bonus

    def _prefer_answer_nodes(
        self,
        candidates: list[Candidate],
        analysis: QueryAnalysis,
    ) -> list[Candidate]:
        answer_types = {"fact", "preference", "task", "state", "event"}
        answer_candidates = [candidate for candidate in candidates if answer_types.intersection(candidate.node.node_labels)]
        if answer_candidates:
            return answer_candidates
        return list(candidates)

    def _to_result(self, candidate: Candidate) -> SearchResult:
        node = candidate.node
        metadata = {
            "node_labels": node.node_labels,
            "node_id": node.node_id,
            "source_session_id": node.metadata.get("source_session_id"),
            "source_event_id": node.metadata.get("source_event_id"),
            "source_turn_ids": node.metadata.get("source_turn_ids", []),
            "hyper_edge_ids": sorted(candidate.edge_ids),
            "edge_types": sorted(candidate.edge_types),
            "score_parts": candidate.score_parts,
            "time": node.time.model_dump(mode="json"),
            "node_metadata": node.metadata,
        }
        if node.local_graph.triples:
            metadata["triples"] = [triple.model_dump(mode="json") for triple in node.local_graph.triples[:5]]
        return SearchResult(
            id=node.node_id,
            content=compose_result_content(node, sorted(candidate.edge_types)),
            score=float(candidate.score),
            metadata=metadata,
        )


def _expandable_role(edge: HyperEdge, node_id: str) -> bool:
    role = edge.roles.get(node_id, "")
    return role in {
        "derived_fact",
        "state_fact",
        "evidence_event",
        "time_member",
        "topic_evidence",
        "preference_evidence",
    }


def _has_label(node: MemoryNode, label: str) -> bool:
    return label in node.node_labels
