from __future__ import annotations

from dataclasses import dataclass

from comgmem.config import ModelConfig, NLPConfig, RecallConfig, RetrievalConfig
from comgmem.embeddings import EmbeddingClient
from comgmem.llms.base import LLMClient
from comgmem.retrieval.fusion import FusedNode, RankedNodeList, reciprocal_rank_fusion_channels
from comgmem.retrieval.graph_ripple import GraphRippleExpansion, RankedEdge, Track1RankedEdge
from comgmem.retrieval.lexical_recall import LexicalNodeHit, SQLiteFTSRecall
from comgmem.retrieval.query_analysis import build_query_analyzer
from comgmem.retrieval.ranking import edge_level_rrf
from comgmem.retrieval.vector_recall import DenseVectorRecall, VectorEdgeHit, VectorNodeHit
from comgmem.schema import HyperEdge, SearchResult
from comgmem.stores.base import MemoryStore
from comgmem.stores.vector_store import VectorStore


@dataclass
class Track2RankedEdge:
    edge: HyperEdge
    score: float
    vector_hits: list[dict[str, object]]
    score_parts: dict[str, object]


class Retriever:
    def __init__(
        self,
        store: MemoryStore,
        config: RetrievalConfig,
        *,
        recall_config: RecallConfig | None = None,
        nlp_config: NLPConfig | None = None,
        query_analysis_llm: LLMClient | None = None,
        query_analysis_llm_config: ModelConfig | None = None,
        embedding_client: EmbeddingClient | None = None,
        vector_stores: dict[str, VectorStore] | None = None,
    ) -> None:
        self.store = store
        self.config = config
        self.recall_config = recall_config or RecallConfig()
        self.analyzer = build_query_analyzer(
            config,
            nlp_config=nlp_config,
            llm=query_analysis_llm,
            llm_config=query_analysis_llm_config,
        )
        self.lexical_recall = SQLiteFTSRecall(store, config)
        self.vector_recall = DenseVectorRecall(
            store,
            config,
            embedding_client=embedding_client,
            vector_stores=vector_stores,
        )
        self.graph_ripple = GraphRippleExpansion(store, config, recall_config=self.recall_config)

    def search(
        self,
        query: str,
        *,
        namespace: str,
        top_k: int,
        current_turn: int | None = None,
    ) -> list[SearchResult]:
        if top_k <= 0:
            return []

        analysis = self.analyzer.analyze(query)
        query_vector = self.vector_recall.embed_query(analysis.query)
        lexical_hits = self.lexical_recall.recall(namespace=namespace, query=analysis.query)
        vector_hits = self.vector_recall.recall(
            namespace=namespace,
            query=analysis.query,
            query_vector=query_vector,
        )
        edge_hits = self.vector_recall.recall_hyper_edges(
            namespace=namespace,
            query=analysis.query,
            query_vector=query_vector,
        )

        node_ranking = self._rank_nodes(namespace=namespace, lexical_hits=lexical_hits, vector_hits=vector_hits)
        track1_edges = self.graph_ripple.rank_track1_edges(namespace=namespace, node_ranking=node_ranking)
        track2_edges = self._rank_track2_edges(namespace=namespace, edge_hits=edge_hits)
        edge_ranking = self._edge_level_rrf(
            namespace=namespace,
            track1_edges=track1_edges,
            track2_edges=track2_edges,
        )
        core_edges = edge_ranking[: self._edge_core_limit()]
        ranked_edges = self.graph_ripple.attach_cluster_periphery(namespace=namespace, ranked_edges=core_edges)

        limit = min(top_k, self.config.final_top_k, len(ranked_edges))
        return [
            self._to_result(item, analysis_metadata=analysis.to_metadata(), current_turn=current_turn)
            for item in ranked_edges[:limit]
        ]

    def _rank_nodes(
        self,
        *,
        namespace: str,
        lexical_hits: list[LexicalNodeHit],
        vector_hits: list[VectorNodeHit],
    ) -> list[FusedNode]:
        vector_node_ids = list(dict.fromkeys(hit.node_id for hit in vector_hits))
        vector_nodes = self.store.get_nodes(namespace, vector_node_ids)
        vector_nodes_by_id = {node.node_id: node for node in vector_nodes}
        vector_hits_by_node: dict[str, list[dict[str, object]]] = {}
        best_vector_score_by_channel: dict[str, dict[str, float]] = {}
        for hit in vector_hits:
            channel_scores = best_vector_score_by_channel.setdefault(hit.channel, {})
            channel_scores[hit.node_id] = max(channel_scores.get(hit.node_id, float("-inf")), hit.score)
            vector_hits_by_node.setdefault(hit.node_id, []).append(_vector_node_payload(hit))

        ranked_lists = [
            RankedNodeList(
                nodes=[hit.node for hit in lexical_hits],
                channel="lexical",
                score_key="rrf_lexical",
            )
        ]
        for channel in ("node_content", "node_local_graph"):
            scores = best_vector_score_by_channel.get(channel, {})
            channel_nodes = sorted(
                [
                    vector_nodes_by_id[node_id]
                    for node_id in vector_node_ids
                    if node_id in vector_nodes_by_id and node_id in scores
                ],
                key=lambda node: scores.get(node.node_id, float("-inf")),
                reverse=True,
            )
            ranked_lists.append(
                RankedNodeList(
                    nodes=channel_nodes,
                    channel=channel,
                    score_key=f"rrf_{channel}",
                )
            )

        return reciprocal_rank_fusion_channels(
            ranked_lists=ranked_lists,
            vector_hit_payloads=vector_hits_by_node,
            k=max(1, self.config.node_rrf_k),
        )

    def _rank_track2_edges(self, *, namespace: str, edge_hits: list[VectorEdgeHit]) -> list[Track2RankedEdge]:
        if not edge_hits:
            return []
        edges = self.store.get_edges(namespace, list(dict.fromkeys(hit.edge_id for hit in edge_hits)))
        edges_by_id = {edge.edge_id: edge for edge in edges if edge.status == "active"}
        ranked: list[Track2RankedEdge] = []
        seen_edge_ids: set[str] = set()
        for hit in edge_hits:
            edge = edges_by_id.get(hit.edge_id)
            if edge is None or edge.edge_id in seen_edge_ids:
                continue
            seen_edge_ids.add(edge.edge_id)
            ranked.append(
                Track2RankedEdge(
                    edge=edge,
                    score=hit.score,
                    vector_hits=[_vector_edge_payload(hit)],
                    score_parts={
                        "track2_vector_score": hit.score,
                    },
                )
            )
        return ranked

    def _edge_level_rrf(
        self,
        *,
        namespace: str,
        track1_edges: list[Track1RankedEdge],
        track2_edges: list[Track2RankedEdge],
    ) -> list[RankedEdge]:
        edge_rrf_k = self._edge_rrf_k()
        track1_by_id = {item.edge.edge_id: item for item in track1_edges}
        track2_by_id = {item.edge.edge_id: item for item in track2_edges}
        rrf_results = edge_level_rrf(
            track1_edge_ids=[item.edge.edge_id for item in track1_edges],
            track2_edge_ids=[item.edge.edge_id for item in track2_edges],
            k=edge_rrf_k,
            track1_tiebreak_scores={item.edge.edge_id: item.score for item in track1_edges},
            track2_tiebreak_scores={item.edge.edge_id: item.score for item in track2_edges},
        )
        ranked: list[RankedEdge] = []
        for rrf_result in rrf_results:
            edge_id = rrf_result.edge_id
            track1 = track1_by_id.get(edge_id)
            track2 = track2_by_id.get(edge_id)
            if track1 is None and track2 is None:
                continue
            if track1 is not None:
                edge = track1.edge
            else:
                assert track2 is not None
                edge = track2.edge
            nodes = (
                track1.nodes
                if track1 is not None
                else self.graph_ripple.materialize_edge_nodes(namespace=namespace, edge=edge)
            )
            score_parts = dict(rrf_result.score_parts)
            if track1 is not None:
                score_parts.update(track1.score_parts)
            if track2 is not None:
                score_parts.update(track2.score_parts)
            ranked.append(
                RankedEdge(
                    edge=edge,
                    score=rrf_result.score,
                    nodes=nodes,
                    score_parts=score_parts,
                    hit_node_ids=track1.hit_node_ids if track1 is not None else set(),
                    edge_vector_hits=track2.vector_hits if track2 is not None else [],
                )
            )
        return ranked

    def _edge_rrf_k(self) -> int:
        return max(1, self.config.edge_rrf_k)

    def _edge_core_limit(self) -> int:
        return max(0, self.config.edge_core_top_k)

    def _to_result(self, ranked_edge: RankedEdge, *, analysis_metadata: dict, current_turn: int | None) -> SearchResult:
        edge = ranked_edge.edge
        metadata = {
            "query_analysis": analysis_metadata,
            "edge_id": edge.edge_id,
            "hyper_edge_ids": [edge.edge_id],
            "edge_description": edge.description,
            "edge_node_ids": edge.node_ids,
            "channels": self._result_channels(ranked_edge),
            "hit_node_ids": sorted(ranked_edge.hit_node_ids),
            "cluster_ids": sorted(ranked_edge.cluster_ids),
            "cluster_edge_descriptions": ranked_edge.cluster_edge_descriptions,
            "periphery_edges": self._periphery_edges_metadata(ranked_edge, current_turn=current_turn),
            "periphery_nodes": [
                self._node_metadata(item, current_turn=current_turn) for item in ranked_edge.periphery_nodes
            ],
            "score_parts": ranked_edge.score_parts,
            "edge_vector_hits": ranked_edge.edge_vector_hits,
            "time": edge.time.model_dump(mode="json"),
            "relative_time": _relative_time_for_bundle(
                edge.time,
                current_turn=current_turn,
                source_turn_ids=_strings(edge.metadata.get("source_turn_ids")),
            ),
            "edge_metadata": edge.metadata,
            "edge_nodes": [
                self._node_metadata(item, current_turn=current_turn, edge=edge) for item in ranked_edge.nodes
            ],
        }
        return SearchResult(
            id=edge.edge_id,
            content=self._edge_content(ranked_edge, current_turn=current_turn),
            score=float(ranked_edge.score),
            metadata=metadata,
        )

    def _result_channels(self, ranked_edge: RankedEdge) -> list[str]:
        channels = {channel for node in ranked_edge.nodes for channel in node.channels}
        channels.update(str(hit["channel"]) for hit in ranked_edge.edge_vector_hits if hit.get("channel"))
        return sorted(channels)

    def _node_metadata(
        self,
        fused: FusedNode,
        *,
        current_turn: int | None,
        edge: HyperEdge | None = None,
    ) -> dict[str, object]:
        node = fused.node
        source_turn_ids = _strings(node.metadata.get("source_turn_ids"))
        payload: dict[str, object] = {
            "node_id": node.node_id,
            "node_labels": node.node_labels,
            "content": node.content,
            "summary": node.summary,
            "score": fused.score,
            "channels": sorted(fused.channels),
            "score_parts": fused.score_parts,
            "matched_vector_items": fused.vector_hits,
            "source_turn_ids": source_turn_ids,
            "time": node.time.model_dump(mode="json"),
            "relative_time": _relative_time_for_bundle(
                node.time,
                current_turn=current_turn,
                source_turn_ids=source_turn_ids,
            ),
            "node_metadata": node.metadata,
            "triples": [
                _triple_metadata(triple, current_turn=current_turn)
                for triple in _rank_node_triples(
                    node.local_graph.triples,
                    edge=edge,
                    limit=self._node_triple_limit(),
                )
            ],
        }
        return payload

    def _node_triple_limit(self) -> int | None:
        configured = self.recall_config.node_triple_limit
        return None if configured is None else max(0, configured)

    def _periphery_edges_metadata(self, ranked_edge: RankedEdge, *, current_turn: int | None) -> list[dict[str, object]]:
        nodes_by_id = {
            item.node.node_id: item
            for item in [*ranked_edge.nodes, *ranked_edge.periphery_nodes]
        }
        payloads: list[dict[str, object]] = []
        for edge_payload in ranked_edge.periphery_edges:
            payload = dict(edge_payload)
            edge_metadata = payload.get("edge_metadata")
            edge_metadata = edge_metadata if isinstance(edge_metadata, dict) else {}
            source_turn_ids = _strings(edge_metadata.get("source_turn_ids") or payload.get("source_turn_ids"))
            node_ids = _strings(payload.get("node_ids"))
            payload["relative_time"] = _relative_time_for_payload(
                payload.get("time"),
                current_turn=current_turn,
                source_turn_ids=source_turn_ids,
            )
            payload["nodes"] = [
                self._node_metadata(
                    nodes_by_id[node_id],
                    current_turn=current_turn,
                    edge=edge_payload_to_edge(payload),
                )
                for node_id in node_ids
                if node_id in nodes_by_id
            ]
            payloads.append(payload)
        return payloads

    def _edge_content(self, ranked_edge: RankedEdge, *, current_turn: int | None) -> str:
        edge = ranked_edge.edge
        turn_inserted_at = self.store.turn_inserted_at_by_id(
            edge.namespace,
            _ranked_edge_source_turn_ids(ranked_edge),
        )
        seen_node_ids: set[str] = set()
        seen_edge_ids = {edge.edge_id}
        blocks = [
            _format_edge_memory_context(
                index=1,
                description=edge.description,
                relative_time=_relative_time_for_bundle(
                    edge.time,
                    current_turn=current_turn,
                    source_turn_ids=_strings(edge.metadata.get("source_turn_ids")),
                ),
                source_turn_ids=_strings(edge.metadata.get("source_turn_ids")),
                nodes=[
                    self._node_metadata(item, current_turn=current_turn, edge=edge)
                    for item in ranked_edge.nodes
                ],
                seen_node_ids=seen_node_ids,
                include_turn_ids=self.recall_config.include_turn_ids_in_context,
                include_real_time=self.recall_config.include_real_time_in_context,
                turn_inserted_at=turn_inserted_at,
            )
        ]
        periphery_edges = self._periphery_edges_metadata(ranked_edge, current_turn=current_turn)
        memory_index = 2
        for edge_payload in periphery_edges:
            edge_id = str(edge_payload.get("edge_id") or "")
            if edge_id and edge_id in seen_edge_ids:
                continue
            if edge_id:
                seen_edge_ids.add(edge_id)
            nodes = edge_payload.get("nodes")
            blocks.append(
                _format_edge_memory_context(
                    index=memory_index,
                    description=str(edge_payload.get("description") or ""),
                    relative_time=edge_payload.get("relative_time"),
                    source_turn_ids=_strings(
                        (edge_payload.get("edge_metadata") or {}).get("source_turn_ids")
                        if isinstance(edge_payload.get("edge_metadata"), dict)
                        else edge_payload.get("source_turn_ids")
                    ),
                    nodes=[node for node in nodes if isinstance(node, dict)] if isinstance(nodes, list) else [],
                    seen_node_ids=seen_node_ids,
                    include_turn_ids=self.recall_config.include_turn_ids_in_context,
                    include_real_time=self.recall_config.include_real_time_in_context,
                    turn_inserted_at=turn_inserted_at,
                )
            )
            memory_index += 1
        return "\n\n".join(block for block in blocks if block.strip())


