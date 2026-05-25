# C-HyperMem Memory 维护实现差异

本文档对照 `docs/hypergraph_memory_architecture.md`、`docs/development_architecture.md`、`docs/current_implementation.md` 与当前代码，单独记录 memory 维护相关设计和实现差异。

这里的“memory 维护”主要指：

- 写入时对已有节点、事实、超边、簇和索引的复用、更新、退役与冲突处理。
- 检索访问后对 memory activation time 的更新。
- 与维护相关的 prompt、配置、持久化和向量索引清理。

不展开完整检索 pipeline，检索设计以 `retrieval_design.md` 和 `current_implementation.md` 为准。

## 1. 总体结论

当前实现已经形成了可运行的轻量维护闭环：

```text
新增交互
  -> 一次 LLM 抽取
  -> entity alias 精确复用
  -> 同 node_id 的节点合并
  -> assertion 构造候选 fact
  -> 同一 subject_node_id + predicate 下存在 active 旧 fact 时触发 fact_merge
  -> merge/update 复用旧 fact，keep_separate 写入新 fact
  -> needs_contradiction_check 时继续触发 contradiction_check
  -> 仅在 LLM 判定 contradiction 时标记旧 fact/triples 作废
  -> 创建 correction edge
  -> 更新 SQLite / FTS / 向量索引
```

但它仍低于长期设计中的完整维护系统：

- `fact_merge.md` 已接入主写入流程；`edge_merge.md`、`edge_conflict_check.md`、`edge_cluster_merge.md` 目前仍只存在于 prompt/config/registry 中，没有接入主写入流程。
- HyperEdge 只做确定性构建和 ID 去重，没有候选召回、LLM 合并、成员追加或版本化维护。
- EdgeCluster 只按确定性 topic fingerprint 复用，并追加 description variants；没有后台 cluster merge，也没有基于 LLM 的簇冲突健康检查。
- LocalNodeGraph 维护仍很浅，主要来自 event participants 和 assertion SPO；旧 fact 退役时，节点与其内部 triple 都会标记为 retired/invalidated，但尚未表达复杂 triple-level time qualifiers。
- 时间维护已覆盖 created/inserted/updated/access_count 等基础字段，但设计中的 recency decay、access boost、边访问时间、复杂 valid_time 维护尚未接入。

## 2. 文档预期与当前实现对照

| 维护点 | 文档预期 | 当前实现 | 差异判断 |
| --- | --- | --- | --- |
| 一次抽取，系统组装 | LLM 只输出 `entities/events/assertions/sources`，系统维护节点、边、簇和 ID | `LLMMemoryExtractor` 一次抽取，`GraphAssembler` 组装；LLM 不生成系统 ID | 已对齐 |
| Entity alias resolution | 实体别名对齐先于 ID 生成，可匹配 canonical/display/aliases/entity_type | `EntityResolver` 使用 normalized aliases + 可选 `entity_type` 精确查 `entity_alias_index` | 部分实现，仅精确匹配，无复杂消歧 |
| 节点复用和合并 | 同一 canonical fingerprint 复用节点，标签可累积，local graph 可合并 | `merge_node()` 合并 labels/attributes/metadata/local_graph，更新 `updated_at/updated_turn` | 已实现轻量版本 |
| fact merge/update | `fact_merge.md` 判断 merge/update/keep separate/needs contradiction | 同一 `property_key` 下存在 active 旧 fact 时先调用 `fact_merge.md`；支持多个旧 fact block，LLM 通过 `existing:0/1/...` refs 返回受影响候选 | 已接入轻量主流程 |
| fact contradiction | 同一属性候选冲突时调用 `contradiction_check.md`，旧 fact 可退役/失效 | 仅当 `fact_merge` 返回 `needs_contradiction_check` 时调用；contradiction + retired/invalidated 才退役 | 已接入核心路径 |
| 无维护 LLM 时的冲突处理 | 文档强调不要规则兜底 | overlap fact 需要维护 LLM；无 LLM 直接抛 `RuntimeError` | 与当前约束一致 |
| HyperEdge merge | 成员重叠只作召回信号，需 relation/role/polarity/time 兼容后保守复用或追加 | `BasicHyperEdgeBuilder` 只创建 evidence/state/correction；按 deterministic fingerprint 去重 | 未接入候选召回和 LLM merge |
| EdgeCluster 维护 | 相关边聚合到 cluster，可有 conflict_state、description variants、后台 merge | 按 `cluster_hint` / description fingerprint 复用 cluster，追加 variants；correction 新簇置 `contains_conflict` | 部分实现，无后台整理和 LLM conflict check |
| LocalNodeGraph 维护 | 所有节点统一 local graph，支持 triples/attributes/roles/qualifiers/time scope | event participants、entity type、fact SPO；fact triple 可挂 `scope_edge_id/role_in_edge/edge_relation` | 部分实现，结构浅 |
| 退役事实的索引维护 | retired fact 不应被 active recall 命中 | retired node 不进 FTS；node-local-graph/node_content/node_summary 向量点会删除；fact_property_index 行置 retired/invalidated | 基本对齐 |
| 退役事实的 triple 状态 | 旧 fact 和其局部 triple 应能表达 superseded/invalidated | 退役旧 fact 时同步设置 `LocalTriple.status/superseded_by/invalidated_by` | 已补齐基础状态同步 |
| 时间维护 | world/lifecycle/activation 分层，写入、更新、访问分别维护 | `make_time_bundle()`、`touch_node_update()`、`touch_node_access()` 已覆盖节点基础时间；检索后只更新命中 nodes | 部分实现，edge access/decay 未用 |
| 后台维护 | `edge_cluster_merge` 可在 memory 增长后触发宏观整理 | `background_maintenance.enabled=false`，没有调度器和触发代码 | 未接入 |

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
5. 调用 `IngestionPipeline` 和 `GraphAssembler` 生成 nodes / retired_nodes / edges / clusters / indexes。
6. 先 upsert nodes，再删除 retired nodes 的向量点，再 upsert edges/clusters/indexes；merge/update 后的旧 node 和 keep_separate 后的新 node 都会进入本轮 nodes 输出并重新索引。

