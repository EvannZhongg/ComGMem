from __future__ import annotations

from c_hypermem import Memory
from c_hypermem.pipeline.context import AssemblyContext
from c_hypermem.pipeline.local_graph_builder import LocalGraphBuilder
from c_hypermem.pipeline.node_builder import NodeBuilder
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
