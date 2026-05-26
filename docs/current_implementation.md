# C-HyperMem 当前实现状态

本文档记录当前代码实现进展，并说明它与 `development_architecture.md` 的差异。`development_architecture.md` 仍是长期架构蓝图，但本文也记录已经通过实现验证、应反向固化到后续开发中的修正点。后续写代码时不要机械遵守设计文档；若本文标记为“当前实现更优 / 应保持”的地方，应优先以本文为准。

维护约定：后续每次执行新的代码修改或架构调整，都必须同步更新本文档，确保本文始终反映当前真实实现，而不是只反映历史设计。

## 1. 对外入口

- `c_hypermem.Memory` 是当前唯一推荐入口。
- 已支持 `from_config/reset/add/add_memory/search/stats/close`。
- `add_memory(...)` 会规范化为 `AgentInteraction`；`add(...)` 会规范化为 `MemoryImportBatch`。
- 如果配置中存在 `llm` 且未显式传入 extractor，`Memory` 会创建默认 `LLMMemoryExtractor`。

## 2. 配置与环境

- 默认配置入口为 `configs/default.yaml`。
- 模型、embedding、token 计算和 NLP 模型路径配置拆在 `configs/models.yaml`，节点标签和 turn 记录配置拆在 `configs/node_labels.yaml`。
- token 计算配置统一放在 `configs/models.yaml` 的 `token_counting.tokenizer_encoding`，NLP 查询分析的 spaCy 路径放在 `configs/models.yaml` 的 `nlp.model_path`；维护项只配置触发阈值和 prompt。
- 仅读取 C-HyperMem 项目根目录下的 `.env`，当前可以直接使用`.env`调用模型进行测试。
- `.env` 已加入 `.gitignore`，仓库提供 `.env.example`。
- `embedding.batch_size` 已加入配置，默认值为 `10`。
- `ingestion.context_window_messages` 控制传给抽取模型的最近上下文消息数，默认值为 `3`。
- `index.vector` 默认改为 `qdrant`；`index.vector_store` 提供本地 Qdrant 路径和 collection 名称，默认无需用户额外配置服务端。
- `maintenance` 是顶层配置；当前只包含 `node_summary.*`、`local_triples.*` 和 `hyper_edge_description.*`，不再包含 EdgeCluster 维护入口。
- 当前默认节点标签包括：`event/fact/entity/state/preference/task/instruction/tool`。
- `turn` 不是 `MemoryNode` 标签；它是独立的原始对话记录配置，写入 `turns` 表和 `turn_dialogue` 向量索引，不需要 `LocalNodeGraph`。
- `configs/default.yaml` 中的 `include` 是配置加载器指令，会在进入 `MemoryConfig` 前合并 `models.yaml` 和 `node_labels.yaml`，因此保留；它不是运行时配置项。
- `prompt_version`、`node_identity.*`、顶层 `local_graph.*`、`time.relative_decay.*`、`ingestion.event_mode/max_facts_per_event/extractor`、`extraction.output_schema/forbid_model_ids/forbid_confidence/allow_unknown_node_labels`、`hyperedges.*`、以及 node label 内的 `local_graph` 策略已从当前配置中移除。这些字段此前只被 Pydantic 接收、停留在设计文档中，或只是把系统默认实现从配置绕一圈传回代码，当前不再暴露给用户。

## 3. 当前 Schema

核心 schema 位于 `c_hypermem/schema.py`：

- `MemoryNode`：统一节点结构，使用 `node_labels` 表达语义类型。
- `HyperEdge`：具体高阶关系实例，核心字段已收敛到 description、member node ids、metadata、time/status/member policy 等；`edge_type/relation/polarity/roles` 已不再是 Pydantic schema 核心字段。
- `EdgeCluster`：确定性锚点聚合视图，不强制合并边，不做相似度聚类或后台维护。当前锚点包括共享成员节点，以及符合 eligibility 的 active local triples subject/object 端点重合。
- `LocalNodeGraph`：所有节点共享的局部图结构，只包含统一 triples；旧 `attributes/roles` 已从 schema 移除。
- `ExtractedNode`：新的抽取节点候选，包含 `ref/labels/canonical_text/summaries/triples/edge_summary_refs/metadata`。节点构建时间由系统写入，不由 LLM 输出。
- `ExtractedEdgeSummary`：新的抽取边摘要候选，包含 `ref/description/metadata`。
- `MemoryExtraction`：LLM 一次抽取输出主入口已切换为 `nodes/edge_summaries/metadata`；旧 `entities/events/assertions/sources` 不再是主抽取 schema 字段。

Schema 层当前会拒绝旧抽取 shape，以及 LLM 输出的 `sources/source_refs/source_ref/edge_type/relation/polarity/roles/time` 等不应由模型生成的来源、typed-edge 或构建时间字段。系统 ID 由 `utils/ids.py` 生成，LLM 不生成 `node_id/edge_id/triple_id`。

