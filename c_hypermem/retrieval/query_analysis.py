from __future__ import annotations

import json
import re
import threading
from dataclasses import dataclass, field
from typing import Any, Protocol

from c_hypermem.config import RetrievalConfig
from c_hypermem.errors import ConfigError
from c_hypermem.llms.base import LLMClient
from c_hypermem.utils.prompts import PromptRegistry
from c_hypermem.utils.text import normalize_text


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
    llm: LLMClient | None = None,
    prompt_registry: PromptRegistry | None = None,
) -> QueryAnalyzer:
    if config.query_analysis is False:
        return DisabledQueryAnalyzer()
    if config.query_analysis == "nlp":
        return SpacyQueryAnalyzer()
    if config.query_analysis == "llm":
        if llm is None:
            raise ConfigError("retrieval.query_analysis='llm' requires an LLM client or config.llm.")
        return LLMQueryAnalyzer(llm=llm, prompt_registry=prompt_registry)
    raise ConfigError(f"Unsupported retrieval.query_analysis mode: {config.query_analysis!r}")


class DisabledQueryAnalyzer:
    def analyze(self, query: str) -> QueryAnalysis:
        return QueryAnalysis.disabled(query)


class LLMQueryAnalyzer:
    def __init__(
        self,
        *,
        llm: LLMClient,
        prompt_registry: PromptRegistry | None = None,
    ) -> None:
        self.llm = llm
        self.prompt_registry = prompt_registry or PromptRegistry()

    def analyze(self, query: str) -> QueryAnalysis:
        prompt = self._render_prompt(query)
        payload = self.llm.generate_json(prompt)
        return _analysis_from_payload(query, "llm", payload)

    def _render_prompt(self, query: str) -> str:
        prompt = self.prompt_registry.load("retrieval.query_analysis")
        return (
            f"{prompt.text.rstrip()}\n\n"
            "# Query\n"
            f"{query}\n\n"
            "# Output JSON Shape\n"
            "Return one JSON object with keys: normalized_query, bm25_query, entities, attributes.\n"
            "entities must be a list of objects with string keys type and text.\n"
            "attributes may contain query intent, time constraints, expected labels, or other retrieval hints.\n"
            "Do not output memory IDs, scores, or retrieval results."
        )


class SpacyQueryAnalyzer:
    def __init__(self) -> None:
        self._nlp_full: Any | None = None
        self._nlp_lemma: Any | None = None
        self._lock = threading.Lock()

    def analyze(self, query: str) -> QueryAnalysis:
        return QueryAnalysis(
            query=query,
            mode="nlp",
            normalized_query=normalize_text(query),
            bm25_query=self._lemmatize_for_bm25(query),
            entities=[{"type": entity_type, "text": entity_text} for entity_type, entity_text in self._extract_entities(query)],
        )

    def _load_full(self) -> Any:
        if self._nlp_full is not None:
            return self._nlp_full
        with self._lock:
            if self._nlp_full is None:
                self._nlp_full = _load_spacy_model(disable=None)
        return self._nlp_full

    def _load_lemma(self) -> Any:
        if self._nlp_lemma is not None:
            return self._nlp_lemma
        with self._lock:
            if self._nlp_lemma is None:
                self._nlp_lemma = _load_spacy_model(disable=["ner", "parser"])
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

    def _extract_entities(self, text: str) -> list[tuple[str, str]]:
        doc = self._load_full()(text)
        return _extract_entities_from_doc(doc)


def _load_spacy_model(*, disable: list[str] | None) -> Any:
    try:
        import spacy
    except ImportError as exc:
        raise ConfigError("retrieval.query_analysis='nlp' requires spaCy. Install c-hypermem[nlp].") from exc
    try:
        if disable is None:
            return spacy.load("en_core_web_sm")
        return spacy.load("en_core_web_sm", disable=disable)
    except Exception as exc:
        raise ConfigError(
            "retrieval.query_analysis='nlp' requires spaCy model en_core_web_sm. "
            "Install it with: python -m spacy download en_core_web_sm"
        ) from exc


