# ComGMem Retrieval Design

本文档记录 ComGMem 检索算法的设计概念和 pipeline 边界。具体代码模块、真实配置、测试覆盖和当前实现进度放在 `current_implementation.md` 中维护；本文只保留检索设计应遵守的结构。

当前约束：

- `retrieval.query_analysis: false` 是唯一当前开发目标。
- 当前 false 链路不使用 `comgmem/prompts/retrieval/query_analysis.md`。
- 当前 false 链路不加载 spaCy。
- `nlp` / `llm` query analysis 只保留为未来扩展入口；当前不参与默认开发链路。
- 不做规则化 query 抽取，尤其不从 query 中用规则抽取 entity candidates。
- 不做兜底召回策略。
- 当前仍是开发环境，不考虑旧数据迁移或兼容。

## 参数边界

检索参数应围绕以下边界组织，具体字段名和默认值以 `current_implementation.md` 与配置文件为准：

- query analysis 模式。
- lexical recall 候选数。
- node content / node local graph 向量召回候选数。`node_content` 向量文本由 `MemoryNode.content` 与 `MemoryNode.summary` 拼接生成，不再拆分独立 `node_summary` 向量通道。
- HyperEdge description 向量召回候选数。
- node-level RRF 常数 `node_rrf_k`，用于 Track 1 的 FTS / node_content / node_local_graph 融合。
- node RRF 后进入 Track 1 图谱种子阶段的 seed 数，记为 `K1`，当前可对应 `graph_seed_top_k`；该值不应超过 lexical / node_content / node_local_graph 三路召回候选数之和。
- edge-level RRF 常数 `edge_rrf_k`。
- edge-level RRF 后保留的核心 HyperEdge 数，记为 `K2`，当前配置为 `edge_core_top_k`。
- controlled cluster ripple 每条 core edge 可附加的 sibling edge 数量上限，当前配置为 `recall.cluster_periphery_edge_limit`。
- controlled cluster ripple 每条 core edge 可附加的 periphery node 数量上限，当前配置为 `recall.cluster_periphery_node_limit`。
- 每个 node 输出到召回上下文的 active triples 数量上限，当前配置为 `recall.node_triple_limit`。
- 召回上下文是否在 edge 行和 triple 行标注 `turn_ids`，当前配置为 `recall.include_turn_ids_in_context`。
- HyperEdge coherence 的 `alpha` / `beta` 权重。
- 最终返回的 HyperEdge 数量。

## 检索 Pipeline

```text
Memory.search(query, namespace)
  -> query analysis
  -> Track 1: node-centric edge ranking
     -> lexical node recall
     -> node_content vector recall
     -> node_local_graph vector recall
     -> node RRF across lexical / node_content / node_local_graph
     -> take Top K1 MemoryNodes as Seed Set S
     -> collect incident HyperEdges from S as E_cand
     -> score each E_cand with node base score + edge coherence
     -> produce Track 1 Edge Ranking
  -> Track 2: edge-centric direct ranking
     -> hyper_edge_description vector recall
     -> read active HyperEdges by edge_id
     -> produce Track 2 Edge Ranking
  -> Edge-Level RRF
     -> fuse Track 1 and Track 2 edge rankings by edge_id
     -> prune to Top K2 core HyperEdges
  -> controlled cluster ripple
     -> only clusters attached to Top K2 core HyperEdges may expand
     -> attach sibling edge descriptions and related active nodes as periphery
  -> final top-k HyperEdges, each carrying member nodes
```

最终返回上下文应以 HyperEdge 为组织单位：每条 core edge 至少包含 edge description、edge 下的 active nodes、每个 node 的 active triples，以及相对当前回答 turn 的时间距离。由 controlled cluster ripple 带出的 sibling edges 也应按 sibling edge 分组携带 description、nodes、node triples 与相对 turn 距离；这些 sibling context 只补充背景，不参与 core edge 排名。

## 向量召回

用户 query 会先被向量化，然后分别查询两个 node 向量索引和一个 HyperEdge 向量索引：

- `node_content`
- `node_local_graph`
- `hyper_edge_description`

node 向量命中必须通过 payload 中的 `node_id` 回到 SQLite canonical store 读取 `MemoryNode`。HyperEdge description 向量命中必须通过 payload 中的 `edge_id` 回到 SQLite canonical store 读取 active `HyperEdge`。向量索引只作为可重建旁路索引，不作为权威数据源。

