from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


NodeLabel = str
MemoryStatus = Literal["active", "retired", "invalidated", "uncertain"]
ConflictState = Literal["none", "contains_conflict", "needs_review"]

_EXTRA_FORBID = ConfigDict(extra="forbid")


class ValidTime(BaseModel):
    start: str | None = None
    end: str | None = None
    as_of: str | None = None


class WorldTime(BaseModel):
    event_time: str | None = None
    valid_time: ValidTime | None = None
    source_timestamp: str | None = None


class LifecycleTime(BaseModel):
    created_at: str | None = None
    inserted_at: str | None = None
    updated_at: str | None = None
    deleted_at: str | None = None


class ActivationTime(BaseModel):
    created_turn: int | None = None
    inserted_turn: int | None = None
    updated_turn: int | None = None
    last_access_turn: int | None = None
    access_count: int = 0


class TimeBundle(BaseModel):
    world: WorldTime = Field(default_factory=WorldTime)
    lifecycle: LifecycleTime = Field(default_factory=LifecycleTime)
    activation: ActivationTime = Field(default_factory=ActivationTime)


class LocalTriple(BaseModel):
    triple_id: str | None = None
    subject: str
    predicate: str
    object: str
    status: MemoryStatus = "active"
    scope_edge_id: str | None = None
    scope_cluster_id: str | None = None
    superseded_by: str | None = None
    invalidated_by: str | None = None
    qualifiers: dict[str, Any] = Field(default_factory=dict)


class LocalNodeGraph(BaseModel):
    triples: list[LocalTriple] = Field(default_factory=list)


class ExtractedTriple(BaseModel):
    model_config = _EXTRA_FORBID

    subject: str
    predicate: str
    object: str
    qualifiers: dict[str, Any] = Field(default_factory=dict)


class ExtractedNode(BaseModel):
    model_config = _EXTRA_FORBID

    ref: str
    labels: list[str] = Field(default_factory=list)
    canonical_text: str
    summaries: list[str] = Field(default_factory=list)
    triples: list[ExtractedTriple] = Field(default_factory=list)
    edge_summary_refs: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_extracted_node(self) -> "ExtractedNode":
        self.ref = self.ref.strip()
        self.canonical_text = self.canonical_text.strip()
        self.labels = _unique_non_empty_strings(self.labels)
        self.summaries = _unique_non_empty_strings(self.summaries)
        self.edge_summary_refs = _unique_non_empty_strings(self.edge_summary_refs)
        if not self.ref:
            raise ValueError("ExtractedNode.ref is required.")
        if not self.canonical_text:
            raise ValueError("ExtractedNode.canonical_text is required.")
        return self


class ExtractedEdgeSummary(BaseModel):
    model_config = _EXTRA_FORBID

    ref: str
    description: str
    metadata: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_edge_summary(self) -> "ExtractedEdgeSummary":
        self.ref = self.ref.strip()
        self.description = self.description.strip()
        if not self.ref:
            raise ValueError("ExtractedEdgeSummary.ref is required.")
        if not self.description:
            raise ValueError("ExtractedEdgeSummary.description is required.")
        return self


class MemoryExtraction(BaseModel):
    model_config = _EXTRA_FORBID

    nodes: list[ExtractedNode] = Field(default_factory=list)
    edge_summaries: list[ExtractedEdgeSummary] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_extraction_refs(self) -> "MemoryExtraction":
        node_refs = [node.ref for node in self.nodes]
        edge_refs = [edge.ref for edge in self.edge_summaries]
        duplicate_node_refs = _duplicates(node_refs)
        duplicate_edge_refs = _duplicates(edge_refs)
        if duplicate_node_refs:
            raise ValueError(f"Duplicate ExtractedNode refs: {', '.join(duplicate_node_refs)}")
        if duplicate_edge_refs:
            raise ValueError(f"Duplicate ExtractedEdgeSummary refs: {', '.join(duplicate_edge_refs)}")

        edge_ref_set = set(edge_refs)
        missing_refs = sorted(
            {
                edge_ref
                for node in self.nodes
                for edge_ref in node.edge_summary_refs
                if edge_ref not in edge_ref_set
            }
        )
        if missing_refs:
            raise ValueError(f"Unknown edge_summary_refs: {', '.join(missing_refs)}")
        return self


