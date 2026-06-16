from __future__ import annotations

from dataclasses import dataclass

from comgmem.config import RetrievalConfig
from comgmem.embeddings import EmbeddingClient
from comgmem.stores.base import MemoryStore
from comgmem.stores.vector_store import VectorSearchHit, VectorStore


_NODE_VECTOR_CHANNELS = ("node_content", "node_local_graph")
_VECTOR_ITEM_LABELS = {
    "node_content": "node_content",
    "node_local_graph": "node_local_graph",
}


@dataclass(frozen=True)
class VectorNodeHit:
    node_id: str
    channel: str
    score: float
    hit: VectorSearchHit


@dataclass(frozen=True)
class VectorEdgeHit:
    edge_id: str
    channel: str
    score: float
    hit: VectorSearchHit


class DenseVectorRecall:
    """Recall node candidates from rebuildable dense vector indexes."""

    def __init__(
        self,
        store: MemoryStore,
        config: RetrievalConfig,
        *,
        embedding_client: EmbeddingClient | None = None,
        vector_stores: dict[str, VectorStore] | None = None,
    ) -> None:
        self.store = store
        self.config = config
        self.embedding_client = embedding_client
        self.vector_stores = vector_stores or {}

    def embed_query(self, query: str) -> list[float] | None:
        if self.embedding_client is None:
            return None
        embeddings = self.embedding_client.embed([query])
        if not embeddings:
            return None
        return embeddings[0]

    def recall(
        self,
        *,
        namespace: str,
        query: str,
        query_vector: list[float] | None,
    ) -> list[VectorNodeHit]:
        if query_vector is None:
            return []
        hits = self._search_node_channels(namespace=namespace, query=query, query_vector=query_vector)
        if not hits:
            return []

        nodes = self.store.get_nodes(namespace, list(dict.fromkeys(hit.node_id for hit in hits)))
        active_node_ids = {node.node_id for node in nodes if node.status == "active"}
        return [hit for hit in hits if hit.node_id in active_node_ids]

    def recall_hyper_edges(
        self,
        *,
        namespace: str,
        query: str,
        query_vector: list[float] | None,
    ) -> list[VectorEdgeHit]:
        if query_vector is None:
            return []
        limit = max(0, self.config.hyper_edge_description_vector_top_k)
        if limit <= 0:
            return []
        store = self.vector_stores.get("hyper_edge_description")
        search = getattr(store, "search", None)
        if not callable(search):
            return []

        results: list[VectorEdgeHit] = []
        for hit in search(
            query=query,
            vector=query_vector,
            top_k=limit,
            filters={"namespace": namespace, "edge_status": "active"},
        ):
            if hit.payload.get("item_type") != "hyper_edge_description":
                continue
            edge_id = str(hit.payload.get("edge_id") or "")
            if not edge_id:
                continue
            results.append(
                VectorEdgeHit(
                    edge_id=edge_id,
                    channel="hyper_edge_description_vector",
                    score=float(hit.score),
                    hit=hit,
                )
            )

        edges = self.store.get_edges(namespace, list(dict.fromkeys(hit.edge_id for hit in results)))
        active_edge_ids = {edge.edge_id for edge in edges if edge.status == "active"}
        return [hit for hit in results if hit.edge_id in active_edge_ids]

    def _search_node_channels(
        self,
        *,
        namespace: str,
        query: str,
        query_vector: list[float],
    ) -> list[VectorNodeHit]:
        results: list[VectorNodeHit] = []
        for channel in _NODE_VECTOR_CHANNELS:
            store = self.vector_stores.get(channel)
            search = getattr(store, "search", None)
            limit = self._channel_limit(channel)
            if limit <= 0:
                continue
            if not callable(search):
                continue
            for hit in search(
                query=query,
                vector=query_vector,
                top_k=limit,
                filters={"namespace": namespace, "node_status": "active"},
            ):
                if hit.payload.get("item_type") != _VECTOR_ITEM_LABELS[channel]:
                    continue
                node_id = str(hit.payload.get("node_id") or "")
                if not node_id:
                    continue
                results.append(
                    VectorNodeHit(
                        node_id=node_id,
                        channel=_VECTOR_ITEM_LABELS[channel],
                        score=float(hit.score),
                        hit=hit,
                    )
                )
        return results

    def _channel_limit(self, channel: str) -> int:
        if channel == "node_content":
            return max(0, self.config.node_content_vector_top_k)
        if channel == "node_local_graph":
            return max(0, self.config.node_local_graph_vector_top_k)
        return 0
