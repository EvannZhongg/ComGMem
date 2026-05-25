# 同构 MemoryNode 与描述型 HyperEdge 重构计划

本文档记录 C-HyperMem 从当前 `entities/events/assertions -> fact/event/entity builder -> typed edge` 写入方式，重构为 **同构 MemoryNode + description-only HyperEdge** 的工程计划。

本文只描述重构边界、模块影响和实施顺序；当前真实实现状态仍以 `docs/current_implementation.md` 为准。架构目标以 `docs/hypergraph_memory_architecture.md` 为准。

注意：开发过程禁止使用任何规则化抽取策略或是兜底策略，在项目架构更新或重构时无需考虑旧数据的兼容，当前仍为开发环境无任何需要保留或兼容的数据。

## 1. 目标设计

重构后的核心约束：

- 所有长期记忆对象都使用同一个 `MemoryNode` 结构。
- `entity/event/fact/preference/task/instruction/tool` 都是同级 `node_labels`，不对应不同节点 schema。
- LLM 输出 `nodes / edge_summaries`，不再输出 `entities / events / assertions / sources`。
- 每个 node 可以携带自己的 `triples`，不再由代码按 event/fact/entity 固定写死局部图。
- `LocalNodeGraph` 只保留统一 triples；不需要 `attributes` 或 `roles`。
- LLM 不输出 `polarity`。
- LLM 不输出 `edge_type`、`relation`、`roles`。
- HyperEdge 核心只保留 `description + node_ids + metadata`；来源由系统根据当前 user/assistant 交互的 `turn_id` 写入 MemoryNode / HyperEdge metadata，例如 `source_turn_ids`。
- `nodes[]` 和 LLM 输出的 `edge_summaries[]` 都不承载 `source_refs`；真实交互来源由系统在组装 HyperEdge 时绑定，再由 HyperEdge 解释哪些节点在该交互中被共同关联。
- 系统内部可选推断 `metadata.inferred_edge_type` 和 `metadata.inferred_relation`，作为检索、维护或分析策略缓存，而不是超边成立条件。

## 2. 设计参考

实施时优先参考 `docs/hypergraph_memory_architecture.md` 的这些部分：

| 设计主题 | 参考章节 | 对重构的要求 |
| --- | --- | --- |
| 共享节点池 | `## 2. 共享节点池` | `MemoryNode` 统一结构，标签同级，node identity 不依赖 label。 |
| ID 生成 | `## 3. ID 生成原则` | LLM 只输出临时 ref；系统生成 `node_id/edge_id/triple_id`。 |
| 高阶边 | `## 4. 高阶边` | HyperEdge 核心由 description 和成员节点构成；`edge_type/relation` 只允许系统可选推断。 |
| 一次抽取 | `## 5. 一次抽取，系统组装` | 抽取输出改为 `nodes / edge_summaries`；来源由系统绑定当前 `turn_id`。 |
| 复合节点 | `## 6. 复合节点` | 任意 label 的 node 都可以拥有 triples；不再为不同 label 定制 local graph schema。 |
| 时间模型 | `## 7. 双时间指标` | 节点和边仍保留 world/lifecycle/activation 时间；重构不删除时间层。 |
| 结构摘要 | `## 8. 当前结构摘要` | 外层 HyperEdge 表达“为什么一起看”，内层 triples 表达节点自身语义。 |

同时需要对照 `docs/current_implementation.md`：

- `## 3. 当前 Schema`
- `## 4. 写入 Pipeline`
- `## 5. 维护 Prompt`
- `## 6. 存储`
- `## 7. 检索现状`

这些章节记录当前代码真实行为，重构时需要逐步更新，避免文档与代码继续分叉。

## 3. 模块影响面

### 3.1 Schema

涉及文件：

- `c_hypermem/schema.py`

需要调整：

- 新增 `ExtractedNode`，字段建议包括：
  - `ref`
  - `labels`
  - `canonical_text`
  - `summaries`
  - `triples`
  - `edge_summary_refs`
  - `time`
  - `metadata`
- 新增 `ExtractedEdgeSummary`，字段建议包括：
  - `ref`
  - `description`
  - `metadata`
- `MemoryExtraction` 改为：
  - `nodes`
  - `edge_summaries`
  - `metadata`
