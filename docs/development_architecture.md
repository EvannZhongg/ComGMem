# ComGMem 开发架构

本文档描述 ComGMem 当前代码应遵守的工程架构和后续开发边界。实现细节以 `comgmem/` 和 `docs/current_implementation.md` 为准；本文件用于指导后续扩展，不保留旧数据兼容路径。

开发约束：

- 不在开发过程中加入规则化抽取策略或兜底抽取策略。
- 抽取模型只输出语义候选；系统负责 ID、来源、时间、scope、存储、索引和检索组装。
- 当前仍是开发环境，不需要兼容旧 schema、旧表或旧数据。
- `retrieval.query_analysis` 是检索侧独立能力，不参与写入抽取重构。

## 1. 包边界

ComGMem 是独立 memory 包，不依赖 `agent_memory_eval` 或其他评测框架内部模块。

依赖方向：

```text
application / examples / optional adapters
  -> comgmem public API
  -> comgmem internal modules
```

禁止方向：

```text
comgmem internal modules
  -> agent_memory_eval
```

公开 API 收敛到：

```python
from comgmem import Memory

memory = Memory.from_config("configs/default.yaml")
memory.reset(namespace="sample")
memory.add_memory(user_input="...", assistant_output="...", namespace="sample")
results = memory.search("...", namespace="sample", top_k=10)
stats = memory.stats(namespace="sample")
memory.close()
```

如果需要接入评测框架，只能通过 thin adapter 调用 `comgmem.Memory`，并在 adapter 边界完成数据格式转换。ComGMem 核心 schema 不应出现 `MemorySession`、`MemoryItem`、LongMemEval、LOCOMO、suite、runner 等评测框架概念。

## 2. 当前目录结构

当前主结构：

```text
ComGMem/
  comgmem/
    __init__.py
    memory.py
    config.py
    schema.py
    errors.py

    pipeline/
      ingestion.py
      extraction.py
      assembly.py
      node_builder.py
      local_graph_builder.py
      hyperedge_builder.py
      edge_cluster_builder.py
      maintenance.py
      graph_utils.py
      context.py

    retrieval/
      query_analysis.py
      recall.py
      lexical_recall.py
      vector_recall.py
      fusion.py
      graph_ripple.py
      ranking.py

    stores/
      base.py
      sqlite_store.py
      vector_store.py
      lexical_store.py

    llms/
      base.py
      openai_compatible.py

    embeddings/
      base.py
      model_client.py

    prompts/
      extraction/memory_extraction.md
      retrieval/query_analysis.md
      maintenance/node_summary_compaction.md
      maintenance/local_triple_merge.md
      maintenance/hyper_edge_description_compaction.md

    adapters/
    utils/

  configs/
    default.yaml
    models.yaml
    node_labels.yaml

  examples/
    quickstart.py

  tests/
  docs/
```

后续新增模块时优先保持这些边界：

- `memory.py` 只做对外入口和高层协调。
- `pipeline/assembly.py` 只做写入编排，不内联构建、维护或检索算法。
- `pipeline/maintenance.py` 放置写入期维护策略。
- `retrieval/` 放置召回、融合、图扩展和结果格式化。
- `stores/` 只负责持久化和索引访问，不做业务语义判断。
- `prompts/` 每个 prompt 使用独立 markdown 文件，Python 中不硬编码长 prompt。

## 3. 数据模型

核心结构：

```text
Memory = MemoryNodes + LocalNodeGraphs + HyperEdges + EdgeClusters + Turns
```

### MemoryNode

`MemoryNode` 是统一长期记忆对象。语义类型通过可累积 `node_labels` 表达，例如 `entity/fact/state/preference/task/event/instruction/tool`。

节点包含：

- `node_id`
- `canonical_text`
- `normalized_text`
- `fingerprint`
- `node_labels`
- `content`
- `summary`
- `attributes`
- `metadata`
- `time`
- `local_graph`

带 `entity` label 的节点可以通过 canonical text 和 `metadata.aliases` 写入 `entity_alias_index`，用于精确复用。

### LocalNodeGraph

`LocalNodeGraph` 当前只包含统一 `LocalTriple` 列表。所有 label 都走同一结构，不按 entity/event/fact 拆分内部图 schema。

`LocalTriple` 包含：

- `subject`
- `predicate`
- `object`
- `status`
- `scope_edge_ids`
- `scope_cluster_id`
- `qualifiers`

系统在 qualifiers 中维护 `source_turn_ids`、`source_triple_ids` 和 `maintenance_*` 字段。LLM 不生成这些系统字段。

### HyperEdge

`HyperEdge` 是 description-only 高阶关系实例。核心字段为：

- `edge_id`
- `edge_fingerprint`
- `description`
- `node_ids`
- `weights`
- `member_signature`
- `metadata`
- `time`

HyperEdge 不要求 typed edge 字段。后续如果需要分析型分类，只能作为普通 metadata 另行设计，不能成为抽取主路径。

### EdgeCluster

