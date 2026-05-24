# C-HyperMem 当前实现状态

本文档记录当前代码实现进展，并说明它与 `development_architecture.md` 的差异。长期目标仍以设计文档为准；本文只描述当前代码实际做到的部分。

## 1. 对外入口

- `c_hypermem.Memory` 是当前唯一推荐入口。
- 已支持 `from_config/reset/add/add_memory/search/stats/close`。
- `add_memory(...)` 会规范化为 `AgentInteraction`；`add(...)` 会规范化为 `MemoryImportBatch`。
- 如果配置中存在 `llm` 且未显式传入 extractor，`Memory` 会创建默认 `LLMMemoryExtractor`。

## 2. 配置与环境

- 默认配置入口为 `configs/default.yaml`。
- 模型配置拆在 `configs/models.yaml`，节点标签配置拆在 `configs/node_labels.yaml`。
- 仅读取 C-HyperMem 项目根目录下的 `.env`，当前可以直接使用`.env`调用模型进行测试。
- `.env` 已加入 `.gitignore`，仓库提供 `.env.example`。
- `embedding.batch_size` 已加入配置，默认值为 `10`。
- 当前默认节点标签包括：`turn/event/fact/entity/state/preference/task/instruction/tool`。
- `default_policy` 是系统内部 fallback 策略；传入 prompt 时不会以 `default_policy` 名称暴露给 LLM。

## 3. 当前 Schema

核心 schema 位于 `c_hypermem/schema.py`：

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
- `node_labels.yaml` 的启用标签描述会注入抽取 prompt 的 `{{NODE_LABELS}}`。
- 抽取输出归一化为 `MemoryExtraction`。
- `assertions` 是当前构建事实节点的主输入：每条 assertion 会转为 `fact` 节点、LocalNodeGraph triple、property index 和基础超边成员。
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

## 5. 维护 Prompt

`c_hypermem/prompts/maintenance` 已按当前维护编排拆分：

- `fact_merge.md`：property_key 重合后判断 merge/update/keep separate/需要冲突检查。
- `contradiction_check.md`：处理基础 SPO 冲突。
- `edge_merge.md`：构建新边时判断复用边、追加成员、新版本或新建边。
- `edge_conflict_check.md`：边挂载进 Cluster 后更新簇健康/冲突状态。
- `edge_cluster_merge.md`：后台宏观 Cluster 整理。

`configs/default.yaml` 已配置这些 prompt 路径，并预留：

```yaml
edge_clusters:
  background_maintenance:
    enabled: false
    trigger_every_k_writes: 100
```

注意：这些维护 prompt 当前主要是接口和策略预留，除基础冲突退役外，完整 LLM 维护调用链尚未接入。

## 6. 存储

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

## 7. 检索现状

检索代码暂时保持轻量实现：

- `Retriever` 使用 `LexicalScorer` 做 BM25-like 召回。
- 支持基于 HyperEdge 的简单扩展。
- 可根据 `preference/task/entity/time` 等信号做少量结构化加分。

检索侧尚未按设计文档完整重构，后续需要补充向量召回、EdgeCluster 扩展、query analysis LLM、冲突感知排序等。

## 8. 与开发架构文档的实现差异

当前实现与 `development_architecture.md` 的主要差异如下：

- 设计文档中的 `node_builder.py` 尚未独立拆出；当前由 `GraphAssembler` 同时完成实体解析、节点构建、局部图构建和基础边构建。
- `LocalGraphBuilder` 仍是占位；局部 triple 主要在 `GraphAssembler` 中生成。
- `GraphMaintenance` 仍是占位；只有基础 SPO 冲突退役逻辑已经直接写在 `GraphAssembler` 中。
- 设计中的增量构建缓存只实现了表结构，尚未实现 cache cursor、prefix hash、append-only/rebuild 判断。
- 维护 prompt 已写好，但 fact merge、edge merge、edge conflict、cluster merge 的 LLM 调用还未接入主流程。
- `turn/state/task/instruction/tool` 已作为标签配置存在，但尚未都有专门构建策略；当前主要依靠 LLM 输出 labels 和统一节点结构承载。
- 向量索引配置和 embedding client 已有，但检索主流程仍以 lexical recall 为主，尚未启用完整向量召回链路。
- EdgeCluster 当前由边描述轻量生成，尚未实现复杂的相似 cluster 召回、LLM cluster merge 和宏观整理。

## 9. 暂用轻量化方案的原因

部分设计仍需要评测反馈或更明确的策略边界，因此当前采用轻量化实现：

- **实体消歧**：先用 alias / normalized text 精确匹配，避免过早引入 LLM entity resolution 的不可控合并。
- **事实冲突**：先用 property_key + object 差异做保守退役；复杂兼容关系后续交给 `fact_merge.md` 和 `contradiction_check.md`。
- **边合并**：当前不主动复用高度重叠边，优先保守新建；`edge_merge.md` 已准备好用于后续接入。
- **Cluster 整理**：先创建基础 cluster，后台宏观整理只预留配置 `trigger_every_k_writes`。
- **局部图谱**：先从 assertions 生成最基本 SPO triple，避免让 LLM 直接构建复杂 graph。
- **检索**：先保留 lexical + edge expansion，等写入结构稳定后再处理向量召回和冲突感知排序。

这些轻量方案的原则是：先保证统一 schema、系统生成 ID、一次抽取和基础写入闭环稳定，再逐步补维护和检索增强。

## 10. 验证

当前测试覆盖：

- 默认配置和 split config 加载。
- `.env` 模型变量解析。
- embedding `batch_size` 配置和分批调用。
- 统一节点 schema 和 SQLite 表结构。
- 显式 extractor 到系统组装链路。
- 默认节点标签集合。
- `default_policy` 不作为 prompt label 渲染。
- 抽取 prompt 注入 `node_labels.yaml`。
- 维护 prompt registry 加载。
- 冲突 fact 退役与 correction edge。

常用验证命令：

```powershell
python -m compileall -q c_hypermem
python -m pytest -q
```