- `LocalNodeGraph` 去掉或废弃：
  - `attributes`
  - `roles`
- `ExtractedAssertion/ExtractedEntity/ExtractedEvent` 不再作为主抽取 schema。
- `ExtractedAssertion.polarity` 删除。
- `HyperEdge.polarity` 删除。
- `HyperEdge.roles` 不再作为核心字段，新的写入流程不再依赖。

### 3.2 抽取 Prompt 与归一化

涉及文件：

- `c_hypermem/prompts/extraction/memory_extraction.md`
- `c_hypermem/pipeline/extraction.py`

需要调整：

- Prompt 输出 shape 改为 `nodes / edge_summaries`。
- 删除 prompt 中 `entities/events/assertions`、`polarity`、`assertions as single carrier` 的规则。
- `_strict_json_shape()` 改为要求 `nodes/edge_summaries`。
- `normalize_extraction_payload()` 改为归一化：
  - node refs 唯一性
  - edge summary refs 唯一性
  - node 内 triples
  - node.edge_summary_refs 必须引用存在的 edge summary
  - 不接受 LLM 输出 `sources` 或 `source_refs`
- GraphAssembler / NodeBuilder / HyperEdgeBuilder 从 `AssemblyContext.metadata.turn_ids` 读取当前 user/assistant 交互来源，并写入 MemoryNode / HyperEdge metadata 的 `source_turn_ids`。

### 3.3 节点构建与局部图

涉及文件：

- `c_hypermem/pipeline/node_builder.py`
- `c_hypermem/pipeline/local_graph_builder.py`
- `c_hypermem/pipeline/entity_resolution.py`
- `c_hypermem/pipeline/graph_utils.py`

需要调整：

- 用 `build_node(extracted_node, context)` 替代 `build_event_node/build_fact_node/build_or_update_entity_node` 的主路径。
- entity alias resolution 从“只处理 ExtractedEntity”改为“对带 entity label 或可作为实体别名的 node 处理”。
- LocalGraphBuilder 不再按 event participants / fact SPO / entity type 写死 triples。
- LocalGraphBuilder 只负责：
  - 规范化 triples
  - 去重 triples
  - 补 source qualifier
  - 补 scope 信息
- `merge_local_graph()` 只合并 triples，不再合并 local graph attributes/roles。

### 3.4 写入组装

涉及文件：

- `c_hypermem/pipeline/assembly.py`
- `c_hypermem/pipeline/hyperedge_builder.py`
- `c_hypermem/pipeline/edge_cluster_builder.py`
- `c_hypermem/pipeline/ingestion.py`

需要调整：

- GraphAssembler 的主流程改为：

```text
MemoryExtraction.nodes
  -> build all MemoryNodes
  -> build ref_to_node_id
  -> collect edge_summary_refs from nodes
  -> build HyperEdges from edge summaries, member nodes, and context turn_ids
  -> build EdgeClusters
  -> build alias/property/maintenance indexes
```

- `BasicHyperEdgeBuilder` 新增 description-only 构建路径：

```text
ExtractedEdgeSummary + member nodes -> HyperEdge(description, node_ids)
```

- 删除默认写死的：
  - `event -> facts evidence edge`
  - `subject entity + fact state edge`
- `correction` 仍可保留为系统维护生成的特殊边，但它不来自 LLM `edge_type`。
- EdgeClusterBuilder 不再依赖 `edge.edge_type == state/correction`。应改为基于：
  - edge description
  - member node labels
  - source scope
  - optional inferred metadata

### 3.5 维护逻辑

涉及文件：

- `c_hypermem/pipeline/maintenance.py`
- `c_hypermem/prompts/maintenance/fact_merge.md`
- `c_hypermem/prompts/maintenance/contradiction_check.md`
- `c_hypermem/prompts/maintenance/edge_merge.md`
- `c_hypermem/prompts/maintenance/edge_conflict_check.md`
- `c_hypermem/prompts/maintenance/edge_cluster_merge.md`

需要调整：

- `fact_merge` 泛化为 `memory_node_merge`。
- `contradiction_check` 泛化为 `memory_conflict_check`。
- 冲突候选不再只来自 `subject_node_id + predicate` 的 fact property key。
- label policy 需要声明哪些 label 可被更新/冲突检查，例如：
  - `preference`
  - `state`
  - `task`
  - `instruction`
  - `fact`
