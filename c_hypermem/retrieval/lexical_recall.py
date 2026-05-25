from __future__ import annotations

from dataclasses import dataclass

from c_hypermem.config import RetrievalConfig
from c_hypermem.schema import MemoryNode
from c_hypermem.stores.base import MemoryStore


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