注意：当前已完成阶段 1-5 的主路径，并已开始阶段 6 的 Node summary 和 LocalTriple 维护。旧测试迁移和示例迁移仍会在后续阶段继续清理。

## 4. 写入 Pipeline

当前写入链路：

```text
Memory.add_memory/add
  -> IngestionPipeline
  -> LLMMemoryExtractor 或显式 extractor
  -> GraphAssembler
     -> GraphMaintenance(node summary / local triple maintenance during node merge)
  -> SQLiteStore
  -> VectorStore(Qdrant, rebuildable side indexes)
```

已实现内容：

- 一次抽取：`LLMMemoryExtractor` 渲染 `prompts/extraction/memory_extraction.md`。
- 抽取输入已改为 `ExtractionWindow(context, target)`：
  - `context` 是最近 K 条消息，仅用于代词、时间和省略信息消解。
  - `target` 是当前最新消息或交互片段，LLM 只能从 target 中抽取新增记忆。
  - `add_memory(...)` 每次把当前 interaction 作为 target；`add(messages)` 会按消息顺序逐条模拟增量 target。
- `node_labels.yaml` 的启用标签描述会注入抽取 prompt 的 `{{NODE_LABELS}}`。
- 抽取 prompt 已切换为 `nodes/edge_summaries`。`pipeline/extraction.py` 的 `normalize_extraction_payload()` 只接受该新结构，并严格拒绝旧 `entities/events/assertions/sources` 与模型输出来源字段。
- `nodes` 是当前构建 MemoryNode 的唯一主输入。每个 `ExtractedNode` 会转为统一 `MemoryNode`，node 内 triples 只来自模型输出的 `nodes[].triples`。
- `ExtractedNode` 不接受 `time` 字段；所有节点和边的 `time.world.event_time/source_timestamp/valid_time.start` 默认写入系统构建时 UTC 时间，和 node label 无关。
- 原始交互消息不写入 `nodes`；`Memory` 会先写入独立 `turns` 表，再把当前 `turn_id` 放入 `metadata.turn_ids`，GraphAssembler 组装出的节点和边会在 metadata 中带上 `source_turn_ids`。
- `add_memory(...)` 中同一次交互的 user / assistant 消息共享同一个 `turn_id`；`turns` 表仍按消息行保存，但写入侧会额外把该 `turn_id` 下的 User Prompt 与 Assistant Output 拼成一段完整对话日志，写入独立的 `turn_dialogue` 向量索引。Observation / tool 日志不进入该 turn dialogue 向量。
- `GraphAssembler` 负责系统组装：
  - 编排 `NodeBuilder.build_node()` 构建或复用同构 `MemoryNode`。
  - 编排 `LocalGraphBuilder` 规范化和去重 `ExtractedNode.triples`。
  - 根据 `node.edge_summary_refs` 反向收集 HyperEdge 成员。
  - 编排 `BasicHyperEdgeBuilder.build_from_summary()` 构建 description-only HyperEdge。
  - 编排 `BasicEdgeClusterBuilder` 为共享成员节点和 local-triple semantic anchor 建立 EdgeCluster 关系，并追加 cluster members。
  - 编排 `GraphMaintenance.merge_node()` 对同一 node 的跨来源 summary 和 node 内 triples 做维护。
  - 对带 `entity` label 的节点写入 entity alias index；新主路径不再写入 fact property index。

## 5. 维护

旧 `fact_merge.md`、`contradiction_check.md`、`edge_merge.md`、`edge_cluster_merge.md`、`edge_conflict_check.md` 已从 prompt registry 和 prompt 资源中移除。当前主写入路径不再保留旧 fact/property/role/polarity 维护入口。

当前已接入 Node summary 维护：

- 同一 node 被不同 `source_turn_ids` 再次写入且 incoming summary 非空时，`GraphMaintenance` 会把新 summary 追加到已有 summary。
- 系统在 `node.metadata.maintenance.node_summary` 中维护 `summary_source_turn_ids`、`pending_source_turn_ids`、`compaction_count` 和最近一次压缩触发信息；这些 ID 只由系统写入。
- 当 `pending_source_turn_ids` 数量达到 `maintenance.node_summary.compact_after_k_sources`，默认 `10`，或累计 summary 的 token 数达到 `maintenance.node_summary.max_tokens`，默认 `2048`，会强触发 `maintenance/node_summary_compaction.md`。
- 压缩 prompt 是自然语言 prompt；LLM 只返回 `{"summary": "..."}`，不输出系统 ID、来源、图结构、置信度或维护动作。
- 如果达到压缩触发条件但没有维护 LLM，写入会显式失败，不做规则兜底摘要。
- summary 变化后仍通过原有写入闭环持久化 SQLite/FTS，并重写合并后的 `node_content` 向量点。该向量文本由 `MemoryNode.content` 与 `MemoryNode.summary` 拼接生成。

当前已接入 LocalTriple 维护：