- correction edge 由系统维护生成，description 可以类似：

```text
"New memory updates or invalidates previous memory about Alice's interview preference."
```

- `edge_merge.md` 不再要求 relation/roles/polarity；改为 description、members、source/time、inferred metadata 只作为辅助信号。

### 3.6 存储与迁移

涉及文件：

- `c_hypermem/stores/sqlite_store.py`
- `c_hypermem/stores/base.py`

需要调整：

- `nodes.local_graph_json` 新写入不产生 `attributes/roles`。
- `triples.role_in_edge` 可以废弃或保留为空；新的 scope 信息优先进入 qualifiers：

```json
{"scope_edge_id": "...", "scope_cluster_id": "...", "edge_description": "..."}
```

- `hyper_edges.polarity` 废弃或保留为空/unknown。
- `hyper_edge_members.role` 废弃或保留为空。
- `fact_property_index` 需要重新评估：
  - 新设计可改为更通用的 `node_property_index` 或 `triple_property_index`。
- 需要数据库迁移策略：
  - 开发期可 reset namespace。

### 3.7 向量索引

涉及文件：

- `c_hypermem/stores/vector_store.py`
- `c_hypermem/memory.py`

需要调整：

- `node_local_graph_embedding_text()` 继续可用，但文案应从 `Related facts` 改为 `Related triples` 或 `Local graph`。
- payload 中 `attributes` 可保留为 node-level system attributes，但不再读取 local graph attributes。
- payload 中 `role_in_edge` 可废弃或保留为空。
- Edge summary / HyperEdge description 可以考虑增加独立向量索引：
  - 当前已有 EdgeCluster canonical/variant 向量。
  - 重构后可新增 `hyper_edge_description` collection，或先继续只索引 EdgeCluster。
- `turn_dialogue` payload 里的 `roles` 是 user/assistant 对话角色，不是语义角色；建议改名为 `dialogue_roles`，避免和旧 HyperEdge roles 混淆。

### 3.8 检索

涉及文件：

- `c_hypermem/retrieval/recall.py`
- `c_hypermem/retrieval/graph_ripple.py`
- `c_hypermem/retrieval/expansion.py`
- `c_hypermem/retrieval/ranking.py`
- `c_hypermem/retrieval/context.py`

需要调整：

- SearchResult 仍返回 edge-centered 结果，但 edge metadata 不再依赖 `edge_type/relation/roles`。
- 图扩展策略从 role-based 改为：
  - node label
  - edge description
  - member count
  - source/time proximity
  - optional inferred metadata
- `edge_coherence` 仍可保留：同一 HyperEdge 内多个 seed hit 加分。
- `metadata.edge_roles` 删除。
- result content 以 edge description + member node summaries/triples 组织。

### 3.9 配置

涉及文件：

- `configs/node_labels.yaml`
- `configs/default.yaml`
- `c_hypermem/config.py`

需要调整：

- 删除或废弃 `allow_roles`。
- 删除或废弃 `require_relation_role_polarity_compatibility_for_merge`。
- `node_labels.yaml` 从“label 描述 + 部分索引策略”升级为 node label policy：
  - alias strategy
  - index strategy
  - time preference
  - merge/conflict strategy
  - retrieval priority
- 增加可选 inferred edge metadata 配置：

```yaml
hyperedges:
  infer_edge_metadata: false
  inferred_fields:
    - inferred_edge_type
    - inferred_relation
```

### 3.10 测试与示例

涉及文件：

- `tests/test_memory.py`
- `examples/quickstart.py`
- `examples/longmemeval_*.py`

需要调整：

- 新增 schema 测试：
  - `nodes/edge_summaries` payload 可解析。
  - LLM 不输出 `sources/source_refs/edge_type/relation/polarity/roles`。
  - node.edge_summary_refs 可反向构建 HyperEdge members。
  - MemoryNode / HyperEdge metadata 使用系统注入的 `source_turn_ids` 回溯 user/assistant turns。
- 重写旧测试中对以下字段的断言：
  - `polarity`
  - `roles`
  - `role_in_edge`
  - `fact_property_index`
  - `evidence/state edge`
