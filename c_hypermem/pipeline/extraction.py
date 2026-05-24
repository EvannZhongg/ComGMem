from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

from c_hypermem.config import MemoryConfig
from c_hypermem.llms.base import LLMClient
from c_hypermem.llms.openai_compatible import OpenAICompatibleLLM
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
        payload = self.llm.generate_json(prompt)
        return normalize_extraction_payload(payload)

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
    data = dict(payload or {})
    data["entities"] = [_normalize_entity(item) for item in _list(data.get("entities"))]
    data["events"] = [_normalize_event(item) for item in _list(data.get("events"))]
    data["assertions"] = [_normalize_assertion(item) for item in _list(data.get("assertions"))]
    data["sources"] = [_normalize_source(item) for item in _list(data.get("sources"))]
    return MemoryExtraction.model_validate(data)


def _normalize_entity(value: Any) -> dict[str, Any]:
    data = _as_dict(value, text_key="name")
    data["name"] = str(data.get("name") or data.get("text") or data.get("summary") or "").strip()
    data["labels"] = _strings(data.get("labels"))
    data["aliases"] = _strings(data.get("aliases"))
    if "type" not in data and "entity_type" in data:
        data["type"] = data["entity_type"]
    return data


def _normalize_event(value: Any) -> dict[str, Any]:
    data = _as_dict(value, text_key="summary")
    data["summary"] = str(data.get("summary") or data.get("text") or "").strip()
    data["participants"] = [_normalize_participant(item) for item in _list(data.get("participants"))]
    data["labels"] = _strings(data.get("labels"))
    return data


def _normalize_participant(value: Any) -> dict[str, Any]:
    data = _as_dict(value, text_key="name")
    data["name"] = str(data.get("name") or data.get("text") or "").strip()
    return data


def _normalize_assertion(value: Any) -> dict[str, Any]:
    data = _as_dict(value, text_key="object")
    if "object" not in data and "value" in data:
        data["object"] = data["value"]
    if "subject" not in data:
        data["subject"] = ""
    if "predicate" not in data:
        data["predicate"] = "related_to"
    data["subject"] = str(data.get("subject") or "").strip()
    data["predicate"] = str(data.get("predicate") or "").strip()
    data["object"] = str(data.get("object") or "").strip()
    data["labels"] = _strings(data.get("labels"))
    return data


def _normalize_source(value: Any) -> dict[str, Any]:
    data = _as_dict(value, text_key="text")
    data["text"] = str(data.get("text") or data.get("content") or "").strip()
    if "ref" not in data and "source_ref" in data:
        data["ref"] = data["source_ref"]
    return data


def _render_node_labels(config: MemoryConfig) -> str:
    rows = []
    for label, policy in config.node_labels.labels.items():
        if not policy.enabled:
            continue
        description = policy.description or "No description provided."
        rows.append(f"- {label}: {description}")
    if config.node_labels.default_policy.description:
        rows.append(f"- Other precise labels are allowed: {config.node_labels.default_policy.description}")
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
        'Return one JSON object with keys "entities", "events", "assertions", and "sources". '
        "Use assertions as the single carrier for facts, attributes, and triples. "
        "Use Context only to resolve references and extract memories only from Target. "
        "Do not output node_id, edge_id, entity_id, triple_id, confidence, salience, weight, or graph structure."
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


def _as_dict(value: Any, *, text_key: str) -> dict[str, Any]:
    if isinstance(value, dict):
        return dict(value)
    return {text_key: str(value)}


def _list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return [value]


def _strings(value: Any) -> list[str]:
    return [str(item).strip() for item in _list(value) if str(item).strip()]
