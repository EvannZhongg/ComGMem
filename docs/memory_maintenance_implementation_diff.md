# C-HyperMem Memory 维护实现差异

本文档对照 `docs/hypergraph_memory_architecture.md`、`docs/homogeneous_node_hyperedge_refactor.md`、`docs/current_implementation.md` 与当前代码，单独记录 memory 维护相关设计和实现差异。

这里的“memory 维护”主要指：

- 写入时对已有 `MemoryNode`、description-only `HyperEdge`、`EdgeCluster` 和索引的复用、更新、退役与冲突处理。
- 检索访问后对 memory activation time 的更新。
- 与维护相关的 prompt、配置、持久化和向量索引清理。

不展开完整检索 pipeline，检索设计以 `docs/current_implementation.md` 为准。

## 1. 总体结论

当前实现已经从旧 `entities/events/assertions -> fact/event/entity builder -> typed edge` 写入方式切换为：

```text
新增交互
  -> 一次 LLM 抽取 nodes / edge_summaries
  -> NodeBuilder 构建同构 MemoryNode
  -> entity alias 精确复用已有 entity node
  -> 同 node_id 的节点轻量合并
  -> LocalGraphBuilder 规范化和去重 node 内 triples
  -> edge_summary_refs 反向组装 description-only HyperEdge
  -> EdgeClusterBuilder 复用或创建 EdgeCluster，并追加 description variants
  -> GraphMaintenance 在 node merge 时维护 Node summary 和 LocalTriple
  -> 写入 SQLite / FTS / 向量索引
```

当前维护能力是**轻量、保守、无规则兜底**的：

- `GraphMaintenance` 已从 no-op post-assembly hook 改为 node merge 维护组件。
- 当前主写入路径不调用旧 `fact_merge.md`、`contradiction_check.md`、`edge_merge.md`，这些旧 prompt 已删除。
- Node summary 维护已接入：
  - 同一 node 的不同 `source_turn_ids` summary 在低于阈值时直接拼接。
  - 达到 `maintenance.node_summary.compact_after_k_sources`，默认 `10`，或 `maintenance.node_summary.max_tokens`，默认 `2048`，强触发 `maintenance/node_summary_compaction.md`。
  - LLM 只输出压缩后的 `summary`；系统继续负责 ID、来源、触发条件、状态 metadata 和索引重写。
  - 达到触发条件但没有维护 LLM 时显式失败，不做规则兜底。
- LocalTriple 维护已接入：
  - 同一 node 内 incoming triple 先匹配 existing active triples 的 normalized subject，再匹配 normalized predicate；只要 `(subject, predicate)` 相同即触发 LLM 路由。
  - LLM 输出 `keep_existing/keep_new/keep_both/merge/needs_review`，系统负责退役、追加、保存 merged triple、标记 uncertain 和索引重写。
  - 有同 S/P 候选但没有维护 LLM 时显式失败，不做规则兜底。
- 不存在旧 `fact_property_index`、`edge_type/relation/polarity/roles`、`role_in_edge/edge_relation` 兼容路径。
- 节点复用只包含：
  - 同 deterministic `node_id` 的节点合并。
  - 带 `entity` label 的节点通过 `entity_alias_index` 精确复用。
- 当前不做规则化冲突判断、事实退役、fallback 抽取或旧数据迁移。

低于长期设计的部分：

- 还没有统一 MemoryNode merge/update/conflict 的 LLM 维护链；当前仅完成 Node summary 和 LocalTriple 维护。
- 还没有 description-only HyperEdge 的候选召回、复用、成员追加或版本化维护。
- EdgeCluster 只做确定性聚合与 description variant 追加，没有后台 cluster merge 或冲突健康检查。
- access maintenance 只更新命中 nodes，不更新 HyperEdge / EdgeCluster。
- `time.relative_decay`、`access_boost`、temporal filter 尚未进入评分和维护流程。

## 2. 当前维护状态对照

