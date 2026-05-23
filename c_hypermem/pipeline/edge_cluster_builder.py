from __future__ import annotations

from typing import Any, Protocol

from c_hypermem.schema import EdgeCluster, EdgeClusterMember, HyperEdge


class EdgeClusterBuilder(Protocol):
    """Builds related EdgeClusters without forcing HyperEdge merges."""

    def build(
        self,
        edges: list[HyperEdge],
        *,
        namespace: str,
        metadata: dict[str, Any],
        current_turn: int,
    ) -> tuple[list[EdgeCluster], list[EdgeClusterMember]]: ...