- `LocalGraphBuilder` 仍只负责对 incoming triples 做 normalized SPO 批内去重。
- 同一 node merge 时，incoming triple 会先与已有 active triples 对齐 normalized subject；若 normalized `(subject, predicate, object)` 完全相同，则不触发 LLM，只把系统来源写回既有 triple。
- 若 subject 相同、predicate 相同但 object 不同，会先收集同一 node 下所有这类冲突，再批量触发一次 `maintenance/local_triple_merge.md`。
- LLM 只做批量路由判断：一次返回与冲突数组等长、顺序一致的 `LocalTripleMergeDecision` JSON 数组；单个决策仍只允许 `keep_existing`、`keep_new`、`keep_both`、`merge`、`needs_review`。
- 系统根据 LLM 返回的 caller refs 执行动作：丢弃 incoming、追加 incoming、退役 affected existing、保存 merged triple 或把 incoming 标为 `uncertain`。
- 系统在 triple qualifiers 中维护 provenance：`source_turn_ids` 记录来源 turn，`source_triple_ids` 记录每次抽取来源实例；`maintenance_*_triple_ids` 记录被丢弃、替换、关联或合并的规范 `triple_id`。LLM 不生成这些 ID。
- 系统在 `node.metadata.maintenance.local_triples.triple_distribution` 中维护当前 node triples 的派生统计，包括总数、status 分布、active predicate 分布和 active subject/predicate 分布；该字段在 node 初次写入和每次维护更新后刷新。
- 如果有同 S/P 候选但没有维护 LLM，写入会显式失败，不做规则兜底。
- 无论 LLM 返回 `keep_existing/keep_new/keep_both/merge/needs_review` 哪个动作，该 node 都会随本次写入重写 `node_local_graph` 向量；向量文本只包含 active triples，因此被退役或标为 uncertain 的 triple 不参与拼接构建索引。

`configs/default.yaml` 当前维护配置示例：

```yaml
maintenance:
  node_summary:
    enabled: true
    compact_after_k_sources: 10
    max_tokens: 2048
    prompt: maintenance/node_summary_compaction.md
  local_triples:
    enabled: true
    prompt: maintenance/local_triple_merge.md
  hyper_edge_description:
    enabled: true
    compact_after_k_sources: 10
    max_tokens: 2048
    prompt: maintenance/hyper_edge_description_compaction.md
```

## 6. 存储

当前存储实现为 `SQLiteStore`：

- `nodes`
- `triples`
- `hyper_edges`
- `hyper_edge_members`
- `edge_clusters`
- `edge_cluster_members`
- `entity_alias_index`
- `turns`

已直接删除旧 SQLite 主路径和旧表/列依赖；开发期不做旧数据兼容。新 schema 不再创建或写入 `fact_property_index`，也不再在 `hyper_edges`、`hyper_edge_members`、`triples` 中保存旧 `edge_type`、`relation`、`polarity`、member `role`、`role_in_edge`、`edge_relation`。`triples` 表使用 `scope_edge_ids_json` 保存多值 HyperEdge scope，避免共享节点被后续 HyperEdge 覆盖旧三元组上下文。

向量索引当前通过 `c_hypermem/stores/vector_store.py` 接入：

- `VectorStore` 是向量存储抽象接口。
- `QdrantVectorStore` 是当前默认实现，使用本地 embedded Qdrant 路径 `runs/c_hypermem/vector_index`。
- SQLite 仍是 canonical store；Qdrant 只作为可重建的旁路索引。`Memory.reset(namespace)` 会同步删除该 namespace 在所有向量 collection 中的点。
- 若未配置 embedding client/model，则不会默认创建向量索引；若配置了 embedding 且 `index.vector=qdrant`，`Memory` 会创建默认 Qdrant vector stores。
- 不同向量语义类型使用不同 Qdrant collection，避免全部混入同一个向量表：
  - `c_hypermem_memory_node_local_graph`
  - `c_hypermem_memory_node_content`
  - `c_hypermem_memory_hyper_edge_description`
  - `c_hypermem_memory_edge_cluster_canonical`
  - `c_hypermem_memory_turn_dialogue`
- `node_local_graph` collection 保存 node-local-graph 向量：每个带 LocalGraph triples 的 `MemoryNode` 只写入 1 个向量点，而不是每条 triple 一个向量点。写入前会把节点核心内容和该节点内部所有 triples 揉成一段完整文本；不加入 `Core content`、`Local graph` 等额外语义注释，避免增加噪声。例如：

  ```text
  Alice prefers morning interviews
  - Alice prefers morning interviews
  ```

  payload 中仍保留 `node_id/triple_ids/triple_count/triples/attributes/node_metadata`，其中每条 triple 的 edge scope 使用 `scope_edge_ids` 列表。当一个 node 后续新增或更新 triples 时，会用同一个 `node_id` 生成的向量点 ID 覆盖更新该 node 的 local graph 向量；该向量可以被该 node 内每个 triple 通过 payload 中的 `triple_ids` 回指。如果更新后的 active node 已没有可索引 active triples，写入闭环会显式删除该 node 的 `node_local_graph` 向量点，避免旧 local graph 向量滞留。
