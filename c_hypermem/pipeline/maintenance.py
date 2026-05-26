from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter

from c_hypermem.config import MemoryConfig
from c_hypermem.errors import ConfigError
from c_hypermem.llms.base import LLMClient
from c_hypermem.pipeline.context import AssemblyContext
from c_hypermem.pipeline.graph_utils import (
    dedupe_labels,
    deep_merge_dict,
)
from c_hypermem.schema import HyperEdge, LocalTriple, MemoryNode
from c_hypermem.utils.ids import make_local_triple_id, make_source_triple_id, semantic_triple_qualifiers
from c_hypermem.utils.prompts import PromptRegistry
from c_hypermem.utils.text import normalize_text
from c_hypermem.utils.time import touch_node_update, utc_now_iso


class GraphMaintenance:
    """Memory maintenance for the homogeneous node write path."""

    def __init__(
        self,
        config: MemoryConfig,
        *,
        llm: LLMClient | None = None,
        prompt_registry: PromptRegistry | None = None,
        token_counter: "TokenCounter | None" = None,
    ) -> None:
        self.config = config
        self.llm = llm
        self.prompt_registry = prompt_registry or PromptRegistry()
        self._token_counter = token_counter

    def merge_node(self, existing: MemoryNode | None, incoming: MemoryNode, context: AssemblyContext) -> MemoryNode:
        if existing is None:
            return self._initialize_new_node(incoming, context)

        incoming_source_ids = _source_turn_ids(incoming)
        incoming_summary = incoming.summary.strip()

        existing.node_labels = dedupe_labels([*existing.node_labels, *incoming.node_labels])
        existing.attributes = deep_merge_dict(existing.attributes, incoming.attributes)
        existing.metadata = deep_merge_dict(existing.metadata, incoming.metadata)
        self._maintain_local_triples(existing, incoming, context)
        if not existing.content and incoming.content:
            existing.content = incoming.content

        self._maintain_node_summary(
            existing,
            incoming_summary=incoming_summary,
            incoming_source_ids=incoming_source_ids,
            context=context,
        )
        _refresh_local_triple_distribution(existing, context)
        return touch_node_update(existing, context.current_turn)

    def merge_edge(self, existing: HyperEdge | None, incoming: HyperEdge, context: AssemblyContext) -> HyperEdge:
        if existing is None:
            return self._initialize_new_edge(incoming, context)

        incoming_source_ids = _source_turn_ids_from_metadata(incoming.metadata)
        incoming_description = incoming.description.strip()

        existing.status = incoming.status if existing.status != "active" else existing.status
        existing.member_signature = incoming.member_signature
        existing.member_version = max(existing.member_version, incoming.member_version)
        existing.node_ids = list(dict.fromkeys([*existing.node_ids, *incoming.node_ids]))
        existing.weights = {**existing.weights, **incoming.weights}
        existing.metadata = _merge_edge_metadata(existing.metadata, incoming.metadata)
        if existing.time.world.event_time is None:
            existing.time.world.event_time = incoming.time.world.event_time
        if existing.time.world.source_timestamp is None:
            existing.time.world.source_timestamp = incoming.time.world.source_timestamp
        existing.time.lifecycle.updated_at = utc_now_iso()
        existing.time.activation.updated_turn = context.current_turn

        self._maintain_edge_description(
            existing,
            incoming_description=incoming_description,
            incoming_source_ids=incoming_source_ids,
            context=context,
        )
        return existing

    def _maintain_local_triples(
        self,
        existing: MemoryNode,
        incoming: MemoryNode,
        context: AssemblyContext,
    ) -> None:
        _initialize_triple_provenance(incoming, context)
        merge_tasks: list[LocalTripleMergeTask] = []
        for index, incoming_triple in enumerate(incoming.local_graph.triples):
            same_subject = [
                triple
                for triple in existing.local_graph.triples
                if triple.status == "active" and _triple_subject_key(triple) == _triple_subject_key(incoming_triple)
            ]
            same_spo = [triple for triple in same_subject if _triple_op_key(triple) == _triple_op_key(incoming_triple)]
            if same_spo:
                _merge_duplicate_triple_provenance(same_spo[0], incoming_triple, context)
                continue
            candidates = [
                triple
                for triple in same_subject
                if _triple_predicate_key(triple) == _triple_predicate_key(incoming_triple)
            ]
            if not candidates or not self.config.maintenance.local_triples.enabled:
                existing.local_graph.triples.append(incoming_triple)
                continue
            if self.llm is None:
                raise RuntimeError(
                    "Local triple maintenance found matching subject/predicate candidates and requires an LLM."
                )
            merge_tasks.append(
                LocalTripleMergeTask(
                    incoming_ref=f"incoming:{index}",
                    incoming_triple=incoming_triple,
                    candidates=candidates,
                )
            )

        if not merge_tasks:
            return
        decisions = self._decide_local_triple_merges(existing, merge_tasks, context)
        for task, decision in zip(merge_tasks, decisions, strict=True):
            self._apply_local_triple_decision(existing, task.incoming_triple, task.candidates, decision, context)

    def _decide_local_triple_merge(
        self,
        node: MemoryNode,
        incoming_triple: LocalTriple,
        candidates: list[LocalTriple],
        context: AssemblyContext,
    ) -> "LocalTripleMergeDecision":
        decisions = self._decide_local_triple_merges(
            node,
            [
                LocalTripleMergeTask(
                    incoming_ref="incoming:0",
                    incoming_triple=incoming_triple,
                    candidates=candidates,
                )
            ],
            context,
        )
        return decisions[0]

    def _decide_local_triple_merges(
        self,
        node: MemoryNode,
        tasks: list["LocalTripleMergeTask"],
        context: AssemblyContext,
    ) -> list["LocalTripleMergeDecision"]:
        prompt = self._render_local_triple_merge_prompt(node, tasks, context)
        payload = self.llm.generate_json(prompt)  # type: ignore[union-attr]
        decisions = TypeAdapter(list[LocalTripleMergeDecision]).validate_python(payload)
        if len(decisions) != len(tasks):
            raise RuntimeError(
                f"Local triple maintenance expected {len(tasks)} decisions but received {len(decisions)}."
            )
        return decisions

    def _apply_local_triple_decision(
        self,
        node: MemoryNode,
        incoming_triple: LocalTriple,
        candidates: list[LocalTriple],
        decision: "LocalTripleMergeDecision",
        context: AssemblyContext,
    ) -> list[LocalTriple]:
        candidate_by_ref = {f"existing:{index}": triple for index, triple in enumerate(candidates)}
        affected = [candidate_by_ref[ref] for ref in decision.affected_existing_refs if ref in candidate_by_ref]

        if decision.decision == "keep_existing":
            for triple in affected or candidates:
                _mark_triple_kept_over_incoming(triple, incoming_triple, decision, context)
            _record_triple_maintenance(node, "keep_existing", decision, context)
            return []

        if decision.decision == "keep_both":
            related = affected or candidates
            _mark_triple_kept_alongside_candidates(incoming_triple, related, decision, context)
            for triple in related:
                _mark_existing_kept_alongside_incoming(triple, incoming_triple, decision, context)
            node.local_graph.triples.append(incoming_triple)
            _record_triple_maintenance(node, "keep_both", decision, context)
            return [incoming_triple]

        if decision.decision == "keep_new":
            for triple in affected or candidates:
                _retire_triple(triple, reason=decision.rationale, current_turn=context.current_turn, replacement=incoming_triple)
            _mark_triple_replacement(incoming_triple, affected or candidates, decision, context)
            node.local_graph.triples.append(incoming_triple)
            _record_triple_maintenance(node, "keep_new", decision, context)
            return [incoming_triple]

        if decision.decision == "merge":
            if decision.merged_triple is None:
                raise RuntimeError("Local triple maintenance decision 'merge' requires merged_triple.")
            for triple in affected or candidates:
                _retire_triple(triple, reason=decision.rationale, current_turn=context.current_turn)
            merged = LocalTriple(
                subject=decision.merged_triple.subject.strip(),
                predicate=decision.merged_triple.predicate.strip(),
                object=decision.merged_triple.object.strip(),
                qualifiers=decision.merged_triple.qualifiers,
            )
            _initialize_triple_provenance_for_triple(merged, node, context)
            _mark_triple_merge(merged, incoming_triple, affected or candidates, decision, context)
            for triple in affected or candidates:
                triple.superseded_by = merged.triple_id
                triple.qualifiers = {
                    **dict(triple.qualifiers),
                    "maintenance_replaced_by_triple_id": merged.triple_id,
                }
            node.local_graph.triples.append(merged)
            _record_triple_maintenance(node, "merge", decision, context)
            return [merged]

        if decision.decision == "needs_review":
            incoming_triple.status = "uncertain"
            incoming_triple.qualifiers = {
                **dict(incoming_triple.qualifiers),
                "maintenance_decision": "needs_review",
                "maintenance_rationale": decision.rationale,
            }
            _mark_triple_needs_review(incoming_triple, affected or candidates, decision, context)
            node.local_graph.triples.append(incoming_triple)
            _record_triple_maintenance(node, "needs_review", decision, context)
            return [incoming_triple]

        raise RuntimeError(f"Unsupported local triple maintenance decision: {decision.decision}")

    def _render_local_triple_merge_prompt(
        self,
        node: MemoryNode,
        tasks: list["LocalTripleMergeTask"],
        context: AssemblyContext,
    ) -> str:
        prompt_id = _prompt_id_from_path(self.config.maintenance.local_triples.prompt)
        prompt = self.prompt_registry.load(prompt_id)
        node_context = {
            "labels": node.node_labels,
            "canonical_text": node.canonical_text,
            "content": node.content,
            "summary": node.summary,
        }
        conflicts = [_local_triple_conflict_prompt_payload(task) for task in tasks]
        replacements = {
            "{{NODE_CONTEXT}}": _compact_json(node_context),
            "{{LOCAL_TRIPLE_CONFLICTS}}": _compact_json(conflicts),
            "{{STRICT_JSON_SHAPE}}": (
                'Return exactly one JSON array with one decision object per conflict, in the same order: '
                '[{"decision":"keep_existing|keep_new|keep_both|merge|needs_review",'
                '"affected_existing_refs":["existing:0"],'
                '"merged_triple":{"subject":"...","predicate":"...","object":"...","qualifiers":{}},'
                '"rationale":"Brief reason."}]. Use null for merged_triple unless decision is merge.'
            ),
        }
        rendered = prompt.text
        for placeholder, value in replacements.items():
            rendered = rendered.replace(placeholder, value)
        return rendered

    def _initialize_new_node(self, node: MemoryNode, context: AssemblyContext) -> MemoryNode:
        _initialize_triple_provenance(node, context)
        _refresh_local_triple_distribution(node, context)
        if not self.config.maintenance.node_summary.enabled:
            return node
        state = _summary_state(node)
        source_ids = _source_turn_ids(node) if node.summary.strip() else []
        state["summary_source_turn_ids"] = _unique_strings([*_strings(state.get("summary_source_turn_ids")), *source_ids])
        state["pending_source_turn_ids"] = _unique_strings([*_strings(state.get("pending_source_turn_ids")), *source_ids])
        state.setdefault("compaction_count", 0)
        _set_summary_state(node, state)
        trigger = self._summary_trigger(node.summary, state)
        if trigger is not None:
            node.summary = self._compact_node_summary(node, trigger=trigger, context=context)
            _mark_summary_compacted(node, trigger, context)
        return node

    def _initialize_new_edge(self, edge: HyperEdge, context: AssemblyContext) -> HyperEdge:
        if not self.config.maintenance.hyper_edge_description.enabled:
            return edge
        state = _edge_description_state(edge)
        source_ids = _source_turn_ids_from_metadata(edge.metadata) if edge.description.strip() else []
        state["description_source_turn_ids"] = _unique_strings(
            [*_strings(state.get("description_source_turn_ids")), *source_ids]
        )
        state["pending_source_turn_ids"] = _unique_strings([*_strings(state.get("pending_source_turn_ids")), *source_ids])
        state.setdefault("compaction_count", 0)
        _set_edge_description_state(edge, state)
        trigger = self._edge_description_trigger(edge.description, state)
        if trigger is not None:
            edge.description = self._compact_edge_description(edge, trigger=trigger, context=context)
            _mark_edge_description_compacted(edge, trigger, context)
        return edge

    def _maintain_node_summary(
        self,
        node: MemoryNode,
        *,
        incoming_summary: str,
        incoming_source_ids: list[str],
        context: AssemblyContext,
    ) -> None:
        if not self.config.maintenance.node_summary.enabled:
            if not node.summary and incoming_summary:
                node.summary = incoming_summary
            return
        if not incoming_summary:
            return

        state = _summary_state(node)
        known_sources = _strings(state.get("summary_source_turn_ids"))
        new_source_ids = [source_id for source_id in incoming_source_ids if source_id not in known_sources]
        if not new_source_ids:
            return

        node.summary = _join_summaries(node.summary, incoming_summary)
        state["summary_source_turn_ids"] = _unique_strings([*known_sources, *new_source_ids])
        state["pending_source_turn_ids"] = _unique_strings(
            [*_strings(state.get("pending_source_turn_ids")), *new_source_ids]
        )
        state.setdefault("compaction_count", 0)
        _set_summary_state(node, state)

        trigger = self._summary_trigger(node.summary, state)
        if trigger is None:
            return
        node.summary = self._compact_node_summary(node, trigger=trigger, context=context)
        _mark_summary_compacted(node, trigger, context)

    def _maintain_edge_description(
        self,
        edge: HyperEdge,
        *,
        incoming_description: str,
        incoming_source_ids: list[str],
        context: AssemblyContext,
    ) -> None:
        if not self.config.maintenance.hyper_edge_description.enabled:
            if not edge.description and incoming_description:
                edge.description = incoming_description
            return
        if not incoming_description:
            return

        state = _edge_description_state(edge)
        known_sources = _strings(state.get("description_source_turn_ids"))
        new_source_ids = [source_id for source_id in incoming_source_ids if source_id not in known_sources]
        if incoming_description.strip() not in _description_parts(edge.description):
            edge.description = _join_summaries(edge.description, incoming_description)
        if not new_source_ids:
            return

        state["description_source_turn_ids"] = _unique_strings([*known_sources, *new_source_ids])
        state["pending_source_turn_ids"] = _unique_strings(
            [*_strings(state.get("pending_source_turn_ids")), *new_source_ids]
        )
        state.setdefault("compaction_count", 0)
        _set_edge_description_state(edge, state)

        trigger = self._edge_description_trigger(edge.description, state)
        if trigger is None:
            return
        edge.description = self._compact_edge_description(edge, trigger=trigger, context=context)
        _mark_edge_description_compacted(edge, trigger, context)

    def _summary_trigger(self, summary: str, state: dict[str, Any]) -> dict[str, Any] | None:
        if not summary.strip():
            return None
        summary_config = self.config.maintenance.node_summary
        pending_count = len(_strings(state.get("pending_source_turn_ids")))
        token_count = self._count_tokens(summary)
        reasons = []
        if pending_count >= summary_config.compact_after_k_sources:
            reasons.append("source_count")
        if token_count >= summary_config.max_tokens:
            reasons.append("token_limit")
        if not reasons:
            return None
        return {
            "reasons": reasons,
            "pending_source_count": pending_count,
            "compact_after_k_sources": summary_config.compact_after_k_sources,
            "token_count": token_count,
            "max_tokens": summary_config.max_tokens,
        }

    def _edge_description_trigger(self, description: str, state: dict[str, Any]) -> dict[str, Any] | None:
        if not description.strip():
            return None
        description_config = self.config.maintenance.hyper_edge_description
        pending_count = len(_strings(state.get("pending_source_turn_ids")))
        token_count = self._count_tokens(description)
        reasons = []
        if pending_count >= description_config.compact_after_k_sources:
            reasons.append("source_count")
        if token_count >= description_config.max_tokens:
            reasons.append("token_limit")
        if not reasons:
            return None
        return {
            "reasons": reasons,
            "pending_source_count": pending_count,
            "compact_after_k_sources": description_config.compact_after_k_sources,
            "token_count": token_count,
            "max_tokens": description_config.max_tokens,
        }

    def _compact_node_summary(
        self,
        node: MemoryNode,
        *,
        trigger: dict[str, Any],
        context: AssemblyContext,
    ) -> str:
        if self.llm is None:
            raise RuntimeError("Node summary maintenance reached a compaction trigger and requires an LLM.")
        prompt = self._render_summary_compaction_prompt(node, trigger, context)
        payload = self.llm.generate_json(prompt)
        result = NodeSummaryCompactionResult.model_validate(payload)
        summary = result.summary.strip()
        if not summary:
            raise RuntimeError("Node summary maintenance LLM returned an empty summary.")
        return summary

    def _compact_edge_description(
        self,
        edge: HyperEdge,
        *,
        trigger: dict[str, Any],
        context: AssemblyContext,
    ) -> str:
        if self.llm is None:
            raise RuntimeError("HyperEdge description maintenance reached a compaction trigger and requires an LLM.")
        prompt = self._render_edge_description_compaction_prompt(edge, trigger, context)
        payload = self.llm.generate_json(prompt)
        result = HyperEdgeDescriptionCompactionResult.model_validate(payload)
        description = result.description.strip()
        if not description:
            raise RuntimeError("HyperEdge description maintenance LLM returned an empty description.")
        return description

    def _render_summary_compaction_prompt(
        self,
        node: MemoryNode,
        trigger: dict[str, Any],
        context: AssemblyContext,
    ) -> str:
        prompt_id = _prompt_id_from_path(self.config.maintenance.node_summary.prompt)
        prompt = self.prompt_registry.load(prompt_id)
        state = _summary_state(node)
        node_context = {
            "labels": node.node_labels,
            "canonical_text": node.canonical_text,
            "content": node.content,
            "source_ref_count": len(_strings(state.get("summary_source_turn_ids"))),
            "pending_source_count": len(_strings(state.get("pending_source_turn_ids"))),
        }
        replacements = {
            "{{NODE_CONTEXT}}": _compact_json(node_context),
            "{{ACCUMULATED_SUMMARY}}": node.summary,
            "{{TRIGGER_CONTEXT}}": _compact_json(trigger),
            "{{STRICT_JSON_SHAPE}}": (
                'Return exactly one JSON object: {"summary": "A compact summary for this MemoryNode."}.'
            ),
        }
        rendered = prompt.text
        for placeholder, value in replacements.items():
            rendered = rendered.replace(placeholder, value)
        return rendered

    def _render_edge_description_compaction_prompt(
        self,
        edge: HyperEdge,
        trigger: dict[str, Any],
        context: AssemblyContext,
    ) -> str:
        prompt_id = _prompt_id_from_path(self.config.maintenance.hyper_edge_description.prompt)
        prompt = self.prompt_registry.load(prompt_id)
        state = _edge_description_state(edge)
        edge_context = {
            "member_node_ids": edge.node_ids,
            "member_count": len(edge.node_ids),
            "source_ref_count": len(_strings(state.get("description_source_turn_ids"))),
            "pending_source_count": len(_strings(state.get("pending_source_turn_ids"))),
        }
        replacements = {
            "{{EDGE_CONTEXT}}": _compact_json(edge_context),
            "{{ACCUMULATED_DESCRIPTION}}": edge.description,
            "{{TRIGGER_CONTEXT}}": _compact_json(trigger),
            "{{STRICT_JSON_SHAPE}}": (
                'Return exactly one JSON object: {"description": "A compact description for this HyperEdge."}.'
            ),
        }
        rendered = prompt.text
        for placeholder, value in replacements.items():
            rendered = rendered.replace(placeholder, value)
        return rendered

    def _count_tokens(self, text: str) -> int:
        if self._token_counter is None:
            self._token_counter = TikTokenCounter(self.config.token_counting.tokenizer_encoding)
        return self._token_counter.count(text)


class NodeSummaryCompactionResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    summary: str


class HyperEdgeDescriptionCompactionResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    description: str


@dataclass(frozen=True)
class LocalTripleMergeTask:
    incoming_ref: str
    incoming_triple: LocalTriple
    candidates: list[LocalTriple]


class MergedTriplePayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    subject: str
    predicate: str
    object: str
    qualifiers: dict[str, Any] = Field(default_factory=dict)


class LocalTripleMergeDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")

    decision: Literal["keep_existing", "keep_new", "keep_both", "merge", "needs_review"]
    affected_existing_refs: list[str] = Field(default_factory=list)
    merged_triple: MergedTriplePayload | None = None
    rationale: str = ""


class TokenCounter:
    def count(self, text: str) -> int:
        raise NotImplementedError


class TikTokenCounter(TokenCounter):
    def __init__(self, encoding_name: str) -> None:
        try:
            import tiktoken
        except ImportError as exc:
            raise ConfigError("Install tiktoken to use node summary token-limit maintenance.") from exc
        self.encoding = tiktoken.get_encoding(encoding_name)

    def count(self, text: str) -> int:
        return len(self.encoding.encode(text))


def _summary_state(node: MemoryNode) -> dict[str, Any]:
    maintenance = node.metadata.get("maintenance")
    if not isinstance(maintenance, dict):
        return {}
    state = maintenance.get("node_summary")
    return dict(state) if isinstance(state, dict) else {}


