# C-HyperMem Retrieval Design

本文档记录当前开发阶段的检索流程。它替换旧的检索设计草案，只描述已经决定进入当前实现的路径。

当前约束：

- `retrieval.query_analysis: false` 是唯一当前开发目标。
- 不使用 `c_hypermem/prompts/retrieval/query_analysis.md`。
- 不加载 spaCy。
- 不做规则化 query 抽取。
- 不做兜底召回策略。
- 当前仍是开发环境，不考虑旧数据迁移或兼容。

## 配置

```yaml
retrieval:
  query_analysis: false
  lexical_top_k: 30
  node_content_vector_top_k: 20
  node_local_graph_vector_top_k: 20
  node_summary_vector_top_k: 10
  final_top_k: 10
```

字段含义：

- `query_analysis`: 当前固定为 `false`，表示检索不做 LLM 或 spaCy query analysis。
- `lexical_top_k`: SQLite FTS 召回的 MemoryNode 数量。
- `node_content_vector_top_k`: `node_content` vector collection 召回数量。
- `node_local_graph_vector_top_k`: `node_local_graph` vector collection 召回数量。代码中对应现有 `triple` vector store。
- `node_summary_vector_top_k`: `node_summary` vector collection 召回数量。
- `final_top_k`: RRF 融合后的最终 MemoryNode 数量，当前为 10。

## 当前流程

```text
Memory.search(query, namespace)
  -> Retriever.search(...)
     -> QueryAnalyzer.analyze(query)
        - query_analysis=false 时返回原始 query metadata
     -> DenseVectorRecall.embed_query(query)
     -> DenseVectorRecall.recall(...)
        - node_content top 20
        - node_local_graph top 20
        - node_summary top 10
     -> SQLiteFTSRecall.recall(...)
        - SQLite FTS top 30
     -> reciprocal_rank_fusion(...)
     -> final top 10 MemoryNode
     -> SearchResult
```

## 向量召回

用户 query 会先被向量化，然后分别查询三个向量索引：

- `node_content`
- `node_summary`
- `node_local_graph`

现有代码中 `node_local_graph` 复用历史命名的 `triple` vector store，但 payload 的 `item_type` 为 `node_local_graph`。

每个向量命中必须通过 payload 中的 `node_id` 回到 SQLite canonical store 读取 `MemoryNode`。向量索引只作为可重建旁路索引，不作为权威数据源。

## SQLite FTS 召回

用户 query 同时送入 SQLite FTS：

- FTS 表：`nodes_fts`
- 索引字段：`content`、`summary`、`local_graph`
- namespace 与 node_id 作为非全文索引字段保存，用于过滤和回表。

词法召回通过 `SQLiteFTSRecall` 封装。后续如果开发者要替换成 BM25 或其他词法算法，应替换这个模块，而不是把算法写进 `Retriever`。

## 融合策略

当前使用 Reciprocal Rank Fusion。

常数 `k` 写死为 60，并封装在 `retrieval/fusion.py` 中，后续可替换该模块。

```text
score(node) =
  1 / (60 + rank_lexical)
  + 1 / (60 + rank_vector)
```

其中：

- `rank_lexical` 来自 SQLite FTS 结果排序。
- `rank_vector` 来自三路向量召回合并后的排序。
- 如果某个节点只出现在一路召回中，只计算该路的 RRF 分数。

三路向量召回内部先按每个节点的最佳向量分数形成一个 vector 排名，再与 lexical 排名做 RRF。

## SearchResult Metadata

当前结果 metadata 包含：

- `channels`: `lexical`、`vector`
- `score_parts`: `rrf_lexical`、`rrf_vector`
- `matched_vector_items`: 向量命中的 channel、score、point id、text、payload
- `node_id`
- `node_labels`
- `source_turn_ids`
- `triples`
- `time`
- `node_metadata`
- `query_analysis`

## 当前不做

当前检索流程不接入：

- HyperEdge expansion
- EdgeCluster expansion
- entity alias recall
- turn dialogue recall
- recency decay
- access boost
- temporal filter
- LLM rerank
- spaCy query analysis
- LLM query analysis

这些能力如果后续需要加入，应等到对应开发阶段再设计和实现。