- `node_content` 向量：索引 `MemoryNode.content` 与 `MemoryNode.summary` 的拼接文本，payload 中保留 `node_id/node_labels/status/time/metadata` 等信息。当前不再创建独立 `node_summary` 向量 collection。
- `hyper_edge_description` 向量：索引每条具体 `HyperEdge.description`，payload 中保留 `edge_id/edge_fingerprint/node_ids/member_signature/time/metadata` 等信息。
- `edge_cluster_canonical` 向量：索引 `EdgeCluster.canonical_description`，payload 中保留 `cluster_id/cluster_labels/conflict_state` 等信息。
- `turn_dialogue` 向量：只索引同一个 `turn_id` 下 role 为 `user` 和 `assistant` 的消息，按轮次拼接为完整对话日志，且 payload 中必须带 `turn_id`、`turn_index` 和 `dialogue_roles`。后续检索命中该向量时，应拿 `turn_id` 回 SQLite `turns` 表提取完整对话，而不是依赖向量库中的文本作为权威上下文。
- 当节点退役时，会删除该节点对应的 node-local-graph 和 node_content 向量点，避免非 active 节点继续被向量召回。其中 node-local-graph 向量删除显式调用 `node_local_graph` collection 对应的 vector store。

## 7. 检索现状

当前检索链路已落地为 `docs/retrieval_design.md` 描述的 Dual-Track Edge-Level RRF。入口仍是 `Memory.search(query, namespace, top_k)`，返回的每条 `SearchResult` 代表一条最终 HyperEdge，而不是单个 MemoryNode。

当前流程：

```text
Memory.search(query, namespace)
  -> Track 1: Node-Centric Edge Ranking
     -> FTS / node_content / node_local_graph 三路召回
     -> node-level RRF
     -> Top K1 seed MemoryNodes
     -> incident HyperEdges
     -> Score_track1(E) = max(S_node(v), v in E ∩ S)
                          * (1 + alpha * max(0, N_hit - 1) ^ beta)
  -> Track 2: Edge-Centric Direct Ranking
     -> hyper_edge_description vector recall
     -> active HyperEdge ranking
  -> Edge-Level RRF
     -> S_final_edge(E) = 1 / (edge_rrf_k + rank_track1(E))
                        + 1 / (edge_rrf_k + rank_track2(E))
     -> Top K2 core HyperEdges
  -> Controlled Cluster Ripple
     -> only clusters attached to Top K2 core edges may expand
  -> SearchResult(core edge + member nodes + periphery context)
```

已实现模块：

- `retrieval/lexical_recall.py`：封装 SQLite FTS 词法召回。当前 FTS 表为 `nodes_fts`，索引 `content/summary/local_graph`。
- `retrieval/vector_recall.py`：封装 node_content、node_local_graph 和 hyper_edge_description 向量召回。node 向量命中通过 payload 的 `node_id` 回 SQLite 读取 active `MemoryNode`；HyperEdge description 命中通过 payload 的 `edge_id` 回 SQLite 读取 active `HyperEdge`。
- `retrieval/fusion.py`：封装 node-level RRF，只融合 lexical / node_content / node_local_graph 三个 node 通道。
- `retrieval/ranking.py`：封装 edge-level RRF，只消费 Track 1 / Track 2 的 HyperEdge rank。
- `retrieval/graph_ripple.py`：负责 Track 1 的 node-derived edge ranking，以及 Top K2 core edges 的 controlled cluster periphery 附加。
- `retrieval/recall.py`：负责编排 Track 1、Track 2、edge-level RRF、Top K2 剪枝和 `SearchResult` 输出。
- `stores/vector_store.py`：`VectorStore` 已支持 `search(...)`，`QdrantVectorStore.search(...)` 使用 `query_points`。
- `stores/sqlite_store.py`：提供 `search_nodes_fts(...)`、`get_incident_edges(...)`、`get_edges(...)` 和 EdgeCluster 回表接口。

当前检索配置：

```yaml
retrieval:
  query_analysis: false
  node_rrf_k: 60
  edge_rrf_k: 60
  lexical_top_k: 30
  node_content_vector_top_k: 20
  node_local_graph_vector_top_k: 20
  hyper_edge_description_vector_top_k: 10
  graph_seed_top_k: 70
  edge_core_top_k: 10
  cluster_periphery_edge_limit: 20
  cluster_periphery_node_limit: 50
  edge_coherence_alpha: 0.5
  edge_coherence_beta: 2.0
  final_top_k: 10
```

`node_rrf_k` 控制 Track 1 内部 node-level RRF；`graph_seed_top_k` 控制 node RRF 后进入图扩散的 seed nodes 数量，默认不超过 `lexical_top_k + node_content_vector_top_k + node_local_graph_vector_top_k`；`edge_rrf_k` 控制 Track 1 / Track 2 汇合时的 edge-level RRF；`edge_core_top_k` 显式控制 K2 core edge 剪枝规模。`cluster_periphery_edge_limit` 和 `cluster_periphery_node_limit` 控制每条 core edge 能带出的外围 sibling edges 与 periphery nodes；设为 `null` 表示不限制，设为 `0` 表示不附加。`final_top_k` 控制最终返回的核心 HyperEdge 数量，不是 MemoryNode 数量。