def _set_summary_state(node: MemoryNode, state: dict[str, Any]) -> None:
    maintenance = node.metadata.get("maintenance")
    maintenance = dict(maintenance) if isinstance(maintenance, dict) else {}
    maintenance["node_summary"] = state
    node.metadata["maintenance"] = maintenance


def _edge_description_state(edge: HyperEdge) -> dict[str, Any]:
    maintenance = edge.metadata.get("maintenance")
    if not isinstance(maintenance, dict):
        return {}
    state = maintenance.get("hyper_edge_description")
    return dict(state) if isinstance(state, dict) else {}


def _set_edge_description_state(edge: HyperEdge, state: dict[str, Any]) -> None:
    maintenance = edge.metadata.get("maintenance")
    maintenance = dict(maintenance) if isinstance(maintenance, dict) else {}
    maintenance["hyper_edge_description"] = state
    edge.metadata["maintenance"] = maintenance


def _mark_summary_compacted(node: MemoryNode, trigger: dict[str, Any], context: AssemblyContext) -> None:
    state = _summary_state(node)
    state["pending_source_turn_ids"] = []
    state["compaction_count"] = int(state.get("compaction_count") or 0) + 1
    state["last_compacted_turn"] = context.current_turn
    state["last_compaction_trigger"] = trigger
    _set_summary_state(node, state)


