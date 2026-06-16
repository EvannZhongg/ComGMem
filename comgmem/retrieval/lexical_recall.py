from __future__ import annotations

from dataclasses import dataclass

from comgmem.config import RetrievalConfig
from comgmem.schema import MemoryNode
from comgmem.stores.base import MemoryStore


@dataclass(frozen=True)
class LexicalNodeHit:
    node: MemoryNode
    score: float


class SQLiteFTSRecall:
    """Lexical node recall backed by SQLite FTS."""

    def __init__(self, store: MemoryStore, config: RetrievalConfig) -> None:
        self.store = store
        self.config = config

    def recall(self, *, namespace: str, query: str) -> list[LexicalNodeHit]:
        return [
            LexicalNodeHit(node=node, score=score)
            for node, score in self.store.search_nodes_fts(namespace, query, self.config.lexical_top_k)
        ]
