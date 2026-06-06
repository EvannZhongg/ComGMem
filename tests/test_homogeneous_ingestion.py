from __future__ import annotations

import pytest
import yaml

from c_hypermem import Memory
from c_hypermem.config import MemoryConfig
from c_hypermem.pipeline.context import AssemblyContext
from c_hypermem.pipeline.local_graph_builder import LocalGraphBuilder
from c_hypermem.pipeline.node_builder import NodeBuilder
from c_hypermem.stores.vector_store import VectorSearchHit, make_vector_point_id, node_local_graph_embedding_text
from c_hypermem.schema import ExtractedNode, LocalNodeGraph, LocalTriple, MemoryExtraction, MemoryNode


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


def test_default_config_uses_global_token_counting_config():
    config = MemoryConfig.load("configs/default.yaml")
    default_raw = yaml.safe_load(open("configs/default.yaml", encoding="utf-8")) or {}
    models_raw = yaml.safe_load(open("configs/models.yaml", encoding="utf-8")) or {}

    assert "default_top_k" not in default_raw
    assert config.token_counting.tokenizer_encoding == "cl100k_base"
    assert models_raw["token_counting"]["tokenizer_encoding"] == "cl100k_base"
    assert config.nlp.model_path == "models/en_core_web_sm"
    assert models_raw["nlp"]["model_path"] == "models/en_core_web_sm"
    assert config.retrieval.node_rrf_k == 60
    assert config.retrieval.hyper_edge_description_vector_top_k == 10
    assert config.recall.cluster_periphery_edge_limit == 20
    assert config.recall.cluster_periphery_node_limit == 50
    assert config.recall.node_triple_limit == 20
    assert config.recall.include_turn_ids_in_context
    assert config.recall.include_real_time_in_context
    assert default_raw["ingestion"]["pass_recent_context"] is True
    assert config.ingestion.pass_recent_context
    assert config.edge_clusters.stop_nodes == ["User", "Assistant"]
    assert "description_variants_limit" not in default_raw["edge_clusters"]
    assert not hasattr(config.edge_clusters, "description_variants_limit")
    assert "unconfigured_label_policy" not in (default_raw.get("node_labels") or {})
    assert not hasattr(config.node_labels, "unconfigured_label_policy")
    assert "tokenizer_encoding" not in default_raw["maintenance"]["node_summary"]
    assert "tokenizer_encoding" not in default_raw["maintenance"]["hyper_edge_description"]
    assert "nlp" not in default_raw
    assert "edge_cluster" not in default_raw["maintenance"]
    assert not hasattr(config.maintenance, "edge_cluster")


def test_ingestion_can_disable_recent_context(tmp_path):
    extractor = RecordingWindowExtractor()
    memory = Memory.from_config(
        {
            "storage": {"path": str(tmp_path / "memory.sqlite3")},
            "ingestion": {"pass_recent_context": False, "context_window_messages": 3},
        },
        extractor=extractor,
    )
    namespace = "disabled_context_ns"
    memory.reset(namespace)

    memory.add_memory("First turn.", namespace=namespace)
    memory.add_memory("Second turn.", namespace=namespace)
    memory.close()

    assert [[message.content for message in window.target] for window in extractor.windows] == [
        ["First turn."],
        ["Second turn."],
    ]
    assert [[message.content for message in window.context] for window in extractor.windows] == [[], []]


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
    assert all("edge_summary_refs" in edge.metadata for edge in edges)
    assert all(edge.node_ids for edge in edges)
    assert stats["entity_aliases"] >= 1
    assert any(cluster.cluster_labels == ["shared_node"] for cluster in clusters)
    assert results
    assert "Alice -prefers- morning interviews" in results[0]["content"]
    assert "edge_type" not in results[0]["metadata"]
    assert "edge_relation" not in results[0]["metadata"]
    assert "edge_roles" not in results[0]["metadata"]
    assert "cluster_description_variants" not in results[0]["metadata"]
    assert "cluster_edge_descriptions" in results[0]["metadata"]
    assert all(node.time.world.event_time for node in nodes)
    assert all(node.time.world.source_timestamp for node in nodes)


def test_search_context_includes_triples_sibling_edges_and_relative_turns(tmp_path):
    memory = Memory.from_config(
        {
            "storage": {"path": str(tmp_path / "memory.sqlite3")},
            "retrieval": {"edge_core_top_k": 1, "final_top_k": 1},
        },
        extractor=StaticHomogeneousExtractor(),
    )
    namespace = "search_context_shape_ns"
    memory.reset(namespace)

    memory.add_memory("Alice prefers morning interviews.", namespace=namespace)
    results = memory.search("morning interviews", namespace=namespace, top_k=1)
    memory.close()

    assert len(results) == 1
    result = results[0]
    assert result["content"].startswith("memory1\uff1a")
    assert "memory2\uff1a" in result["content"]
    assert "Core edge:" not in result["content"]
    assert "Sibling edges:" not in result["content"]
    assert "Triples:" not in result["content"]
    assert "Alice -prefers- morning interviews" in result["content"]
    assert result["content"].count("Alice -prefers- morning interviews") == 1
    assert "current_turn_id=turn:1" in result["content"]
    assert "source_turn id=turn:0" in result["content"]
    assert "turn_distance=" not in result["content"]
    assert "current_turn=" not in result["content"]

    metadata = result["metadata"]
    assert metadata["relative_time"]["turn_distance"] == 1
    assert metadata["edge_nodes"]
    assert all(node["relative_time"]["turn_distance"] == 1 for node in metadata["edge_nodes"])
    assert any(
        triple["relative_time"]["turn_distance"] == 1
        for node in metadata["edge_nodes"]
        for triple in node["triples"]
    )
    assert metadata["periphery_edges"]
    assert metadata["periphery_edges"][0]["relative_time"]["turn_distance"] == 1
    assert metadata["periphery_edges"][0]["nodes"]
    assert any(node["triples"] for node in metadata["periphery_edges"][0]["nodes"])


def test_edge_cluster_groups_edges_with_shared_member_node(tmp_path):
    memory = Memory.from_config(
        {"storage": {"path": str(tmp_path / "memory.sqlite3")}},
        extractor=StaticHomogeneousExtractor(),
    )
    namespace = "shared_node_cluster_ns"
    memory.reset(namespace)

    memory.add_memory("Alice prefers morning interviews.", namespace=namespace)
    edges = memory.store.list_edges(namespace)
    clusters = memory.store.list_edge_clusters(namespace)
    members = memory.store.list_edge_cluster_members(namespace)
    memory.close()

    assert len(edges) == 2
    shared_clusters = [cluster for cluster in clusters if cluster.cluster_labels == ["shared_node"]]
    assert len(shared_clusters) == 1
    cluster = shared_clusters[0]
    shared_node_ids = cluster.metadata["shared_node_ids"]
    assert len(shared_node_ids) == 1
    shared_node_id = shared_node_ids[0]
    assert all(shared_node_id in edge.node_ids for edge in edges)
    assert cluster.cluster_labels == ["shared_node"]
    assert cluster.conflict_state == "none"
    assert cluster.canonical_description == f"HyperEdges sharing node: {shared_node_id}"
    assert cluster.metadata["cluster_basis"] == "shared_node"
    assert cluster.metadata["cluster_reasons"] == ["shared_node"]
    assert {occurrence["node_id"] for occurrence in cluster.metadata["anchor_occurrences"]} == {shared_node_id}
    shared_members = [member for member in members if member.cluster_id == cluster.cluster_id]
    assert {member.edge_id for member in shared_members} == {edge.edge_id for edge in edges}


