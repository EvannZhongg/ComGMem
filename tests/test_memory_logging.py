from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Sequence

from comgmem.config import MemoryConfig
from comgmem.memory import Memory
from comgmem.schema import MemoryExtraction
from comgmem.stores.vector_store import VectorRecord, VectorSearchHit


def test_memory_storage_log_is_written_under_namespace_directory(tmp_path):
    log_dir = tmp_path / "logs"
    memory = Memory(
        MemoryConfig(
            storage={"path": str(tmp_path / "memory.sqlite3")},
            logging={"path": str(log_dir)},
            embedding={"model": "test-embedding", "max_tokens": 1000},
            index={"use_embedding": True},
            ingestion={"pass_recent_context": False},
        ),
        extractor=_EmptyExtractor(),
        embedding_client=_EmbeddingClient(),
        vector_store=_VectorStore(),
    )

    memory.add_memory(user_input="remember alpha", assistant_output="stored beta", namespace="tenant/a")

    record = _read_single_log_record(log_dir / "tenant%2Fa" / "memory_writes.jsonl")
    assert record["event"] == "memory_storage"
    assert record["namespace"] == "tenant/a"
    assert record["turn_id"] == "turn:0"
    assert record["operation"] == "add_memory"
    assert record["token_usage"]["input_message_tokens"] > 0
    assert record["token_usage"]["stored_message_tokens"] > 0
    assert record["token_usage"]["embedding_input_tokens"] > 0
    assert _step_names(record) == [
        "receive_input",
        "append_turn",
        "ingest_messages",
        "vector_index",
        "persist_output",
    ]
    vector_step = next(step for step in record["steps"] if step["name"] == "vector_index")
    assert vector_step["item_type"] == "turn_dialogue"
    assert vector_step["embedding_input_tokens"] > 0

    memory.close()


def test_memory_storage_log_records_compaction_token_usage(tmp_path):
    log_dir = tmp_path / "logs"
    memory = Memory(
        MemoryConfig(
            storage={"path": str(tmp_path / "memory.sqlite3")},
            logging={"path": str(log_dir)},
            llm={"model": "test-llm", "max_tokens": 5},
            index={"use_embedding": False},
            ingestion={"pass_recent_context": False},
        ),
        extractor=_EmptyExtractor(),
        maintenance_llm=_CompressionLLM("compressed detail"),
    )

    memory.add("alpha " * 20, namespace="ns")

    record = _read_single_log_record(log_dir / "ns" / "memory_writes.jsonl")
    assert "message_compaction" in _step_names(record)
    assert record["token_usage"]["compression_source_tokens"] > 0
    assert record["token_usage"]["compression_prompt_tokens"] > 0
    assert record["token_usage"]["compression_output_tokens"] > 0

    memory.close()


def _read_single_log_record(path: Path) -> dict[str, Any]:
    rows = path.read_text(encoding="utf-8").splitlines()
    assert len(rows) == 1
    return json.loads(rows[0])


def _step_names(record: dict[str, Any]) -> list[str]:
    return [step["name"] for step in record["steps"]]


class _EmptyExtractor:
    def extract(self, window: Any, context: Any) -> MemoryExtraction:
        return MemoryExtraction()


class _CompressionLLM:
    def __init__(self, compressed_text: str) -> None:
        self.compressed_text = compressed_text

    def generate_json(self, prompt: str) -> dict[str, Any]:
        return {"compressed_text": self.compressed_text}


class _EmbeddingClient:
    def embed(self, texts: list[str]) -> list[list[float]]:
        return [[1.0, 0.0] for _ in texts]


class _VectorStore:
    def __init__(self) -> None:
        self.records: list[VectorRecord] = []

    def upsert(self, records: Sequence[VectorRecord]) -> None:
        self.records.extend(records)

    def search(
        self,
        *,
        query: str,
        vector: list[float],
        top_k: int,
        filters: dict[str, Any] | None = None,
    ) -> list[VectorSearchHit]:
        return []

    def delete(self, ids: Sequence[str]) -> None:
        return None

    def delete_namespace(self, namespace: str) -> None:
        return None

    def close(self) -> None:
        return None