Track 1 的 edge coherence 使用乘法器：

```text
Score_track1(E) =
  max(S_node(v) for v in E ∩ S)
  * (1 + alpha * max(0, N_hit(E) - 1) ^ beta)
```

其中 `N_hit(E)` 只统计 Top K1 seed nodes 中属于该边的节点数。HyperEdge description 向量命中不参与 coherence，也不会投影为成员 node 的 RRF 分数。coherence 只写入 edge-level `score_parts`，例如 `edge_coherence_multiplier` 和 `edge_coherence_bonus`。

edge-level RRF 使用 Track 1 和 Track 2 的 edge rank：

```text
S_final_edge(E) =
  1 / (edge_rrf_k + rank_track1(E))
  + 1 / (edge_rrf_k + rank_track2(E))
```

缺失 Track 的 RRF 贡献在 `score_parts` 中显式记为 `0.0`，双轨同时命中时两路贡献相加。Top K2 剪枝前使用确定性排序：`edge_rrf_score` 降序、Track 2 `track2_vector_score` 降序、Track 1 `track1_edge_score` 降序、`edge_id` 升序。

SearchResult 当前结构要点：

- `id`：`edge_id`。
- `content`：core edge description 加 core edge 内 node 内容。
- `score`：edge-level RRF 后的最终 edge score。
- `metadata.edge_metadata`：系统写入的 edge metadata，例如 `source_turn_ids`。
- `metadata.edge_nodes`：core edge 内的 active member nodes，每个 node 带 `node_id/content/summary/score/channels/score_parts/matched_vector_items/source_turn_ids/triples/time/node_metadata`。
- `metadata.edge_vector_hits`：Track 2 的 HyperEdge description 向量命中，不再混入 node 的 `matched_vector_items`。
- `metadata.score_parts`：包含 `rrf_track1`、`rrf_track2`、`track1_rank`、`track2_rank`、`track1_edge_score`、`track2_vector_score`、`edge_rrf_score` 和 tie-breaker 分数等可解释字段。
- `metadata.cluster_edge_descriptions`：如果 core edge 属于 EdgeCluster，会从该 cluster 的成员 HyperEdges 动态读取 edge descriptions。
- `metadata.periphery_edges` / `metadata.periphery_nodes`：只由 Top K2 core edges 所属 cluster 带出的 sibling context，并受 `cluster_periphery_edge_limit` / `cluster_periphery_node_limit` 限制；periphery 不参与 core edge 排名，也不会继续触发新的 cluster 扩散。

当前仍不接入：

- LLM query analysis。
- spaCy query analysis。
- entity alias recall。
- turn dialogue recall。
- temporal filter。
- recency decay / access boost。
- LLM rerank。

## 8. 与开发架构文档的实现关系

当前实现已经对齐 `development_architecture.md` 的核心方向：

- C-HyperMem 保持独立包边界，不依赖 `agent_memory_eval`。
- 对外入口收敛到 `Memory.from_config/reset/add/add_memory/search/stats/close`。
- LLM 只做一次紧凑语义抽取，输出 `nodes/edge_summaries`，不生成系统 ID、权重、来源、typed-edge 字段或构建时间。
- 系统统一生成 `MemoryNode/HyperEdge/EdgeCluster/LocalTriple` ID。
- `MemoryNode` 使用统一 schema，语义类型通过可累积 `node_labels` 表达。
- 带 `entity` label 的节点会使用 canonical_text 和显式 metadata.aliases 建 alias index，用于后续精确复用 entity node。
- `HyperEdge` 与成员表分离，`EdgeCluster` 聚合相关边但不强制合并边。当前已按共享成员 node 和 local-triple semantic anchor 聚合。
- `LocalNodeGraph` 采用统一结构，基础 triple 已持久化到 `triples` 表。
- description-only HyperEdge 已打通，来源回溯通过系统注入的 `source_turn_ids` 完成。

当前实现仍低于设计文档的部分：