def _mark_edge_description_compacted(edge: HyperEdge, trigger: dict[str, Any], context: AssemblyContext) -> None:
    state = _edge_description_state(edge)
    state["pending_source_turn_ids"] = []
    state["compaction_count"] = int(state.get("compaction_count") or 0) + 1
    state["last_compacted_turn"] = context.current_turn
    state["last_compaction_trigger"] = trigger
    _set_edge_description_state(edge, state)


def _initialize_triple_provenance(node: MemoryNode, context: AssemblyContext) -> None:
    for triple in node.local_graph.triples:
        _initialize_triple_provenance_for_triple(triple, node, context)


def _initialize_triple_provenance_for_triple(
    triple: LocalTriple,
    node: MemoryNode,
    context: AssemblyContext,
) -> None:
    if triple.triple_id is None:
        triple.triple_id = make_local_triple_id(
            node.namespace,
            node.node_id,
            triple.subject,
            triple.predicate,
            triple.object,
            triple.qualifiers,
        )
    source_turn_ids = _strings(node.metadata.get("source_turn_ids")) or _strings(context.metadata.get("turn_ids"))
    qualifiers = dict(triple.qualifiers)
    qualifiers["source_turn_ids"] = _unique_strings([*_strings(qualifiers.get("source_turn_ids")), *source_turn_ids])
    source_triple_ids = _strings(qualifiers.get("source_triple_ids"))
    if not source_triple_ids and triple.triple_id is not None:
        source_triple_ids = [_source_triple_id(node.namespace, triple.triple_id, source_turn_ids)]
    qualifiers["source_triple_ids"] = _unique_strings(source_triple_ids)
    triple.qualifiers = qualifiers


