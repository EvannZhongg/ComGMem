from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol, Sequence
from uuid import NAMESPACE_URL, uuid5

from c_hypermem.errors import ConfigError, StoreError
from c_hypermem.schema import EdgeCluster, LocalTriple, MemoryNode, Message


_QDRANT_CLIENTS: dict[Path, Any] = {}


@dataclass(frozen=True)
class VectorRecord:
    id: str
    vector: list[float]
    text: str
    payload: dict[str, Any]


@dataclass(frozen=True)
class VectorIndexItem:
    id: str
    text: str
    payload: dict[str, Any]


NodeLocalGraphIndexItem = VectorIndexItem


class VectorStore(Protocol):
    """Dense index abstraction for rebuildable memory indexes."""

    def upsert(self, records: Sequence[VectorRecord]) -> None: ...

    def delete(self, ids: Sequence[str]) -> None: ...

    def delete_namespace(self, namespace: str) -> None: ...

    def close(self) -> None: ...


class QdrantVectorStore:
    """Qdrant-backed vector store using local embedded storage by default."""

    def __init__(self, *, path: str | Path, collection_name: str) -> None:
        self.path = Path(path)
        self.collection_name = collection_name
        self._client: Any | None = None
        self._owns_client = True
        self._vector_size: int | None = None

    @classmethod
    def with_client(cls, *, path: str | Path, collection_name: str, client: Any) -> "QdrantVectorStore":
        store = cls(path=path, collection_name=collection_name)
        store._client = client
        store._owns_client = False
        return store

    @property
    def client(self) -> Any:
        if self._client is None:
            try:
                from qdrant_client import QdrantClient
            except ImportError as exc:
                raise ConfigError("Install c-hypermem[vector] to use the Qdrant vector store.") from exc
            self.path.mkdir(parents=True, exist_ok=True)
            resolved_path = self.path.resolve()
            self._client = _QDRANT_CLIENTS.get(resolved_path)
            if self._client is None:
                self._client = QdrantClient(path=str(resolved_path))
                _QDRANT_CLIENTS[resolved_path] = self._client
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

    def delete(self, ids: Sequence[str]) -> None:
        if not ids:
            return
        if self._client is None and not self.path.exists():
            return
        if not self._collection_exists():
            return
        try:
            from qdrant_client.http.models import PointIdsList

            self.client.delete(
                collection_name=self.collection_name,
                points_selector=PointIdsList(points=list(dict.fromkeys(ids))),
                wait=True,
            )
        except Exception as exc:
            raise StoreError(f"Failed to delete {len(ids)} Qdrant vector record(s).") from exc

    def close(self) -> None:
        if self._client is None:
            return
        if not self._owns_client:
            self._client = None
            return
        close = getattr(self._client, "close", None)
        if callable(close):
            close()
        _QDRANT_CLIENTS.pop(self.path.resolve(), None)
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


def node_local_graph_embedding_text(node: MemoryNode) -> str:
    """Return the node-level text sent to the embedding model for local graph recall."""

    lines: list[str] = []
    content = node.content.strip()
    if content:
        lines.append(f"Core content: {content}")

    triples = [triple for triple in node.local_graph.triples if triple.triple_id is not None]
    if triples:
        lines.append("Related facts:")
        for triple in triples:
            text = triple_embedding_text(triple)
            if text:
                lines.append(f"- {text}")

    return "\n".join(lines).strip()


def collect_node_local_graph_index_items(nodes: Sequence[MemoryNode]) -> list[NodeLocalGraphIndexItem]:
    items: list[NodeLocalGraphIndexItem] = []
    for node in nodes:
        if not node.local_graph.triples:
            continue
        triples = [triple for triple in node.local_graph.triples if triple.triple_id is not None]
        if not triples:
            continue
        text = node_local_graph_embedding_text(node)
        if not text:
            continue
        payload = {
            "namespace": node.namespace,
            "item_type": "node_local_graph",
            "node_id": node.node_id,
            "node_labels": node.node_labels,
            "node_status": node.status,
            "canonical_text": node.canonical_text,
            "normalized_text": node.normalized_text,
            "fingerprint": node.fingerprint,
            "triple_ids": [triple.triple_id for triple in triples],
            "triple_count": len(triples),
            "triples": [
                {
                    "triple_id": triple.triple_id,
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
                }
                for triple in triples
            ],
            "attributes": node.attributes,
            "node_content": node.content,
            "node_summary": node.summary,
            "node_time": node.time.model_dump(mode="json"),
            "node_metadata": node.metadata,
        }
        items.append(
            NodeLocalGraphIndexItem(
                id=make_vector_point_id(node.namespace, "triple", node.node_id),
                text=text,
                payload={key: value for key, value in payload.items() if value is not None},
            )
        )
    return items


def collect_node_content_index_items(nodes: Sequence[MemoryNode]) -> list[VectorIndexItem]:
    return [
        VectorIndexItem(
            id=make_vector_point_id(node.namespace, "node_content", node.node_id),
            text=node.content,
            payload=_node_payload(node, "node_content"),
        )
        for node in nodes
        if node.content.strip()
    ]


