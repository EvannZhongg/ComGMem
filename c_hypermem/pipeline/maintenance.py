from __future__ import annotations

from typing import Any

from c_hypermem.pipeline.context import AssemblyContext
from c_hypermem.pipeline.graph_utils import deep_merge_dict, merge_local_graph, source_metadata
from c_hypermem.schema import (
    EdgeCluster,
    EdgeClusterMember,
    ExtractedAssertion,
    FactPropertyIndexEntry,
    HyperEdge,
    MemoryNode,
)
from c_hypermem.stores.base import MemoryStore
from c_hypermem.llms.base import LLMClient
from c_hypermem.utils.prompts import PromptRegistry
from c_hypermem.utils.time import touch_node_update, utc_now_iso


class GraphMaintenance:
    """Apply graph maintenance that requires semantic model decisions."""

    def __init__(
        self,
        store: MemoryStore | None = None,
        *,
        llm: LLMClient | None = None,
        prompt_registry: PromptRegistry | None = None,
    ) -> None:
        self.store = store
        self.llm = llm
        self.prompt_registry = prompt_registry or PromptRegistry()

    def retire_conflicting_facts(
        self,
        *,
        property_key: str,
        new_fact: MemoryNode,
        assertion: ExtractedAssertion,
        context: AssemblyContext,
        correction_edge_builder,
        old_facts: list[MemoryNode] | None = None,
    ) -> tuple[list[MemoryNode], list[HyperEdge], list[FactPropertyIndexEntry]]:
        if self.store is None:
            return [], [], []

        retired_nodes: list[MemoryNode] = []
        correction_edges: list[HyperEdge] = []
        retired_properties: list[FactPropertyIndexEntry] = []
        if old_facts is None:
            old_properties = self.store.find_fact_properties(context.namespace, property_key, status="active")
            if not old_properties:
                return retired_nodes, correction_edges, retired_properties
            old_fact_ids = [item.fact_node_id for item in old_properties if item.fact_node_id != new_fact.node_id]
            old_facts = self.store.get_nodes(context.namespace, old_fact_ids)
        for old_fact, decision in zip(old_facts, self._check_contradictions(property_key, assertion, old_facts, context)):
            if not decision.should_retire:
                continue
            self._retire_fact(old_fact, new_fact, assertion, context, decision)
            retired_nodes.append(old_fact)
            correction_edges.append(correction_edge_builder(old_fact, new_fact, context))
            retired_properties.append(
                FactPropertyIndexEntry(
                    namespace=context.namespace,
                    property_key=property_key,
                    subject_node_id=old_fact.attributes.get("subject_node_id"),
                    predicate=assertion.predicate,
                    fact_node_id=old_fact.node_id,
                    status=decision.old_status,  # type: ignore[arg-type]
                    updated_at=utc_now_iso(),
                )
            )
        return retired_nodes, correction_edges, retired_properties

    def resolve_fact_overlap(
        self,
        *,
        new_fact: MemoryNode,
        assertion: ExtractedAssertion,
        old_facts: list[MemoryNode],
        context: AssemblyContext,
    ) -> "FactOverlapDecision":
        if not old_facts:
            return FactOverlapDecision(decision="keep_separate", affected_refs=[])
        if self.llm is None:
            raise RuntimeError(
                "GraphMaintenance requires an LLM to check overlapping fact merge/update decisions. "
                "Provide a maintenance_llm or configure config.llm."
            )

        prompt = self._render_fact_merge_prompt(new_fact, assertion, old_facts, context)
        payload = self.llm.generate_json(prompt)
        return _parse_fact_overlap_payload(payload, old_facts)

    def apply_fact_overlap_update(
        self,
        *,
        old_fact: MemoryNode,
        new_fact: MemoryNode,
        assertion: ExtractedAssertion,
        decision: "FactOverlapDecision",
        context: AssemblyContext,
    ) -> MemoryNode:
        old_fact.node_labels = list(dict.fromkeys([*old_fact.node_labels, *new_fact.node_labels]))
        old_fact.attributes.update({key: value for key, value in new_fact.attributes.items() if value not in (None, [], {})})
        old_fact.metadata = deep_merge_dict(old_fact.metadata, source_metadata(context, source_ref=assertion.source_ref))
        old_fact.local_graph = merge_local_graph(old_fact.local_graph, new_fact.local_graph)
        if decision.decision == "update":
            merged_fact = decision.merged_fact.strip()
            old_fact.content = merged_fact or new_fact.content
            old_fact.summary = merged_fact or new_fact.summary
            old_fact.canonical_text = old_fact.content
            old_fact.normalized_text = new_fact.normalized_text
            old_fact.attributes["subject"] = assertion.subject
            old_fact.attributes["predicate"] = assertion.predicate
            old_fact.attributes["object"] = assertion.object
            old_fact.attributes["polarity"] = assertion.polarity
            old_fact.local_graph.triples = new_fact.local_graph.triples
            old_fact.local_graph.attributes = dict(new_fact.local_graph.attributes)
        return touch_node_update(old_fact, context.current_turn)

    def apply(
        self,
        nodes: list[MemoryNode],
        edges: list[HyperEdge],
        edge_clusters: list[EdgeCluster],
        edge_cluster_members: list[EdgeClusterMember],
    ) -> tuple[list[MemoryNode], list[HyperEdge], list[EdgeCluster], list[EdgeClusterMember]]:
        return nodes, edges, edge_clusters, edge_cluster_members

    def _render_fact_merge_prompt(
        self,
        new_fact: MemoryNode,
        assertion: ExtractedAssertion,
        old_facts: list[MemoryNode],
        context: AssemblyContext,
    ) -> str:
        prompt = self.prompt_registry.load("maintenance.fact_merge")
        values = {
            "{{NEW_FACT}}": new_fact.content,
            "{{NEW_FACT_SOURCE}}": _format_source_messages(self._current_source_messages(context)),
            "{{EXISTING_FACTS}}": _format_existing_fact_blocks(old_facts, context, self.store),
            "{{STRICT_JSON_SHAPE}}": (
                'Return one JSON object with "decision", "affected_existing_refs", '
                '"merged_fact", and "rationale". For keep_separate, affected_existing_refs must be [].'
            ),
        }
        return _render_template(prompt.text, values)

    def _check_contradictions(
        self,
        property_key: str,
        assertion: ExtractedAssertion,
        old_facts: list[MemoryNode],
        context: AssemblyContext,
    ) -> list["ContradictionDecision"]:
        if not old_facts:
            return []
        if self.llm is None:
            raise RuntimeError(
                "GraphMaintenance requires an LLM to check overlapping fact contradictions. "
                "Provide a maintenance_llm or configure config.llm."
            )

        prompt = self._render_contradiction_prompt(property_key, assertion, old_facts, context)
        payload = self.llm.generate_json(prompt)
        return _parse_contradiction_payload(payload, old_facts)

    def _render_contradiction_prompt(
        self,
        property_key: str,
        assertion: ExtractedAssertion,
        old_facts: list[MemoryNode],
        context: AssemblyContext,
    ) -> str:
        prompt = self.prompt_registry.load("maintenance.contradiction_check")
        return "\n".join(
            [
                prompt.text,
                "",
                "# Candidate Facts",
                "",
                "New fact:",
                " ".join(part for part in [assertion.subject, assertion.predicate, assertion.object] if part).strip(),
                "",
                "New fact source:",
                _format_source_messages(self._current_source_messages(context)),
                "",
                "Existing facts to check:",
                _format_existing_fact_blocks(old_facts, context, self.store),
                "",
                "Temporal hints:",
                f"- Current turn: {context.current_turn}",
                f"- Date: {context.metadata.get('date') or 'unknown'}",
                f"- Timestamp: {context.metadata.get('timestamp') or 'unknown'}",
                "",
                "# Strict JSON Shape",
                (
                    'Return one JSON object with "conflict_state", "affected_existing_refs", '
                    '"recommended_old_status", "valid_time_update", and "rationale".'
                ),
            ]
        )

    def _retire_fact(
        self,
        old_fact: MemoryNode,
        new_fact: MemoryNode,
        assertion: ExtractedAssertion,
        context: AssemblyContext,
        decision: "ContradictionDecision",
    ) -> None:
        old_fact.status = decision.old_status
        old_fact.superseded_by = new_fact.node_id
        old_fact.invalidated_by = new_fact.node_id
        old_fact.status_reason = decision.rationale or "LLM judged the new fact conflicts with this older fact"
        old_fact.status_updated_at = utc_now_iso()
        for triple in old_fact.local_graph.triples:
            triple.status = decision.old_status  # type: ignore[assignment]
            triple.superseded_by = new_fact.node_id
            triple.invalidated_by = new_fact.node_id
        if old_fact.time.world.valid_time and not old_fact.time.world.valid_time.end:
            old_fact.time.world.valid_time.end = decision.old_end or assertion.time or context.metadata.get("date")
        touch_node_update(old_fact, context.current_turn)

    def _current_source_messages(self, context: AssemblyContext) -> list[Any]:
        if self.store is None:
            return []
        turn_ids = _string_list(context.metadata.get("turn_ids"))
        return self.store.list_turn_messages(context.namespace, turn_ids)