def _vector_node_payload(hit: VectorNodeHit) -> dict[str, object]:
    return {
        "channel": hit.channel,
        "score": hit.score,
        "id": hit.hit.id,
        "text": hit.hit.text,
        "payload": hit.hit.payload,
    }


def _vector_edge_payload(hit: VectorEdgeHit) -> dict[str, object]:
    return {
        "channel": hit.channel,
        "score": hit.score,
        "id": hit.hit.id,
        "text": hit.hit.text,
        "payload": hit.hit.payload,
    }


def _triple_metadata(triple, *, current_turn: int | None) -> dict[str, object]:
    payload = triple.model_dump(mode="json")
    payload["relative_time"] = _relative_time_for_source_turns(
        current_turn=current_turn,
        source_turn_ids=_strings(triple.qualifiers.get("source_turn_ids")),
    )
    return payload


def _rank_node_triples(triples, *, edge: HyperEdge | None, limit: int | None) -> list[object]:
    active = [triple for triple in triples if triple.status == "active"]
    ranked = [
        triple
        for _, triple in sorted(
            enumerate(active),
            key=lambda pair: (
                -_triple_edge_priority(pair[1], edge),
                -_latest_turn(_strings(pair[1].qualifiers.get("source_turn_ids"))),
                pair[0],
            ),
        )
    ]
    return ranked if limit is None else ranked[:limit]


