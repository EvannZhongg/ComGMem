from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from c_hypermem.errors import StoreError
from c_hypermem.schema import (
    EdgeCluster,
    EdgeClusterMember,
    EntityAliasIndexEntry,
    HyperEdge,
    LocalNodeGraph,
    Message,
    MemoryNode,
    TimeBundle,
)
from c_hypermem.utils.ids import make_local_triple_id, make_member_signature
from c_hypermem.utils.time import utc_now_iso
from c_hypermem.utils.text import tokenize


class SQLiteStore:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(self.path))
        self.conn.row_factory = sqlite3.Row
        self._init_schema()

    def reset_namespace(self, namespace: str) -> None:
        with self.conn:
            for table in [
                "triples",
                "edge_cluster_members",
                "edge_clusters",
                "hyper_edge_members",
                "hyper_edges",
                "nodes_fts",
                "nodes",
                "entity_alias_index",
                "turns",
            ]:
                self.conn.execute(f"DELETE FROM {table} WHERE namespace = ?", (namespace,))

    def upsert_nodes(self, nodes: list[MemoryNode]) -> None:
        with self.conn:
            for node in nodes:
                for triple in node.local_graph.triples:
                    if triple.triple_id is None:
                        triple.triple_id = make_local_triple_id(
                            node.namespace,
                            node.node_id,
                            triple.subject,
                            triple.predicate,
                            triple.object,
                            triple.qualifiers,
                        )
                self.conn.execute(
                    """
                    INSERT INTO nodes (
                        namespace, node_id, canonical_text, normalized_text, fingerprint,
                        node_labels_json, status, superseded_by, invalidated_by,
                        status_reason, status_updated_at, content, summary, attributes_json,
                        absolute_time_json, relative_time_json, local_graph_json, metadata_json
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(namespace, node_id) DO UPDATE SET
                        canonical_text = excluded.canonical_text,
                        normalized_text = excluded.normalized_text,
                        fingerprint = excluded.fingerprint,
                        node_labels_json = excluded.node_labels_json,
                        status = excluded.status,
                        superseded_by = excluded.superseded_by,
                        invalidated_by = excluded.invalidated_by,
                        status_reason = excluded.status_reason,
                        status_updated_at = excluded.status_updated_at,
                        content = excluded.content,
                        summary = excluded.summary,
                        attributes_json = excluded.attributes_json,
                        absolute_time_json = excluded.absolute_time_json,
                        relative_time_json = excluded.relative_time_json,
                        local_graph_json = excluded.local_graph_json,
                        metadata_json = excluded.metadata_json
                    """,
                    (
                        node.namespace,
                        node.node_id,
                        node.canonical_text,
                        node.normalized_text,
                        node.fingerprint,
                        _to_json(node.node_labels),
                        node.status,
                        node.superseded_by,
                        node.invalidated_by,
                        node.status_reason,
                        node.status_updated_at,
                        node.content,
                        node.summary,
                        _to_json(node.attributes),
                        _to_json(node.time.world),
                        _to_json(
                            {
                                "lifecycle": node.time.lifecycle.model_dump(mode="json"),
                                "activation": node.time.activation.model_dump(mode="json"),
                            }
                        ),
                        _to_json(node.local_graph),
                        _to_json(node.metadata),
                    ),
                )
                self.conn.execute(
                    "DELETE FROM nodes_fts WHERE namespace = ? AND node_id = ?",
                    (node.namespace, node.node_id),
                )
                if node.status == "active":
                    self.conn.execute(
                        """
                        INSERT INTO nodes_fts(namespace, node_id, content, summary, local_graph)
                        VALUES (?, ?, ?, ?, ?)
                        """,
                        (
                            node.namespace,
                            node.node_id,
                            node.content,
                            node.summary,
                            _local_graph_fts_text(node),
                        ),
                    )
                self.conn.execute(
                    "DELETE FROM triples WHERE namespace = ? AND owner_node_id = ?",
                    (node.namespace, node.node_id),
                )
                for triple in node.local_graph.triples:
                    self.conn.execute(
                        """
                        INSERT INTO triples (
                            namespace, triple_id, owner_node_id, subject, predicate, object,
                            status, scope_edge_id, scope_cluster_id,
                            superseded_by, invalidated_by, qualifiers_json, metadata_json
                        )
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        ON CONFLICT(namespace, triple_id) DO UPDATE SET
                            owner_node_id = excluded.owner_node_id,
                            subject = excluded.subject,
                            predicate = excluded.predicate,
                            object = excluded.object,
                            status = excluded.status,
                            scope_edge_id = excluded.scope_edge_id,
                            scope_cluster_id = excluded.scope_cluster_id,
                            superseded_by = excluded.superseded_by,
                            invalidated_by = excluded.invalidated_by,
                            qualifiers_json = excluded.qualifiers_json,
                            metadata_json = excluded.metadata_json
                        """,
                        (
                            node.namespace,
                            triple.triple_id,
                            node.node_id,
                            triple.subject,
                            triple.predicate,
                            triple.object,
                            triple.status,
                            triple.scope_edge_id,
                            triple.scope_cluster_id,
                            triple.superseded_by,
                            triple.invalidated_by,
                            _to_json(triple.qualifiers),
                            _to_json({}),
                        ),
                    )

    def upsert_edges(self, edges: list[HyperEdge]) -> None:
        with self.conn:
            for edge in edges:
                if not edge.member_signature:
                    edge.member_signature = make_member_signature(edge.node_ids)
                self.conn.execute(
                    """
                    INSERT INTO hyper_edges (
                        namespace, edge_id, edge_fingerprint, description,
                        status, member_policy, member_signature, member_version,
                        absolute_time_json, relative_time_json, metadata_json
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(namespace, edge_id) DO UPDATE SET
                        edge_fingerprint = excluded.edge_fingerprint,
                        description = excluded.description,
                        status = excluded.status,
                        member_policy = excluded.member_policy,
                        member_signature = excluded.member_signature,
                        member_version = excluded.member_version,
                        absolute_time_json = excluded.absolute_time_json,
                        relative_time_json = excluded.relative_time_json,
                        metadata_json = excluded.metadata_json
                    """,
                    (
                        edge.namespace,
                        edge.edge_id,
                        edge.edge_fingerprint,
                        edge.description,
                        edge.status,
                        edge.member_policy,
                        edge.member_signature,
                        edge.member_version,
                        _to_json(edge.time.world),
                        _to_json(
                            {
                                "lifecycle": edge.time.lifecycle.model_dump(mode="json"),
                                "activation": edge.time.activation.model_dump(mode="json"),
                            }
                        ),
                        _to_json(edge.metadata),
                    ),
                )
                self.conn.execute(
                    "DELETE FROM hyper_edge_members WHERE namespace = ? AND edge_id = ?",
                    (edge.namespace, edge.edge_id),
                )
                for node_id in edge.node_ids:
                    self.conn.execute(
                        """
                        INSERT INTO hyper_edge_members (namespace, edge_id, node_id, weight)
                        VALUES (?, ?, ?, ?)
                        """,
                        (
                            edge.namespace,
                            edge.edge_id,
                            node_id,
                            edge.weights.get(node_id, 1.0),
                        ),
                    )

    def upsert_edge_clusters(self, clusters: list[EdgeCluster]) -> None:
        with self.conn:
            for cluster in clusters:
                self.conn.execute(
                    """
                    INSERT INTO edge_clusters (
                        namespace, cluster_id, cluster_fingerprint, canonical_description,
                        cluster_labels_json, aliases_json, conflict_state,
                        description_variants_json, status, metadata_json
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(namespace, cluster_id) DO UPDATE SET
                        cluster_fingerprint = excluded.cluster_fingerprint,
                        canonical_description = excluded.canonical_description,
                        cluster_labels_json = excluded.cluster_labels_json,
                        aliases_json = excluded.aliases_json,
                        conflict_state = excluded.conflict_state,
                        description_variants_json = excluded.description_variants_json,
                        status = excluded.status,
                        metadata_json = excluded.metadata_json
                    """,
                    (
                        cluster.namespace,
                        cluster.cluster_id,
                        cluster.cluster_fingerprint,
                        cluster.canonical_description,
                        _to_json(cluster.cluster_labels),
                        _to_json(cluster.aliases),
                        cluster.conflict_state,
                        _to_json([variant.model_dump(mode="json") for variant in cluster.description_variants]),
                        cluster.status,
                        _to_json(cluster.metadata),
                    ),
                )

    def upsert_edge_cluster_members(self, members: list[EdgeClusterMember]) -> None:
        with self.conn:
            for member in members:
                self.conn.execute(
                    """
                    INSERT INTO edge_cluster_members (
                        namespace, cluster_id, edge_id, relation_to_cluster, status, metadata_json
                    )
                    VALUES (?, ?, ?, ?, ?, ?)
                    ON CONFLICT(namespace, cluster_id, edge_id) DO UPDATE SET
                        relation_to_cluster = excluded.relation_to_cluster,
                        status = excluded.status,
                        metadata_json = excluded.metadata_json
                    """,
                    (
                        member.namespace,
                        member.cluster_id,
                        member.edge_id,
                        member.relation_to_cluster,
                        member.status,
                        _to_json(member.metadata),
                    ),
                )

    def append_turn(
        self,
        namespace: str,
        turn_id: str,
        turn_index: int,
        messages: list[Message],
        metadata: dict[str, Any] | None = None,
    ) -> None:
        if not messages:
            return
        turn_metadata = metadata or {}
        inserted_at = utc_now_iso()
        with self.conn:
            for message_index, message in enumerate(messages):
                self.conn.execute(
                    """
                    INSERT INTO turns (
                        namespace, turn_id, turn_index, message_index, role, content,
                        timestamp, message_metadata_json, turn_metadata_json, inserted_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(namespace, turn_id, message_index) DO UPDATE SET
                        turn_index = excluded.turn_index,
                        role = excluded.role,
                        content = excluded.content,
                        timestamp = excluded.timestamp,
                        message_metadata_json = excluded.message_metadata_json,
                        turn_metadata_json = excluded.turn_metadata_json,
                        inserted_at = excluded.inserted_at
                    """,
                    (
                        namespace,
                        turn_id,
                        turn_index,
                        message_index,
                        message.role,
                        message.content,
                        message.timestamp,
                        _to_json(message.metadata),
                        _to_json(turn_metadata),
                        inserted_at,
                    ),
                )

    def list_recent_turn_messages(self, namespace: str, limit: int) -> list[Message]:
        if limit <= 0:
            return []
        rows = self.conn.execute(
            """
            SELECT *
            FROM turns
            WHERE namespace = ?
              AND turn_index IN (
                SELECT DISTINCT turn_index
                FROM turns
                WHERE namespace = ?
                ORDER BY turn_index DESC
                LIMIT ?
              )
            ORDER BY turn_index ASC, message_index ASC
            """,
            (namespace, namespace, limit),
        ).fetchall()
        return [_message_from_turn_row(row) for row in rows]

    def list_turn_messages(self, namespace: str, turn_ids: list[str]) -> list[Message]:
        if not turn_ids:
            return []
        unique_turn_ids = list(dict.fromkeys(turn_ids))
        placeholders = ",".join("?" for _ in unique_turn_ids)
        rows = self.conn.execute(
            f"""
            SELECT *
            FROM turns
            WHERE namespace = ? AND turn_id IN ({placeholders})
            ORDER BY turn_index ASC, message_index ASC
            """,
            [namespace, *unique_turn_ids],
        ).fetchall()
        return [_message_from_turn_row(row) for row in rows]

    def next_turn_index(self, namespace: str) -> int:
        row = self.conn.execute(
            "SELECT COALESCE(MAX(turn_index) + 1, 0) AS next_turn_index FROM turns WHERE namespace = ?",
            (namespace,),
        ).fetchone()
        return int(row["next_turn_index"])

    def upsert_entity_aliases(self, aliases: list[EntityAliasIndexEntry]) -> None:
        with self.conn:
            for alias in aliases:
                self.conn.execute(
                    """
                    INSERT INTO entity_alias_index (
                        namespace, normalized_alias, entity_type, node_id, source_count, updated_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?)
                    ON CONFLICT(namespace, normalized_alias, entity_type) DO UPDATE SET
                        node_id = excluded.node_id,
                        source_count = entity_alias_index.source_count + excluded.source_count,
                        updated_at = excluded.updated_at
                    """,
                    (
                        alias.namespace,
                        alias.normalized_alias,
                        alias.entity_type or "",
                        alias.node_id,
                        alias.source_count,
                        alias.updated_at or utc_now_iso(),
                    ),
                )

    def find_entity_alias(
        self,
        namespace: str,
        normalized_aliases: list[str],
        entity_type: str | None = None,
    ) -> EntityAliasIndexEntry | None:
        if not normalized_aliases:
            return None
        placeholders = ",".join("?" for _ in normalized_aliases)
        params: list[Any] = [namespace, *normalized_aliases]
        type_filter = ""
        if entity_type is not None:
            type_filter = "AND entity_type IN (?, '')"
            params.append(entity_type)
        row = self.conn.execute(
            f"""
            SELECT *
            FROM entity_alias_index
            WHERE namespace = ? AND normalized_alias IN ({placeholders}) {type_filter}
            ORDER BY source_count DESC, updated_at DESC
            LIMIT 1
            """,
            params,
        ).fetchone()
        if not row:
            return None
        return EntityAliasIndexEntry(
            namespace=row["namespace"],
            normalized_alias=row["normalized_alias"],
            entity_type=row["entity_type"] or None,
            node_id=row["node_id"],
            source_count=int(row["source_count"]),
            updated_at=row["updated_at"],
        )

    def list_nodes(self, namespace: str) -> list[MemoryNode]:
        rows = self.conn.execute(
            "SELECT * FROM nodes WHERE namespace = ? ORDER BY rowid",
            (namespace,),
        ).fetchall()
        return [_node_from_row(row) for row in rows]

    def list_edges(self, namespace: str) -> list[HyperEdge]:
        rows = self.conn.execute(
            "SELECT * FROM hyper_edges WHERE namespace = ? ORDER BY rowid",
            (namespace,),
        ).fetchall()
        return [_edge_from_row(row, _edge_members(self.conn, row["namespace"], row["edge_id"])) for row in rows]

    def get_edges(self, namespace: str, edge_ids: list[str]) -> list[HyperEdge]:
        if not edge_ids:
            return []
        placeholders = ",".join("?" for _ in edge_ids)
        rows = self.conn.execute(
            f"SELECT * FROM hyper_edges WHERE namespace = ? AND edge_id IN ({placeholders})",
            [namespace, *edge_ids],
        ).fetchall()
        by_id = {}
        for row in rows:
            edge = _edge_from_row(row, _edge_members(self.conn, row["namespace"], row["edge_id"]))
            by_id[edge.edge_id] = edge
        return [by_id[edge_id] for edge_id in edge_ids if edge_id in by_id]

    def list_edge_clusters(self, namespace: str) -> list[EdgeCluster]:
        rows = self.conn.execute(
            "SELECT * FROM edge_clusters WHERE namespace = ? ORDER BY rowid",
            (namespace,),
        ).fetchall()
        return [_cluster_from_row(row) for row in rows]

    def list_edge_cluster_members(
        self,
        namespace: str,
        cluster_ids: list[str] | None = None,
    ) -> list[EdgeClusterMember]:
        if not cluster_ids:
            rows = self.conn.execute(
                "SELECT * FROM edge_cluster_members WHERE namespace = ? ORDER BY rowid",
                (namespace,),
            ).fetchall()
        else:
            placeholders = ",".join("?" for _ in cluster_ids)
            rows = self.conn.execute(
                f"""
                SELECT *
                FROM edge_cluster_members
                WHERE namespace = ? AND cluster_id IN ({placeholders})
                ORDER BY rowid
                """,
                [namespace, *cluster_ids],
            ).fetchall()
        return [_cluster_member_from_row(row) for row in rows]

    def find_edge_cluster_by_fingerprint(self, namespace: str, cluster_fingerprint: str) -> EdgeCluster | None:
        row = self.conn.execute(
            """
            SELECT *
            FROM edge_clusters
            WHERE namespace = ? AND cluster_fingerprint = ?
            ORDER BY rowid
            LIMIT 1
            """,
            (namespace, cluster_fingerprint),
        ).fetchone()
        if not row:
            return None
        return _cluster_from_row(row)

    def get_nodes(self, namespace: str, node_ids: list[str]) -> list[MemoryNode]:
        if not node_ids:
            return []
        placeholders = ",".join("?" for _ in node_ids)
        rows = self.conn.execute(
            f"SELECT * FROM nodes WHERE namespace = ? AND node_id IN ({placeholders})",
            [namespace, *node_ids],
        ).fetchall()
        by_id = {}
        for row in rows:
            node = _node_from_row(row)
            by_id[node.node_id] = node
        return [by_id[node_id] for node_id in node_ids if node_id in by_id]

    def search_nodes_fts(self, namespace: str, query: str, top_k: int) -> list[tuple[MemoryNode, float]]:
        if top_k <= 0 or not query.strip():
            return []
        fts_query = _safe_fts_query(query)
        if not fts_query:
            return []
        try:
            rows = self.conn.execute(
                """
                SELECT n.*, bm25(nodes_fts) AS lexical_rank
                FROM nodes_fts
                JOIN nodes n
                  ON n.namespace = nodes_fts.namespace AND n.node_id = nodes_fts.node_id
                WHERE nodes_fts MATCH ?
                  AND nodes_fts.namespace = ?
                  AND n.status = 'active'
                ORDER BY lexical_rank
                LIMIT ?
                """,
                (fts_query, namespace, top_k),
            ).fetchall()
        except sqlite3.Error as exc:
            raise StoreError(f"SQLite FTS node search failed for query {query!r}.") from exc
        return [(_node_from_row(row), -float(row["lexical_rank"] or 0.0)) for row in rows]

    def get_incident_edges(self, namespace: str, node_ids: list[str]) -> list[HyperEdge]:
        if not node_ids:
            return []
        placeholders = ",".join("?" for _ in node_ids)
        rows = self.conn.execute(
            f"""
            SELECT DISTINCT he.*
            FROM hyper_edges he
            JOIN hyper_edge_members hem
              ON he.namespace = hem.namespace AND he.edge_id = hem.edge_id
            WHERE hem.namespace = ? AND hem.node_id IN ({placeholders})
            ORDER BY he.rowid
            """,
            [namespace, *node_ids],
        ).fetchall()
        return [_edge_from_row(row, _edge_members(self.conn, row["namespace"], row["edge_id"])) for row in rows]

    def get_edge_clusters_for_edges(self, namespace: str, edge_ids: list[str]) -> list[EdgeCluster]:
        if not edge_ids:
            return []
        placeholders = ",".join("?" for _ in edge_ids)
        rows = self.conn.execute(
            f"""
            SELECT DISTINCT ec.*
            FROM edge_clusters ec
            JOIN edge_cluster_members ecm
              ON ec.namespace = ecm.namespace AND ec.cluster_id = ecm.cluster_id
            WHERE ecm.namespace = ? AND ecm.edge_id IN ({placeholders})
            ORDER BY ec.rowid
            """,
            [namespace, *edge_ids],
        ).fetchall()
        return [_cluster_from_row(row) for row in rows]

    def stats(self, namespace: str) -> dict[str, int]:
        result: dict[str, int] = {}
        for key, table in {
            "nodes": "nodes",
            "hyper_edges": "hyper_edges",
            "hyper_edge_members": "hyper_edge_members",
            "edge_clusters": "edge_clusters",
            "edge_cluster_members": "edge_cluster_members",
            "triples": "triples",
            "entity_aliases": "entity_alias_index",
            "turn_messages": "turns",
        }.items():
            row = self.conn.execute(
                f"SELECT COUNT(*) AS count FROM {table} WHERE namespace = ?",
                (namespace,),
            ).fetchone()
            result[key] = int(row["count"])

        row = self.conn.execute(
            "SELECT COUNT(DISTINCT turn_id) AS count FROM turns WHERE namespace = ?",
            (namespace,),
        ).fetchone()
        result["turns"] = int(row["count"])

        for node in self.list_nodes(namespace):
            for label in node.node_labels:
                result[f"nodes.{label}"] = result.get(f"nodes.{label}", 0) + 1
        return result

    def close(self) -> None:
        self.conn.close()

    def _init_schema(self) -> None:
        try:
            with self.conn:
                self.conn.executescript(
                    """
                    PRAGMA journal_mode = WAL;

                    CREATE TABLE IF NOT EXISTS nodes (
                        namespace TEXT NOT NULL,
                        node_id TEXT NOT NULL,
                        canonical_text TEXT NOT NULL,
                        normalized_text TEXT NOT NULL,
                        fingerprint TEXT NOT NULL,
                        node_labels_json TEXT NOT NULL,
                        status TEXT NOT NULL DEFAULT 'active',
                        superseded_by TEXT,
                        invalidated_by TEXT,
                        status_reason TEXT,
                        status_updated_at TEXT,
                        content TEXT NOT NULL,
                        summary TEXT NOT NULL DEFAULT '',
                        attributes_json TEXT NOT NULL,
                        absolute_time_json TEXT NOT NULL,
                        relative_time_json TEXT NOT NULL,
                        local_graph_json TEXT NOT NULL,
                        metadata_json TEXT NOT NULL,
                        PRIMARY KEY (namespace, node_id)
                    );

                    CREATE INDEX IF NOT EXISTS idx_nodes_namespace_fingerprint
                        ON nodes(namespace, fingerprint);

                    CREATE INDEX IF NOT EXISTS idx_nodes_namespace_normalized_text
                        ON nodes(namespace, normalized_text);

                    CREATE VIRTUAL TABLE IF NOT EXISTS nodes_fts USING fts5(
                        namespace UNINDEXED,
                        node_id UNINDEXED,
                        content,
                        summary,
                        local_graph
                    );

                    CREATE TABLE IF NOT EXISTS hyper_edges (
                        namespace TEXT NOT NULL,
                        edge_id TEXT NOT NULL,
                        edge_fingerprint TEXT NOT NULL,
                        description TEXT NOT NULL DEFAULT '',
                        status TEXT NOT NULL DEFAULT 'active',
                        member_policy TEXT NOT NULL DEFAULT 'immutable',
                        member_signature TEXT NOT NULL DEFAULT '',
                        member_version INTEGER NOT NULL DEFAULT 1,
                        absolute_time_json TEXT NOT NULL,
                        relative_time_json TEXT NOT NULL,
                        metadata_json TEXT NOT NULL,
                        PRIMARY KEY (namespace, edge_id)
                    );

                    CREATE INDEX IF NOT EXISTS idx_hyper_edges_namespace_fingerprint
                        ON hyper_edges(namespace, edge_fingerprint);

                    CREATE TABLE IF NOT EXISTS hyper_edge_members (
                        namespace TEXT NOT NULL,
                        edge_id TEXT NOT NULL,
                        node_id TEXT NOT NULL,
                        weight REAL NOT NULL DEFAULT 1.0
                    );

                    CREATE INDEX IF NOT EXISTS idx_hyper_edge_members_node
                        ON hyper_edge_members(namespace, node_id);

                    CREATE TABLE IF NOT EXISTS edge_clusters (
                        namespace TEXT NOT NULL,
                        cluster_id TEXT NOT NULL,
                        cluster_fingerprint TEXT NOT NULL,
                        canonical_description TEXT NOT NULL,
                        cluster_labels_json TEXT NOT NULL,
                        aliases_json TEXT NOT NULL,
                        conflict_state TEXT NOT NULL DEFAULT 'none',
                        description_variants_json TEXT NOT NULL,
                        status TEXT NOT NULL DEFAULT 'active',
                        metadata_json TEXT NOT NULL,
                        PRIMARY KEY (namespace, cluster_id)
                    );

                    CREATE INDEX IF NOT EXISTS idx_edge_clusters_namespace_fingerprint
                        ON edge_clusters(namespace, cluster_fingerprint);

                    CREATE TABLE IF NOT EXISTS edge_cluster_members (
                        namespace TEXT NOT NULL,
                        cluster_id TEXT NOT NULL,
                        edge_id TEXT NOT NULL,
                        relation_to_cluster TEXT NOT NULL,
                        status TEXT NOT NULL DEFAULT 'active',
                        metadata_json TEXT NOT NULL,
                        PRIMARY KEY (namespace, cluster_id, edge_id)
                    );

                    CREATE INDEX IF NOT EXISTS idx_edge_cluster_members_edge
                        ON edge_cluster_members(namespace, edge_id);

                    CREATE TABLE IF NOT EXISTS triples (
                        namespace TEXT NOT NULL,
                        triple_id TEXT NOT NULL,
                        owner_node_id TEXT NOT NULL,
                        subject TEXT NOT NULL,
                        predicate TEXT NOT NULL,
                        object TEXT NOT NULL,
                        status TEXT NOT NULL DEFAULT 'active',
                        scope_edge_id TEXT,
                        scope_cluster_id TEXT,
                        superseded_by TEXT,
                        invalidated_by TEXT,
                        qualifiers_json TEXT NOT NULL,
                        metadata_json TEXT NOT NULL,
                        PRIMARY KEY (namespace, triple_id)
                    );

                    CREATE INDEX IF NOT EXISTS idx_triples_owner
                        ON triples(namespace, owner_node_id);

                    CREATE INDEX IF NOT EXISTS idx_triples_scope_edge
                        ON triples(namespace, scope_edge_id);

                    CREATE INDEX IF NOT EXISTS idx_triples_scope_cluster
                        ON triples(namespace, scope_cluster_id);

                    CREATE TABLE IF NOT EXISTS entity_alias_index (
                        namespace TEXT NOT NULL,
                        normalized_alias TEXT NOT NULL,
                        entity_type TEXT NOT NULL DEFAULT '',
                        node_id TEXT NOT NULL,
                        source_count INTEGER NOT NULL DEFAULT 1,
                        updated_at TEXT NOT NULL,
                        PRIMARY KEY (namespace, normalized_alias, entity_type)
                    );

                    CREATE INDEX IF NOT EXISTS idx_entity_alias_lookup
                        ON entity_alias_index(namespace, normalized_alias, entity_type);

                    CREATE TABLE IF NOT EXISTS turns (
                        namespace TEXT NOT NULL,
                        turn_id TEXT NOT NULL,
                        turn_index INTEGER NOT NULL,
                        message_index INTEGER NOT NULL,
                        role TEXT NOT NULL,
                        content TEXT NOT NULL,
                        timestamp TEXT,
                        message_metadata_json TEXT NOT NULL,
                        turn_metadata_json TEXT NOT NULL,
                        inserted_at TEXT NOT NULL,
                        PRIMARY KEY (namespace, turn_id, message_index)
                    );

                    CREATE INDEX IF NOT EXISTS idx_turns_namespace_order
                        ON turns(namespace, turn_index, message_index);

                    """
                )
        except sqlite3.DatabaseError as exc:
            raise StoreError(f"Failed to initialize SQLite store: {self.path}") from exc


