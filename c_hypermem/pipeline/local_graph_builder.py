from __future__ import annotations

from c_hypermem.schema import ExtractedNode, LocalNodeGraph, LocalTriple, MemoryNode
from c_hypermem.utils.text import normalize_text


class LocalGraphBuilder:
    """Build uniform LocalNodeGraph payloads from extracted node triples."""

    def build(self, nodes: list[MemoryNode]) -> list[MemoryNode]:
        return nodes

    def build_node(self, node: MemoryNode, extracted: ExtractedNode) -> MemoryNode:
        triples: list[LocalTriple] = []
        seen: set[tuple[str, str, str]] = set()
        for triple in extracted.triples:
            key = (
                normalize_text(triple.subject),
                normalize_text(triple.predicate),
                normalize_text(triple.object),
            )
            if not all(key) or key in seen:
                continue
            seen.add(key)
            triples.append(
                LocalTriple(
                    subject=triple.subject,
                    predicate=triple.predicate,
                    object=triple.object,
                    qualifiers=dict(triple.qualifiers),
                )
            )
        node.local_graph = LocalNodeGraph(triples=triples)
        return node
