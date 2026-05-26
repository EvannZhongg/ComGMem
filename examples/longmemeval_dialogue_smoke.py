from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from collections import Counter
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from c_hypermem import Memory
from c_hypermem.config import MemoryConfig


DEFAULT_FIXTURE = PROJECT_ROOT / "examples" / "longmemeval_s_cleaned_smoke_dialogue_1.json"
DEFAULT_RUN_DIR = PROJECT_ROOT / "runs" / "longmemeval_dialogue_smoke"
DEFAULT_CONFIG = PROJECT_ROOT / "configs" / "default.yaml"


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Build C-HyperMem memory for one LongMemEval dialogue smoke fixture. "
            "This runs ingestion, SQLite persistence, embeddings, and Qdrant vector indexing, "
            "but intentionally skips final retrieval/search."
        )
    )
    parser.add_argument("--fixture", type=Path, default=DEFAULT_FIXTURE)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--run-dir", type=Path, default=DEFAULT_RUN_DIR)
    parser.add_argument("--namespace", default=None)
    parser.add_argument("--max-pairs", type=int, default=None, help="Limit ingested user/assistant pairs.")
    parser.add_argument("--reuse-existing", action="store_true", help="Do not reset namespace before ingesting.")
    parser.add_argument("--node-sample", type=int, default=8, help="How many touched nodes to show after each turn.")
    parser.add_argument("--edge-sample", type=int, default=6, help="How many touched edges/clusters to show after each turn.")
    args = parser.parse_args()

    fixture = args.fixture.resolve()
    run_dir = args.run_dir.resolve()
    run_dir.mkdir(parents=True, exist_ok=True)
    logger = _configure_logging(run_dir)

    logger.info("C-HyperMem LongMemEval dialogue memory-build smoke")
    logger.info("project_root=%s", PROJECT_ROOT)
    logger.info("fixture=%s", fixture)
    logger.info("config=%s", args.config.resolve())
    logger.info("run_dir=%s", run_dir)

    sample = _load_single_sample(fixture)
    session_id = _single(sample, "haystack_session_ids")
    session_date = _single(sample, "haystack_dates")
    messages = _single(sample, "haystack_sessions")
    pairs = _conversation_pairs(messages)
    if args.max_pairs is not None:
        pairs = pairs[: args.max_pairs]
    namespace = args.namespace or f"longmemeval:{sample['question_id']}:{session_id}"

    config = MemoryConfig.load(args.config)
    config.storage.path = str(run_dir / "memory.sqlite3")
    config.index.vector_store.path = str(run_dir / "vector_index")

    logger.info("namespace=%s", namespace)
    logger.info("question_id=%s question=%s", sample.get("question_id"), sample.get("question"))
    logger.info("expected_answer=%s", sample.get("answer"))
    logger.info("session_id=%s session_date=%s", session_id, session_date)
    logger.info("message_count=%d pair_count=%d", len(messages), len(pairs))
    logger.info(
        "llm_model=%s embedding_model=%s embedding_enabled=%s vector_backend=%s vector_path=%s",
        config.llm.model if config.llm else None,
        config.embedding.model if config.embedding else None,
        bool(config.index.use_embedding and config.embedding is not None),
        config.index.vector,
        config.index.vector_store.path,
    )

    started = time.perf_counter()
    memory = Memory(config)
    try:
        logger.info("vector_stores=%s", _vector_store_summary(memory))
        if not args.reuse_existing:
            logger.info("reset namespace and vector indexes")
            memory.reset(namespace)
        else:
            logger.info("reuse_existing=true; namespace reset skipped")

        previous_stats = memory.stats(namespace)
        logger.info("initial_stats=%s", _compact_json(previous_stats))
        for pair_index, pair in enumerate(pairs):
            turn_id = f"turn:{memory.stats(namespace).get('turns', 0)}"
            _log_pair_start(logger, pair_index, len(pairs), turn_id, pair)
            pair_started = time.perf_counter()
            memory.add_memory(
                user_input=pair.get("user"),
                assistant_output=pair.get("assistant"),
                namespace=namespace,
                metadata={
                    "benchmark": "longmemeval",
                    "question_id": sample["question_id"],
                    "question_type": sample.get("question_type"),
                    "session_id": session_id,
                    "session_date": session_date,
                    "pair_index": pair_index,
                    "has_answer": bool(pair.get("has_answer")),
                    "date": session_date,
                },
            )
            current_stats = memory.stats(namespace)
            logger.info(
                "turn_done turn_id=%s elapsed=%.2fs stats_delta=%s totals=%s",
                turn_id,
                time.perf_counter() - pair_started,
                _compact_json(_stats_delta(previous_stats, current_stats)),
                _compact_json(_selected_stats(current_stats)),
            )
            _log_touched_graph(
                logger,
                memory,
                namespace,
                turn_id=turn_id,
                node_sample=args.node_sample,
                edge_sample=args.edge_sample,
            )
            logger.info("vector_counts=%s", _compact_json(_vector_counts(memory)))
            previous_stats = current_stats

        final_stats = memory.stats(namespace)
        graph_report = _graph_report(memory, namespace)
        vector_counts = _vector_counts(memory)
        summary = {
            "fixture": str(fixture),
            "run_dir": str(run_dir),
            "namespace": namespace,
            "question_id": sample.get("question_id"),
            "question": sample.get("question"),
            "expected_answer": sample.get("answer"),
            "session_id": session_id,
            "session_date": session_date,
            "message_count": len(messages),
            "ingested_pairs": len(pairs),
            "elapsed_sec": round(time.perf_counter() - started, 3),
            "storage_path": config.storage.path,
            "vector_store_path": config.index.vector_store.path,
            "llm_model": config.llm.model if config.llm else None,
            "embedding_model": config.embedding.model if config.embedding else None,
            "stats": final_stats,
            "graph_report": graph_report,
            "vector_counts": vector_counts,
        }
        summary_path = run_dir / "memory_build_summary.json"
        summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        logger.info("final_stats=%s", _compact_json(_selected_stats(final_stats)))
        logger.info("graph_report=%s", _compact_json(graph_report))
        logger.info("vector_counts=%s", _compact_json(vector_counts))
        logger.info("summary_path=%s", summary_path)
        logger.info("completed elapsed=%.2fs", time.perf_counter() - started)
    finally:
        memory.close()


