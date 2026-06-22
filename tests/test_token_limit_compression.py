from __future__ import annotations

from typing import Any, Sequence

from comgmem.config import MemoryConfig
from comgmem.memory import Memory
from comgmem.schema import MemoryExtraction
from comgmem.stores.vector_store import VectorIndexItem, VectorRecord, VectorSearchHit


def test_add_memory_compacts_long_user_and_assistant_messages_before_storage(tmp_path):
    llm = _CompressionLLM("compressed conversation detail")
    extractor = _RecordingExtractor()
    memory = Memory(
        MemoryConfig(
            storage={"path": str(tmp_path / "memory.sqlite3")},
            llm={"model": "test-llm", "max_tokens": 5},
            index={"use_embedding": False},
            ingestion={"pass_recent_context": False},
        ),
        extractor=extractor,
        maintenance_llm=llm,
    )

    memory.add_memory(
        user_input="alpha " * 20,
        assistant_output="beta " * 20,
        namespace="ns",
    )

    stored = memory.store.list_turn_messages("ns", ["turn:0"])
    assert [message.content for message in stored] == [
        "compressed conversation detail",
        "compressed conversation detail",
    ]
    assert stored[0].metadata["compression"]["content_type"] == "user input"
    assert stored[1].metadata["compression"]["content_type"] == "assistant output"
    assert [message.content for message in extractor.target_messages] == [
        "compressed conversation detail",
        "compressed conversation detail",
    ]
    assert len(llm.prompts) == 2

    memory.close()


def test_vector_index_text_is_compacted_before_embedding_and_upsert(tmp_path):
    llm = _CompressionLLM("compressed vector detail")
    embedding = _EmbeddingClient()
    vector_store = _VectorStore()
    memory = Memory(
        MemoryConfig(
            storage={"path": str(tmp_path / "memory.sqlite3")},
            llm={"model": "test-llm", "max_tokens": 1000},
            embedding={"model": "test-embedding", "max_tokens": 5},
            index={"use_embedding": True},
        ),
        extractor=_RecordingExtractor(),
        maintenance_llm=llm,
        embedding_client=embedding,
        vector_store=vector_store,
    )

    memory._index_items(
        "node_content",
        [VectorIndexItem(id="point:1", text="gamma " * 20, payload={"namespace": "ns"})],
    )

    assert embedding.texts == ["compressed vector detail"]
    assert vector_store.records[0].text == "compressed vector detail"
    assert vector_store.records[0].payload["compression"]["content_type"] == "node_content embedding text"
    assert len(llm.prompts) == 1

    memory.close()


def test_turn_dialogue_is_compacted_before_storage_when_combined_embedding_text_exceeds_limit(tmp_path):
    llm = _CompressionLLM("compressed turn detail")
    embedding = _EmbeddingClient()
    vector_store = _VectorStore()
    extractor = _RecordingExtractor()
    memory = Memory(
        MemoryConfig(
            storage={"path": str(tmp_path / "memory.sqlite3")},
            llm={"model": "test-llm", "max_tokens": 1000},
            embedding={"model": "test-embedding", "max_tokens": 12},
            index={"use_embedding": True},
            ingestion={"pass_recent_context": False},
        ),
        extractor=extractor,
        maintenance_llm=llm,
        embedding_client=embedding,
        vector_store=vector_store,
    )

    memory.add_memory(
        user_input="alpha " * 8,
        assistant_output="beta " * 8,
        namespace="ns",
    )

    stored = memory.store.list_turn_messages("ns", ["turn:0"])
    assert [(message.role, message.content) for message in stored] == [
        ("dialogue_summary", "compressed turn detail")
    ]
    assert stored[0].metadata["compression"]["content_type"] == "conversation turn dialogue"
    assert [(message.role, message.content) for message in extractor.target_messages] == [
        ("dialogue_summary", "compressed turn detail")
    ]
    assert embedding.texts == ["Dialogue summary: compressed turn detail"]
    assert len(llm.prompts) == 1

    memory.close()


def test_text_under_configured_limits_is_not_compacted(tmp_path):
    llm = _CompressionLLM("unused")
    extractor = _RecordingExtractor()
    memory = Memory(
        MemoryConfig(
            storage={"path": str(tmp_path / "memory.sqlite3")},
            llm={"model": "test-llm", "max_tokens": 1000},
            index={"use_embedding": False},
            ingestion={"pass_recent_context": False},
        ),
        extractor=extractor,
        maintenance_llm=llm,
    )

    memory.add("short message", namespace="ns")

    stored = memory.store.list_turn_messages("ns", ["turn:0"])
    assert stored[0].content == "short message"
    assert "compression" not in stored[0].metadata
    assert llm.prompts == []

    memory.close()


class _CompressionLLM:
    def __init__(self, compressed_text: str) -> None:
        self.compressed_text = compressed_text
        self.prompts: list[str] = []

    def generate_json(self, prompt: str) -> dict[str, Any]:
        self.prompts.append(prompt)
        return {"compressed_text": self.compressed_text}


class _RecordingExtractor:
    def __init__(self) -> None:
        self.target_messages = []

    def extract(self, window: Any, context: Any) -> MemoryExtraction:
        self.target_messages = list(window.target)
        return MemoryExtraction()


class _EmbeddingClient:
    def __init__(self) -> None:
        self.texts: list[str] = []

    def embed(self, texts: list[str]) -> list[list[float]]:
        self.texts = list(texts)
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
