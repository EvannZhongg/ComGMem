# 复合节点高阶关联 Memory 架构

本文档描述 ComGMem 的记忆结构理念：为什么使用共享节点、节点内局部三元组、description-only HyperEdge 和 EdgeCluster。它不重复展开代码组织、配置字段、检索实现和测试覆盖：

- 当前代码状态见 `docs/current_implementation.md`。
- 工程边界和后续开发路线见 `docs/development_architecture.md`。
- 检索算法细节见 `docs/retrieval_design.md`。

本文只保留结构性概念，以及尚未实现但仍可作为未来方向的设计。

## 1. 当前结构基线

当前已经落地的核心结构可以概括为：

```text
Memory = MemoryNodes + LocalNodeGraphs + HyperEdges + EdgeClusters + Turns
```

其中：

- `MemoryNode` 是长期记忆对象，所有语义类型通过 `node_labels` 表达。
- `LocalNodeGraph` 是节点内部的局部三元组集合。
- `HyperEdge` 是 description-only 的高阶关系实例，用成员节点集合和自然语言 description 表达“这些节点为什么应被一起看”。
- `EdgeCluster` 是相关 HyperEdges 的确定性聚合视图，用于检索时带出 sibling context。
- `Turns` 保存原始交互消息，是来源回溯和真实插入时间的权威记录。

LLM 不直接构建超图，也不生成系统 ID、来源字段、时间字段、权重或 typed-edge 字段。LLM 只输出 `nodes` 和 `edge_summaries`；系统负责后续组装。

## 2. 共享节点池

共享节点池保存可复用的长期记忆对象。节点不绑定唯一组织方式，同一个节点可以被多条 HyperEdge 共享。

`MemoryNode` 采用统一 schema。`entity`、`fact`、`event`、`state`、`preference`、`task`、`instruction` 等不是不同内部结构，而是节点标签。

示意：

```text
node:alice
node:alice_prefers_morning_interviews
node:alice_interview_discussion

edge:e1 -> {node:alice, node:alice_prefers_morning_interviews}
edge:e2 -> {node:alice, node:alice_prefers_morning_interviews, node:alice_interview_discussion}
```

这种结构的目标是避免同一事实或实体在多个视角中被复制。节点表达“可复用的记忆对象”，HyperEdge 表达“这些对象在一次语义关系中如何共同出现”。

### 标签原则

`node_labels` 来自配置并注入抽取 prompt，用来告诉模型当前更偏好哪些记忆类型。标签不是存储 schema 的分支，也不参与 node id 生成。

当前已存在的标签偏好包括：

- `entity`
- `fact`
- `state`
- `preference`
- `task`
- `event`
- `instruction`
- `tool`

`instruction` 当前只是普通标签。未来可以作为高优先级策略记忆，例如检索时优先召回，或在最终 reader/system prompt 中置顶。

`tool` 当前默认不启用。未来真实 agent 场景中，可以把 tool call、tool result、observation 或外部执行证据抽象为普通节点标签，但不应引入独立存储 schema。

## 3. 节点身份与别名

节点 ID 由系统生成。当前身份思路是：

```text
canonical_text
  -> normalized_text
  -> fingerprint
  -> namespace + fingerprint
  -> node_id
```

LLM 可以提供 canonical text、labels、summary、triples 和普通 metadata，但不能生成：

- `node_id`
- `edge_id`
- `triple_id`
- namespace
- storage primary key
- 权重、置信度或来源 ID

带 `entity` label 的节点可以先走 alias 精确复用：

```text
canonical_text / metadata.aliases
  -> normalize
  -> entity_alias_index
  -> reuse existing node_id or create new node
```

当前 alias 复用是轻量精确匹配。未来如果引入 LLM 实体消歧，也应作为候选确认流程，不能替代系统 ID 生成和 namespace 隔离。

## 4. LocalNodeGraph

`LocalNodeGraph` 是节点内部的小型语义结构。当前只包含统一 `LocalTriple` 列表。

