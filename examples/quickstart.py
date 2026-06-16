from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from comgmem import Memory
from comgmem.config import MemoryConfig, ModelConfig
from comgmem.embeddings import EmbeddingModelClient
from comgmem.llms.openai_compatible import OpenAICompatibleLLM
from comgmem.pipeline.extraction import LLMMemoryExtractor


DB_PATH = PROJECT_ROOT / "runs" / "quickstart.sqlite3"
NAMESPACE = "quickstart"


class LoggingLLM:
    def __init__(self, config: ModelConfig) -> None:
        self.config = config
        self.client = OpenAICompatibleLLM(config)
        self.calls: list[dict[str, Any]] = []

    def generate_json(self, prompt: str) -> dict[str, Any]:
        call_no = len(self.calls) + 1
        started = time.perf_counter()
        print(f"[llm:{call_no}] model={self.config.model} prompt_chars={len(prompt)}")
        payload = self.client.generate_json(prompt)
        elapsed = time.perf_counter() - started
        keys = sorted(payload) if isinstance(payload, dict) else []
        self.calls.append({"kind": "json", "prompt_chars": len(prompt), "elapsed_sec": elapsed, "keys": keys})
        print(f"[llm:{call_no}] ok elapsed_sec={elapsed:.2f} keys={keys}")
        return payload


class LoggingEmbeddingClient:
    def __init__(self, config: ModelConfig) -> None:
        self.config = config
        self.client = EmbeddingModelClient(config)
        self.calls: list[dict[str, Any]] = []

    def embed(self, texts: list[str]) -> list[list[float]]:
        call_no = len(self.calls) + 1
        chars = sum(len(text) for text in texts)
        started = time.perf_counter()
        print(f"[embedding:{call_no}] model={self.config.model} texts={len(texts)} chars={chars}")
        vectors = self.client.embed(texts)
        elapsed = time.perf_counter() - started
        dims = len(vectors[0]) if vectors else 0
        self.calls.append({"texts": len(texts), "chars": chars, "elapsed_sec": elapsed, "dims": dims})
        print(f"[embedding:{call_no}] ok elapsed_sec={elapsed:.2f} dims={dims}")
        return vectors


def main() -> None:
    config = _quickstart_config()
    llm = LoggingLLM(_required_model(config.llm, "llm"))
    embedding = LoggingEmbeddingClient(_required_model(config.embedding, "embedding"))
    memory: Memory | None = None
    started = time.perf_counter()

    try:
        print("[quickstart] starting local smoke test")
        print(f"[quickstart] sqlite={DB_PATH}")
        print(f"[quickstart] namespace={NAMESPACE}")

        extractor = LLMMemoryExtractor(config, llm=llm)
        memory = Memory.from_config(
            config,
            extractor=extractor,
            maintenance_llm=llm,
            embedding_client=embedding,
        )
        memory.reset(NAMESPACE)

        print("[quickstart] add_memory")
        memory.add_memory(
            user_input="Alice prefers morning interviews and wants reminders to be concise.",
            assistant_output="I will remember Alice's interview timing and reminder preference.",
            namespace=NAMESPACE,
            metadata={"session_id": "quickstart-session", "date": "2026-05-28"},
        )

        print("[quickstart] search")
        results = memory.search("What interview timing does Alice prefer?", namespace=NAMESPACE, top_k=3)
        stats = memory.stats(NAMESPACE)

        print("[quickstart] stats")
        print(json.dumps(stats, ensure_ascii=False, indent=2, sort_keys=True))
        print("[quickstart] search_results")
        print(json.dumps(_compact_results(results), ensure_ascii=False, indent=2))
        print("[quickstart] model_calls")
        print(
            json.dumps(
                {
                    "llm_calls": len(llm.calls),
                    "embedding_calls": len(embedding.calls),
                    "llm": llm.calls,
                    "embedding": embedding.calls,
                    "elapsed_sec": round(time.perf_counter() - started, 2),
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        print("[quickstart] all checks passed; model, embedding, storage, indexing, and retrieval configs look OK.")
    finally:
        if memory is not None:
            memory.close()
        _delete_sqlite_files(DB_PATH)
        print(f"[quickstart] deleted {DB_PATH}")


def _quickstart_config() -> MemoryConfig:
    config = MemoryConfig.load(PROJECT_ROOT / "configs" / "default.yaml")
    data = config.model_dump(mode="json")
    data["storage"]["path"] = str(DB_PATH)
    data["index"]["vector_store"]["collection_name"] = "comgmem_quickstart"
    return MemoryConfig.model_validate(data)


def _required_model(config: ModelConfig | None, name: str) -> ModelConfig:
    if config is None:
        raise RuntimeError(f"quickstart requires {name} config.")
    missing = [
        field
        for field in ("model", "api_key")
        if not getattr(config, field, None) or str(getattr(config, field)).startswith("${")
    ]
    if missing:
        joined = ", ".join(missing)
        raise RuntimeError(f"quickstart requires resolved {name} config fields: {joined}. Check .env.")
    return config


def _compact_results(results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    compacted = []
    for result in results:
        compacted.append(
            {
                "score": result.get("score"),
                "content": result.get("content"),
                "context": result.get("context"),
                "metadata": {
                    "edge_id": result.get("metadata", {}).get("edge_id"),
                    "node_count": len(result.get("metadata", {}).get("edge_nodes", [])),
                },
            }
        )
    return compacted


def _delete_sqlite_files(path: Path) -> None:
    for candidate in (path, path.with_name(f"{path.name}-wal"), path.with_name(f"{path.name}-shm")):
        if candidate.exists():
            candidate.unlink()


if __name__ == "__main__":
    main()
