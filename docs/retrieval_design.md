# C-HyperMem 检索流程设计

本文档基于当前 `C-HyperMem` 代码和 `docs/` 下已有架构文档，设计下一阶段检索流程。重点是把已完成的写入侧结构化图谱与多路向量索引接入 `Memory.search()`，形成可解释、可消融、可逐步实现的混合检索链路。

参考代码：

- C-HyperMem 当前入口：`c_hypermem/memory.py`
- C-HyperMem 当前检索：`c_hypermem/retrieval/recall.py`、`query_analysis.py`、`expansion.py`
- C-HyperMem 当前 lexical：`c_hypermem/stores/lexical_store.py`
- C-HyperMem 当前向量写入：`c_hypermem/stores/vector_store.py`
- mem0 参考实现：`mem0/mem0/memory/main.py`、`mem0/mem0/utils/lemmatization.py`、`mem0/mem0/utils/entity_extraction.py`、`mem0/mem0/utils/scoring.py`

## 1. 当前状态摘要

当前 `Memory.search()` 的实际链路为：

```text
Memory.search(query, namespace, top_k)
  -> Retriever.search(...)
     -> QueryAnalyzer.analyze(query)  # controlled by retrieval.query_analysis
     -> store.list_nodes(namespace)
     -> LexicalScorer.score(query, nodes)
     -> _apply_access_scores(...)
     -> EdgeExpansion.expand(...)  # incident HyperEdge expansion
     -> SearchResult
  -> _record_access(...)
```

已具备的能力：

- `LexicalScorer` 对节点快照做 BM25-like 召回，文本来源包括 `canonical_text/normalized_text/content/summary/node_labels/aliases/attributes/local_graph.triples`。
- `QueryAnalyzer` 已独立成可配置模块：
  - `false`：禁用 query analysis，评测阶段默认使用。
  - `llm`：调用 `prompts/retrieval/query_analysis.md`，返回 JSON metadata。
  - `nlp`：使用可选 spaCy 处理 query，产出 `normalized_query/bm25_query/entities`。
- `EdgeExpansion` 通过 incident HyperEdge 扩展候选节点，并给 seed / expanded 节点追加 `edge_coherence`、`edge_expansion` 分数。
- `SearchResult.metadata.score_parts` 已可解释。
- 写入侧已经把以下语义类型分别写入 Qdrant collection：
  - `triple`：实际为 node-local-graph 向量，一个 node 一个点。
  - `node_content`
  - `node_summary`
  - `edge_cluster_canonical`
  - `edge_cluster_variant`
  - `turn_dialogue`

主要缺口：

- `VectorStore` 协议当前只有 `upsert/delete/delete_namespace/close`，没有 search 接口。
- 检索主流程尚未使用 Qdrant 向量召回。
- 尚未使用 `edge_cluster_canonical/variant` 做 cluster 召回和扩展。
- `turn_dialogue` 向量命中后还没有回 SQLite `turns` 表取完整对话。
- Query analysis 当前只写入结果 metadata，尚未参与召回、过滤或排序。

## 2. 设计目标

检索流程目标：

- 用 `lexical + vector + entity + hyperedge + edge cluster + turn dialogue` 多路召回提高答案事实命中率。
- 保持 SQLite 为 canonical store，Qdrant 只是可重建旁路索引。
- 保持不同语义向量 collection 分开召回、分开限流、分开解释。
- 让召回和排序可解释：每个结果返回命中的通道、分数来源、相关边/簇、命中 triple 或 dialogue turn。
- 避免把 `Retriever` 写成巨型类；新增能力应拆到 `query_analysis.py`、`vector_recall.py`、`expansion.py`、`ranking.py`、`context.py`。
- 先做 deterministic + embedding 的 M1 检索增强，LLM query analysis / rerank 作为可选 M2。

## 3. 目标检索 Pipeline

```text
Memory.search(query, namespace, top_k, metadata=None)
  1. QueryAnalysis:
     - false: no analysis, no query-derived retrieval hints
     - llm: prompt-based JSON query metadata
     - nlp: spaCy normalized_query / bm25_query / entities

  2. Parallel recall:
     - lexical node recall
     - vector node_content recall
     - vector node_summary recall
     - vector node_local_graph recall
     - entity alias recall
     - optional turn_dialogue recall
     - optional edge_cluster canonical / variant recall

  3. Candidate merge:
     - merge by node_id / cluster_id / turn_id
     - keep all evidence in score_parts and hit metadata
     - semantic threshold只过滤纯向量弱命中，不过滤强 lexical/entity 命中

  4. Graph expansion:
     - seed node -> incident HyperEdges -> related nodes
     - seed edge -> EdgeCluster -> sibling edges -> related nodes
     - seed cluster -> member edges -> member nodes
     - turn_dialogue -> source_turn_id -> graph nodes sourced from that turn

  5. Ranking:
     - combine semantic, lexical, entity, structural, temporal, recency, access
     - prefer answer labels: fact/preference/task/state/event
     - penalize retired/invalidated/uncertain unless explicitly asked

  6. Context composition:
     - return concise node answer text first
     - include source_turn_ids / triples / hyper_edge_ids / cluster_ids
     - optionally include turn dialogue snippets only when useful
```

## 4. QueryAnalysis 配置

当前实现已支持 `retrieval.query_analysis` 配置：

```yaml
retrieval:
  # Query analysis modes: false disables analysis for evaluation; "llm" uses prompts/retrieval/query_analysis.md; "nlp" uses optional spaCy processing.
  query_analysis: false
```

