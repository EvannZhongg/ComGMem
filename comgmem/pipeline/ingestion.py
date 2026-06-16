from __future__ import annotations

from typing import Any

from comgmem.config import MemoryConfig
from comgmem.errors import IngestionNotConfiguredError
from comgmem.llms.base import LLMClient
from comgmem.llms.openai_compatible import OpenAICompatibleLLM
from comgmem.pipeline.assembly import GraphAssembler
from comgmem.pipeline.context import AssemblyContext
from comgmem.pipeline.edge_cluster_builder import EdgeClusterBuilder
from comgmem.pipeline.extraction import ExtractionContext, ExtractionWindow, MemoryExtractor
from comgmem.pipeline.hyperedge_builder import HyperEdgeBuilder
from comgmem.pipeline.local_graph_builder import LocalGraphBuilder
from comgmem.schema import AgentInteraction, IngestionOutput, MemoryImportBatch, Message
from comgmem.stores.base import MemoryStore


class IngestionPipeline:
    def __init__(
        self,
        config: MemoryConfig,
        store: MemoryStore,
        *,
        extractor: MemoryExtractor | None = None,
        hyperedge_builder: HyperEdgeBuilder | None = None,
        edge_cluster_builder: EdgeClusterBuilder | None = None,
        maintenance_llm: LLMClient | None = None,
    ) -> None:
        self.config = config
        self.store = store
        self.extractor = extractor
        if maintenance_llm is None and config.llm is not None:
            maintenance_llm = OpenAICompatibleLLM(config.llm)
        self.assembler = GraphAssembler(config, store, maintenance_llm=maintenance_llm)
        self.local_graph_builder = LocalGraphBuilder()
        self.hyperedge_builder = hyperedge_builder
        self.edge_cluster_builder = edge_cluster_builder

    def ingest_interaction(
        self,
        interaction: AgentInteraction,
        *,
        namespace: str,
        current_turn: int,
        recent_messages: list[Message] | None = None,
    ) -> IngestionOutput:
        return self._ingest_messages(
            interaction_messages(interaction),
            recent_messages=recent_messages,
            namespace=namespace,
            metadata=interaction_metadata(interaction),
            current_turn=current_turn,
        )

    def ingest_batch(
        self,
        batch: MemoryImportBatch,
        *,
        namespace: str,
        current_turn: int,
        recent_messages: list[Message] | None = None,
    ) -> IngestionOutput:
        return self._ingest_messages(
            batch.messages,
            recent_messages=recent_messages,
            namespace=namespace,
            metadata=batch.metadata,
            current_turn=current_turn,
        )

    def _ingest_messages(
        self,
        messages: list[Message],
        *,
        recent_messages: list[Message] | None,
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
        window = ExtractionWindow(context=list(recent_messages or []), target=messages)
        extraction = self.extractor.extract(window, context)
        assembly_context = AssemblyContext(namespace=namespace, metadata=metadata, current_turn=current_turn)
        nodes, retired_nodes, edges, edge_clusters, edge_cluster_members, entity_aliases = (
            self.assembler.assemble(
                extraction,
                assembly_context,
            )
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
                nodes=nodes,
                namespace=namespace,
                metadata=metadata,
                current_turn=current_turn,
            )
            edge_clusters.extend(clusters)
            edge_cluster_members.extend(members)
        return IngestionOutput(
            nodes=nodes,
            retired_nodes=retired_nodes,
            edges=edges,
            edge_clusters=edge_clusters,
            edge_cluster_members=edge_cluster_members,
            entity_aliases=entity_aliases,
        )


def interaction_messages(interaction: AgentInteraction) -> list[Message]:
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
    return messages


def interaction_metadata(interaction: AgentInteraction) -> dict[str, Any]:
    metadata = dict(interaction.metadata)
    if interaction.tool_calls:
        metadata["tool_calls"] = [call.model_dump(mode="json") for call in interaction.tool_calls]
    if interaction.tool_results:
        metadata["tool_results"] = [result.model_dump(mode="json") for result in interaction.tool_results]
    if interaction.attachments:
        metadata["attachments"] = [attachment.model_dump(mode="json") for attachment in interaction.attachments]
    if interaction.trace:
        metadata["trace"] = interaction.trace
    return metadata