示例：

```json
{
  "canonical_text": "Alice prefers morning interviews.",
  "node_labels": ["preference"],
  "local_graph": {
    "triples": [
      {
        "subject": "Alice",
        "predicate": "prefers",
        "object": "morning interviews"
      }
    ]
  }
}
```

LocalNodeGraph 的作用：

- 把节点内容拆成可检索、可维护的局部事实。
- 为 HyperEdge 和 EdgeCluster 提供更细粒度的语义支撑。
- 让同一个节点在不同 HyperEdge scope 下被解释和排序。

系统会给 triple 补充：

- `triple_id`
- `status`
- `scope_edge_ids`
- `source_turn_ids`
- `source_triple_ids`
- maintenance qualifiers

这些字段不由 LLM 输出。

未来可以扩展 triple qualifiers 表达 valid time、condition、source evidence 等信息，但仍应保持 LocalNodeGraph 的统一结构，不为不同 label 建多套子图 schema。

## 5. HyperEdge

`HyperEdge` 表示一条具体高阶关系实例。当前已经采用 description-only 形式：边的成立只依赖 description 和成员节点集合，不需要额外类型化字段。

概念示例：

```json
{
  "edge_id": "edge:...",
  "description": "Alice discussed her interview scheduling preference.",
  "node_ids": [
    "node:alice",
    "node:alice_prefers_morning_interviews"
  ],
  "metadata": {
    "source_turn_ids": ["turn:0"]
  }
}
```

关键点：

- HyperEdge 连接共享节点池中的节点。
- 同一节点可以挂到多条 HyperEdge。
- description 是边的核心语义说明。
- 成员集合决定当前基础 edge fingerprint。
- 来源由系统从 turn metadata 注入。
- LLM 只输出 `edge_summaries[].description` 和 ref，不输出 edge id 或来源字段。

### 保守合并原则

HyperEdge 不应因为成员重叠或 description 相似就直接合并。两个边可能成员相近，但表达支持、修正、更新或冲突关系。

当前实现采用保守策略：

- 相同成员集合会得到稳定 fingerprint。
- 不同成员集合形成不同具体 HyperEdge。
- description 可按来源累计并在阈值后压缩。
- 相关边通过 EdgeCluster 组织，而不是强制合并。

未来如果实现 HyperEdge merge，应先有明确候选召回、冲突感知和 LLM 语义判定；失败时显式失败，不应靠规则兜底。

## 6. EdgeCluster

EdgeCluster 是相关 HyperEdges 的聚合视图。它解决的问题是：多条具体边可能应在检索上下文中一起出现，但不应被合成同一条边。

当前已落地两类 cluster：

- `shared_node`：多条 HyperEdge 共享同一个 member node id。
- `semantic_anchor`：不同 HyperEdge 的 active local triples 在 subject/object endpoint 上出现可用交叉。

semantic anchor 的当前 eligibility：

- `subject_subject` 可以触发。
- `subject_object` 可以触发。
- `object_subject` 可以触发。
- `object_object` 不触发。
- `edge_clusters.stop_nodes` 只屏蔽 `subject_subject`，不屏蔽 `subject_object/object_subject`。

示意：

```text
edge A:
  Alice -has_pet- Toby

edge B:
  Toby -is_a- cat

anchor = Toby
reason = object_subject
cluster = semantic_anchor(Toby)
```

EdgeCluster 不做：

- HyperEdge merge
- 相似度聚类
- LLM cluster merge
- 全局冲突维护
- 后台宏观整理

未来可以接入 `edge_cluster_canonical` 向量召回，但必须保持 periphery 受控扩展：cluster 可以带出 sibling context，不应无限多跳扩散。

## 7. 一次抽取，系统组装

当前写入理念已经落地为“一次抽取，系统组装”：

```text
target interaction
  -> LLM outputs nodes / edge_summaries
  -> system builds MemoryNodes
  -> system builds LocalTriples
  -> system builds HyperEdges from edge_summary_refs
  -> system builds EdgeClusters from deterministic anchors
  -> system writes source_turn_ids and indexes
```