三种模式：

- `false`：禁用 query analysis。评测阶段建议使用该模式，避免 query 预处理对结果产生额外变量。
- `llm`：调用 `c_hypermem/prompts/retrieval/query_analysis.md`，要求配置 `llm`。LLM 输出只作为 query metadata，不直接返回记忆、不生成 memory id、不生成分数。
- `nlp`：使用可选 spaCy 策略，提供 `normalized_query/bm25_query/entities`。需要额外安装 `c-hypermem[nlp]` 和 `en_core_web_sm`。

当前 `QueryAnalysis` 结构为：

```python
@dataclass(frozen=True)
class QueryAnalysis:
    query: str
    mode: str
    normalized_query: str = ""
    bm25_query: str = ""
    entities: list[dict[str, str]] = field(default_factory=list)
    attributes: dict[str, Any] = field(default_factory=dict)
```

实现约束：

- `false` 不做任何 query 派生分析。
- `llm` 模式缺少 `config.llm` 时直接报错。
- `nlp` 模式缺少 spaCy 或 `en_core_web_sm` 时直接报错；不做静默回退。
- 当前检索策略不会根据 query analysis 做 preference/task/entity/time 等规则加分。

## 5. 召回通道设计

### 5.1 Lexical Node Recall

保留当前 `LexicalScorer`，但建议改造为两个输入：

- 原始 query：用于 exact phrase bonus。
- `bm25_query`：用于 BM25 terms。

候选输出：

```python
CandidateHit(
    target_type="node",
    target_id=node.node_id,
    score=...,
    channel="lexical",
    score_parts={"lexical": ..., "exact": ...},
    evidence={"matched_text": "..."}
)
```

如果短期内不接 SQLite FTS，可以继续用当前 namespace snapshot BM25-like scorer。后续数据量变大时再把 `text_lemmatized` 或 node searchable text 落入 SQLite FTS/Qdrant sparse index。

### 5.2 Dense Vector Node Recall

给 `VectorStore` 增加 search 协议：

```python
@dataclass(frozen=True)
class VectorSearchHit:
    id: str
    score: float
    payload: dict[str, Any]
    text: str = ""

class VectorStore(Protocol):
    def search(
        self,
        *,
        query: str,
        vector: list[float],
        top_k: int,
        filters: dict[str, Any] | None = None,
    ) -> list[VectorSearchHit]: ...
```

`QdrantVectorStore.search(...)` 应：

- 用 `query_points` 按 cosine 相似度查当前 collection。
- filters 至少支持 `namespace` 和 active status。
- 返回 payload 中的 `item_type/node_id/cluster_id/turn_id/text`。

召回通道：

- `node_content`：适合直接事实文本和偏好。
- `node_summary`：适合 event/fact 的摘要式匹配。
- `triple` / `node_local_graph`：适合 subject-predicate-object 关系问题。

各通道独立限流：

```yaml
retrieval:
  vector_top_n: 30
  vector_channels:
    node_content: 20
    node_summary: 15
    node_local_graph: 30
```

### 5.3 Entity Alias Recall

C-HyperMem 已有 `entity_alias_index`，但当前只用于写入侧 entity resolution。检索侧建议增加：

```text
query entity candidates
  -> normalize aliases
  -> find_entity_alias(namespace, aliases)
  -> seed entity node
  -> incident HyperEdge expansion
  -> related fact/state/event nodes
```

这条通道适合 “What is Toby?”、“Who is Alice's manager?” 这类实体锚点强的问题。命中实体节点本身不一定直接返回，更多作为图扩展 seed。

### 5.4 Entity Vector Boost

借鉴 mem0 的 entity store 思路，但 C-HyperMem 不需要新增独立 entity vector store 作为第一选择，因为已有：

- `entity_alias_index`
- `entity` label 的 `node_content/node_summary`
- entity 节点的 local graph

第一版建议：

- 用 `entity_alias_index` 做确定性实体锚定。
- 如果 alias 没命中，再用 `node_content` collection 过滤 `node_labels contains entity` 做 entity semantic recall。
- 对同一实体相关的 fact/state/event 节点加 `entity_boost`，而不是直接把 entity 节点排到顶部。

分数可借鉴 mem0：

```text
entity_boost = similarity * 0.5 * memory_count_weight
memory_count_weight = 1 / (1 + 0.001 * (linked_count - 1)^2)
```

C-HyperMem 中 `linked_count` 可替换为该实体 incident edges 或相关 fact 数，避免超级实体把大量无关记忆全部推高。

### 5.5 EdgeCluster Recall / Expansion

写入侧已索引：

- `edge_cluster_canonical`
- `edge_cluster_variant`

检索侧应把 cluster 作为中间候选：

```text
query vector
  -> edge_cluster_canonical / edge_cluster_variant
  -> cluster_id
  -> store.list_edge_cluster_members(cluster_id)
  -> edge_ids
  -> edge.node_ids
  -> node candidates
```

Cluster 召回的作用不是直接返回 cluster 文本，而是召回一组相关边和节点。它适合：

- 主题漂移后的同一工作集。
- 近似重复和补充事实。
- 需要把多个事实合在一起回答的问题。
- 冲突事实提示：`conflict_state=contains_conflict` 时结果 metadata 应暴露。

### 5.6 Turn Dialogue Recall

`turn_dialogue` 向量不应直接作为权威答案文本。命中后流程：

