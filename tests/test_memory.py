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
from c_hypermem.pipeline.context import AssemblyContext
from c_hypermem.pipeline.entity_resolution import EntityResolution
from c_hypermem.pipeline.local_graph_builder import LocalGraphBuilder
from c_hypermem.pipeline.maintenance import GraphMaintenance
from c_hypermem.pipeline.node_builder import NodeBuilder, collect_entities
from c_hypermem.pipeline.extraction import ExtractionContext, ExtractionWindow, LLMMemoryExtractor, _render_node_labels
from c_hypermem.retrieval.expansion import EdgeExpansion
from c_hypermem.schema import ExtractedAssertion, ExtractedEntity, MemoryExtraction, Message
from c_hypermem.stores.vector_store import (
    QdrantVectorStore,
    collect_node_local_graph_index_items,
    make_vector_point_id,
    node_local_graph_embedding_text,
    triple_embedding_text,
    turn_dialogue_embedding_text,
)
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
    nodes = memory.store.list_nodes(namespace)
    edges = memory.store.list_edges(namespace)
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
    assert stats["turns"] == 1
    assert stats["turn_messages"] == 1
    assert results
    assert "Alice prefers morning interviews" in results[0]["content"]
    assert "state" in results[0]["metadata"]["edge_types"]
    assert all(node.metadata.get("source_turn_ids") == ["turn:0"] for node in nodes)
    assert all(edge.metadata.get("source_turn_ids") == ["turn:0"] for edge in edges)


def test_sqlite_hyper_edges_use_member_table(tmp_path):
    db_path = tmp_path / "memory.sqlite3"
    memory = Memory.from_config({"storage": {"path": str(db_path)}})
    memory.close()

    with sqlite3.connect(db_path) as conn:
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()}
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
    assert "ingestion_cache" not in tables
    assert "turns" in tables


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
    assert config.embedding.batch_size == 10
    assert default_raw["include"] == ["models.yaml", "node_labels.yaml"]
    assert default_raw["ingestion"]["context_window_messages"] == 3
    assert "incremental_build" not in default_raw["ingestion"]
    assert config.ingestion.context_window_messages == 3
    assert "node_types" not in default_raw
    assert config.extraction.prompt == "extraction/memory_extraction.md"
    assert config.extraction.pass_node_labels_to_prompt
    assert "entity" in config.node_labels.labels
    assert {"turn", "event", "fact", "entity", "state", "preference", "task", "instruction", "tool"} <= set(
        config.node_labels.labels
    )
    assert config.node_identity.strategy == "canonical_fingerprint"
    assert not config.node_identity.include_node_labels
    assert config.hyperedges.basic_edge_types == ["evidence", "state", "correction"]
    assert config.edge_clusters.enabled
    assert config.edge_clusters.maintenance_prompts.fact_merge == "maintenance/fact_merge.md"
    assert config.edge_clusters.maintenance_prompts.contradiction_check == "maintenance/contradiction_check.md"
    assert config.edge_clusters.background_maintenance.trigger_every_k_writes == 100
    assert config.index.vector == "qdrant"
    assert config.index.vector_store.backend == "qdrant"
    assert config.index.vector_store.collection_name == "c_hypermem_memory"
    assert config.local_graph.configured_by_node_labels
    assert config.node_labels.labels["event"].indexing.time_index
    assert dict_config.llm is not None
    assert dict_config.node_labels.labels["fact"].property_index


def test_embedding_model_client_is_generic_entrypoint():
    config = MemoryConfig.load("configs/default.yaml")
    client = EmbeddingModelClient.from_config(config.embedding)

    assert client.config.model == os.getenv("CHYPERMEM_EMBEDDING_MODEL", "${CHYPERMEM_EMBEDDING_MODEL}")


def test_embedding_model_client_batches_requests():
    client = EmbeddingModelClient.from_config(
        {
            "model": "embedding-test",
            "batch_size": 2,
        }
    )
    fake_client = FakeEmbeddingClient()
    client._client = fake_client

    embeddings = client.embed(["a", "b", "c", "d", "e"])

    assert fake_client.inputs == [["a", "b"], ["c", "d"], ["e"]]
    assert len(embeddings) == 5


