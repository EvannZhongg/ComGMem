from __future__ import annotations

from typing import Protocol

from c_hypermem.schema import (
    EdgeCluster,
    EdgeClusterMember,
    EntityAliasIndexEntry,
    FactPropertyIndexEntry,
    HyperEdge,
    MemoryNode,
)


class MemoryStore(Protocol):
    def reset_namespace(self, namespace: str) -> None: ...

    def upsert_nodes(self, nodes: list[MemoryNode]) -> None: ...

    def upsert_edges(self, edges: list[HyperEdge]) -> None: ...

    def upsert_edge_clusters(self, clusters: list[EdgeCluster]) -> None: ...

    def upsert_edge_cluster_members(self, members: list[EdgeClusterMember]) -> None: ...

    def list_nodes(self, namespace: str) -> list[MemoryNode]: ...

    def list_edges(self, namespace: str) -> list[HyperEdge]: ...

    def list_edge_clusters(self, namespace: str) -> list[EdgeCluster]: ...

    def list_edge_cluster_members(self, namespace: str, cluster_ids: list[str] | None = None) -> list[EdgeClusterMember]: ...

    def get_nodes(self, namespace: str, node_ids: list[str]) -> list[MemoryNode]: ...

    def get_incident_edges(self, namespace: str, node_ids: list[str]) -> list[HyperEdge]: ...

    def get_edge_clusters_for_edges(self, namespace: str, edge_ids: list[str]) -> list[EdgeCluster]: ...

    def upsert_entity_aliases(self, aliases: list[EntityAliasIndexEntry]) -> None: ...

    def find_entity_alias(
        self,
        namespace: str,
        normalized_aliases: list[str],
        entity_type: str | None = None,
    ) -> EntityAliasIndexEntry | None: ...

    def upsert_fact_properties(self, properties: list[FactPropertyIndexEntry]) -> None: ...

    def find_fact_properties(
        self,
        namespace: str,
        property_key: str,
        status: str | None = "active",
    ) -> list[FactPropertyIndexEntry]: ...

    def stats(self, namespace: str) -> dict[str, int]: ...

    def close(self) -> None: ...