```text
turn_dialogue hit
  -> payload.turn_id
  -> SQLite turns 表取该 turn_id 的 user/assistant messages
  -> 找 source_turn_ids 包含该 turn_id 的 nodes/edges
  -> 将相关 nodes 作为候选
  -> 仅在 query wants_dialogue 或缺少结构化节点时附带 dialogue snippet
```

这能保留“我当时怎么说的”这类问题的答案能力，同时避免聊天流水账污染结构化图谱结果。

## 6. 候选合并与排序

建议新增 `retrieval/ranking.py`，把分数合并从 `Retriever` 拆出。

候选结构：

```python
@dataclass
class RetrievalCandidate:
    node: MemoryNode
    score: float = 0.0
    score_parts: dict[str, float] = field(default_factory=dict)
    channels: set[str] = field(default_factory=set)
    edge_ids: set[str] = field(default_factory=set)
    cluster_ids: set[str] = field(default_factory=set)
    turn_ids: set[str] = field(default_factory=set)
    hit_payloads: list[dict[str, Any]] = field(default_factory=list)
```

第一版推荐加权：

```text
score =
  0.35 * dense_best
  + 0.25 * lexical_norm
  + 0.15 * entity_boost
  + 0.15 * edge_cluster_or_hyperedge_coherence
  + 0.05 * temporal_match
  + 0.03 * recency_bonus
  + 0.02 * access_boost
```

实现上不要一开始强行归一到严格概率。更重要的是：

- 所有分量进入 `score_parts`。
- 每个通道上限可控，避免某一路召回压倒全部结果。
- `fact/preference/task/state/event` 作为 answer labels 优先返回。
- `entity` 节点如果只是 seed，除非 query 明确问实体本身，否则排在相关 fact/state 后面。
- `retired/invalidated` 默认过滤；如果召回到 correction edge，可在 metadata 里提示历史事实被替换。

可借鉴 mem0 的 `score_and_rank(...)` 思路：先用 semantic threshold 控制纯向量噪声，再 additive merge BM25/entity 信号。但 C-HyperMem 需要让 strong lexical/entity/hyperedge seed 有机会进入排序，不应被 semantic threshold 一刀切。

## 7. SearchResult 组装

`content` 建议保持简洁：

```text
[fact+preference] Alice prefers morning interviews.
Source: turn=turn:17 date=2024-01-03 edge_types=state,evidence
```

`metadata` 建议增加：

- `channels`
- `score_parts`
- `node_id/node_labels/status`
- `matched_vector_items`
- `matched_triples`
- `hyper_edge_ids/edge_types`
- `cluster_ids/cluster_conflict_states`
- `source_turn_ids`
- `dialogue_snippet`，仅当启用 turn dialogue 且确实有帮助。

## 8. 建议实施顺序

1. 扩展 `VectorStore` / `QdrantVectorStore` search 接口，只支持 dense search + namespace filter。
2. 新增 `retrieval/vector_recall.py`，接入 `node_content/node_summary/triple` 三路向量召回。
3. 扩展 `Retriever` 候选结构，合并 lexical + vector + 当前 EdgeExpansion。
4. 后续若启用 query-derived recall，再让 `QueryAnalyzer` 的输出进入独立召回通道；不要把规则加分重新塞回 `Retriever`。
5. 接入 entity alias recall 和 entity seed expansion。
6. 接入 `edge_cluster_canonical/variant` recall 和 cluster expansion。
7. 接入 `turn_dialogue` recall，命中后回 SQLite turns 表和 source_turn_ids。
8. 把 ranking 迁移到 `retrieval/ranking.py`，补充可解释 `score_parts`。
9. 增加消融配置：`use_vector_recall/use_entity_recall/use_edge_cluster_expansion/use_turn_dialogue_recall`。

## 9. mem0 检索策略参考

mem0 当前检索核心在 `mem0/mem0/memory/main.py::_search_vector_store`，同步版本关键步骤如下：

```python
def _search_vector_store(self, query, filters, limit, threshold=0.1):
    if threshold is None:
        threshold = 0.1

    # Step 1: Preprocess query
    query_lemmatized = lemmatize_for_bm25(query)
    query_entities = extract_entities(query)

    # Step 2: Embed query
    embeddings = self.embedding_model.embed(query, "search")

    # Step 3: Semantic search (over-fetch for scoring pool)
    internal_limit = max(limit * 4, 60)
    semantic_results = self.vector_store.search(
        query=query, vectors=embeddings, top_k=internal_limit, filters=filters
    )

    # Step 4: Keyword search (if store supports it)
    keyword_results = self.vector_store.keyword_search(
        query=query_lemmatized, top_k=internal_limit, filters=filters
    )

    # Step 5: Compute BM25 scores from keyword results
    bm25_scores = {}
    if keyword_results is not None:
        midpoint, steepness = get_bm25_params(query, lemmatized=query_lemmatized)
        for mem in keyword_results:
            mem_id = str(mem.id) if hasattr(mem, "id") else str(mem.get("id", ""))
            raw_score = mem.score if hasattr(mem, "score") else mem.get("score", 0)
            if raw_score and raw_score > 0:
                bm25_scores[mem_id] = normalize_bm25(raw_score, midpoint, steepness)

    # Step 6: Compute entity boosts
    entity_boosts = {}
    if query_entities:
        entity_boosts = self._compute_entity_boosts(query_entities, filters)

    # Step 7: Build candidate set from semantic results
    candidates = []
    for mem in semantic_results:
        candidates.append({
            "id": str(mem.id),
            "score": mem.score,
            "payload": mem.payload if hasattr(mem, "payload") else {},
        })

    # Step 8: Score and rank
    scored_results = score_and_rank(
        semantic_results=candidates,
        bm25_scores=bm25_scores,
        entity_boosts=entity_boosts,
        threshold=threshold,
        top_k=limit,
    )
```