| 维护点 | 当前实现 | 差异判断 |
| --- | --- | --- |
| 一次抽取 | LLM 只输出 `nodes/edge_summaries/metadata`；schema 拒绝旧 `entities/events/assertions/sources` | 已对齐新架构 |
| Node summary | `ExtractedNode.summaries` 可为空；`NodeBuilder` 将非空 summaries 拼成 `MemoryNode.summary` | 不保证每个 node 都有 summary |
| Node summary 维护 | `GraphMaintenance.merge_node()` 按系统 `source_turn_ids` 追踪 summary 来源；低于 `k` 时字符串拼接；达到来源数或 token 上限时调用 `node_summary_compaction` prompt 压缩 | 已完成第一阶段 Node summary 维护 |
| Entity alias 复用 | 仅带 `entity` label 的节点使用 `canonical_text + metadata.aliases` 写入/查询 alias index | 已实现精确复用，不做模糊消歧 |
| 普通节点复用 | `node_id = hash(namespace + fingerprint)`；同 ID 时合并 labels/attributes/metadata/local_graph | 已实现轻量合并 |
| LocalGraph 维护 | incoming triples 先按 normalized SPO 去重；同 node merge 时 normalized S/P 相同触发 `local_triple_merge` LLM 路由 | 已完成轻量 LocalTriple 维护 |
| HyperEdge 构建 | 由 `edge_summaries[].description` + member nodes 构建；schema/DB 不含 `edge_type/relation` | 已切到 description-only |
| HyperEdge merge | 仅按 deterministic `edge_id` 批内去重和 DB upsert；无候选召回或 LLM merge | 未接入 |
| EdgeCluster 维护 | 按 edge description / optional metadata fingerprint 复用 cluster，追加 description variants | 轻量实现 |
| 维护 prompt | 旧 fact/typed-edge prompts 已删除；当前保留 `maintenance.node_summary_compaction` 和 `maintenance.local_triple_merge` | 已清理旧主路径 prompt |
| 退役/冲突 | 当前主路径没有 node conflict 判断、retire/invalidated 维护链 | 未接入 |
| 向量索引维护 | upsert active nodes/edges/clusters；retired nodes 的 node vectors 有删除入口，但当前主路径几乎不产生 retired nodes | 部分实现 |
| 访问维护 | `Memory.search()` 后对结果中的 nodes 调用 `touch_node_access()` | 只覆盖 nodes |

## 3. 当前真实维护链路

### 3.1 写入入口

代码入口：

- `c_hypermem/memory.py`
- `c_hypermem/pipeline/ingestion.py`
- `c_hypermem/pipeline/assembly.py`

`Memory.add_memory(...)` 会：

1. 规范化为 `AgentInteraction`。
2. 分配 `turn_id` / `turn_index`。
3. 把原始消息写入 `turns` 表。
4. 将最近 K 个 turn messages 作为 context，把本次消息作为 target 交给 extractor。
5. `LLMMemoryExtractor` 或显式 extractor 返回 `MemoryExtraction(nodes, edge_summaries, metadata)`。
6. `GraphAssembler` 生成 nodes / edges / edge_clusters / edge_cluster_members / entity_aliases。
7. `GraphAssembler` 在同 node_id / entity alias 复用时调用 `GraphMaintenance.merge_node(...)` 维护 Node summary 和 LocalTriple。
8. `Memory._persist_output(...)` 写入 SQLite，并更新 FTS / Qdrant side indexes。

当前实现要点：

- context 只用于消解 target 中的指代、省略和相对表达；不会单独从 context 生成 memory。
- 系统将当前 `turn_id` 写入 `metadata.turn_ids`，再由 `source_metadata()` 注入 node / edge 的 `source_turn_ids`；如果 LLM metadata 中出现同名来源字段，系统来源会覆盖它。
- 原始 turns 与结构化 memory graph 分离。

### 3.2 Node 构建与 summary 行为

代码入口：

- `c_hypermem/pipeline/node_builder.py`
- `c_hypermem/pipeline/graph_utils.py`
- `c_hypermem/schema.py`

