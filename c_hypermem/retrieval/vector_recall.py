from __future__ import annotations

from dataclasses import dataclass

from c_hypermem.config import RetrievalConfig
from c_hypermem.embeddings import EmbeddingClient
from c_hypermem.stores.base import MemoryStore
from c_hypermem.stores.vector_store import VectorSearchHit, VectorStore


_NODE_VECTOR_CHANNELS = ("node_content", "triple", "node_summary")
_VECTOR_ITEM_LABELS = {
    "node_content": "node_content",
    "node_summary": "node_summary",
    "triple": "node_local_graph",
}


@dataclass(frozen=True)
class VectorNodeHit:
    node_id: str
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
        if channel == "node_summary":
            return max(0, self.config.node_summary_vector_top_k)
        if channel == "triple":
            return max(0, self.config.node_local_graph_vector_top_k)
        return 0