mem0 的关键特征：

- 查询先做三种处理：`lemmatize_for_bm25(query)`、`extract_entities(query)`、`embedding_model.embed(query, "search")`。
- dense vector search 是主候选池，`internal_limit = max(limit * 4, 60)` 做 over-fetch。
- keyword BM25 和 entity search 不直接生成最终候选，而是给 semantic candidates 加分。
- BM25 原始分使用 query-length-adaptive sigmoid 归一化到 `[0, 1]`。
- entity boost 使用独立 entity store，命中 entity 后把 boost 传给 linked memories。

### 9.1 mem0: `lemmatize_for_bm25(query)`

源文件：`mem0/mem0/utils/lemmatization.py`

实现方案：

- 使用 spaCy lemma model：`get_nlp_lemma()`。
- spaCy 不可用时返回原始文本，保证检索不中断。
- 对小写文本做 lemmatization。
- 过滤标点和 stop words。
- 只保留 `lemma.isalnum()` 的 lemma。
- 对 `-ing` 结尾且 lemma 不同的 token，同时加入原词，缓解 “meeting” 名词/动词歧义。

对应代码：

```python
def lemmatize_for_bm25(text: str) -> str:
    """Lemmatize text for BM25 matching.

    Returns space-joined lemmas for full-text search. Falls back to
    the original text if spaCy is unavailable.
    """
    from mem0.utils.spacy_models import get_nlp_lemma

    nlp = get_nlp_lemma()
    if nlp is None:
        return text

    doc = nlp(text.lower())
    tokens = []

    for token in doc:
        if token.is_punct or token.is_stop:
            continue

        lemma = token.lemma_
        if lemma.isalnum():
            tokens.append(lemma)

        # Also add original if it ends in -ing and differs from lemma.
        # This handles noun/verb ambiguity (meeting/meet, attending/attend).
        if token.text.endswith("ing") and token.text != lemma and token.text.isalnum():
            tokens.append(token.text)

    return " ".join(tokens)
```

C-HyperMem 可借鉴点：

- 在 query 和入库 searchable text 两端使用同样 lemmatization。
- spaCy 作为可选依赖；在 C-HyperMem 当前实现中，`query_analysis: nlp` 缺依赖会直接报错，不做静默回退。
- 对中文或多语言数据不要强依赖英文 lemma，后续可按语言开关。

### 9.2 mem0: `extract_entities(query)`

源文件：`mem0/mem0/utils/entity_extraction.py`

实现方案：

- 使用 spaCy full model：`get_nlp_full()`。
- spaCy 不可用时返回空列表，保证检索不中断。
- 返回去重后的 `(entity_type, entity_text)` 列表。
- 抽取类型包括：
  - `PROPER`：非句首噪声的专名/大写多词序列。
  - `QUOTED`：单引号或双引号内文本。
  - `COMPOUND`：名词短语、noun-noun compound、带具体修饰词的复合名词。
  - `NOUN`：在特定 fallback 场景下保留的单名词。
- 清理 markdown/项目符号/过长文本/泛化尾词，并按 `PROPER > COMPOUND > QUOTED > NOUN` 保留最佳类型。

对应入口代码：

```python
def extract_entities(text: str) -> List[Tuple[str, str]]:
    """Extract named entities, quoted text, and noun compounds from text.

    This is the public API that accepts a string. It loads the spaCy model
    internally and delegates to _extract_entities_from_doc().

    Args:
        text: Input text to extract entities from.

    Returns:
        Deduplicated list of (entity_type, entity_text) tuples.
        Entity types: PROPER, QUOTED, COMPOUND, NOUN.
        Returns empty list if spaCy is unavailable.
    """
    from mem0.utils.spacy_models import get_nlp_full

    nlp = get_nlp_full()
    if nlp is None:
        return []

    doc = nlp(text)
    return _extract_entities_from_doc(doc)
```

`_extract_entities_from_doc(doc)` 的核心逻辑可概括为：

```python
def _extract_entities_from_doc(doc) -> List[Tuple[str, str]]:
    entities = []
    text = doc.text
    tokens = list(doc)

    # 1. PROPER: collect capitalized proper/noun/adjective sequences,
    # skipping labels, formatting markers, sentence-start noise, and trailing
    # function words.

    # 2. QUOTED: collect non-trivial text inside double quotes and single quotes.

    # 3. COMPOUND/NOUN: iterate doc.noun_chunks, split possessives/punctuation,
    # remove determiners/pronouns/punctuation/stop-like generic tokens, then
    # keep noun compounds or specific adjective+noun phrases.

    # 4. VERB fallback: for some mis-tagged verb heads used as objects/subjects,
    # recover compound phrases.

    # 5. Deduplicate and cleanup:
    # - strip markdown markers, trailing colons, numeric list prefixes
    # - drop artifacts and generic single capitalized words
    # - keep best type by priority
    # - remove entities that are substrings of longer entities

    return deduped_entities
```

C-HyperMem 可借鉴点：

- query entity extraction 不应依赖 LLM。
- 先用实体作为召回 seed，不要强制把实体节点本身作为最终答案。
- 与 C-HyperMem 的 `entity_alias_index` 结合，比 mem0 的纯 entity vector store 更适合当前图结构。
- C-HyperMem 的 `nlp` 模式移植了该思路，但不会在 spaCy 不可用时继续猜测实体。