当前 `ExtractedNode` 字段：

```text
ref
labels
canonical_text
summaries
triples
edge_summary_refs
metadata
```

`MemoryNode.summary` 的来源：

- `NodeBuilder.build_node()` 读取 `ExtractedNode.summaries`。
- 会过滤空字符串。
- 用空格拼接为单个 `MemoryNode.summary`。
- 如果 LLM 没给 summaries，summary 就是空字符串。
- 空 summary 不会写入 `node_summary` 向量。

同一 node 未来再次被抽取出来时：

- 如果 incoming node 与已有 node 同 `node_id`，或 entity alias 命中已有 entity node，则进入 `GraphMaintenance.merge_node(existing, incoming, context)`。
- 当前 merge 行为：
  - labels 取并集。
  - attributes / metadata 做深合并。
  - incoming local_graph triples 先按 normalized SPO 去重；不同 S/P 直接追加。
  - 若 incoming triple 与 existing active triple 的 normalized S/P 相同，则调用 `maintenance.local_triple_merge` prompt 做路由判断。
  - 如果 incoming summary 非空且来自新的 `source_turn_ids`，会追加到 existing summary。
  - `node.metadata.maintenance.node_summary.summary_source_turn_ids` 记录已进入 summary 的来源。
  - `node.metadata.maintenance.node_summary.pending_source_turn_ids` 记录上次压缩以来的来源批次。
  - pending 来源数达到 `maintenance.node_summary.compact_after_k_sources`，或 summary token 数达到 `maintenance.node_summary.max_tokens` 时，强触发 `maintenance.node_summary_compaction`。
  - `content` 同理，只有旧 content 为空时才补写。
  - 更新 `updated_at/updated_turn`。

因此当前 summary 维护是**来源驱动、强触发、无兜底**的：

- 低于阈值时仅拼接不同来源 summary，并随写入重建 `node_summary` 向量。
- 达到阈值时必须由维护 LLM 生成压缩摘要。
- LLM 不输出 node_id、source_turn_ids、图结构、置信度或维护动作。
- 无维护 LLM 或返回空 summary 时写入失败。

当前仍不会用 summary 内容做规则化冲突判断，也不会由代码兜底生成摘要。

### 3.3 Entity alias 维护

代码入口：

- `c_hypermem/pipeline/assembly.py`
- `c_hypermem/stores/sqlite_store.py`

当前行为：

- 只有 node labels 中包含 `entity` 时才参与 alias index。
- alias 候选来自：
  - node 的 `canonical_text`
  - LLM 显式输出的 `metadata.aliases`
- 写入 `entity_alias_index(namespace, normalized_alias, entity_type, node_id, source_count, updated_at)`。
- 新 entity node 构建时，会先用 normalized aliases 查询已有 alias entry。
- 命中后加载已有 node 并调用 `merge_node()`。

边界：

- 这不是规则化抽取；不会从文本中猜别名。
- 不对非 entity label 建 alias index。
- 不做模糊匹配、跨样本同名消歧或 LLM entity merge。

### 3.4 LocalGraph 维护

代码入口：

- `c_hypermem/pipeline/local_graph_builder.py`
- `c_hypermem/pipeline/graph_utils.py`
- `c_hypermem/stores/sqlite_store.py`

当前行为：

- 每个 node 都可以携带 `ExtractedNode.triples`。
- `LocalGraphBuilder.build_node()` 对 incoming triples 按 normalized `(subject, predicate, object)` 去重。
- `GraphMaintenance.merge_node()` 对 different S/P triples 直接追加。
- 当 incoming triple 和 existing active triple 的 normalized `(subject, predicate, object)` 完全相同时，不调用 LLM，只合并系统来源 provenance。
- 当 incoming triple 和 existing active triple 的 normalized `(subject, predicate)` 相同但 object 不同时，调用 `maintenance/local_triple_merge.md`。
- LLM 路由动作：
  - `keep_existing`: 丢弃 incoming。
  - `keep_new`: 退役 affected existing，保存 incoming。
  - `keep_both`: 保存 incoming，旧 triple 保持 active。
  - `merge`: 退役 affected existing，保存 LLM 返回的 merged triple。
  - `needs_review`: 保存 incoming 且标记为 `uncertain`。
