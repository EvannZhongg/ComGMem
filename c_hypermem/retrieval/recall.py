from __future__ import annotations

from c_hypermem.config import NLPConfig, RetrievalConfig
from c_hypermem.embeddings import EmbeddingClient
from c_hypermem.llms.base import LLMClient
from c_hypermem.retrieval.fusion import FusedNode, reciprocal_rank_fusion
from c_hypermem.retrieval.graph_ripple import GraphRippleExpansion, RankedEdge
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
        self.graph_ripple = GraphRippleExpansion(store, config)

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
        ranked_edges = self.graph_ripple.expand(namespace=namespace, initial=fused)

        limit = min(top_k, self.config.final_top_k)
        return [self._to_result(item, analysis_metadata=analysis.to_metadata()) for item in ranked_edges[:limit]]

    def _to_result(self, ranked_edge: RankedEdge, *, analysis_metadata: dict) -> SearchResult:
        edge = ranked_edge.edge
        metadata = {
            "query_analysis": analysis_metadata,
            "edge_id": edge.edge_id,
            "hyper_edge_ids": [edge.edge_id],
            "edge_description": edge.description,
            "edge_node_ids": edge.node_ids,
            "channels": sorted({channel for node in ranked_edge.nodes for channel in node.channels}),
            "hit_node_ids": sorted(ranked_edge.hit_node_ids),
            "cluster_ids": sorted(ranked_edge.cluster_ids),
            "cluster_description_variants": ranked_edge.cluster_description_variants,
            "score_parts": ranked_edge.score_parts,
            "time": edge.time.model_dump(mode="json"),
            "edge_metadata": edge.metadata,
            "edge_nodes": [self._node_metadata(item) for item in ranked_edge.nodes],
        }
        return SearchResult(
            id=edge.edge_id,
            content=self._edge_content(ranked_edge),
            score=float(ranked_edge.score),
            metadata=metadata,
        )

    def _node_metadata(self, fused: FusedNode) -> dict[str, object]:
        node = fused.node
        payload: dict[str, object] = {
            "node_id": node.node_id,
            "node_labels": node.node_labels,
            "content": node.content,
            "summary": node.summary,
            "score": fused.score,
            "channels": sorted(fused.channels),
            "score_parts": fused.score_parts,
            "matched_vector_items": fused.vector_hits,
            "source_turn_ids": node.metadata.get("source_turn_ids", []),
            "time": node.time.model_dump(mode="json"),
            "node_metadata": node.metadata,
            "triples": [
                triple.model_dump(mode="json")
                for triple in node.local_graph.triples
                if triple.status == "active"
            ],
        }
        return payload

    def _edge_content(self, ranked_edge: RankedEdge) -> str:
        edge = ranked_edge.edge
        node_lines = "\n".join(f"- {item.node.content}" for item in ranked_edge.nodes)
        return f"{edge.description}\nNodes:\n{node_lines}"
