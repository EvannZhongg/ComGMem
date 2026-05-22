from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
import yaml

from c_hypermem import Memory
from c_hypermem.config import MemoryConfig
from c_hypermem.embeddings import EmbeddingModelClient
from c_hypermem.errors import IngestionNotConfiguredError
from c_hypermem.schema import HyperEdge, IngestionOutput, LocalNodeGraph, MemoryNode
from c_hypermem.utils.ids import make_edge_id, make_node_id
from c_hypermem.utils.time import make_time_bundle


def test_add_requires_explicit_extractor(tmp_path):
    memory = Memory.from_config({"storage": {"path": str(tmp_path / "memory.sqlite3")}})
    namespace = "test_ns"
    memory.reset(namespace)

    with pytest.raises(IngestionNotConfiguredError):
        memory.add_memory(
            user_input="Alice prefers morning interviews.",
            assistant_output="I will remember that.",
            namespace=namespace,
            metadata={"session_id": "S1", "date": "2024-01-03"},
        )

    assert memory.stats(namespace)["nodes"] == 0
    memory.close()


def test_add_uses_explicit_extractor_only(tmp_path):
    extractor = StaticExtractor()
    memory = Memory.from_config(
        {"storage": {"path": str(tmp_path / "memory.sqlite3")}},
        extractor=extractor,
    )
    namespace = "explicit_ns"
    memory.reset(namespace)

    memory.add(
        [{"role": "user", "content": "raw input is not parsed by C-HyperMem"}],
        namespace=namespace,
        metadata={"session_id": "S1", "date": "2024-01-03"},
    )
    results = memory.search("morning interviews", namespace=namespace, top_k=3)
    stats = memory.stats(namespace)
    memory.close()

    assert extractor.called
    assert stats["nodes"] == 1
    assert stats["hyper_edges"] == 1
    assert stats["triples"] == 0
    assert results
    assert "Alice prefers morning interviews" in results[0]["content"]
    assert results[0]["metadata"]["edge_types"] == ["state"]


def test_sqlite_hyper_edges_use_member_table(tmp_path):
    db_path = tmp_path / "memory.sqlite3"
    memory = Memory.from_config({"storage": {"path": str(db_path)}})
    memory.close()

    with sqlite3.connect(db_path) as conn:
        edge_columns = {row[1] for row in conn.execute("PRAGMA table_info(hyper_edges)").fetchall()}
        member_columns = {row[1] for row in conn.execute("PRAGMA table_info(hyper_edge_members)").fetchall()}

    assert "node_ids_json" not in edge_columns
    assert "roles_json" not in edge_columns
    assert "weights_json" not in edge_columns
    assert {"edge_key", "member_policy", "member_signature", "member_version"} <= edge_columns
    assert {"edge_id", "node_id", "role", "weight"} <= member_columns


def test_default_config_includes_split_config_files():
    config = MemoryConfig.load("configs/default.yaml")
    default_raw = yaml.safe_load(Path("configs/default.yaml").read_text(encoding="utf-8"))

    assert config.llm is not None
    assert config.llm.provider == "openai_compatible"
    assert config.llm.model == "${CHYPERMEM_LLM_MODEL}"
    assert config.llm.base_url == "${CHYPERMEM_LLM_BASE_URL}"
    assert config.llm.api_key == "${CHYPERMEM_LLM_API_KEY}"
    assert config.embedding is not None
    assert config.embedding.provider == "openai_compatible"
    assert config.embedding.model == "${CHYPERMEM_EMBEDDING_MODEL}"
    assert config.embedding.base_url == "${CHYPERMEM_EMBEDDING_BASE_URL}"
    assert config.embedding.api_key == "${CHYPERMEM_EMBEDDING_API_KEY}"
    assert default_raw["include"] == ["models.yaml", "node_types.yaml"]
    assert "node_types" not in default_raw
    assert config.extraction.prompt == "extraction/memory_extraction.md"
    assert "entity" in config.node_types.types
    assert config.hyperedges.basic_edge_types == ["evidence", "state", "correction"]


def test_embedding_model_client_is_generic_entrypoint():
    config = MemoryConfig.load("configs/default.yaml")
    client = EmbeddingModelClient.from_config(config.embedding)

    assert client.config.model == "${CHYPERMEM_EMBEDDING_MODEL}"


class StaticExtractor:
    def __init__(self) -> None:
        self.called = False

    def extract(self, messages, context):
        self.called = True
        node = MemoryNode(
            id=make_node_id(context.namespace, "preference", "alice-morning-interviews"),
            namespace=context.namespace,
            type="preference",
            content="Alice prefers morning interviews.",
            summary="Alice prefers morning interviews.",
            metadata={
                "source_session_id": context.metadata.get("session_id"),
                "date": context.metadata.get("date"),
            },
            time=make_time_bundle(
                current_turn=context.current_turn,
                event_time=context.metadata.get("date"),
                valid_start=context.metadata.get("date"),
            ),
            local_graph=LocalNodeGraph(),
            dedupe_key="preference:alice-morning-interviews",
        )
        edge = HyperEdge(
            id=make_edge_id(
                context.namespace,
                "state",
                "describes_entity_state",
                "profile:alice_preferences",
            ),
            namespace=context.namespace,
            edge_type="state",
            relation="describes_entity_state",
            edge_key="profile:alice_preferences",
            member_policy="appendable",
            node_ids=[node.id],
            roles={node.id: "preference_evidence"},
            weights={node.id: 1.0},
            metadata={"created_by": "test_extractor"},
            time=make_time_bundle(current_turn=context.current_turn),
        )
        return IngestionOutput(nodes=[node], edges=[edge])
