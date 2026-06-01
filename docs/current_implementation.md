# C-HyperMem 当前实现状态

本文档描述当前代码的真实实现。后续修改代码或架构时，需要同步更新本文档；如果本文与较早的设计文档存在冲突，以当前代码和本文为准。

## 1. 对外入口

- `c_hypermem.Memory` 是推荐入口，支持 `from_config/reset/add/add_memory/search/stats/close`。
- `Memory.from_config(...)` 接受配置文件路径、dict、`MemoryConfig` 或 `None`。
- `add_memory(...)` 面向 agent 交互，会规范化为 `AgentInteraction`，并把同一次 user/assistant 交互写入同一个 `turn_id`。
- `add(...)` 面向低层导入，会把字符串或 message dict 列表规范化为 `MemoryImportBatch`，并按消息顺序逐条增量写入。
- 如果配置中存在 `llm` 且未显式传入 extractor，`Memory` 会创建默认 `LLMMemoryExtractor`。也可以显式传入自定义 extractor、maintenance LLM、embedding client、vector store 或外部 builder。

## 2. 配置与环境

- 默认配置入口为 `configs/default.yaml`，其中 `include` 会合并 `configs/models.yaml` 和 `configs/node_labels.yaml`。
- `.env` 只从 C-HyperMem 项目根目录读取，用于解析 `CHYPERMEM_LLM_*` 和 `CHYPERMEM_EMBEDDING_*` 环境变量。
- LLM 与 embedding 客户端均使用 OpenAI-compatible API。
- 默认主存储为 SQLite：`runs/c_hypermem/memory.sqlite3`。
- 默认向量后端为本地 embedded Qdrant：`runs/c_hypermem/vector_index`。
- `ingestion.pass_recent_context` 默认 `false`；开启后由 `ingestion.context_window_messages` 控制最近上下文 turn 数。
- `maintenance` 当前包含 `node_summary`、`local_triples`、`hyper_edge_description` 三类维护配置。
- `edge_clusters.enabled` 控制是否构建 EdgeCluster；`edge_clusters.stop_nodes` 来自 `configs/node_labels.yaml`，默认包含 `User` 和 `Assistant`。
- 默认节点标签包括 `entity/fact/state/preference/task/event/instruction/tool`，其中 `tool.enabled=false`。
- `turn` 不是 `MemoryNode` 标签；它是独立的原始对话记录配置，写入 `turns` 表并可写入 `turn_dialogue` 向量索引。

## 3. Schema

核心 schema 位于 `c_hypermem/schema.py`：

- `MemoryNode`：统一记忆节点，使用 `node_labels` 表达语义类型，节点内部可携带 `LocalNodeGraph`。
- `LocalNodeGraph`：节点内部局部图，目前只包含 `LocalTriple` 列表。
- `LocalTriple`：包含 `subject/predicate/object/status/scope_edge_ids/qualifiers` 等字段。系统在 qualifiers 中维护来源和维护元数据。
- `HyperEdge`：description-only 高阶关系实例，核心由 `description`、`node_ids`、`weights`、`metadata` 和时间信息组成。
- `EdgeCluster`：确定性锚点聚合视图，用于把相关 HyperEdges 组织在一起，不负责合并 HyperEdge。
- `Message/AgentInteraction/MemoryImportBatch`：写入入口的原始交互结构。
- `SearchResult`：检索输出结构，每条结果以一个核心 HyperEdge 为中心。

LLM 抽取输出 schema：

- `MemoryExtraction` 只包含 `nodes` 和 `edge_summaries`。
- `ExtractedNode` 包含 `ref/labels/canonical_text/summaries/triples/edge_summary_refs/metadata`。
- `ExtractedEdgeSummary` 包含 `ref/description/metadata`。
- `normalize_extraction_payload()` 要求 payload 必须有 `nodes` 和 `edge_summaries`，并由 Pydantic 禁止额外字段。
- 抽取模型不生成系统 ID、来源字段、构建时间、权重、typed-edge 字段或图结构字段。

## 4. 写入 Pipeline

当前写入链路：

```text
Memory.add_memory/add
  -> append turns
  -> IngestionPipeline
  -> LLMMemoryExtractor 或显式 extractor
  -> GraphAssembler
     -> NodeBuilder
     -> GraphMaintenance
     -> BasicHyperEdgeBuilder
     -> BasicEdgeClusterBuilder
  -> SQLiteStore
  -> Qdrant side indexes
```

写入行为：

