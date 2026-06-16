from __future__ import annotations

import sys
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from comgmem.config import ModelConfig, NLPConfig, RetrievalConfig
from comgmem.errors import ConfigError
from comgmem.llms.base import LLMClient
from comgmem.llms.retrying import generate_json_with_parse_retries
from comgmem.utils.prompts import PromptRegistry
from comgmem.utils.text import normalize_text


@dataclass(frozen=True)
class QueryAnalysis:
    query: str
    mode: str
    normalized_query: str = ""
    bm25_query: str = ""
    entities: list[dict[str, str]] = field(default_factory=list)
    attributes: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def disabled(cls, query: str) -> "QueryAnalysis":
        return cls(query=query, mode="false")

    def to_metadata(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "normalized_query": self.normalized_query,
            "bm25_query": self.bm25_query,
            "entities": self.entities,
            "attributes": self.attributes,
        }


class QueryAnalyzer(Protocol):
    def analyze(self, query: str) -> QueryAnalysis: ...


def build_query_analyzer(
    config: RetrievalConfig,
    *,
    nlp_config: NLPConfig | None = None,
    llm: LLMClient | None = None,
    llm_config: ModelConfig | None = None,
    prompt_registry: PromptRegistry | None = None,
) -> QueryAnalyzer:
    if config.query_analysis is False:
        return DisabledQueryAnalyzer()
    if config.query_analysis == "nlp":
        return SpacyQueryAnalyzer(nlp_config or NLPConfig())
    if config.query_analysis == "llm":
        if llm is None:
            raise ConfigError("retrieval.query_analysis='llm' requires an LLM client or config.llm.")
        return LLMQueryAnalyzer(llm=llm, llm_config=llm_config, prompt_registry=prompt_registry)
    raise ConfigError(f"Unsupported retrieval.query_analysis mode: {config.query_analysis!r}")


class DisabledQueryAnalyzer:
    def analyze(self, query: str) -> QueryAnalysis:
        return QueryAnalysis.disabled(query)


class LLMQueryAnalyzer:
    def __init__(
        self,
        *,
        llm: LLMClient,
        llm_config: ModelConfig | None = None,
        prompt_registry: PromptRegistry | None = None,
    ) -> None:
        self.llm = llm
        self.llm_config = llm_config
        self.prompt_registry = prompt_registry or PromptRegistry()

    def analyze(self, query: str) -> QueryAnalysis:
        prompt = self._render_prompt(query)
        return generate_json_with_parse_retries(
            self.llm,
            prompt,
            lambda payload: _analysis_from_payload(query, "llm", payload),
            config=self.llm_config,
        )

    def _render_prompt(self, query: str) -> str:
        prompt = self.prompt_registry.load("retrieval.query_analysis")
        return prompt.text.replace("{{QUERY}}", query)


class SpacyQueryAnalyzer:
    def __init__(self, config: NLPConfig) -> None:
        self.config = config
        self._nlp_lemma: Any | None = None
        self._lock = threading.Lock()

    def analyze(self, query: str) -> QueryAnalysis:
        return QueryAnalysis(
            query=query,
            mode="nlp",
            normalized_query=normalize_text(query),
            bm25_query=self._lemmatize_for_bm25(query),
            entities=[],
        )

    def _load_lemma(self) -> Any:
        if self._nlp_lemma is not None:
            return self._nlp_lemma
        with self._lock:
            if self._nlp_lemma is None:
                self._nlp_lemma = _load_spacy_model(self.config.model_path, disable=["ner", "parser"])
        return self._nlp_lemma

    def _lemmatize_for_bm25(self, text: str) -> str:
        doc = self._load_lemma()(text.lower())
        tokens: list[str] = []
        for token in doc:
            if token.is_punct or token.is_stop:
                continue
            lemma = token.lemma_
            if lemma.isalnum():
                tokens.append(lemma)
            if token.text.endswith("ing") and token.text != lemma and token.text.isalnum():
                tokens.append(token.text)
        return " ".join(tokens)


def _load_spacy_model(model_path: str, *, disable: list[str] | None) -> Any:
    try:
        import spacy
    except ImportError as exc:
        raise ConfigError("retrieval.query_analysis='nlp' requires spaCy. Install comgmem[nlp].") from exc
    resolved_model = _resolve_model_path(model_path)
    try:
        if disable is None:
            return spacy.load(resolved_model)
        return spacy.load(resolved_model, disable=disable)
    except Exception as path_exc:
        target_dir = Path(resolved_model)
        if target_dir.exists():
            sys.path.insert(0, str(target_dir))
            try:
                if disable is None:
                    return spacy.load("en_core_web_sm")
                return spacy.load("en_core_web_sm", disable=disable)
            except Exception:
                try:
                    sys.path.remove(str(target_dir))
                except ValueError:
                    pass
        raise ConfigError(
            "retrieval.query_analysis='nlp' requires a valid spaCy model. "
            f"Configured nlp.model_path={model_path!r}. "
            "Install a model package, or install one into the configured local path."
        ) from path_exc


def _resolve_model_path(model_path: str) -> str:
    path = Path(model_path)
    if path.exists():
        return str(path)
    if not path.is_absolute():
        candidate = Path.cwd() / path
        if candidate.exists():
            return str(candidate)
    return model_path


def _analysis_from_payload(query: str, mode: str, payload: dict[str, Any]) -> QueryAnalysis:
    if not isinstance(payload, dict):
        raise ValueError("Query analysis payload must be a JSON object.")
    data = dict(payload or {})
    return QueryAnalysis(
        query=query,
        mode=mode,
        normalized_query=str(data.get("normalized_query") or ""),
        bm25_query=str(data.get("bm25_query") or ""),
        entities=_entity_dicts(data.get("entities")),
        attributes=data.get("attributes") if isinstance(data.get("attributes"), dict) else {},
    )


def _entity_dicts(value: Any) -> list[dict[str, str]]:
    entities: list[dict[str, str]] = []
    if not isinstance(value, list):
        return entities
    for item in value:
        if isinstance(item, dict):
            text = str(item.get("text") or item.get("name") or "").strip()
            entity_type = str(item.get("type") or item.get("entity_type") or "").strip()
        else:
            text = str(item).strip()
            entity_type = ""
        if text:
            entities.append({"type": entity_type, "text": text})
    return entities