def _merge_duplicate_triple_provenance(
    existing: LocalTriple,
    incoming: LocalTriple,
    context: AssemblyContext,
) -> None:
    qualifiers = dict(existing.qualifiers)
    incoming_qualifiers = dict(incoming.qualifiers)
    qualifiers["source_turn_ids"] = _unique_strings(
        [*_strings(qualifiers.get("source_turn_ids")), *_strings(incoming_qualifiers.get("source_turn_ids"))]
    )
    qualifiers["source_triple_ids"] = _unique_strings(
        [
            *_strings(qualifiers.get("source_triple_ids")),
            *_strings(incoming_qualifiers.get("source_triple_ids")),
        ]
    )
    qualifiers["maintenance_last_action"] = "duplicate_spo"
    qualifiers["maintenance_updated_turn"] = context.current_turn
    qualifiers["maintenance_updated_at"] = utc_now_iso()
    existing.qualifiers = qualifiers


def _retire_triple(
    triple: LocalTriple,
    *,
    reason: str,
    current_turn: int | None,
    replacement: LocalTriple | None = None,
) -> None:
    triple.status = "retired"
    qualifiers = dict(triple.qualifiers)
    qualifiers["maintenance_status_reason"] = reason
    qualifiers["maintenance_updated_turn"] = current_turn
    qualifiers["maintenance_updated_at"] = utc_now_iso()
    if replacement is not None:
        triple.superseded_by = replacement.triple_id
        qualifiers["maintenance_replaced_by_triple_id"] = replacement.triple_id
    triple.qualifiers = qualifiers