def test_shared_node_triples_keep_original_scope_when_new_edge_reuses_node(tmp_path):
    memory = Memory.from_config(
        {"storage": {"path": str(tmp_path / "memory.sqlite3")}},
        extractor=SequenceHomogeneousExtractor(
            [
                {
                    "edge_summaries": [{"ref": "e1", "description": "Andrew's pet context."}],
                    "nodes": [
                        {
                            "ref": "andrew",
                            "labels": ["entity", "person"],
                            "canonical_text": "Andrew",
                            "summaries": ["Andrew is a person."],
                            "triples": [{"subject": "Andrew", "predicate": "has_pet", "object": "Toby"}],
                            "edge_summary_refs": ["e1"],
                            "metadata": {"aliases": ["Andrew"]},
                        },
                        {
                            "ref": "toby",
                            "labels": ["entity", "pet"],
                            "canonical_text": "Toby",
                            "summaries": ["Toby is Andrew's pet."],
                            "triples": [{"subject": "Toby", "predicate": "is_a", "object": "pet"}],
                            "edge_summary_refs": ["e1"],
                        },
                    ],
                },
                {
                    "edge_summaries": [{"ref": "e2", "description": "Andrew's tool context."}],
                    "nodes": [
                        {
                            "ref": "andrew",
                            "labels": ["entity", "person"],
                            "canonical_text": "Andrew",
                            "summaries": ["Andrew uses Obsidian."],
                            "triples": [{"subject": "Andrew", "predicate": "uses", "object": "Obsidian"}],
                            "edge_summary_refs": ["e2"],
                            "metadata": {"aliases": ["Andrew"]},
                        },
                        {
                            "ref": "obsidian",
                            "labels": ["entity", "tool"],
                            "canonical_text": "Obsidian",
                            "summaries": ["Obsidian is Andrew's tool."],
                            "triples": [{"subject": "Obsidian", "predicate": "is_a", "object": "tool"}],
                            "edge_summary_refs": ["e2"],
                        },
                    ],
                },
            ]
        ),
    )
    namespace = "shared_node_scope_ns"
    memory.reset(namespace)

    memory.add_memory("Andrew has a pet named Toby.", namespace=namespace)
    first_edge = memory.store.list_edges(namespace)[0]
    memory.add_memory("Andrew uses Obsidian.", namespace=namespace)
    andrew = next(node for node in memory.store.list_nodes(namespace) if node.canonical_text == "Andrew")
    edges_by_description = {edge.description: edge for edge in memory.store.list_edges(namespace)}
    memory.close()

    pet_scope = next(triple.scope_edge_ids for triple in andrew.local_graph.triples if triple.predicate == "has_pet")
    tool_scope = next(triple.scope_edge_ids for triple in andrew.local_graph.triples if triple.predicate == "uses")
    tool_edge = edges_by_description["Andrew's tool context."]

    assert pet_scope == [first_edge.edge_id]
    assert tool_scope == [tool_edge.edge_id]
    assert first_edge.edge_id != tool_edge.edge_id


def test_edge_cluster_groups_edges_with_semantic_anchor_from_local_triples(tmp_path):
    memory = Memory.from_config(
        {"storage": {"path": str(tmp_path / "memory.sqlite3")}},
        extractor=SequenceHomogeneousExtractor(
            [
                {
                    "edge_summaries": [{"ref": "e1", "description": "Alice owns Project Atlas."}],
                    "nodes": [
                        {
                            "ref": "n1",
                            "labels": ["fact"],
                            "canonical_text": "Alice owns Project Atlas.",
                            "summaries": ["Alice owns Project Atlas."],
                            "triples": [{"subject": "Alice", "predicate": "owns", "object": "Project Atlas"}],
                            "edge_summary_refs": ["e1"],
                        }
                    ],
                },
                {
                    "edge_summaries": [{"ref": "e2", "description": "Project Atlas status is green."}],
                    "nodes": [
                        {
                            "ref": "n2",
                            "labels": ["state"],
                            "canonical_text": "Project Atlas status is green.",
                            "summaries": ["Project Atlas status is green."],
                            "triples": [{"subject": "Project Atlas", "predicate": "status", "object": "green"}],
                            "edge_summary_refs": ["e2"],
                        }
                    ],
                },
            ]
        ),
    )
    namespace = "semantic_anchor_cluster_ns"
    memory.reset(namespace)

    memory.add_memory("Alice owns Project Atlas.", namespace=namespace)
    memory.add_memory("Project Atlas status is green.", namespace=namespace)
    edges = memory.store.list_edges(namespace)
    clusters = memory.store.list_edge_clusters(namespace)
    semantic_clusters = [cluster for cluster in clusters if cluster.cluster_labels == ["semantic_anchor"]]
    semantic_members = memory.store.list_edge_cluster_members(
        namespace,
        [cluster.cluster_id for cluster in semantic_clusters],
    )
    memory.close()

    assert len(edges) == 2
    assert len(semantic_clusters) == 1
    cluster = semantic_clusters[0]
    assert cluster.canonical_description == "HyperEdges sharing semantic anchor: project atlas"
    assert cluster.metadata["cluster_basis"] == "semantic_anchor"
    assert cluster.metadata["anchor_value"] == "project atlas"
    assert cluster.metadata["cluster_reasons"] == ["object_subject"]
    assert {occurrence["position"] for occurrence in cluster.metadata["anchor_occurrences"]} == {"object", "subject"}
    assert {member.edge_id for member in semantic_members} == {edge.edge_id for edge in edges}


def test_edge_cluster_does_not_group_semantic_anchor_from_object_object_only(tmp_path):
    memory = Memory.from_config(
        {"storage": {"path": str(tmp_path / "memory.sqlite3")}},
        extractor=SequenceHomogeneousExtractor(
            [
                {
                    "edge_summaries": [{"ref": "e1", "description": "Alice owns Project Atlas."}],
                    "nodes": [
                        {
                            "ref": "n1",
                            "labels": ["fact"],
                            "canonical_text": "Alice owns Project Atlas.",
                            "summaries": ["Alice owns Project Atlas."],
                            "triples": [{"subject": "Alice", "predicate": "owns", "object": "Project Atlas"}],
                            "edge_summary_refs": ["e1"],
                        }
                    ],
                },
                {
                    "edge_summaries": [{"ref": "e2", "description": "Bob manages Project Atlas."}],
                    "nodes": [
                        {
                            "ref": "n2",
                            "labels": ["fact"],
                            "canonical_text": "Bob manages Project Atlas.",
                            "summaries": ["Bob manages Project Atlas."],
                            "triples": [{"subject": "Bob", "predicate": "manages", "object": "Project Atlas"}],
                            "edge_summary_refs": ["e2"],
                        }
                    ],
                },
            ]
        ),
    )
    namespace = "object_object_only_no_semantic_cluster_ns"
    memory.reset(namespace)

    memory.add_memory("Alice owns Project Atlas.", namespace=namespace)
    memory.add_memory("Bob manages Project Atlas.", namespace=namespace)
    clusters = memory.store.list_edge_clusters(namespace)
    memory.close()

    semantic_clusters = [cluster for cluster in clusters if cluster.cluster_labels == ["semantic_anchor"]]
    assert semantic_clusters == []


