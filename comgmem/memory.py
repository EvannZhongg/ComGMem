from __future__ import annotations

from pathlib import Path
from typing import Any

from comgmem.config import MemoryConfig
from comgmem.embeddings import EmbeddingClient, EmbeddingModelClient
from comgmem.errors import ConfigError
from comgmem.llms.base import LLMClient
from comgmem.llms.openai_compatible import OpenAICompatibleLLM
from comgmem.pipeline import IngestionPipeline
from comgmem.pipeline.edge_cluster_builder import EdgeClusterBuilder
from comgmem.pipeline.extraction import LLMMemoryExtractor, MemoryExtractor
from comgmem.pipeline.hyperedge_builder import HyperEdgeBuilder
from comgmem.pipeline.ingestion import interaction_messages
from comgmem.retrieval import Retriever
from comgmem.schema import AgentInteraction, MemoryImportBatch, Message
from comgmem.stores import SQLiteStore
from comgmem.stores.vector_store import (
    QdrantVectorStore,
    VectorRecord,
    VectorStore,
    VectorIndexItem,
    collect_edge_cluster_canonical_index_items,
    collect_hyper_edge_description_index_items,
    collect_node_content_index_items,
    collect_node_local_graph_index_items,
    collect_turn_dialogue_index_item,
    make_vector_point_id,
    turn_dialogue_embedding_text,
)
from comgmem.utils.compression import TokenLimitCompressor
from comgmem.utils.memory_log import NamespaceMemoryLogWriter
from comgmem.utils.time import touch_node_access, utc_now_iso
from comgmem.utils.token_counting import TikTokenCounter, TokenCounter


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
            self.vector_store = self._default_qdrant_vector_store("node_local_graph")
        self.vector_stores: dict[str, VectorStore] = {}
        if self.vector_store is not None:
            self.vector_stores["node_local_graph"] = self.vector_store
            if vector_store is not None:
                for item_type in _enabled_vector_index_types(config):
                    self.vector_stores[item_type] = vector_store
            elif self.embedding_client is not None and config.index.vector == "qdrant":
                for item_type in _enabled_vector_index_types(config):
                    if item_type == "node_local_graph":
                        continue
                    self.vector_stores[item_type] = self._default_qdrant_vector_store(item_type)
        compression_llm = maintenance_llm
        if compression_llm is None and config.llm is not None:
            compression_llm = OpenAICompatibleLLM(config.llm)
        self.compressor = TokenLimitCompressor(config, llm=compression_llm)
        self.memory_log_writer = NamespaceMemoryLogWriter(config.logging, base_path=_log_base_path(config))
        self._token_counter: TokenCounter | None = None
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
            recall_config=config.recall,
            nlp_config=config.nlp,
            query_analysis_llm=query_analysis_llm,
            query_analysis_llm_config=config.llm if query_analysis_llm is not None else None,
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
        current_turn = self._next_turn(namespace)
        turn_id = _turn_id(current_turn)
        log_record = self._new_log_record(
            namespace=namespace,
            turn_id=turn_id,
            turn_index=current_turn,
            operation="add_memory",
            messages=interaction_messages(interaction),
        )
        if interaction.user_input is not None:
            interaction.user_input = self._compress_message_for_ingestion(
                interaction.user_input,
                content_type="user input",
                log_record=log_record,
            )
        if interaction.assistant_output is not None:
            interaction.assistant_output = self._compress_message_for_ingestion(
                interaction.assistant_output,
                content_type="assistant output",
                log_record=log_record,
            )
        target_messages = self._compress_turn_dialogue_for_ingestion(
            interaction_messages(interaction),
            log_record=log_record,
        )
        recent_messages = self._recent_context(namespace)
        interaction.metadata = _with_turn_ids(interaction.metadata, [turn_id])
        self.store.append_turn(namespace, turn_id, current_turn, target_messages, interaction.metadata)
        self._append_log_step(
            log_record,
            "append_turn",
            stored_message_count=len(target_messages),
            stored_message_tokens=_messages_token_count(self, target_messages),
        )
        output = self.ingestion.ingest_messages(
            target_messages,
            namespace=namespace,
            metadata=interaction.metadata,
            current_turn=current_turn,
            recent_messages=recent_messages,
        )
        self._append_log_step(
            log_record,
            "ingest_messages",
            recent_context_message_count=len(recent_messages),
            recent_context_tokens=_messages_token_count(self, recent_messages),
            **_output_counts(output),
        )
        self._index_turn_dialogue(namespace, turn_id, current_turn, target_messages, interaction.metadata, log_record)
        self._persist_output(output, log_record=log_record)
        self._write_log(namespace, log_record)

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
            log_record = self._new_log_record(
                namespace=namespace,
                turn_id=turn_id,
                turn_index=current_turn,
                operation="add",
                messages=[message],
            )
            message = self._compress_message_for_ingestion(
                message,
                content_type=f"{message.role} message",
                log_record=log_record,
            )
            batch = MemoryImportBatch(messages=[message], metadata=_with_turn_ids(metadata or {}, [turn_id]))
            recent_messages = self._recent_context(namespace)
            self.store.append_turn(namespace, turn_id, current_turn, [message], batch.metadata)
            self._append_log_step(
                log_record,
                "append_turn",
                stored_message_count=1,
                stored_message_tokens=_messages_token_count(self, [message]),
            )
            output = self.ingestion.ingest_batch(
                batch,
                namespace=namespace,
                current_turn=current_turn,
                recent_messages=recent_messages,
            )
            self._append_log_step(
                log_record,
                "ingest_batch",
                recent_context_message_count=len(recent_messages),
                recent_context_tokens=_messages_token_count(self, recent_messages),
                **_output_counts(output),
            )
            self._index_turn_dialogue(namespace, turn_id, current_turn, [message], batch.metadata, log_record)
            self._persist_output(output, log_record=log_record)
            self._write_log(namespace, log_record)

    def search(
        self,
        query: str,
        namespace: str = "default",
        top_k: int = 10,
        metadata: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        current_turn = self._turn_counters.get(namespace)
        results = self.retriever.search(query, namespace=namespace, top_k=top_k, current_turn=current_turn)
        accessed_node_ids = [
            str(node.get("node_id"))
            for result in results
            for node in result.metadata.get("edge_nodes", [])
            if isinstance(node, dict) and node.get("node_id")
        ]
        self._record_access(namespace, list(dict.fromkeys(accessed_node_ids)), current_turn)
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
        if not self.config.ingestion.pass_recent_context:
            return []
        window_size = max(0, self.config.ingestion.context_window_messages)
        if window_size == 0:
            return []
        return self.store.list_recent_turn_messages(namespace, window_size)

    def _persist_output(self, output: Any, *, log_record: dict[str, Any] | None = None) -> None:
        self.store.upsert_nodes(output.nodes)
        self._delete_retired_vectors(output.retired_nodes)
        self.store.upsert_edges(output.edges)
        self.store.upsert_edge_clusters(output.edge_clusters)
        self._index_nodes_edges_and_clusters(output.nodes, output.edges, output.edge_clusters, log_record=log_record)
        self.store.upsert_edge_cluster_members(output.edge_cluster_members)
        self.store.upsert_entity_aliases(output.entity_aliases)
        if log_record is not None:
            self._append_log_step(log_record, "persist_output", **_output_counts(output))

    def _delete_retired_vectors(self, nodes: list[Any]) -> None:
        if not nodes:
            return
        local_graph_ids = list(
            dict.fromkeys(
                make_vector_point_id(node.namespace, "node_local_graph", node.node_id)
                for node in nodes
                if node.local_graph.triples
            )
        )
        node_local_graph_store = self.vector_stores.get("node_local_graph") or self.vector_store
        if node_local_graph_store is not None and local_graph_ids:
            node_local_graph_store.delete(local_graph_ids)
        ids_by_type: dict[str, list[str]] = {
            "node_content": [
                make_vector_point_id(node.namespace, "node_content", node.node_id)
                for node in nodes
            ],
        }
        for item_type, ids in ids_by_type.items():
            store = self.vector_stores.get(item_type)
            unique_ids = list(dict.fromkeys(ids))
            if store is not None and unique_ids:
                store.delete(unique_ids)

    def _index_nodes_edges_and_clusters(
        self,
        nodes: list[Any],
        edges: list[Any],
        clusters: list[Any],
        *,
        log_record: dict[str, Any] | None = None,
    ) -> None:
        node_local_graph_items = [
            item
            for item in collect_node_local_graph_index_items(nodes)
            if item.payload.get("node_status") == "active"
        ]
        self._delete_empty_node_local_graph_vectors(nodes, node_local_graph_items)
        self._index_items(
            "node_content",
            [
                item
                for item in collect_node_content_index_items(nodes)
                if item.payload.get("node_status") == "active"
            ],
            log_record=log_record,
        )
        self._index_items(
            "node_local_graph",
            node_local_graph_items,
            log_record=log_record,
        )
        self._index_items(
            "hyper_edge_description",
            [
                item
                for item in collect_hyper_edge_description_index_items(edges)
                if item.payload.get("edge_status") == "active"
            ],
            log_record=log_record,
        )
        self._index_items(
            "edge_cluster_canonical",
            [
                item
                for item in collect_edge_cluster_canonical_index_items(clusters)
                if item.payload.get("status") == "active"
            ],
            log_record=log_record,
        )

    def _delete_empty_node_local_graph_vectors(self, nodes: list[Any], items: list[VectorIndexItem]) -> None:
        node_local_graph_store = self.vector_stores.get("node_local_graph") or self.vector_store
        if node_local_graph_store is None:
            return
        indexed_ids = {str(item.payload.get("node_id")) for item in items if item.payload.get("node_id")}
        stale_ids = [
            make_vector_point_id(node.namespace, "node_local_graph", node.node_id)
            for node in nodes
            if getattr(node, "status", None) == "active" and node.node_id not in indexed_ids
        ]
        stale_ids = list(dict.fromkeys(stale_ids))
        if stale_ids:
            node_local_graph_store.delete(stale_ids)

    def _index_turn_dialogue(
        self,
        namespace: str,
        turn_id: str,
        turn_index: int,
        messages: list[Message],
        metadata: dict[str, Any],
        log_record: dict[str, Any] | None = None,
    ) -> None:
        if not self.config.turn.enabled or not self.config.turn.indexing.vector:
            return
        item = collect_turn_dialogue_index_item(
            namespace=namespace,
            turn_id=turn_id,
            turn_index=turn_index,
            messages=messages,
            metadata=metadata,
        )
        if item is None:
            return
        self._index_items("turn_dialogue", [item], log_record=log_record)

    def _index_items(
        self,
        item_type: str,
        items: list[VectorIndexItem],
        *,
        log_record: dict[str, Any] | None = None,
    ) -> None:
        store = self.vector_stores.get(item_type)
        if store is None or self.embedding_client is None:
            if log_record is not None and items:
                self._append_log_step(log_record, "vector_index_skipped", item_type=item_type, item_count=len(items))
            return
        if not items:
            return
        original_token_count = sum(self._count_tokens(item.text) for item in items)
        items = [self._compress_vector_item(item_type, item, log_record=log_record) for item in items]
        indexed_token_count = sum(self._count_tokens(item.text) for item in items)
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
        if log_record is not None:
            self._append_log_step(
                log_record,
                "vector_index",
                item_type=item_type,
                item_count=len(items),
                original_token_count=original_token_count,
                embedding_input_tokens=indexed_token_count,
            )
            _add_log_tokens(log_record, "embedding_input_tokens", indexed_token_count)

    def _compress_message_for_ingestion(
        self,
        message: Message,
        *,
        content_type: str,
        log_record: dict[str, Any] | None = None,
    ) -> Message:
        compression = self.compressor.compress_if_needed(
            message.content,
            max_tokens=_ingestion_message_max_tokens(self.config),
            content_type=content_type,
        )
        if compression is None:
            return message
        self._record_compression(log_record, "message_compaction", content_type, compression)
        metadata = dict(message.metadata)
        metadata["compression"] = {
            "content_type": content_type,
            "original_token_count": compression.original_token_count,
            "compressed_token_count": compression.compressed_token_count,
            "max_tokens": compression.max_tokens,
        }
        return message.model_copy(update={"content": compression.text, "metadata": metadata})

    def _compress_turn_dialogue_for_ingestion(
        self,
        messages: list[Message],
        *,
        log_record: dict[str, Any] | None = None,
    ) -> list[Message]:
        text = turn_dialogue_embedding_text(messages)
        compression = self.compressor.compress_if_needed(
            text,
            max_tokens=self.config.embedding.max_tokens
            if self.config.index.use_embedding and self.config.embedding is not None
            else None,
            content_type="conversation turn dialogue",
        )
        if compression is None:
            return messages
        self._record_compression(log_record, "turn_dialogue_compaction", "conversation turn dialogue", compression)
        return [
            Message(
                role="dialogue_summary",
                content=compression.text,
                metadata={
                    "compression": {
                        "content_type": "conversation turn dialogue",
                        "original_token_count": compression.original_token_count,
                        "compressed_token_count": compression.compressed_token_count,
                        "max_tokens": compression.max_tokens,
                    }
                },
            )
        ]

    def _compress_vector_item(
        self,
        item_type: str,
        item: VectorIndexItem,
        *,
        log_record: dict[str, Any] | None = None,
    ) -> VectorIndexItem:
        compression = self.compressor.compress_if_needed(
            item.text,
            max_tokens=self.config.embedding.max_tokens if self.config.embedding is not None else None,
            content_type=f"{item_type} embedding text",
        )
        if compression is None:
            return item
        self._record_compression(log_record, "vector_text_compaction", f"{item_type} embedding text", compression)
        payload = dict(item.payload)
        payload["compression"] = {
            "content_type": f"{item_type} embedding text",
            "original_token_count": compression.original_token_count,
            "compressed_token_count": compression.compressed_token_count,
            "max_tokens": compression.max_tokens,
        }
        return VectorIndexItem(id=item.id, text=compression.text, payload=payload)

    def _new_log_record(
        self,
        *,
        namespace: str,
        turn_id: str,
        turn_index: int,
        operation: str,
        messages: list[Message],
    ) -> dict[str, Any]:
        token_count = _messages_token_count(self, messages)
        record: dict[str, Any] = {
            "schema_version": 1,
            "event": "memory_storage",
            "namespace": namespace,
            "turn_id": turn_id,
            "turn_index": turn_index,
            "operation": operation,
            "started_at": utc_now_iso(),
            "steps": [],
            "token_usage": {
                "input_message_tokens": token_count,
                "stored_message_tokens": 0,
                "compression_source_tokens": 0,
                "compression_prompt_tokens": 0,
                "compression_output_tokens": 0,
                "embedding_input_tokens": 0,
            },
        }
        self._append_log_step(
            record,
            "receive_input",
            message_count=len(messages),
            roles=[message.role for message in messages],
            token_count=token_count,
        )
        return record

    def _append_log_step(self, record: dict[str, Any], name: str, **fields: Any) -> None:
        step = {"name": name, "at": utc_now_iso()}
        step.update(fields)
        record["steps"].append(step)

    def _record_compression(
        self,
        record: dict[str, Any] | None,
        step_name: str,
        content_type: str,
        compression: Any,
    ) -> None:
        if record is None:
            return
        self._append_log_step(
            record,
            step_name,
            content_type=content_type,
            original_token_count=compression.original_token_count,
            compressed_token_count=compression.compressed_token_count,
            prompt_token_count=compression.prompt_token_count,
            max_tokens=compression.max_tokens,
        )
        _add_log_tokens(record, "compression_source_tokens", compression.original_token_count)
        _add_log_tokens(record, "compression_prompt_tokens", compression.prompt_token_count)
        _add_log_tokens(record, "compression_output_tokens", compression.compressed_token_count)

    def _write_log(self, namespace: str, record: dict[str, Any]) -> None:
        record["completed_at"] = utc_now_iso()
        record["token_usage"]["stored_message_tokens"] = sum(
            step.get("stored_message_tokens", 0)
            for step in record["steps"]
            if step.get("name") == "append_turn"
        )
        self.memory_log_writer.write(namespace, record)

    def _count_tokens(self, text: str) -> int:
        if self._token_counter is None:
            self._token_counter = TikTokenCounter(self.config.token_counting.tokenizer_encoding)
        return self._token_counter.count(text)


def _messages_token_count(memory: Memory, messages: list[Message]) -> int:
    return sum(memory._count_tokens(message.content) for message in messages)


def _add_log_tokens(record: dict[str, Any], key: str, value: int) -> None:
    token_usage = record["token_usage"]
    token_usage[key] = int(token_usage.get(key, 0)) + int(value)


def _output_counts(output: Any) -> dict[str, int]:
    return {
        "node_count": len(output.nodes),
        "retired_node_count": len(output.retired_nodes),
        "edge_count": len(output.edges),
        "edge_cluster_count": len(output.edge_clusters),
        "edge_cluster_member_count": len(output.edge_cluster_members),
        "entity_alias_count": len(output.entity_aliases),
    }


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
    "node_local_graph",
    "node_content",
    "hyper_edge_description",
    "edge_cluster_canonical",
    "turn_dialogue",
]


def _enabled_vector_index_types(config: MemoryConfig) -> list[str]:
    item_types = list(_VECTOR_INDEX_TYPES)
    if not config.turn.enabled or not config.turn.indexing.vector:
        item_types.remove("turn_dialogue")
    return item_types


def _vector_collection_name(base_name: str, item_type: str) -> str:
    return f"{base_name}_{item_type}"


def _ingestion_message_max_tokens(config: MemoryConfig) -> int | None:
    limits = []
    if config.llm is not None and config.llm.max_tokens is not None:
        limits.append(config.llm.max_tokens)
    if config.index.use_embedding and config.embedding is not None and config.embedding.max_tokens is not None:
        limits.append(config.embedding.max_tokens)
    return min(limits) if limits else None


def _log_base_path(config: MemoryConfig) -> Path:
    if config.logging.path is not None:
        return Path(config.logging.path)
    return Path(config.storage.path).parent


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