def _mark_triple_kept_over_incoming(
    existing: LocalTriple,
    incoming: LocalTriple,
    decision: LocalTripleMergeDecision,
    context: AssemblyContext,
) -> None:
    qualifiers = dict(existing.qualifiers)
    incoming_qualifiers = dict(incoming.qualifiers)
    qualifiers["source_turn_ids"] = _unique_strings(
        [*_strings(qualifiers.get("source_turn_ids")), *_strings(incoming_qualifiers.get("source_turn_ids"))]
    )
    qualifiers["source_triple_ids"] = _unique_strings(
        [
            *_strings(qualifiers.get("source_triple_ids")),
            *_strings(incoming_qualifiers.get("source_triple_ids")),
        ]
    )
    qualifiers["maintenance_last_action"] = "keep_existing"
    qualifiers["maintenance_discarded_triple_ids"] = _unique_strings(
        [*_strings(qualifiers.get("maintenance_discarded_triple_ids")), *([incoming.triple_id] if incoming.triple_id else [])]
    )
    qualifiers["maintenance_discarded_source_turn_ids"] = _unique_strings(
        [
            *_strings(qualifiers.get("maintenance_discarded_source_turn_ids")),
            *_strings(incoming_qualifiers.get("source_turn_ids")),
        ]
    )
    qualifiers["maintenance_rationale"] = decision.rationale
    qualifiers["maintenance_updated_turn"] = context.current_turn
    qualifiers["maintenance_updated_at"] = utc_now_iso()
    existing.qualifiers = qualifiers


def _mark_triple_kept_alongside_candidates(
    incoming: LocalTriple,
    candidates: list[LocalTriple],
    decision: LocalTripleMergeDecision,
    context: AssemblyContext,
) -> None:
    qualifiers = dict(incoming.qualifiers)
    qualifiers["maintenance_last_action"] = "keep_both"
    qualifiers["maintenance_related_triple_ids"] = _unique_strings(
        [*_strings(qualifiers.get("maintenance_related_triple_ids")), *[triple.triple_id for triple in candidates if triple.triple_id]]
    )
    qualifiers["maintenance_related_source_turn_ids"] = _unique_strings(
        [
            *_strings(qualifiers.get("maintenance_related_source_turn_ids")),
            *[turn_id for triple in candidates for turn_id in _strings(triple.qualifiers.get("source_turn_ids"))],
        ]
    )
    qualifiers["maintenance_rationale"] = decision.rationale
    qualifiers["maintenance_updated_turn"] = context.current_turn
    qualifiers["maintenance_updated_at"] = utc_now_iso()
    incoming.qualifiers = qualifiers


def _mark_existing_kept_alongside_incoming(
    existing: LocalTriple,
    incoming: LocalTriple,
    decision: LocalTripleMergeDecision,
    context: AssemblyContext,
) -> None:
    qualifiers = dict(existing.qualifiers)
    qualifiers["maintenance_last_action"] = "keep_both"
    qualifiers["maintenance_related_triple_ids"] = _unique_strings(
        [*_strings(qualifiers.get("maintenance_related_triple_ids")), *([incoming.triple_id] if incoming.triple_id else [])]
    )
    qualifiers["maintenance_related_source_turn_ids"] = _unique_strings(
        [
            *_strings(qualifiers.get("maintenance_related_source_turn_ids")),
            *_strings(incoming.qualifiers.get("source_turn_ids")),
        ]
    )
    qualifiers["maintenance_rationale"] = decision.rationale
    qualifiers["maintenance_updated_turn"] = context.current_turn
    qualifiers["maintenance_updated_at"] = utc_now_iso()
    existing.qualifiers = qualifiers