实现差异：

- 文档中的“增量 target + recent context”已经实现。
- 应用层 `ingestion_cache/prefix_hash/cursor` 已删除，和当前文档一致。
- `add(messages)` 会逐条消息模拟增量 target，而不是把整段历史一次性抽取成一个 target。

### 3.2 实体维护

代码入口：

- `c_hypermem/pipeline/entity_resolution.py`
- `c_hypermem/pipeline/node_builder.py`
- `c_hypermem/pipeline/graph_utils.py`
- `c_hypermem/stores/sqlite_store.py`

当前行为：

- 从显式 entities、event participants、assertion subjects 收集实体候选。
- 使用 entity name + aliases 形成 alias 集合。
- `find_entity_alias(namespace, normalized_aliases, entity_type)` 精确查库。
- 命中则复用旧 `MemoryNode`，并追加标签、aliases、attributes、metadata。
- 未命中则生成新的 canonical fingerprint 和 `node_id`。
- `entity_alias_index` 会持久化 normalized alias -> shared node_id。

和文档差异：

- 已满足“别名对齐先于 ID 生成”的核心方向。
- 目前没有 LLM 实体消歧、模糊匹配、display_name 特殊匹配或跨上下文 disambiguation。
- `entity_type` 只作为可选精确过滤条件，不是完整实体类型系统。

### 3.3 事实维护

代码入口：

- `c_hypermem/pipeline/assembly.py`
- `c_hypermem/pipeline/maintenance.py`
- `c_hypermem/prompts/maintenance/contradiction_check.md`
- `c_hypermem/stores/sqlite_store.py`

当前行为：

1. 每条 assertion 先构造候选 fact node。
2. `property_key = subject_node_id + compact(predicate)`。
3. 查找同一 property_key 下 active 旧 fact。
4. 若没有旧 fact，候选 fact 正常写入 nodes、fact property index、state/evidence edge 和向量索引。
5. 若存在旧 fact，先调用 `maintenance.fact_merge`。该 prompt 使用显式占位符：

```text
{{NEW_FACT}}
{{NEW_FACT_SOURCE}}
{{EXISTING_FACTS}}
{{STRICT_JSON_SHAPE}}
```

其中 `{{EXISTING_FACTS}}` 支持多个旧事实，系统按如下自然语言 block 渲染：

```text
existing:0
Fact: Alice loves tea.
Source:
User said: "I really love tea."

existing:1
Fact: Alice loves green tea.
Source:
User said: "Green tea is my favorite."
```

LLM 不接收 `turn_ids/node_id/fact_node_id/property_key` 等内部字段，只通过 `affected_existing_refs` 返回 `existing:0/1/...`。系统内部负责把 ref 映射回旧 fact node。

6. `fact_merge` 的决策会分流：

| decision | 当前处理 |
| --- | --- |
| `merge` | 复用旧 fact，不创建新 fact；合并 labels/attributes/metadata/local_graph，更新 `updated_at/updated_turn`，旧 node 重新 upsert 与重新索引。 |
| `update` | 复用旧 fact node_id，用 `merged_fact` 或新 fact 文本更新 content/summary/SPO/local graph，旧 node 重新 upsert 与重新索引。 |
| `keep_separate` | 不操作旧 fact，候选新 fact 正常入库并写入 node_content/node_summary/node-local-graph 向量索引。 |
| `needs_contradiction_check` | 只把 `affected_existing_refs` 对应旧 fact 传入 `contradiction_check`。 |

