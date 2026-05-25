from __future__ import annotations

from typing import Any

from c_hypermem.utils.hashing import stable_hash
from c_hypermem.utils.text import normalize_text


SYSTEM_TRIPLE_QUALIFIER_KEYS = frozenset(
    {
        "scope_edge_id",
        "scope_cluster_id",
        "edge_description",
        "maintenance_status_reason",
        "maintenance_updated_turn",
        "maintenance_updated_at",
        "maintenance_decision",
        "maintenance_rationale",
        "source_turn_ids",
        "source_triple_ids",
        "maintenance_last_action",
        "maintenance_related_triple_ids",
        "maintenance_related_source_turn_ids",
        "maintenance_discarded_triple_ids",
        "maintenance_discarded_source_turn_ids",
        "maintenance_replaced_triple_ids",
        "maintenance_replaced_source_turn_ids",
        "maintenance_replaced_by_triple_id",
        "maintenance_merged_triple_ids",
        "maintenance_merged_source_turn_ids",
    }
)


def make_fingerprint(canonical_text: str, disambiguation_hint: Any | None = None) -> str:
    digest = stable_hash(normalize_text(canonical_text), disambiguation_hint or {}, length=64)
    return f"sha256:{digest}"


def make_node_id(namespace: str, fingerprint: str) -> str:
    digest = stable_hash(namespace, fingerprint)
    return f"node:{digest}"


def make_edge_id(namespace: str, edge_fingerprint: str) -> str:
    digest = stable_hash(namespace, edge_fingerprint)
    return f"edge:{digest}"


def make_cluster_id(namespace: str, cluster_fingerprint: str) -> str:
    digest = stable_hash(namespace, cluster_fingerprint)
    return f"cluster:{digest}"


def make_member_signature(member_ids: list[str], roles: dict[str, str] | None = None) -> str:
    role_items = sorted((roles or {}).items())
    digest = stable_hash(sorted(member_ids), role_items, length=64)
    return f"sha256:{digest}"


def semantic_triple_qualifiers(qualifiers: dict[str, Any] | None = None) -> dict[str, Any]:
    qualifiers = qualifiers or {}
    return {key: value for key, value in qualifiers.items() if key not in SYSTEM_TRIPLE_QUALIFIER_KEYS}


def make_triple_semantic_key(
    namespace: str,
    owner_node_id: str,
    subject: str,
    predicate: str,
    object_: str,
    qualifiers: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "namespace": namespace,
        "owner_node_id": owner_node_id,
        "subject": normalize_text(subject),
        "predicate": normalize_text(predicate),
        "object": normalize_text(object_),
        "qualifiers": semantic_triple_qualifiers(qualifiers),
    }


def make_triple_id_from_semantic_key(semantic_key: dict[str, Any]) -> str:
    digest = stable_hash(
        semantic_key["namespace"],
        semantic_key["owner_node_id"],
        semantic_key["subject"],
        semantic_key["predicate"],
        semantic_key["object"],
        semantic_key.get("qualifiers") or {},
    )
    return f"triple:{digest}"


def make_local_triple_id(
    namespace: str,
    owner_node_id: str,
    subject: str,
    predicate: str,
    object_: str,
    qualifiers: dict[str, Any] | None = None,
) -> str:
    return make_triple_id_from_semantic_key(
        make_triple_semantic_key(namespace, owner_node_id, subject, predicate, object_, qualifiers)
    )


def make_source_triple_id(namespace: str, triple_id: str, source_turn_ids: list[str]) -> str:
    digest = stable_hash(namespace, triple_id, sorted(source_turn_ids))
    return f"source-triple:{digest}"
