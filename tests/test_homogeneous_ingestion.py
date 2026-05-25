from __future__ import annotations

import pytest

from c_hypermem import Memory
from c_hypermem.pipeline.context import AssemblyContext
from c_hypermem.pipeline.local_graph_builder import LocalGraphBuilder
from c_hypermem.pipeline.node_builder import NodeBuilder
from c_hypermem.stores.vector_store import make_vector_point_id, node_local_graph_embedding_text
from c_hypermem.schema import ExtractedNode, MemoryExtraction


def test_node_builder_builds_homogeneous_node_from_extracted_node():
    builder = NodeBuilder(LocalGraphBuilder())
    node = builder.build_node(
        ExtractedNode.model_validate(
            {
                "ref": "n1",
                "labels": ["preference"],
                "canonical_text": "Alice prefers morning interviews.",
                "summaries": ["Alice prefers morning interviews."],
                "triples": [
                    {"subject": "Alice", "predicate": "prefers", "object": "morning interviews"},
                    {"subject": "Alice", "predicate": "prefers", "object": "morning interviews"},
                ],
                "edge_summary_refs": ["e1"],
            }
        ),
        AssemblyContext(namespace="builder_ns", metadata={"turn_ids": ["turn:0"]}, current_turn=0),
    )

    assert node.node_labels == ["preference"]
    assert node.content == "Alice prefers morning interviews."
    assert node.metadata["source_turn_ids"] == ["turn:0"]
    assert node.metadata["edge_summary_refs"] == ["e1"]
    assert len(node.local_graph.triples) == 1
    assert node.local_graph.triples[0].predicate == "prefers"


def test_ingestion_builds_nodes_and_description_only_hyperedges(tmp_path):
    memory = Memory.from_config(
        {"storage": {"path": str(tmp_path / "memory.sqlite3")}},
        extractor=StaticHomogeneousExtractor(),
    )
    namespace = "homogeneous_ns"
    memory.reset(namespace)

    memory.add_memory(
        user_input="Alice prefers morning interviews.",
        assistant_output="I will remember that.",
        namespace=namespace,
        metadata={"date": "2024-01-03", "session_id": "S1"},
    )
    nodes = memory.store.list_nodes(namespace)
    edges = memory.store.list_edges(namespace)
    clusters = memory.store.list_edge_clusters(namespace)
    stats = memory.stats(namespace)
    results = memory.search("morning interviews", namespace=namespace, top_k=3)
    memory.close()

    assert {label for node in nodes for label in node.node_labels} >= {"entity", "person", "preference", "event"}
    assert len(edges) == 2
    assert {edge.description for edge in edges} == {
        "Alice stated her morning interview preference in this interaction.",
        "Alice's interview scheduling preference.",
    }
    assert all(edge.metadata["source_turn_ids"] == ["turn:0"] for edge in edges)
    assert all("edge_summary_ref" in edge.metadata for edge in edges)
    assert all(edge.node_ids for edge in edges)
    assert stats["entity_aliases"] >= 1
    assert clusters
    assert results
    assert "Alice prefers morning interviews" in results[0]["content"]
    assert "edge_type" not in results[0]["metadata"]
    assert "edge_relation" not in results[0]["metadata"]
    assert "edge_roles" not in results[0]["metadata"]
    assert all(node.time.world.event_time for node in nodes)
    assert all(node.time.world.source_timestamp for node in nodes)


def test_entity_label_nodes_reuse_existing_alias_entry(tmp_path):
    memory = Memory.from_config(
        {"storage": {"path": str(tmp_path / "memory.sqlite3")}},
        extractor=SequenceHomogeneousExtractor(
            [
                {
                    "edge_summaries": [{"ref": "e1", "description": "Alice identity."}],
                    "nodes": [
                        {
                            "ref": "n1",
                            "labels": ["entity", "person"],
                            "canonical_text": "Alice",
                            "summaries": ["Alice is the user."],
                            "edge_summary_refs": ["e1"],
                            "metadata": {"aliases": ["Alice"]},
                        }
                    ],
                },
                {
                    "edge_summaries": [{"ref": "e1", "description": "Alice updated profile."}],
                    "nodes": [
                        {
                            "ref": "n1",
                            "labels": ["entity", "person"],
                            "canonical_text": "Alice",
                            "summaries": ["Alice is preparing for interviews."],
                            "edge_summary_refs": ["e1"],
                            "metadata": {"aliases": ["Alice"]},
                        }
                    ],
                },
            ]
        ),
    )
    namespace = "entity_alias_reuse_ns"
    memory.reset(namespace)

    memory.add_memory("I am Alice.", namespace=namespace)
    memory.add_memory("I am preparing for interviews.", namespace=namespace)
    nodes = memory.store.list_nodes(namespace)
    memory.close()

    assert len(nodes) == 1
    assert nodes[0].node_labels == ["entity", "person"]
    assert nodes[0].time.activation.updated_turn == 1


