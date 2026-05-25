from __future__ import annotations

from c_hypermem.config import NLPConfig, RetrievalConfig
from c_hypermem.embeddings import EmbeddingClient
from c_hypermem.llms.base import LLMClient
from c_hypermem.retrieval.context import compose_result_content
from c_hypermem.retrieval.fusion import FusedNode, reciprocal_rank_fusion
from c_hypermem.retrieval.lexical_recall import SQLiteFTSRecall
from c_hypermem.retrieval.query_analysis import build_query_analyzer
from c_hypermem.retrieval.vector_recall import DenseVectorRecall
from c_hypermem.schema import SearchResult
from c_hypermem.stores.base import MemoryStore
from c_hypermem.stores.vector_store import VectorStore


class Retriever:
    def __init__(
        self,
        store: MemoryStore,
        config: RetrievalConfig,
        *,
        nlp_config: NLPConfig | None = None,
        query_analysis_llm: LLMClient | None = None,
        embedding_client: EmbeddingClient | None = None,
        vector_stores: dict[str, VectorStore] | None = None,
    ) -> None:
        self.store = store
        self.config = config
        self.analyzer = build_query_analyzer(config, nlp_config=nlp_config, llm=query_analysis_llm)
        self.lexical_recall = SQLiteFTSRecall(store, config)
        self.vector_recall = DenseVectorRecall(
            store,
            config,
            embedding_client=embedding_client,
            vector_stores=vector_stores,
        )

    def search(
        self,
        query: str,
        *,
        namespace: str,
        top_k: int,
        current_turn: int | None = None,
    ) -> list[SearchResult]:
        analysis = self.analyzer.analyze(query)
        query_vector = self.vector_recall.embed_query(analysis.query)
        lexical_hits = self.lexical_recall.recall(namespace=namespace, query=analysis.query)
        vector_hits = self.vector_recall.recall(
            namespace=namespace,
            query=analysis.query,
            query_vector=query_vector,
        )

        vector_node_ids = list(dict.fromkeys(hit.node_id for hit in vector_hits))
        vector_nodes = self.store.get_nodes(namespace, vector_node_ids)
        vector_nodes_by_id = {node.node_id: node for node in vector_nodes}
        vector_hits_by_node: dict[str, list[dict[str, object]]] = {}
        best_vector_score: dict[str, float] = {}
        for hit in vector_hits:
            best_vector_score[hit.node_id] = max(best_vector_score.get(hit.node_id, float("-inf")), hit.score)
            vector_hits_by_node.setdefault(hit.node_id, []).append(
                {
                    "channel": hit.channel,
                    "score": hit.score,
                    "id": hit.hit.id,
                    "text": hit.hit.text,
                    "payload": hit.hit.payload,
                }
            )

        ranked_vector_nodes = sorted(
            [vector_nodes_by_id[node_id] for node_id in vector_node_ids if node_id in vector_nodes_by_id],
            key=lambda node: best_vector_score.get(node.node_id, float("-inf")),
            reverse=True,
        )
        fused = reciprocal_rank_fusion(
            lexical_nodes=[hit.node for hit in lexical_hits],
            vector_nodes=ranked_vector_nodes,
            vector_hit_payloads=vector_hits_by_node,
        )

        limit = min(top_k, self.config.final_top_k)
        return [self._to_result(item, analysis_metadata=analysis.to_metadata()) for item in fused[:limit]]

    def _to_result(self, fused: FusedNode, *, analysis_metadata: dict) -> SearchResult:
        node = fused.node
        metadata = {
            "node_labels": node.node_labels,
            "node_id": node.node_id,
            "query_analysis": analysis_metadata,
            "source_session_id": node.metadata.get("source_session_id"),
            "source_event_id": node.metadata.get("source_event_id"),
            "source_turn_ids": node.metadata.get("source_turn_ids", []),
            "channels": sorted(fused.channels),
            "matched_vector_items": fused.vector_hits,
            "score_parts": fused.score_parts,
            "time": node.time.model_dump(mode="json"),
            "node_metadata": node.metadata,
        }
        if node.local_graph.triples:
            metadata["triples"] = [triple.model_dump(mode="json") for triple in node.local_graph.triples[:5]]
        return SearchResult(
            id=node.node_id,
            content=compose_result_content(node, []),
            score=float(fused.score),
            metadata=metadata,
        )