HyperEdge description 不再投影为 node-level RRF 分数。它作为 Track 2 直接产生 edge ranking，随后与 Track 1 的 node-derived edge ranking 在 edge-level RRF 汇合。

`node_content` 索引文本为 node content 与 node summary 的拼接；当 summary 拼接或压缩维护导致 `MemoryNode.summary` 更新时，写入闭环必须用同一个 node content vector point id 覆盖更新。

## Lexical 召回

用户 query 同时进入词法召回通道。第一阶段可使用 SQLite FTS；后续也可以替换为 BM25 或其他开发者自定义算法。

词法召回应作为独立算法模块，不应把 FTS / BM25 细节写死在检索编排器里。

## Track 1: Node-Centric Edge Ranking

Track 1 只消费三路 node 召回：

- SQLite FTS lexical recall。
- `node_content` vector recall。
- `node_local_graph` vector recall。

先对这三路 MemoryNode 排名做 node-level RRF：

```text
S_node(n) =
  1 / (node_rrf_k + rank_lexical)
  + 1 / (node_rrf_k + rank_node_content_vector)
  + 1 / (node_rrf_k + rank_node_local_graph_vector)
```

其中：

- `rank_lexical` 来自 SQLite FTS 结果排序。
- `rank_node_content_vector` 来自 node content + summary 向量召回排序。
- `rank_node_local_graph_vector` 来自 node local graph 向量召回排序。
- 如果某个节点只出现在一路召回中，只计算该路的 RRF 分数。

每个 node 向量通道内部先按每个节点的最佳向量分数形成该通道排名，再与 lexical 排名做 RRF。

然后取 Top `K1` 个 MemoryNode 作为 Seed Set `S`，通过 `get_incident_edges(namespace, seed_node_ids)` 收集候选边集合 `E_cand`。

Track 1 的 edge score 是边级分数，不回写到 MemoryNode 分数上：

```text
Score_track1(E) =
  max(S_node(v) for v in E ∩ S)
  * (1 + alpha * max(0, N_hit(E) - 1) ^ beta)
```

其中：

- `E`: 候选 HyperEdge。
- `S`: Top `K1` seed nodes。
- `N_hit(E) = |E ∩ S|`。
- `max(S_node(v) for v in E ∩ S)`: 该边被命中的最强节点证据。
- `alpha` / `beta`: HyperEdge coherence 参数。

当 `N_hit <= 1` 时，coherence multiplier 为 `1`，不产生多节点相干性增强；当 `N_hit >= 2` 时，多 seed 命中会指数式放大该边的 Track 1 排名信号。

## Track 2: Edge-Centric Direct Ranking

Track 2 只消费 `hyper_edge_description` 向量召回：

```text
query -> hyper_edge_description vector index -> active HyperEdges
```

该通道直接产出 HyperEdge ranking。排序来自向量检索结果顺序；原始向量相似度只用于该通道内部排序和解释，不直接与 Track 1 的 edge score 相加。

Track 2 的职责是从“语境描述”直接命中相关 HyperEdge，避免 HED 证据经过成员节点投影后被稀释。

## Edge-Level RRF & Prune

Track 1 和 Track 2 都产出 HyperEdge ranking，因此最终融合发生在 edge-level：

```text
S_final_edge(E) =
  1 / (edge_rrf_k + rank_track1(E))
  + 1 / (edge_rrf_k + rank_track2(E))
```

如果某条边只出现在一个 Track 中，另一个 Track 的贡献为 `0`。

融合后按 `S_final_edge(E)` 降序排序，并严格截断为 Top `K2` 核心 HyperEdges。`K2` 是控制图谱扩散规模的关键参数，由 `edge_core_top_k` 显式配置。

如果某条边缺失某个 Track 命中，该 Track 的 RRF 贡献显式为 `0`，等价于该 Track rank 为正无穷。若一条边同时出现在 Track 1 和 Track 2，则两路 RRF 贡献相加。

Top K2 剪枝前的排序必须是确定性的。同分时按以下顺序打破平局：

1. `S_final_edge(E)`。
2. Track 2 HyperEdge description 向量相似度。
3. Track 1 edge score。
4. `edge_id` 字典序，作为完全同分时的稳定排序键。

## Controlled Cluster Ripple

只有 Top `K2` 核心 HyperEdges 才允许触发 EdgeCluster 扩散。

扩散步骤：

1. 读取 Top `K2` core edges 的 active member nodes，作为核心主线上下文。
2. 查询这些 core edges 所属的 EdgeCluster。
3. 读取这些 cluster 内的 sibling HyperEdges。
4. 将 sibling edge descriptions 和相关 active nodes 作为背景补充 periphery。

