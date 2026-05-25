from __future__ import annotations

from typing import Any

from c_hypermem.config import MemoryConfig
from c_hypermem.llms.base import LLMClient
from c_hypermem.pipeline.context import AssemblyContext
from c_hypermem.pipeline.edge_cluster_builder import BasicEdgeClusterBuilder
from c_hypermem.pipeline.graph_utils import (
    dedupe_cluster_members,
    dedupe_clusters,
    dedupe_edges,
    merge_node,
    string_list,
)
from c_hypermem.pipeline.hyperedge_builder import BasicHyperEdgeBuilder
from c_hypermem.pipeline.node_builder import NodeBuilder
from c_hypermem.schema import (
    EdgeCluster,
    EdgeClusterMember,
    EntityAliasIndexEntry,
    HyperEdge,
    MemoryExtraction,
    MemoryNode,
)
from c_hypermem.stores.base import MemoryStore
from c_hypermem.utils.text import normalize_text
from c_hypermem.utils.time import utc_now_iso


class GraphAssembler:
    """Coordinate extracted homogeneous nodes into graph components."""

    def __init__(self, config: MemoryConfig, store: MemoryStore, *, maintenance_llm: LLMClient | None = None) -> None:
        self.config = config
        self.store = store
        self.node_builder = NodeBuilder()
        self.hyperedge_builder = BasicHyperEdgeBuilder(config)
        self.edge_cluster_builder = BasicEdgeClusterBuilder(config, store)
        self.maintenance_llm = maintenance_llm

    def assemble(self, extraction: MemoryExtraction, context: AssemblyContext) -> tuple[
        list[MemoryNode],
        list[MemoryNode],
        list[HyperEdge],
        list[EdgeCluster],
        list[EdgeClusterMember],
        list[EntityAliasIndexEntry],
    ]:
        nodes_by_id: dict[str, MemoryNode] = {}
        ref_to_node_id: dict[str, str] = {}

        for extracted_node in extraction.nodes:
            node = self.node_builder.build_node(extracted_node, context)
            existing = nodes_by_id.get(node.node_id) or self._existing_entity_node(extracted_node, context)
            merged = self._merge_node(existing, node, context)
            nodes_by_id[merged.node_id] = merged
            ref_to_node_id[extracted_node.ref] = merged.node_id

        edges = self._build_edges(extraction, ref_to_node_id, nodes_by_id, context)

        clusters: list[EdgeCluster] = []
        cluster_members: list[EdgeClusterMember] = []
        if self.config.edge_clusters.enabled:
            clusters, cluster_members = self.edge_cluster_builder.build(
                edges,
                namespace=context.namespace,
                metadata=context.metadata,
                current_turn=context.current_turn,
            )

        alias_entries = self._alias_entries(context.namespace, list(nodes_by_id.values()))
        return (
            list(nodes_by_id.values()),
            [],
            dedupe_edges(edges),
            dedupe_clusters(clusters),
            dedupe_cluster_members(cluster_members),
            alias_entries,
        )

    def _build_edges(
        self,
        extraction: MemoryExtraction,
        ref_to_node_id: dict[str, str],
        nodes_by_id: dict[str, MemoryNode],
        context: AssemblyContext,
    ) -> list[HyperEdge]:
        edge_ref_members: dict[str, list[MemoryNode]] = {edge.ref: [] for edge in extraction.edge_summaries}
        for extracted_node in extraction.nodes:
            node_id = ref_to_node_id[extracted_node.ref]
            node = nodes_by_id[node_id]
            for edge_ref in extracted_node.edge_summary_refs:
                edge_ref_members[edge_ref].append(node)

        edges: list[HyperEdge] = []
        for edge_summary in extraction.edge_summaries:
            members = edge_ref_members.get(edge_summary.ref, [])
            if not members:
                continue
            edges.append(self.hyperedge_builder.build_from_summary(edge_summary, members, context))
        return edges

    def _merge_node(self, existing: MemoryNode | None, incoming: MemoryNode, context: AssemblyContext) -> MemoryNode:
        if existing is None:
            stored = self.store.get_nodes(context.namespace, [incoming.node_id])
            existing = stored[0] if stored else None
        return merge_node(existing, incoming, context)

    def _alias_entries(self, namespace: str, nodes: list[MemoryNode]) -> list[EntityAliasIndexEntry]:
        entries: list[EntityAliasIndexEntry] = []
        for node in nodes:
            if "entity" not in node.node_labels:
                continue
            aliases = list(dict.fromkeys([node.canonical_text, *string_list(node.attributes.get("aliases"))]))
            entity_type = node.attributes.get("entity_type")
            entity_type = entity_type if isinstance(entity_type, str) else None
            for alias in aliases:
                normalized = normalize_text(alias)
                if not normalized:
                    continue
                entries.append(
                    EntityAliasIndexEntry(
                        namespace=namespace,
                        normalized_alias=normalized,
                        entity_type=entity_type,
                        node_id=node.node_id,
                        updated_at=utc_now_iso(),
                    )
                )
        return entries

    def _existing_entity_node(self, extracted_node: Any, context: AssemblyContext) -> MemoryNode | None:
        if "entity" not in extracted_node.labels:
            return None
        aliases = _entity_aliases(extracted_node)
        normalized_aliases = [normalize_text(alias) for alias in aliases if normalize_text(alias)]
        entity_type = extracted_node.metadata.get("entity_type") or extracted_node.metadata.get("type")
        entity_type = entity_type if isinstance(entity_type, str) else None
        existing_alias = self.store.find_entity_alias(context.namespace, normalized_aliases, entity_type)
        if existing_alias is None:
            return None
        nodes = self.store.get_nodes(context.namespace, [existing_alias.node_id])
        return nodes[0] if nodes else None


def _entity_aliases(extracted_node: Any) -> list[str]:
    aliases = [extracted_node.canonical_text]
    metadata_aliases = extracted_node.metadata.get("aliases")
    if isinstance(metadata_aliases, list):
        aliases.extend(str(alias) for alias in metadata_aliases)
    return list(dict.fromkeys(alias.strip() for alias in aliases if alias.strip()))
