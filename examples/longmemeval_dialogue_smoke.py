from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from c_hypermem import Memory
from c_hypermem.config import MemoryConfig


DEFAULT_FIXTURE = Path("../LongMemEval/data/longmemeval_s_cleaned_smoke_dialogue_1.json")
DEFAULT_RUN_DIR = Path("runs/longmemeval_dialogue_smoke")


def main() -> None:
    parser = argparse.ArgumentParser(description="Run a real C-HyperMem smoke test on one LongMemEval dialogue.")
    parser.add_argument("--fixture", type=Path, default=DEFAULT_FIXTURE)
    parser.add_argument("--run-dir", type=Path, default=DEFAULT_RUN_DIR)
    parser.add_argument("--namespace", default=None)
    parser.add_argument("--top-k", type=int, default=8)
    parser.add_argument("--max-pairs", type=int, default=None, help="Limit ingested user/assistant pairs.")
    parser.add_argument("--reuse-existing", action="store_true", help="Skip ingestion and only report/search existing DB.")
    parser.add_argument(
        "--enable-vector",
        action="store_true",
        help="Also run embedding/Qdrant indexing. Disabled by default so this smoke only requires the LLM config.",
    )
    args = parser.parse_args()

    fixture = args.fixture.resolve()
    run_dir = args.run_dir.resolve()
    run_dir.mkdir(parents=True, exist_ok=True)

    sample = json.loads(fixture.read_text(encoding="utf-8"))[0]
    if len(sample.get("haystack_sessions", [])) != 1:
        raise ValueError("This smoke runner expects exactly one continuous LongMemEval session.")

    config = MemoryConfig.load("configs/default.yaml")
    config.storage.path = str(run_dir / "memory.sqlite3")
    if not args.enable_vector:
        config.index.use_embedding = False
        config.index.vector = "none"

    namespace = args.namespace or f"longmemeval:{sample['question_id']}:{sample['haystack_session_ids'][0]}"
    memory = Memory(config)
    try:
        session_id = sample["haystack_session_ids"][0]
        session_date = sample["haystack_dates"][0]
        messages = sample["haystack_sessions"][0]
        pairs = _conversation_pairs(messages)
        if args.max_pairs is not None:
            pairs = pairs[: args.max_pairs]

        if not args.reuse_existing:
            memory.reset(namespace)
            for pair_index, pair in enumerate(pairs):
                started = time.perf_counter()
                print(f"[smoke] ingesting pair {pair_index + 1}/{len(pairs)}", flush=True)
                memory.add_memory(
                    user_input=pair.get("user"),
                    assistant_output=pair.get("assistant"),
                    namespace=namespace,
                    metadata={
                        "benchmark": "longmemeval",
                        "question_id": sample["question_id"],
                        "session_id": session_id,
                        "session_date": session_date,
                        "pair_index": pair_index,
                        "date": session_date,
                    },
                )
                print(f"[smoke] pair {pair_index + 1} done in {time.perf_counter() - started:.1f}s", flush=True)

        results = memory.search(sample["question"], namespace=namespace, top_k=args.top_k)
        stats = memory.stats(namespace)
        graph_report = _graph_report(memory, namespace)
    finally:
        memory.close()

    summary = {
        "fixture": str(fixture),
        "run_dir": str(run_dir),
        "namespace": namespace,
        "question_id": sample["question_id"],
        "question": sample["question"],
        "expected_answer": sample["answer"],
        "session_id": sample["haystack_session_ids"][0],
        "session_date": sample["haystack_dates"][0],
        "message_count": len(sample["haystack_sessions"][0]),
        "ingested_pairs": len(pairs),
        "llm_model": config.llm.model if config.llm else None,
        "vector_enabled": bool(args.enable_vector and config.index.use_embedding),
        "stats": stats,
        "graph_report": graph_report,
        "top_results": [
            {
                "rank": index + 1,
                "id": result.get("id"),
                "score": result.get("score"),
                "content": result.get("content"),
                "metadata": result.get("metadata", {}),
            }
            for index, result in enumerate(results)
        ],
        "retrieval_contains_expected_answer": any(
            str(sample["answer"]).lower() in str(result.get("content", "")).lower() for result in results
        ),
    }
    summary_path = run_dir / "summary.json"
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    print(json.dumps(summary, ensure_ascii=False, indent=2))


def _conversation_pairs(messages: list[dict[str, Any]]) -> list[dict[str, str]]:
    pairs: list[dict[str, str]] = []
    pending_user: str | None = None
    for message in messages:
        role = str(message.get("role", "user"))
        content = str(message.get("content", ""))
        if role == "user":
            if pending_user is not None:
                pairs.append({"user": pending_user})
            pending_user = content
        elif role == "assistant":
            if pending_user is None:
                pairs.append({"assistant": content})
            else:
                pairs.append({"user": pending_user, "assistant": content})
                pending_user = None
    if pending_user is not None:
        pairs.append({"user": pending_user})
    return pairs


def _graph_report(memory: Memory, namespace: str) -> dict[str, Any]:
    nodes = memory.store.list_nodes(namespace)
    edges = memory.store.list_edges(namespace)
    clusters = memory.store.list_edge_clusters(namespace)
    members = memory.store.list_edge_cluster_members(namespace)
    label_counts: dict[str, int] = {}
    for node in nodes:
        for label in node.node_labels:
            label_counts[label] = label_counts.get(label, 0) + 1
    fact_nodes = [node for node in nodes if "fact" in node.node_labels]
    entity_nodes = [node for node in nodes if "entity" in node.node_labels]
    return {
        "label_counts": dict(sorted(label_counts.items())),
        "edge_type_counts": _count_by(edges, "edge_type"),
        "cluster_label_counts": _cluster_label_counts(clusters),
        "cluster_member_count": len(members),
        "entity_nodes": [
            {
                "id": node.node_id,
                "content": node.content,
                "labels": node.node_labels,
                "aliases": node.metadata.get("aliases"),
                "source_turn_ids": node.metadata.get("source_turn_ids"),
            }
            for node in entity_nodes[:20]
        ],
        "fact_nodes": [
            {
                "id": node.node_id,
                "status": node.status,
                "content": node.content,
                "attributes": node.attributes,
                "source_turn_ids": node.metadata.get("source_turn_ids"),
                "triples": [
                    {
                        "subject": triple.subject,
                        "predicate": triple.predicate,
                        "object": triple.object,
                        "status": triple.status,
                    }
                    for triple in node.local_graph.triples
                ],
            }
            for node in fact_nodes[:50]
        ],
        "answer_fact_candidates": [
            {
                "id": node.node_id,
                "content": node.content,
                "attributes": node.attributes,
                "triples": [
                    {
                        "subject": triple.subject,
                        "predicate": triple.predicate,
                        "object": triple.object,
                    }
                    for triple in node.local_graph.triples
                ],
            }
            for node in fact_nodes
            if "business administration" in (node.content + " " + json.dumps(node.attributes)).lower()
        ],
    }


def _count_by(items: list[Any], attr: str) -> dict[str, int]:
    counts: dict[str, int] = {}
    for item in items:
        value = str(getattr(item, attr))
        counts[value] = counts.get(value, 0) + 1
    return dict(sorted(counts.items()))


def _cluster_label_counts(clusters: list[Any]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for cluster in clusters:
        for label in cluster.cluster_labels or ["unlabeled"]:
            counts[label] = counts.get(label, 0) + 1
    return dict(sorted(counts.items()))


if __name__ == "__main__":
    main()
