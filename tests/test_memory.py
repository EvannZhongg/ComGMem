from __future__ import annotations

import os
import sqlite3
from pathlib import Path

import pytest
import yaml

from c_hypermem import Memory
from c_hypermem.config import MemoryConfig
from c_hypermem.embeddings import EmbeddingModelClient
from c_hypermem.errors import IngestionNotConfiguredError
from c_hypermem.schema import MemoryExtraction
from c_hypermem.utils.prompts import PromptRegistry


def test_add_requires_extractor_when_no_llm_is_configured(tmp_path):
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
    assert stats["nodes"] == 3
    assert stats["nodes.entity"] == 1
    assert stats["nodes.person"] == 1
    assert stats["nodes.fact"] == 1
    assert stats["nodes.event"] == 1
    assert stats["hyper_edges"] == 2
    assert stats["edge_clusters"] == 2
    assert stats["edge_cluster_members"] == 2
    assert stats["triples"] >= 1
    assert stats["entity_aliases"] >= 1
    assert stats["fact_properties"] == 1
    assert results
    assert "Alice prefers morning interviews" in results[0]["content"]
    assert "state" in results[0]["metadata"]["edge_types"]


def test_sqlite_hyper_edges_use_member_table(tmp_path):
    db_path = tmp_path / "memory.sqlite3"
    memory = Memory.from_config({"storage": {"path": str(db_path)}})
    memory.close()

    with sqlite3.connect(db_path) as conn:
        node_columns = {row[1] for row in conn.execute("PRAGMA table_info(nodes)").fetchall()}
        edge_columns = {row[1] for row in conn.execute("PRAGMA table_info(hyper_edges)").fetchall()}
        member_columns = {row[1] for row in conn.execute("PRAGMA table_info(hyper_edge_members)").fetchall()}
        cluster_columns = {row[1] for row in conn.execute("PRAGMA table_info(edge_clusters)").fetchall()}
        triple_columns = {row[1] for row in conn.execute("PRAGMA table_info(triples)").fetchall()}
        fact_index_columns = {row[1] for row in conn.execute("PRAGMA table_info(fact_property_index)").fetchall()}
        alias_index_columns = {row[1] for row in conn.execute("PRAGMA table_info(entity_alias_index)").fetchall()}

    assert {"canonical_text", "normalized_text", "fingerprint", "node_labels_json"} <= node_columns
    assert "node_type" not in node_columns
    assert "node_ids_json" not in edge_columns
    assert "roles_json" not in edge_columns
    assert "weights_json" not in edge_columns
    assert "edge_key" not in edge_columns
    assert {"edge_fingerprint", "description", "polarity", "status"} <= edge_columns
    assert {"member_policy", "member_signature", "member_version"} <= edge_columns
    assert {"edge_id", "node_id", "role", "weight"} <= member_columns
    assert {"cluster_id", "cluster_fingerprint", "canonical_description", "conflict_state"} <= cluster_columns
    assert {"scope_edge_id", "scope_cluster_id", "role_in_edge", "edge_relation"} <= triple_columns
    assert {"subject_node_id", "fact_node_id"} <= fact_index_columns
    assert {"node_id"} <= alias_index_columns
    assert "entity_id" not in alias_index_columns
    assert "fact_id" not in fact_index_columns


def test_default_config_includes_split_config_files():
    config = MemoryConfig.load("configs/default.yaml")
    dict_config = MemoryConfig.load({"include": ["configs/models.yaml", "configs/node_labels.yaml"]})
    default_raw = yaml.safe_load(Path("configs/default.yaml").read_text(encoding="utf-8"))

    assert config.llm is not None
    assert config.llm.provider == "openai_compatible"
    assert config.llm.model == os.getenv("CHYPERMEM_LLM_MODEL", "${CHYPERMEM_LLM_MODEL}")
    assert config.llm.base_url == os.getenv("CHYPERMEM_LLM_BASE_URL", "${CHYPERMEM_LLM_BASE_URL}")
    assert config.llm.api_key == os.getenv("CHYPERMEM_LLM_API_KEY", "${CHYPERMEM_LLM_API_KEY}")
    assert config.embedding is not None
    assert config.embedding.provider == "openai_compatible"
    assert config.embedding.model == os.getenv("CHYPERMEM_EMBEDDING_MODEL", "${CHYPERMEM_EMBEDDING_MODEL}")
    assert config.embedding.base_url == os.getenv("CHYPERMEM_EMBEDDING_BASE_URL", "${CHYPERMEM_EMBEDDING_BASE_URL}")
    assert config.embedding.api_key == os.getenv("CHYPERMEM_EMBEDDING_API_KEY", "${CHYPERMEM_EMBEDDING_API_KEY}")
    assert default_raw["include"] == ["models.yaml", "node_labels.yaml"]
    assert "node_types" not in default_raw
    assert config.extraction.prompt == "extraction/memory_extraction.md"
    assert config.extraction.pass_node_labels_to_prompt
    assert "entity" in config.node_labels.labels
    assert config.node_identity.strategy == "canonical_fingerprint"
    assert not config.node_identity.include_node_labels
    assert config.hyperedges.basic_edge_types == ["evidence", "state", "correction"]
    assert config.edge_clusters.enabled
    assert config.local_graph.configured_by_node_labels
    assert config.node_labels.labels["event"].indexing.time_index
    assert dict_config.llm is not None
    assert dict_config.node_labels.labels["fact"].property_index