class MemoryNode(BaseModel):
    node_id: str
    namespace: str
    canonical_text: str
    normalized_text: str
    fingerprint: str
    node_labels: list[NodeLabel] = Field(default_factory=list)
    status: MemoryStatus = "active"
    superseded_by: str | None = None
    invalidated_by: str | None = None
    status_reason: str | None = None
    status_updated_at: str | None = None
    content: str
    summary: str = ""
    attributes: dict[str, Any] = Field(default_factory=dict)
    metadata: dict[str, Any] = Field(default_factory=dict)
    time: TimeBundle = Field(default_factory=TimeBundle)
    local_graph: LocalNodeGraph = Field(default_factory=LocalNodeGraph)


class HyperEdge(BaseModel):
    edge_id: str
    namespace: str
    edge_fingerprint: str
    description: str = ""
    status: MemoryStatus = "active"
    member_signature: str = ""
    member_version: int = 1
    node_ids: list[str]
    weights: dict[str, float] = Field(default_factory=dict)
    metadata: dict[str, Any] = Field(default_factory=dict)
    time: TimeBundle = Field(default_factory=TimeBundle)


class EdgeDescriptionVariant(BaseModel):
    text: str
    source_edge_id: str | None = None


class EdgeCluster(BaseModel):
    cluster_id: str
    namespace: str
    cluster_fingerprint: str
    canonical_description: str
    cluster_labels: list[str] = Field(default_factory=list)
    aliases: list[str] = Field(default_factory=list)
    conflict_state: ConflictState = "none"
    description_variants: list[EdgeDescriptionVariant] = Field(default_factory=list)
    status: MemoryStatus = "active"
    metadata: dict[str, Any] = Field(default_factory=dict)


class EdgeClusterMember(BaseModel):
    namespace: str
    cluster_id: str
    edge_id: str
    status: MemoryStatus = "active"
    metadata: dict[str, Any] = Field(default_factory=dict)


class EntityAliasIndexEntry(BaseModel):
    namespace: str
    normalized_alias: str
    entity_type: str | None = None
    node_id: str
    source_count: int = 1
    updated_at: str | None = None


class Message(BaseModel):
    role: str
    content: str
    timestamp: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class ToolCall(BaseModel):
    id: str | None = None
    name: str
    arguments: dict[str, Any] = Field(default_factory=dict)
    timestamp: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class ToolResult(BaseModel):
    tool_call_id: str | None = None
    content: str = ""
    status: str | None = None
    timestamp: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class Observation(BaseModel):
    type: str = "observation"
    content: str
    timestamp: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class Attachment(BaseModel):
    type: str
    uri: str
    metadata: dict[str, Any] = Field(default_factory=dict)


class AgentInteraction(BaseModel):
    type: Literal["agent_interaction"] = "agent_interaction"
    user_input: Message | None = None
    assistant_output: Message | None = None
    tool_calls: list[ToolCall] = Field(default_factory=list)
    tool_results: list[ToolResult] = Field(default_factory=list)
    observations: list[Observation] = Field(default_factory=list)
    attachments: list[Attachment] = Field(default_factory=list)
    trace: dict[str, Any] = Field(default_factory=dict)
    metadata: dict[str, Any] = Field(default_factory=dict)


class MemoryImportBatch(BaseModel):
    type: Literal["memory_import_batch"] = "memory_import_batch"
    messages: list[Message]
    metadata: dict[str, Any] = Field(default_factory=dict)


class IngestionOutput(BaseModel):
    nodes: list[MemoryNode] = Field(default_factory=list)
    retired_nodes: list[MemoryNode] = Field(default_factory=list)
    edges: list[HyperEdge] = Field(default_factory=list)
    edge_clusters: list[EdgeCluster] = Field(default_factory=list)
    edge_cluster_members: list[EdgeClusterMember] = Field(default_factory=list)
    entity_aliases: list[EntityAliasIndexEntry] = Field(default_factory=list)


class SearchResult(BaseModel):
    id: str
    content: str
    score: float
    metadata: dict[str, Any] = Field(default_factory=dict)


def _unique_non_empty_strings(values: list[str]) -> list[str]:
    return list(dict.fromkeys(str(value).strip() for value in values if str(value).strip()))


def _duplicates(values: list[str]) -> list[str]:
    seen: set[str] = set()
    duplicates: list[str] = []
    for value in values:
        if value in seen and value not in duplicates:
            duplicates.append(value)
        seen.add(value)
    return duplicates