def _triple_edge_priority(triple, edge: HyperEdge | None) -> int:
    if edge is None:
        return 0
    edge_id = edge.edge_id
    if edge_id and edge_id in _strings(getattr(triple, "scope_edge_ids", [])):
        return 2
    qualifiers = getattr(triple, "qualifiers", {})
    if isinstance(qualifiers, dict) and edge_id in _strings(qualifiers.get("scope_edge_ids")):
        return 2
    edge_turns = set(_strings(edge.metadata.get("source_turn_ids")))
    if edge_turns and edge_turns.intersection(_strings(qualifiers.get("source_turn_ids"))):
        return 1
    return 0


def _latest_turn(source_turn_ids: list[str]) -> int:
    turns = [_turn_index(turn_id) for turn_id in source_turn_ids]
    concrete = [turn for turn in turns if turn is not None]
    return max(concrete) if concrete else -1


def edge_payload_to_edge(payload: dict[str, object]) -> HyperEdge | None:
    edge_id = str(payload.get("edge_id") or "")
    if not edge_id:
        return None
    edge_metadata = payload.get("edge_metadata")
    return HyperEdge(
        edge_id=edge_id,
        namespace=str(edge_metadata.get("namespace") if isinstance(edge_metadata, dict) else ""),
        edge_fingerprint="",
        description=str(payload.get("description") or ""),
        node_ids=_strings(payload.get("node_ids")),
        metadata=edge_metadata if isinstance(edge_metadata, dict) else {},
    )


