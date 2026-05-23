from __future__ import annotations

from c_hypermem.schema import MemoryNode


def compose_result_content(node: MemoryNode, edge_types: list[str]) -> str:
    label = "+".join(node.node_labels) if node.node_labels else "memory"
    source = node.metadata.get("source_session_id")
    date = node.metadata.get("date") or node.time.world.event_time
    suffix_parts = []
    if source:
        suffix_parts.append(f"session={source}")
    if date:
        suffix_parts.append(f"date={date}")
    if edge_types:
        suffix_parts.append(f"edge_types={','.join(edge_types)}")
    suffix = f"\nSource: {' '.join(suffix_parts)}" if suffix_parts else ""
    return f"[{label}] {node.content}{suffix}"
