# C-HyperMem Memory 维护实现差异

本文档对照 `docs/hypergraph_memory_architecture.md`、`docs/homogeneous_node_hyperedge_refactor.md`、`docs/current_implementation.md` 与当前代码，单独记录 memory 维护相关设计和实现差异。

这里的“memory 维护”主要指：

- 写入时对已有 `MemoryNode`、description-only `HyperEdge` 和索引的复用、更新、退役与冲突处理。`EdgeCluster` 当前作为确定性锚点聚合视图，不作为维护对象。
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
  -> EdgeClusterBuilder 为共享成员节点的 HyperEdges 建立 EdgeCluster 关系
  -> GraphMaintenance 在 node merge 时维护 Node summary 和 LocalTriple
  -> 写入 SQLite / FTS / 向量索引
```

当前维护能力是**轻量、保守、无规则兜底**的：

- `GraphMaintenance` 已从 no-op post-assembly hook 改为 node merge 维护组件。
- 当前主写入路径不调用旧 `fact_merge.md`、`contradiction_check.md`、`edge_merge.md`，这些旧 prompt 已删除。
- 当前阶段目标不是补齐重型全局维护，而是在每个流程先形成可运行、可解释的轻量实现：
  - `MemoryNode`: deterministic `node_id` / entity alias 精确复用，summary 来源驱动拼接与阈值压缩。
  - `LocalTriple`: 同 node 内 normalized S/P 候选按 node 批量触发一次 LLM 路由；完全同 SPO 只合并系统 provenance。
  - `HyperEdge`: 相同成员节点集合生成同一 `edge_id`，同 ID 时合并 description/source metadata 并重写 edge 向量。
  - `EdgeCluster`: 由共享成员节点 + local-triple semantic anchor 形成确定性锚点聚合视图，不作为维护对象。
  - `Index`: 对 active objects 做 upsert；对当前已产生的 retired node 有 node vector 删除入口。
- Node summary 维护已接入：
  - 同一 node 的不同 `source_turn_ids` summary 在低于阈值时直接拼接。
  - 达到 `maintenance.node_summary.compact_after_k_sources`，默认 `10`，或 `maintenance.node_summary.max_tokens`，默认 `2048`，强触发 `maintenance/node_summary_compaction.md`。
  - LLM 只输出压缩后的 `summary`；系统继续负责 ID、来源、触发条件、状态 metadata 和索引重写。
  - 达到触发条件但没有维护 LLM 时显式失败，不做规则兜底。
- LocalTriple 维护已接入：
  - 同一 node 内 incoming triple 先匹配 existing active triples 的 normalized subject，再匹配 normalized predicate；只要 `(subject, predicate)` 相同即进入该 node 的批量维护任务。
  - LLM 一次输出与批量冲突数组等长、顺序一致的 `LocalTripleMergeDecision` JSON 数组；单个决策为 `keep_existing/keep_new/keep_both/merge/needs_review`，系统负责退役、追加、保存 merged triple、标记 uncertain 和索引重写。
  - 有同 S/P 候选但没有维护 LLM 时显式失败，不做规则兜底。
- 不存在旧 `fact_property_index`、`edge_type/relation/polarity/roles`、`role_in_edge/edge_relation` 兼容路径。
- 节点复用只包含：
  - 同 deterministic `node_id` 的节点合并。
  - 带 `entity` label 的节点通过 `entity_alias_index` 精确复用。
- 当前不做规则化冲突判断、事实退役、fallback 抽取或旧数据迁移。

低于长期设计的部分：

- 还没有统一 MemoryNode merge/update/conflict 的 LLM 维护链；当前仅完成 Node summary 和 LocalTriple 维护。
- HyperEdge 当前只做同 `edge_id` 的轻量复用和 description/source metadata 合并；向量召回候选、LLM edge merge、成员追加和版本化属于未来计划，不是当前主要目标。
- EdgeCluster 当前只做确定性锚点聚合视图，没有维护配置、后台 cluster merge 或冲突健康检查；锚点包括共享成员节点和符合 eligibility 的 subject/object 端点重合。
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
| LocalGraph 维护 | incoming triples 先按 normalized SPO 去重；同 node merge 时 normalized S/P 相同的冲突按 node 收集，并一次触发 `local_triple_merge` LLM 批量路由 | 已完成轻量 LocalTriple 维护 |
| HyperEdge 构建 | 由 `edge_summaries[].description` + member nodes 构建；schema/DB 不含 `edge_type/relation` | 已切到 description-only |
| HyperEdge merge | `edge_id = hash(namespace + sorted(member_node_ids))`；同成员集合复用同 edge，并合并 description/source metadata；description 变化后重写 `hyper_edge_description` 向量 | 已完成轻量同 ID 维护 |
| EdgeCluster 维护 | 当前不作为维护对象；只保留共享成员节点与 local-triple semantic anchor 的确定性聚合视图，仍不引入 LLM cluster 维护 | 已移除维护入口 |
| 维护 prompt | 旧 fact/typed-edge prompts 已删除；当前保留 `maintenance.node_summary_compaction` 和 `maintenance.local_triple_merge` | 已清理旧主路径 prompt |
| 退役/冲突 | 当前主路径没有 node conflict 判断、retire/invalidated 维护链 | 未接入 |
| 向量索引维护 | active nodes/edges/clusters 会 upsert；node summary/local graph/edge description 变化会用稳定 point id 覆盖；retired nodes 的 node vectors 有删除入口 | 轻量 upsert 已接入，完整退役/删除链路未完成 |
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
- summary 不再写入独立 `node_summary` 向量；`MemoryNode.summary` 会拼接到 `node_content` 向量文本中。

同一 node 未来再次被抽取出来时：

- 如果 incoming node 与已有 node 同 `node_id`，或 entity alias 命中已有 entity node，则进入 `GraphMaintenance.merge_node(existing, incoming, context)`。
- 当前 merge 行为：
  - labels 取并集。
  - attributes / metadata 做深合并。
  - incoming local_graph triples 先按 normalized SPO 去重；不同 S/P 直接追加。
  - 若 incoming triple 与 existing active triple 的 normalized S/P 相同，则收集为维护任务；同一 node 的全部维护任务会合并到一次 `maintenance.local_triple_merge` prompt 中做批量路由判断。
  - 如果 incoming summary 非空且来自新的 `source_turn_ids`，会追加到 existing summary。
  - `node.metadata.maintenance.node_summary.summary_source_turn_ids` 记录已进入 summary 的来源。
  - `node.metadata.maintenance.node_summary.pending_source_turn_ids` 记录上次压缩以来的来源批次。
  - pending 来源数达到 `maintenance.node_summary.compact_after_k_sources`，或 summary token 数达到 `maintenance.node_summary.max_tokens` 时，强触发 `maintenance.node_summary_compaction`。
  - `content` 同理，只有旧 content 为空时才补写。
  - 更新 `updated_at/updated_turn`。

因此当前 summary 维护是**来源驱动、强触发、无兜底**的：

- 低于阈值时仅拼接不同来源 summary，并随写入重建合并后的 `node_content` 向量。
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
- 当 incoming triple 和 existing active triple 的 normalized `(subject, predicate)` 相同但 object 不同时，收集为该 node 的批量维护任务；同一 node 下所有任务通过一次 `maintenance/local_triple_merge.md` 调用处理。
- LLM 返回一个与输入冲突数组等长、顺序一致的 JSON 数组；单个路由动作：
  - `keep_existing`: 丢弃 incoming。
  - `keep_new`: 退役 affected existing，保存 incoming。
  - `keep_both`: 保存 incoming，旧 triple 保持 active。
  - `merge`: 退役 affected existing，保存 LLM 返回的 merged triple。
  - `needs_review`: 保存 incoming 且标记为 `uncertain`。
- triple qualifiers 中由系统维护 `source_turn_ids` 与 `source_triple_ids`；LLM 路由触发后，系统还会写入 `maintenance_discarded_triple_ids`、`maintenance_replaced_triple_ids`、`maintenance_related_triple_ids` 或 `maintenance_merged_triple_ids` 等追踪字段。
- 退役或 uncertain 状态由系统写入 `LocalTriple.status` 和 maintenance qualifiers；LLM 不输出 `triple_id` 或系统来源字段。
- node metadata 中由系统维护 `maintenance.local_triples.triple_distribution`，记录当前 triples 的总数、status 分布、active predicate 分布和 active subject/predicate 分布；初次写入和每次 node 维护更新后刷新。
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
- `edge_id` 由 namespace 和排序后的 `member_node_ids` 生成；description、`source_turn_ids`、`edge_ref` 不参与 ID。
- `member_signature` 由 node_ids 生成。
- 同一批或跨批次写入中，如果新 edge 与已有 edge 的成员节点集合完全相同，会得到同一个 `edge_id`。
- 同 ID edge 合并时，当前只做轻量维护：
  - description 去重后拼接；
  - `source_turn_ids` 合并；
  - `edge_summary_refs` 合并；
  - `metadata.maintenance.hyper_edge_description` 记录 description 来源和压缩状态；
  - 达到 `maintenance.hyper_edge_description.compact_after_k_sources` 或 `max_tokens` 时调用 description compaction prompt；
  - description 更新后用同一 vector point id 重写 `hyper_edge_description` 向量。
- SQLite `hyper_edges` 表不包含 `edge_type/relation/polarity`。
- SQLite `hyper_edge_members` 表不包含 role。

当前没有，也不是当前主要目标：

- 相似 HyperEdge 召回。
- description-only edge merge prompt 调用。
- member append/version 判断。
- edge retired/invalidated 维护。
- edge access time 更新。

这些能力可作为未来增强放到独立阶段设计。当前阶段优先保持 deterministic same-id reuse，避免把相似度阈值、edge merge 语义判断和版本状态机提前引入主路径。

### 3.6 EdgeCluster 聚合视图

代码入口：

- `c_hypermem/pipeline/edge_cluster_builder.py`
- `c_hypermem/stores/sqlite_store.py`

当前行为：

- EdgeCluster 当前是确定性锚点 HyperEdge 聚合视图。
- 两条 HyperEdge 只要共享至少一个 `member_node_id`，就可以进入同一 EdgeCluster。
- 除共享 member node 外，如果两条 HyperEdge 的成员 node 下 active LocalTriples 出现符合 eligibility 的 normalized subject/object 端点重合，也可以进入 EdgeCluster。端点 eligibility 为：`subject_object` / `object_subject` 命中至少 1 次，或同一 edge pair 上 `subject_subject` 命中至少 2 个文本不同的 normalized subject；单独 `object_object` 不再建立 cluster。
- `BasicEdgeClusterBuilder` 统一使用 `AnchorKey/AnchorOccurrence` 构建 shared-node 与 semantic-anchor clusters；两类 cluster 共享同一套 fingerprint、metadata merge、description variant append 和 `EdgeClusterMember` 去重流程。
- 示例：edge A 的某个成员 node 有 `S1-P1-O1`，edge B 的某个成员 node 有 `S2-P2-O2`；如果 `O1 == S2`，则保持两个 triples 仍为单跳表达，同时建立一个 `semantic_anchor` cluster，把两条 edge 组织到同一检索视图中。
- 如果同一组 edge 同时共享 member node，且还存在 eligible `S-S` 或 `S-O/O-S` 等多个端点锚点，应在 cluster metadata 中保留多个 `cluster_reasons` / `anchor_occurrences`，并对 `EdgeClusterMember(cluster_id, edge_id)` 做确定性去重。
- EdgeCluster 不触发 HyperEdge merge，也不判断支持、更新、冲突关系。
- `description_variants` 只作为检索上下文资产保存，不作为 cluster 相似度合并依据。
- `EdgeClusterMember.relation_to_cluster` 已移除；cluster 成立依据通过 `EdgeCluster.cluster_labels` 与 metadata 表达。shared-node cluster 使用 `cluster_labels=["shared_node"]` 与 `metadata.shared_node_ids`；semantic-anchor cluster 使用 `cluster_labels=["semantic_anchor"]` 与 `metadata.cluster_basis/anchor_value/anchor_positions/anchor_occurrences/cluster_reasons`。

当前没有：

- EdgeCluster 维护配置。
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

当前已完成的轻量索引维护：

- active `MemoryNode.content` 与非空 `MemoryNode.summary` 拼接写入 `node_content`。
- active node 的 active triples 拼入 `node_local_graph`，并用稳定 `node_id` point id 覆盖更新。
- concrete `HyperEdge.description` 写入 `hyper_edge_description`，同 `edge_id` 更新 description 后覆盖同一 point id。
- `EdgeCluster.canonical_description` 和 `description_variants` 写入独立 collection。
- `turn_dialogue` 按 `turn_id` 写入独立 collection。
- `Memory.reset(namespace)` 会删除该 namespace 下所有向量 collection 的点。
- 当前已有 retired node 的 node vectors 删除入口；但主路径很少产生 retired nodes。

完整索引维护尚未实现的部分：

- node `content/summary/local_graph` 变化后的删除差异处理仍主要依赖稳定 point id 覆盖；如果某个字段从非空变为空，旧向量点的细粒度删除策略需要补齐。
- retired / invalidated node 的所有 node vectors 删除入口已有，但主写入维护链路很少产生 retired / invalidated nodes，实际触发覆盖不足。
- retired / invalidated HyperEdge 后删除或过滤 `hyper_edge_description` 点的链路尚未接入。
- edge member set、status、time、metadata 改变后的索引一致性策略尚未完整定义；当前主要覆盖 description upsert。
- retired / merged / invalidated EdgeCluster 后删除或过滤 `edge_cluster_canonical`、`edge_cluster_variant` 点的链路尚未接入。
- EdgeCluster description variants 被截断或移除时，旧 variant point 的删除策略尚未接入。
- turn dialogue 的细粒度删除或重写策略尚未定义；当前主要随 namespace reset 清理。
- SQLite 写入与向量 upsert/delete 之间还没有事务级一致性保障；向量索引仍定位为可重建旁路索引。
- access maintenance 只更新 nodes；HyperEdge / EdgeCluster 的访问时间变化不会触发索引更新。

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
- summary 更新后必须重新写入合并后的 `node_content` 向量。

仍待定义的 node-level merge/update/conflict 维护：

- 是否更新 `content`。
- 是否追加或退役 triples。
- 是否 retire/invalidate existing node。
- 是否 keep separate 或 needs review。

### 4.3 LocalTriple 维护

当前 LocalTriple 已支持同 S/P 候选的轻量批量 LLM 路由。下一阶段可增加：

- triple-level superseded_by / invalidated_by。
- valid_time / qualifier 维护。

但不应通过 predicate 白名单或硬编码多值谓词判断冲突。冲突必须由候选召回 + LLM 语义判断完成。

### 4.4 HyperEdge 维护

当前 concrete HyperEdge 的主目标是轻量同 ID 维护：

- 相同 `sorted(member_node_ids)` 生成同一 `edge_id`。
- 同 ID 时合并 description/source metadata。
- description 更新后重写 `hyper_edge_description` 向量。
- 不因为成员重叠、description 相似或 EdgeCluster 关联直接合并 HyperEdge。

以下 description-only edge maintenance 属于未来计划，不作为当前主要目标：

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

未来接入这些能力前，需要先明确召回阈值、LLM 决策 schema、版本链路、retired/invalidated edge 的索引删除策略，以及失败时是否显式中断写入。

### 4.5 EdgeCluster 边界

当前不设计 EdgeCluster 维护链路：

- 不保留 `maintenance.edge_cluster.*` 配置。
- 不做 EdgeCluster 相似向量召回。
- 不调用 LLM cluster merge prompt。
- 不做 EdgeCluster conflict health check。
- 不引入后台维护调度器。

EdgeCluster 的职责只保留为确定性锚点聚合：

- 共享 `member_node_id` 的 HyperEdges 可以进入同一 EdgeCluster。
- active LocalTriples 的 subject/object 端点规范化后相同，也可以形成 semantic-anchor EdgeCluster。
- semantic anchor 只消费已有 triples，不从原文额外抽取，不重写 `S-P-O` 结构。
- EdgeCluster 聚合不等于 HyperEdge merge。
- 语义冲突、更新和合并应由具体 `MemoryNode` / `HyperEdge` 维护链路处理，而不是由 cluster 级状态机处理。

### 4.6 索引维护

当前阶段先保持轻量索引维护：active object upsert，description/summary/local graph 变化后用稳定 point id 覆盖更新。完整索引维护作为未来计划，需要覆盖：

- node summary/content/local_graph 从非空变为空时删除对应向量点。
- node retired/invalidated 后删除 `node_content`、`node_local_graph` 点。
- edge description 从非空变为空时删除 `hyper_edge_description` 点。
- edge retired/invalidated 后删除或过滤 `hyper_edge_description` 点。
- edge member set、status、time、metadata 变化后的 payload 重写策略。
- cluster canonical / variants 变化后重写 cluster 向量点；这属于索引同步，不是 EdgeCluster 维护决策。
- cluster variant 被截断、移除或替换后删除旧 variant 点。
- cluster retired/merged/invalidated 后删除或过滤 `edge_cluster_canonical`、`edge_cluster_variant` 点。
- turn dialogue 更新、删除或重放时的细粒度索引策略。
- SQLite canonical write 与 vector side-index 更新失败时的恢复 / 重建策略。

当前 `QdrantVectorStore.delete_namespace()` 可以 reset namespace；更细粒度删除已支持 point ids，但 edge/cluster retired path 还没接入。

## 5. 当前实现优先级判断

建议保持的原则：

- 一次抽取，系统组装。
- LLM 不生成系统 ID、来源字段、typed-edge 字段或构建时间。
- 维护 prompt 按候选触发，不无条件串联多轮 LLM。
- 无维护 LLM 时不做规则兜底。
- HyperEdge 使用 deterministic same-id reuse；不因成员重叠、description 相似或 EdgeCluster 关联直接合并。
- EdgeCluster 聚合不等于 HyperEdge merge，且当前不做 EdgeCluster 维护。
- SQLite 是 canonical store，向量索引是可重建旁路索引。
- 原始 turns 与结构化 memory graph 分离。

优先补齐项：

1. 保持并验证当前轻量维护闭环：Node summary、LocalTriple、HyperEdge same-id description merge、EdgeCluster shared-node grouping、active object vector upsert。
2. 明确完整索引维护的删除/过滤策略，尤其是 empty-field、retired/invalidated edge、retired/merged cluster、variant 截断后的旧 point 清理。
3. 为 LocalTriple 维护补充 valid_time、superseded_by / invalidated_by 链接。
4. 定义 MemoryNode merge/update/conflict prompt 和 schema。
5. 将 access maintenance 扩展到 HyperEdge / EdgeCluster，或明确只以 node activation 作为评分依据。
6. 未来再单独设计基于 `hyper_edge_description` 向量召回的 edge maintenance；该项不是当前主要目标。

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