def test_node_summary_maintenance_concatenates_sources_below_k_and_reindexes(tmp_path):
    embedding_client = RecordingEmbeddingClient()
    vector_store = RecordingVectorStore()
    memory = Memory.from_config(
        {
            "storage": {"path": str(tmp_path / "memory.sqlite3")},
            "maintenance": {
                "node_summary": {
                    "compact_after_k_sources": 3,
                    "max_tokens": 1000,
                }
            },
        },
        extractor=SequenceHomogeneousExtractor(
            [
                _single_entity_payload("Alice is the user."),
                _single_entity_payload("Alice is preparing for interviews."),
            ]
        ),
        embedding_client=embedding_client,
        vector_store=vector_store,
    )
    namespace = "node_summary_concat_ns"
    memory.reset(namespace)

    memory.add_memory("I am Alice.", namespace=namespace)
    memory.add_memory("I am preparing for interviews.", namespace=namespace)
    node = memory.store.list_nodes(namespace)[0]
    memory.close()

    assert node.summary == "Alice is the user.\nAlice is preparing for interviews."
    summary_state = node.metadata["maintenance"]["node_summary"]
    assert summary_state["summary_source_turn_ids"] == ["turn:0", "turn:1"]
    assert summary_state["pending_source_turn_ids"] == ["turn:0", "turn:1"]
    summary_records = [
        record
        for record in vector_store.records
        if record.payload["item_type"] == "node_summary" and record.payload["node_id"] == node.node_id
    ]
    assert summary_records[-1].text == node.summary


def test_node_summary_maintenance_compacts_at_k_sources(tmp_path):
    maintenance_llm = MaintenanceLLM([{"summary": "Alice is a user preparing for interviews."}])
    memory = Memory.from_config(
        {
            "storage": {"path": str(tmp_path / "memory.sqlite3")},
            "maintenance": {
                "node_summary": {
                    "compact_after_k_sources": 2,
                    "max_tokens": 1000,
                }
            },
        },
        extractor=SequenceHomogeneousExtractor(
            [
                _single_entity_payload("Alice is the user."),
                _single_entity_payload("Alice is preparing for interviews."),
            ]
        ),
        maintenance_llm=maintenance_llm,
    )
    namespace = "node_summary_compact_k_ns"
    memory.reset(namespace)

    memory.add_memory("I am Alice.", namespace=namespace)
    memory.add_memory("I am preparing for interviews.", namespace=namespace)
    node = memory.store.list_nodes(namespace)[0]
    memory.close()

    assert node.summary == "Alice is a user preparing for interviews."
    assert maintenance_llm.call_count == 1
    assert "Alice is the user.\nAlice is preparing for interviews." in maintenance_llm.prompts[0]
    assert "node_id" not in maintenance_llm.prompts[0]
    summary_state = node.metadata["maintenance"]["node_summary"]
    assert summary_state["pending_source_turn_ids"] == []
    assert summary_state["compaction_count"] == 1
    assert summary_state["last_compaction_trigger"]["reasons"] == ["source_count"]


def test_node_summary_maintenance_compacts_at_token_limit_before_k(tmp_path):
    maintenance_llm = MaintenanceLLM([{"summary": "Alice interview preference."}])
    memory = Memory.from_config(
        {
            "storage": {"path": str(tmp_path / "memory.sqlite3")},
            "maintenance": {
                "node_summary": {
                    "compact_after_k_sources": 10,
                    "max_tokens": 5,
                }
            },
        },
        extractor=SequenceHomogeneousExtractor(
            [
                _single_entity_payload("Alice prefers calm morning interview scheduling with detailed preparation notes."),
                _single_entity_payload("Alice also wants short commute windows around interviews."),
            ]
        ),
        maintenance_llm=maintenance_llm,
    )
    namespace = "node_summary_compact_token_ns"
    memory.reset(namespace)

    memory.add_memory("I prefer calm morning interview scheduling with notes.", namespace=namespace)
    node = memory.store.list_nodes(namespace)[0]
    memory.close()

    assert node.summary == "Alice interview preference."
    assert maintenance_llm.call_count == 1
    assert node.metadata["maintenance"]["node_summary"]["last_compaction_trigger"]["reasons"] == ["token_limit"]