### 9.3 mem0: `embedding_model.embed(query, "search")`

源文件：`mem0/mem0/memory/main.py::_search_vector_store`

实现方案：

- 对 query 调用 embedding model，`memory_action="search"` 用来区分 add/update/search 场景。
- 生成的 dense vector 用于 vector store semantic search。
- semantic search 过量召回，作为后续 BM25/entity 加分的候选池。

对应代码：

```python
# Step 2: Embed query
embeddings = self.embedding_model.embed(query, "search")

# Step 3: Semantic search (over-fetch for scoring pool)
internal_limit = max(limit * 4, 60)
semantic_results = self.vector_store.search(
    query=query,
    vectors=embeddings,
    top_k=internal_limit,
    filters=filters,
)
```

C-HyperMem 当前 embedding client 代码为 `EmbeddingModelClient.embed(texts: list[str])`，没有 action 参数。建议保持简单接口：

```python
query_vector = self.embedding_client.embed([analysis.query])[0]
```

如果后续需要兼容不同 provider 的 action / task type，可以扩展为：

```python
EmbeddingClient.embed(texts: list[str], *, purpose: Literal["add", "search"] = "add")
```

但第一版不建议为 action 参数改动全链路，除非当前 embedding provider 明确需要 query/document 双塔任务类型。

### 9.4 mem0: BM25 归一化与 additive ranking

源文件：`mem0/mem0/utils/scoring.py`

关键代码：

```python
def get_bm25_params(query: str, *, lemmatized: Optional[str] = None) -> tuple:
    if lemmatized is None:
        from mem0.utils.lemmatization import lemmatize_for_bm25
        lemmatized = lemmatize_for_bm25(query)
    num_terms = len(lemmatized.split()) if lemmatized else 1

    if num_terms <= 3:
        return 5.0, 0.7
    elif num_terms <= 6:
        return 7.0, 0.6
    elif num_terms <= 9:
        return 9.0, 0.5
    elif num_terms <= 15:
        return 10.0, 0.5
    else:
        return 12.0, 0.5


def normalize_bm25(raw_score: float, midpoint: float, steepness: float) -> float:
    return 1.0 / (1.0 + math.exp(-steepness * (raw_score - midpoint)))
```

```python
ENTITY_BOOST_WEIGHT = 0.5


def score_and_rank(
    semantic_results: List[Dict[str, Any]],
    bm25_scores: Dict[str, float],
    entity_boosts: Dict[str, float],
    threshold: float,
    top_k: int,
) -> List[Dict[str, Any]]:
    has_bm25 = bool(bm25_scores)
    has_entity = bool(entity_boosts)

    max_possible = 1.0
    if has_bm25:
        max_possible += 1.0
    if has_entity:
        max_possible += ENTITY_BOOST_WEIGHT

    scored = []

    for result in semantic_results:
        mem_id = result.get("id")
        if mem_id is None:
            continue

        semantic_score = result.get("score", 0.0)
        if semantic_score < threshold:
            continue

        mem_id_str = str(mem_id)
        bm25_score = bm25_scores.get(mem_id_str, 0.0)
        entity_boost = entity_boosts.get(mem_id_str, 0.0)

        raw_combined = semantic_score + bm25_score + entity_boost
        combined = min(raw_combined / max_possible, 1.0)

        scored.append({
            "id": mem_id_str,
            "score": combined,
            "payload": result.get("payload"),
        })

    scored.sort(key=lambda x: x["score"], reverse=True)
    return scored[:top_k]
```

C-HyperMem 可借鉴点：

- BM25 原始分需要归一化，否则不同 query 长度下不可比。
- additive ranking 简单、可解释，适合第一版。
- C-HyperMem 的图扩展候选不一定来自 semantic pool，所以 ranking 入口应接受所有通道候选，而不是只接受 dense semantic candidates。

## 10. MemoryOS 参考实现

MemoryOS 的检索入口在 `MemoryOS/memoryos-pypi/retriever.py`，核心策略是分层并行检索：

- 中期记忆：按 session summary 召回相关会话段，再在命中 session 内按 page embedding 召回具体 QA page。
- 用户长期知识：在 user knowledge deque 中做向量相似检索。
- 助手长期知识：在 assistant knowledge deque 中做向量相似检索。
- 三路检索用 `ThreadPoolExecutor(max_workers=3)` 并行执行，最终返回 `retrieved_pages/retrieved_user_knowledge/retrieved_assistant_knowledge`。

对应代码：

```python
def retrieve_context(self, user_query: str,
                     user_id: str,
                     segment_similarity_threshold=0.1,
                     page_similarity_threshold=0.1,
                     knowledge_threshold=0.01,
                     top_k_sessions=5,
                     top_k_knowledge=20):
    print(f"Retriever: Starting PARALLEL retrieval for query: '{user_query[:50]}...'")

    tasks = [
        lambda: self._retrieve_mid_term_context(user_query, segment_similarity_threshold, page_similarity_threshold, top_k_sessions),
        lambda: self._retrieve_user_knowledge(user_query, knowledge_threshold, top_k_knowledge),
        lambda: self._retrieve_assistant_knowledge(user_query, knowledge_threshold, top_k_knowledge)
    ]

    with ThreadPoolExecutor(max_workers=3) as executor:
        futures = []
        for i, task in enumerate(tasks):
            future = executor.submit(task)
            futures.append((i, future))

        results = [None] * 3
        for task_idx, future in futures:
            try:
                results[task_idx] = future.result()
            except Exception as e:
                print(f"Error in retrieval task {task_idx}: {e}")
                results[task_idx] = []

    retrieved_mid_term_pages, retrieved_user_knowledge, retrieved_assistant_knowledge = results

    return {
        "retrieved_pages": retrieved_mid_term_pages or [],
        "retrieved_user_knowledge": retrieved_user_knowledge or [],
        "retrieved_assistant_knowledge": retrieved_assistant_knowledge or [],
        "retrieved_at": get_timestamp()
    }
```