- Node summary、LocalTriple 与 description-only HyperEdge description 维护已接入同构节点/边合并路径；更完整的 memory node merge/update/conflict 仍待实现。
- `state/task/instruction/tool` 已作为标签配置存在，但尚未都有专门构建策略；当前主要依靠 LLM 输出 labels 和统一节点结构承载。`turn` 已从节点标签配置中独立出来，只作为对话记录和来源追踪配置。
- 检索主流程已接入 node_content（content + summary 拼接）、node-local-graph 和 HyperEdge description 向量召回，并已按 Dual-Track Edge-Level RRF 落地；当前尚未接入 EdgeCluster canonical / variant 向量召回，也未接入 turn_dialogue 向量召回。
- EdgeCluster 当前保留为确定性锚点聚合视图；检索侧已能在命中 edge 所属 cluster 时带出 sibling edge descriptions 和 sibling edge nodes。`BasicEdgeClusterBuilder` 统一使用 `AnchorKey/AnchorOccurrence` 构建 shared-node 与 semantic-anchor clusters：共享成员 node 形成 `cluster_labels=["shared_node"]`；semantic-anchor cluster 只在不同 HyperEdge 的 active local triples 满足 `subject_subject` 至少 1 次，或 `subject_object/object_subject` 至少 1 次时建立。`edge_clusters.stop_nodes` 中的 normalized 文本（默认 `User`、`Assistant`）作为 subject 参与 `subject_subject` 时不触发 cluster；但 object 与 stop-node subject 的 `subject_object/object_subject` 交叉命中仍可触发。单独 `object_object` 不再建立 semantic cluster。两类 cluster metadata 都记录 `cluster_basis/anchor_value/anchor_occurrences/cluster_reasons`；semantic anchor 额外记录 `anchor_positions`。当前不做相似 cluster 向量召回、LLM cluster merge、后台宏观整理或复杂冲突状态维护。
- LocalNodeGraph 当前只覆盖 event participants、entity attributes 和 assertion SPO；还没有从事件内部关系、工具调用、任务状态中构建更丰富的局部图。

## 9. 设计仍不明确时的轻量替代方案

以下点在设计文档中有方向，但工程边界、触发条件或评测收益还不明确。当前实现先采用轻量方案，避免过早引入不可控复杂度：

- **实体消歧**  
  设计方向：后续可引入更复杂的 entity resolution。  
  当前方案：只用 normalized alias 和可选 `entity_type` 精确匹配。  
  原因：LLM 合并实体的误合并成本很高，尤其是不同样本、同名人物、宠物/项目重名场景。后续若引入 LLM，只能作为候选确认，不应直接覆盖 alias 精确匹配结果。

- **memory node merge / update / contradiction**
  设计方向：旧 fact merge/conflict prompt 需要泛化为统一 MemoryNode 级维护。
  当前方案：写入主路径只做确定性同 ID / entity alias 精确复用，并已实现 Node summary 的跨来源拼接与阈值压缩，以及 LocalTriple 同 S/P 候选的批量 LLM 路由维护；仍不进行规则化事实 merge、node 冲突退役或 fallback 抽取。
  原因：谓词是否多值、object 是否兼容、时间有效期如何更新都是语义判断，硬编码容易误退役事实。后续若接入维护 LLM，必须由明确候选召回触发，失败时显式失败。

- **HyperEdge 复用与合并**
  设计方向：根据 description、members、source/time 和 optional inferred metadata 召回候选并判断复用、追加成员、新版本或新建。
  当前方案：基础边保守新建，只按确定性 fingerprint 去重；成员重叠不触发合并。
  原因：成员相近的边可能表达支持、修正或冲突关系，直接合并会污染语义。后续 edge merge 必须先完成冲突感知和 description-only edge 兼容判断。

- **EdgeCluster 聚合**  
  设计方向：共享 `member_node_id` 的 HyperEdges 进入同一 EdgeCluster 视图；不同 edge 下成员 node 的 active triples 如果 subject/object 端点规范化后相同，也进入 EdgeCluster，用于检索时带出 sibling edges。  
  当前方案：不保留 EdgeCluster 维护配置，不做相似度召回、LLM cluster merge、冲突健康检查或后台整理。
  当前实现：保持 triples 单跳形态，不改写 `S-P-O`；为符合 eligibility 的 normalized endpoint value 生成 deterministic semantic anchor fingerprint；同一 edge pair 同时命中共享 node 和多个 triple anchors 时，保留 `cluster_reasons` / anchor metadata，并对 cluster members 去重。semantic-anchor eligibility 为：`subject_subject` 至少 1 次，或 `subject_object/object_subject` 至少 1 次；`edge_clusters.stop_nodes` 中的 subject 不触发 `subject_subject`，但不屏蔽交叉命中；单独 `object_object` 不成立。
  原因：EdgeCluster 先承担轻量组织视图；语义合并和冲突判断应留在具体 HyperEdge / MemoryNode 维护链路里。

- **LocalNodeGraph 丰富度**
  设计方向：节点内部保存三元组、qualifiers 和局部状态。
  当前方案：只消费 `ExtractedNode.triples`，并规范化去重；不按 label 写死 entity/event/fact 的构图策略。
  原因：所有 label 的 node 都应走同一构建路径，避免重新引入旧的分类型抽取主路径。

- **标签专门策略**  
  设计方向：`state/task/instruction/tool` 可有各自的时间、索引、检索策略；`turn` 作为独立对话记录可有自己的时间和 turn_dialogue 索引策略。  
  当前方案：`state/task/instruction/tool` 先通过统一 `MemoryNode` 承载，暂不新增专用 schema 或强规则构建器；`turn` 写入 `turns` 表，不作为节点入库。  
  原因：真实 agent 数据中的 tool/task/instruction 形态差异较大，过早固定策略会限制后续适配。