- `Memory` 先把原始消息写入 `turns` 表，并把当前 `turn_id` 注入 interaction metadata。
- `LLMMemoryExtractor` 渲染 `prompts/extraction/memory_extraction.md`，输入为 `ExtractionWindow(context, target)`。
- `context` 只作为最近上下文辅助理解；新增记忆只能来自 `target`。
- 启用的 node label 描述会注入抽取 prompt。
- `NodeBuilder` 把每个 `ExtractedNode` 转成 `MemoryNode`，系统生成 node id、fingerprint、时间和来源 metadata。
- 带 `entity` label 的节点会使用 canonical text 和 `metadata.aliases` 做 alias 精确复用。
- `GraphMaintenance.merge_node()` 负责同一节点的 summary 和 local triples 维护。
- `BasicHyperEdgeBuilder` 根据 `edge_summaries` 和反向收集到的成员节点构建 HyperEdge。
- `GraphMaintenance.merge_edge()` 负责 HyperEdge description 的来源累计和阈值压缩。
- `BasicEdgeClusterBuilder` 基于共享成员节点和 local triple semantic anchor 构建 EdgeCluster。
- `Memory._persist_output()` 负责写入 SQLite、更新向量索引、写入 entity alias。
- `turn_dialogue` 向量只拼接同一 turn 下的 user/assistant 消息，不包含 observation/tool 日志。

## 5. 维护

维护逻辑集中在 `c_hypermem/pipeline/maintenance.py`。维护 prompt 只在明确候选或阈值条件下触发；没有维护 LLM 时，语义维护场景会显式失败，不做规则兜底。

### Node Summary

- 同一 node 被不同来源再次写入时，incoming summaries 会进入该 node 的 summary 状态。
- 低于阈值时，summary 以来源片段形式累计。
- 当 pending source 数量达到 `maintenance.node_summary.compact_after_k_sources`，或 token 数达到 `maintenance.node_summary.max_tokens`，会调用 `maintenance/node_summary_compaction.md`。
- LLM 只返回 `{"summary": "..."}`。
- summary 变化后会重写 SQLite、FTS 和 `node_content` 向量。

### LocalTriple

- `LocalGraphBuilder` 先对 incoming triples 做 normalized SPO 批内去重。
- 新 node 初始化和已有 node merge 都会经过同一个 local triple 维护入口。
- normalized SPO 完全相同的 triple 不触发 LLM，只合并系统来源。
- normalized subject/predicate 相同但 object 不同的候选会批量调用 `maintenance/local_triple_merge.md`。
- maintenance 决策必须携带对应 conflict 的 `incoming_ref`，系统按 ref 对齐决策，不依赖数组顺序。
- LLM 决策仅允许 `keep_existing/keep_new/keep_both/merge/needs_review`。
- 系统根据决策追加、退役、合并或标记 uncertain triples，并维护 `source_turn_ids/source_triple_ids/maintenance_*` qualifiers。
- `node.metadata.maintenance.local_triples.triple_distribution` 保存 active/status/predicate 分布等派生统计。
- `node_local_graph` 向量只包含 active triples。

### HyperEdge Description

- HyperEdge description 会按来源累计。
- 当来源数或 token 数达到 `maintenance.hyper_edge_description.*` 阈值时，会调用 `maintenance/hyper_edge_description_compaction.md`。
- LLM 只返回 `{"description": "..."}`。
- description 变化后会重写 SQLite 和 `hyper_edge_description` 向量。

## 6. EdgeCluster

`BasicEdgeClusterBuilder` 构建两类 cluster：

- `shared_node`：多个 HyperEdges 共享同一个 member node id。
- `semantic_anchor`：不同 HyperEdges 的 active local triples 在 subject/object endpoint 上出现可用交叉。

semantic anchor eligibility：

- `subject_subject` 可以触发，但 anchor value 在 `edge_clusters.stop_nodes` 中时不触发。
- `subject_object` 和 `object_subject` 可以触发。
- `object_object` 不触发。
- stop_nodes 只屏蔽 `subject_subject`，不屏蔽 `subject_object/object_subject`。

EdgeCluster 只作为检索时的上下文扩展视图：

- 不触发 HyperEdge merge。
- 不做相似度聚类。
- 不调用 LLM cluster merge。
- 不承担全局冲突维护。
- metadata 保存 `cluster_basis/anchor_value/anchor_occurrences/cluster_reasons`，semantic anchor 还保存 `anchor_positions`。

## 7. 存储与索引

SQLite 是 canonical store，当前表包括：

- `nodes`
- `nodes_fts`
- `triples`
- `hyper_edges`
- `hyper_edge_members`
- `edge_clusters`
- `edge_cluster_members`
- `entity_alias_index`
- `turns`

SQLite 行为：

- `nodes_fts` 索引 node 的 `content/summary/local_graph`。
- `triples.scope_edge_ids_json` 保存一个 triple 关联的多个 HyperEdge scope。
- `turns` 以 message 行保存原始交互，并记录 `inserted_at`，用于检索上下文中的真实插入时间。
- `Memory.reset(namespace)` 会清理该 namespace 的 SQLite 记录和所有向量 collection 中的点。

向量索引由 `c_hypermem/stores/vector_store.py` 提供：