class FactOverlapDecision:
    def __init__(
        self,
        *,
        decision: str,
        affected_refs: list[str],
        merged_fact: str = "",
        rationale: str = "",
    ) -> None:
        self.decision = decision
        self.affected_refs = affected_refs
        self.merged_fact = merged_fact
        self.rationale = rationale

    @property
    def should_merge_or_update(self) -> bool:
        return self.decision in {"merge", "update"}

    @property
    def needs_contradiction_check(self) -> bool:
        return self.decision == "needs_contradiction_check"


class ContradictionDecision:
    def __init__(
        self,
        *,
        ref: str,
        conflict_state: str,
        old_status: str,
        old_end: str | None = None,
        rationale: str = "",
    ) -> None:
        self.ref = ref
        self.conflict_state = conflict_state
        self.old_status = old_status
        self.old_end = old_end
        self.rationale = rationale

    @property
    def should_retire(self) -> bool:
        return self.conflict_state == "contradiction" and self.old_status in {"retired", "invalidated"}


def _parse_contradiction_payload(payload: dict[str, Any], old_facts: list[MemoryNode]) -> list[ContradictionDecision]:
    conflict_state = str(payload.get("conflict_state") or "uncertain").strip().lower()
    old_status = str(payload.get("recommended_old_status") or "uncertain").strip().lower()
    rationale = str(payload.get("rationale") or "")
    valid_time_update = payload.get("valid_time_update") if isinstance(payload.get("valid_time_update"), dict) else {}
    old_end = valid_time_update.get("old_end")
    affected_refs = payload.get("affected_existing_refs")
    if not isinstance(affected_refs, list):
        affected_refs = []
    affected = {str(ref) for ref in affected_refs}

    decisions: list[ContradictionDecision] = []
    for index, _ in enumerate(old_facts):
        ref = f"existing:{index}"
        if ref in affected:
            decisions.append(
                ContradictionDecision(
                    ref=ref,
                    conflict_state=conflict_state,
                    old_status=_memory_status(old_status),
                    old_end=str(old_end) if old_end else None,
                    rationale=rationale,
                )
            )
        else:
            decisions.append(
                ContradictionDecision(
                    ref=ref,
                    conflict_state="compatible",
                    old_status="active",
                    rationale=rationale,
                )
            )
    return decisions