约束：

- 只有属于 Top `K2` core edges 的 cluster 才有资格扩散。
- sibling edges 不反向开启新的 cluster 扩散，避免横向爆炸。
- 每条 core edge 最多附加 `recall.cluster_periphery_edge_limit` 条 sibling edges；为空表示不限制，`0` 表示不附加。候选 sibling edges 会按自身 `source_turn_ids` / activation turn 的最新 turn 优先排序后再截断。
- 每条 core edge 最多附加 `recall.cluster_periphery_node_limit` 个 periphery nodes；为空表示不限制，`0` 表示不附加。候选 nodes 会在已保留 sibling edges 中按 node / triple 最新 turn 优先排序后再截断。
- 每个返回 node 最多输出 `recall.node_triple_limit` 条 active triples；为空表示不限制，`0` 表示不输出 triples。triple 排序优先保留当前 edge scope 命中的 triples，其次保留当前 edge source turn 命中的 triples，再按 triple `source_turn_ids` 最新优先。
- 当 `recall.include_turn_ids_in_context=true` 时，最终 `content` 的 edge 行与 triple 行会标注 `turn_ids`，帮助 reader 把 edge 与 triple 来源对齐；设为 `false` 时只移除上下文文本中的 turn id 标注，不移除 metadata 中的来源字段。
- periphery 只作为补充上下文，不参与 core edge 的 edge-level RRF 排名。
- 涟漪扩散只依赖已有图结构，不分析 query，不做规则化抽取，不做兜底策略。

## Edge Coherence

Edge coherence 只属于 Track 1 的 edge ranking 阶段。它衡量一条 HyperEdge 是否同时被多个 node seed 命中。

```text
coherence_multiplier(E) =
  1 + alpha * max(0, N_hit(E) - 1) ^ beta
```

其中：

- `E`: 被命中的 HyperEdge。
- `N_hit(E)`: Top `K1` seed set 中属于该边的唯一 `node_id` 数量。
- `alpha`: `retrieval.edge_coherence_alpha`。
- `beta`: `retrieval.edge_coherence_beta`。

实现约束：

- `N_hit <= 1` 时 multiplier 为 `1`。
- `N_hit >= 2` 时 multiplier 放大 Track 1 edge score。
- 同一个 `node_id` 即使被多个 query token、别名或召回通道命中，也只能对 `N_hit` 贡献 1。
- HED Track 不参与 `N_hit`，避免一条 HED 命中因为 edge 内有多个成员而制造虚假的多 seed coherence。
- coherence 不再写回所有成员 node 的分数；它是 edge-level ranking 解释字段。

## Final Edge Result

`final_top_k` 控制最终返回的 edge 数量，而不是 node 数量。

每个 `SearchResult` 表示一条 HyperEdge：

- `id`: `edge_id`
- `content`: edge description + edge 内 node 内容
- `score`: edge-level score
- `metadata.edge_nodes`: 该 edge 内包含的 MemoryNode 列表
- `metadata.edge_metadata`: 系统写入的 edge metadata，例如 `source_turn_ids`

Edge-level score 来自 edge-level RRF 后的最终分数：

```text
S_edge = S_final_edge(E)
```

这样 `final_top_k` 选择的是最相关的故事线/关系边，再把这些边内的节点整体返回。

## 结果边界

最终返回单位是 HyperEdge，而不是单个 MemoryNode。每条结果应包含：

- edge identity、description、system metadata。
- edge-level score 与可解释 score parts。
- core edge 内成员 nodes。
- cluster ripple 带出的 periphery sibling edge descriptions 和相关 active nodes。
- 每个 node 的内容、分数来源和 triples；triple 的 HyperEdge 上下文使用 `scope_edge_ids`，同一个 triple 可以保留多个 edge scope。
- 每个 node 的系统来源字段，例如 `source_turn_ids`、`node_metadata`。
- 如果 edge 属于 EdgeCluster，附带 cluster id，以及从 cluster member HyperEdges 动态读取的 edge descriptions。

具体 JSON metadata 字段以 `current_implementation.md` 为准。

## 当前不做

当前检索流程不接入：

- entity alias recall
- turn dialogue recall
- recency decay
- access boost
- temporal filter
- LLM rerank
- spaCy query analysis
- LLM query analysis

这些能力如果后续需要加入，应等到对应开发阶段再设计和实现。
