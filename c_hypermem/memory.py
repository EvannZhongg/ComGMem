from __future__ import annotations

from pathlib import Path
from typing import Any

from c_hypermem.config import MemoryConfig
from c_hypermem.embeddings import EmbeddingClient, EmbeddingModelClient
from c_hypermem.errors import ConfigError
from c_hypermem.llms.base import LLMClient
from c_hypermem.llms.openai_compatible import OpenAICompatibleLLM
from c_hypermem.pipeline import IngestionPipeline
from c_hypermem.pipeline.edge_cluster_builder import EdgeClusterBuilder
from c_hypermem.pipeline.extraction import LLMMemoryExtractor, MemoryExtractor
from c_hypermem.pipeline.hyperedge_builder import HyperEdgeBuilder
from c_hypermem.pipeline.ingestion import interaction_messages
from c_hypermem.retrieval import Retriever
from c_hypermem.schema import AgentInteraction, MemoryImportBatch, Message
from c_hypermem.stores import SQLiteStore
from c_hypermem.stores.vector_store import (
    QdrantVectorStore,
    VectorRecord,
    VectorStore,
    VectorIndexItem,
    collect_edge_cluster_canonical_index_items,
    collect_edge_cluster_variant_index_items,
    collect_node_content_index_items,
    collect_node_local_graph_index_items,
    collect_node_summary_index_items,
    collect_turn_dialogue_index_item,
    make_vector_point_id,
)
from c_hypermem.utils.time import touch_node_access