def collect_node_summary_index_items(nodes: Sequence[MemoryNode]) -> list[VectorIndexItem]:
    return [
        VectorIndexItem(
            id=make_vector_point_id(node.namespace, "node_summary", node.node_id),
            text=node.summary,
            payload=_node_payload(node, "node_summary"),
        )
        for node in nodes
        if node.summary.strip()
    ]


def collect_edge_cluster_canonical_index_items(clusters: Sequence[EdgeCluster]) -> list[VectorIndexItem]:
    return [
        VectorIndexItem(
            id=make_vector_point_id(cluster.namespace, "edge_cluster_canonical", cluster.cluster_id),
            text=cluster.canonical_description,
            payload=_cluster_payload(cluster, "edge_cluster_canonical"),
        )
        for cluster in clusters
        if cluster.canonical_description.strip()
    ]


def collect_edge_cluster_variant_index_items(clusters: Sequence[EdgeCluster]) -> list[VectorIndexItem]:
    items: list[VectorIndexItem] = []
    for cluster in clusters:
        seen: set[str] = set()
        for index, variant in enumerate(cluster.description_variants):
            text = variant.text.strip()
            if not text or text in seen:
                continue
            seen.add(text)
            variant_id = f"{cluster.cluster_id}:variant:{index}:{variant.source_edge_id or ''}:{text}"
            payload = _cluster_payload(
                cluster,
                "edge_cluster_variant",
                {
                    "variant_index": index,
                    "source_edge_id": variant.source_edge_id,
                },
            )
            items.append(
                VectorIndexItem(
                    id=make_vector_point_id(cluster.namespace, "edge_cluster_variant", variant_id),
                    text=text,
                    payload={key: value for key, value in payload.items() if value is not None},
                )
            )
    return items


def collect_turn_dialogue_index_item(
    *,
    namespace: str,
    turn_id: str,
    turn_index: int,
    messages: Sequence[Message],
    metadata: dict[str, Any],
) -> VectorIndexItem | None:
    text = turn_dialogue_embedding_text(messages)
    if not text:
        return None
    payload = {
        "namespace": namespace,
        "item_type": "turn_dialogue",
        "turn_id": turn_id,
        "turn_index": turn_index,
        "roles": [
            _turn_dialogue_role_label(message.role)
            for message in messages
            if _turn_dialogue_role_label(message.role) is not None and message.content.strip()
        ],
        "turn_metadata": metadata,
    }
    return VectorIndexItem(
        id=make_vector_point_id(namespace, "turn_dialogue", turn_id),
        text=text,
        payload={key: value for key, value in payload.items() if value is not None},
    )


def turn_dialogue_embedding_text(messages: Sequence[Message]) -> str:
    lines: list[str] = []
    for message in messages:
        label = _turn_dialogue_role_label(message.role)
        content = message.content.strip()
        if label is None or not content:
            continue
        lines.append(f"{label}: {content}")
    return "\n".join(lines)


def make_vector_point_id(namespace: str, item_type: str, item_id: str | None = None) -> str:
    if item_id is None:
        item_id = item_type
        item_type = "triple"
    return str(uuid5(NAMESPACE_URL, f"c-hypermem:{namespace}:{item_type}:{item_id}"))


def _node_payload(node: MemoryNode, item_type: str) -> dict[str, Any]:
    payload = {
        "namespace": node.namespace,
        "item_type": item_type,
        "node_id": node.node_id,
        "node_labels": node.node_labels,
        "node_status": node.status,
        "canonical_text": node.canonical_text,
        "normalized_text": node.normalized_text,
        "fingerprint": node.fingerprint,
        "superseded_by": node.superseded_by,
        "invalidated_by": node.invalidated_by,
        "content": node.content,
        "summary": node.summary,
        "attributes": node.attributes,
        "node_time": node.time.model_dump(mode="json"),
        "node_metadata": node.metadata,
    }
    return {key: value for key, value in payload.items() if value is not None}


def _cluster_payload(cluster: EdgeCluster, item_type: str, extra: dict[str, Any] | None = None) -> dict[str, Any]:
    payload = {
        "namespace": cluster.namespace,
        "item_type": item_type,
        "cluster_id": cluster.cluster_id,
        "cluster_fingerprint": cluster.cluster_fingerprint,
        "canonical_description": cluster.canonical_description,
        "cluster_labels": cluster.cluster_labels,
        "aliases": cluster.aliases,
        "conflict_state": cluster.conflict_state,
        "status": cluster.status,
        "cluster_metadata": cluster.metadata,
    }
    if extra:
        payload.update(extra)
    return {key: value for key, value in payload.items() if value is not None}


def _turn_dialogue_role_label(role: str) -> str | None:
    normalized = role.strip().lower()
    if normalized == "user":
        return "User"
    if normalized == "assistant":
        return "Assistant"
    return None


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