`EdgeCluster` 是相关 HyperEdges 的确定性聚合视图，用于检索时带出 sibling context。它不负责合并 HyperEdges，也不承担全局冲突维护。

当前 cluster 类型：

- `shared_node`
- `semantic_anchor`

### Turns

`turns` 表保存原始 user/assistant/tool/observation 消息，用于最近上下文、审计和真实插入时间回溯。它不是图谱节点表。

## 4. 抽取 Prompt

默认写入抽取 prompt：

```text
comgmem/prompts/extraction/memory_extraction.md
```

抽取输入：

```text
ExtractionWindow(context, target)
```

- `context`：最近历史，只用于代词、时间和省略信息消解。
- `target`：当前新增交互，模型只能从 target 中抽取新增记忆。

抽取输出：

```json
{
  "nodes": [
    {
      "ref": "n1",
      "labels": ["preference"],
      "canonical_text": "Alice prefers morning interviews.",
      "summaries": ["Alice prefers morning interviews."],
      "triples": [
        {"subject": "Alice", "predicate": "prefers", "object": "morning interviews"}
      ],
      "edge_summary_refs": ["e1"],
      "metadata": {}
    }
  ],
  "edge_summaries": [
    {
      "ref": "e1",
      "description": "Alice discussed her interview scheduling preference.",
      "metadata": {}
    }
  ]
}
```

LLM 不输出：

- 系统 ID，例如 `node_id/edge_id/triple_id`
- 来源字段，例如 `source_ref/source_refs`
- 构建时间字段
- 权重、置信度、salience
- typed-edge 字段
- 外层图结构

`node_labels.yaml` 中启用的标签描述会注入 prompt。标签配置是偏好说明，不是严格白名单；模型输出未配置标签时，系统仍按统一 `MemoryNode` 入库。

## 5. 写入 Pipeline

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

关键边界：

- `Memory` 负责 turn id 生成、原始消息入库、向量索引写入和对外 API。
- `IngestionPipeline` 负责把 interaction/batch 变成 extraction window，再交给 assembler。
- `LLMMemoryExtractor` 只做 prompt 渲染、模型调用和 schema normalization。
- `GraphAssembler` 只做编排。
- `NodeBuilder` 构建节点初始结构。
- `GraphMaintenance` 维护 node summary、local triples 和 HyperEdge description。
- `BasicHyperEdgeBuilder` 由 edge summary 和成员 nodes 构建 HyperEdge。
- `BasicEdgeClusterBuilder` 构建 shared-node 和 semantic-anchor clusters。
- `SQLiteStore` 是 canonical store。
- Qdrant 向量索引是可重建旁路索引。

写入时，当前 turn id 会进入 interaction metadata，再由系统写入节点、边和 triple qualifiers 的来源字段。

## 6. 维护策略

维护 prompt 只在明确候选或阈值条件下触发。不允许为避免失败而引入规则兜底。

### Node Summary

配置：

```yaml
maintenance:
  node_summary:
    enabled: true
    compact_after_k_sources: 10
    max_tokens: 2048
    prompt: maintenance/node_summary_compaction.md
```

行为：

- 低于阈值时累计不同来源的 summary。
- 达到来源数或 token 阈值时调用 LLM 压缩。
- 没有维护 LLM 时显式失败。
- 压缩结果重写 node summary、FTS 和 `node_content` 向量。

### LocalTriple

配置：

```yaml
maintenance:
  local_triples:
    enabled: true
    prompt: maintenance/local_triple_merge.md
```

行为：

- incoming triples 先做 normalized SPO 批内去重。
- normalized SPO 完全相同则合并系统来源，不调用 LLM。
- normalized subject/predicate 相同但 object 不同，批量调用 LLM 路由。
- 决策动作仅允许 `keep_existing/keep_new/keep_both/merge/needs_review`。
- `needs_review` 会保留 incoming，但标记为 `uncertain`。
- active triples 才进入 `node_local_graph` 向量文本。

### HyperEdge Description

配置：

```yaml
maintenance:
  hyper_edge_description:
    enabled: true
    compact_after_k_sources: 10
    max_tokens: 2048
    prompt: maintenance/hyper_edge_description_compaction.md
```

行为：

- description 按来源累计。
- 达到阈值后调用 LLM 压缩。
- 压缩结果重写 HyperEdge description 和 `hyper_edge_description` 向量。

## 7. EdgeCluster 设计

EdgeCluster 是检索上下文组织视图，不是合并机制。

### Shared Node

多个 HyperEdges 共享同一个 member node id 时，会进入同一个 `shared_node` cluster。

### Semantic Anchor

系统会从 active local triples 的 subject/object endpoint 中提取 normalized anchor。不同 HyperEdges 在相同 anchor 上满足以下条件时，可以进入 `semantic_anchor` cluster：

- `subject_subject`
- `subject_object`
- `object_subject`

不触发：

- `object_object`

