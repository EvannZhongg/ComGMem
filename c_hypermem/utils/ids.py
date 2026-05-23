from __future__ import annotations

from typing import Any

from c_hypermem.utils.hashing import stable_hash
from c_hypermem.utils.text import normalize_text


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


def make_triple_id(
    namespace: str,
    owner_node_id: str,
    subject: str,
    predicate: str,
    object_: str,
    qualifiers: dict[str, Any] | None = None,
) -> str:
    digest = stable_hash(
        namespace,
        owner_node_id,
        normalize_text(subject),
        normalize_text(predicate),
        normalize_text(object_),
        qualifiers or {},
    )
    return f"triple:{digest}"
