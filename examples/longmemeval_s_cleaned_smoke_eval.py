from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from comgmem import Memory
from comgmem.config import MemoryConfig


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_FIXTURE = PROJECT_ROOT / "examples" / "longmemeval_s_cleaned_smoke_1.json"
DEFAULT_CONFIG = PROJECT_ROOT / "configs" / "default.yaml"
DEFAULT_RUN_DIR = PROJECT_ROOT / "runs" / "longmemeval_s_cleaned_smoke_1_eval"
DEFAULT_NAMESPACE_PREFIX = "longmemeval_s_cleaned_smoke_1"


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Run ComGMem end-to-end on longmemeval_s_cleaned_smoke_1.json: "
            "build memory from all haystack sessions, then query each question."
        )
    )
    parser.add_argument("--fixture", type=Path, default=DEFAULT_FIXTURE)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--run-dir", type=Path, default=DEFAULT_RUN_DIR)
    parser.add_argument("--namespace-prefix", default=DEFAULT_NAMESPACE_PREFIX)
    parser.add_argument("--top-k", type=int, default=10)
    parser.add_argument("--max-samples", type=int, default=None, help="Limit samples for a quick local check.")
    parser.add_argument("--max-sessions", type=int, default=None, help="Limit sessions per sample.")
    parser.add_argument("--max-pairs", type=int, default=None, help="Limit user/assistant pairs per session.")
    parser.add_argument("--reuse-existing", action="store_true", help="Skip ingestion and only run queries.")
    parser.add_argument(
        "--resume",
        action="store_true",
        help="Continue an interrupted run by skipping pairs already recorded in SQLite turns.",
    )
    parser.add_argument("--no-reset", action="store_true", help="Do not clear namespaces before ingestion.")
    parser.add_argument(
        "--disable-vector",
        action="store_true",
        help="Disable embedding/Qdrant indexing and use SQLite FTS + graph retrieval only.",
    )
    parser.add_argument("--log-every", type=int, default=1, help="Log every N ingested pairs.")
    args = parser.parse_args()

    run_dir = args.run_dir.resolve()
    run_dir.mkdir(parents=True, exist_ok=True)
    logger = _configure_logging(run_dir)

    fixture = args.fixture.resolve()
    samples = _load_samples(fixture)
    if args.max_samples is not None:
        samples = samples[: args.max_samples]
    if not samples:
        raise ValueError(f"No samples found in fixture: {fixture}")

    config = MemoryConfig.load(args.config.resolve())
    config.storage.path = str(run_dir / "memory.sqlite3")
    config.index.vector_store.path = str(run_dir / "vector_index")
    if args.disable_vector:
        config.index.use_embedding = False
        config.index.vector = "none"

    config_path = run_dir / "effective_config.json"
    config_path.write_text(json.dumps(config.model_dump(mode="json"), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    logger.info("fixture=%s", fixture)
    logger.info("run_dir=%s", run_dir)
    logger.info("samples=%s top_k=%s reuse_existing=%s", len(samples), args.top_k, args.reuse_existing)
    logger.info("llm_model=%s embedding_model=%s vector_enabled=%s", _model_name(config.llm), _model_name(config.embedding), config.index.use_embedding)

    memory = Memory(config)
    started_all = time.perf_counter()
    summaries: list[dict[str, Any]] = []
    try:
        for sample_index, sample in enumerate(samples, start=1):
            summary = _run_sample(
                memory,
                sample,
                sample_index=sample_index,
                sample_count=len(samples),
                namespace_prefix=args.namespace_prefix,
                top_k=args.top_k,
                max_sessions=args.max_sessions,
                max_pairs=args.max_pairs,
                reuse_existing=args.reuse_existing,
                resume=args.resume,
                reset=not args.no_reset and not args.resume,
                log_every=max(1, args.log_every),
                logger=logger,
            )
            summaries.append(summary)
            _write_json(run_dir / f"{summary['question_id']}.summary.json", summary)
            _append_jsonl(run_dir / "results.jsonl", summary)
    finally:
        memory.close()

    report = {
        "fixture": str(fixture),
        "run_dir": str(run_dir),
        "config": str(config_path),
        "sample_count": len(summaries),
        "elapsed_sec": round(time.perf_counter() - started_all, 3),
        "metrics": _aggregate_metrics(summaries),
        "summaries": summaries,
    }
    _write_json(run_dir / "summary.json", report)
    logger.info("done elapsed=%.1fs summary=%s", report["elapsed_sec"], run_dir / "summary.json")


def _run_sample(
    memory: Memory,
    sample: dict[str, Any],
    *,
    sample_index: int,
    sample_count: int,
    namespace_prefix: str,
    top_k: int,
    max_sessions: int | None,
    max_pairs: int | None,
    reuse_existing: bool,
    resume: bool,
    reset: bool,
    log_every: int,
    logger: logging.Logger,
) -> dict[str, Any]:
    question_id = str(sample.get("question_id") or f"sample_{sample_index}")
    namespace = f"{namespace_prefix}:{question_id}"
    sessions = _sessions(sample)
    if max_sessions is not None:
        sessions = sessions[:max_sessions]

    total_pairs = sum(len(_conversation_pairs(session["messages"], max_pairs=max_pairs)) for session in sessions)
    logger.info(
        "[%s/%s] question_id=%s sessions=%s pairs=%s namespace=%s",
        sample_index,
        sample_count,
        question_id,
        len(sessions),
        total_pairs,
        namespace,
    )

    ingested_pairs = 0
    skipped_pairs = 0
    completed_pair_keys = _completed_pair_keys(memory, namespace) if resume and not reuse_existing else set()
    ingest_started = time.perf_counter()
    if not reuse_existing:
        if reset:
            logger.info("[%s] reset namespace", question_id)
            memory.reset(namespace)
        elif resume:
            logger.info("[%s] resume namespace with %s completed pair keys", question_id, len(completed_pair_keys))

        for session_index, session in enumerate(sessions, start=1):
            pairs = _conversation_pairs(session["messages"], max_pairs=max_pairs)
            logger.info(
                "[%s] ingest session %s/%s session_id=%s date=%s pairs=%s",
                question_id,
                session_index,
                len(sessions),
                session["session_id"],
                session["date"],
                len(pairs),
            )
            for pair_index, pair in enumerate(pairs, start=1):
                pair_key = (session["session_id"], pair_index - 1)
                if pair_key in completed_pair_keys:
                    skipped_pairs += 1
                    if skipped_pairs % log_every == 0 or skipped_pairs == len(completed_pair_keys):
                        logger.info(
                            "[%s] skip completed pair session=%s pair=%s/%s skipped=%s",
                            question_id,
                            session_index,
                            pair_index,
                            len(pairs),
                            skipped_pairs,
                        )
                    continue
                pair_started = time.perf_counter()
                memory.add_memory(
                    user_input=pair.get("user"),
                    assistant_output=pair.get("assistant"),
                    namespace=namespace,
                    metadata={
                        "benchmark": "longmemeval",
                        "question_id": question_id,
                        "question_type": sample.get("question_type"),
                        "question_date": sample.get("question_date"),
                        "answer_session_ids": sample.get("answer_session_ids", []),
                        "source_session_id": session["session_id"],
                        "source_session_index": session_index - 1,
                        "source_pair_index": pair_index - 1,
                        "date": session["date"],
                    },
                )
                ingested_pairs += 1
                completed_total = skipped_pairs + ingested_pairs
                remaining_pairs = max(0, total_pairs - completed_total)
                if ingested_pairs % log_every == 0 or completed_total == total_pairs:
                    logger.info(
                        (
                            "[%s] pair done total_progress=%s/%s "
                            "new_this_run=%s skipped=%s remaining=%s session=%s pair=%s/%s elapsed=%.1fs"
                        ),
                        question_id,
                        completed_total,
                        total_pairs,
                        ingested_pairs,
                        skipped_pairs,
                        remaining_pairs,
                        session_index,
                        pair_index,
                        len(pairs),
                        time.perf_counter() - pair_started,
                    )
    else:
        logger.info("[%s] reuse_existing=true, skip memory build", question_id)

    _ensure_turn_counter(memory, namespace)
    stats_after_ingest = memory.stats(namespace)
    logger.info("[%s] memory stats after ingest: %s", question_id, _compact_json(stats_after_ingest))

    query = str(sample.get("question") or "")
    logger.info("[%s] query start top_k=%s question=%s", question_id, top_k, query)
    query_started = time.perf_counter()
    results = memory.search(query, namespace=namespace, top_k=top_k)
    logger.info("[%s] query done results=%s elapsed=%.1fs", question_id, len(results), time.perf_counter() - query_started)
    for rank, result in enumerate(results, start=1):
        logger.info(
            "[%s] result #%s score=%.4f id=%s preview=%s",
            question_id,
            rank,
            float(result.get("score", 0.0)),
            result.get("id"),
            _single_line(str(result.get("content", "")), 180),
        )

    expected_answer = str(sample.get("answer") or "")
    turn_session_ids = _turn_session_ids(memory, namespace)
    top_results = [
        _result_summary(index, result, turn_session_ids=turn_session_ids)
        for index, result in enumerate(results, start=1)
    ]
    answer_session_recall = _answer_session_recall(top_results, sample.get("answer_session_ids", []))
    return {
        "question_id": question_id,
        "question_type": sample.get("question_type"),
        "namespace": namespace,
        "question": query,
        "question_date": sample.get("question_date"),
        "expected_answer": expected_answer,
        "answer_session_ids": sample.get("answer_session_ids", []),
        "session_count": len(sessions),
        "planned_pairs": total_pairs,
        "ingested_pairs": ingested_pairs,
        "skipped_pairs": skipped_pairs,
        "reuse_existing": reuse_existing,
        "resume": resume,
        "ingest_elapsed_sec": round(time.perf_counter() - ingest_started, 3),
        "stats": stats_after_ingest,
        "turn_session_ids": turn_session_ids,
        "top_results": top_results,
        "retrieval_contains_expected_answer": _contains_text(top_results, expected_answer),
        "answer_session_recall": answer_session_recall,
        "retrieval_hits_answer_session": answer_session_recall["hit"],
    }


def _load_samples(path: Path) -> list[dict[str, Any]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(payload, list):
        return [dict(item) for item in payload]
    if isinstance(payload, dict):
        return [payload]
    raise ValueError(f"Fixture must contain a JSON object or list: {path}")


def _sessions(sample: dict[str, Any]) -> list[dict[str, Any]]:
    dates = list(sample.get("haystack_dates") or [])
    session_ids = list(sample.get("haystack_session_ids") or [])
    sessions = list(sample.get("haystack_sessions") or [])
    rows: list[dict[str, Any]] = []
    for index, messages in enumerate(sessions):
        rows.append(
            {
                "date": str(dates[index]) if index < len(dates) else None,
                "session_id": str(session_ids[index]) if index < len(session_ids) else f"session_{index}",
                "messages": list(messages or []),
            }
        )
    return rows


def _conversation_pairs(messages: list[dict[str, Any]], *, max_pairs: int | None = None) -> list[dict[str, str]]:
    pairs: list[dict[str, str]] = []
    pending_user: str | None = None
    for message in messages:
        role = str(message.get("role", "user")).lower()
        content = str(message.get("content", "")).strip()
        if not content:
            continue
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
        else:
            if pending_user is None:
                pairs.append({"user": f"[{role}] {content}"})
            else:
                pending_user = f"{pending_user}\n\n[{role}] {content}"
        if max_pairs is not None and len(pairs) >= max_pairs:
            return pairs[:max_pairs]
    if pending_user is not None:
        pairs.append({"user": pending_user})
    if max_pairs is not None:
        return pairs[:max_pairs]
    return pairs


def _result_summary(rank: int, result: dict[str, Any], *, turn_session_ids: dict[str, str]) -> dict[str, Any]:
    metadata = dict(result.get("metadata") or {})
    source_turn_ids = _collect_source_turn_ids(metadata)
    source_session_ids = _source_session_ids_for_turns(source_turn_ids, turn_session_ids)
    return {
        "rank": rank,
        "id": result.get("id"),
        "score": result.get("score"),
        "content": result.get("content"),
        "edge_node_ids": metadata.get("edge_node_ids", []),
        "channels": metadata.get("channels", []),
        "hit_node_ids": metadata.get("hit_node_ids", []),
        "source_turn_ids": source_turn_ids,
        "source_session_ids": source_session_ids,
        "relative_time": metadata.get("relative_time", {}),
        "edge_metadata": metadata.get("edge_metadata", {}),
        "edge_nodes": metadata.get("edge_nodes", []),
        "periphery_edges": metadata.get("periphery_edges", []),
        "periphery_nodes": metadata.get("periphery_nodes", []),
        "score_parts": metadata.get("score_parts", {}),
    }


def _turn_session_ids(memory: Memory, namespace: str) -> dict[str, str]:
    rows = memory.store.conn.execute(
        """
        SELECT DISTINCT turn_id, turn_metadata_json
        FROM turns
        WHERE namespace = ?
        """,
        (namespace,),
    ).fetchall()
    mapping: dict[str, str] = {}
    for row in rows:
        turn_id = str(row["turn_id"])
        try:
            metadata = json.loads(row["turn_metadata_json"] or "{}")
        except json.JSONDecodeError:
            continue
        session_id = (
            metadata.get("source_session_id")
            or metadata.get("session_id")
            or metadata.get("conversation_id")
        )
        if session_id:
            mapping[turn_id] = str(session_id)
    return mapping


def _ensure_turn_counter(memory: Memory, namespace: str) -> None:
    if namespace not in memory._turn_counters:
        memory._turn_counters[namespace] = memory.store.next_turn_index(namespace)


def _answer_session_recall(
    top_results: list[dict[str, Any]],
    answer_session_ids: Any,
) -> dict[str, Any]:
    expected = _string_set(answer_session_ids)
    retrieved_by_rank: list[dict[str, Any]] = []
    hit_ranks: list[int] = []
    for result in top_results:
        rank = int(result.get("rank") or 0)
        sessions = _string_set(result.get("source_session_ids", []))
        hits = sorted(expected.intersection(sessions))
        retrieved_by_rank.append(
            {
                "rank": rank,
                "source_session_ids": sorted(sessions),
                "matched_answer_session_ids": hits,
            }
        )
        if hits and rank:
            hit_ranks.append(rank)
    retrieved = sorted({session_id for item in retrieved_by_rank for session_id in item["source_session_ids"]})
    matched = sorted(expected.intersection(retrieved))
    return {
        "answer_session_ids": sorted(expected),
        "retrieved_source_session_ids": retrieved,
        "matched_answer_session_ids": matched,
        "hit": bool(matched),
        "first_hit_rank": min(hit_ranks) if hit_ranks else None,
        "by_rank": retrieved_by_rank,
    }


def _completed_pair_keys(memory: Memory, namespace: str) -> set[tuple[str, int]]:
    rows = memory.store.conn.execute(
        """
        SELECT DISTINCT turn_metadata_json
        FROM turns
        WHERE namespace = ?
        """,
        (namespace,),
    ).fetchall()
    keys: set[tuple[str, int]] = set()
    for row in rows:
        try:
            metadata = json.loads(row["turn_metadata_json"] or "{}")
        except json.JSONDecodeError:
            continue
        session_id = metadata.get("source_session_id")
        pair_index = metadata.get("source_pair_index")
        if session_id is None or pair_index is None:
            continue
        try:
            keys.add((str(session_id), int(pair_index)))
        except (TypeError, ValueError):
            continue
    return keys


def _collect_source_turn_ids(value: Any) -> list[str]:
    collected: list[str] = []

    def visit(item: Any) -> None:
        if isinstance(item, dict):
            if "source_turn_ids" in item:
                collected.extend(_strings(item.get("source_turn_ids")))
            for child in item.values():
                visit(child)
        elif isinstance(item, list):
            for child in item:
                visit(child)

    visit(value)
    return list(dict.fromkeys(collected))


def _source_session_ids_for_turns(source_turn_ids: list[str], turn_session_ids: dict[str, str]) -> list[str]:
    return list(dict.fromkeys(turn_session_ids[turn_id] for turn_id in source_turn_ids if turn_id in turn_session_ids))


def _aggregate_metrics(summaries: list[dict[str, Any]]) -> dict[str, Any]:
    sample_count = len(summaries)
    answer_text_hits = sum(1 for item in summaries if item.get("retrieval_contains_expected_answer"))
    answer_session_hits = sum(1 for item in summaries if item.get("retrieval_hits_answer_session"))
    first_hit_ranks = [
        item["answer_session_recall"]["first_hit_rank"]
        for item in summaries
        if (item.get("answer_session_recall") or {}).get("first_hit_rank") is not None
    ]
    return {
        "sample_count": sample_count,
        "answer_text_hit_count": answer_text_hits,
        "answer_text_hit_rate": _rate(answer_text_hits, sample_count),
        "answer_session_hit_count": answer_session_hits,
        "answer_session_hit_rate": _rate(answer_session_hits, sample_count),
        "answer_session_first_hit_ranks": first_hit_ranks,
    }


def _contains_text(results: list[dict[str, Any]], needle: str) -> bool:
    normalized = needle.strip().lower()
    if not normalized:
        return False
    return any(normalized in json.dumps(result, ensure_ascii=False).lower() for result in results)


def _string_set(value: Any) -> set[str]:
    return set(_strings(value))


def _strings(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    if value in (None, "", [], {}):
        return []
    return [str(value).strip()]


def _rate(count: int, total: int) -> float:
    if total <= 0:
        return 0.0
    return round(count / total, 4)


def _configure_logging(run_dir: Path) -> logging.Logger:
    logger = logging.getLogger("longmemeval_smoke_eval")
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


def _write_json(path: Path, payload: Any) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _append_jsonl(path: Path, payload: Any) -> None:
    with path.open("a", encoding="utf-8") as file:
        file.write(json.dumps(payload, ensure_ascii=False) + "\n")


def _model_name(config: Any) -> str | None:
    return getattr(config, "model", None) if config is not None else None


def _compact_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _single_line(value: str, limit: int) -> str:
    normalized = " ".join(value.split())
    if len(normalized) <= limit:
        return normalized
    return normalized[: limit - 3] + "..."


if __name__ == "__main__":
    main()