### 10.1 MemoryOS: 中期记忆检索

源文件：`MemoryOS/memoryos-pypi/mid_term.py`

中期记忆是两阶段检索：

1. 将 query embedding 与所有 session summary embedding 做 FAISS `IndexFlatIP` 内积检索。
2. 对超过 `segment_similarity_threshold` 的 session，逐个计算 page embedding 与 query embedding 的点积。
3. 只保留超过 `page_similarity_threshold` 的 page。
4. 命中后更新 session 访问统计：`N_visit/last_visit_time/access_count_lfu/H_segment`。

对应核心代码：

```python
def search_sessions(self, query_text, segment_similarity_threshold=0.1, page_similarity_threshold=0.1,
                    top_k_sessions=5, keyword_alpha=1.0, recency_tau_search=3600):
    if not self.sessions:
        return []

    query_vec = get_embedding(
        query_text,
        model_name=self.embedding_model_name,
        **self.embedding_model_kwargs
    )
    query_vec = normalize_vector(query_vec)

    session_ids = list(self.sessions.keys())
    summary_embeddings_list = [self.sessions[s]["summary_embedding"] for s in session_ids]
    summary_embeddings_np = np.array(summary_embeddings_list, dtype=np.float32)

    dim = summary_embeddings_np.shape[1]
    index = faiss.IndexFlatIP(dim)
    index.add(summary_embeddings_np)

    query_arr_np = np.array([query_vec], dtype=np.float32)
    distances, indices = index.search(query_arr_np, min(top_k_sessions, len(session_ids)))

    results = []
    current_time_str = get_timestamp()

    for i, idx in enumerate(indices[0]):
        if idx == -1:
            continue

        session_id = session_ids[idx]
        session = self.sessions[session_id]
        semantic_sim_score = float(distances[0][i])

        session_keywords = set(session.get("summary_keywords", []))
        s_topic_keywords = 0

        session_relevance_score = semantic_sim_score + keyword_alpha * s_topic_keywords

        if session_relevance_score >= segment_similarity_threshold:
            matched_pages_in_session = []
            for page in session.get("details", []):
                page_embedding = np.array(page["page_embedding"], dtype=np.float32)
                page_sim_score = float(np.dot(page_embedding, query_vec))

                if page_sim_score >= page_similarity_threshold:
                    matched_pages_in_session.append({"page_data": page, "score": page_sim_score})

            if matched_pages_in_session:
                session["N_visit"] += 1
                session["last_visit_time"] = current_time_str
                session["access_count_lfu"] = session.get("access_count_lfu", 0) + 1
                self.access_frequency[session_id] = session["access_count_lfu"]
                session["H_segment"] = compute_segment_heat(session)
                self.rebuild_heap()

                results.append({
                    "session_id": session_id,
                    "session_summary": session["summary"],
                    "session_relevance_score": session_relevance_score,
                    "matched_pages": sorted(matched_pages_in_session, key=lambda x: x["score"], reverse=True)
                })

    self.save()
    return sorted(results, key=lambda x: x["session_relevance_score"], reverse=True)
```

`Retriever._retrieve_mid_term_context(...)` 会把所有命中 session 的 page 放入一个固定容量 heap，只保留全局 top pages：

```python
top_pages_heap = []
page_counter = 0
for session_match in matched_sessions:
    for page_match in session_match.get("matched_pages", []):
        page_data = page_match["page_data"]
        page_score = page_match["score"]
        combined_score = page_score

        if len(top_pages_heap) < self.retrieval_queue_capacity:
            heapq.heappush(top_pages_heap, (combined_score, page_counter, page_data))
            page_counter += 1
        elif combined_score > top_pages_heap[0][0]:
            heapq.heappop(top_pages_heap)
            heapq.heappush(top_pages_heap, (combined_score, page_counter, page_data))
            page_counter += 1

retrieved_pages = [item[2] for item in sorted(top_pages_heap, key=lambda x: x[0], reverse=True)]
```

### 10.2 MemoryOS: 长期知识检索

源文件：`MemoryOS/memoryos-pypi/long_term.py`

长期知识以 deque 保存，每条知识保存 normalized embedding。检索时临时构建 FAISS `IndexFlatIP`，按内积相似度召回，并用 threshold 过滤。

对应代码：

```python
def _search_knowledge_deque(self, query, knowledge_deque: deque, threshold=0.1, top_k=5):
    if not knowledge_deque:
        return []

    query_vec = get_embedding(
        query,
        model_name=self.embedding_model_name,
        **self.embedding_model_kwargs
    )
    query_vec = normalize_vector(query_vec)

    embeddings = []
    valid_entries = []
    for entry in knowledge_deque:
        if "knowledge_embedding" in entry and entry["knowledge_embedding"]:
            embeddings.append(np.array(entry["knowledge_embedding"], dtype=np.float32))
            valid_entries.append(entry)

    if not embeddings:
        return []

    embeddings_np = np.array(embeddings, dtype=np.float32)
    if embeddings_np.ndim == 1:
        if embeddings_np.shape[0] == 0:
            return []
        embeddings_np = embeddings_np.reshape(1, -1)

    dim = embeddings_np.shape[1]
    index = faiss.IndexFlatIP(dim)
    index.add(embeddings_np)

    query_arr = np.array([query_vec], dtype=np.float32)
    distances, indices = index.search(query_arr, min(top_k, len(valid_entries)))

    results = []
    for i, idx in enumerate(indices[0]):
        if idx != -1:
            similarity_score = float(distances[0][i])
            if similarity_score >= threshold:
                results.append(valid_entries[idx])

    results.sort(
        key=lambda x: float(np.dot(np.array(x["knowledge_embedding"], dtype=np.float32), query_vec)),
        reverse=True
    )
    return results
```

