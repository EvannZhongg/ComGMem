from __future__ import annotations

from typing import Any, Protocol

from c_hypermem.config import MemoryConfig
from c_hypermem.pipeline.context import AssemblyContext
from c_hypermem.pipeline.graph_utils import source_metadata
from c_hypermem.schema import ExtractedEdgeSummary, HyperEdge, MemoryNode
from c_hypermem.utils.ids import make_edge_id, make_fingerprint, make_member_signature
from c_hypermem.utils.time import make_time_bundle


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
        edge_fingerprint = make_fingerprint(
            edge_summary.description,
            {
                "member_node_ids": sorted(node_ids),
                "source_turn_ids": context.metadata.get("turn_ids", []),
                "edge_ref": edge_summary.ref,
            },
        )
        edge = HyperEdge(
            edge_id=make_edge_id(context.namespace, edge_fingerprint),
            namespace=context.namespace,
            edge_fingerprint=edge_fingerprint,
            description=edge_summary.description,
            member_policy=self.config.hyperedges.member_policy_default,  # type: ignore[arg-type]
            member_signature=make_member_signature(node_ids),
            node_ids=node_ids,
            weights={node_id: 1.0 for node_id in node_ids},
            metadata=source_metadata(
                context,
                source_ref=None,
                extra={
                    "edge_summary_ref": edge_summary.ref,
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
    for node in member_nodes:
        for triple in node.local_graph.triples:
            qualifiers = dict(triple.qualifiers)
            qualifiers.setdefault("scope_edge_id", edge.edge_id)
            qualifiers.setdefault("edge_description", edge.description)
            triple.qualifiers = qualifiers
            triple.scope_edge_id = edge.edge_id