`edge_clusters.stop_nodes` 只屏蔽 `subject_subject`。如果 stop node 作为 object 与另一个 triple 的 subject 发生 `object_subject` 或 `subject_object` 交叉，当前仍会触发 cluster。

## 8. 存储与索引

SQLite 表：

```text
nodes
nodes_fts
triples
hyper_edges
hyper_edge_members
edge_clusters
edge_cluster_members
entity_alias_index
turns
```

向量 item type：

```text
node_content
node_local_graph
hyper_edge_description
edge_cluster_canonical
turn_dialogue
```

当前检索主流程使用：

- SQLite FTS
- `node_content`
- `node_local_graph`
- `hyper_edge_description`

当前已写入但未接入召回主流程：

- `edge_cluster_canonical`
- `turn_dialogue`

索引原则：

- SQLite 是权威数据源。
- Qdrant 是可重建 side index。
- 每类语义向量使用独立 collection。
- `Memory.reset(namespace)` 必须同步清理 SQLite 和向量点。

## 9. 检索架构

入口：

```python
results = memory.search(query, namespace="...", top_k=10)
```

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

Track 1：

- lexical / node_content / node_local_graph 先形成 node ranking。
- seed nodes 通过 incident HyperEdges 转成 edge candidates。
- 同一 edge 命中多个 seed nodes 时应用 edge coherence multiplier。

Track 2：

- `hyper_edge_description` 向量直接召回 HyperEdges。

融合：

- Track 1 和 Track 2 在 edge-level RRF 汇合。
- Top K2 core edges 之后才触发 cluster periphery。
- periphery 只补充上下文，不参与 core edge 排名，也不继续多跳扩散。

SearchResult：

- 每条结果以一个核心 HyperEdge 为中心。
- `content` 输出 `memoryN：description` 和相关 active triples。
- `current_turn_id`、`source_turn id`、`real time` 由 recall 配置控制显示。
- metadata 保留 query analysis、score parts、edge nodes、periphery edges/nodes、relative time 等结构化信息。

## 10. 配置结构

默认配置由三部分组成：

```text
configs/default.yaml
configs/models.yaml
configs/node_labels.yaml
```

核心配置块：

```yaml
storage:
  backend: sqlite
  path: runs/comgmem/memory.sqlite3

ingestion:
  pass_recent_context: false
  context_window_messages: 3

extraction:
  prompt: extraction/memory_extraction.md
  pass_node_labels_to_prompt: true

edge_clusters:
  enabled: true

maintenance:
  node_summary: ...
  local_triples: ...
  hyper_edge_description: ...

index:
  lexical: sqlite_fts
  vector: qdrant
  use_embedding: true

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
  final_top_k: 10

recall:
  cluster_periphery_edge_limit: 15
  cluster_periphery_node_limit: 50
  node_triple_limit: 30
  include_turn_ids_in_context: true
  include_real_time_in_context: false
```

`models.yaml` 提供 LLM、embedding、token counting 和 NLP 配置。`node_labels.yaml` 提供 turn 配置、EdgeCluster stop nodes 和默认 label policy。

## 11. 可选 Adapter

如果接入 `agent_memory_eval`，adapter 应位于评测仓库或 `comgmem/adapters/` 可选模块中。职责只包括：

- 读取配置。
- 创建 `comgmem.Memory`。
- 把评测 session 转为 `Memory.add(...)` 或 `Memory.add_memory(...)` 输入。
- 把 `Memory.search(...)` 输出转为评测框架需要的 item。
- 返回 debug stats。

adapter 不应实现 extraction、graph assembly、maintenance、ranking、storage 或 prompt 逻辑。

## 12. 后续开发路线

当前已具备：

- 独立包结构和公开 `Memory` API。
- `nodes/edge_summaries` 抽取链路。
- 同构 `MemoryNode`、description-only `HyperEdge`、deterministic `EdgeCluster`。
- SQLite canonical store。
- Qdrant rebuildable side indexes。
- Node summary、LocalTriple、HyperEdge description 三类维护。
- Dual-track edge-level RRF 检索。
- quickstart 本地模型链路自检。

优先后续项：

- 补齐 memory node merge / conflict / contradiction 的明确候选召回和 LLM 维护流程。
- 接入 `edge_cluster_canonical` 召回，但保持 periphery 受控扩展。
- 接入 `turn_dialogue` 召回，并通过 `turn_id` 回 SQLite 取权威原文。
- 设计 entity alias recall 的召回位置和去重策略。
- 增加 temporal filter，但不要把时间解析和 rerank 与基础召回耦合。
- 设计 task/instruction/state 的专用检索优先级。
- 完成标准评测 adapter 和 ablation。

## 13. 验证

常用命令：

```powershell
python -m compileall -q comgmem
python -m pytest -q
python examples\quickstart.py
```

`quickstart.py` 会真实调用 LLM、embedding、SQLite、Qdrant 和检索链路。成功时会输出：

```text
[quickstart] all checks passed; model, embedding, storage, indexing, and retrieval configs look OK.
```
