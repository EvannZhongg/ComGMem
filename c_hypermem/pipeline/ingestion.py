from __future__ import annotations

from typing import Any

from c_hypermem.config import MemoryConfig
from c_hypermem.errors import IngestionNotConfiguredError
from c_hypermem.pipeline.assembly import AssemblyContext, GraphAssembler
from c_hypermem.pipeline.edge_cluster_builder import EdgeClusterBuilder
from c_hypermem.pipeline.extraction import ExtractionContext, MemoryExtractor
from c_hypermem.pipeline.hyperedge_builder import HyperEdgeBuilder
from c_hypermem.pipeline.local_graph_builder import LocalGraphBuilder
from c_hypermem.pipeline.maintenance import GraphMaintenance
from c_hypermem.schema import AgentInteraction, IngestionOutput, MemoryImportBatch, Message
from c_hypermem.stores.base import MemoryStore


class IngestionPipeline:
    def __init__(
        self,
        config: MemoryConfig,
        store: MemoryStore,
        *,
        extractor: MemoryExtractor | None = None,
        hyperedge_builder: HyperEdgeBuilder | None = None,
        edge_cluster_builder: EdgeClusterBuilder | None = None,
    ) -> None:
        self.config = config
        self.store = store
        self.extractor = extractor
        self.assembler = GraphAssembler(config, store)
        self.local_graph_builder = LocalGraphBuilder()
        self.hyperedge_builder = hyperedge_builder
        self.edge_cluster_builder = edge_cluster_builder
        self.maintenance = GraphMaintenance()

    def ingest_interaction(
        self,
        interaction: AgentInteraction,
        *,
        namespace: str,
        current_turn: int,
    ) -> IngestionOutput:
        messages: list[Message] = []
        if interaction.user_input:
            messages.append(interaction.user_input)
        if interaction.assistant_output:
            messages.append(interaction.assistant_output)
        for observation in interaction.observations:
            messages.append(
                Message(
                    role=f"observation:{observation.type}",
                    content=observation.content,
                    timestamp=observation.timestamp,
                    metadata=observation.metadata,
                )
            )
        metadata = dict(interaction.metadata)
        if interaction.tool_calls:
            metadata["tool_calls"] = [call.model_dump(mode="json") for call in interaction.tool_calls]
        if interaction.tool_results:
            metadata["tool_results"] = [result.model_dump(mode="json") for result in interaction.tool_results]
        if interaction.attachments:
            metadata["attachments"] = [attachment.model_dump(mode="json") for attachment in interaction.attachments]
        if interaction.trace:
            metadata["trace"] = interaction.trace
        return self._ingest_messages(messages, namespace=namespace, metadata=metadata, current_turn=current_turn)

    def ingest_batch(
        self,
        batch: MemoryImportBatch,
        *,
        namespace: str,
        current_turn: int,
    ) -> IngestionOutput:
        return self._ingest_messages(
            batch.messages,
            namespace=namespace,
            metadata=batch.metadata,
            current_turn=current_turn,
        )

    def _ingest_messages(
        self,
        messages: list[Message],
        *,
        namespace: str,
        metadata: dict[str, Any],
        current_turn: int,
    ) -> IngestionOutput:
        if not messages:
            return IngestionOutput()
        if self.extractor is None:
            raise IngestionNotConfiguredError(
                "No memory extractor is configured. Pass an explicit extractor to Memory(...)."
            )
        context = ExtractionContext(namespace=namespace, metadata=metadata, current_turn=current_turn)
        extraction = self.extractor.extract(messages, context)
        assembly_context = AssemblyContext(namespace=namespace, metadata=metadata, current_turn=current_turn)
        nodes, edges, edge_clusters, edge_cluster_members, entity_aliases, fact_properties = self.assembler.assemble(
            extraction,
            assembly_context,
        )
        nodes = self.local_graph_builder.build(nodes)
        if self.hyperedge_builder is not None:
            edges.extend(
                self.hyperedge_builder.build(
                    nodes,
                    namespace=namespace,
                    metadata=metadata,
                    current_turn=current_turn,
                )
            )
        if self.edge_cluster_builder is not None:
            clusters, members = self.edge_cluster_builder.build(
                edges,
                namespace=namespace,
                metadata=metadata,
                current_turn=current_turn,
            )
            edge_clusters.extend(clusters)
            edge_cluster_members.extend(members)
        nodes, edges, edge_clusters, edge_cluster_members = self.maintenance.apply(
            nodes,
            edges,
            edge_clusters,
            edge_cluster_members,
        )
        return IngestionOutput(
            nodes=nodes,
            edges=edges,
            edge_clusters=edge_clusters,
            edge_cluster_members=edge_cluster_members,
            entity_aliases=entity_aliases,
            fact_properties=fact_properties,
        )