def test_node_summary_compaction_requires_maintenance_llm(tmp_path):
    memory = Memory.from_config(
        {
            "storage": {"path": str(tmp_path / "memory.sqlite3")},
            "maintenance": {
                "node_summary": {
                    "compact_after_k_sources": 1,
                    "max_tokens": 1000,
                }
            },
        },
        extractor=SequenceHomogeneousExtractor([_single_entity_payload("Alice is the user.")]),
    )
    namespace = "node_summary_requires_llm_ns"
    memory.reset(namespace)

    with pytest.raises(RuntimeError, match="requires an LLM"):
        memory.add_memory("I am Alice.", namespace=namespace)
    memory.close()


def test_local_triple_maintenance_keep_new_retires_existing_triple_and_reindexes(tmp_path):
    maintenance_llm = MaintenanceLLM(
        [
            {
                "decision": "keep_new",
                "affected_existing_refs": ["existing:0"],
                "merged_triple": None,
                "rationale": "The new location replaces the old location.",
            }
        ]
    )
    embedding_client = RecordingEmbeddingClient()
    vector_store = RecordingVectorStore()
    memory = Memory.from_config(
        {"storage": {"path": str(tmp_path / "memory.sqlite3")}},
        extractor=SequenceHomogeneousExtractor(
            [
                _single_entity_payload(
                    "Alice lives in California.",
                    triples=[{"subject": "Alice", "predicate": "lives_in", "object": "California"}],
                ),
                _single_entity_payload(
                    "Alice lives in San Francisco.",
                    triples=[{"subject": "Alice", "predicate": "lives_in", "object": "San Francisco"}],
                ),
            ]
        ),
        maintenance_llm=maintenance_llm,
        embedding_client=embedding_client,
        vector_store=vector_store,
    )
    namespace = "local_triple_keep_new_ns"
    memory.reset(namespace)

    memory.add_memory("I live in California.", namespace=namespace)
    memory.add_memory("I live in San Francisco.", namespace=namespace)
    node = memory.store.list_nodes(namespace)[0]
    memory.close()

    assert maintenance_llm.call_count == 1
    assert "existing:0" in maintenance_llm.prompts[0]
    triples = node.local_graph.triples
    assert [(triple.object, triple.status) for triple in triples] == [
        ("California", "retired"),
        ("San Francisco", "active"),
    ]
    assert triples[0].superseded_by == triples[1].triple_id
    assert triples[0].qualifiers["source_turn_ids"] == ["turn:0"]
    assert triples[1].qualifiers["source_turn_ids"] == ["turn:1"]
    assert triples[1].qualifiers["maintenance_replaced_triple_ids"] == [triples[0].triple_id]
    assert triples[1].qualifiers["maintenance_replaced_source_turn_ids"] == ["turn:0"]
    graph_records = [
        record
        for record in vector_store.records
        if record.payload["item_type"] == "node_local_graph" and record.payload["node_id"] == node.node_id
    ]
    assert graph_records[-1].text.endswith("- Alice lives_in San Francisco")
    assert "California" not in graph_records[-1].text


def test_local_triple_maintenance_keep_existing_reindexes_active_graph_without_incoming(tmp_path):
    maintenance_llm = MaintenanceLLM(
        [
            {
                "decision": "keep_existing",
                "affected_existing_refs": ["existing:0"],
                "merged_triple": None,
                "rationale": "The existing triple already covers the incoming triple.",
            }
        ]
    )
    embedding_client = RecordingEmbeddingClient()
    vector_store = RecordingVectorStore()
    memory = Memory.from_config(
        {"storage": {"path": str(tmp_path / "memory.sqlite3")}},
        extractor=SequenceHomogeneousExtractor(
            [
                _single_entity_payload(
                    "Alice likes tea.",
                    triples=[{"subject": "Alice", "predicate": "likes", "object": "tea"}],
                ),
                _single_entity_payload(
                    "Alice still likes tea.",
                    triples=[{"subject": "Alice", "predicate": "likes", "object": "tea ceremony"}],
                ),
            ]
        ),
        maintenance_llm=maintenance_llm,
        embedding_client=embedding_client,
        vector_store=vector_store,
    )
    namespace = "local_triple_keep_existing_ns"
    memory.reset(namespace)

    memory.add_memory("I like tea.", namespace=namespace)
    memory.add_memory("I still like tea.", namespace=namespace)
    node = memory.store.list_nodes(namespace)[0]
    memory.close()

    assert maintenance_llm.call_count == 1
    assert [(triple.object, triple.status) for triple in node.local_graph.triples] == [("tea", "active")]
    triple = node.local_graph.triples[0]
    assert triple.qualifiers["source_turn_ids"] == ["turn:0", "turn:1"]
    assert len(triple.qualifiers["source_triple_ids"]) == 2
    assert len(triple.qualifiers["maintenance_discarded_triple_ids"]) == 1
    assert triple.qualifiers["maintenance_discarded_triple_ids"][0] != triple.triple_id
    assert triple.qualifiers["maintenance_discarded_source_turn_ids"] == ["turn:1"]
    graph_records = [
        record
        for record in vector_store.records
        if record.payload["item_type"] == "node_local_graph" and record.payload["node_id"] == node.node_id
    ]
    assert graph_records[-1].text.endswith("- Alice likes tea")
    assert len(graph_records) == 2


