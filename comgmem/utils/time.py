from __future__ import annotations

from datetime import datetime, timezone

from comgmem.schema import HyperEdge, MemoryNode, TimeBundle, ValidTime


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def make_time_bundle(
    *,
    current_turn: int | None = None,
    event_time: str | None = None,
    source_timestamp: str | None = None,
    valid_start: str | None = None,
) -> TimeBundle:
    now = utc_now_iso()
    bundle = TimeBundle()
    absolute_time = event_time or now
    bundle.world.event_time = absolute_time
    bundle.world.source_timestamp = source_timestamp or now
    bundle.world.valid_time = ValidTime(start=valid_start or absolute_time)
    bundle.lifecycle.created_at = now
    bundle.lifecycle.inserted_at = now
    bundle.activation.created_turn = current_turn
    bundle.activation.inserted_turn = current_turn
    return bundle


def touch_node_update(node: MemoryNode, current_turn: int | None = None) -> MemoryNode:
    node.time.lifecycle.updated_at = utc_now_iso()
    node.time.activation.updated_turn = current_turn
    return node


def touch_edge_update(edge: HyperEdge, current_turn: int | None = None) -> HyperEdge:
    edge.time.lifecycle.updated_at = utc_now_iso()
    edge.time.activation.updated_turn = current_turn
    return edge


def touch_node_access(node: MemoryNode, current_turn: int | None = None) -> MemoryNode:
    node.time.activation.last_access_turn = current_turn
    node.time.activation.access_count += 1
    return node


def touch_edge_access(edge: HyperEdge, current_turn: int | None = None) -> HyperEdge:
    edge.time.activation.last_access_turn = current_turn
    edge.time.activation.access_count += 1
    return edge


def decay_weight(inserted_turn: int | None, current_turn: int | None, decay_lambda: float) -> float:
    if inserted_turn is None or current_turn is None:
        return 1.0
    distance = max(0, current_turn - inserted_turn)
    return pow(2.718281828459045, -decay_lambda * distance)
