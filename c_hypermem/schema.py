from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


NodeLabel = str
MemoryStatus = Literal["active", "retired", "invalidated", "uncertain"]
ConflictState = Literal["none", "contains_conflict", "needs_review"]
MemberPolicy = Literal["immutable", "appendable", "versioned"]
Polarity = Literal["positive", "negative", "neutral", "unknown"]


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
    role_in_edge: str | None = None
    edge_relation: str | None = None
    superseded_by: str | None = None
    invalidated_by: str | None = None
    qualifiers: dict[str, Any] = Field(default_factory=dict)


class LocalNodeGraph(BaseModel):
    triples: list[LocalTriple] = Field(default_factory=list)
    attributes: dict[str, Any] = Field(default_factory=dict)
    roles: dict[str, str] = Field(default_factory=dict)


class ExtractedSource(BaseModel):
    text: str = ""
    ref: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class ExtractedEntity(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    name: str
    entity_type: str | None = Field(default=None, alias="type")
    labels: list[str] = Field(default_factory=list)
    aliases: list[str] = Field(default_factory=list)
    source_ref: str | None = None
    attributes: dict[str, Any] = Field(default_factory=dict)


class EventParticipant(BaseModel):
    name: str
    role: str | None = None


class ExtractedEvent(BaseModel):
    summary: str
    time: str | None = None
    participants: list[EventParticipant] = Field(default_factory=list)
    labels: list[str] = Field(default_factory=list)
    source_ref: str | None = None
    attributes: dict[str, Any] = Field(default_factory=dict)


class ExtractedAssertion(BaseModel):
    subject: str
    predicate: str
    object: str
    source_ref: str | None = None
    polarity: Polarity = "positive"
    time: str | None = None
    labels: list[str] = Field(default_factory=list)
    attributes: dict[str, Any] = Field(default_factory=dict)


class MemoryExtraction(BaseModel):
    entities: list[ExtractedEntity] = Field(default_factory=list)
    events: list[ExtractedEvent] = Field(default_factory=list)
    assertions: list[ExtractedAssertion] = Field(default_factory=list)
    sources: list[ExtractedSource] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)


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
    edge_type: str
    relation: str
    description: str = ""
    polarity: Polarity = "unknown"
    status: MemoryStatus = "active"
    member_policy: MemberPolicy = "immutable"
    member_signature: str = ""
    member_version: int = 1
    node_ids: list[str]
    roles: dict[str, str] = Field(default_factory=dict)
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
    relation_to_cluster: str = "supports"
    status: MemoryStatus = "active"
    metadata: dict[str, Any] = Field(default_factory=dict)


class EntityAliasIndexEntry(BaseModel):
    namespace: str
    normalized_alias: str
    entity_type: str | None = None
    node_id: str
    source_count: int = 1
    updated_at: str | None = None


class FactPropertyIndexEntry(BaseModel):
    namespace: str
    property_key: str
    subject_node_id: str | None = None
    predicate: str
    fact_node_id: str
    status: MemoryStatus = "active"
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
    edges: list[HyperEdge] = Field(default_factory=list)
    edge_clusters: list[EdgeCluster] = Field(default_factory=list)
    edge_cluster_members: list[EdgeClusterMember] = Field(default_factory=list)
    entity_aliases: list[EntityAliasIndexEntry] = Field(default_factory=list)
    fact_properties: list[FactPropertyIndexEntry] = Field(default_factory=list)


class SearchResult(BaseModel):
    id: str
    content: str
    score: float
    metadata: dict[str, Any] = Field(default_factory=dict)