def test_local_triple_maintenance_duplicate_spo_merges_turn_provenance_without_llm(tmp_path):
    maintenance_llm = MaintenanceLLM([])
    memory = Memory.from_config(
        {"storage": {"path": str(tmp_path / "memory.sqlite3")}},
        extractor=SequenceHomogeneousExtractor(
            [
                _single_entity_payload(
                    "Alice likes tea.",
                    triples=[{"subject": "Alice", "predicate": "likes", "object": "tea"}],
                ),
                _single_entity_payload(
                    "Alice still likes tea.",
                    triples=[{"subject": "Alice", "predicate": "likes", "object": "tea"}],
                ),
            ]
        ),
        maintenance_llm=maintenance_llm,
    )
    namespace = "local_triple_duplicate_spo_ns"
    memory.reset(namespace)

    memory.add_memory("I like tea.", namespace=namespace)
    memory.add_memory("I still like tea.", namespace=namespace)
    node = memory.store.list_nodes(namespace)[0]
    memory.close()

    assert maintenance_llm.call_count == 0
    assert len(node.local_graph.triples) == 1
    triple = node.local_graph.triples[0]
    assert triple.qualifiers["source_turn_ids"] == ["turn:0", "turn:1"]
    assert len(triple.qualifiers["source_triple_ids"]) == 2
    assert triple.qualifiers["maintenance_last_action"] == "duplicate_spo"


def test_local_triple_maintenance_keep_both_preserves_compatible_values(tmp_path):
    maintenance_llm = MaintenanceLLM(
        [
            {
                "decision": "keep_both",
                "affected_existing_refs": [],
                "merged_triple": None,
                "rationale": "Both preferences can coexist.",
            }
        ]
    )
    memory = Memory.from_config(
        {"storage": {"path": str(tmp_path / "memory.sqlite3")}},
        extractor=SequenceHomogeneousExtractor(
            [
                _single_entity_payload(
                    "Alice likes tea.",
                    triples=[{"subject": "Alice", "predicate": "likes", "object": "tea"}],
                ),
                _single_entity_payload(
                    "Alice likes coffee.",
                    triples=[{"subject": "Alice", "predicate": "likes", "object": "coffee"}],
                ),
            ]
        ),
        maintenance_llm=maintenance_llm,
    )
    namespace = "local_triple_keep_both_ns"
    memory.reset(namespace)

    memory.add_memory("I like tea.", namespace=namespace)
    memory.add_memory("I like coffee.", namespace=namespace)
    node = memory.store.list_nodes(namespace)[0]
    memory.close()

    assert [(triple.object, triple.status) for triple in node.local_graph.triples] == [
        ("tea", "active"),
        ("coffee", "active"),
    ]
    assert node.local_graph.triples[1].qualifiers["maintenance_related_triple_ids"] == [
        node.local_graph.triples[0].triple_id
    ]
    assert node.local_graph.triples[1].qualifiers["maintenance_related_source_turn_ids"] == ["turn:0"]