推荐最小输出：

```json
{
  "edge_summaries": [
    {
      "ref": "e1",
      "description": "Alice stated her morning interview preference."
    }
  ],
  "nodes": [
    {
      "ref": "n1",
      "labels": ["entity", "person"],
      "canonical_text": "Alice",
      "summaries": ["Alice is the person whose preference was discussed."],
      "triples": [
        {"subject": "Alice", "predicate": "is_a", "object": "person"}
      ],
      "edge_summary_refs": ["e1"]
    },
    {
      "ref": "n2",
      "labels": ["preference"],
      "canonical_text": "Alice prefers morning interviews.",
      "summaries": ["Alice prefers morning interviews."],
      "triples": [
        {"subject": "Alice", "predicate": "prefers", "object": "morning interviews"}
      ],
      "edge_summary_refs": ["e1"]
    }
  ]
}
```

不要让模型输出 source refs、time 字段、node ids、edge ids 或 graph structure。当前 turn、真实插入时间和来源回溯由系统掌握。

## 8. 时间模型

ComGMem 区分两类时间：

- world time：真实世界时间或有效期。
- lifecycle / activation time：系统写入、更新、访问和 turn 相关时间。

当前系统会为节点和边写入 lifecycle / activation 信息，并通过 `turns.inserted_at` 支持检索上下文中的 `real time=` 显示。

未来可增强：

- 从自然语言中抽取更可靠的 world event time。
- 为 task/state/instruction 设计更明确的 valid time 策略。
- 为 temporal filter 提供查询时间条件解析。
- 把时间解析作为独立候选和 LLM 判定流程，而不是在写入链路中硬编码规则。

`turn_distance` 一类相对值应按检索时当前 turn 动态计算，不应作为长期事实写入。

## 9. 未来可行方向

以下方向仍有价值，但不是当前主链路的一部分。

### MemoryNode Merge / Conflict

未来可以为同一实体、同一偏好、同一任务状态引入更完整的 merge/conflict 维护。但必须满足：

- 有明确候选召回。
- 有 LLM 或其他语义判定边界。
- 没有维护 LLM 时显式失败。
- 不通过硬编码谓词或规则兜底退役事实。

### HyperEdge Merge

未来可以引入 description-only HyperEdge merge，但不能仅凭成员重叠或文本相似。候选判断至少应考虑：

- description 是否表达同一关系。
- source/time 是否兼容。
- member nodes 是否表达相同语义角色。
- 是否存在修正、否定或冲突信号。

### EdgeCluster Recall

当前 cluster 已写入 canonical description 向量，但未接入召回主流程。未来可以把它作为召回通道，但输出仍应回到 concrete HyperEdges 和 nodes，避免把 cluster 自身当成事实。

### Turn Dialogue Recall

当前 turn dialogue 已可写入向量索引。未来召回时应通过命中的 `turn_id` 回 SQLite `turns` 表取权威原文，而不是把向量 payload 当作最终上下文。

### Instruction / Task / State Policy

`instruction`、`task`、`state` 当前是普通节点标签。未来可以设计：

- instruction 优先注入 reader prompt。
- task 按进度和截止时间排序。
- state 按 valid time 和最新性筛选。

这些策略应作为检索或 reader prompt 组织层能力，不应破坏统一 MemoryNode schema。

## 10. 结构摘要

ComGMem 的核心选择是：

```text
共享 MemoryNode
  + 节点内 LocalTriple
  + description-only HyperEdge
  + deterministic EdgeCluster
  + independent turn log
```

外层 HyperEdge 表达“这些记忆为什么一起出现”；内层 LocalNodeGraph 表达“这个节点自身有哪些局部语义”；EdgeCluster 表达“哪些具体边在检索时值得相邻带出”。这三个层次各自独立，避免把抽取、合并、检索和上下文组织混成一个不可控的大结构。
