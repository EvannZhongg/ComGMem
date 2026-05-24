from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field

from c_hypermem.errors import ConfigError


class StorageConfig(BaseModel):
    backend: str = "sqlite"
    path: str = "runs/c_hypermem/memory.sqlite3"


class VectorStoreConfig(BaseModel):
    backend: str = "qdrant"
    path: str = "runs/c_hypermem/vector_index"
    collection_name: str = "c_hypermem_memory"


class ModelConfig(BaseModel):
    provider: str = "openai_compatible"
    model: str
    base_url: str | None = None
    api_key: str | None = None
    batch_size: int = 10


class IngestionConfig(BaseModel):
    event_mode: str = "interaction"
    context_window_messages: int = 3
    max_facts_per_event: int = 12
    extractor: str | None = None


class ExtractionConfig(BaseModel):
    prompt: str = "extraction/memory_extraction.md"
    output_schema: str = "minimal_memory_candidates"
    forbid_model_ids: bool = True
    forbid_confidence: bool = True
    pass_node_labels_to_prompt: bool = True
    allow_unknown_node_labels: bool = True


class NodeIdentityDisambiguationConfig(BaseModel):
    enabled: bool = True
    hint_sources: list[str] = Field(default_factory=lambda: ["aliases", "local_graph", "source_scope", "metadata"])


class NodeIdentityConfig(BaseModel):
    strategy: str = "canonical_fingerprint"
    include_namespace: bool = True
    include_node_labels: bool = False
    disambiguation: NodeIdentityDisambiguationConfig = Field(default_factory=NodeIdentityDisambiguationConfig)


class LocalGraphPolicyConfig(BaseModel):
    enabled: bool = True
    allow_triples: bool = True
    allow_attributes: bool = True
    allow_roles: bool = True


class IndexingPolicyConfig(BaseModel):
    lexical: bool = True
    vector: bool = True
    alias_index: bool = False
    time_index: bool = False


class TimePolicyConfig(BaseModel):
    prefer_world_time: bool = False


class NodeLabelConfig(BaseModel):
    enabled: bool = True
    description: str = ""
    alias_resolution: bool = False
    property_index: bool = False
    local_graph: LocalGraphPolicyConfig = Field(default_factory=LocalGraphPolicyConfig)
    indexing: IndexingPolicyConfig = Field(default_factory=IndexingPolicyConfig)
    time: TimePolicyConfig = Field(default_factory=TimePolicyConfig)


class NodeLabelsConfig(BaseModel):
    model_config = ConfigDict(extra="allow")

    default_policy: NodeLabelConfig = Field(default_factory=NodeLabelConfig)

    def model_post_init(self, __context: Any) -> None:
        extra = self.__pydantic_extra__ or {}
        for key, value in list(extra.items()):
            extra[key] = NodeLabelConfig.model_validate(value)

    @property
    def labels(self) -> dict[str, NodeLabelConfig]:
        return dict(self.__pydantic_extra__ or {})


class HyperEdgeResolutionConfig(BaseModel):
    use_member_overlap_as_recall_signal: bool = True
    require_relation_role_polarity_compatibility_for_merge: bool = True


class HyperEdgesConfig(BaseModel):
    enabled: bool = True
    build_from_extraction: bool = True
    merge_policy: str = "conservative"
    member_policy_default: str = "appendable"
    basic_edge_types: list[str] = Field(default_factory=lambda: ["evidence", "state", "correction"])
    resolution: HyperEdgeResolutionConfig = Field(default_factory=HyperEdgeResolutionConfig)


class EdgeClusterPromptsConfig(BaseModel):
    fact_merge: str = "maintenance/fact_merge.md"
    contradiction_check: str = "maintenance/contradiction_check.md"
    edge_merge: str = "maintenance/edge_merge.md"
    edge_cluster_merge: str = "maintenance/edge_cluster_merge.md"
    edge_conflict_check: str = "maintenance/edge_conflict_check.md"


class BackgroundClusterMaintenanceConfig(BaseModel):
    enabled: bool = False
    trigger_every_k_writes: int = 100


class EdgeClustersConfig(BaseModel):
    enabled: bool = True
    create_from_related_hyperedges: bool = True
    allow_conflict_clusters: bool = True
    description_variants_limit: int = 8
    maintenance_prompts: EdgeClusterPromptsConfig = Field(default_factory=EdgeClusterPromptsConfig)
    background_maintenance: BackgroundClusterMaintenanceConfig = Field(
        default_factory=BackgroundClusterMaintenanceConfig
    )