- `node_content`：索引 `MemoryNode.content + summary`。
- `node_local_graph`：按 node 聚合索引 active local triples，每个 node 一个向量点。
- `hyper_edge_description`：索引 `HyperEdge.description`。
- `edge_cluster_canonical`：索引 `EdgeCluster.canonical_description`。
- `turn_dialogue`：索引同一 turn 下 user/assistant 拼接文本。

每类语义向量使用独立 Qdrant collection，collection 名称由 `index.vector_store.collection_name` 加 item type 后缀组成。SQLite 是权威数据源，Qdrant 是可重建旁路索引。

当前检索实际使用 `node_content`、`node_local_graph`、`hyper_edge_description`；`edge_cluster_canonical` 和 `turn_dialogue` 已写入但未接入召回主流程。

## 8. 检索

入口为 `Memory.search(query, namespace, top_k)`，返回 edge-centered `SearchResult`。

当前检索流程：

```text
query
  -> query analysis
  -> SQLite FTS node recall
  -> node_content vector recall
  -> node_local_graph vector recall
  -> node-level RRF
  -> Track 1 node-derived edge ranking
  -> hyper_edge_description vector recall
  -> Track 2 direct edge ranking
  -> edge-level RRF
  -> Top K2 core edges
  -> controlled cluster periphery
  -> SearchResult
```

模块职责：

- `retrieval/query_analysis.py`：支持 `false`、`"llm"`、`"nlp"` 三种模式，默认 `false`。
- `retrieval/lexical_recall.py`：从 SQLite FTS 召回 nodes。
- `retrieval/vector_recall.py`：从 node_content、node_local_graph、hyper_edge_description 向量召回 nodes/edges。
- `retrieval/fusion.py`：对 node 召回结果做 RRF。
- `retrieval/graph_ripple.py`：把 node seeds 转成 incident HyperEdges，并附加受限 cluster periphery。
- `retrieval/ranking.py`：对 Track 1 和 Track 2 做 edge-level RRF。
- `retrieval/recall.py`：编排完整检索流程并格式化 `SearchResult`。

Track 1 edge score：

```text
Score_track1(E) =
  max(S_node(v) for v in E ∩ seeds)
  * (1 + alpha * max(0, N_hit(E) - 1) ^ beta)
```

Track 2 直接使用 `hyper_edge_description` 向量命中的 edge 分数排序。Track 1 和 Track 2 汇合时使用 edge-level RRF：

```text
S_final_edge(E) =
  1 / (edge_rrf_k + rank_track1(E))
  + 1 / (edge_rrf_k + rank_track2(E))
```

SearchResult 内容：

- `id` 是核心 `edge_id`。
- `content` 先输出 core edge，再输出 sibling edges；每个 memory block 下拼接该 edge 相关 node 的 active triples。
- edge 行始终可输出 `current_turn_id=turn:N`。
- `recall.include_turn_ids_in_context=true` 时，edge/triple 行追加 `source_turn id=...`。
- `recall.include_real_time_in_context=true` 时，追加 `real time=...`，来源为 `turns.inserted_at`。
- metadata 中保留 edge、node、triple、score parts、query analysis、cluster periphery 和 relative time 等结构化信息。

当前默认检索配置：

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
  edge_coherence_alpha: 0.5
  edge_coherence_beta: 2.0
  final_top_k: 10
recall:
  cluster_periphery_edge_limit: 15
  cluster_periphery_node_limit: 50
  node_triple_limit: 30
  include_turn_ids_in_context: true
  include_real_time_in_context: false
```

## 9. 当前未接入的能力

以下能力有配置或代码雏形，但不在默认主链路中，或尚未实现为召回/维护策略：

- `edge_cluster_canonical` 向量召回。
- `turn_dialogue` 向量召回。
- entity alias recall。
- temporal filter。
- recency decay / access boost。
- LLM rerank。
- 完整 memory node merge / conflict / contradiction 维护。
- tool label 专门构建策略。
- task/instruction/state 的专用检索优先级。

## 10. 当前工程原则

- 抽取只输出紧凑语义候选；系统负责 ID、来源、时间、scope、存储和索引。
- 不在写入链路中加入规则化抽取或兜底抽取。
- 维护 prompt 必须有明确候选、触发条件和失败语义。
- EdgeCluster 是上下文组织视图，不是 HyperEdge merge 的前置条件。
- SQLite 是权威存储，向量索引必须可重建。
- 不同语义向量使用独立 collection，检索侧按通道分别限流和解释。

## 11. 验证

常用验证命令：

```powershell
python -m compileall -q c_hypermem
python -m pytest -q
python examples\quickstart.py
```

`examples/quickstart.py` 会真实调用 LLM、embedding、SQLite、Qdrant 和检索链路；成功时会输出：

```text
[quickstart] all checks passed; model, embedding, storage, indexing, and retrieval configs look OK.
```
