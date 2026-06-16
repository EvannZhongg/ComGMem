from __future__ import annotations

from comgmem.schema import LocalNodeGraph, LocalTriple, MemoryNode
from comgmem.stores.sqlite_store import SQLiteStore
from comgmem.utils.ids import make_local_triple_id, make_triple_semantic_key, semantic_triple_qualifiers


def test_local_triple_id_ignores_system_provenance_qualifiers():
    semantic_qualifiers = {"valid_time": {"as_of": "2024-01-03"}}
    with_provenance = {
        **semantic_qualifiers,
        "source_turn_ids": ["turn:0"],
        "source_triple_ids": ["source-triple:old"],
        "maintenance_last_action": "duplicate_spo",
        "maintenance_updated_turn": 3,
    }

    plain_id = make_local_triple_id("ns", "node:1", "Alice", "likes", "Tea", semantic_qualifiers)
    provenance_id = make_local_triple_id("ns", "node:1", " Alice ", "LIKES", "tea", with_provenance)

    assert provenance_id == plain_id
    assert semantic_triple_qualifiers(with_provenance) == semantic_qualifiers
    assert make_triple_semantic_key("ns", "node:1", " Alice ", "LIKES", "tea", with_provenance) == {
        "namespace": "ns",
        "owner_node_id": "node:1",
        "subject": "alice",
        "predicate": "likes",
        "object": "tea",
        "qualifiers": semantic_qualifiers,
    }


def test_sqlite_triple_id_fallback_uses_semantic_identity_without_provenance(tmp_path):
    store = SQLiteStore(tmp_path / "memory.sqlite3")
    node = MemoryNode(
        node_id="node:1",
        namespace="fallback_ns",
        canonical_text="Alice",
        normalized_text="alice",
        fingerprint="sha256:alice",
        content="Alice",
        local_graph=LocalNodeGraph(
            triples=[
                LocalTriple(
                    subject="Alice",
                    predicate="likes",
                    object="tea",
                    qualifiers={
                        "source_turn_ids": ["turn:0"],
                        "source_triple_ids": ["source-triple:old"],
                        "maintenance_last_action": "duplicate_spo",
                    },
                )
            ]
        ),
    )

    store.upsert_nodes([node])
    stored = store.get_nodes("fallback_ns", ["node:1"])[0]
    store.close()

    assert stored.local_graph.triples[0].triple_id == make_local_triple_id(
        "fallback_ns",
        "node:1",
        "Alice",
        "likes",
        "tea",
        {},
    )