- **检索增强**
  设计方向：lexical + vector + hyperedge + edge cluster + temporal + rerank。
  当前方案：检索侧代码采用 SQLite FTS、node_content（content + summary 拼接）、node-local-graph 与 HyperEdge description。前三路先做 node-level RRF，并通过 incident HyperEdges 形成 Track 1 edge ranking；HyperEdge description 作为 Track 2 直接形成 edge ranking；两路在 edge-level RRF 汇合后 Top K2 剪枝，再做受控 cluster ripple。EdgeCluster canonical / variant 向量召回、turn_dialogue 召回、entity alias recall、temporal filter 和 LLM rerank 暂未接入。
  原因：先完成 query_analysis=false 下的可解释混合召回和 edge-centered 返回，再逐步增加更多召回通道，避免把 query analysis、rerank 和多跳召回同时引入。

- **事件驱动增量抽取**
  设计方向：每次只抽取最新 target，同时提供最近上下文辅助理解。
  当前方案：删除 `ingestion_cache` 表和应用层 hash/cache 游标配置；原始消息写入独立 `turns` 表；`LLMMemoryExtractor` 接收 `ExtractionWindow(context, target)`，`context_window_messages` 控制上下文 K。
  原因：避免在应用层维护容易失真的 prefix/cursor 状态机，把重复 system prompt 的成本交给模型服务端 prompt caching，同时保留必要的语境消解能力。

- **交互日志与知识图谱分离**
  设计方向：非结构化聊天流水账不应混入高度结构化的图谱节点。
  当前方案：`turns` 表保存原始历史交互，`nodes/hyper_edges/edge_clusters` 只保存抽取后的结构化记忆对象。当前 target 的 `turn_id` 会写入 `metadata.turn_ids`，并通过 `source_metadata()` 进入节点和边的 `source_turn_ids`。同时，系统会把同一 `turn_id` 下的 user / assistant 消息拼成 turn dialogue 向量，payload 保留 `turn_id`，用于后续命中后回 SQLite 取完整对话。
  原因：这样既能直接从交互日志实现微型滑动窗口，又能让图谱对象保留稳定溯源。

这些轻量方案的原则是：先保证统一 schema、系统生成 ID、一次抽取和基础写入闭环稳定，再逐步补维护和检索增强。

## 10. 当前实现优于或修正原设计的地方

以下实现选择已经比原始设计文字更清晰，后续应优先保留，必要时把设计文档反向更新：

- **`GraphAssembler` 只保留编排职责**  
  原设计列出了多个 pipeline 模块，但没有明确 `GraphAssembler` 是否继续承载实现细节。当前拆分为 `EntityResolver`、`NodeBuilder`、`LocalGraphBuilder`、`BasicHyperEdgeBuilder`、`BasicEdgeClusterBuilder`、`GraphMaintenance` 后，边界更清楚。后续不要把实体解析、节点构建、局部图构建或冲突维护重新塞回 `GraphAssembler`。

- **基础构建器与外部扩展 builder 并存**  
  当前 `BasicHyperEdgeBuilder` / `BasicEdgeClusterBuilder` 承担内置 M1 规则，`IngestionPipeline` 仍保留可注入的 `hyperedge_builder` / `edge_cluster_builder` 扩展点。这个形态比“只有 protocol 占位”更可运行，也比直接把规则写死在 assembler 更容易替换。

- **维护 prompt 按候选触发，不做规则兜底**
  原设计容易让后续实现把多个 maintenance prompt 串到每次写入中。当前主路径只在 Node summary 达到来源数或 token 阈值时调用 `maintenance.node_summary_compaction`，或在 LocalTriple 出现同 S/P 候选时按 node 批量调用一次 `maintenance.local_triple_merge`；后续接入新 prompt 时，应有明确召回候选、触发条件、成本控制和失败处理，不应每次写入无条件多轮调用 LLM，也不应用脆弱规则代替语义判决。

- **`nodes` 作为长期记忆对象的唯一主输入**
  当前不再接受 `entities/events/assertions/sources` 主抽取 shape，也不建立 fact property index。后续即使扩展 extraction schema，也应避免同一事实在多个字段重复入库。

- **EdgeCluster 不是 HyperEdge merge 的前置条件**  
  当前实现允许先创建具体 HyperEdge，再用共享节点 cluster 轻量聚合。EdgeCluster 不触发具体边合并，也不承担 cluster merge 维护。

- **检索扩展属于独立 retrieval 组件**
  `Retriever` 只负责编排，不直接写具体召回算法。当前拆分为 `SQLiteFTSRecall`、`DenseVectorRecall`、`reciprocal_rank_fusion` 和 `GraphRippleExpansion`。旧的 node-centered `EdgeExpansion` 与 node result formatter 已移除。

