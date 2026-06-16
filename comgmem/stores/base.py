from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from comgmem.schema import (
    EdgeCluster,
    EdgeClusterMember,
    EntityAliasIndexEntry,
    HyperEdge,
    Message,
    MemoryNode,
)


@dataclass(frozen=True)
class TripleEndpointRecord:
    triple_id: str
    owner_node_id: str
    edge_id: str
    subject: str
    object: str


class MemoryStore(Protocol):
    def reset_namespace(self, namespace: str) -> None: ...

    def upsert_nodes(self, nodes: list[MemoryNode]) -> None: ...

    def upsert_edges(self, edges: list[HyperEdge]) -> None: ...

    def upsert_edge_clusters(self, clusters: list[EdgeCluster]) -> None: ...

    def upsert_edge_cluster_members(self, members: list[EdgeClusterMember]) -> None: ...

    def append_turn(
        self,
        namespace: str,
        turn_id: str,
        turn_index: int,
        messages: list[Message],
        metadata: dict,
    ) -> None: ...

    def list_recent_turn_messages(self, namespace: str, limit: int) -> list[Message]: ...

    def list_turn_messages(self, namespace: str, turn_ids: list[str]) -> list[Message]: ...

    def turn_inserted_at_by_id(self, namespace: str, turn_ids: list[str]) -> dict[str, str]: ...

    def next_turn_index(self, namespace: str) -> int: ...

    def list_nodes(self, namespace: str) -> list[MemoryNode]: ...

    def list_edges(self, namespace: str) -> list[HyperEdge]: ...

    def get_edges(self, namespace: str, edge_ids: list[str]) -> list[HyperEdge]: ...

    def list_edge_clusters(self, namespace: str) -> list[EdgeCluster]: ...

    def list_edge_cluster_members(self, namespace: str, cluster_ids: list[str] | None = None) -> list[EdgeClusterMember]: ...

    def find_edge_cluster_by_fingerprint(self, namespace: str, cluster_fingerprint: str) -> EdgeCluster | None: ...

    def get_nodes(self, namespace: str, node_ids: list[str]) -> list[MemoryNode]: ...

    def search_nodes_fts(self, namespace: str, query: str, top_k: int) -> list[tuple[MemoryNode, float]]: ...

    def get_incident_edges(self, namespace: str, node_ids: list[str]) -> list[HyperEdge]: ...

    def get_edge_clusters_for_edges(self, namespace: str, edge_ids: list[str]) -> list[EdgeCluster]: ...

    def find_triples_by_endpoints(self, namespace: str, endpoint_values: list[str]) -> list[TripleEndpointRecord]: ...

    def upsert_entity_aliases(self, aliases: list[EntityAliasIndexEntry]) -> None: ...

    def find_entity_alias(
        self,
        namespace: str,
        normalized_aliases: list[str],
        entity_type: str | None = None,
    ) -> EntityAliasIndexEntry | None: ...

    def stats(self, namespace: str) -> dict[str, int]: ...

    def close(self) -> None: ...