def test_local_triple_maintenance_merge_replaces_candidates_with_merged_triple(tmp_path):
    maintenance_llm = MaintenanceLLM(
        [
            {
                "decision": "merge",
                "affected_existing_refs": ["existing:0"],
                "merged_triple": {
                    "subject": "Alice",
                    "predicate": "works_at",
                    "object": "OpenAI in San Francisco",
                    "qualifiers": {"specificity": "city"},
                },
                "rationale": "The new triple is a more specific version.",
            }
        ]
    )
    memory = Memory.from_config(
        {"storage": {"path": str(tmp_path / "memory.sqlite3")}},
        extractor=SequenceHomogeneousExtractor(
            [
                _single_entity_payload(
                    "Alice works at OpenAI.",
                    triples=[{"subject": "Alice", "predicate": "works_at", "object": "OpenAI"}],
                ),
                _single_entity_payload(
                    "Alice works at OpenAI in San Francisco.",
                    triples=[{"subject": "Alice", "predicate": "works_at", "object": "OpenAI in San Francisco"}],
                ),
            ]
        ),
        maintenance_llm=maintenance_llm,
    )
    namespace = "local_triple_merge_ns"
    memory.reset(namespace)

    memory.add_memory("I work at OpenAI.", namespace=namespace)
    memory.add_memory("I work at OpenAI in San Francisco.", namespace=namespace)
    node = memory.store.list_nodes(namespace)[0]
    memory.close()

    assert [(triple.object, triple.status) for triple in node.local_graph.triples] == [
        ("OpenAI", "retired"),
        ("OpenAI in San Francisco", "active"),
    ]
    assert node.local_graph.triples[1].qualifiers["specificity"] == "city"
    assert node.local_graph.triples[0].superseded_by == node.local_graph.triples[1].triple_id
    merged_from_ids = node.local_graph.triples[1].qualifiers["maintenance_merged_triple_ids"]
    assert merged_from_ids[0] == node.local_graph.triples[0].triple_id
    assert merged_from_ids[1].startswith("triple:")
    assert merged_from_ids[1] != node.local_graph.triples[1].triple_id
    assert len(node.local_graph.triples[1].qualifiers["source_triple_ids"]) == 2
    assert node.local_graph.triples[1].qualifiers["maintenance_merged_source_turn_ids"] == ["turn:0", "turn:1"]


def test_local_triple_maintenance_requires_llm_for_same_subject_predicate(tmp_path):
    memory = Memory.from_config(
        {"storage": {"path": str(tmp_path / "memory.sqlite3")}},
        extractor=SequenceHomogeneousExtractor(
            [
                _single_entity_payload(
                    "Alice lives in California.",
                    triples=[{"subject": "Alice", "predicate": "lives_in", "object": "California"}],
                ),
                _single_entity_payload(
                    "Alice lives in San Francisco.",
                    triples=[{"subject": "Alice", "predicate": "lives_in", "object": "San Francisco"}],
                ),
            ]
        ),
    )
    namespace = "local_triple_requires_llm_ns"
    memory.reset(namespace)

    memory.add_memory("I live in California.", namespace=namespace)
    with pytest.raises(RuntimeError, match="requires an LLM"):
        memory.add_memory("I live in San Francisco.", namespace=namespace)
    memory.close()


def test_local_triple_maintenance_does_not_call_llm_for_different_predicate(tmp_path):
    maintenance_llm = MaintenanceLLM([])
    memory = Memory.from_config(
        {"storage": {"path": str(tmp_path / "memory.sqlite3")}},
        extractor=SequenceHomogeneousExtractor(
            [
                _single_entity_payload(
                    "Alice likes tea.",
                    triples=[{"subject": "Alice", "predicate": "likes", "object": "tea"}],
                ),
                _single_entity_payload(
                    "Alice lives in California.",
                    triples=[{"subject": "Alice", "predicate": "lives_in", "object": "California"}],
                ),
            ]
        ),
        maintenance_llm=maintenance_llm,
    )
    namespace = "local_triple_no_overlap_ns"
    memory.reset(namespace)

    memory.add_memory("I like tea.", namespace=namespace)
    memory.add_memory("I live in California.", namespace=namespace)
    node = memory.store.list_nodes(namespace)[0]
    memory.close()

    assert maintenance_llm.call_count == 0
    assert [(triple.predicate, triple.object, triple.status) for triple in node.local_graph.triples] == [
        ("likes", "tea", "active"),
        ("lives_in", "California", "active"),
    ]