def _format_edge_memory_context(
    *,
    index: int,
    description: str,
    relative_time: object,
    source_turn_ids: list[str],
    nodes: list[dict[str, object]],
    seen_node_ids: set[str],
    include_turn_ids: bool,
    include_real_time: bool,
    turn_inserted_at: dict[str, str],
) -> str:
    turn_suffix = _turn_context_suffix(
        source_turn_ids,
        bracket="paren",
        current_turn=_current_turn_from_relative_time(relative_time),
        include_turn_ids=include_turn_ids,
        include_real_time=include_real_time,
        turn_inserted_at=turn_inserted_at,
    )
    lines = [
        f"memory{index}\uff1a{description}{turn_suffix}",
    ]
    for node in nodes:
        node_id = str(node.get("node_id") or "")
        if node_id and node_id in seen_node_ids:
            continue
        if node_id:
            seen_node_ids.add(node_id)
        triples = node.get("triples")
        if not isinstance(triples, list):
            continue
        for triple in triples:
            if not isinstance(triple, dict):
                continue
            lines.append(
                _format_triple_line(
                    triple,
                    include_turn_ids=include_turn_ids,
                    include_real_time=include_real_time,
                    turn_inserted_at=turn_inserted_at,
                )
            )
    return "\n".join(lines)


