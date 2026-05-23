from __future__ import annotations

from pathlib import Path
from typing import Any

from c_hypermem.config import MemoryConfig
from c_hypermem.pipeline import IngestionPipeline
from c_hypermem.pipeline.edge_cluster_builder import EdgeClusterBuilder
from c_hypermem.pipeline.extraction import LLMMemoryExtractor, MemoryExtractor
from c_hypermem.pipeline.hyperedge_builder import HyperEdgeBuilder
from c_hypermem.retrieval import Retriever
from c_hypermem.schema import AgentInteraction, MemoryImportBatch, Message
from c_hypermem.stores import SQLiteStore
from c_hypermem.utils.time import touch_node_access


class Memory:
    def __init__(
        self,
        config: MemoryConfig,
        *,
        extractor: MemoryExtractor | None = None,
        hyperedge_builder: HyperEdgeBuilder | None = None,
        edge_cluster_builder: EdgeClusterBuilder | None = None,
    ) -> None:
        self.config = config
        self.store = SQLiteStore(Path(config.storage.path))
        if extractor is None and config.llm is not None:
            extractor = LLMMemoryExtractor(config)
        self.ingestion = IngestionPipeline(
            config,
            self.store,
            extractor=extractor,
            hyperedge_builder=hyperedge_builder,
            edge_cluster_builder=edge_cluster_builder,
        )
        self.retriever = Retriever(self.store, config.retrieval)
        self._turn_counters: dict[str, int] = {}

    @classmethod
    def from_config(
        cls,
        config: str | Path | dict[str, Any] | MemoryConfig | None = None,
        *,
        extractor: MemoryExtractor | None = None,
        hyperedge_builder: HyperEdgeBuilder | None = None,
        edge_cluster_builder: EdgeClusterBuilder | None = None,
    ) -> "Memory":
        return cls(
            MemoryConfig.load(config),
            extractor=extractor,
            hyperedge_builder=hyperedge_builder,
            edge_cluster_builder=edge_cluster_builder,
        )

    def reset(self, namespace: str = "default") -> None:
        self.store.reset_namespace(namespace)
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
        current_turn = self._next_turn(namespace, increment=2)
        output = self.ingestion.ingest_interaction(
            interaction,
            namespace=namespace,
            current_turn=current_turn,
        )
        self._persist_output(output)

    def add(
        self,
        messages: str | list[dict[str, Any]],
        namespace: str = "default",
        metadata: dict[str, Any] | None = None,
    ) -> None:
        message_objs = _normalize_messages(messages)
        batch = MemoryImportBatch(messages=message_objs, metadata=metadata or {})
        current_turn = self._next_turn(namespace, increment=max(1, len(message_objs)))
        output = self.ingestion.ingest_batch(batch, namespace=namespace, current_turn=current_turn)
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
        self.store.close()

    def _next_turn(self, namespace: str, increment: int) -> int:
        current = self._turn_counters.get(namespace, 0)
        self._turn_counters[namespace] = current + increment
        return current

    def _record_access(self, namespace: str, node_ids: list[str], current_turn: int | None) -> None:
        if not node_ids:
            return
        nodes = self.store.get_nodes(namespace, node_ids)
        touched = [touch_node_access(node, current_turn) for node in nodes]
        self.store.upsert_nodes(touched)

    def _persist_output(self, output: Any) -> None:
        self.store.upsert_nodes(output.nodes)
        self.store.upsert_edges(output.edges)
        self.store.upsert_edge_clusters(output.edge_clusters)
        self.store.upsert_edge_cluster_members(output.edge_cluster_members)
        self.store.upsert_entity_aliases(output.entity_aliases)
        self.store.upsert_fact_properties(output.fact_properties)


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