def test_memory_indexes_node_local_graph_as_single_vector(tmp_path):
    embedding_client = RecordingEmbeddingClient()
    vector_store = RecordingVectorStore()
    memory = Memory.from_config(
        {"storage": {"path": str(tmp_path / "memory.sqlite3")}},
        extractor=StaticExtractor(),
        embedding_client=embedding_client,
        vector_store=vector_store,
    )
    namespace = "vector_triples_ns"
    memory.reset(namespace)

    memory.add_memory("Alice prefers morning interviews.", namespace=namespace)
    nodes = memory.store.list_nodes(namespace)
    fact_nodes = [node for node in nodes if "fact" in node.node_labels]
    memory.close()

    assert fact_nodes
    fact_local_graph_text = node_local_graph_embedding_text(fact_nodes[0])
    assert embedding_client.inputs == [
        [
            "User: Alice prefers morning interviews.",
        ],
        [
            "Alice discussed interview scheduling.",
            "Alice",
            "Alice prefers morning interviews",
        ],
        [
            "Alice discussed interview scheduling.",
            "Alice",
            "Alice prefers morning interviews",
        ],
        [
            (
                "Core content: Alice discussed interview scheduling.\n"
                "Related facts:\n"
                "- Alice participated_as speaker"
            ),
            fact_local_graph_text,
        ],
        [
            "evidence: supports_extracted_facts",
            "Alice prefers morning interviews",
        ],
        [
            "Alice discussed interview scheduling. supports 1 extracted fact(s).",
            "Alice prefers morning interviews",
        ],
    ]
    assert len(vector_store.records) == 13
    item_types = [record.payload["item_type"] for record in vector_store.records]
    assert item_types.count("turn_dialogue") == 1
    assert item_types.count("node_content") == 3
    assert item_types.count("node_summary") == 3
    assert item_types.count("node_local_graph") == 2
    assert item_types.count("edge_cluster_canonical") == 2
    assert item_types.count("edge_cluster_variant") == 2
    record = next(
        item
        for item in vector_store.records
        if item.payload["item_type"] == "node_local_graph" and item.payload["node_id"] == fact_nodes[0].node_id
    )
    assert record.payload["item_type"] == "node_local_graph"
    assert record.payload["namespace"] == namespace
    assert record.payload["node_id"] == fact_nodes[0].node_id
    assert record.payload["triple_ids"] == [fact_nodes[0].local_graph.triples[0].triple_id]
    assert record.payload["triple_count"] == 1
    assert record.id == make_vector_point_id(namespace, "triple", fact_nodes[0].node_id)
    assert vector_store.deleted_namespaces == [namespace]
    assert vector_store.closed


def test_turn_dialogue_vector_indexes_user_and_assistant_by_turn(tmp_path):
    embedding_client = RecordingEmbeddingClient()
    vector_store = RecordingVectorStore()
    memory = Memory.from_config(
        {"storage": {"path": str(tmp_path / "memory.sqlite3")}},
        extractor=StaticExtractor(),
        embedding_client=embedding_client,
        vector_store=vector_store,
    )
    namespace = "turn_dialogue_vector_ns"
    memory.reset(namespace)

    memory.add_memory(
        user_input="帮我查一下去北京的航班，另外我是素食主义者，帮我备注一下航空餐。",
        assistant_output="已经为您查询到明天早上 8 点的航班，并为您备注了素食餐。",
        namespace=namespace,
        observations=[{"type": "tool", "content": "工具输出不应进入 turn dialogue 向量。"}],
    )
    memory.close()

    turn_records = [
        record
        for record in vector_store.records
        if record.payload["item_type"] == "turn_dialogue"
    ]
    assert len(turn_records) == 1
    assert turn_records[0].text == (
        "User: 帮我查一下去北京的航班，另外我是素食主义者，帮我备注一下航空餐。\n"
        "Assistant: 已经为您查询到明天早上 8 点的航班，并为您备注了素食餐。"
    )
    assert turn_records[0].payload["namespace"] == namespace
    assert turn_records[0].payload["turn_id"] == "turn:0"
    assert turn_records[0].payload["turn_index"] == 0
    assert turn_records[0].payload["roles"] == ["User", "Assistant"]
    assert "工具输出" not in turn_records[0].text
    assert turn_records[0].id == make_vector_point_id(namespace, "turn_dialogue", "turn:0")