def test_edge_cluster_groups_subject_subject_after_one_non_stop_subject_match(tmp_path):
    memory = Memory.from_config(
        {"storage": {"path": str(tmp_path / "memory.sqlite3")}},
        extractor=SequenceHomogeneousExtractor(
            [
                {
                    "edge_summaries": [{"ref": "e1", "description": "Alice's work profile."}],
                    "nodes": [
                        {
                            "ref": "n1",
                            "labels": ["fact"],
                            "canonical_text": "Alice's work profile.",
                            "summaries": ["Alice has work profile details."],
                            "triples": [
                                {"subject": "Alice", "predicate": "has_role", "object": "analyst"},
                                {"subject": "Alice", "predicate": "uses_tool", "object": "Todoist"},
                            ],
                            "edge_summary_refs": ["e1"],
                        }
                    ],
                },
                {
                    "edge_summaries": [{"ref": "e2", "description": "Alice's planning profile."}],
                    "nodes": [
                        {
                            "ref": "n2",
                            "labels": ["fact"],
                            "canonical_text": "Alice's planning profile.",
                            "summaries": ["Alice has planning profile details."],
                            "triples": [
                                {"subject": "Alice", "predicate": "has_degree", "object": "Business Administration"},
                                {"subject": "Alice", "predicate": "tracks_expenses_with", "object": "Mint"},
                            ],
                            "edge_summary_refs": ["e2"],
                        }
                    ],
                },
            ]
        ),
    )
    namespace = "subject_subject_single_non_stop_subject_cluster_ns"
    memory.reset(namespace)

    memory.add_memory("Alice has a work profile.", namespace=namespace)
    memory.add_memory("Alice has a planning profile.", namespace=namespace)
    edges = memory.store.list_edges(namespace)
    clusters = memory.store.list_edge_clusters(namespace)
    semantic_clusters = [cluster for cluster in clusters if cluster.cluster_labels == ["semantic_anchor"]]
    memory.close()

    assert len(edges) == 2
    assert len(semantic_clusters) == 1
    cluster = semantic_clusters[0]
    assert cluster.metadata["anchor_value"] == "alice"
    assert cluster.metadata["cluster_reasons"] == ["subject_subject"]


def test_edge_cluster_stop_node_subject_subject_does_not_trigger_cluster(tmp_path):
    memory = Memory.from_config(
        {"storage": {"path": str(tmp_path / "memory.sqlite3")}},
        extractor=SequenceHomogeneousExtractor(
            [
                {
                    "edge_summaries": [{"ref": "e1", "description": "User's work profile."}],
                    "nodes": [
                        {
                            "ref": "n1",
                            "labels": ["fact"],
                            "canonical_text": "User's work profile.",
                            "summaries": ["User has work profile details."],
                            "triples": [
                                {"subject": "User", "predicate": "has_role", "object": "analyst"},
                                {"subject": "User", "predicate": "uses_tool", "object": "Todoist"},
                            ],
                            "edge_summary_refs": ["e1"],
                        }
                    ],
                },
                {
                    "edge_summaries": [{"ref": "e2", "description": "User's planning profile."}],
                    "nodes": [
                        {
                            "ref": "n2",
                            "labels": ["fact"],
                            "canonical_text": "User's planning profile.",
                            "summaries": ["User has planning profile details."],
                            "triples": [
                                {"subject": "User", "predicate": "has_degree", "object": "Business Administration"},
                                {"subject": "User", "predicate": "tracks_expenses_with", "object": "Mint"},
                            ],
                            "edge_summary_refs": ["e2"],
                        }
                    ],
                },
            ]
        ),
    )
    namespace = "subject_subject_stop_node_subject_no_cluster_ns"
    memory.reset(namespace)

    memory.add_memory("User has a work profile.", namespace=namespace)
    memory.add_memory("User has a planning profile.", namespace=namespace)
    clusters = memory.store.list_edge_clusters(namespace)
    memory.close()

    semantic_clusters = [cluster for cluster in clusters if cluster.cluster_labels == ["semantic_anchor"]]
    assert semantic_clusters == []


def test_edge_cluster_stop_node_object_subject_still_triggers_cluster(tmp_path):
    memory = Memory.from_config(
        {"storage": {"path": str(tmp_path / "memory.sqlite3")}},
        extractor=SequenceHomogeneousExtractor(
            [
                {
                    "edge_summaries": [{"ref": "e1", "description": "Workspace assigned Assistant to User."}],
                    "nodes": [
                        {
                            "ref": "n1",
                            "labels": ["fact"],
                            "canonical_text": "Workspace assigned Assistant to User.",
                            "summaries": ["Workspace assigned Assistant to User."],
                            "triples": [{"subject": "Workspace", "predicate": "assigned_to", "object": "User"}],
                            "edge_summary_refs": ["e1"],
                        }
                    ],
                },
                {
                    "edge_summaries": [{"ref": "e2", "description": "User owns Project Atlas."}],
                    "nodes": [
                        {
                            "ref": "n2",
                            "labels": ["fact"],
                            "canonical_text": "User owns Project Atlas.",
                            "summaries": ["User owns Project Atlas."],
                            "triples": [{"subject": "User", "predicate": "owns", "object": "Project Atlas"}],
                            "edge_summary_refs": ["e2"],
                        }
                    ],
                },
            ]
        ),
    )
    namespace = "stop_node_object_subject_cluster_ns"
    memory.reset(namespace)

    memory.add_memory("Workspace assigned Assistant to User.", namespace=namespace)
    memory.add_memory("User owns Project Atlas.", namespace=namespace)
    edges = memory.store.list_edges(namespace)
    clusters = memory.store.list_edge_clusters(namespace)
    semantic_clusters = [cluster for cluster in clusters if cluster.cluster_labels == ["semantic_anchor"]]
    semantic_members = memory.store.list_edge_cluster_members(
        namespace,
        [cluster.cluster_id for cluster in semantic_clusters],
    )
    memory.close()

    assert len(edges) == 2
    assert len(semantic_clusters) == 1
    cluster = semantic_clusters[0]
    assert cluster.metadata["anchor_value"] == "user"
    assert set(cluster.metadata["cluster_reasons"]) <= {"subject_object", "object_subject"}
    assert {occurrence["position"] for occurrence in cluster.metadata["anchor_occurrences"]} == {"object", "subject"}
    assert {member.edge_id for member in semantic_members} == {edge.edge_id for edge in edges}


