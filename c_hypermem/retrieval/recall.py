from __future__ import annotations

from c_hypermem.config import RetrievalConfig
from c_hypermem.llms.base import LLMClient
from c_hypermem.retrieval.context import compose_result_content
from c_hypermem.retrieval.expansion import Candidate, EdgeExpansion
from c_hypermem.retrieval.query_analysis import build_query_analyzer
from c_hypermem.schema import MemoryNode, SearchResult
from c_hypermem.stores.base import MemoryStore
from c_hypermem.stores.lexical_store import LexicalScorer
from c_hypermem.utils.time import decay_weight


class Retriever:
    def __init__(self, store: MemoryStore, config: RetrievalConfig, *, query_analysis_llm: LLMClient | None = None) -> None:
        self.store = store
        self.config = config
        self.analyzer = build_query_analyzer(config, llm=query_analysis_llm)
        self.lexical = LexicalScorer()
        self.expansion = EdgeExpansion(store, config)

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
            self._apply_access_scores(candidate, current_turn)

        if self.config.use_hyperedge_expansion and candidates:
            self.expansion.expand(namespace, candidates)

        ranked = sorted(candidates.values(), key=lambda item: item.score, reverse=True)[:top_k]
        return [self._to_result(candidate, analysis_metadata=analysis.to_metadata()) for candidate in ranked]

    def _apply_access_scores(
        self,
        candidate: Candidate,
        current_turn: int | None,
    ) -> None:
        node = candidate.node
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

    def _to_result(self, candidate: Candidate, *, analysis_metadata: dict) -> SearchResult:
        node = candidate.node
        metadata = {
            "node_labels": node.node_labels,
            "node_id": node.node_id,
            "query_analysis": analysis_metadata,
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