def test_turn_dialogue_embedding_text_skips_non_dialogue_roles():
    text = turn_dialogue_embedding_text(
        [
            Message(role="user", content="Question"),
            Message(role="observation:tool", content="Tool result"),
            Message(role="assistant", content="Answer"),
        ]
    )

    assert text == "User: Question\nAssistant: Answer"


def test_default_qdrant_vector_stores_use_separate_collections(tmp_path):
    memory = Memory.from_config(
        {
            "storage": {"path": str(tmp_path / "memory.sqlite3")},
            "embedding": {"model": "embedding-test"},
            "index": {
                "vector": "qdrant",
                "vector_store": {"path": str(tmp_path / "vectors"), "collection_name": "test_collection"},
            },
        },
        extractor=StaticExtractor(),
    )

    stores = memory.vector_stores
    collection_names = {
        item_type: store.collection_name
        for item_type, store in stores.items()
        if isinstance(store, QdrantVectorStore)
    }
    memory.close()

    assert collection_names == {
        "triple": "test_collection_triples",
        "node_content": "test_collection_node_content",
        "node_summary": "test_collection_node_summary",
        "edge_cluster_canonical": "test_collection_edge_cluster_canonical",
        "edge_cluster_variant": "test_collection_edge_cluster_variant",
        "turn_dialogue": "test_collection_turn_dialogue",
    }


def test_collect_node_local_graph_index_items_builds_one_vector_per_node_and_skips_unpersisted_triples():
    builder = NodeBuilder(LocalGraphBuilder())
    context = AssemblyContext(namespace="skip_ns", metadata={}, current_turn=0)
    subject = builder.build_or_update_entity_node(
        ExtractedEntity(name="Alice"),
        resolution=EntityResolution(aliases={"Alice"}),
        context=context,
    )
    fact = builder.build_fact_node(
        ExtractedAssertion(subject="Alice", predicate="prefers", object="tea"),
        subject,
        context,
    )

    assert fact.local_graph.triples[0].triple_id is None
    assert triple_embedding_text(fact.local_graph.triples[0]) == "Alice prefers tea"
    assert node_local_graph_embedding_text(fact) == "Core content: Alice prefers tea"
    assert collect_node_local_graph_index_items([fact]) == []


def test_memory_does_not_create_default_vector_store_without_embedding_config(tmp_path):
    memory = Memory.from_config({"storage": {"path": str(tmp_path / "memory.sqlite3")}}, extractor=StaticExtractor())

    assert memory.vector_store is None
    assert memory.embedding_client is None

    memory.close()


def test_memory_closes_default_vector_store_when_configured(tmp_path):
    memory = Memory.from_config(
        {
            "storage": {"path": str(tmp_path / "memory.sqlite3")},
            "embedding": {"model": "embedding-test"},
            "index": {
                "vector": "qdrant",
                "vector_store": {"path": str(tmp_path / "vectors"), "collection_name": "test_collection"},
            },
        },
        extractor=StaticExtractor(),
    )

    assert memory.vector_store is not None

    memory.close()


def test_maintenance_prompt_registry_loads_edge_prompts():
    registry = PromptRegistry()

    assert registry.load("maintenance.fact_merge").hash.startswith("sha256:")
    assert registry.load("maintenance.contradiction_check").hash.startswith("sha256:")
    assert registry.load("maintenance.edge_merge").hash.startswith("sha256:")
    assert registry.load("maintenance.edge_cluster_merge").hash.startswith("sha256:")
    assert registry.load("maintenance.edge_conflict_check").hash.startswith("sha256:")


def test_default_policy_is_not_rendered_as_prompt_label():
    config = MemoryConfig.load("configs/default.yaml")
    rendered = _render_node_labels(config)

    assert "default_policy" not in rendered
    assert "Other precise labels are allowed" in rendered


def test_extraction_prompt_injects_node_label_config():
    config = MemoryConfig.load("configs/default.yaml")
    extractor = LLMMemoryExtractor(config, llm=StaticLLM({}))
    prompt = extractor._render_prompt(
        ExtractionWindow(
            context=[Message(role="user", content="My flight is to Beijing.")],
            target=[Message(role="user", content="Book the 8 AM one.")],
        ),
        ExtractionContext(namespace="prompt_ns", metadata={"session_id": "S1"}, current_turn=0),
    )

    assert "{{NODE_LABELS}}" not in prompt
    assert "{{INTERACTION_METADATA}}" not in prompt
    assert "{{RECENT_CONTEXT}}" not in prompt
    assert "{{TARGET_MESSAGES}}" not in prompt
    assert "{{STRICT_JSON_SHAPE}}" not in prompt
    assert "- entity:" in prompt
    assert "- instruction:" in prompt
    assert "Other precise labels are allowed" in prompt
    assert "node_id" in prompt
    assert "## Interaction Metadata" in prompt
    assert "## Context: Recent History" in prompt
    assert "## Target to Extract" in prompt
    assert "[context:0]" in prompt
    assert "[target:0]" in prompt
    assert "extract memories only from Target" in prompt


