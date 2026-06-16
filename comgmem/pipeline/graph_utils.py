from __future__ import annotations

import json
from typing import Any

from comgmem.pipeline.context import AssemblyContext
from comgmem.schema import (
    EdgeCluster,
    EdgeClusterMember,
    HyperEdge,
    LocalNodeGraph,
)
from comgmem.utils.text import compact_key, normalize_text


def source_metadata(
    context: AssemblyContext,
    *,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    metadata = dict(extra or {})
    metadata.update({
        "source_session_id": context.metadata.get("session_id") or context.metadata.get("conversation_id"),
        "date": context.metadata.get("date"),
        "source_turn_ids": context.metadata.get("turn_ids", []),
    })
    return {key: value for key, value in metadata.items() if value not in (None, [], {})}


def dedupe_labels(labels: list[str]) -> list[str]:
    cleaned = []
    for label in labels:
        value = compact_key(label)
        if value and value not in cleaned:
            cleaned.append(value)
    return cleaned


def string_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(item) for item in value]
    return [str(value)]


def merge_local_graph(existing: LocalNodeGraph, incoming: LocalNodeGraph) -> LocalNodeGraph:
    triple_keys = {
        (normalize_text(triple.subject), normalize_text(triple.predicate), normalize_text(triple.object))
        for triple in existing.triples
    }
    for triple in incoming.triples:
        key = (normalize_text(triple.subject), normalize_text(triple.predicate), normalize_text(triple.object))
        if key not in triple_keys:
            existing.triples.append(triple)
            triple_keys.add(key)
    return existing


def deep_merge_dict(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    merged = dict(base)
    for key, value in override.items():
        if value in (None, [], {}):
            continue
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = deep_merge_dict(merged[key], value)
        elif isinstance(value, list) and isinstance(merged.get(key), list):
            merged[key] = _dedupe_list([*merged[key], *value])
        else:
            merged[key] = value
    return merged


def _dedupe_list(values: list[Any]) -> list[Any]:
    seen: set[str] = set()
    deduped: list[Any] = []
    for value in values:
        marker = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
        if marker in seen:
            continue
        seen.add(marker)
        deduped.append(value)
    return deduped


def dedupe_edges(edges: list[HyperEdge]) -> list[HyperEdge]:
    return list({edge.edge_id: edge for edge in edges}.values())


def dedupe_clusters(clusters: list[EdgeCluster]) -> list[EdgeCluster]:
    by_id: dict[str, EdgeCluster] = {}
    for cluster in clusters:
        existing = by_id.get(cluster.cluster_id)
        if existing is None:
            by_id[cluster.cluster_id] = cluster
            continue
        existing.conflict_state = (
            "contains_conflict"
            if "contains_conflict" in {existing.conflict_state, cluster.conflict_state}
            else existing.conflict_state
        )
    return list(by_id.values())


def dedupe_cluster_members(members: list[EdgeClusterMember]) -> list[EdgeClusterMember]:
    return list({(member.cluster_id, member.edge_id): member for member in members}.values())