def _to_json(value: Any) -> str:
    if hasattr(value, "model_dump"):
        value = value.model_dump(mode="json")
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _from_json(value: str, default: Any) -> Any:
    if not value:
        return default
    return json.loads(value)


def _node_from_row(row: sqlite3.Row) -> MemoryNode:
    return MemoryNode(
        node_id=row["node_id"],
        namespace=row["namespace"],
        canonical_text=row["canonical_text"],
        normalized_text=row["normalized_text"],
        fingerprint=row["fingerprint"],
        node_labels=_from_json(row["node_labels_json"], []),
        status=row["status"],
        superseded_by=row["superseded_by"],
        invalidated_by=row["invalidated_by"],
        status_reason=row["status_reason"],
        status_updated_at=row["status_updated_at"],
        content=row["content"],
        summary=row["summary"],
        attributes=_from_json(row["attributes_json"], {}),
        time=_time_from_columns(row["absolute_time_json"], row["relative_time_json"]),
        local_graph=LocalNodeGraph.model_validate(_from_json(row["local_graph_json"], {})),
        metadata=_from_json(row["metadata_json"], {}),
    )


def _edge_from_row(row: sqlite3.Row, members: list[sqlite3.Row]) -> HyperEdge:
    node_ids = [member["node_id"] for member in members]
    weights = {member["node_id"]: float(member["weight"]) for member in members}
    return HyperEdge(
        edge_id=row["edge_id"],
        namespace=row["namespace"],
        edge_fingerprint=row["edge_fingerprint"],
        description=row["description"],
        status=row["status"],
        member_policy=row["member_policy"],
        member_signature=row["member_signature"],
        member_version=int(row["member_version"]),
        node_ids=node_ids,
        weights=weights,
        time=_time_from_columns(row["absolute_time_json"], row["relative_time_json"]),
        metadata=_from_json(row["metadata_json"], {}),
    )