def _format_triple_line(
    triple: dict[str, object],
    *,
    include_turn_ids: bool,
    include_real_time: bool,
    turn_inserted_at: dict[str, str],
) -> str:
    subject = str(triple.get("subject") or "").strip()
    predicate = str(triple.get("predicate") or "").strip()
    obj = str(triple.get("object") or "").strip()
    qualifiers = triple.get("qualifiers")
    qualifiers = qualifiers if isinstance(qualifiers, dict) else {}
    source_turn_ids = _strings(qualifiers.get("source_turn_ids"))
    turn_suffix = _turn_context_suffix(
        source_turn_ids,
        bracket="square",
        current_turn=None,
        include_turn_ids=include_turn_ids,
        include_real_time=include_real_time,
        turn_inserted_at=turn_inserted_at,
    )
    return f"{subject} -{predicate}- {obj}{turn_suffix}"


def _ranked_edge_source_turn_ids(ranked_edge: RankedEdge) -> list[str]:
    values = [
        *_strings(ranked_edge.edge.metadata.get("source_turn_ids")),
        *[
            turn_id
            for item in [*ranked_edge.nodes, *ranked_edge.periphery_nodes]
            for turn_id in _strings(item.node.metadata.get("source_turn_ids"))
        ],
        *[
            turn_id
            for item in [*ranked_edge.nodes, *ranked_edge.periphery_nodes]
            for triple in item.node.local_graph.triples
            for turn_id in _strings(triple.qualifiers.get("source_turn_ids"))
        ],
        *[
            turn_id
            for edge_payload in ranked_edge.periphery_edges
            for turn_id in _strings(_edge_payload_source_turn_ids(edge_payload))
        ],
    ]
    return list(dict.fromkeys(values))


