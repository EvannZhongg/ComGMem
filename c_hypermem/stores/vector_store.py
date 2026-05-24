from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol, Sequence
from uuid import NAMESPACE_URL, uuid5

from c_hypermem.errors import ConfigError, StoreError
from c_hypermem.schema import LocalTriple, MemoryNode


@dataclass(frozen=True)
class VectorRecord:
    id: str
    vector: list[float]
    text: str
    payload: dict[str, Any]


@dataclass(frozen=True)
class TripleIndexItem:
    id: str
    text: str
    payload: dict[str, Any]


class VectorStore(Protocol):
    """Dense index abstraction for rebuildable memory indexes."""

    def upsert(self, records: Sequence[VectorRecord]) -> None: ...

    def delete_namespace(self, namespace: str) -> None: ...

    def close(self) -> None: ...


class QdrantVectorStore:
    """Qdrant-backed vector store using local embedded storage by default."""

    def __init__(self, *, path: str | Path, collection_name: str) -> None:
        self.path = Path(path)
        self.collection_name = collection_name
        self._client: Any | None = None
        self._vector_size: int | None = None

    @property
    def client(self) -> Any:
        if self._client is None:
            try:
                from qdrant_client import QdrantClient
            except ImportError as exc:
                raise ConfigError("Install c-hypermem[vector] to use the Qdrant vector store.") from exc
            self.path.mkdir(parents=True, exist_ok=True)
            self._client = QdrantClient(path=str(self.path))
        return self._client

    def upsert(self, records: Sequence[VectorRecord]) -> None:
        if not records:
            return
        vector_size = len(records[0].vector)
        if vector_size <= 0:
            raise StoreError("Cannot upsert an empty embedding vector into Qdrant.")
        if any(len(record.vector) != vector_size for record in records):
            raise StoreError("Qdrant upsert received vectors with inconsistent dimensions.")

        self._ensure_collection(vector_size)
        try:
            from qdrant_client.http.models import PointStruct

            points = [
                PointStruct(
                    id=record.id,
                    vector=record.vector,
                    payload={**record.payload, "text": record.text},
                )
                for record in records
            ]
            self.client.upsert(collection_name=self.collection_name, points=points, wait=True)
        except Exception as exc:
            raise StoreError(f"Failed to upsert {len(records)} vector record(s) into Qdrant.") from exc

    def delete_namespace(self, namespace: str) -> None:
        if self._client is None and not self.path.exists():
            return
        if not self._collection_exists():
            return
        try:
            from qdrant_client.http.models import FieldCondition, Filter, FilterSelector, MatchValue

            self.client.delete(
                collection_name=self.collection_name,
                points_selector=FilterSelector(
                    filter=Filter(
                        must=[
                            FieldCondition(
                                key="namespace",
                                match=MatchValue(value=namespace),
                            )
                        ]
                    )
                ),
                wait=True,
            )
        except Exception as exc:
            raise StoreError(f"Failed to delete Qdrant vectors for namespace {namespace!r}.") from exc

    def close(self) -> None:
        if self._client is None:
            return
        close = getattr(self._client, "close", None)
        if callable(close):
            close()
        self._client = None

    def _ensure_collection(self, vector_size: int) -> None:
        if self._vector_size == vector_size:
            return
        collection = self._get_collection()
        if collection is None:
            try:
                from qdrant_client.http.models import Distance, VectorParams

                self.client.create_collection(
                    collection_name=self.collection_name,
                    vectors_config=VectorParams(size=vector_size, distance=Distance.COSINE),
                )
            except Exception as exc:
                raise StoreError(f"Failed to create Qdrant collection {self.collection_name!r}.") from exc
            self._vector_size = vector_size
            return

        existing_size = _collection_vector_size(collection)
        if existing_size is not None and existing_size != vector_size:
            raise StoreError(
                "Qdrant collection vector size mismatch: "
                f"collection={self.collection_name!r} existing={existing_size} incoming={vector_size}"
            )
        self._vector_size = vector_size

    def _collection_exists(self) -> bool:
        return self._get_collection() is not None

    def _get_collection(self) -> Any | None:
        client = self.client
        try:
            return client.get_collection(self.collection_name)
        except Exception:
            return None


def triple_embedding_text(triple: LocalTriple) -> str:
    """Return the exact text sent to the embedding model for one triple."""

    return " ".join(part for part in [triple.subject, triple.predicate, triple.object] if part).strip()


def collect_triple_index_items(nodes: Sequence[MemoryNode]) -> list[TripleIndexItem]:
    items: list[TripleIndexItem] = []
    for node in nodes:
        for triple in node.local_graph.triples:
            if triple.triple_id is None:
                continue
            text = triple_embedding_text(triple)
            payload = {
                "namespace": node.namespace,
                "item_type": "triple",
                "triple_id": triple.triple_id,
                "owner_node_id": node.node_id,
                "owner_node_labels": node.node_labels,
                "owner_node_status": node.status,
                "subject": triple.subject,
                "predicate": triple.predicate,
                "object": triple.object,
                "status": triple.status,
                "scope_edge_id": triple.scope_edge_id,
                "scope_cluster_id": triple.scope_cluster_id,
                "role_in_edge": triple.role_in_edge,
                "edge_relation": triple.edge_relation,
                "superseded_by": triple.superseded_by,
                "invalidated_by": triple.invalidated_by,
                "qualifiers": triple.qualifiers,
                "node_content": node.content,
                "node_summary": node.summary,
                "node_time": node.time.model_dump(mode="json"),
                "node_metadata": node.metadata,
            }
            items.append(
                TripleIndexItem(
                    id=make_vector_point_id(node.namespace, triple.triple_id),
                    text=text,
                    payload={key: value for key, value in payload.items() if value is not None},
                )
            )
    return items


def make_vector_point_id(namespace: str, item_id: str) -> str:
    return str(uuid5(NAMESPACE_URL, f"c-hypermem:{namespace}:{item_id}"))


def _collection_vector_size(collection: Any) -> int | None:
    config = getattr(collection, "config", None)
    params = getattr(config, "params", None)
    vectors = getattr(params, "vectors", None)
    if hasattr(vectors, "size"):
        return int(vectors.size)
    if isinstance(vectors, dict) and vectors:
        first = next(iter(vectors.values()))
        if hasattr(first, "size"):
            return int(first.size)
    return None
