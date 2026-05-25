from __future__ import annotations

from typing import Any

from c_hypermem.pipeline.context import AssemblyContext
from c_hypermem.schema import (
    EdgeCluster,
    EdgeClusterMember,
    HyperEdge,
    LocalNodeGraph,
    MemoryNode,
)
from c_hypermem.utils.text import compact_key, normalize_text


def source_metadata(
    context: AssemblyContext,
    *,
    source_ref: str | None,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    metadata = {
        "source_ref": source_ref,
        "source_session_id": context.metadata.get("session_id") or context.metadata.get("conversation_id"),
        "date": context.metadata.get("date"),
        "source_turn_ids": context.metadata.get("turn_ids", []),
    }
    if extra:
        metadata.update(extra)
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
            merged[key] = list(dict.fromkeys([*merged[key], *value]))
        else:
            merged[key] = value
    return merged


def dedupe_edges(edges: list[HyperEdge]) -> list[HyperEdge]:
    return list({edge.edge_id: edge for edge in edges}.values())


def dedupe_clusters(clusters: list[EdgeCluster]) -> list[EdgeCluster]:
    by_id: dict[str, EdgeCluster] = {}
    for cluster in clusters:
        existing = by_id.get(cluster.cluster_id)
        if existing is None:
            by_id[cluster.cluster_id] = cluster
            continue
        existing.description_variants.extend(cluster.description_variants)
        existing.description_variants = existing.description_variants[:8]
        existing.conflict_state = (
            "contains_conflict"
            if "contains_conflict" in {existing.conflict_state, cluster.conflict_state}
            else existing.conflict_state
        )
    return list(by_id.values())


def dedupe_cluster_members(members: list[EdgeClusterMember]) -> list[EdgeClusterMember]:
    return list({(member.cluster_id, member.edge_id): member for member in members}.values())


def merge_node(existing: MemoryNode | None, incoming: MemoryNode, context: AssemblyContext) -> MemoryNode:
    if existing is None:
        return incoming
    from c_hypermem.utils.time import touch_node_update

    existing.node_labels = dedupe_labels([*existing.node_labels, *incoming.node_labels])
    existing.attributes = deep_merge_dict(existing.attributes, incoming.attributes)
    existing.metadata = deep_merge_dict(existing.metadata, incoming.metadata)
    existing.local_graph = merge_local_graph(existing.local_graph, incoming.local_graph)
    if not existing.summary and incoming.summary:
        existing.summary = incoming.summary
    if not existing.content and incoming.content:
        existing.content = incoming.content
    return touch_node_update(existing, context.current_turn)
