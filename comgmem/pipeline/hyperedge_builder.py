from __future__ import annotations

from typing import Any, Protocol

from comgmem.config import MemoryConfig
from comgmem.pipeline.context import AssemblyContext
from comgmem.pipeline.graph_utils import source_metadata
from comgmem.schema import ExtractedEdgeSummary, HyperEdge, MemoryNode
from comgmem.utils.ids import make_edge_fingerprint, make_edge_id, make_member_signature
from comgmem.utils.time import make_time_bundle


class HyperEdgeBuilder(Protocol):
    """Builds additional HyperEdges from assembled memory nodes."""

    def build(
        self,
        nodes: list[MemoryNode],
        *,
        namespace: str,
        metadata: dict[str, Any],
        current_turn: int,
    ) -> list[HyperEdge]: ...


class BasicHyperEdgeBuilder:
    """Build description-only HyperEdges from extracted edge summaries."""

    def __init__(self, config: MemoryConfig) -> None:
        self.config = config

    def build(
        self,
        nodes: list[MemoryNode],
        *,
        namespace: str,
        metadata: dict[str, Any],
        current_turn: int,
    ) -> list[HyperEdge]:
        return []

    def build_from_summary(
        self,
        edge_summary: ExtractedEdgeSummary,
        member_nodes: list[MemoryNode],
        context: AssemblyContext,
    ) -> HyperEdge:
        node_ids = list(dict.fromkeys(node.node_id for node in member_nodes))
        edge_fingerprint = make_edge_fingerprint(node_ids)
        edge = HyperEdge(
            edge_id=make_edge_id(context.namespace, edge_fingerprint),
            namespace=context.namespace,
            edge_fingerprint=edge_fingerprint,
            description=edge_summary.description,
            member_signature=make_member_signature(node_ids),
            node_ids=node_ids,
            weights={node_id: 1.0 for node_id in node_ids},
            metadata=source_metadata(
                context,
                extra={
                    "edge_summary_refs": [edge_summary.ref],
                    **dict(edge_summary.metadata),
                },
            ),
            time=make_time_bundle(
                current_turn=context.current_turn,
                event_time=None,
                source_timestamp=context.metadata.get("timestamp"),
            ),
        )
        _scope_member_triples(edge, member_nodes)
        return edge


def _scope_member_triples(edge: HyperEdge, member_nodes: list[MemoryNode]) -> None:
    edge_source_turn_ids = _strings(edge.metadata.get("source_turn_ids"))
    for node in member_nodes:
        for triple in node.local_graph.triples:
            if edge_source_turn_ids and not set(edge_source_turn_ids).intersection(
                _strings(triple.qualifiers.get("source_turn_ids"))
            ):
                continue
            qualifiers = dict(triple.qualifiers)
            scope_edge_ids = _unique_strings([*triple.scope_edge_ids, *_strings(qualifiers.get("scope_edge_ids"))])
            if edge.edge_id not in scope_edge_ids:
                scope_edge_ids.append(edge.edge_id)
            qualifiers["scope_edge_ids"] = scope_edge_ids

            edge_descriptions = qualifiers.get("scope_edge_descriptions")
            edge_descriptions = dict(edge_descriptions) if isinstance(edge_descriptions, dict) else {}
            if edge.description.strip():
                edge_descriptions[edge.edge_id] = edge.description
            if edge_descriptions:
                qualifiers["scope_edge_descriptions"] = edge_descriptions

            triple.qualifiers = qualifiers
            triple.scope_edge_ids = scope_edge_ids


def _strings(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    if value in (None, "", [], {}):
        return []
    return [str(value).strip()]


def _unique_strings(values: list[str]) -> list[str]:
    return list(dict.fromkeys(value for value in values if value))