- **不同语义向量使用独立 collection**
  当前已索引 node-local-graph、合并后的节点 content/summary、HyperEdge description、EdgeCluster canonical description、EdgeCluster description variants 和 turn dialogue，但每类向量使用独立 Qdrant collection。检索侧已接入 node-local-graph、node_content、HyperEdge description 向量召回，并保持分别限流、分别解释；后续新增 EdgeCluster 或 turn_dialogue 召回时也应保持该隔离方式。

## 11. 验证

当前测试覆盖：

- 阶段 1 schema 重构：`MemoryExtraction` 可解析 `nodes/edge_summaries`；拒绝旧 `entities/events/assertions/sources`；拒绝 LLM 输出来源字段和 typed-edge 字段；`HyperEdge` schema 不再暴露 `polarity/roles`。
- 阶段 2 抽取重构：`memory_extraction.md` 输出 shape 改为 `nodes/edge_summaries`；parser 不再做旧抽取 shape 映射；字段数组和对象 shape 类型错误会直接失败。
- 阶段 3-4 写入重构：`NodeBuilder/LocalGraphBuilder/GraphAssembler` 消费同构节点和 edge summaries；新写入路径可构建 description-only HyperEdge 并通过 edge-centered retrieval 召回。
- 默认配置和 split config 加载。
- `.env` 模型变量解析。
- embedding `batch_size` 配置和分批调用。
- Context/Target 增量抽取窗口。
- SQLite schema 不再创建 `ingestion_cache`。
- SQLite `turns` 表保存交互历史，并为节点/边写入 `source_turn_ids`。
- 默认向量后端配置为 Qdrant。
- node-local-graph 向量索引按 node 聚合写入：一个 node 的 `content/triples` 拼成一段文本，只写入 1 个向量点，payload 中保留该 node 下所有 `triple_ids`。当前不再对散碎 triple 逐条 embedding。
- 写入侧统一通过 `Memory._index_nodes_edges_and_clusters(...)` 为 node-local-graph、合并后的 node content/summary、HyperEdge description、EdgeCluster canonical description 和 EdgeCluster variants 建索引。
- `MemoryNode.content` 和 `MemoryNode.summary` 分别写入独立向量 collection。
- `HyperEdge.description` 写入独立向量 collection。
- `EdgeCluster.canonical_description` 写入独立向量 collection；cluster member edge descriptions 不缓存在 EdgeCluster 上，由检索时按成员 HyperEdges 动态读取。
- 复用已有 EdgeCluster 时会追加 description variant，并参与后续向量写入。
- `turn_dialogue` 向量按 `turn_id` 轮次拼接 user / assistant 消息，跳过 observation / tool 日志，并在 payload 中保存 `turn_id`。
- 默认 Qdrant collection 命名保持按向量语义类型隔离。
- 统一节点 schema 和 SQLite 表结构。
- 显式 extractor 到系统组装链路。
- 默认节点标签集合。
- `unconfigured_label_policy` 已从配置和 prompt 渲染路径移除；未配置标签的开放性由抽取 prompt 的通用规则说明。
- 抽取 prompt 注入 `node_labels.yaml`。
- Node summary 维护：低于 `k` 时跨来源拼接并重写 node_content 向量；达到 `k` 或 token 上限时强触发 LLM 压缩；无维护 LLM 时显式失败。
- LocalTriple 维护：同 node 内 normalized S/P 相同触发按 node 批量 LLM 路由，覆盖 `keep_existing/keep_new/keep_both/merge/needs_review`，并验证 retired triples 不进入 node-local-graph 向量文本。
- 维护 prompt registry 加载 `maintenance.node_summary_compaction` 和 `maintenance.local_triple_merge`。
- LLM contradiction check 驱动的冲突 fact 退役与 correction edge。
- `loves/travels_to` 等多值语义由维护 LLM 判为 compatible 时不会被错误退役。
- EdgeCluster 当前作为确定性锚点聚合视图，多个 sibling edges 会追加到 shared-node 或 semantic-anchor cluster。
- SQLite FTS 召回通过 `nodes_fts` 检索 `content/summary/local_graph`。
- node_content、node-local-graph 两路向量召回接入检索主流程。
- lexical、node_content vector、node-local-graph vector 三路在 node-level RRF 融合后形成 Track 1 edge ranking。
- HyperEdge description 向量召回作为 Track 2 直接形成 edge ranking，不再投影到成员节点。
- Track 1 与 Track 2 在 edge-level RRF 汇合，并在 Top K2 core edges 后才触发 controlled cluster ripple。
- `edge_coherence` 在同一 HyperEdge 出现多个唯一 `node_id` seed hits 时作为 Track 1 edge-level multiplier，不写回成员 node 分数。
- `Memory.search()` 返回 top K 条 HyperEdge，每条 edge 的 `metadata.edge_nodes` 携带成员 nodes 和各 node 的 triples。

常用验证命令：

```powershell
python -m compileall -q c_hypermem
python -m pytest -q
```
