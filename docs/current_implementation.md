# C-HyperMem 当前实现状态

本文档记录当前代码实现进展，便于和 `hypergraph_memory_architecture.md`、`development_architecture.md` 对照。它只描述现状，不替代长期设计文档。

## 1. 对外入口

- `c_hypermem.Memory` 是当前唯一推荐入口。
- 已支持 `from_config/reset/add/add_memory/search/stats/close`。
- `add_memory(...)` 会规范化为 `AgentInteraction`；`add(...)` 会规范化为 `MemoryImportBatch`。
- 如果配置中存在 `llm` 且未显式传入 extractor，`Memory` 会创建默认 `LLMMemoryExtractor`。

## 2. 配置与环境

- 默认配置入口为 `configs/default.yaml`。
- 模型配置拆在 `configs/models.yaml`，节点标签配置拆在 `configs/node_labels.yaml`。
- 仅读取 C-HyperMem 项目根目录下的 `.env`，不读取上级目录 `.env`。
- 当前默认节点标签已包含：`turn/event/fact/entity/state/preference/task/instruction/tool`。
- `default_policy` 是系统内部 fallback 策略；传入 prompt 时不会以 `default_policy` 名称暴露给 LLM。

## 3. Schema

当前核心 schema 位于 `c_hypermem/schema.py`：

- `MemoryNode`：统一节点结构，使用 `node_labels` 表达语义类型。
- `HyperEdge`：具体高阶关系实例，成员通过 `hyper_edge_members` 表保存。
- `EdgeCluster`：相关 HyperEdge 的聚合工作集，不强制合并边。
- `LocalNodeGraph`：所有节点共享的局部图结构，包含 triples、attributes、roles。
- `MemoryExtraction`：LLM 一次抽取输出，只包含 `entities/events/assertions/sources`。

系统 ID 由 `utils/ids.py` 生成，LLM 不生成 `node_id/edge_id/triple_id`。

## 4. 写入 Pipeline

当前写入链路：

```text
Memory.add_memory/add
  -> IngestionPipeline
  -> LLMMemoryExtractor 或显式 extractor
  -> GraphAssembler
  -> SQLiteStore
```

已实现内容：

- 一次抽取：`LLMMemoryExtractor` 渲染 `prompts/extraction/memory_extraction.md`。
- 抽取输出归一化为 `MemoryExtraction`。
- `GraphAssembler` 负责系统组装：
  - 轻量 entity alias resolution。
  - 构建或复用 `entity` 节点。
  - 构建 `event` 节点。
  - 将 assertions 构建为 `fact` 节点和 LocalNodeGraph triples。
  - 对 preference 谓词追加 `preference` 标签。
  - 构建基础 `evidence/state/correction` HyperEdge。
  - 创建基础 EdgeCluster 与 cluster members。
  - 写入 entity alias index 和 fact property index。
- 简单冲突事实处理：
  - 同一 subject node + predicate 下新旧 object 不同时，旧 fact 标记为 `retired`。
  - 创建 `correction` HyperEdge。
  - 同步退役旧 fact property index 行。

## 5. 存储

当前存储实现为 `SQLiteStore`：

- `nodes`
- `triples`
- `hyper_edges`
- `hyper_edge_members`
- `edge_clusters`
- `edge_cluster_members`
- `entity_alias_index`
- `fact_property_index`
- `ingestion_cache`

其中 `ingestion_cache` 表已预留，但增量缓存逻辑尚未正式启用。

## 6. 检索现状

检索代码暂时保持轻量实现：

- `Retriever` 使用 `LexicalScorer` 做 BM25-like 召回。
- 支持基于 HyperEdge 的简单扩展。
- 可根据 `preference/task/entity/time` 等信号做少量结构化加分。

检索侧尚未按设计文档完整重构，后续需要补充向量召回、EdgeCluster 扩展、query analysis LLM、冲突感知排序等。

## 7. 当前占位或待完善

- `LocalGraphBuilder` 仍是占位，局部图主要由 `GraphAssembler` 直接生成。
- `GraphMaintenance` 仍是占位，只做透传。
- `HyperEdgeBuilder`、`EdgeClusterBuilder` 仍保留为可插拔协议。
- 增量构建缓存只建了表结构，尚未启用。
- tool / instruction / turn / state / task 的专门构建策略尚未完全展开，目前主要依赖 LLM 输出标签和通用节点结构承载。
- 检索部分暂未处理为完整架构版本。

## 8. 验证

当前测试覆盖：

- 默认配置和 split config 加载。
- `.env` 模型变量解析。
- 统一节点 schema 和 SQLite 表结构。
- 显式 extractor 到系统组装链路。
- 默认节点标签集合。
- `default_policy` 不作为 prompt label 渲染。
- 冲突 fact 退役与 correction edge。

常用验证命令：

```powershell
python -m compileall -q c_hypermem
python -m pytest -q
```