C-HyperMem 可借鉴点：

- 分层召回很适合 C-HyperMem 的 `turn_dialogue -> graph nodes`、`EdgeCluster -> edges -> nodes` 两阶段检索。
- 中期 session summary 先筛、page 再筛的结构，可类比为 `EdgeCluster` 先筛、cluster member nodes 再筛。
- 检索命中后更新访问统计和 heat，和 C-HyperMem 当前 `last_access_turn/access_count` 可以合流。
- 固定容量 heap 适合控制 expansion 后的上下文预算，避免图扩展无限膨胀。
- MemoryOS 把短期历史、中期召回、长期画像/知识分别放入 prompt；C-HyperMem 也应区分结构化节点答案、turn dialogue 证据、长期 instruction/profile 类节点。

## 11. A-mem 参考实现

A-mem 的检索入口主要在：

- `A-mem/agentic_memory/retrievers.py`
- `A-mem/agentic_memory/memory_system.py`

核心策略：

- 使用 ChromaDB + SentenceTransformer 做语义检索。
- 每条 memory 是 `MemoryNote`，除 content 外还维护 `keywords/context/tags/links/retrieval_count/timestamp/last_accessed/evolution_history/category`。
- 新增记忆时，先检索 nearest neighbors，再让 LLM 决定是否强化链接或更新邻居的 context/tags。
- 检索时先返回 Chroma 命中，再追加 linked memories 作为邻居扩展。

### 11.1 A-mem: Chroma Retriever

源文件：`A-mem/agentic_memory/retrievers.py`

对应代码：

```python
class ChromaRetriever:
    """Vector database retrieval using ChromaDB"""

    def __init__(self,
                 collection_name: str = "memories",
                 model_name: str = "all-MiniLM-L6-v2"):
        self.client = chromadb.Client(Settings(allow_reset=True))
        self.embedding_function = SentenceTransformerEmbeddingFunction(
            model_name=model_name
        )
        self.collection = self.client.get_or_create_collection(
            name=collection_name, embedding_function=self.embedding_function
        )

    def add_document(self, document: str, metadata: Dict, doc_id: str):
        processed_metadata = {}
        for key, value in metadata.items():
            if isinstance(value, list):
                processed_metadata[key] = json.dumps(value)
            elif isinstance(value, dict):
                processed_metadata[key] = json.dumps(value)
            else:
                processed_metadata[key] = str(value)

        self.collection.add(
            documents=[document], metadatas=[processed_metadata], ids=[doc_id]
        )

    def search(self, query: str, k: int = 5):
        results = self.collection.query(query_texts=[query], n_results=k)

        if (results is not None) and (results.get("metadatas", [])):
            results["metadatas"] = self._convert_metadata_types(
                results["metadatas"])

        return results
```

### 11.2 A-mem: 普通语义检索

源文件：`A-mem/agentic_memory/memory_system.py`

`search(...)` 直接读取 Chroma 的 `ids/distances`，再回本地 `self.memories` 取结构化 note：

```python
def search(self, query: str, k: int = 5) -> List[Dict[str, Any]]:
    """Search for memories using a hybrid retrieval approach."""
    search_results = self.retriever.search(query, k)
    memories = []

    for i, doc_id in enumerate(search_results["ids"][0]):
        memory = self.memories.get(doc_id)
        if memory:
            memories.append({
                "id": doc_id,
                "content": memory.content,
                "context": memory.context,
                "keywords": memory.keywords,
                "score": search_results["distances"][0][i]
            })

    return memories[:k]
```

### 11.3 A-mem: Agentic 检索与链接扩展

`search_agentic(...)` 在 Chroma 命中后，会追加每条 memory 的 `links` 指向的邻居记忆。邻居结果标记 `is_neighbor=True`，这相当于基于 memory graph 的一跳扩展。

对应代码：