- triple qualifiers 中由系统维护 `source_turn_ids` 与 `source_triple_ids`；LLM 路由触发后，系统还会写入 `maintenance_discarded_triple_ids`、`maintenance_replaced_triple_ids`、`maintenance_related_triple_ids` 或 `maintenance_merged_triple_ids` 等追踪字段。
- 退役或 uncertain 状态由系统写入 `LocalTriple.status` 和 maintenance qualifiers；LLM 不输出 `triple_id` 或系统来源字段。
- 如果出现同 S/P 候选但没有维护 LLM，写入显式失败。
- SQLite `triples` 表持久化 node 内 triples。
- 无论路由结果是 `keep_existing/keep_new/keep_both/merge/needs_review` 哪一种，该 node 都会在本次写入后重写 `node_local_graph` 向量；非 active triples 保留在 canonical store 中，但不会进入 SQLite FTS local_graph 文本、node-local-graph 向量文本或检索返回的 active triples。
- `HyperEdge` scope 会写入 triple 的：
  - `scope_edge_id`
  - qualifiers 中的 `scope_edge_id`
  - qualifiers 中的 `edge_description`

已删除：

- 不再按 event/fact/entity label 写死 local graph。
- 不再有 local graph roles。
- 不再写 `role_in_edge` / `edge_relation`。
- 不再有 `polarity` attribute 主路径。

缺口：

- triple-level valid_time、superseded_by / invalidated_by 串联尚未接入。
- 工具调用、任务状态、观察结果等复杂结构仍完全依赖 LLM 输出 triples，代码不做规则抽取。

### 3.5 HyperEdge 维护

代码入口：

- `c_hypermem/pipeline/hyperedge_builder.py`
- `c_hypermem/pipeline/assembly.py`
- `c_hypermem/stores/sqlite_store.py`

当前行为：

- `ExtractedEdgeSummary.description` 是 edge 的核心语义。
- `GraphAssembler` 根据 `node.edge_summary_refs` 反向收集成员 nodes。
- `BasicHyperEdgeBuilder.build_from_summary()` 创建 `HyperEdge(description, node_ids, metadata, time)`。
- `edge_id` 由 description、member_node_ids、source_turn_ids、edge_ref 生成 fingerprint 后派生。
- `member_signature` 由 node_ids 生成。
- SQLite `hyper_edges` 表不包含 `edge_type/relation/polarity`。
- SQLite `hyper_edge_members` 表不包含 role。

当前没有：

- 相似 HyperEdge 召回。
- description-only edge merge prompt 调用。
- member append/version 判断。
- edge retired/invalidated 维护。
- edge access time 更新。

### 3.6 EdgeCluster 维护

代码入口：

- `c_hypermem/pipeline/edge_cluster_builder.py`
- `c_hypermem/stores/sqlite_store.py`

当前行为：

- 对每条 concrete HyperEdge 生成或复用一个 EdgeCluster。
- 默认 cluster label 为 `memory_context`。
- cluster description 当前来自 edge description。
- fingerprint 默认基于 cluster description + label。
- 同 fingerprint cluster 命中时追加 `description_variants`。
- `EdgeClusterMember.relation_to_cluster` 当前仍存在，默认多为 `supports`；这是 cluster 内部成员关系，不是 HyperEdge `relation` 字段。

当前没有：

- EdgeCluster 相似向量召回。
- LLM cluster merge。
- LLM conflict health check。
- 后台维护调度器。

### 3.7 索引与访问维护

代码入口：

- `c_hypermem/memory.py`
- `c_hypermem/stores/sqlite_store.py`
- `c_hypermem/stores/vector_store.py`
- `c_hypermem/retrieval/vector_recall.py`
- `c_hypermem/utils/time.py`