def _mark_triple_replacement(
    incoming: LocalTriple,
    replaced: list[LocalTriple],
    decision: LocalTripleMergeDecision,
    context: AssemblyContext,
) -> None:
    qualifiers = dict(incoming.qualifiers)
    qualifiers["maintenance_last_action"] = "keep_new"
    qualifiers["maintenance_replaced_triple_ids"] = _unique_strings(
        [*_strings(qualifiers.get("maintenance_replaced_triple_ids")), *[triple.triple_id for triple in replaced if triple.triple_id]]
    )
    qualifiers["maintenance_replaced_source_turn_ids"] = _unique_strings(
        [
            *_strings(qualifiers.get("maintenance_replaced_source_turn_ids")),
            *[turn_id for triple in replaced for turn_id in _strings(triple.qualifiers.get("source_turn_ids"))],
        ]
    )
    qualifiers["maintenance_rationale"] = decision.rationale
    qualifiers["maintenance_updated_turn"] = context.current_turn
    qualifiers["maintenance_updated_at"] = utc_now_iso()
    incoming.qualifiers = qualifiers


def _mark_triple_merge(
    merged: LocalTriple,
    incoming: LocalTriple,
    merged_from: list[LocalTriple],
    decision: LocalTripleMergeDecision,
    context: AssemblyContext,
) -> None:
    qualifiers = dict(merged.qualifiers)
    source_turn_ids = [
        *[turn_id for triple in merged_from for turn_id in _strings(triple.qualifiers.get("source_turn_ids"))],
        *_strings(incoming.qualifiers.get("source_turn_ids")),
    ]
    source_triple_ids = [
        *[source_id for triple in merged_from for source_id in _strings(triple.qualifiers.get("source_triple_ids"))],
        *_strings(incoming.qualifiers.get("source_triple_ids")),
    ]
    merged_triple_ids = [
        *[triple.triple_id for triple in merged_from if triple.triple_id],
        *([incoming.triple_id] if incoming.triple_id else []),
    ]
    qualifiers["source_turn_ids"] = _unique_strings(source_turn_ids)
    qualifiers["source_triple_ids"] = _unique_strings(source_triple_ids)
    qualifiers["maintenance_last_action"] = "merge"
    qualifiers["maintenance_merged_triple_ids"] = _unique_strings(merged_triple_ids)
    qualifiers["maintenance_merged_source_turn_ids"] = _unique_strings(source_turn_ids)
    qualifiers["maintenance_rationale"] = decision.rationale
    qualifiers["maintenance_updated_turn"] = context.current_turn
    qualifiers["maintenance_updated_at"] = utc_now_iso()
    merged.qualifiers = qualifiers


def _mark_triple_needs_review(
    incoming: LocalTriple,
    candidates: list[LocalTriple],
    decision: LocalTripleMergeDecision,
    context: AssemblyContext,
) -> None:
    qualifiers = dict(incoming.qualifiers)
    qualifiers["maintenance_last_action"] = "needs_review"
    qualifiers["maintenance_related_triple_ids"] = _unique_strings(
        [*_strings(qualifiers.get("maintenance_related_triple_ids")), *[triple.triple_id for triple in candidates if triple.triple_id]]
    )
    qualifiers["maintenance_related_source_turn_ids"] = _unique_strings(
        [
            *_strings(qualifiers.get("maintenance_related_source_turn_ids")),
            *[turn_id for triple in candidates for turn_id in _strings(triple.qualifiers.get("source_turn_ids"))],
        ]
    )
    qualifiers["maintenance_rationale"] = decision.rationale
    qualifiers["maintenance_updated_turn"] = context.current_turn
    qualifiers["maintenance_updated_at"] = utc_now_iso()
    incoming.qualifiers = qualifiers


def _record_triple_maintenance(
    node: MemoryNode,
    action: str,
    decision: LocalTripleMergeDecision,
    context: AssemblyContext,
) -> None:
    maintenance = node.metadata.get("maintenance")
    maintenance = dict(maintenance) if isinstance(maintenance, dict) else {}
    local_triples = maintenance.get("local_triples")
    local_triples = dict(local_triples) if isinstance(local_triples, dict) else {}
    local_triples["last_action"] = action
    local_triples["last_rationale"] = decision.rationale
    local_triples["last_updated_turn"] = context.current_turn
    local_triples["decision_count"] = int(local_triples.get("decision_count") or 0) + 1
    maintenance["local_triples"] = local_triples
    node.metadata["maintenance"] = maintenance