def _edge_payload_source_turn_ids(edge_payload: dict[str, object]) -> object:
    edge_metadata = edge_payload.get("edge_metadata")
    if isinstance(edge_metadata, dict):
        return edge_metadata.get("source_turn_ids")
    return edge_payload.get("source_turn_ids")


def _turn_context_suffix(
    source_turn_ids: list[str],
    *,
    bracket: str,
    current_turn: int | None,
    include_turn_ids: bool,
    include_real_time: bool,
    turn_inserted_at: dict[str, str],
) -> str:
    if current_turn is None and not source_turn_ids:
        return ""
    parts = []
    if current_turn is not None:
        parts.append(f"current_turn_id={_turn_id_label(current_turn)}")
    if include_turn_ids:
        parts.append(f"source_turn id={_turn_ids_label(source_turn_ids)}")
    if include_real_time:
        real_time_label = _real_time_label(source_turn_ids, turn_inserted_at)
        if real_time_label:
            parts.append(f"real time={real_time_label}")
    if not parts:
        return ""
    text = ", ".join(parts)
    return f"，{text}" if bracket == "paren" else f" [{text}]"


def _real_time_label(source_turn_ids: list[str], turn_inserted_at: dict[str, str]) -> str:
    return ",".join(
        inserted_at
        for inserted_at in [turn_inserted_at.get(turn_id) for turn_id in source_turn_ids]
        if inserted_at
    )


def _turn_ids_label(source_turn_ids: list[str]) -> str:
    return ",".join(source_turn_ids)


def _turn_id_label(turn_index: int) -> str:
    return f"turn:{turn_index}"


def _current_turn_from_relative_time(relative_time: object) -> int | None:
    if not isinstance(relative_time, dict):
        return None
    return _int_or_none(relative_time.get("current_turn"))


def _relative_time_for_bundle(
    bundle,
    *,
    current_turn: int | None,
    source_turn_ids: list[str],
) -> dict[str, object]:
    activation = getattr(bundle, "activation", None)
    created_turn = getattr(activation, "created_turn", None)
    inserted_turn = getattr(activation, "inserted_turn", None)
    updated_turn = getattr(activation, "updated_turn", None)
    last_access_turn = getattr(activation, "last_access_turn", None)
    return _relative_time_for_activation(
        current_turn=current_turn,
        created_turn=created_turn,
        inserted_turn=inserted_turn,
        updated_turn=updated_turn,
        last_access_turn=last_access_turn,
        source_turn_ids=source_turn_ids,
    )