当前 SQLite / FTS：

- active node 写入 `nodes_fts`。
- 非 active node upsert 时会删除旧 FTS 行且不再插入。

当前 Qdrant collections：

- `node_content`
- `node_summary`
- `node_local_graph`
- `hyper_edge_description`
- `edge_cluster_canonical`
- `edge_cluster_variant`
- `turn_dialogue`

当前 node-local-graph embedding 文本：

```text
<node.content>
- <triple 1 subject predicate object>
- <triple 2 subject predicate object>
```

不再加入 `Core content:` / `Local graph:` 等语义注释。

当前 edge indexing：

- 每条 concrete `HyperEdge.description` 写入 `hyper_edge_description` collection。
- EdgeCluster canonical / variants 仍分别写入独立 collection。
- 当前检索主流程尚未使用 `hyper_edge_description`、EdgeCluster 或 turn_dialogue 向量召回；它们是可用索引资产。

当前访问维护：

- `Memory.search()` 返回后，会对结果 `metadata.edge_nodes` 中的 nodes 调用 `touch_node_access()`。
- 只更新 node 的 `last_access_turn/access_count`。
- 不更新 HyperEdge / EdgeCluster access time。

## 4. 后续维护重构建议

### 4.1 维护重构的边界

后续应继续把维护对象从旧 fact/property 改成：

```text
MemoryNode
HyperEdge(description-only)
EdgeCluster
LocalTriple
Indexes
```

禁止重新引入：

- 规则化抽取。
- fallback 抽取。
- `fact_property_index`。
- `edge_type/relation/polarity/roles` 作为 HyperEdge schema 或 DB 字段。
- 旧数据兼容迁移。

### 4.2 MemoryNode 维护

建议新增 node-level maintenance，而不是恢复 fact-level maintenance：

- 候选召回应先来自确定性信号：
  - 同 `node_id`。
  - entity alias 精确命中。
  - 可选：向量召回出的同 namespace active nodes。
- LLM 维护只在有明确候选时触发。
- 无 LLM 或 LLM 失败时应显式失败，不做规则兜底。

待定义决策：

```text
reuse_node
update_node
keep_separate
retire_existing
needs_review
```

当前已落地的 summary 维护契约：

- 小于 `maintenance.node_summary.compact_after_k_sources` 时，同一 node 的不同来源 summary 直接字符串拼接。
- 即使未达到 `k`，只要 summary token 数达到 `maintenance.node_summary.max_tokens`，也会触发压缩。
- 压缩由 `maintenance/node_summary_compaction.md` 完成，输入包含 node context、累计 summary 和触发原因。
- 输出只允许压缩后的 `summary`，不允许输出系统 ID、来源字段、图结构、置信度或维护动作。
- summary 更新后必须重新写入 `node_summary` 向量。

仍待定义的 node-level merge/update/conflict 维护：

- 是否更新 `content`。
- 是否追加或退役 triples。
- 是否 retire/invalidate existing node。
- 是否 keep separate 或 needs review。

### 4.3 LocalTriple 维护

当前 LocalTriple 已支持同 S/P 候选的轻量 LLM 路由。下一阶段可增加：

- triple-level superseded_by / invalidated_by。
- valid_time / qualifier 维护。

但不应通过 predicate 白名单或硬编码多值谓词判断冲突。冲突必须由候选召回 + LLM 语义判断完成。

### 4.4 HyperEdge 维护

当前 concrete HyperEdge 保守创建。下一阶段可接入 description-only edge maintenance：

- 使用 `hyper_edge_description` 向量召回候选 edges。
- 成员重叠只能作为召回信号，不能作为合并依据。
- LLM 输入应包含：
  - candidate edge description
  - candidate member node summaries / canonical_text
  - candidate source_turn_ids
  - existing edge descriptions
  - existing member node summaries / canonical_text
  - source/time metadata
- 输出应避免 typed relation/roles/polarity。

候选决策可为：

```text
reuse_edge
append_members
new_version
new_edge
needs_review
```

