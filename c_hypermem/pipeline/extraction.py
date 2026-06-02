from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

from c_hypermem.config import MemoryConfig
from c_hypermem.llms.base import LLMClient
from c_hypermem.llms.openai_compatible import OpenAICompatibleLLM
from c_hypermem.llms.retrying import generate_json_with_parse_retries
from c_hypermem.schema import MemoryExtraction, Message
from c_hypermem.utils.prompts import PromptRegistry
from c_hypermem.utils.text import truncate


@dataclass(frozen=True)
class ExtractionContext:
    namespace: str
    metadata: dict[str, Any]
    current_turn: int


@dataclass(frozen=True)
class ExtractionWindow:
    context: list[Message]
    target: list[Message]


class MemoryExtractor(Protocol):
    """Produces compact semantic candidates from normalized messages."""

    def extract(self, window: ExtractionWindow, context: ExtractionContext) -> MemoryExtraction: ...


class LLMMemoryExtractor:
    """One-pass extraction that leaves graph construction to C-HyperMem."""

    def __init__(
        self,
        config: MemoryConfig,
        *,
        llm: LLMClient | None = None,
        prompt_registry: PromptRegistry | None = None,
    ) -> None:
        if llm is None and config.llm is None:
            raise ValueError("LLM extraction requires config.llm or an explicit llm client.")
        self.config = config
        self.llm = llm or OpenAICompatibleLLM(config.llm)  # type: ignore[arg-type]
        self.prompt_registry = prompt_registry or PromptRegistry()

    def extract(self, window: ExtractionWindow, context: ExtractionContext) -> MemoryExtraction:
        prompt = self._render_prompt(window, context)
        return generate_json_with_parse_retries(
            self.llm,
            prompt,
            normalize_extraction_payload,
            config=self.config.llm,
        )

    def _render_prompt(self, window: ExtractionWindow, context: ExtractionContext) -> str:
        prompt_id = _prompt_id_from_path(self.config.extraction.prompt)
        prompt = self.prompt_registry.load(prompt_id)
        prompt_text = _render_prompt_template(
            prompt.text,
            self.config,
            interaction_metadata=_compact_json(context.metadata),
            recent_context=_render_messages(window.context, ref_prefix="context") or "None",
            target_messages=_render_messages(window.target, ref_prefix="target"),
            strict_json_shape=_strict_json_shape(),
        )
        parts = [prompt_text]
        if "{{NODE_LABELS}}" not in prompt.text and self.config.extraction.pass_node_labels_to_prompt:
            parts.insert(1, "\n# Enabled Node Label Preferences\n" + _render_node_labels(self.config))
        return "\n".join(parts)


def normalize_extraction_payload(payload: dict[str, Any]) -> MemoryExtraction:
    if not isinstance(payload, dict):
        raise ValueError("Extraction payload must be a JSON object.")
    data = dict(payload or {})
    missing_keys = [key for key in ("nodes", "edge_summaries") if key not in data]
    if missing_keys:
        raise ValueError(f"Extraction payload missing required keys: {', '.join(missing_keys)}")
    data["nodes"] = [_normalize_node(item) for item in _array(data.get("nodes"), "nodes")]
    data["edge_summaries"] = [
        _normalize_edge_summary(item) for item in _array(data.get("edge_summaries"), "edge_summaries")
    ]
    return MemoryExtraction.model_validate(data)


def _normalize_node(value: Any) -> dict[str, Any]:
    data = _mapping(value, "nodes[]")
    for key in ("ref", "canonical_text"):
        if key in data:
            data[key] = str(data[key]).strip()
    if "labels" in data:
        data["labels"] = _strings(data["labels"], "nodes[].labels")
    if "summaries" in data:
        data["summaries"] = _strings(data["summaries"], "nodes[].summaries")
    if "edge_summary_refs" in data:
        data["edge_summary_refs"] = _strings(data["edge_summary_refs"], "nodes[].edge_summary_refs")
    if "triples" in data:
        data["triples"] = [_normalize_triple(item) for item in _array(data["triples"], "nodes[].triples")]
    return data


def _normalize_triple(value: Any) -> dict[str, Any]:
    data = _mapping(value, "nodes[].triples[]")
    for key in ("subject", "predicate", "object"):
        if key in data:
            data[key] = str(data[key]).strip()
    return data


def _normalize_edge_summary(value: Any) -> dict[str, Any]:
    data = _mapping(value, "edge_summaries[]")
    for key in ("ref", "description"):
        if key in data:
            data[key] = str(data[key]).strip()
    return data


def _render_node_labels(config: MemoryConfig) -> str:
    rows = []
    for label, policy in config.node_labels.labels.items():
        if not policy.enabled:
            continue
        description = policy.description or "No description provided."
        rows.append(f"- {label}: {description}")
    return "\n".join(rows) or "No configured labels."


def _render_prompt_template(
    template: str,
    config: MemoryConfig,
    *,
    interaction_metadata: str = "",
    recent_context: str = "",
    target_messages: str = "",
    strict_json_shape: str = "",
) -> str:
    node_labels = _render_node_labels(config) if config.extraction.pass_node_labels_to_prompt else "Not provided."
    replacements = {
        "{{NODE_LABELS}}": node_labels,
        "{{INTERACTION_METADATA}}": interaction_metadata,
        "{{RECENT_CONTEXT}}": recent_context,
        "{{TARGET_MESSAGES}}": target_messages,
        "{{STRICT_JSON_SHAPE}}": strict_json_shape,
    }
    rendered = template
    for placeholder, value in replacements.items():
        rendered = rendered.replace(placeholder, value)
    return rendered


def _strict_json_shape() -> str:
    return (
        'Return one JSON object with keys "nodes" and "edge_summaries". '
        "Each node must have ref, labels, canonical_text, summaries, triples, edge_summary_refs, and optional metadata. "
        "Each edge summary must have ref, description, and optional metadata. "
        "Use Context only to resolve references and extract memories only from Target. "
        "Do not output sources, source_ref, source_refs, node_id, edge_id, entity_id, triple_id, edge_type, relation, roles, polarity, nodes[].time, confidence, salience, weight, or graph structure."
    )


def _render_messages(messages: list[Message], *, ref_prefix: str = "message") -> str:
    rendered = []
    for index, message in enumerate(messages):
        timestamp = f" time={message.timestamp}" if message.timestamp else ""
        rendered.append(f"[{ref_prefix}:{index}] role={message.role}{timestamp}\n{truncate(message.content, 4000)}")
    return "\n\n".join(rendered)


def _prompt_id_from_path(path: str) -> str:
    normalized = path.replace("\\", "/")
    if normalized == "extraction/memory_extraction.md":
        return "extraction.memory"
    return normalized.removesuffix(".md").replace("/", ".")


def _compact_json(value: Any) -> str:
    import json

    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _mapping(value: Any, location: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{location} must be an object.")
    return dict(value)


def _array(value: Any, location: str) -> list[Any]:
    if not isinstance(value, list):
        raise ValueError(f"{location} must be an array.")
    return value


def _strings(value: Any, location: str) -> list[str]:
    return [str(item).strip() for item in _array(value, location) if str(item).strip()]