class LocalGraphConfig(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    enabled: bool = True
    schema_name: str = Field(default="uniform", alias="schema")
    configured_by_node_labels: bool = True


class RelativeDecayConfig(BaseModel):
    enabled: bool = True
    unit: str = "turn"
    decay_lambda: float = 0.03
    access_boost: float = 0.05


class TimeConfig(BaseModel):
    relative_decay: RelativeDecayConfig = Field(default_factory=RelativeDecayConfig)


class IndexConfig(BaseModel):
    lexical: str = "sqlite_fts"
    vector: str = "qdrant"
    use_embedding: bool = True
    vector_store: VectorStoreConfig = Field(default_factory=VectorStoreConfig)


class RetrievalConfig(BaseModel):
    query_analysis: Literal[False, "llm", "nlp"] = False
    lexical_top_n: int = 30
    vector_top_n: int = 30
    edge_top_n: int = 30
    rerank_top_n: int = 12
    use_hyperedge_expansion: bool = True
    use_edge_cluster_expansion: bool = False
    use_temporal_filter: bool = True
    use_recency_decay: bool = True
    recency_decay_lambda: float = 0.03
    access_boost: float = 0.05


class MemoryConfig(BaseModel):
    storage: StorageConfig = Field(default_factory=StorageConfig)
    llm: ModelConfig | None = None
    embedding: ModelConfig | None = None
    ingestion: IngestionConfig = Field(default_factory=IngestionConfig)
    extraction: ExtractionConfig = Field(default_factory=ExtractionConfig)
    node_identity: NodeIdentityConfig = Field(default_factory=NodeIdentityConfig)
    node_labels: NodeLabelsConfig = Field(default_factory=NodeLabelsConfig)
    hyperedges: HyperEdgesConfig = Field(default_factory=HyperEdgesConfig)
    edge_clusters: EdgeClustersConfig = Field(default_factory=EdgeClustersConfig)
    local_graph: LocalGraphConfig = Field(default_factory=LocalGraphConfig)
    time: TimeConfig = Field(default_factory=TimeConfig)
    index: IndexConfig = Field(default_factory=IndexConfig)
    retrieval: RetrievalConfig = Field(default_factory=RetrievalConfig)
    default_top_k: int = 10
    prompt_version: str = "0.1.0"
    metadata: dict[str, Any] = Field(default_factory=dict)

    @classmethod
    def load(cls, config: str | Path | dict[str, Any] | None = None) -> "MemoryConfig":
        if config is None:
            return cls()

        if isinstance(config, cls):
            return config

        if isinstance(config, dict):
            raw = dict(config)
            if "include" in raw:
                _load_project_dotenv(Path.cwd())
                raw = _load_includes(raw, Path.cwd())
            return cls.model_validate(_resolve_env_placeholders(_normalize_external_config(raw)))

        path = Path(config)
        if not path.exists():
            raise ConfigError(f"Config file does not exist: {path}")

        try:
            raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except yaml.YAMLError as exc:
            raise ConfigError(f"Invalid YAML config: {path}") from exc

        if not isinstance(raw, dict):
            raise ConfigError(f"Config must be a mapping: {path}")
        _load_project_dotenv(path)
        raw = _load_includes(raw, path.parent)
        return cls.model_validate(_resolve_env_placeholders(_normalize_external_config(raw)))

    def stable_dict(self) -> dict[str, Any]:
        return self.model_dump(mode="json", exclude={"metadata"})


def _normalize_external_config(raw: dict[str, Any]) -> dict[str, Any]:
    data = dict(raw)
    data.pop("include", None)
    return data


def _load_includes(raw: dict[str, Any], base_dir: Path) -> dict[str, Any]:
    include_values = raw.get("include", [])
    if isinstance(include_values, (str, Path)):
        include_paths = [include_values]
    else:
        include_paths = list(include_values or [])

    merged: dict[str, Any] = {}
    for include_path in include_paths:
        path = (base_dir / Path(include_path)).resolve()
        if not path.exists():
            raise ConfigError(f"Included config file does not exist: {path}")
        try:
            included = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except yaml.YAMLError as exc:
            raise ConfigError(f"Invalid YAML config: {path}") from exc
        if not isinstance(included, dict):
            raise ConfigError(f"Included config must be a mapping: {path}")
        merged = _deep_merge(merged, _load_includes(included, path.parent))
    return _deep_merge(merged, dict(raw))


def _resolve_env_placeholders(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _resolve_env_placeholders(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_resolve_env_placeholders(item) for item in value]
    if isinstance(value, str) and value.startswith("${") and value.endswith("}"):
        import os

        return os.getenv(value[2:-1], value)
    return value


def _load_project_dotenv(path: Path) -> None:
    import os

    root = _find_project_root(path)
    env_path = root / ".env"
    if not env_path.exists():
        return
    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, raw_value = line.split("=", 1)
        key = key.strip()
        value = raw_value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


def _find_project_root(path: Path) -> Path:
    start = path if path.is_dir() else path.parent
    for candidate in [start, *start.parents]:
        if (candidate / "pyproject.toml").exists() and (candidate / "c_hypermem").exists():
            return candidate
    return start


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    result = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = value
    return result