_GENERIC_HEADS = {
    "thing", "stuff", "way", "time", "experience", "situation", "case",
    "fact", "matter", "issue", "idea", "thought", "feeling", "place",
    "area", "part", "kind", "type", "sort", "lot", "bit", "day", "year",
    "week", "month", "moment", "instance", "example", "technique",
    "method", "approach", "process", "step", "tool", "result", "outcome",
    "goal", "task", "item", "topic", "scale", "size", "level", "degree",
    "amount", "number", "style", "look", "color", "colour", "shape",
    "form", "piece", "section", "side", "end", "edge", "surface", "point",
}

_CIRCUMSTANTIAL_MODS = {
    "solo", "individual", "team", "group", "joint", "collaborative",
    "first", "last", "next", "previous", "final", "initial", "main", "side",
}

_NON_SPECIFIC_ADJ = {
    "many", "few", "several", "some", "any", "all", "most", "more",
    "less", "much", "little", "enough", "various", "numerous", "multiple",
    "countless", "great", "good", "bad", "nice", "terrible", "awful",
    "awesome", "amazing", "wonderful", "horrible", "excellent", "poor",
    "best", "worst", "fine", "okay", "new", "old", "recent", "past",
    "future", "current", "previous", "next", "last", "first", "latest",
    "early", "late", "former", "modern", "ancient", "big", "small",
    "large", "tiny", "huge", "enormous", "long", "short", "tall", "high",
    "low", "wide", "narrow", "thick", "thin", "deep", "shallow",
    "similar", "different", "same", "other", "another", "such", "certain",
    "important", "main", "major", "minor", "key", "primary", "real",
    "actual", "true", "whole", "entire", "full", "complete", "total",
    "basic", "simple", "interesting", "boring", "exciting", "special",
    "particular", "general", "common", "unique", "rare", "typical",
    "usual", "normal", "regular", "possible", "likely", "potential",
    "available", "necessary", "only", "solo", "individual", "team",
    "group", "joint", "collaborative", "final", "initial", "side",
}

_GENERIC_ENDINGS = {
    "work", "works", "job", "jobs", "task", "tasks", "stuff", "things",
    "thing", "info", "information", "details", "data", "content",
    "material", "materials", "activities", "activity", "efforts", "effort",
    "options", "option", "choices", "choice", "results", "result",
    "output", "outputs", "products", "product", "items", "item",
}

_GENERIC_CAPS = {
    "works", "items", "things", "stuff", "resources", "options", "tips",
    "ideas", "steps", "ways", "methods", "tools", "features", "benefits",
    "examples", "details", "notes", "instructions", "guidelines",
    "recommendations", "suggestions", "overview", "summary", "conclusion",
    "introduction", "pros", "cons", "advantages", "disadvantages",
}

_FORMATTING_MARKERS = {"*", "-", "+", "\u2022", "\u2013", "\u2014", "#", "##", "###", "**", "__"}


def _analysis_from_payload(query: str, mode: str, payload: dict[str, Any]) -> QueryAnalysis:
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


def _is_sentence_start(tokens: list[Any], idx: int) -> bool:
    if idx == 0:
        return True
    tok = tokens[idx]
    if tok.is_sent_start:
        return True
    prev = tokens[idx - 1].text
    return prev in ".!?:" or prev in _FORMATTING_MARKERS or "\n" in prev


def _strip_generic_ending(toks: list[Any]) -> list[Any]:
    if len(toks) <= 1:
        return toks
    last = toks[-1].lemma_.lower() if hasattr(toks[-1], "lemma_") else toks[-1].lower()
    return toks[:-1] if last in _GENERIC_ENDINGS and len(toks) > 2 else toks


def _lemmatize_compound(toks: list[Any]) -> str:
    return " ".join(t.lemma_ if t.pos_ == "NOUN" else t.text for t in toks)