def test_add_memory_passes_recent_context_and_target_to_extractor(tmp_path):
    extractor = RecordingWindowExtractor()
    memory = Memory.from_config(
        {
            "storage": {"path": str(tmp_path / "memory.sqlite3")},
            "ingestion": {"context_window_messages": 1},
        },
        extractor=extractor,
    )
    namespace = "window_interaction_ns"
    memory.reset(namespace)

    memory.add_memory("I am going to Beijing tomorrow.", namespace=namespace)
    memory.add_memory("Book the 8 AM one.", namespace=namespace)
    memory.close()

    assert len(extractor.windows) == 2
    assert [message.content for message in extractor.windows[0].context] == []
    assert [message.content for message in extractor.windows[0].target] == ["I am going to Beijing tomorrow."]
    assert [message.content for message in extractor.windows[1].context] == ["I am going to Beijing tomorrow."]
    assert [message.content for message in extractor.windows[1].target] == ["Book the 8 AM one."]


def test_add_batches_as_incremental_single_message_targets(tmp_path):
    extractor = RecordingWindowExtractor()
    memory = Memory.from_config(
        {
            "storage": {"path": str(tmp_path / "memory.sqlite3")},
            "ingestion": {"context_window_messages": 2},
        },
        extractor=extractor,
    )
    namespace = "window_batch_ns"
    memory.reset(namespace)

    memory.add(
        [
            {"role": "user", "content": "Turn one."},
            {"role": "assistant", "content": "Turn two."},
            {"role": "user", "content": "Turn three."},
        ],
        namespace=namespace,
    )
    memory.close()

    assert len(extractor.windows) == 3
    assert [[message.content for message in window.target] for window in extractor.windows] == [
        ["Turn one."],
        ["Turn two."],
        ["Turn three."],
    ]
    assert [[message.content for message in window.context] for window in extractor.windows] == [
        [],
        ["Turn one."],
        ["Turn one.", "Turn two."],
    ]


def test_context_window_uses_persisted_turn_table(tmp_path):
    db_path = tmp_path / "memory.sqlite3"
    first_extractor = RecordingWindowExtractor()
    memory = Memory.from_config(
        {
            "storage": {"path": str(db_path)},
            "ingestion": {"context_window_messages": 2},
        },
        extractor=first_extractor,
    )
    namespace = "persisted_window_ns"
    memory.reset(namespace)
    memory.add_memory("First turn.", namespace=namespace)
    memory.close()

    second_extractor = RecordingWindowExtractor()
    memory = Memory.from_config(
        {
            "storage": {"path": str(db_path)},
            "ingestion": {"context_window_messages": 2},
        },
        extractor=second_extractor,
    )
    memory.add_memory("Second turn.", namespace=namespace)
    stats = memory.stats(namespace)
    memory.close()

    assert [message.content for message in second_extractor.windows[0].context] == ["First turn."]
    assert [message.content for message in second_extractor.windows[0].target] == ["Second turn."]
    assert stats["turns"] == 2
    assert stats["turn_messages"] == 2


def test_context_window_limit_counts_turns_not_turn_messages(tmp_path):
    extractor = RecordingWindowExtractor()
    memory = Memory.from_config(
        {
            "storage": {"path": str(tmp_path / "memory.sqlite3")},
            "ingestion": {"context_window_messages": 2},
        },
        extractor=extractor,
    )
    namespace = "window_turn_limit_ns"
    memory.reset(namespace)

    memory.add_memory("First user question.", namespace=namespace)
    memory.add_memory(
        "Second user question.",
        namespace=namespace,
        observations=[
            {"type": "tool", "content": f"tool observation {index}"}
            for index in range(5)
        ],
    )
    memory.add_memory("Third user question.", namespace=namespace)
    memory.close()

    assert [message.content for message in extractor.windows[2].target] == ["Third user question."]
    assert [message.content for message in extractor.windows[2].context] == [
        "First user question.",
        "Second user question.",
        "tool observation 0",
        "tool observation 1",
        "tool observation 2",
        "tool observation 3",
        "tool observation 4",
    ]


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
        maintenance_llm=MaintenanceLLM(
            [
                {
                    "conflict_state": "contradiction",
                    "affected_existing_refs": ["existing:0"],
                    "recommended_old_status": "retired",
                    "valid_time_update": {"old_end": "2024-01-04"},
                    "rationale": "The new species replaces the old species for the same pet.",
                }
            ]
        ),
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