7. `contradiction_check` 仅当 `fact_merge` 路由到 `needs_contradiction_check` 时触发。仅当返回：

```json
{
  "conflict_state": "contradiction",
  "recommended_old_status": "retired|invalidated"
}
```

才会：

- 将旧 fact node 标为 retired 或 invalidated。
- 将旧 fact 的 local triples 同步标为 retired 或 invalidated，并写入 `superseded_by/invalidated_by`。
- 设置 `superseded_by` / `invalidated_by` 指向新 fact。
- 设置 `status_reason` / `status_updated_at`。
- 必要时更新旧 fact 的 `valid_time.end`。
- 创建 `correction` HyperEdge。
- 写入旧 fact 的 retired/invalidated property index 行。
- 删除旧 fact 的 node-local-graph、node_content、node_summary 向量点。

和文档差异：

- `fact_merge.md` 已接入，但触发条件仍故意收窄到同一 `property_key` 下 active 旧 fact；不会跨 predicate 或通过向量相似度找候选。
- `merge/update` 当前复用旧 fact node，不新增 state/evidence edge；新来源主要通过 metadata/local_graph 合并体现。
- `keep_separate` 会正常创建新 fact 和 state/evidence edge，并触发新 fact 向量索引。
- 若同一 property_key 下有旧 fact 且没有维护 LLM，当前选择抛错，而不是静默跳过或规则判断。

### 3.4 HyperEdge 维护

代码入口：

- `c_hypermem/pipeline/hyperedge_builder.py`
- `c_hypermem/pipeline/graph_utils.py`
- `c_hypermem/stores/sqlite_store.py`

当前只内置三类边：

- `evidence`: event node 支持一组 extracted fact nodes。
- `state`: subject entity + fact + optional event。
- `correction`: new fact invalidates old fact。

当前行为：

- `edge_id` 由 description + edge_type + relation + roles + source_scope 的 fingerprint 生成。
- `member_signature` 由 node_ids + roles 生成。
- `dedupe_edges()` 只按 `edge_id` 做批内去重。
- SQLite upsert edge 时会先删除该 edge 的旧 members，再按当前 node_ids/roles 重写 member 表。

和文档差异：

- 没有实现“召回相似既有 HyperEdge -> LLM 判断 reuse/append/version/new_edge”。
- `edge_merge.md` 未被调用。
- `member_version` 当前保持默认值，成员变化没有显式版本递增逻辑。
- 成员重叠不会触发任何合并或冲突检查，符合“保守不合并”的底线，但低于设计中的维护能力。

### 3.5 EdgeCluster 维护

代码入口：

- `c_hypermem/pipeline/edge_cluster_builder.py`
- `c_hypermem/stores/sqlite_store.py`

当前行为：

- 对每条新 edge 生成 cluster label 和 cluster description。
- state edge 优先使用 `cluster_hint.kind/subject_node_id/predicate` 生成 cluster fingerprint。
- 若同批或库中已有同 fingerprint cluster，则复用并追加 description variant。
- correction edge 创建/进入的 cluster relation 为 `updates`，其他多为 `supports`。
- 新建 correction cluster 时 `conflict_state="contains_conflict"`；普通 cluster 为 `none`。

和文档差异：

- `edge_conflict_check.md` 未被调用，因此新边进入 cluster 后不会由 LLM 更新 cluster health。
- `edge_cluster_merge.md` 未被调用，后台宏观整理没有调度器。
- cluster 之间没有 cross-link 或 merge 关系表。
- 复用策略是确定性 fingerprint，不做语义相似 cluster 召回。

### 3.6 LocalNodeGraph 维护

代码入口：

- `c_hypermem/pipeline/local_graph_builder.py`
- `c_hypermem/pipeline/graph_utils.py`
- `c_hypermem/stores/sqlite_store.py`

当前行为：

- event node: participants -> `participated_as` triples 和 roles。
- entity node: entity_type -> local_graph attributes。
- fact node: assertion SPO -> 单条 LocalTriple，polarity -> local_graph attributes。
- fact triple 被挂入 edge 时会补 `scope_edge_id`、`role_in_edge`、`edge_relation`。
- 同 node merge 时，local graph 按 normalized SPO 去重合并。
- `fact_merge=update` 时会用新 assertion 的 SPO 替换旧 fact 的 local graph triple。
- `contradiction_check` 退役旧 fact 时，会同步把旧 fact 的 local triples 标记为 retired/invalidated。