def _refresh_local_triple_distribution(node: MemoryNode, context: AssemblyContext) -> None:
    triples = list(node.local_graph.triples)
    status_counts: dict[str, int] = {}
    active_predicate_counts: dict[str, int] = {}
    active_subject_predicate_counts: dict[str, int] = {}
    scoped_count = 0

    for triple in triples:
        status_counts[triple.status] = status_counts.get(triple.status, 0) + 1
        if triple.scope_edge_id or triple.scope_cluster_id:
            scoped_count += 1
        if triple.status != "active":
            continue
        predicate_key = _triple_predicate_key(triple)
        subject_predicate_key = f"{_triple_subject_key(triple)}|{predicate_key}"
        active_predicate_counts[predicate_key] = active_predicate_counts.get(predicate_key, 0) + 1
        active_subject_predicate_counts[subject_predicate_key] = active_subject_predicate_counts.get(subject_predicate_key, 0) + 1

    distribution = {
        "total": len(triples),
        "active": status_counts.get("active", 0),
        "retired": status_counts.get("retired", 0),
        "invalidated": status_counts.get("invalidated", 0),
        "uncertain": status_counts.get("uncertain", 0),
        "by_status": dict(sorted(status_counts.items())),
        "active_by_predicate": dict(sorted(active_predicate_counts.items())),
        "active_by_subject_predicate": dict(sorted(active_subject_predicate_counts.items())),
        "scoped": scoped_count,
        "unscoped": len(triples) - scoped_count,
        "last_updated_turn": context.current_turn,
        "last_updated_at": utc_now_iso(),
    }

    maintenance = node.metadata.get("maintenance")
    maintenance = dict(maintenance) if isinstance(maintenance, dict) else {}
    local_triples = maintenance.get("local_triples")
    local_triples = dict(local_triples) if isinstance(local_triples, dict) else {}
    local_triples["triple_distribution"] = distribution
    maintenance["local_triples"] = local_triples
    node.metadata["maintenance"] = maintenance


def _triple_subject_key(triple: LocalTriple) -> str:
    return normalize_text(triple.subject)


def _triple_predicate_key(triple: LocalTriple) -> str:
    return normalize_text(triple.predicate)


def _triple_op_key(triple: LocalTriple) -> tuple[str, str]:
    return (normalize_text(triple.predicate), normalize_text(triple.object))


def _source_triple_id(namespace: str, triple_id: str, source_turn_ids: list[str]) -> str:
    return make_source_triple_id(namespace, triple_id, source_turn_ids or ["turn:unknown"])


def _triple_prompt_payload(triple: LocalTriple) -> dict[str, Any]:
    return {
        "subject": triple.subject,
        "predicate": triple.predicate,
        "object": triple.object,
        "status": triple.status,
        "qualifiers": semantic_triple_qualifiers(triple.qualifiers),
    }


def _local_triple_conflict_prompt_payload(task: LocalTripleMergeTask) -> dict[str, Any]:
    existing_triples = [
        {
            "ref": f"existing:{index}",
            "subject": triple.subject,
            "predicate": triple.predicate,
            "object": triple.object,
            "status": triple.status,
            "qualifiers": semantic_triple_qualifiers(triple.qualifiers),
        }
        for index, triple in enumerate(task.candidates)
    ]
    return {
        "incoming_ref": task.incoming_ref,
        "incoming_triple": _triple_prompt_payload(task.incoming_triple),
        "existing_triples": existing_triples,
    }


def _source_turn_ids(node: MemoryNode) -> list[str]:
    return _strings(node.metadata.get("source_turn_ids"))


def _source_turn_ids_from_metadata(metadata: dict[str, Any]) -> list[str]:
    return _strings(metadata.get("source_turn_ids"))


def _merge_edge_metadata(existing: dict[str, Any], incoming: dict[str, Any]) -> dict[str, Any]:
    merged = deep_merge_dict(existing, incoming)
    merged["source_turn_ids"] = _unique_strings(
        [*_strings(existing.get("source_turn_ids")), *_strings(incoming.get("source_turn_ids"))]
    )
    merged["edge_summary_refs"] = _unique_strings(
        [
            *_strings(existing.get("edge_summary_refs")),
            *_strings(existing.get("edge_summary_ref")),
            *_strings(incoming.get("edge_summary_refs")),
            *_strings(incoming.get("edge_summary_ref")),
        ]
    )
    merged.pop("edge_summary_ref", None)
    return merged


def _strings(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    if value in (None, "", [], {}):
        return []
    return [str(value).strip()]


def _unique_strings(values: list[str]) -> list[str]:
    return list(dict.fromkeys(value for value in values if value))


def _join_summaries(existing: str, incoming: str) -> str:
    parts = [part.strip() for part in [existing, incoming] if part.strip()]
    return "\n".join(parts)


def _description_parts(description: str) -> set[str]:
    return {part.strip() for part in description.splitlines() if part.strip()}


def _prompt_id_from_path(path: str) -> str:
    normalized = path.replace("\\", "/")
    if normalized == "maintenance/node_summary_compaction.md":
        return "maintenance.node_summary_compaction"
    if normalized == "maintenance/local_triple_merge.md":
        return "maintenance.local_triple_merge"
    if normalized == "maintenance/hyper_edge_description_compaction.md":
        return "maintenance.hyper_edge_description_compaction"
    return normalized.removesuffix(".md").replace("/", ".")


def _compact_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