- 增加同构节点测试：
  - preference node 独立入库。
  - task node 独立入库。
  - instruction node 独立入库。
  - event/entity/fact/preference 使用相同 node schema。
- 增加 description-only edge 检索测试。

## 4. 重构顺序

不再使用 M1/M2。建议按下面顺序推进，每一步结束都应更新 `docs/current_implementation.md`。

| 顺序 | 阶段 | 状态 | 目标 | 主要文件 |
| --- | --- | --- | --- | --- |
| 0 | 设计文档对齐 | 已开始 | 明确同构节点、description-only edge、可选 inferred metadata。 | `docs/hypergraph_memory_architecture.md`, 本文档 |
| 1 | Schema 引入新结构 | 未开始 | 增加 `ExtractedNode/ExtractedEdgeSummary`，删除旧抽取 schema 主路径。 | `schema.py` |
| 2 | 抽取 prompt 与 parser 重构 | 未开始 | LLM 输出 `nodes/edge_summaries`，禁止输出来源字段。 | `memory_extraction.md`, `extraction.py` |
| 3 | NodeBuilder 与 LocalGraphBuilder 重构 | 未开始 | 统一 `build_node`，删除按 label 写死 triples 的主路径。 | `node_builder.py`, `local_graph_builder.py` |
| 4 | GraphAssembler 重构 | 未开始 | 按 refs 组装 nodes 和 HyperEdges，并从 context.turn_ids 写入系统来源 metadata。 | `assembly.py`, `node_builder.py`, `hyperedge_builder.py` |
| 5 | 存储与索引调整 | 未开始 | 新写入不依赖 polarity/roles；来源回溯依赖系统注入的 `source_turn_ids`。 | `sqlite_store.py`, `vector_store.py` |
| 6 | 维护逻辑泛化 | 未开始 | fact merge/conflict 泛化为 memory node merge/conflict。 | `maintenance.py`, maintenance prompts |
| 7 | 检索适配 | 未开始 | 检索不依赖 edge_type/relation/roles，消费 description-only edge。 | `retrieval/*.py` |
| 8 | 配置收敛 | 未开始 | node label policy 与 inferred metadata 配置落地。 | `config.py`, `configs/*.yaml` |
| 9 | 测试和示例迁移 | 未开始 | 删除旧断言，补齐新结构测试。 | `tests/test_memory.py`, `examples/*.py` |
| 10 | 旧路径清理 | 未开始 | 移除旧 `entities/events/assertions` 主路径和旧 prompt。 | pipeline/schema/docs |

## 5. 推荐的落地方式

由于当前仍为开发环境且不需要兼容旧数据，建议直接切换主路径：

1. 在 schema 层新增新结构，并移除旧 `entities/events/assertions/sources` 主抽取入口。
2. 抽取 prompt 切换到新结构。
3. parser 只接受 `nodes/edge_summaries`，并拒绝 LLM 输出 `sources/source_refs`。
4. assembly 内部只消费新结构，并从 `AssemblyContext.metadata.turn_ids` 注入 MemoryNode / HyperEdge 来源。
5. 测试覆盖同构节点、description-only edge、系统来源绑定和 turn 回溯。

旧结构迁移只作为人工理解参考，不作为代码兼容入口：

```text
旧 entities[] -> 新 nodes[] with labels ["entity", ...]
旧 events[] -> 新 nodes[] with labels ["event", ...]
旧 assertions[] -> 新 nodes[] with labels ["fact", ...] 或原 assertion labels
旧 evidence/state edge -> 新 edge_summaries[] description
```

该映射只用于人工理解旧概念如何落到新结构，不应实现为代码兼容入口。

## 6. 当前进度

- 已完成：`docs/hypergraph_memory_architecture.md` 已调整为同构节点、`nodes/edge_summaries`、description-only HyperEdge、系统绑定 `source_turn_ids`、可选 inferred metadata 的设计方向。
- 已完成：本文档新增，列出重构顺序和影响模块。
- 未开始：代码 schema 重构。
- 未开始：抽取 prompt 和 parser 重构。
- 未开始：写入、维护、存储、检索适配。

下一步建议从 **阶段 1：Schema 引入新结构** 开始。Schema 不稳定时先不要改 assembly，否则会反复返工。