class Memory:
    def __init__(
        self,
        config: MemoryConfig,
        *,
        extractor: MemoryExtractor | None = None,
        hyperedge_builder: HyperEdgeBuilder | None = None,
        edge_cluster_builder: EdgeClusterBuilder | None = None,
        maintenance_llm: LLMClient | None = None,
        embedding_client: EmbeddingClient | None = None,
        vector_store: VectorStore | None = None,
    ) -> None:
        self.config = config
        self.store = SQLiteStore(Path(config.storage.path))
        if extractor is None and config.llm is not None:
            extractor = LLMMemoryExtractor(config)
        self.embedding_client = embedding_client
        if self.embedding_client is None and config.index.use_embedding and config.embedding is not None:
            self.embedding_client = EmbeddingModelClient(config.embedding)
        self.vector_store = vector_store
        if self.vector_store is None and self.embedding_client is not None and config.index.vector == "qdrant":
            self.vector_store = self._default_qdrant_vector_store("triple")
        self.vector_stores: dict[str, VectorStore] = {}
        if self.vector_store is not None:
            self.vector_stores["triple"] = self.vector_store
            if vector_store is not None:
                for item_type in _VECTOR_INDEX_TYPES:
                    self.vector_stores[item_type] = vector_store
            elif self.embedding_client is not None and config.index.vector == "qdrant":
                self.vector_stores.update(
                    {
                        "node_content": self._default_qdrant_vector_store("node_content"),
                        "node_summary": self._default_qdrant_vector_store("node_summary"),
                        "edge_cluster_canonical": self._default_qdrant_vector_store("edge_cluster_canonical"),
                        "edge_cluster_variant": self._default_qdrant_vector_store("edge_cluster_variant"),
                        "turn_dialogue": self._default_qdrant_vector_store("turn_dialogue"),
                    }
                )
        self.ingestion = IngestionPipeline(
            config,
            self.store,
            extractor=extractor,
            hyperedge_builder=hyperedge_builder,
            edge_cluster_builder=edge_cluster_builder,
            maintenance_llm=maintenance_llm,
        )
        query_analysis_llm = None
        if config.retrieval.query_analysis == "llm":
            if config.llm is None:
                raise ConfigError("retrieval.query_analysis='llm' requires config.llm.")
            query_analysis_llm = OpenAICompatibleLLM(config.llm)
        self.retriever = Retriever(
            self.store,
            config.retrieval,
            nlp_config=config.nlp,
            query_analysis_llm=query_analysis_llm,
            embedding_client=self.embedding_client,
            vector_stores=self.vector_stores,
        )
        self._turn_counters: dict[str, int] = {}

    @classmethod
    def from_config(
        cls,
        config: str | Path | dict[str, Any] | MemoryConfig | None = None,
        *,
        extractor: MemoryExtractor | None = None,
        hyperedge_builder: HyperEdgeBuilder | None = None,
        edge_cluster_builder: EdgeClusterBuilder | None = None,
        maintenance_llm: LLMClient | None = None,
        embedding_client: EmbeddingClient | None = None,
        vector_store: VectorStore | None = None,
    ) -> "Memory":
        return cls(
            MemoryConfig.load(config),
            extractor=extractor,
            hyperedge_builder=hyperedge_builder,
            edge_cluster_builder=edge_cluster_builder,
            maintenance_llm=maintenance_llm,
            embedding_client=embedding_client,
            vector_store=vector_store,
        )

    def reset(self, namespace: str = "default") -> None:
        self.store.reset_namespace(namespace)
        for store in _unique_vector_stores(self.vector_stores):
            store.delete_namespace(namespace)
        self._turn_counters[namespace] = 0

    def add_memory(
        self,
        user_input: str | dict[str, Any] | None = None,
        assistant_output: str | dict[str, Any] | None = None,
        namespace: str = "default",
        metadata: dict[str, Any] | None = None,
        tool_calls: list[dict[str, Any]] | None = None,
        tool_results: list[dict[str, Any]] | None = None,
        observations: list[dict[str, Any]] | None = None,
        attachments: list[dict[str, Any]] | None = None,
        trace: dict[str, Any] | None = None,
    ) -> None:
        interaction = AgentInteraction.model_validate(
            {
                "user_input": _message_or_none(user_input, default_role="user"),
                "assistant_output": _message_or_none(assistant_output, default_role="assistant"),
                "tool_calls": tool_calls or [],
                "tool_results": tool_results or [],
                "observations": observations or [],
                "attachments": attachments or [],
                "trace": trace or {},
                "metadata": metadata or {},
            }
        )
        target_messages = interaction_messages(interaction)
        current_turn = self._next_turn(namespace)
        turn_id = _turn_id(current_turn)
        recent_messages = self._recent_context(namespace)
        interaction.metadata = _with_turn_ids(interaction.metadata, [turn_id])
        self.store.append_turn(namespace, turn_id, current_turn, target_messages, interaction.metadata)
        output = self.ingestion.ingest_interaction(
            interaction,
            namespace=namespace,
            current_turn=current_turn,
            recent_messages=recent_messages,
        )
        self._index_turn_dialogue(namespace, turn_id, current_turn, target_messages, interaction.metadata)
        self._persist_output(output)

    def add(
        self,
        messages: str | list[dict[str, Any]],
        namespace: str = "default",
        metadata: dict[str, Any] | None = None,
    ) -> None:
        message_objs = _normalize_messages(messages)
        for message in message_objs:
            current_turn = self._next_turn(namespace)
            turn_id = _turn_id(current_turn)
            batch = MemoryImportBatch(messages=[message], metadata=_with_turn_ids(metadata or {}, [turn_id]))
            recent_messages = self._recent_context(namespace)
            self.store.append_turn(namespace, turn_id, current_turn, [message], batch.metadata)
            output = self.ingestion.ingest_batch(
                batch,
                namespace=namespace,
                current_turn=current_turn,
                recent_messages=recent_messages,
            )
            self._index_turn_dialogue(namespace, turn_id, current_turn, [message], batch.metadata)
            self._persist_output(output)

    def search(
        self,
        query: str,
        namespace: str = "default",
        top_k: int = 10,
        metadata: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        current_turn = self._turn_counters.get(namespace)
        results = self.retriever.search(query, namespace=namespace, top_k=top_k, current_turn=current_turn)
        self._record_access(namespace, [result.id for result in results], current_turn)
        return [result.model_dump(mode="json") for result in results]

    def stats(self, namespace: str = "default") -> dict[str, Any]:
        stats = self.store.stats(namespace)
        stats["namespace"] = namespace
        stats["current_turn"] = self._turn_counters.get(namespace, 0)
        return stats

    def close(self) -> None:
        for store in _unique_vector_stores(self.vector_stores):
            store.close()
        self.store.close()

    def _default_qdrant_vector_store(self, item_type: str, *, client: Any | None = None) -> VectorStore:
        collection_name = _vector_collection_name(self.config.index.vector_store.collection_name, item_type)
        if client is not None:
            return QdrantVectorStore.with_client(
                path=self.config.index.vector_store.path,
                collection_name=collection_name,
                client=client,
            )
        return QdrantVectorStore(
            path=self.config.index.vector_store.path,
            collection_name=collection_name,
        )

    def _next_turn(self, namespace: str) -> int:
        if namespace not in self._turn_counters:
            self._turn_counters[namespace] = self.store.next_turn_index(namespace)
        current = self._turn_counters[namespace]
        self._turn_counters[namespace] = current + 1
        return current

    def _record_access(self, namespace: str, node_ids: list[str], current_turn: int | None) -> None:
        if not node_ids:
            return
        nodes = self.store.get_nodes(namespace, node_ids)
        touched = [touch_node_access(node, current_turn) for node in nodes]
        self.store.upsert_nodes(touched)

    def _recent_context(self, namespace: str) -> list[Message]:
        window_size = max(0, self.config.ingestion.context_window_messages)
        if window_size == 0:
            return []
        return self.store.list_recent_turn_messages(namespace, window_size)

    def _persist_output(self, output: Any) -> None:
        self.store.upsert_nodes(output.nodes)
        self._delete_retired_vectors(output.retired_nodes)
        self.store.upsert_edges(output.edges)
        self.store.upsert_edge_clusters(output.edge_clusters)
        self._index_nodes_and_clusters(output.nodes, output.edge_clusters)
        self.store.upsert_edge_cluster_members(output.edge_cluster_members)
        self.store.upsert_entity_aliases(output.entity_aliases)
        self.store.upsert_fact_properties(output.fact_properties)

    def _delete_retired_vectors(self, nodes: list[Any]) -> None:
        if not nodes:
            return
        local_graph_ids = list(
            dict.fromkeys(
                make_vector_point_id(node.namespace, "triple", node.node_id)
                for node in nodes
                if node.local_graph.triples
            )
        )
        if self.vector_store is not None and local_graph_ids:
            self.vector_store.delete(local_graph_ids)
        ids_by_type: dict[str, list[str]] = {
            "node_content": [
                make_vector_point_id(node.namespace, "node_content", node.node_id)
                for node in nodes
            ],
            "node_summary": [
                make_vector_point_id(node.namespace, "node_summary", node.node_id)
                for node in nodes
            ],
        }
        for item_type, ids in ids_by_type.items():
            store = self.vector_stores.get(item_type)
            unique_ids = list(dict.fromkeys(ids))
            if store is not None and unique_ids:
                store.delete(unique_ids)

    def _index_nodes_and_clusters(self, nodes: list[Any], clusters: list[Any]) -> None:
        self._index_items(
            "node_content",
            [
                item
                for item in collect_node_content_index_items(nodes)
                if item.payload.get("node_status") == "active"
            ],
        )
        self._index_items(
            "node_summary",
            [
                item
                for item in collect_node_summary_index_items(nodes)
                if item.payload.get("node_status") == "active"
            ],
        )
        self._index_items(
            "triple",
            [
                item
                for item in collect_node_local_graph_index_items(nodes)
                if item.payload.get("node_status") == "active"
            ],
        )
        self._index_items(
            "edge_cluster_canonical",
            [
                item
                for item in collect_edge_cluster_canonical_index_items(clusters)
                if item.payload.get("status") == "active"
            ],
        )
        self._index_items(
            "edge_cluster_variant",
            [
                item
                for item in collect_edge_cluster_variant_index_items(clusters)
                if item.payload.get("status") == "active"
            ],
        )

    def _index_turn_dialogue(
        self,
        namespace: str,
        turn_id: str,
        turn_index: int,
        messages: list[Message],
        metadata: dict[str, Any],
    ) -> None:
        item = collect_turn_dialogue_index_item(
            namespace=namespace,
            turn_id=turn_id,
            turn_index=turn_index,
            messages=messages,
            metadata=metadata,
        )
        if item is None:
            return
        self._index_items("turn_dialogue", [item])

    def _index_items(self, item_type: str, items: list[VectorIndexItem]) -> None:
        store = self.vector_stores.get(item_type)
        if store is None or self.embedding_client is None:
            return
        if not items:
            return
        embeddings = self.embedding_client.embed([item.text for item in items])
        records = [
            VectorRecord(
                id=item.id,
                vector=vector,
                text=item.text,
                payload=item.payload,
            )
            for item, vector in zip(items, embeddings)
        ]
        store.upsert(records)


def _message_or_none(value: str | dict[str, Any] | None, *, default_role: str) -> dict[str, Any] | None:
    if value is None:
        return None
    if isinstance(value, str):
        return {"role": default_role, "content": value}
    data = dict(value)
    data.setdefault("role", default_role)
    data.setdefault("content", "")
    return data


def _normalize_messages(messages: str | list[dict[str, Any]]) -> list[Message]:
    if isinstance(messages, str):
        return [Message(role="user", content=messages)]
    return [Message.model_validate(message) for message in messages]


def _turn_id(turn_index: int) -> str:
    return f"turn:{turn_index}"


def _with_turn_ids(metadata: dict[str, Any], turn_ids: list[str]) -> dict[str, Any]:
    merged = dict(metadata)
    existing = merged.get("turn_ids")
    existing_ids = [str(item) for item in existing] if isinstance(existing, list) else []
    merged["turn_ids"] = list(dict.fromkeys([*existing_ids, *turn_ids]))
    return merged


_VECTOR_INDEX_TYPES = [
    "triple",
    "node_content",
    "node_summary",
    "edge_cluster_canonical",
    "edge_cluster_variant",
    "turn_dialogue",
]


def _vector_collection_name(base_name: str, item_type: str) -> str:
    if item_type == "triple":
        return f"{base_name}_triples"
    return f"{base_name}_{item_type}"


def _unique_vector_stores(stores: dict[str, VectorStore]) -> list[VectorStore]:
    unique: list[VectorStore] = []
    seen: set[int] = set()
    for store in stores.values():
        client = getattr(store, "_client", None)
        marker = id(client) if client is not None else id(store)
        if marker in seen:
            continue
        seen.add(marker)
        unique.append(store)
    return unique