def test_retired_fact_local_graph_vector_is_removed_from_vector_store(tmp_path):
    embedding_client = RecordingEmbeddingClient()
    vector_store = RecordingVectorStore()
    extractor = SequenceExtractor(
        [
            {
                "entities": [{"name": "Toby", "type": "pet"}],
                "events": [{"summary": "The user described Toby.", "participants": [{"name": "Toby"}]}],
                "assertions": [{"subject": "Toby", "predicate": "is_a", "object": "dog"}],
            },
            {
                "entities": [{"name": "Toby", "type": "pet"}],
                "events": [{"summary": "The user corrected Toby.", "participants": [{"name": "Toby"}]}],
                "assertions": [{"subject": "Toby", "predicate": "is_a", "object": "cat"}],
            },
        ]
    )
    memory = Memory.from_config(
        {"storage": {"path": str(tmp_path / "memory.sqlite3")}},
        extractor=extractor,
        maintenance_llm=MaintenanceLLM(
            [
                {
                    "conflict_state": "contradiction",
                    "affected_existing_refs": ["existing:0"],
                    "recommended_old_status": "retired",
                    "valid_time_update": {},
                    "rationale": "The new species replaces the old species for the same pet.",
                }
            ]
        ),
        embedding_client=embedding_client,
        vector_store=vector_store,
    )
    namespace = "retired_vector_ns"
    memory.reset(namespace)

    memory.add_memory("Toby is a dog.", namespace=namespace)
    memory.add_memory("Toby is my cat.", namespace=namespace)
    retired = [node for node in memory.store.list_nodes(namespace) if node.status == "retired"]
    memory.close()

    assert retired
    assert make_vector_point_id(namespace, "triple", retired[0].node_id) in vector_store.deleted_ids
    assert any("Related facts:\n- Toby is_a dog" in record.text for record in vector_store.records)
    assert any("Related facts:\n- Toby is_a cat" in record.text for record in vector_store.records)


def test_edge_cluster_builder_reuses_existing_property_cluster(tmp_path):
    extractor = SequenceExtractor(
        [
            {
                "entities": [{"name": "Alice"}],
                "assertions": [{"subject": "Alice", "predicate": "loves", "object": "tea"}],
            },
            {
                "entities": [{"name": "Alice"}],
                "assertions": [{"subject": "Alice", "predicate": "loves", "object": "coffee"}],
            },
        ]
    )
    memory = Memory.from_config(
        {"storage": {"path": str(tmp_path / "memory.sqlite3")}},
        extractor=extractor,
        maintenance_llm=MaintenanceLLM(
            [
                {
                    "conflict_state": "compatible",
                    "affected_existing_refs": [],
                    "recommended_old_status": "active",
                    "valid_time_update": {},
                    "rationale": "A person can love both tea and coffee.",
                }
            ]
        ),
    )
    namespace = "cluster_reuse_ns"
    memory.reset(namespace)

    memory.add_memory("Alice loves tea.", namespace=namespace)
    memory.add_memory("Alice loves coffee.", namespace=namespace)
    clusters = memory.store.list_edge_clusters(namespace)
    members = memory.store.list_edge_cluster_members(namespace)
    edges = memory.store.list_edges(namespace)
    state_edges = [edge for edge in edges if edge.edge_type == "state"]
    memory.close()

    state_cluster_ids = {member.cluster_id for member in members if member.edge_id in {edge.edge_id for edge in state_edges}}
    assert len(state_edges) == 2
    assert len(state_cluster_ids) == 1
    assert len(clusters) < len(edges)