和文档差异：

- 尚未表达工具调用、任务状态、事件内部复杂关系、triple-level time qualifiers。
- LocalTriple 的状态同步已覆盖基础 fact 退役，但还没有维护 triple-level valid time 或复杂 qualifier。
- LocalGraphBuilder 的 `build(nodes)` 当前是 no-op，真正构建发生在 NodeBuilder 调用具体 build_* 方法时。

### 3.7 索引与访问维护

代码入口：

- `c_hypermem/memory.py`
- `c_hypermem/stores/sqlite_store.py`
- `c_hypermem/stores/vector_store.py`
- `c_hypermem/utils/time.py`

当前行为：

- SQLite canonical store 是权威数据源。
- active node 会进入 `nodes_fts`；retired/invalidated node upsert 时会先删除旧 FTS 行且不再插入。
- `fact_merge=merge/update` 的旧 fact node 会重新 upsert 并重新写入 node_content、node_summary 和 node-local-graph 向量。
- `fact_merge=keep_separate` 的新 fact 会作为 active node 正常写入 node_content、node_summary 和 node-local-graph 向量。
- retired nodes 的以下向量点会删除：
  - `triple` collection 中的 node-local-graph 点。
  - `node_content` 点。
  - `node_summary` 点。
- `edge_cluster_canonical`、`edge_cluster_variant`、`turn_dialogue` 会写入向量 collection，但当前检索主流程尚未使用 cluster/turn dialogue 向量召回。
- `Memory.search()` 返回后，会对结果中 `metadata.edge_nodes` 的 node 调用 `touch_node_access()`，更新 `last_access_turn/access_count`。

和文档差异：

- 访问维护只更新 nodes，不更新 HyperEdge / EdgeCluster 的 access_count。
- `time.relative_decay` 和 `access_boost` 已配置，但当前检索评分没有接入。
- 退役节点的 EdgeCluster variant 向量不会因旧 fact 退役而删除；如果 variant 文本引用旧 fact，后续需要单独维护策略。

## 4. 当前实现优先级判断

以下当前实现选择与文档方向一致，建议保持：

- 维护 prompt 按候选触发，不在每次写入时无条件串联多轮 LLM。
- 无维护 LLM 时不做规则兜底，避免静默误退役事实。
- HyperEdge 保守新建，不因成员重叠直接合并。
- EdgeCluster 聚合不等于 HyperEdge merge，具体边仍保留独立语义。
- SQLite 是 canonical store，向量索引是可重建旁路索引。
- 原始 turns 与结构化 graph nodes 分离。

以下差异是后续应优先补齐的维护缺口：

1. 为 `fact_merge` 增加更细的多候选处理策略，例如当多个 old facts 同时被 `merge/update` 命中时是否允许合成一个 canonical fact。
2. 明确 `merge/update` 是否需要补充 evidence edge 或 source edge；当前只更新旧 fact node 和索引，不新增事实边。
3. 为 HyperEdge 实现候选召回和 `edge_merge.md` 调用，但仍保持成员重叠只作召回信号。
4. 接入 `edge_conflict_check.md`，在 edge 挂入 cluster 后更新 `conflict_state` 和 `relation_to_cluster`。
5. 为 `background_maintenance` 增加真实触发器，再接入 `edge_cluster_merge.md` 做低频 cluster 整理。
6. 将 access maintenance 扩展到 HyperEdge / EdgeCluster，或明确只以 node activation 作为当前评分依据。
7. 明确 retired fact 是否需要清理或标记相关 EdgeCluster variants，避免未来 cluster vector recall 命中旧事实描述。

## 5. 代码位置索引

- 对外入口：`c_hypermem/memory.py`
- 写入编排：`c_hypermem/pipeline/ingestion.py`
- 图组装：`c_hypermem/pipeline/assembly.py`
- 语义维护：`c_hypermem/pipeline/maintenance.py`
- 实体解析：`c_hypermem/pipeline/entity_resolution.py`
- 节点构建：`c_hypermem/pipeline/node_builder.py`
- 局部图构建：`c_hypermem/pipeline/local_graph_builder.py`
- 超边构建：`c_hypermem/pipeline/hyperedge_builder.py`
- 簇构建：`c_hypermem/pipeline/edge_cluster_builder.py`
- 持久化：`c_hypermem/stores/sqlite_store.py`
- 向量索引：`c_hypermem/stores/vector_store.py`
- 时间维护：`c_hypermem/utils/time.py`
- 维护 prompts：`c_hypermem/prompts/maintenance/`
- 相关测试：`tests/test_memory.py`