def _cluster_from_row(row: sqlite3.Row) -> EdgeCluster:
    return EdgeCluster(
        namespace=row["namespace"],
        cluster_id=row["cluster_id"],
        cluster_fingerprint=row["cluster_fingerprint"],
        canonical_description=row["canonical_description"],
        cluster_labels=_from_json(row["cluster_labels_json"], []),
        aliases=_from_json(row["aliases_json"], []),
        conflict_state=row["conflict_state"],
        description_variants=_from_json(row["description_variants_json"], []),
        status=row["status"],
        metadata=_from_json(row["metadata_json"], {}),
    )


def _cluster_member_from_row(row: sqlite3.Row) -> EdgeClusterMember:
    return EdgeClusterMember(
        namespace=row["namespace"],
        cluster_id=row["cluster_id"],
        edge_id=row["edge_id"],
        relation_to_cluster=row["relation_to_cluster"],
        status=row["status"],
        metadata=_from_json(row["metadata_json"], {}),
    )


def _local_graph_fts_text(node: MemoryNode) -> str:
    return " ".join(
        f"{triple.subject} {triple.predicate} {triple.object}"
        for triple in node.local_graph.triples
        if triple.status == "active"
    )


def _safe_fts_query(query: str) -> str:
    terms = list(dict.fromkeys(tokenize(query)))
    return " OR ".join(f'"{term}"' for term in terms)


def _message_from_turn_row(row: sqlite3.Row) -> Message:
    metadata = _from_json(row["message_metadata_json"], {})
    metadata.setdefault("turn_id", row["turn_id"])
    metadata.setdefault("turn_index", int(row["turn_index"]))
    return Message(
        role=row["role"],
        content=row["content"],
        timestamp=row["timestamp"],
        metadata=metadata,
    )


def _edge_members(conn: sqlite3.Connection, namespace: str, edge_id: str) -> list[sqlite3.Row]:
    return conn.execute(
        """
        SELECT node_id, weight
        FROM hyper_edge_members
        WHERE namespace = ? AND edge_id = ?
        ORDER BY rowid
        """,
        (namespace, edge_id),
    ).fetchall()


def _time_from_columns(absolute_time_json: str, relative_time_json: str) -> TimeBundle:
    time = TimeBundle()
    time.world = time.world.model_validate(_from_json(absolute_time_json, {}))
    relative_time = _from_json(relative_time_json, {})
    time.lifecycle = time.lifecycle.model_validate(relative_time.get("lifecycle", {}))
    time.activation = time.activation.model_validate(relative_time.get("activation", {}))
    return time