def test_edge_cluster_groups_subject_subject_after_two_distinct_subjects_for_same_pair(tmp_path):
    memory = Memory.from_config(
        {"storage": {"path": str(tmp_path / "memory.sqlite3")}},
        extractor=SequenceHomogeneousExtractor(
            [
                {
                    "edge_summaries": [{"ref": "e1", "description": "Alice and Bob work profile."}],
                    "nodes": [
                        {
                            "ref": "n1",
                            "labels": ["fact"],
                            "canonical_text": "Alice and Bob work profile.",
                            "summaries": ["Alice and Bob have work profile details."],
                            "triples": [
                                {"subject": "Alice", "predicate": "has_role", "object": "analyst"},
                                {"subject": "Bob", "predicate": "has_role", "object": "designer"},
                            ],
                            "edge_summary_refs": ["e1"],
                        }
                    ],
                },
                {
                    "edge_summaries": [{"ref": "e2", "description": "Alice and Bob planning profile."}],
                    "nodes": [
                        {
                            "ref": "n2",
                            "labels": ["fact"],
                            "canonical_text": "Alice and Bob planning profile.",
                            "summaries": ["Alice and Bob have planning profile details."],
                            "triples": [
                                {"subject": "Alice", "predicate": "has_degree", "object": "Business Administration"},
                                {"subject": "Bob", "predicate": "uses_tool", "object": "Mint"},
                            ],
                            "edge_summary_refs": ["e2"],
                        }
                    ],
                },
            ]
        ),
    )
    namespace = "subject_subject_two_distinct_subjects_cluster_ns"
    memory.reset(namespace)

    memory.add_memory("Alice and Bob have a work profile.", namespace=namespace)
    memory.add_memory("Alice and Bob have a planning profile.", namespace=namespace)
    edges = memory.store.list_edges(namespace)
    clusters = memory.store.list_edge_clusters(namespace)
    semantic_clusters = [cluster for cluster in clusters if cluster.cluster_labels == ["semantic_anchor"]]
    semantic_members = memory.store.list_edge_cluster_members(
        namespace,
        [cluster.cluster_id for cluster in semantic_clusters],
    )
    memory.close()

    assert len(edges) == 2
    assert {cluster.metadata["anchor_value"] for cluster in semantic_clusters} == {"alice", "bob"}
    assert all(cluster.metadata["cluster_reasons"] == ["subject_subject"] for cluster in semantic_clusters)
    assert all(
        {occurrence["position"] for occurrence in cluster.metadata["anchor_occurrences"]} == {"subject"}
        for cluster in semantic_clusters
    )
    assert {member.edge_id for member in semantic_members} == {edge.edge_id for edge in edges}


def test_edge_cluster_metadata_merge_handles_dict_occurrence_lists(tmp_path):
    memory = Memory.from_config(
        {"storage": {"path": str(tmp_path / "memory.sqlite3")}},
        extractor=SequenceHomogeneousExtractor(
            [
                {
                    "edge_summaries": [
                        {"ref": "e1", "description": "Alice uses Todoist."},
                        {"ref": "e2", "description": "Alice uses Trello."},
                    ],
                    "nodes": [
                        {
                            "ref": "n1",
                            "labels": ["entity"],
                            "canonical_text": "Alice",
                            "summaries": ["Alice is the user."],
                            "triples": [{"subject": "Alice", "predicate": "uses", "object": "Todoist"}],
                            "edge_summary_refs": ["e1", "e2"],
                            "metadata": {"aliases": ["Alice"]},
                        },
                        {
                            "ref": "n2",
                            "labels": ["tool"],
                            "canonical_text": "Todoist",
                            "summaries": ["Todoist is a task app."],
                            "edge_summary_refs": ["e1"],
                        },
                        {
                            "ref": "n3",
                            "labels": ["tool"],
                            "canonical_text": "Trello",
                            "summaries": ["Trello is a task app."],
                            "edge_summary_refs": ["e2"],
                        },
                    ],
                },
                {
                    "edge_summaries": [
                        {"ref": "e3", "description": "Alice is adapting to a 9-to-5 schedule."},
                        {"ref": "e4", "description": "Alice wants to stay on top of work tasks."},
                    ],
                    "nodes": [
                        {
                            "ref": "n1",
                            "labels": ["entity"],
                            "canonical_text": "Alice",
                            "summaries": ["Alice is starting a new job."],
                            "triples": [{"subject": "Alice", "predicate": "has_schedule", "object": "9-to-5"}],
                            "edge_summary_refs": ["e3", "e4"],
                            "metadata": {"aliases": ["Alice"]},
                        },
                        {
                            "ref": "n2",
                            "labels": ["task"],
                            "canonical_text": "Alice wants to stay on top of work tasks.",
                            "summaries": ["Alice wants task organization advice."],
                            "edge_summary_refs": ["e4"],
                        },
                    ],
                },
            ]
        ),
    )
    namespace = "cluster_metadata_merge_dict_lists_ns"
    memory.reset(namespace)

    memory.add_memory("I will try Todoist and Trello.", namespace=namespace)
    memory.add_memory("I am adapting to my new 9-to-5 job.", namespace=namespace)
    clusters = memory.store.list_edge_clusters(namespace)
    memory.close()

    shared_clusters = [cluster for cluster in clusters if cluster.cluster_labels == ["shared_node"]]
    assert shared_clusters
    assert any(len(cluster.metadata["anchor_occurrences"]) >= 4 for cluster in shared_clusters)


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
    content_records = [
        record
        for record in vector_store.records
        if record.payload["item_type"] == "node_content" and record.payload["node_id"] == node.node_id
    ]
    assert content_records[-1].text == f"{node.content}\n{node.summary}"


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