```python
def search_agentic(self, query: str, k: int = 5) -> List[Dict[str, Any]]:
    """Search for memories using ChromaDB retrieval."""
    if not self.memories:
        return []

    try:
        results = self.retriever.search(query, k)

        memories = []
        seen_ids = set()

        if ("ids" not in results or not results["ids"] or
            len(results["ids"]) == 0 or len(results["ids"][0]) == 0):
            return []

        for i, doc_id in enumerate(results["ids"][0][:k]):
            if doc_id in seen_ids:
                continue

            if i < len(results["metadatas"][0]):
                metadata = results["metadatas"][0][i]

                memory_dict = {
                    "id": doc_id,
                    "content": metadata.get("content", ""),
                    "context": metadata.get("context", ""),
                    "keywords": metadata.get("keywords", []),
                    "tags": metadata.get("tags", []),
                    "timestamp": metadata.get("timestamp", ""),
                    "category": metadata.get("category", "Uncategorized"),
                    "is_neighbor": False
                }

                if "distances" in results and len(results["distances"]) > 0 and i < len(results["distances"][0]):
                    memory_dict["score"] = results["distances"][0][i]

                memories.append(memory_dict)
                seen_ids.add(doc_id)

        neighbor_count = 0
        for memory in list(memories):
            if neighbor_count >= k:
                break

            links = memory.get("links", [])
            if not links and "id" in memory:
                mem_obj = self.memories.get(memory["id"])
                if mem_obj:
                    links = mem_obj.links

            for link_id in links:
                if link_id not in seen_ids and neighbor_count < k:
                    neighbor = self.memories.get(link_id)
                    if neighbor:
                        memories.append({
                            "id": link_id,
                            "content": neighbor.content,
                            "context": neighbor.context,
                            "keywords": neighbor.keywords,
                            "tags": neighbor.tags,
                            "timestamp": neighbor.timestamp,
                            "category": neighbor.category,
                            "is_neighbor": True
                        })
                        seen_ids.add(link_id)
                        neighbor_count += 1

        return memories[:k]
    except Exception as e:
        logger.error(f"Error in search_agentic: {str(e)}")
        return []
```

### 11.4 A-mem: 写入时建立可检索链接

A-mem 的检索质量部分来自写入时的 agentic 组织：新增 note 会先查 nearest neighbors，再让 LLM 决定是否：

- `strengthen`：把新 note 链到相关邻居，并更新新 note tags。
- `update_neighbor`：更新邻居 memory 的 context/tags。

对应片段：

```python
def process_memory(self, note: MemoryNote) -> Tuple[bool, MemoryNote]:
    if not self.memories:
        return False, note

    try:
        neighbors_text, indices = self.find_related_memories(note.content, k=5)
        if not neighbors_text or not indices:
            return False, note

        prompt = self._evolution_system_prompt.format(
            content=note.content,
            context=note.context,
            keywords=note.keywords,
            nearest_neighbors_memories=neighbors_text,
            neighbor_number=len(indices)
        )

        response = self.llm_controller.llm.get_completion(
            prompt,
            response_format={...}
        )

        response_json = json.loads(response)
        should_evolve = response_json["should_evolve"]

        if should_evolve:
            actions = response_json["actions"]
            for action in actions:
                if action == "strengthen":
                    suggest_connections = response_json["suggested_connections"]
                    new_tags = response_json["tags_to_update"]
                    note.links.extend(suggest_connections)
                    note.tags = new_tags
                elif action == "update_neighbor":
                    new_context_neighborhood = response_json["new_context_neighborhood"]
                    new_tags_neighborhood = response_json["new_tags_neighborhood"]
                    # update neighbor notes' context/tags

        return should_evolve, note
    except Exception:
        return False, note
```

C-HyperMem 可借鉴点：

- A-mem 的 `links` 与 C-HyperMem 的 `HyperEdge/EdgeCluster` 都是“检索时一跳扩展”的结构依据。C-HyperMem 可以比 A-mem 更精确，因为 edge 有 relation、roles、polarity、cluster conflict state。
- `search_agentic` 的 `is_neighbor` 标记值得借鉴：C-HyperMem 应在 metadata 中区分 direct hit 与 graph-expanded hit。
- A-mem 在写入时用 LLM 维护 tags/context/links；C-HyperMem 当前已有 maintenance prompt，但检索侧第一版应先消费已存在的 EdgeCluster/description variants，暂不把 LLM rerank 放进必选路径。
- A-mem 的 linked neighbor 扩展没有额外重排，C-HyperMem 应对 expanded node 加较低结构分，避免邻居压过直接命中。

## 12. 配置建议

建议在 `configs/default.yaml` 的 `retrieval` 下补充：

```yaml
retrieval:
  lexical_top_n: 30
  vector_top_n: 30
  edge_top_n: 30
  rerank_top_n: 12

  use_vector_recall: true
  use_entity_recall: true
  use_hyperedge_expansion: true
  use_edge_cluster_expansion: true
  use_turn_dialogue_recall: true

  vector_channels:
    node_content: 20
    node_summary: 15
    node_local_graph: 30
    edge_cluster_canonical: 10
    edge_cluster_variant: 10
    turn_dialogue: 10

  semantic_threshold: 0.1
  use_temporal_filter: true
  use_recency_decay: true
  recency_decay_lambda: 0.03
  access_boost: 0.05
```

## 13. 测试与消融

最小测试覆盖：

- `QueryAnalyzer` 在 `false` 模式下不做 query analysis。
- `QueryAnalyzer` 在 `nlp` 模式缺少 spaCy 或模型时明确报错。
- `QueryAnalyzer` 在 `llm` 模式缺少 `config.llm` 时明确报错。
- `VectorStore.search` 能按 namespace 过滤，且不同 collection 返回正确 payload。
- `Retriever` 能合并 lexical + vector 命中同一 node 的 score_parts。
- entity alias 命中后能通过 HyperEdge 找到相关 fact。
- edge cluster 命中后能扩展到 member edge 的相关 nodes。
- turn_dialogue 命中后能返回 `turn_id`，并找到 source_turn_ids 相关节点。
- retired/invalidated 节点默认不返回。

建议消融：

- lexical only
- vector only
- lexical + vector
- lexical + vector + hyperedge expansion
- lexical + vector + hyperedge + edge cluster
- full: lexical + vector + entity + hyperedge + edge cluster + turn dialogue

验证命令沿用当前项目：

```powershell
python -m compileall -q c_hypermem
python -m pytest -q
```
