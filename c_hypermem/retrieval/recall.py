from __future__ import annotations

from dataclasses import dataclass

from c_hypermem.config import NLPConfig, RetrievalConfig
from c_hypermem.embeddings import EmbeddingClient
from c_hypermem.llms.base import LLMClient
from c_hypermem.retrieval.fusion import FusedNode, RankedNodeList, reciprocal_rank_fusion_channels
from c_hypermem.retrieval.graph_ripple import GraphRippleExpansion, RankedEdge, Track1RankedEdge
from c_hypermem.retrieval.lexical_recall import LexicalNodeHit, SQLiteFTSRecall
from c_hypermem.retrieval.query_analysis import build_query_analyzer
from c_hypermem.retrieval.ranking import edge_level_rrf
from c_hypermem.retrieval.vector_recall import DenseVectorRecall, VectorEdgeHit, VectorNodeHit
from c_hypermem.schema import HyperEdge, SearchResult
from c_hypermem.stores.base import MemoryStore
from c_hypermem.stores.vector_store import VectorStore


@dataclass
class Track2RankedEdge:
    edge: HyperEdge
    score: float
    vector_hits: list[dict[str, object]]
    score_parts: dict[str, object]


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
        if top_k <= 0:
            return []

        analysis = self.analyzer.analyze(query)
        query_vector = self.vector_recall.embed_query(analysis.query)
        lexical_hits = self.lexical_recall.recall(namespace=namespace, query=analysis.query)
        vector_hits = self.vector_recall.recall(
            namespace=namespace,
            query=analysis.query,
            query_vector=query_vector,
        )
        edge_hits = self.vector_recall.recall_hyper_edges(
            namespace=namespace,
            query=analysis.query,
            query_vector=query_vector,
        )

        node_ranking = self._rank_nodes(namespace=namespace, lexical_hits=lexical_hits, vector_hits=vector_hits)
        track1_edges = self.graph_ripple.rank_track1_edges(namespace=namespace, node_ranking=node_ranking)
        track2_edges = self._rank_track2_edges(namespace=namespace, edge_hits=edge_hits)
        edge_ranking = self._edge_level_rrf(
            namespace=namespace,
            track1_edges=track1_edges,
            track2_edges=track2_edges,
        )
        core_edges = edge_ranking[: self._edge_core_limit()]
        ranked_edges = self.graph_ripple.attach_cluster_periphery(namespace=namespace, ranked_edges=core_edges)

        limit = min(top_k, self.config.final_top_k, len(ranked_edges))
        return [self._to_result(item, analysis_metadata=analysis.to_metadata()) for item in ranked_edges[:limit]]

    def _rank_nodes(
        self,
        *,
        namespace: str,
        lexical_hits: list[LexicalNodeHit],
        vector_hits: list[VectorNodeHit],
    ) -> list[FusedNode]:
        vector_node_ids = list(dict.fromkeys(hit.node_id for hit in vector_hits))
        vector_nodes = self.store.get_nodes(namespace, vector_node_ids)
        vector_nodes_by_id = {node.node_id: node for node in vector_nodes}
        vector_hits_by_node: dict[str, list[dict[str, object]]] = {}
        best_vector_score_by_channel: dict[str, dict[str, float]] = {}
        for hit in vector_hits:
            channel_scores = best_vector_score_by_channel.setdefault(hit.channel, {})
            channel_scores[hit.node_id] = max(channel_scores.get(hit.node_id, float("-inf")), hit.score)
            vector_hits_by_node.setdefault(hit.node_id, []).append(_vector_node_payload(hit))

        ranked_lists = [
            RankedNodeList(
                nodes=[hit.node for hit in lexical_hits],
                channel="lexical",
                score_key="rrf_lexical",
            )
        ]
        for channel in ("node_content", "node_local_graph"):
            scores = best_vector_score_by_channel.get(channel, {})
            channel_nodes = sorted(
                [
                    vector_nodes_by_id[node_id]
                    for node_id in vector_node_ids
                    if node_id in vector_nodes_by_id and node_id in scores
                ],
                key=lambda node: scores.get(node.node_id, float("-inf")),
                reverse=True,
            )
            ranked_lists.append(
                RankedNodeList(
                    nodes=channel_nodes,
                    channel=channel,
                    score_key=f"rrf_{channel}",
                )
            )

        return reciprocal_rank_fusion_channels(
            ranked_lists=ranked_lists,
            vector_hit_payloads=vector_hits_by_node,
            k=max(1, self.config.rrf_k),
        )

    def _rank_track2_edges(self, *, namespace: str, edge_hits: list[VectorEdgeHit]) -> list[Track2RankedEdge]:
        if not edge_hits:
            return []
        edges = self.store.get_edges(namespace, list(dict.fromkeys(hit.edge_id for hit in edge_hits)))
        edges_by_id = {edge.edge_id: edge for edge in edges if edge.status == "active"}
        ranked: list[Track2RankedEdge] = []
        seen_edge_ids: set[str] = set()
        for hit in edge_hits:
            edge = edges_by_id.get(hit.edge_id)
            if edge is None or edge.edge_id in seen_edge_ids:
                continue
            seen_edge_ids.add(edge.edge_id)
            ranked.append(
                Track2RankedEdge(
                    edge=edge,
                    score=hit.score,
                    vector_hits=[_vector_edge_payload(hit)],
                    score_parts={
                        "track2_vector_score": hit.score,
                    },
                )
            )
        return ranked

    def _edge_level_rrf(
        self,
        *,
        namespace: str,
        track1_edges: list[Track1RankedEdge],
        track2_edges: list[Track2RankedEdge],
    ) -> list[RankedEdge]:
        edge_rrf_k = self._edge_rrf_k()
        track1_by_id = {item.edge.edge_id: item for item in track1_edges}
        track2_by_id = {item.edge.edge_id: item for item in track2_edges}
        rrf_results = edge_level_rrf(
            track1_edge_ids=[item.edge.edge_id for item in track1_edges],
            track2_edge_ids=[item.edge.edge_id for item in track2_edges],
            k=edge_rrf_k,
            track1_tiebreak_scores={item.edge.edge_id: item.score for item in track1_edges},
            track2_tiebreak_scores={item.edge.edge_id: item.score for item in track2_edges},
        )
        ranked: list[RankedEdge] = []
        for rrf_result in rrf_results:
            edge_id = rrf_result.edge_id
            track1 = track1_by_id.get(edge_id)
            track2 = track2_by_id.get(edge_id)
            if track1 is None and track2 is None:
                continue
            if track1 is not None:
                edge = track1.edge
            else:
                assert track2 is not None
                edge = track2.edge
            nodes = (
                track1.nodes
                if track1 is not None
                else self.graph_ripple.materialize_edge_nodes(namespace=namespace, edge=edge)
            )
            score_parts = dict(rrf_result.score_parts)
            if track1 is not None:
                score_parts.update(track1.score_parts)
            if track2 is not None:
                score_parts.update(track2.score_parts)
            ranked.append(
                RankedEdge(
                    edge=edge,
                    score=rrf_result.score,
                    nodes=nodes,
                    score_parts=score_parts,
                    hit_node_ids=track1.hit_node_ids if track1 is not None else set(),
                    edge_vector_hits=track2.vector_hits if track2 is not None else [],
                )
            )
        return ranked

    def _edge_rrf_k(self) -> int:
        configured = self.config.edge_rrf_k
        return max(1, configured if configured is not None else self.config.rrf_k)

    def _edge_core_limit(self) -> int:
        configured = self.config.edge_core_top_k
        return max(0, configured if configured is not None else self.config.final_top_k)

    def _to_result(self, ranked_edge: RankedEdge, *, analysis_metadata: dict) -> SearchResult:
        edge = ranked_edge.edge
        metadata = {
            "query_analysis": analysis_metadata,
            "edge_id": edge.edge_id,
            "hyper_edge_ids": [edge.edge_id],
            "edge_description": edge.description,
            "edge_node_ids": edge.node_ids,
            "channels": self._result_channels(ranked_edge),
            "hit_node_ids": sorted(ranked_edge.hit_node_ids),
            "cluster_ids": sorted(ranked_edge.cluster_ids),
            "cluster_edge_descriptions": ranked_edge.cluster_edge_descriptions,
            "periphery_edges": ranked_edge.periphery_edges,
            "periphery_nodes": [self._node_metadata(item) for item in ranked_edge.periphery_nodes],
            "score_parts": ranked_edge.score_parts,
            "edge_vector_hits": ranked_edge.edge_vector_hits,
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

    def _result_channels(self, ranked_edge: RankedEdge) -> list[str]:
        channels = {channel for node in ranked_edge.nodes for channel in node.channels}
        channels.update(str(hit["channel"]) for hit in ranked_edge.edge_vector_hits if hit.get("channel"))
        return sorted(channels)

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


def _vector_node_payload(hit: VectorNodeHit) -> dict[str, object]:
    return {
        "channel": hit.channel,
        "score": hit.score,
        "id": hit.hit.id,
        "text": hit.hit.text,
        "payload": hit.hit.payload,
    }


def _vector_edge_payload(hit: VectorEdgeHit) -> dict[str, object]:
    return {
        "channel": hit.channel,
        "score": hit.score,
        "id": hit.hit.id,
        "text": hit.hit.text,
        "payload": hit.hit.payload,
    }