def test_node_summary_maintenance_retries_invalid_compaction_payload(tmp_path):
    maintenance_llm = MaintenanceLLM([{"description": "wrong field"}, {"summary": "Alice compact summary."}])
    memory = Memory.from_config(
        {
            "storage": {"path": str(tmp_path / "memory.sqlite3")},
            "llm": {
                "provider": "openai_compatible",
                "model": "test-model",
                "retry_attempts": 2,
            },
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
    namespace = "node_summary_retry_invalid_payload_ns"
    memory.reset(namespace)

    memory.add_memory("I am Alice.", namespace=namespace)
    memory.add_memory("I am preparing for interviews.", namespace=namespace)
    node = memory.store.list_nodes(namespace)[0]
    memory.close()

    assert node.summary == "Alice compact summary."
    assert maintenance_llm.call_count == 2


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


def test_maintenance_token_counter_uses_global_token_counting_config(tmp_path):
    maintenance_llm = MaintenanceLLM([{"summary": "Alice interview preference."}])
    token_counter = RecordingTokenCounter(result=5)
    memory = Memory.from_config(
        {
            "storage": {"path": str(tmp_path / "memory.sqlite3")},
            "token_counting": {"tokenizer_encoding": "test-encoding"},
            "maintenance": {
                "node_summary": {
                    "compact_after_k_sources": 10,
                    "max_tokens": 5,
                }
            },
        },
        extractor=SequenceHomogeneousExtractor(
            [
                _single_entity_payload("Alice prefers calm morning interview scheduling."),
            ]
        ),
        maintenance_llm=maintenance_llm,
    )
    memory.ingestion.assembler.maintenance._token_counter = token_counter
    namespace = "global_token_counting_ns"
    memory.reset(namespace)

    memory.add_memory("I prefer calm morning interviews.", namespace=namespace)
    memory.close()

    assert memory.config.token_counting.tokenizer_encoding == "test-encoding"
    assert token_counter.inputs == [
        "Alice prefers calm morning interview scheduling.",
        "Alice profile.",
    ]
    assert maintenance_llm.call_count == 1


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
                "decisions": [
                    {
                        "incoming_ref": "incoming:0",
                        "decision": "keep_new",
                        "affected_existing_refs": ["existing:0"],
                        "merged_triple": None,
                        "rationale": "The new location replaces the old location.",
                    }
                ]
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
    distribution = node.metadata["maintenance"]["local_triples"]["triple_distribution"]
    assert distribution["total"] == 2
    assert distribution["active"] == 1
    assert distribution["retired"] == 1
    assert distribution["active_by_predicate"] == {"lives_in": 1}
    assert distribution["active_by_subject_predicate"] == {"alice|lives_in": 1}
    graph_records = [
        record
        for record in vector_store.records
        if record.payload["item_type"] == "node_local_graph" and record.payload["node_id"] == node.node_id
    ]
    assert graph_records[-1].text.endswith("- Alice lives_in San Francisco")
    assert "California" not in graph_records[-1].text


def test_local_triple_maintenance_retries_invalid_decision_refs(tmp_path):
    maintenance_llm = MaintenanceLLM(
        [
            {
                "decisions": [
                    {
                        "incoming_ref": "incoming:1",
                        "decision": "keep_new",
                        "affected_existing_refs": ["existing:0"],
                        "merged_triple": None,
                        "rationale": "Wrong incoming ref.",
                    }
                ]
            },
            {
                "decisions": [
                    {
                        "incoming_ref": "incoming:0",
                        "decision": "keep_new",
                        "affected_existing_refs": ["existing:0"],
                        "merged_triple": None,
                        "rationale": "The new location replaces the old location.",
                    }
                ]
            },
        ]
    )
    memory = Memory.from_config(
        {
            "storage": {"path": str(tmp_path / "memory.sqlite3")},
            "llm": {
                "provider": "openai_compatible",
                "model": "test-model",
                "retry_attempts": 2,
            },
        },
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
    )
    namespace = "local_triple_retry_invalid_refs_ns"
    memory.reset(namespace)

    memory.add_memory("I live in California.", namespace=namespace)
    memory.add_memory("I live in San Francisco.", namespace=namespace)
    node = memory.store.list_nodes(namespace)[0]
    memory.close()

    assert [(triple.object, triple.status) for triple in node.local_graph.triples] == [
        ("California", "retired"),
        ("San Francisco", "active"),
    ]
    assert maintenance_llm.call_count == 2


def test_local_triple_maintenance_keep_existing_reindexes_active_graph_without_incoming(tmp_path):
    maintenance_llm = MaintenanceLLM(
        [
            {
                "decisions": [
                    {
                        "incoming_ref": "incoming:0",
                        "decision": "keep_existing",
                        "affected_existing_refs": ["existing:0"],
                        "merged_triple": None,
                        "rationale": "The existing triple already covers the incoming triple.",
                    }
                ]
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
    distribution = node.metadata["maintenance"]["local_triples"]["triple_distribution"]
    assert distribution["total"] == 1
    assert distribution["active"] == 1
    assert distribution["by_status"] == {"active": 1}
    assert distribution["active_by_predicate"] == {"likes": 1}


def test_local_triple_maintenance_routes_same_turn_initial_node_conflicts(tmp_path):
    maintenance_llm = MaintenanceLLM(
        [
            {
                "decisions": [
                    {
                        "incoming_ref": "incoming:1",
                        "decision": "keep_both",
                        "affected_existing_refs": [],
                        "merged_triple": None,
                        "rationale": "Both preferences can coexist.",
                    }
                ]
            }
        ]
    )
    memory = Memory.from_config(
        {"storage": {"path": str(tmp_path / "memory.sqlite3")}},
        extractor=SequenceHomogeneousExtractor(
            [
                _single_entity_payload(
                    "Alice likes tea and coffee.",
                    triples=[
                        {"subject": "Alice", "predicate": "likes", "object": "tea"},
                        {"subject": "Alice", "predicate": "likes", "object": "coffee"},
                    ],
                )
            ]
        ),
        maintenance_llm=maintenance_llm,
    )
    namespace = "local_triple_initial_batch_ns"
    memory.reset(namespace)

    memory.add_memory("I like tea and coffee.", namespace=namespace)
    node = memory.store.list_nodes(namespace)[0]
    memory.close()

    assert maintenance_llm.call_count == 1
    assert '"object":"coffee"' in maintenance_llm.prompts[0]
    assert '"object":"tea"' in maintenance_llm.prompts[0]
    assert [(triple.object, triple.status) for triple in node.local_graph.triples] == [
        ("tea", "active"),
        ("coffee", "active"),
    ]
    assert all(triple.qualifiers["source_turn_ids"] == ["turn:0"] for triple in node.local_graph.triples)
    assert node.metadata["maintenance"]["local_triples"]["decision_count"] == 1


def test_local_triple_maintenance_same_turn_merge_dedupes_turn_id(tmp_path):
    maintenance_llm = MaintenanceLLM(
        [
            {
                "decisions": [
                    {
                        "incoming_ref": "incoming:1",
                        "decision": "merge",
                        "affected_existing_refs": ["existing:0"],
                        "merged_triple": {
                            "subject": "Alice",
                            "predicate": "likes",
                            "object": "tea and coffee",
                            "qualifiers": {},
                        },
                        "rationale": "Both values are compatible preferences from the same turn.",
                    }
                ]
            }
        ]
    )
    memory = Memory.from_config(
        {"storage": {"path": str(tmp_path / "memory.sqlite3")}},
        extractor=SequenceHomogeneousExtractor(
            [
                _single_entity_payload(
                    "Alice likes tea and coffee.",
                    triples=[
                        {"subject": "Alice", "predicate": "likes", "object": "tea"},
                        {"subject": "Alice", "predicate": "likes", "object": "coffee"},
                    ],
                )
            ]
        ),
        maintenance_llm=maintenance_llm,
    )
    namespace = "local_triple_same_turn_merge_ns"
    memory.reset(namespace)

    memory.add_memory("I like tea and coffee.", namespace=namespace)
    node = memory.store.list_nodes(namespace)[0]
    memory.close()

    assert maintenance_llm.call_count == 1
    assert "prefer `merge` over" in maintenance_llm.prompts[0]
    assert [(triple.object, triple.status) for triple in node.local_graph.triples] == [
        ("tea", "retired"),
        ("tea and coffee", "active"),
    ]
    merged = node.local_graph.triples[1]
    assert merged.qualifiers["source_turn_ids"] == ["turn:0"]
    assert merged.qualifiers["maintenance_merged_source_turn_ids"] == ["turn:0"]
    assert len(merged.qualifiers["source_triple_ids"]) == 2


def test_local_triple_maintenance_requires_llm_for_same_turn_initial_node_conflicts(tmp_path):
    memory = Memory.from_config(
        {"storage": {"path": str(tmp_path / "memory.sqlite3")}},
        extractor=SequenceHomogeneousExtractor(
            [
                _single_entity_payload(
                    "Alice lives in California and San Francisco.",
                    triples=[
                        {"subject": "Alice", "predicate": "lives_in", "object": "California"},
                        {"subject": "Alice", "predicate": "lives_in", "object": "San Francisco"},
                    ],
                )
            ]
        ),
    )
    namespace = "local_triple_initial_requires_llm_ns"
    memory.reset(namespace)

    with pytest.raises(RuntimeError, match="requires an LLM"):
        memory.add_memory("I live in California and San Francisco.", namespace=namespace)
    memory.close()


def test_local_triple_maintenance_keep_both_preserves_compatible_values(tmp_path):
    maintenance_llm = MaintenanceLLM(
        [
            {
                "decisions": [
                    {
                        "incoming_ref": "incoming:0",
                        "decision": "keep_both",
                        "affected_existing_refs": [],
                        "merged_triple": None,
                        "rationale": "Both preferences can coexist.",
                    }
                ]
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


def test_local_triple_maintenance_accepts_decisions_object_response(tmp_path):
    maintenance_llm = MaintenanceLLM(
        [
            {
                "decisions": [
                    {
                        "incoming_ref": "incoming:0",
                        "decision": "keep_both",
                        "affected_existing_refs": [],
                        "merged_triple": None,
                        "rationale": "Both preferences can coexist.",
                    }
                ]
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
    namespace = "local_triple_decisions_object_ns"
    memory.reset(namespace)

    memory.add_memory("I like tea.", namespace=namespace)
    memory.add_memory("I like coffee.", namespace=namespace)
    node = memory.store.list_nodes(namespace)[0]
    memory.close()

    assert [(triple.object, triple.status) for triple in node.local_graph.triples] == [
        ("tea", "active"),
        ("coffee", "active"),
    ]


def test_local_triple_maintenance_merge_replaces_candidates_with_merged_triple(tmp_path):
    maintenance_llm = MaintenanceLLM(
        [
            {
                "decisions": [
                    {
                        "incoming_ref": "incoming:0",
                        "decision": "merge",
                        "affected_existing_refs": ["existing:0"],
                        "merged_triple": {
                            "subject": "Alice",
                            "predicate": "works_at",
                            "object": "OpenAI in San Francisco",
                            "qualifiers": {"specificity": "city"},
                            "status": "active",
                            "triple_id": "llm-owned-id-should-be-ignored",
                        },
                        "rationale": "The new triple is a more specific version.",
                    }
                ]
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
    assert "status" not in node.local_graph.triples[1].qualifiers
    assert node.local_graph.triples[1].triple_id != "llm-owned-id-should-be-ignored"
    assert node.local_graph.triples[0].superseded_by == node.local_graph.triples[1].triple_id
    merged_from_ids = node.local_graph.triples[1].qualifiers["maintenance_merged_triple_ids"]
    assert merged_from_ids[0] == node.local_graph.triples[0].triple_id
    assert merged_from_ids[1].startswith("triple:")
    assert merged_from_ids[1] != node.local_graph.triples[1].triple_id
    assert len(node.local_graph.triples[1].qualifiers["source_triple_ids"]) == 2
    assert node.local_graph.triples[1].qualifiers["maintenance_merged_source_turn_ids"] == ["turn:0", "turn:1"]


def test_local_triple_maintenance_batches_multiple_conflicts_for_same_node(tmp_path):
    maintenance_llm = MaintenanceLLM(
        [
            {
                "decisions": [
                    {
                        "incoming_ref": "incoming:1",
                        "decision": "keep_both",
                        "affected_existing_refs": [],
                        "merged_triple": None,
                        "rationale": "Both preferences can coexist.",
                    },
                    {
                        "incoming_ref": "incoming:0",
                        "decision": "keep_new",
                        "affected_existing_refs": ["existing:0"],
                        "merged_triple": None,
                        "rationale": "The new city replaces the older residence.",
                    },
                ]
            }
        ]
    )
    memory = Memory.from_config(
        {"storage": {"path": str(tmp_path / "memory.sqlite3")}},
        extractor=SequenceHomogeneousExtractor(
            [
                _single_entity_payload(
                    "Alice lives in California and likes tea.",
                    triples=[
                        {"subject": "Alice", "predicate": "lives_in", "object": "California"},
                        {"subject": "Alice", "predicate": "likes", "object": "tea"},
                    ],
                ),
                _single_entity_payload(
                    "Alice lives in San Francisco and likes coffee.",
                    triples=[
                        {"subject": "Alice", "predicate": "lives_in", "object": "San Francisco"},
                        {"subject": "Alice", "predicate": "likes", "object": "coffee"},
                    ],
                ),
            ]
        ),
        maintenance_llm=maintenance_llm,
    )
    namespace = "local_triple_batch_ns"
    memory.reset(namespace)

    memory.add_memory("I live in California and like tea.", namespace=namespace)
    memory.add_memory("I live in San Francisco and like coffee.", namespace=namespace)
    node = memory.store.list_nodes(namespace)[0]
    memory.close()

    assert maintenance_llm.call_count == 1
    assert '"incoming_ref":"incoming:0"' in maintenance_llm.prompts[0]
    assert '"incoming_ref":"incoming:1"' in maintenance_llm.prompts[0]
    assert '"predicate":"lives_in"' in maintenance_llm.prompts[0]
    assert '"object":"San Francisco"' in maintenance_llm.prompts[0]
    assert '"predicate":"likes"' in maintenance_llm.prompts[0]
    assert '"object":"coffee"' in maintenance_llm.prompts[0]
    assert [(triple.predicate, triple.object, triple.status) for triple in node.local_graph.triples] == [
        ("lives_in", "California", "retired"),
        ("likes", "tea", "active"),
        ("lives_in", "San Francisco", "active"),
        ("likes", "coffee", "active"),
    ]
    assert node.metadata["maintenance"]["local_triples"]["decision_count"] == 2
    distribution = node.metadata["maintenance"]["local_triples"]["triple_distribution"]
    assert distribution["total"] == 4
    assert distribution["active"] == 3
    assert distribution["retired"] == 1
    assert distribution["active_by_predicate"] == {"likes": 2, "lives_in": 1}
    assert distribution["active_by_subject_predicate"] == {"alice|likes": 2, "alice|lives_in": 1}


def test_local_triple_maintenance_rejects_unmatched_incoming_refs(tmp_path):
    maintenance_llm = MaintenanceLLM(
        [
            {
                "decisions": [
                    {
                        "incoming_ref": "incoming:1",
                        "decision": "keep_both",
                        "affected_existing_refs": [],
                        "merged_triple": None,
                        "rationale": "Mismatched ref should fail.",
                    }
                ]
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
    namespace = "local_triple_unmatched_incoming_ref_ns"
    memory.reset(namespace)

    memory.add_memory("I like tea.", namespace=namespace)
    with pytest.raises(RuntimeError, match="decision refs did not match conflicts"):
        memory.add_memory("I like coffee.", namespace=namespace)
    memory.close()


def test_local_triple_maintenance_rejects_unknown_existing_refs(tmp_path):
    maintenance_llm = MaintenanceLLM(
        [
            {
                "decisions": [
                    {
                        "incoming_ref": "incoming:0",
                        "decision": "keep_new",
                        "affected_existing_refs": ["existing:99"],
                        "merged_triple": None,
                        "rationale": "Unknown candidate ref should fail.",
                    }
                ]
            }
        ]
    )
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
    )
    namespace = "local_triple_unknown_existing_ref_ns"
    memory.reset(namespace)

    memory.add_memory("I live in California.", namespace=namespace)
    with pytest.raises(RuntimeError, match="unknown existing refs"):
        memory.add_memory("I live in San Francisco.", namespace=namespace)
    memory.close()


def test_local_triple_maintenance_requires_affected_refs_for_replacement_decisions(tmp_path):
    maintenance_llm = MaintenanceLLM(
        [
            {
                "decisions": [
                    {
                        "incoming_ref": "incoming:0",
                        "decision": "keep_new",
                        "affected_existing_refs": [],
                        "merged_triple": None,
                        "rationale": "Replacement decisions must identify affected candidates.",
                    }
                ]
            }
        ]
    )
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
    )
    namespace = "local_triple_requires_affected_refs_ns"
    memory.reset(namespace)

    memory.add_memory("I live in California.", namespace=namespace)
    with pytest.raises(RuntimeError, match="requires affected_existing_refs"):
        memory.add_memory("I live in San Francisco.", namespace=namespace)
    memory.close()


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
    assert item_types.count("node_content") == 3
    assert item_types.count("node_local_graph") == 3
    assert item_types.count("hyper_edge_description") == 2
    assert item_types.count("turn_dialogue") == 1

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

    content_record = next(
        record
        for record in vector_store.records
        if record.payload["item_type"] == "node_content"
        and record.payload["node_id"] == preference.node_id
    )
    assert content_record.text == (
        "Alice prefers morning interviews.\n"
        "Alice has a scheduling preference for morning interviews."
    )

    edge_descriptions = {edge.description for edge in edges}
    edge_records = [
        record for record in vector_store.records if record.payload["item_type"] == "hyper_edge_description"
    ]
    assert {record.text for record in edge_records} == edge_descriptions
    assert {record.payload["edge_id"] for record in edge_records} == {edge.edge_id for edge in edges}


def test_turn_dialogue_vector_indexing_can_be_disabled(tmp_path):
    embedding_client = RecordingEmbeddingClient()
    vector_store = RecordingVectorStore()
    memory = Memory.from_config(
        {
            "storage": {"path": str(tmp_path / "memory.sqlite3")},
            "turn": {"indexing": {"vector": False}},
        },
        extractor=StaticHomogeneousExtractor(),
        embedding_client=embedding_client,
        vector_store=vector_store,
    )
    namespace = "turn_dialogue_vector_disabled_ns"
    memory.reset(namespace)

    memory.add_memory(
        user_input="Alice prefers morning interviews.",
        assistant_output="I will remember that.",
        namespace=namespace,
    )
    memory.close()

    item_types = [record.payload["item_type"] for record in vector_store.records]
    assert "turn_dialogue" not in item_types
    embedded_texts = [text for batch in embedding_client.inputs for text in batch]
    assert "User: Alice prefers morning interviews.\nAssistant: I will remember that." not in embedded_texts


def test_empty_active_local_graph_deletes_stale_node_local_graph_vector(tmp_path):
    vector_store = RecordingVectorStore()
    memory = Memory.from_config(
        {"storage": {"path": str(tmp_path / "memory.sqlite3")}},
        embedding_client=RecordingEmbeddingClient(),
        vector_store=vector_store,
    )
    namespace = "empty_local_graph_vector_ns"
    node = MemoryNode(
        node_id="node:empty-local-graph",
        namespace=namespace,
        canonical_text="Alice",
        normalized_text="alice",
        fingerprint="sha256:alice",
        content="Alice",
        local_graph=LocalNodeGraph(
            triples=[
                LocalTriple(
                    triple_id="triple:retired",
                    subject="Alice",
                    predicate="likes",
                    object="tea",
                    status="retired",
                )
            ]
        ),
    )

    memory._index_nodes_edges_and_clusters([node], [], [])
    memory.close()

    assert make_vector_point_id(namespace, "node_local_graph", node.node_id) in vector_store.deleted_ids
    assert all(record.payload["item_type"] != "node_local_graph" for record in vector_store.records)


def test_vector_retrieval_uses_separate_node_rrf_channels(tmp_path):
    embedding_client = RecordingEmbeddingClient()
    vector_store = RecordingVectorStore()
    memory = Memory.from_config(
        {
            "storage": {"path": str(tmp_path / "memory.sqlite3")},
            "retrieval": {"node_rrf_k": 10},
        },
        extractor=StaticHomogeneousExtractor(),
        embedding_client=embedding_client,
        vector_store=vector_store,
    )
    namespace = "separate_node_rrf_channels_ns"
    memory.reset(namespace)

    memory.add_memory("Alice prefers morning interviews.", namespace=namespace)
    content_record = next(record for record in vector_store.records if record.payload["item_type"] == "node_content")
    vector_store.search_hits = [
        VectorSearchHit(
            id=content_record.id,
            score=0.92,
            payload=content_record.payload,
            text=content_record.text,
        )
    ]

    results = memory.search("opaque vector query", namespace=namespace, top_k=1)
    memory.close()

    assert [call["top_k"] for call in vector_store.search_calls[-3:]] == [20, 20, 10]
    assert "node_content" in results[0]["metadata"]["channels"]
    assert any(
        node["score_parts"].get("rrf_node_content") == 1 / 11
        for node in results[0]["metadata"]["edge_nodes"]
    )


def test_hyper_edge_description_vector_recall_returns_edge_candidate(tmp_path):
    embedding_client = RecordingEmbeddingClient()
    vector_store = RecordingVectorStore()
    memory = Memory.from_config(
        {
            "storage": {"path": str(tmp_path / "memory.sqlite3")},
            "retrieval": {
                "node_rrf_k": 10,
                "edge_rrf_k": 10,
                "hyper_edge_description_vector_top_k": 3,
            },
        },
        extractor=StaticHomogeneousExtractor(),
        embedding_client=embedding_client,
        vector_store=vector_store,
    )
    namespace = "hyper_edge_description_vector_recall_ns"
    memory.reset(namespace)

    memory.add_memory("Alice prefers morning interviews.", namespace=namespace)
    edge_record = next(
        record
        for record in vector_store.records
        if record.payload["item_type"] == "hyper_edge_description"
    )
    vector_store.search_hits = [
        VectorSearchHit(
            id=edge_record.id,
            score=0.98,
            payload=edge_record.payload,
            text=edge_record.text,
        )
    ]

    results = memory.search("interview preference", namespace=namespace, top_k=1)
    memory.close()

    assert [call["top_k"] for call in vector_store.search_calls[-3:]] == [20, 20, 3]
    assert results
    assert results[0]["id"] == edge_record.payload["edge_id"]
    assert "hyper_edge_description_vector" in results[0]["metadata"]["channels"]
    assert results[0]["metadata"]["score_parts"]["rrf_track2"] == 1 / 11
    assert results[0]["metadata"]["score_parts"]["track2_rank"] == 1
    assert results[0]["metadata"]["edge_vector_hits"][0]["channel"] == "hyper_edge_description_vector"
    assert all(
        "rrf_hyper_edge_description_vector" not in node["score_parts"]
        for node in results[0]["metadata"]["edge_nodes"]
    )


def test_hyper_edge_description_track_stays_edge_level_without_node_projection(tmp_path):
    embedding_client = RecordingEmbeddingClient()
    vector_store = RecordingVectorStore()
    memory = Memory.from_config(
        {
            "storage": {"path": str(tmp_path / "memory.sqlite3")},
            "retrieval": {
                "node_rrf_k": 10,
                "edge_rrf_k": 10,
                "hyper_edge_description_vector_top_k": 2,
            },
        },
        extractor=StaticHomogeneousExtractor(),
        embedding_client=embedding_client,
        vector_store=vector_store,
    )
    namespace = "projected_edge_rrf_max_ns"
    memory.reset(namespace)

    memory.add_memory("Alice prefers morning interviews.", namespace=namespace)
    edge_records = [
        record
        for record in vector_store.records
        if record.payload["item_type"] == "hyper_edge_description"
    ]
    vector_store.search_hits = [
        VectorSearchHit(
            id=record.id,
            score=0.98 - index * 0.01,
            payload=record.payload,
            text=record.text,
        )
        for index, record in enumerate(edge_records)
    ]

    results = memory.search("opaque edge vector query", namespace=namespace, top_k=2)
    memory.close()

    preference_node = next(
        node
        for result in results
        for node in result["metadata"]["edge_nodes"]
        if node["content"] == "Alice prefers morning interviews."
    )
    assert "rrf_hyper_edge_description_vector" not in preference_node["score_parts"]
    assert preference_node["matched_vector_items"] == []
    assert [result["metadata"]["score_parts"]["rrf_track2"] for result in results] == pytest.approx(
        [1 / 11, 1 / 12]
    )
    assert {
        hit["payload"]["edge_id"]
        for result in results
        for hit in result["metadata"]["edge_vector_hits"]
        if hit["channel"] == "hyper_edge_description_vector"
    } == {record.payload["edge_id"] for record in edge_records}
    assert all("edge_coherence_bonus" not in result["metadata"]["score_parts"] for result in results)


def test_hyperedge_maintenance_reuses_same_member_set_and_reindexes_description(tmp_path):
    embedding_client = RecordingEmbeddingClient()
    vector_store = RecordingVectorStore()
    memory = Memory.from_config(
        {
            "storage": {"path": str(tmp_path / "memory.sqlite3")},
            "maintenance": {
                "hyper_edge_description": {
                    "compact_after_k_sources": 3,
                    "max_tokens": 1000,
                }
            },
        },
        extractor=SequenceHomogeneousExtractor(
            [
                _single_entity_payload("Alice is the user.", edge_description="Alice identity."),
                _single_entity_payload("Alice is preparing for interviews.", edge_description="Alice interview context."),
            ]
        ),
        embedding_client=embedding_client,
        vector_store=vector_store,
    )
    namespace = "hyperedge_reuse_ns"
    memory.reset(namespace)

    memory.add_memory("I am Alice.", namespace=namespace)
    memory.add_memory("I am preparing for interviews.", namespace=namespace)
    edges = memory.store.list_edges(namespace)
    memory.close()

    assert len(edges) == 1
    edge = edges[0]
    assert edge.description == "Alice identity.\nAlice interview context."
    assert edge.metadata["source_turn_ids"] == ["turn:0", "turn:1"]
    assert edge.metadata["edge_summary_refs"] == ["e1"]
    state = edge.metadata["maintenance"]["hyper_edge_description"]
    assert state["description_source_turn_ids"] == ["turn:0", "turn:1"]
    assert state["pending_source_turn_ids"] == ["turn:0", "turn:1"]
    edge_records = [
        record for record in vector_store.records if record.payload["item_type"] == "hyper_edge_description"
    ]
    assert len(edge_records) == 2
    assert edge_records[0].id == edge_records[1].id
    assert edge_records[-1].text == edge.description
    assert edge_records[-1].payload["edge_metadata"]["source_turn_ids"] == ["turn:0", "turn:1"]


def test_hyperedge_description_compacts_at_k_sources(tmp_path):
    maintenance_llm = MaintenanceLLM([{"description": "Alice profile and interview context."}])
    memory = Memory.from_config(
        {
            "storage": {"path": str(tmp_path / "memory.sqlite3")},
            "maintenance": {
                "hyper_edge_description": {
                    "compact_after_k_sources": 2,
                    "max_tokens": 1000,
                }
            },
        },
        extractor=SequenceHomogeneousExtractor(
            [
                _single_entity_payload("Alice is the user.", edge_description="Alice identity."),
                _single_entity_payload("Alice is preparing for interviews.", edge_description="Alice interview context."),
            ]
        ),
        maintenance_llm=maintenance_llm,
    )
    namespace = "hyperedge_description_compact_ns"
    memory.reset(namespace)

    memory.add_memory("I am Alice.", namespace=namespace)
    memory.add_memory("I am preparing for interviews.", namespace=namespace)
    edge = memory.store.list_edges(namespace)[0]
    memory.close()

    assert maintenance_llm.call_count == 1
    assert "Alice identity.\nAlice interview context." in maintenance_llm.prompts[0]
    assert "edge_id" not in maintenance_llm.prompts[0]
    assert edge.description == "Alice profile and interview context."
    assert edge.metadata["source_turn_ids"] == ["turn:0", "turn:1"]
    state = edge.metadata["maintenance"]["hyper_edge_description"]
    assert state["pending_source_turn_ids"] == []
    assert state["compaction_count"] == 1
    assert state["last_compaction_trigger"]["reasons"] == ["source_count"]


def test_hyperedge_description_retries_invalid_compaction_payload(tmp_path):
    maintenance_llm = MaintenanceLLM([{"summary": "wrong field"}, {"description": "Alice compact edge."}])
    memory = Memory.from_config(
        {
            "storage": {"path": str(tmp_path / "memory.sqlite3")},
            "llm": {
                "provider": "openai_compatible",
                "model": "test-model",
                "retry_attempts": 2,
            },
            "maintenance": {
                "hyper_edge_description": {
                    "compact_after_k_sources": 2,
                    "max_tokens": 1000,
                }
            },
        },
        extractor=SequenceHomogeneousExtractor(
            [
                _single_entity_payload("Alice is the user.", edge_description="Alice identity."),
                _single_entity_payload("Alice is preparing for interviews.", edge_description="Alice interview context."),
            ]
        ),
        maintenance_llm=maintenance_llm,
    )
    namespace = "hyperedge_description_retry_invalid_payload_ns"
    memory.reset(namespace)

    memory.add_memory("I am Alice.", namespace=namespace)
    memory.add_memory("I am preparing for interviews.", namespace=namespace)
    edge = memory.store.list_edges(namespace)[0]
    memory.close()

    assert maintenance_llm.call_count == 2
    assert edge.description == "Alice compact edge."


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


class RecordingWindowExtractor:
    def __init__(self):
        self.windows = []

    def extract(self, window, context):
        self.windows.append(window)
        return MemoryExtraction()


def _single_entity_payload(summary, *, triples=None, edge_description="Alice profile."):
    return {
        "edge_summaries": [{"ref": "e1", "description": edge_description}],
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


class RecordingTokenCounter:
    def __init__(self, result):
        self.result = result
        self.inputs = []

    def count(self, text):
        self.inputs.append(text)
        return self.result


class RecordingVectorStore:
    def __init__(self):
        self.records = []
        self.search_hits = []
        self.search_calls = []
        self.deleted_namespaces = []
        self.deleted_ids = []
        self.closed = False

    def upsert(self, records):
        self.records.extend(records)

    def search(self, *, query, vector, top_k, filters=None):
        self.search_calls.append(
            {
                "query": query,
                "vector": vector,
                "top_k": top_k,
                "filters": filters or {},
            }
        )
        return list(self.search_hits)[:top_k]

    def delete(self, ids):
        self.deleted_ids.extend(ids)

    def delete_namespace(self, namespace):
        self.deleted_namespaces.append(namespace)

    def close(self):
        self.closed = True
