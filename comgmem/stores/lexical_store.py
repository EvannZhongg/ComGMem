from __future__ import annotations

from collections import Counter
from math import log

from comgmem.schema import MemoryNode
from comgmem.utils.text import tokenize


class LexicalScorer:
    """Small BM25-like scorer over a namespace snapshot."""

    def score(self, query: str, nodes: list[MemoryNode]) -> list[tuple[MemoryNode, float, dict[str, float]]]:
        query_terms = tokenize(query)
        if not query_terms or not nodes:
            return []

        doc_terms = [tokenize(_node_text(node)) for node in nodes]
        avg_len = sum(len(terms) for terms in doc_terms) / max(len(doc_terms), 1)
        df: Counter[str] = Counter()
        for terms in doc_terms:
            df.update(set(terms))

        query_counter = Counter(query_terms)
        scored: list[tuple[MemoryNode, float, dict[str, float]]] = []
        for node, terms in zip(nodes, doc_terms):
            tf = Counter(terms)
            doc_len = max(len(terms), 1)
            score = 0.0
            exact_bonus = 0.0
            for term, qf in query_counter.items():
                if not tf[term]:
                    continue
                idf = log(1 + (len(nodes) - df[term] + 0.5) / (df[term] + 0.5))
                freq = tf[term]
                denom = freq + 1.5 * (1 - 0.75 + 0.75 * doc_len / max(avg_len, 1))
                score += idf * (freq * 2.5 / denom) * qf
            normalized_content = _node_text(node).lower()
            if query.lower().strip() and query.lower().strip() in normalized_content:
                exact_bonus = 1.0
            total = score + exact_bonus
            if total > 0:
                scored.append((node, total, {"lexical": score, "exact": exact_bonus}))

        scored.sort(key=lambda item: item[1], reverse=True)
        return scored


def _node_text(node: MemoryNode) -> str:
    triple_text = " ".join(
        f"{triple.subject} {triple.predicate} {triple.object}" for triple in node.local_graph.triples
    )
    aliases = " ".join(str(alias) for alias in node.metadata.get("aliases", []))
    attributes = " ".join(str(value) for value in node.attributes.values())
    labels = " ".join(node.node_labels)
    return " ".join(
        [
            node.canonical_text,
            node.normalized_text,
            node.content,
            node.summary,
            labels,
            aliases,
            attributes,
            triple_text,
        ]
    )