def _has_artifacts(txt: str) -> bool:
    return any(
        [
            "**" in txt or "__" in txt or ":*" in txt,
            re.search(r"\s\*\s|\s\*$|^\*\s", txt),
            "  " in txt or "\n" in txt or "\t" in txt,
            len(txt) > 100,
            txt.startswith(("\u2022", "-", "+", "\u2013", "\u2014")),
        ]
    )


def _extract_entities_from_doc(doc: Any) -> list[tuple[str, str]]:
    entities: list[tuple[str, str]] = []
    text = doc.text
    tokens = list(doc)

    i = 0
    while i < len(tokens):
        tok = tokens[i]
        if tok.text in _FORMATTING_MARKERS:
            i += 1
            continue
        is_cap = bool(tok.text) and tok.text[0].isupper()
        is_label = i + 1 < len(tokens) and tokens[i + 1].text == ":"
        if is_cap and not is_label and tok.pos_ in {"PROPN", "NOUN", "ADJ"}:
            seq = [(tok, i)]
            j = i + 1
            while j < len(tokens):
                t = tokens[j]
                if (t.text and t.text[0].isupper()) or t.text.lower() in {"'s", "of", "the", "in", "and", "for", "at", "is"}:
                    seq.append((t, j))
                    j += 1
                else:
                    break
            while seq and seq[-1][0].text.lower() in {"of", "the", "in", "and", "for", "at", "is", "'s"}:
                seq.pop()
            if seq:
                has_mid_cap = any(
                    not _is_sentence_start(tokens, idx)
                    for (t, idx) in seq
                    if t.text[0].isupper() and t.text.lower() not in {"'s", "of", "the", "in", "and", "for", "at", "is"}
                )
                if has_mid_cap:
                    phrase = "".join(t.text_with_ws for (t, _) in seq).strip()
                    if len(phrase) > 2:
                        entities.append(("PROPER", phrase))
            i = j
        else:
            i += 1

    for match in re.finditer(r'"([^"]+)"', text):
        if len(match.group(1).strip()) > 2:
            entities.append(("QUOTED", match.group(1).strip()))
    for match in re.finditer(r"(?:^|[\s\(\[{,;])'([^']+)'(?=[\s\.,;:!?\)\]]|$)", text):
        if len(match.group(1).strip()) > 2:
            entities.append(("QUOTED", match.group(1).strip()))

    for chunk in doc.noun_chunks:
        chunk_tokens = list(chunk)
        split_indices: list[int] = []
        poss_splits: list[int] = []
        for idx, tok in enumerate(chunk_tokens):
            if tok.dep_ == "case" and tok.text in {"'s", "\u2019s", "'"}:
                split_indices.append(idx)
                poss_splits.append(idx)
            elif tok.pos_ == "PUNCT" and tok.text in {"'", '"', "\u2018", "\u2019", "\u201c", "\u201d"}:
                split_indices.append(idx)

        if split_indices:
            groups: list[list[Any]] = []
            prev = 0
            for split_idx in split_indices:
                if split_idx > prev:
                    groups.append(chunk_tokens[prev:split_idx])
                if split_idx in poss_splits:
                    next_split = next((split for split in split_indices if split > split_idx), None)
                    owned = chunk_tokens[split_idx + 1 : next_split if next_split else len(chunk_tokens)]
                    if owned:
                        first_content = next((t for t in owned if t.pos_ not in {"PUNCT", "PART"}), None)
                        if not (first_content and first_content.text and first_content.text[0].isupper()):
                            prev = next_split if next_split else len(chunk_tokens)
                            continue
                prev = split_idx + 1
            if prev < len(chunk_tokens):
                groups.append(chunk_tokens[prev:])
        else:
            groups = [chunk_tokens]

        for group in groups:
            if not group:
                continue
            head = next((t for t in reversed(group) if t.pos_ in {"NOUN", "PROPN"}), None)
            if not head:
                continue
            head_generic = head.lemma_.lower() in _GENERIC_HEADS
            content = [
                t
                for t in group
                if t.pos_ not in {"DET", "PRON", "PUNCT", "PART", "ADP", "SCONJ", "NUM"}
                and (t.pos_ == "ADJ" or not t.is_stop)
            ]
            if not content:
                continue
            compound_toks = [t for t in content if t.dep_ == "compound"]
            adj_toks = [t for t in content if t.pos_ == "ADJ" or t.dep_ == "amod"]
            has_spec_adj = any(t.lemma_.lower() not in _NON_SPECIFIC_ADJ for t in adj_toks)
            if head_generic and not has_spec_adj and not compound_toks:
                continue
            if compound_toks:
                is_circ = any(t.lemma_.lower() in _CIRCUMSTANTIAL_MODS for t in compound_toks)
                if is_circ:
                    val = head.lemma_ if head.pos_ == "NOUN" else head.text
                    if len(val) > 2:
                        entities.append(("NOUN", val))
                else:
                    filtered = _strip_generic_ending(
                        [t for t in content if not (t.pos_ == "ADJ" and t.lemma_.lower() in _NON_SPECIFIC_ADJ)]
                    )
                    if filtered:
                        phrase = _lemmatize_compound(filtered)
                        if len(phrase) > 3 and " " in phrase:
                            entities.append(("COMPOUND", phrase))
            elif len(content) > 1 and has_spec_adj:
                filtered = _strip_generic_ending(
                    [t for t in content if not ((t.pos_ == "ADJ" or t.dep_ == "amod") and t.lemma_.lower() in _NON_SPECIFIC_ADJ)]
                )
                if filtered:
                    phrase = _lemmatize_compound(filtered)
                    if len(phrase) > 3 and " " in phrase:
                        entities.append(("COMPOUND", phrase))

    processed = {entity.lower() for entity_type, entity in entities if entity_type == "COMPOUND"}
    generic_verb_heads = _GENERIC_HEADS | {"find", "buy", "purchase", "sale", "deal", "trip", "visit"}

    def collect_compounds(head: Any) -> list[Any]:
        return [t for t in doc if t.head == head and t.dep_ == "compound"]

    for tok in doc:
        if tok.pos_ == "VERB" and tok.dep_ in {"pobj", "dobj", "nsubj"}:
            comps = sorted(collect_compounds(tok), key=lambda t: t.i)
            if comps:
                phrase_toks = comps if tok.lemma_.lower() in generic_verb_heads else comps + [tok]
                phrase = " ".join(t.text for t in phrase_toks)
                if phrase.lower() not in processed and len(phrase) > 3 and " " in phrase:
                    entities.append(("COMPOUND", phrase))
                    processed.add(phrase.lower())

    seen: set[str] = set()
    deduped = []
    for entity_type, entity in entities:
        key = entity.lower().strip()
        if key not in seen and len(key) > 2:
            seen.add(key)
            deduped.append((entity_type, entity))

    cleaned: list[tuple[str, str]] = []
    for entity_type, entity_text in deduped:
        txt = re.sub(r"^\*+\s*|\s*\*+$", "", entity_text.strip())
        txt = re.sub(r"\s*:+$", "", txt)
        txt = re.sub(r"^\d+\s*\.\s*", "", txt)
        if not txt or len(txt) <= 2 or _has_artifacts(txt):
            continue
        if entity_type == "PROPER" and " " not in txt and txt.lower() in _GENERIC_CAPS:
            continue
        cleaned.append((entity_type, txt))

    type_priority = {"PROPER": 0, "COMPOUND": 1, "QUOTED": 2, "NOUN": 3, "VERB": 4}
    best: dict[str, tuple[str, str]] = {}
    for entity_type, entity in cleaned:
        key = entity.lower()
        if key not in best or type_priority.get(entity_type, 99) < type_priority.get(best[key][0], 99):
            best[key] = (entity_type, entity)
    deduped = list(best.values())
    all_lower = [entity.lower() for _, entity in deduped]
    return [(entity_type, entity) for entity_type, entity in deduped if not any(entity.lower() != other and entity.lower() in other for other in all_lower)]


def _compact_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