def test_reused_edge_cluster_appends_description_variants(tmp_path):
    embedding_client = RecordingEmbeddingClient()
    vector_store = RecordingVectorStore()
    extractor = SequenceExtractor(
        [
            {
                "entities": [{"name": "Alice"}],
                "assertions": [{"subject": "Alice", "predicate": "loves", "object": "tea"}],
            },
            {
                "entities": [{"name": "Alice"}],
                "assertions": [{"subject": "Alice", "predicate": "loves", "object": "coffee"}],
            },
        ]
    )
    memory = Memory.from_config(
        {"storage": {"path": str(tmp_path / "memory.sqlite3")}},
        extractor=extractor,
        maintenance_llm=MaintenanceLLM(
            [
                {
                    "conflict_state": "compatible",
                    "affected_existing_refs": [],
                    "recommended_old_status": "active",
                    "valid_time_update": {},
                    "rationale": "A person can love both tea and coffee.",
                }
            ]
        ),
        embedding_client=embedding_client,
        vector_store=vector_store,
    )
    namespace = "cluster_variant_reuse_ns"
    memory.reset(namespace)

    memory.add_memory("Alice loves tea.", namespace=namespace)
    memory.add_memory("Alice loves coffee.", namespace=namespace)
    clusters = memory.store.list_edge_clusters(namespace)
    state_clusters = [cluster for cluster in clusters if "entity_state" in cluster.cluster_labels]
    memory.close()

    assert len(state_clusters) == 1
    variant_texts = [variant.text for variant in state_clusters[0].description_variants]
    assert variant_texts == ["Alice loves tea", "Alice loves coffee"]
    indexed_variant_texts = [
        record.text
        for record in vector_store.records
        if record.payload["item_type"] == "edge_cluster_variant"
    ]
    assert "Alice loves tea" in indexed_variant_texts
    assert "Alice loves coffee" in indexed_variant_texts


def test_retriever_delegates_graph_expansion(tmp_path):
    memory = Memory.from_config(
        {"storage": {"path": str(tmp_path / "memory.sqlite3")}},
        extractor=StaticExtractor(),
    )
    namespace = "retrieval_expansion_ns"
    memory.reset(namespace)

    memory.add_memory("Alice prefers morning interviews.", namespace=namespace)
    results = memory.search("Alice", namespace=namespace, top_k=5)
    memory.close()

    assert isinstance(memory.retriever.expansion, EdgeExpansion)
    assert any("edge_coherence" in result["metadata"]["score_parts"] for result in results)


def test_node_builder_delegates_local_graph_construction():
    builder = NodeBuilder(LocalGraphBuilder())
    context = AssemblyContext(namespace="builder_ns", metadata={"session_id": "S3", "date": "2024-01-05"}, current_turn=7)
    subject = builder.build_or_update_entity_node(
        ExtractedEntity(name="Alice", labels=["person"]),
        resolution=EntityResolution(aliases={"Alice"}),
        context=context,
    )
    fact = builder.build_fact_node(
        ExtractedAssertion(
            subject="Alice",
            predicate="prefers",
            object="morning interviews",
            source_ref="user_input",
        ),
        subject,
        context,
    )

    assert "preference" in fact.node_labels
    assert fact.local_graph.triples[0].subject == "Alice"
    assert fact.local_graph.triples[0].qualifiers["source_ref"] == "user_input"
    assert subject.local_graph.attributes == {}


def test_collect_entities_adds_event_participants_and_assertion_subjects():
    extraction = MemoryExtraction.model_validate(
        {
            "entities": [{"name": "Alice"}],
            "events": [{"summary": "A meeting happened.", "participants": [{"name": "Bob", "role": "speaker"}]}],
            "assertions": [{"subject": "Project Atlas", "predicate": "status", "object": "green"}],
        }
    )

    names = {entity.name for entity in collect_entities(extraction.events, extraction.assertions, extraction.entities)}

    assert names == {"Alice", "Bob", "Project Atlas"}