def _parse_fact_overlap_payload(payload: dict[str, Any], old_facts: list[MemoryNode]) -> FactOverlapDecision:
    decision = str(payload.get("decision") or "keep_separate").strip().lower()
    if decision not in {"merge", "update", "keep_separate", "needs_contradiction_check"}:
        raise RuntimeError(f"Unsupported fact merge decision: {decision}")
    affected_refs = payload.get("affected_existing_refs")
    if not isinstance(affected_refs, list):
        affected_refs = []
    refs = [str(ref) for ref in affected_refs]
    valid_refs = {f"existing:{index}" for index, _ in enumerate(old_facts)}
    unknown_refs = [ref for ref in refs if ref not in valid_refs]
    if unknown_refs:
        raise RuntimeError(f"Unknown fact merge refs: {unknown_refs}")
    if decision == "keep_separate":
        refs = []
    if decision in {"merge", "update", "needs_contradiction_check"} and not refs:
        raise RuntimeError(f"Fact merge decision {decision!r} requires affected_existing_refs.")
    return FactOverlapDecision(
        decision=decision,
        affected_refs=refs,
        merged_fact=str(payload.get("merged_fact") or ""),
        rationale=str(payload.get("rationale") or ""),
    )


def _memory_status(value: str) -> str:
    if value in {"active", "retired", "invalidated", "uncertain"}:
        return value
    return "uncertain"


def _format_existing_fact_blocks(old_facts: list[MemoryNode], context: AssemblyContext, store: MemoryStore | None) -> str:
    blocks: list[str] = []
    for index, fact in enumerate(old_facts):
        messages = []
        if store is not None:
            messages = store.list_turn_messages(context.namespace, _string_list(fact.metadata.get("source_turn_ids")))
        blocks.append(
            "\n".join(
                [
                    f"existing:{index}",
                    f"Fact: {fact.content}",
                    "Source:",
                    _format_source_messages(messages),
                ]
            )
        )
    return "\n\n".join(blocks) if blocks else "None."


def _format_source_messages(messages: list[Any]) -> str:
    if not messages:
        return "No source text available."
    lines = []
    for message in messages[:6]:
        role = str(getattr(message, "role", "message")).strip() or "message"
        content = _truncate(str(getattr(message, "content", "")), 800)
        if not content:
            continue
        lines.append(f'{role.title()} said: "{content}"')
    return "\n".join(lines) if lines else "No source text available."


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item) for item in value if str(item)]


def _truncate(value: str, limit: int) -> str:
    cleaned = " ".join(value.split())
    if len(cleaned) <= limit:
        return cleaned
    return f"{cleaned[: max(0, limit - 3)]}..."


def _render_template(template: str, values: dict[str, str]) -> str:
    rendered = template
    for placeholder, value in values.items():
        rendered = rendered.replace(placeholder, value)
    return rendered
