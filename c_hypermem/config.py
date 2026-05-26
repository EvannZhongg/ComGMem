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
    retry_attempts: int = 3
    retry_backoff_base_sec: float = 2.0
    retry_backoff_max_sec: float = 60.0


class TokenCountingConfig(BaseModel):
    tokenizer_encoding: str = "cl100k_base"


class NLPConfig(BaseModel):
    model_path: str = "models/en_core_web_sm"


class IngestionConfig(BaseModel):
    context_window_messages: int = 3


class ExtractionConfig(BaseModel):
    prompt: str = "extraction/memory_extraction.md"
    pass_node_labels_to_prompt: bool = True


class IndexingPolicyConfig(BaseModel):
    lexical: bool = True
    vector: bool = True
    alias_index: bool = False
    time_index: bool = False


class TurnConfig(BaseModel):
    enabled: bool = True
    description: str = (
        "Raw conversation turns or message spans that preserve source text, "
        "speaker role, order, and provenance for later evidence tracing."
    )
    indexing: IndexingPolicyConfig = Field(
        default_factory=lambda: IndexingPolicyConfig(lexical=True, vector=True, time_index=True)
    )


class NodeLabelConfig(BaseModel):
    enabled: bool = True
    description: str = ""
    alias_resolution: bool = False
    indexing: IndexingPolicyConfig = Field(default_factory=IndexingPolicyConfig)


class NodeLabelsConfig(BaseModel):
    model_config = ConfigDict(extra="allow")

    def model_post_init(self, __context: Any) -> None:
        extra = self.__pydantic_extra__ or {}
        for key, value in list(extra.items()):
            extra[key] = NodeLabelConfig.model_validate(value)

    @property
    def labels(self) -> dict[str, NodeLabelConfig]:
        return dict(self.__pydantic_extra__ or {})


class NodeSummaryMaintenanceConfig(BaseModel):
    enabled: bool = True
    compact_after_k_sources: int = Field(default=10, ge=1)
    max_tokens: int = Field(default=2048, ge=1)
    prompt: str = "maintenance/node_summary_compaction.md"


class LocalTripleMaintenanceConfig(BaseModel):
    enabled: bool = True
    prompt: str = "maintenance/local_triple_merge.md"


class HyperEdgeDescriptionMaintenanceConfig(BaseModel):
    enabled: bool = True
    compact_after_k_sources: int = Field(default=10, ge=1)
    max_tokens: int = Field(default=2048, ge=1)
    prompt: str = "maintenance/hyper_edge_description_compaction.md"


class MaintenanceConfig(BaseModel):
    node_summary: NodeSummaryMaintenanceConfig = Field(default_factory=NodeSummaryMaintenanceConfig)
    local_triples: LocalTripleMaintenanceConfig = Field(default_factory=LocalTripleMaintenanceConfig)
    hyper_edge_description: HyperEdgeDescriptionMaintenanceConfig = Field(
        default_factory=HyperEdgeDescriptionMaintenanceConfig
    )


class EdgeClustersConfig(BaseModel):
    enabled: bool = True
    stop_nodes: list[str] = Field(default_factory=lambda: ["User", "Assistant"])


class IndexConfig(BaseModel):
    lexical: str = "sqlite_fts"
    vector: str = "qdrant"
    use_embedding: bool = True
    vector_store: VectorStoreConfig = Field(default_factory=VectorStoreConfig)


class RetrievalConfig(BaseModel):
    query_analysis: Literal[False, "llm", "nlp"] = False
    node_rrf_k: int = 60
    edge_rrf_k: int = 60
    lexical_top_k: int = 30
    node_content_vector_top_k: int = 20
    node_local_graph_vector_top_k: int = 20
    hyper_edge_description_vector_top_k: int = 10
    graph_seed_top_k: int = 70
    edge_core_top_k: int = 10
    cluster_periphery_edge_limit: int | None = 20
    cluster_periphery_node_limit: int | None = 50
    edge_coherence_alpha: float = 0.5
    edge_coherence_beta: float = 2.0
    final_top_k: int = 10


class MemoryConfig(BaseModel):
    storage: StorageConfig = Field(default_factory=StorageConfig)
    llm: ModelConfig | None = None
    embedding: ModelConfig | None = None
    token_counting: TokenCountingConfig = Field(default_factory=TokenCountingConfig)
    nlp: NLPConfig = Field(default_factory=NLPConfig)
    ingestion: IngestionConfig = Field(default_factory=IngestionConfig)
    extraction: ExtractionConfig = Field(default_factory=ExtractionConfig)
    node_labels: NodeLabelsConfig = Field(default_factory=NodeLabelsConfig)
    turn: TurnConfig = Field(default_factory=TurnConfig)
    edge_clusters: EdgeClustersConfig = Field(default_factory=EdgeClustersConfig)
    maintenance: MaintenanceConfig = Field(default_factory=MaintenanceConfig)
    index: IndexConfig = Field(default_factory=IndexConfig)
    retrieval: RetrievalConfig = Field(default_factory=RetrievalConfig)
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
