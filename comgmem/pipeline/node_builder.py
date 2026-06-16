from __future__ import annotations

from typing import Any

from comgmem.pipeline.context import AssemblyContext
from comgmem.pipeline.graph_utils import dedupe_labels, source_metadata
from comgmem.pipeline.local_graph_builder import LocalGraphBuilder
from comgmem.schema import ExtractedNode, LocalNodeGraph, MemoryNode
from comgmem.utils.ids import make_fingerprint, make_node_id
from comgmem.utils.text import normalize_text
from comgmem.utils.time import make_time_bundle


class NodeBuilder:
    """Build MemoryNodes from homogeneous extracted node candidates."""

    def __init__(self, local_graph_builder: LocalGraphBuilder | None = None) -> None:
        self.local_graph_builder = local_graph_builder or LocalGraphBuilder()

    def build_node(self, extracted: ExtractedNode, context: AssemblyContext) -> MemoryNode:
        canonical = extracted.canonical_text.strip()
        fingerprint = make_fingerprint(canonical, _disambiguation_hint(extracted))
        summaries = [summary.strip() for summary in extracted.summaries if summary.strip()]
        node = MemoryNode(
            node_id=make_node_id(context.namespace, fingerprint),
            namespace=context.namespace,
            canonical_text=canonical,
            normalized_text=normalize_text(canonical),
            fingerprint=fingerprint,
            node_labels=dedupe_labels(extracted.labels),
            content=canonical,
            summary=" ".join(summaries),
            attributes=_node_attributes(extracted),
            metadata=source_metadata(
                context,
                extra={
                    "extraction_ref": extracted.ref,
                    "edge_summary_refs": list(extracted.edge_summary_refs),
                    **dict(extracted.metadata),
                },
            ),
            time=make_time_bundle(
                current_turn=context.current_turn,
                event_time=None,
                source_timestamp=context.metadata.get("timestamp"),
            ),
            local_graph=LocalNodeGraph(),
        )
        return self.local_graph_builder.build_node(node, extracted)


def _node_attributes(extracted: ExtractedNode) -> dict[str, Any]:
    attributes: dict[str, Any] = {}
    aliases = extracted.metadata.get("aliases")
    if isinstance(aliases, list):
        attributes["aliases"] = [str(alias).strip() for alias in aliases if str(alias).strip()]
    entity_type = extracted.metadata.get("entity_type") or extracted.metadata.get("type")
    if isinstance(entity_type, str) and entity_type.strip():
        attributes["entity_type"] = entity_type.strip()
    return attributes


def _disambiguation_hint(extracted: ExtractedNode) -> dict[str, Any]:
    hint: dict[str, Any] = {}
    entity_type = extracted.metadata.get("entity_type") or extracted.metadata.get("type")
    if isinstance(entity_type, str) and entity_type.strip():
        hint["entity_type"] = entity_type.strip()
    disambiguation = extracted.metadata.get("disambiguation_hint")
    if isinstance(disambiguation, dict):
        hint.update(disambiguation)
    return hint