def _load_single_sample(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, list) or len(payload) != 1:
        raise ValueError(f"Expected fixture to contain exactly one sample list item: {path}")
    sample = payload[0]
    if not isinstance(sample, dict):
        raise ValueError(f"Expected sample to be a JSON object: {path}")
    return sample


def _single(sample: dict[str, Any], key: str) -> Any:
    values = sample.get(key)
    if not isinstance(values, list) or len(values) != 1:
        raise ValueError(f"Expected sample[{key!r}] to be a one-item list.")
    return values[0]


def _conversation_pairs(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    pairs: list[dict[str, Any]] = []
    pending_user: dict[str, Any] | None = None
    for message in messages:
        role = str(message.get("role", "user")).strip().lower()
        content = str(message.get("content", ""))
        has_answer = bool(message.get("has_answer"))
        if role == "user":
            if pending_user is not None:
                pairs.append(
                    {
                        "user": pending_user["content"],
                        "assistant": None,
                        "has_answer": pending_user["has_answer"],
                    }
                )
            pending_user = {"content": content, "has_answer": has_answer}
        elif role == "assistant":
            if pending_user is None:
                pairs.append({"user": None, "assistant": content, "has_answer": has_answer})
            else:
                pairs.append(
                    {
                        "user": pending_user["content"],
                        "assistant": content,
                        "has_answer": bool(pending_user["has_answer"] or has_answer),
                    }
                )
                pending_user = None
    if pending_user is not None:
        pairs.append({"user": pending_user["content"], "assistant": None, "has_answer": pending_user["has_answer"]})
    return pairs


def _log_pair_start(logger: logging.Logger, index: int, total: int, turn_id: str, pair: dict[str, Any]) -> None:
    user = pair.get("user") or ""
    assistant = pair.get("assistant") or ""
    logger.info(
        "turn_start %d/%d turn_id=%s has_answer=%s user_chars=%d assistant_chars=%d",
        index + 1,
        total,
        turn_id,
        bool(pair.get("has_answer")),
        len(user),
        len(assistant),
    )
    logger.info("user_preview=%s", _preview(user, 260))
    if assistant:
        logger.info("assistant_preview=%s", _preview(assistant, 220))


def _log_touched_graph(
    logger: logging.Logger,
    memory: Memory,
    namespace: str,
    *,
    turn_id: str,
    node_sample: int,
    edge_sample: int,
) -> None:
    nodes = memory.store.list_nodes(namespace)
    edges = memory.store.list_edges(namespace)
    clusters = memory.store.list_edge_clusters(namespace)
    touched_nodes = [node for node in nodes if turn_id in _source_turn_ids(node.metadata)]
    touched_node_ids = {node.node_id for node in touched_nodes}
    touched_edges = [
        edge
        for edge in edges
        if turn_id in _source_turn_ids(edge.metadata) or touched_node_ids.intersection(edge.node_ids)
    ]
    touched_edge_ids = {edge.edge_id for edge in touched_edges}
    touched_clusters = [
        cluster
        for cluster in clusters
        if any(variant.source_edge_id in touched_edge_ids for variant in cluster.description_variants)
        or _cluster_mentions_edges(cluster, touched_edge_ids)
    ]
    logger.info(
        "touched_graph turn_id=%s nodes=%d edges=%d clusters=%d labels=%s",
        turn_id,
        len(touched_nodes),
        len(touched_edges),
        len(touched_clusters),
        _compact_json(_label_counts(touched_nodes)),
    )
    for node in touched_nodes[:node_sample]:
        active_triples = [triple for triple in node.local_graph.triples if triple.status == "active"]
        logger.info(
            "  node labels=%s status=%s triples=%d content=%s",
            ",".join(node.node_labels),
            node.status,
            len(active_triples),
            _preview(node.content, 180),
        )
        for triple in active_triples[:3]:
            logger.info(
                "    triple %s | %s | %s",
                _preview(triple.subject, 80),
                _preview(triple.predicate, 80),
                _preview(triple.object, 120),
            )
    if len(touched_nodes) > node_sample:
        logger.info("  ... %d more touched nodes", len(touched_nodes) - node_sample)
    for edge in touched_edges[:edge_sample]:
        logger.info(
            "  edge members=%d description=%s",
            len(edge.node_ids),
            _preview(edge.description, 220),
        )
    if len(touched_edges) > edge_sample:
        logger.info("  ... %d more touched edges", len(touched_edges) - edge_sample)
    for cluster in touched_clusters[:edge_sample]:
        logger.info(
            "  cluster labels=%s variants=%d description=%s",
            ",".join(cluster.cluster_labels),
            len(cluster.description_variants),
            _preview(cluster.canonical_description, 220),
        )


def _graph_report(memory: Memory, namespace: str) -> dict[str, Any]:
    nodes = memory.store.list_nodes(namespace)
    edges = memory.store.list_edges(namespace)
    clusters = memory.store.list_edge_clusters(namespace)
    members = memory.store.list_edge_cluster_members(namespace)
    active_triples = [
        triple
        for node in nodes
        for triple in node.local_graph.triples
        if triple.status == "active"
    ]
    answer_terms = ("business administration", "degree", "graduate", "graduated")
    answer_related_nodes = [
        node
        for node in nodes
        if any(term in (node.content + " " + node.summary).lower() for term in answer_terms)
    ]
    return {
        "label_counts": _label_counts(nodes),
        "node_status_counts": dict(sorted(Counter(node.status for node in nodes).items())),
        "active_triple_predicate_counts": dict(sorted(Counter(triple.predicate for triple in active_triples).items())),
        "edge_count": len(edges),
        "edge_member_count": sum(len(edge.node_ids) for edge in edges),
        "cluster_label_counts": _cluster_label_counts(clusters),
        "cluster_member_count": len(members),
        "answer_related_nodes": [
            {
                "labels": node.node_labels,
                "status": node.status,
                "content": node.content,
                "summary": node.summary,
                "source_turn_ids": _source_turn_ids(node.metadata),
                "triples": [
                    {
                        "subject": triple.subject,
                        "predicate": triple.predicate,
                        "object": triple.object,
                        "status": triple.status,
                    }
                    for triple in node.local_graph.triples[:8]
                ],
            }
            for node in answer_related_nodes[:20]
        ],
    }


def _label_counts(nodes: list[Any]) -> dict[str, int]:
    counts: Counter[str] = Counter()
    for node in nodes:
        counts.update(node.node_labels)
    return dict(sorted(counts.items()))


def _cluster_label_counts(clusters: list[Any]) -> dict[str, int]:
    counts: Counter[str] = Counter()
    for cluster in clusters:
        counts.update(cluster.cluster_labels or ["unlabeled"])
    return dict(sorted(counts.items()))


def _source_turn_ids(metadata: dict[str, Any]) -> list[str]:
    value = metadata.get("source_turn_ids")
    if isinstance(value, list):
        return [str(item) for item in value]
    return []


def _cluster_mentions_edges(cluster: Any, edge_ids: set[str]) -> bool:
    occurrences = cluster.metadata.get("anchor_occurrences")
    if not isinstance(occurrences, list):
        return False
    for occurrence in occurrences:
        if isinstance(occurrence, dict) and occurrence.get("edge_id") in edge_ids:
            return True
    return False


def _stats_delta(before: dict[str, int], after: dict[str, int]) -> dict[str, int]:
    keys = sorted(set(before) | set(after))
    delta: dict[str, int] = {}
    for key in keys:
        before_value = before.get(key, 0)
        after_value = after.get(key, 0)
        if not isinstance(before_value, int) or not isinstance(after_value, int):
            continue
        if before_value != after_value:
            delta[key] = after_value - before_value
    return delta


def _selected_stats(stats: dict[str, int]) -> dict[str, int]:
    keys = [
        "turns",
        "turn_messages",
        "nodes",
        "hyper_edges",
        "hyper_edge_members",
        "edge_clusters",
        "edge_cluster_members",
        "triples",
        "entity_aliases",
    ]
    selected = {key: int(stats.get(key, 0)) for key in keys}
    for key in sorted(key for key in stats if key.startswith("nodes.")):
        selected[key] = int(stats[key])
    return selected


def _vector_store_summary(memory: Memory) -> dict[str, str]:
    summary: dict[str, str] = {}
    for item_type, store in sorted(memory.vector_stores.items()):
        collection = getattr(store, "collection_name", None)
        summary[item_type] = str(collection or store.__class__.__name__)
    return summary


def _vector_counts(memory: Memory) -> dict[str, int | None]:
    counts: dict[str, int | None] = {}
    for item_type, store in sorted(memory.vector_stores.items()):
        client = getattr(store, "_client", None)
        collection_name = getattr(store, "collection_name", None)
        if client is None or not collection_name:
            counts[item_type] = None
            continue
        try:
            counts[item_type] = int(client.count(collection_name=collection_name, exact=True).count)
        except Exception:
            counts[item_type] = None
    return counts


def _preview(text: str, limit: int) -> str:
    compact = " ".join(str(text or "").split())
    if len(compact) <= limit:
        return compact
    return compact[: max(0, limit - 3)] + "..."


def _compact_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _configure_logging(run_dir: Path) -> logging.Logger:
    logger = logging.getLogger("longmemeval_dialogue_smoke")
    logger.setLevel(logging.INFO)
    logger.handlers.clear()
    formatter = logging.Formatter("%(asctime)s | %(levelname)s | %(message)s", datefmt="%H:%M:%S")
    stream_handler = logging.StreamHandler(sys.stdout)
    stream_handler.setFormatter(formatter)
    file_handler = logging.FileHandler(run_dir / "run.log", encoding="utf-8")
    file_handler.setFormatter(formatter)
    logger.addHandler(stream_handler)
    logger.addHandler(file_handler)
    return logger


if __name__ == "__main__":
    main()