def test_vector_indexing_uses_node_local_graph_and_hyper_edge_description(tmp_path):
    embedding_client = RecordingEmbeddingClient()
    vector_store = RecordingVectorStore()
    memory = Memory.from_config(
        {"storage": {"path": str(tmp_path / "memory.sqlite3")}},
        extractor=StaticHomogeneousExtractor(),
        embedding_client=embedding_client,
        vector_store=vector_store,
    )
    namespace = "homogeneous_vector_ns"
    memory.reset(namespace)

    memory.add_memory(
        user_input="Alice prefers morning interviews.",
        assistant_output="I will remember that.",
        namespace=namespace,
    )
    nodes = memory.store.list_nodes(namespace)
    edges = memory.store.list_edges(namespace)
    memory.close()

    item_types = [record.payload["item_type"] for record in vector_store.records]
    assert item_types.count("node_local_graph") == 3
    assert item_types.count("hyper_edge_description") == 2

    preference = next(node for node in nodes if "preference" in node.node_labels)
    preference_graph_text = node_local_graph_embedding_text(preference)
    assert preference_graph_text == (
        "Alice prefers morning interviews.\n"
        "- Alice prefers morning interviews"
    )
    assert "Core content:" not in preference_graph_text
    assert "Local graph:" not in preference_graph_text

    graph_record = next(
        record
        for record in vector_store.records
        if record.payload["item_type"] == "node_local_graph"
        and record.payload["node_id"] == preference.node_id
    )
    assert graph_record.id == make_vector_point_id(namespace, "node_local_graph", preference.node_id)
    assert graph_record.text == preference_graph_text

    edge_descriptions = {edge.description for edge in edges}
    edge_records = [
        record for record in vector_store.records if record.payload["item_type"] == "hyper_edge_description"
    ]
    assert {record.text for record in edge_records} == edge_descriptions
    assert {record.payload["edge_id"] for record in edge_records} == {edge.edge_id for edge in edges}


class StaticHomogeneousExtractor:
    def extract(self, window, context):
        return MemoryExtraction.model_validate(
            {
                "edge_summaries": [
                    {
                        "ref": "e1",
                        "description": "Alice stated her morning interview preference in this interaction.",
                    },
                    {
                        "ref": "e2",
                        "description": "Alice's interview scheduling preference.",
                    },
                ],
                "nodes": [
                    {
                        "ref": "n1",
                        "labels": ["entity", "person"],
                        "canonical_text": "Alice",
                        "summaries": ["Alice is the user."],
                        "triples": [{"subject": "Alice", "predicate": "is_a", "object": "user"}],
                        "edge_summary_refs": ["e2"],
                        "metadata": {"aliases": ["Alice"]},
                    },
                    {
                        "ref": "n2",
                        "labels": ["preference"],
                        "canonical_text": "Alice prefers morning interviews.",
                        "summaries": ["Alice has a scheduling preference for morning interviews."],
                        "triples": [{"subject": "Alice", "predicate": "prefers", "object": "morning interviews"}],
                        "edge_summary_refs": ["e1", "e2"],
                    },
                    {
                        "ref": "n3",
                        "labels": ["event"],
                        "canonical_text": "Alice discussed interview scheduling.",
                        "summaries": ["Alice stated an interview scheduling preference."],
                        "triples": [{"subject": "Alice", "predicate": "discussed", "object": "interview scheduling"}],
                        "edge_summary_refs": ["e1"],
                    },
                ],
            }
        )


class SequenceHomogeneousExtractor:
    def __init__(self, payloads):
        self.payloads = list(payloads)
        self.index = 0

    def extract(self, window, context):
        payload = self.payloads[self.index]
        self.index += 1
        return MemoryExtraction.model_validate(payload)


def _single_entity_payload(summary, *, triples=None):
    return {
        "edge_summaries": [{"ref": "e1", "description": "Alice profile."}],
        "nodes": [
            {
                "ref": "n1",
                "labels": ["entity", "person"],
                "canonical_text": "Alice",
                "summaries": [summary],
                "triples": triples or [],
                "edge_summary_refs": ["e1"],
                "metadata": {"aliases": ["Alice"]},
            }
        ],
    }


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
        self.inputs.append(list(texts))
        return [[float(index + 1)] for index, _ in enumerate(texts)]


class RecordingVectorStore:
    def __init__(self):
        self.records = []
        self.deleted_namespaces = []
        self.deleted_ids = []
        self.closed = False

    def upsert(self, records):
        self.records.extend(records)

    def search(self, *, query, vector, top_k, filters=None):
        return []

    def delete(self, ids):
        self.deleted_ids.extend(ids)

    def delete_namespace(self, namespace):
        self.deleted_namespaces.append(namespace)

    def close(self):
        self.closed = True