def test_graph_maintenance_uses_llm_for_contradiction_decisions(tmp_path):
    extractor = SequenceExtractor(
        [
            {"entities": [{"name": "Alice"}], "assertions": [{"subject": "Alice", "predicate": "loves", "object": "tea"}]},
            {
                "entities": [{"name": "Alice"}],
                "assertions": [{"subject": "Alice", "predicate": "loves", "object": "coffee"}],
            },
            {
                "entities": [{"name": "Alice"}],
                "assertions": [{"subject": "Alice", "predicate": "travels_to", "object": "Paris"}],
            },
            {
                "entities": [{"name": "Alice"}],
                "assertions": [{"subject": "Alice", "predicate": "travels_to", "object": "Berlin"}],
            },
        ]
    )
    maintenance_llm = MaintenanceLLM(
        [
            {
                "conflict_state": "compatible",
                "affected_existing_refs": [],
                "recommended_old_status": "active",
                "valid_time_update": {},
                "rationale": "A person can love both tea and coffee.",
            },
            {
                "conflict_state": "compatible",
                "affected_existing_refs": [],
                "recommended_old_status": "active",
                "valid_time_update": {},
                "rationale": "A person can travel to multiple places.",
            },
        ]
    )
    memory = Memory.from_config(
        {"storage": {"path": str(tmp_path / "memory.sqlite3")}},
        extractor=extractor,
        maintenance_llm=maintenance_llm,
    )
    namespace = "maintenance_llm_ns"
    memory.reset(namespace)

    memory.add_memory("Alice loves tea.", namespace=namespace)
    memory.add_memory("Alice loves coffee.", namespace=namespace)
    memory.add_memory("Alice travels to Paris.", namespace=namespace)
    memory.add_memory("Alice travels to Berlin.", namespace=namespace)
    nodes = memory.store.list_nodes(namespace)
    memory.close()

    assert maintenance_llm.call_count == 2
    assert not [node for node in nodes if node.status == "retired"]
    assert any("loves coffee" in node.content for node in nodes)
    assert any("travels_to Berlin" in node.content for node in nodes)


def test_graph_maintenance_requires_llm_for_overlapping_fact_checks(tmp_path):
    extractor = SequenceExtractor(
        [
            {"entities": [{"name": "Alice"}], "assertions": [{"subject": "Alice", "predicate": "loves", "object": "tea"}]},
            {
                "entities": [{"name": "Alice"}],
                "assertions": [{"subject": "Alice", "predicate": "loves", "object": "coffee"}],
            },
        ]
    )
    memory = Memory.from_config(
        {"storage": {"path": str(tmp_path / "memory.sqlite3")}},
        extractor=extractor,
    )
    namespace = "maintenance_requires_llm_ns"
    memory.reset(namespace)
    memory.add_memory("Alice loves tea.", namespace=namespace)

    with pytest.raises(RuntimeError, match="requires an LLM"):
        memory.add_memory("Alice loves coffee.", namespace=namespace)

    memory.close()


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


class RecordingWindowExtractor:
    def __init__(self):
        self.windows = []

    def extract(self, window, context):
        self.windows.append(window)
        return MemoryExtraction()


class StaticLLM:
    def __init__(self, payload):
        self.payload = payload

    def generate_json(self, prompt):
        return self.payload


class MaintenanceLLM:
    def __init__(self, payloads):
        self.payloads = list(payloads)
        self.prompts = []

    @property
    def call_count(self):
        return len(self.prompts)

    def generate_json(self, prompt):
        self.prompts.append(prompt)
        if not self.payloads:
            raise AssertionError("Unexpected maintenance LLM call")
        return self.payloads.pop(0)


class RecordingEmbeddingClient:
    def __init__(self):
        self.inputs = []

    def embed(self, texts):
        batch = list(texts)
        self.inputs.append(batch)
        return [[float(index), 1.0] for index, _ in enumerate(batch)]


class RecordingVectorStore:
    def __init__(self):
        self.records = []
        self.deleted_ids = []
        self.deleted_namespaces = []
        self.closed = False

    def upsert(self, records):
        self.records.extend(records)

    def delete(self, ids):
        self.deleted_ids.extend(ids)

    def delete_namespace(self, namespace):
        self.deleted_namespaces.append(namespace)

    def close(self):
        self.closed = True


class FakeEmbeddingClient:
    def __init__(self):
        self.inputs = []
        self.embeddings = FakeEmbeddings(self)


class FakeEmbeddings:
    def __init__(self, owner):
        self.owner = owner

    def create(self, *, model, input):
        self.owner.inputs.append(list(input))
        return FakeEmbeddingResponse(len(input))


class FakeEmbeddingResponse:
    def __init__(self, count):
        self.data = [FakeEmbeddingItem(index) for index in range(count)]


class FakeEmbeddingItem:
    def __init__(self, index):
        self.embedding = [float(index)]