def _relative_time_for_payload(
    time_payload: object,
    *,
    current_turn: int | None,
    source_turn_ids: list[str],
) -> dict[str, object]:
    activation = {}
    if isinstance(time_payload, dict):
        raw_activation = time_payload.get("activation")
        if isinstance(raw_activation, dict):
            activation = raw_activation
    return _relative_time_for_activation(
        current_turn=current_turn,
        created_turn=_int_or_none(activation.get("created_turn")),
        inserted_turn=_int_or_none(activation.get("inserted_turn")),
        updated_turn=_int_or_none(activation.get("updated_turn")),
        last_access_turn=_int_or_none(activation.get("last_access_turn")),
        source_turn_ids=source_turn_ids,
    )


def _relative_time_for_source_turns(*, current_turn: int | None, source_turn_ids: list[str]) -> dict[str, object]:
    source_distances = _source_turn_distances(source_turn_ids, current_turn)
    return {
        "current_turn": current_turn,
        "turn_distance": _minimum_distance(source_distances),
        "source_turn_ids": source_turn_ids,
        "source_turn_distances": source_distances,
    }


def _relative_time_for_activation(
    *,
    current_turn: int | None,
    created_turn: int | None,
    inserted_turn: int | None,
    updated_turn: int | None,
    last_access_turn: int | None,
    source_turn_ids: list[str],
) -> dict[str, object]:
    source_distances = _source_turn_distances(source_turn_ids, current_turn)
    inserted_turn_distance = _turn_distance(inserted_turn, current_turn)
    created_turn_distance = _turn_distance(created_turn, current_turn)
    updated_turn_distance = _turn_distance(updated_turn, current_turn)
    last_access_turn_distance = _turn_distance(last_access_turn, current_turn)
    source_turn_distance = _minimum_distance(source_distances)
    return {
        "current_turn": current_turn,
        "turn_distance": _first_not_none(
            source_turn_distance,
            inserted_turn_distance,
            created_turn_distance,
            updated_turn_distance,
            last_access_turn_distance,
        ),
        "created_turn": created_turn,
        "created_turn_distance": created_turn_distance,
        "inserted_turn": inserted_turn,
        "inserted_turn_distance": inserted_turn_distance,
        "updated_turn": updated_turn,
        "updated_turn_distance": updated_turn_distance,
        "last_access_turn": last_access_turn,
        "last_access_turn_distance": last_access_turn_distance,
        "source_turn_ids": source_turn_ids,
        "source_turn_distance": source_turn_distance,
        "source_turn_distances": source_distances,
    }


def _source_turn_distances(source_turn_ids: list[str], current_turn: int | None) -> list[dict[str, object]]:
    distances: list[dict[str, object]] = []
    for turn_id in source_turn_ids:
        turn_index = _turn_index(turn_id)
        distances.append(
            {
                "turn_id": turn_id,
                "turn_index": turn_index,
                "distance": _turn_distance(turn_index, current_turn),
            }
        )
    return distances


def _turn_distance(turn_index: int | None, current_turn: int | None) -> int | None:
    if turn_index is None or current_turn is None:
        return None
    return max(0, current_turn - turn_index)


def _turn_index(turn_id: object) -> int | None:
    if isinstance(turn_id, int):
        return turn_id
    text = str(turn_id).strip()
    if not text:
        return None
    if text.startswith("turn:"):
        text = text.split(":", 1)[1]
    return _int_or_none(text)


def _int_or_none(value: object) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _minimum_distance(distances: list[dict[str, object]]) -> int | None:
    values = [item.get("distance") for item in distances]
    int_values = [value for value in values if isinstance(value, int)]
    return min(int_values) if int_values else None


def _first_not_none(*values: object) -> object:
    for value in values:
        if value is not None:
            return value
    return None


def _strings(value: object) -> list[str]:
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    if value in (None, "", [], {}):
        return []
    return [str(value).strip()]