def test_embedding_model_client_is_generic_entrypoint():
    config = MemoryConfig.load("configs/default.yaml")
    client = EmbeddingModelClient.from_config(config.embedding)

    assert client.config.model == os.getenv("CHYPERMEM_EMBEDDING_MODEL", "${CHYPERMEM_EMBEDDING_MODEL}")


def test_maintenance_prompt_registry_loads_edge_prompts():
    registry = PromptRegistry()

    assert registry.load("maintenance.edge_merge").hash.startswith("sha256:")
    assert registry.load("maintenance.edge_cluster_merge").hash.startswith("sha256:")
    assert registry.load("maintenance.edge_conflict_check").hash.startswith("sha256:")


def test_conflicting_fact_retires_old_fact_and_adds_correction_edge(tmp_path):
    extractor = SequenceExtractor(
        [
            {
                "entities": [{"name": "Toby", "type": "pet"}],
                "events": [{"summary": "The user described Toby.", "participants": [{"name": "Toby"}]}],
                "assertions": [{"subject": "Toby", "predicate": "is_a", "object": "dog"}],
                "sources": [{"text": "Toby is a dog.", "ref": "user_input"}],
            },
            {
                "entities": [{"name": "Toby", "type": "pet"}],
                "events": [{"summary": "The user corrected Toby.", "participants": [{"name": "Toby"}]}],
                "assertions": [{"subject": "Toby", "predicate": "is_a", "object": "cat"}],
                "sources": [{"text": "Toby is my cat.", "ref": "user_input"}],
            },
        ]
    )
    memory = Memory.from_config(
        {"storage": {"path": str(tmp_path / "memory.sqlite3")}},
        extractor=extractor,
    )
    namespace = "conflict_ns"
    memory.reset(namespace)

    memory.add_memory("Toby is a dog.", namespace=namespace, metadata={"session_id": "S2", "date": "2024-01-03"})
    memory.add_memory("Toby is my cat.", namespace=namespace, metadata={"session_id": "S2", "date": "2024-01-04"})
    nodes = memory.store.list_nodes(namespace)
    edges = memory.store.list_edges(namespace)
    stats = memory.stats(namespace)
    memory.close()

    retired = [node for node in nodes if node.status == "retired"]
    assert retired
    assert retired[0].attributes["object"] == "dog"
    assert any(edge.edge_type == "correction" for edge in edges)
    assert stats["fact_properties"] == 2


class StaticExtractor:
    def __init__(self) -> None:
        self.called = False

    def extract(self, messages, context):
        self.called = True
        return MemoryExtraction.model_validate(
            {
                "entities": [{"name": "Alice", "labels": ["person"], "aliases": []}],
                "events": [
                    {
                        "summary": "Alice discussed interview scheduling.",
                        "time": context.metadata.get("date"),
                        "participants": [{"name": "Alice", "role": "speaker"}],
                    }
                ],
                "assertions": [
                    {
                        "subject": "Alice",
                        "predicate": "prefers",
                        "object": "morning interviews",
                        "source_ref": "user_input",
                    }
                ],
                "sources": [{"text": "Alice prefers morning interviews.", "ref": "user_input"}],
            }
        )


class SequenceExtractor:
    def __init__(self, payloads):
        self.payloads = list(payloads)
        self.index = 0

    def extract(self, messages, context):
        payload = self.payloads[self.index]
        self.index += 1
        return MemoryExtraction.model_validate(payload)