维护动作：

- `reuse_edge`: 复用 edge_id，追加 source metadata / description variant。
- `append_members`: 更新 node_ids、member_signature、member_version。
- `new_version`: 新建 edge 或版本化旧 edge，具体策略需先定。
- `new_edge`: 保持独立 edge。
- `needs_review`: 标记 uncertain 或保守新建。

### 4.5 EdgeCluster 维护

下一阶段可分两层：

1. 写入时轻量维护：
   - 新 edge 挂入 cluster 后，可由 LLM 判断 `relation_to_cluster` 和 `conflict_state`。
   - 只在候选 cluster 明确时触发。

2. 后台维护：
   - 根据 `maintenance.edge_cluster.background.trigger_every_k_writes` 低频触发。
   - 使用 EdgeCluster canonical / variants 向量召回相似 clusters。
   - 调用 cluster merge prompt。

仍需保持：

- EdgeCluster 聚合不等于 HyperEdge merge。
- conflict cluster 可以容纳相互冲突的 concrete edges。

### 4.6 索引维护

下一阶段维护动作必须同步索引：

- node summary/content/local_graph 变化后重写对应向量点。
- edge description / members / metadata 变化后重写 `hyper_edge_description` 向量点。
- cluster canonical / variants 变化后重写 cluster 向量点。
- node retired/invalidated 后删除 node_content、node_summary、node_local_graph 点。
- edge retired/invalidated 后删除或过滤 `hyper_edge_description` 点。
- cluster retired/merged 后删除或过滤 cluster 向量点。

当前 `QdrantVectorStore.delete_namespace()` 可以 reset namespace；更细粒度删除已支持 point ids，但 edge/cluster retired path 还没接入。

## 5. 当前实现优先级判断

建议保持的原则：

- 一次抽取，系统组装。
- LLM 不生成系统 ID、来源字段、typed-edge 字段或构建时间。
- 维护 prompt 按候选触发，不无条件串联多轮 LLM。
- 无维护 LLM 时不做规则兜底。
- HyperEdge 保守新建，不因成员重叠直接合并。
- EdgeCluster 聚合不等于 HyperEdge merge。
- SQLite 是 canonical store，向量索引是可重建旁路索引。
- 原始 turns 与结构化 memory graph 分离。

优先补齐项：

1. 定义 MemoryNode merge/update/conflict prompt 和 schema。
2. 为 LocalTriple 维护补充 valid_time、superseded_by / invalidated_by 链接和更细的索引删除策略。
3. 基于 `hyper_edge_description` 向量召回设计 edge maintenance。
4. 为 EdgeCluster 增加 conflict health / relation_to_cluster 维护。
5. 明确 retired/invalidated node/edge/cluster 的向量删除策略。
6. 将 access maintenance 扩展到 HyperEdge / EdgeCluster，或明确只以 node activation 作为评分依据。
7. 继续补齐 memory node merge/conflict、description-only edge maintenance 和 EdgeCluster health/merge prompt。

## 6. 代码位置索引

- 对外入口：`c_hypermem/memory.py`
- 写入编排：`c_hypermem/pipeline/ingestion.py`
- 图组装：`c_hypermem/pipeline/assembly.py`
- 维护 hook：`c_hypermem/pipeline/maintenance.py`
- 节点构建：`c_hypermem/pipeline/node_builder.py`
- 局部图构建：`c_hypermem/pipeline/local_graph_builder.py`
- 超边构建：`c_hypermem/pipeline/hyperedge_builder.py`
- 簇构建：`c_hypermem/pipeline/edge_cluster_builder.py`
- 持久化：`c_hypermem/stores/sqlite_store.py`
- 向量索引：`c_hypermem/stores/vector_store.py`
- 向量召回：`c_hypermem/retrieval/vector_recall.py`
- 时间维护：`c_hypermem/utils/time.py`
- 维护 prompts：`c_hypermem/prompts/maintenance/`
- 当前同构测试：`tests/test_homogeneous_ingestion.py`
