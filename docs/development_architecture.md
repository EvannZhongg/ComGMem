# C-HyperMem 后续开发架构

本文档基于 `docs/hypergraph_memory_architecture.md` 的“复合节点高阶关联 Memory”构思，给出后续工程开发架构。目标是让 C-HyperMem 作为独立 Python 包开发，同时满足 `agent_memory_eval` 的自研 memory backend 接入要求。

核心边界：

- C-HyperMem 是独立 memory 包，负责核心算法、schema、存储、抽取、索引和检索。
- `agent_memory_eval` 只保留 thin adapter，负责 `MemorySession` / `MemoryItem` 格式转换、namespace 隔离和评测日志。
- 评测框架不直接依赖 C-HyperMem 内部模块，只依赖稳定对外 API：`Memory.from_config/reset/add/add_memory/search/stats/close`。
- C-HyperMem 不允许依赖 `agent_memory_eval` 的任何模块、数据类、配置加载器或 runner。依赖方向只能是 `agent_memory_eval -> C-HyperMem`。

## 0. 独立发布边界

C-HyperMem 必须作为独立算法项目开发和发布，定位类似 `mem0` 或 `A-mem` 这类 memory 架构包，而不是 `agent_memory_eval` 的内部 backend 实现。

本地参考项目体现了这个边界：

- `mem0` 是完整 SDK 包，核心代码在 `mem0/` 包内，并通过公开入口暴露 `Memory`、`AsyncMemory`、`MemoryClient` 等对象。
- `A-mem` 是轻量研究型独立包，核心代码在独立 Python 包内，示例和测试通过公开类使用它。

C-HyperMem 也应遵守同样原则：

- 可以被 `pip install -e C-HyperMem` 安装。
- 可以脱离 `agent_memory_eval` 单独运行 examples、tests 和 quickstart。
- 不导入 `agent_memory_eval.models.MemorySession`、`MemoryTurn`、`MemoryItem`。
- 不导入 `agent_memory_eval.config`、runner、suite runner、benchmark adapter 或 token usage tracker。
- 不把 benchmark 字段作为核心 schema 的必需字段；`session_id`、`question_id`、`benchmark` 等只能作为普通 metadata。
- 不把核心算法写进 `agent_memory_eval/backends/c_hypermem_backend.py`。
- 不让普通用户安装或理解 `agent_memory_eval` 才能使用 C-HyperMem。

允许的集成方式只有单向 adapter：

```text
agent_memory_eval/backends/c_hypermem_backend.py
  imports c_hypermem.Memory
  converts MemorySession -> Memory.add(...)
  converts Memory.search(...) -> MemoryItem

c_hypermem/
  never imports agent_memory_eval
```

如果未来希望 C-HyperMem 自带评测适配器，也只能作为可选模块，例如：

```text
c_hypermem/adapters/agent_memory_eval.py
```

这个模块不得被 `c_hypermem.memory`、`schema`、`pipeline`、`retrieval` 等核心模块导入；它只能反向消费核心公开 API。

注意：开发过程禁止使用任何规则化抽取策略或是兜底策略，在项目架构更新或重构时无需考虑旧数据的兼容，当前仍为开发环境无任何需要保留或兼容的数据。

## 1. 总体分层

```text
agent_memory_eval
  -> C-HyperMem adapter
  -> C-HyperMem public API
  -> memory pipeline
  -> stores / llms / embeddings

C-HyperMem internal:
  AgentInteraction / import messages
    -> compact semantic extraction
    -> MemoryNodes
    -> LocalNodeGraphs
    -> HyperEdges
    -> EdgeClusters
    -> Hybrid indexes
    -> Search results
```

C-HyperMem 的核心数据结构保持：

```text
Memory = MemoryNodes + HyperEdges + EdgeClusters + LocalNodeGraphs
```

其中：

- `MemoryNodes`：长期记忆共享节点池。节点使用统一 schema，通过可累积 `node_labels` 表达 fact、entity、event、tool 等语义标签。
- `HyperEdges`：具体高阶关系实例。一个节点可以属于多个超边，超边本身保守维护。
- `EdgeClusters`：相关超边的聚合对象，用来承接主题漂移、近似重复、更新和冲突。
- `LocalNodeGraphs`：复合节点内部挂载的属性、角色、三元组和局部状态。

重要调整：

- 系统根据抽取出的实体、事件、事实、属性、角色、三元组和来源，构建或更新 `HyperEdges`。
- 语义相近或成员重叠的 HyperEdge 不直接强行合并，而是优先挂入同一个 `EdgeCluster`。
- 超边可以表达来源证据、实体状态、时间聚合、修正关系、任务进度、语义聚合等关系。
- `fact`、`entity`、`event` 不作为不同内部 schema 维护，而是统一 `MemoryNode` 的默认 `node_labels`。

依赖方向必须保持：

```text
application / benchmark / examples
  -> c_hypermem public API
  -> c_hypermem internal modules
```

禁止出现：

```text
c_hypermem internal modules
  -> agent_memory_eval
```

## 2. 推荐目录结构

建议在 `C-HyperMem` 后续扩展为独立包：

```text
C-HyperMem/
  c_hypermem/
    __init__.py
    memory.py                  # 对外主入口 Memory
    config.py                  # 配置加载与校验
    schema.py                  # dataclass / pydantic schema
    errors.py

    pipeline/
      ingestion.py             # add() / add_memory() 写入主流程
      extraction.py            # 一次紧凑语义抽取
      assembly.py              # GraphAssembler 写入编排，不承载具体构建细节
      entity_resolution.py     # entity alias 对齐
      node_builder.py          # 根据抽取候选构建或复用 MemoryNode
      node_label_registry.py   # node_labels 配置注册与校验
      hyperedge_builder.py     # HyperEdge 构建与更新
      edge_cluster_builder.py  # EdgeCluster 聚合与冲突状态维护
      local_graph_builder.py   # 复合节点内部三元组/子图构建
      maintenance.py           # 去重、冲突、退役、状态更新

    retrieval/
      query_analysis.py
      recall.py
      lexical_recall.py        # lexical / FTS / BM25-style recall boundary
      vector_recall.py         # node_content / node_summary / node_local_graph recall
      fusion.py                # node-level fusion, e.g. RRF
      graph_ripple.py          # HyperEdge / EdgeCluster ripple expansion
      expansion.py             # legacy/simple incident-edge expansion boundary
      ranking.py               # optional future rerank/ranking policy
      context.py

    stores/
      base.py
      sqlite_store.py          # turns / nodes / hyper_edges / triples / metadata
      vector_store.py          # Qdrant-backed rebuildable vector indexes
      lexical_store.py

    llms/
      base.py
      openai_compatible.py

    prompts/                   # 每个 prompt 用独立 markdown 文件管理
      extraction/
        memory_extraction.md
      retrieval/
        query_analysis.md
      maintenance/
        fact_merge.md
        contradiction_check.md
        edge_merge.md
        edge_cluster_merge.md
        edge_conflict_check.md

    embeddings/
      base.py
      model_client.py

    adapters/                  # 可选集成，不被核心模块依赖
      agent_memory_eval.py     # optional thin adapter helpers only
      langchain.py             # optional ecosystem integration

    utils/
      ids.py
      time.py
      text.py
      hashing.py

  configs/
    default.yaml

  examples/
    quickstart.py

  tests/
  docs/
    hypergraph_memory_architecture.md
    development_architecture.md
  pyproject.toml
  README.md
```

`agent_memory_eval` 中只需要新增或修改：

```text
agent_memory_eval/backends/c_hypermem_backend.py
agent_memory_eval/backends/factory.py
configs/memory/c_hypermem.yaml
configs/suites/*.yaml
```

不要修改：

```text
agent_memory_eval/runner.py
agent_memory_eval/suite_runner.py
agent_memory_eval/agent.py
agent_memory_eval/benchmarks/*.py
LongMemEval/ 或 locomo/ 原始代码
```

### 2.1 包发布要求

`pyproject.toml` 应只打包 `c_hypermem`，不要把 `agent_memory_eval` 或评测仓库路径加入 package include。

建议形态：

```toml
[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[project]
name = "c-hypermem"
version = "0.1.0"
description = "Composite hyperedge memory for long-term agents"
readme = "README.md"
requires-python = ">=3.10"
dependencies = [
    "pydantic>=2",
    "numpy>=1.24",
]

[project.optional-dependencies]
llms = ["openai>=1.0"]
vector = ["qdrant-client>=1.9"]
eval = []
dev = ["pytest", "ruff"]

[tool.hatch.build.targets.wheel]
packages = ["c_hypermem"]
```

公开入口应在 `c_hypermem/__init__.py` 中保持简洁：

```python
from c_hypermem.memory import Memory

__all__ = ["Memory"]
```

## 3. Prompt 管理规范

所有 prompt 必须放在 `c_hypermem/prompts/` 路径下，并且每个 prompt 使用一个独立 `.md` 文件管理。不要把长 prompt 硬编码在 Python 文件里。

第一版推荐只保留一个默认写入抽取 prompt：

```text
c_hypermem/prompts/
  extraction/
    memory_extraction.md
  retrieval/
    query_analysis.md
  maintenance/
    fact_merge.md
    contradiction_check.md
    edge_merge.md
    edge_cluster_merge.md
    edge_conflict_check.md
```

这些 prompt 的作用：

- `extraction/memory_extraction.md`：默认写入抽取 prompt。对新增交互调用一次，输出实体、事件、断言和来源片段。
- `maintenance/fact_merge.md`：只有新旧事实高度相似且系统无法确定是否合并时调用。
- `maintenance/contradiction_check.md`：只有同一实体/属性下出现候选冲突时调用。
- `maintenance/edge_merge.md`：只有候选 HyperEdge 与已有 HyperEdge 高度相似且系统无法确定是否重复时调用。
- `maintenance/edge_cluster_merge.md`：只有多个 EdgeCluster 描述高度相似且系统无法确定是否合并 cluster 时调用。
- `maintenance/edge_conflict_check.md`：只有相关 HyperEdge 可能互相冲突时调用，用于标记 cluster conflict state。
- `retrieval/query_analysis.md`：检索阶段按需分析 query，不参与写入抽取。

默认不要让同一段上下文反复经过多个 prompt 做不同颗粒度抽取。推荐“一次语义抽取，系统组装”：

```text
AgentInteraction
  -> memory_extraction.md 只调用一次
  -> 得到 entities / events / assertions / sources
  -> GraphAssembler 只负责编排写入组装顺序
  -> EntityResolver / NodeBuilder / LocalGraphBuilder / HyperEdgeBuilder / EdgeClusterBuilder / GraphMaintenance 完成具体职责
```

`GraphAssembler` 应保持“编排器”边界：它可以决定实体解析、节点构建、局部图构建、基础边构建、cluster 构建和维护逻辑的调用顺序，但不应重新内联这些实现细节。后续新增维护策略或构建策略时，应优先扩展对应组件，而不是让 `GraphAssembler` 再次膨胀成实体解析、节点构建、局部图构建和维护逻辑的混合体。

`node_labels` 配置需要参与 prompt 渲染。调用 `memory_extraction.md` 前，系统应把当前配置中的启用标签和 `description` 作为 prompt context 传入，让 LLM 知道当前更希望抽取哪些语义标签，以及每类标签应该如何判断。

但 `node_labels` 不是严格白名单。如果 LLM 抽取出了配置之外的标签，系统不需要拦截或丢弃；应正常按统一 `MemoryNode` schema 入库，并使用默认索引、local graph 和时间策略处理。这样可以保留真实 agent 场景中的新型记忆对象，后续再通过配置补充专门策略。

### 3.1 Prompt 拼接顺序与 KV Cache

`memory_extraction.md` 渲染时应把稳定内容放在 prompt 上方，把每轮变化的抽取上下文放在下方。这样在支持前缀缓存 / KV cache 的模型或服务中，稳定前缀更容易复用。

推荐拼接顺序：

```text
1. Stable extraction instruction
   - 任务目标
   - 不要输出系统 ID、权重、置信度
   - 不要提及超图 / HyperEdge 等内部结构

2. Stable output schema
   - entities / events / assertions / sources
   - JSON 格式要求

3. Stable or slowly changing label guide
   - 从 node_labels 配置渲染出的 label name
   - 每个 label 的 description
   - unknown label policy

4. Stable extraction policy
   - 单一事实不要重复写入 facts / attributes / triples
   - 使用 assertions 承载关系性事实
   - source_ref 的使用规则

5. Dynamic extraction context
   - 本轮 user_input
   - assistant_output
   - metadata
   - tool_calls / tool_results / observations
   - 增量构建时的新增消息片段
```

其中第 1 到第 4 部分应尽量稳定，并纳入 `prompt_template_hash`。第 5 部分是动态输入，不应放在 prompt 顶部。若 `node_labels` 配置变化，稳定前缀会变化，缓存自然失效。

如果后续发现单个 `memory_extraction.md` 太大，再拆分 prompt；拆分后也应尽量传入第一次抽取后的结构化候选，而不是重复传入完整上下文。

每个 prompt 文件建议使用 markdown front matter 记录元信息：

```markdown
---
id: extraction.memory
version: 0.1.0
owner: c_hypermem
inputs:
  - agent_interaction
  - metadata
outputs:
  - entities
  - events
  - assertions
  - sources
---

# Task

Extract concise memory candidates from the interaction.

# Output Schema

...
```

代码侧只通过 prompt id 或相对路径加载 prompt：

```python
prompt = prompt_registry.load("extraction.memory")
```

Prompt 管理要求：

- prompt 文件是算法包的一部分，应随 `c_hypermem` 一起发布。
- prompt 修改必须改变 prompt hash，并参与 `prompt_template_hash`。
- 缓存策略中的 `prompt_template_hash` 应由启用的 prompt 文件内容和版本共同计算。
- prompt 文件可以包含输出 schema，但不应要求模型生成系统主键。
- Python 中只保留 prompt loader、template renderer 和 schema validator，不保存长 prompt 文本。

### 3.2 LLM-facing Prompt 原则

给模型的任务描述应使用自然语言信息抽取语义，而不是让模型“构建超图”或“构建高阶边”。

推荐：

```text
Extract entities, events, assertions, and source snippets from the text.
```

避免：

```text
Build a hypergraph with hyperedges, node weights, and graph structure.
```

原因：

- “超图”“高阶边”等概念对模型来说是抽象实现细节，容易被不同模型理解成不同结构。
- 让模型生成图结构、权重或置信度会增加字段复杂度和 hallucination 风险。
- C-HyperMem 的结构应由系统根据候选语义单元组装。

LLM 输出字段应保持短小。不要输出：

- `node_id`
- `edge_id`
- `entity_id` / external entity id
- `triple_id`
- `confidence`
- `salience`
- `weight`
- 外层图结构

推荐最小 JSON：

```json
{
  "entities": [
    {"name": "Alice", "labels": ["person"], "aliases": []},
    {"name": "morning interviews", "labels": ["schedule_preference"], "aliases": []}
  ],
  "events": [
    {
      "summary": "Alice discussed interview scheduling.",
      "time": "2024-01-03",
      "participants": [
        {"name": "Alice", "role": "speaker"}
      ]
    }
  ],
  "assertions": [
    {
      "subject": "Alice",
      "predicate": "prefers",
      "object": "morning interviews",
      "source_ref": "assistant_output"
    }
  ],
  "sources": [
    {"text": "Alice prefers morning interviews.", "ref": "assistant_output"}
  ]
}
```

系统后处理负责：

- 生成所有 canonical id。
- 对实体做别名对齐。
- 合并重复实体和事实。
- 检查冲突事实。
- 将 `assertions` 转成 MemoryNode、LocalNodeGraph triple 和 property index。
- 构建 `MemoryNodes`、`LocalNodeGraphs` 和 `HyperEdges`。
- 计算节点/边权重、抽取次数、访问次数、时间衰减等指标。

## 4. 对外 API

C-HyperMem 对外只暴露稳定主入口：

```python
from c_hypermem import Memory

memory = Memory.from_config("configs/default.yaml")
memory.reset(namespace="sample_001")

results = memory.search(
    "What does Alice prefer?",
    namespace="sample_001",
    top_k=10,
)

memory.add_memory(
    user_input={"role": "user", "content": "..."},
    assistant_output={"role": "assistant", "content": "..."},
    namespace="sample_001",
    metadata={"session_id": "S1", "date": "2024-01-03"},
)

stats = memory.stats(namespace="sample_001")
```

这个 API 必须是通用 memory API，不出现 `MemorySession`、`MemoryItem`、LongMemEval、LOCOMO、suite、runner 等评测框架概念。

### 4.1 Runtime 时序

C-HyperMem 应支持典型 agent 运行时序：

```text
Before answer:
  retrieved = memory.search(current_question, namespace, top_k)
  reader_prompt = build_reader_prompt(current_question, retrieved)

After answer:
  memory.add_memory(user_input, assistant_output, namespace, metadata)
```

读取记忆发生在回答前，写入记忆发生在回答后。这样避免模型在回答当前问题前把尚未生成的 assistant output 写入记忆。

对于真实 agent，`After answer` 阶段可能还包含 tool calls、tool results、observations、attachments、trace 和运行状态。因此 `add_memory` 不应只接受简单 QA 文本，而应能兼容更完整的交互事件。

### 4.2 Memory 类职责

`Memory` 是唯一推荐入口：

```python
class Memory:
    @classmethod
    def from_config(cls, config: str | dict) -> "Memory": ...

    def reset(self, namespace: str) -> None: ...

    def add_memory(
        self,
        user_input: str | dict | None = None,
        assistant_output: str | dict | None = None,
        namespace: str = "default",
        metadata: dict | None = None,
        tool_calls: list[dict] | None = None,
        tool_results: list[dict] | None = None,
        observations: list[dict] | None = None,
        attachments: list[dict] | None = None,
        trace: dict | None = None,
    ) -> None: ...

    def add(
        self,
        messages: str | list[dict],
        namespace: str,
        metadata: dict | None = None,
    ) -> None: ...

    def search(
        self,
        query: str,
        namespace: str,
        top_k: int = 10,
        metadata: dict | None = None,
    ) -> list[dict]: ...

    def stats(self, namespace: str) -> dict: ...

    def close(self) -> None: ...
```

`add(...)` 可以作为低层或兼容接口保留，用于批量导入历史对话、benchmark session 或迁移数据；`add_memory(...)` 是面向真实 agent 交互的推荐入口。

### 4.3 AgentInteraction 输入模型

`add_memory(...)` 内部应规范化为一个通用事件对象：

```python
{
    "type": "agent_interaction",
    "user_input": {
        "role": "user",
        "content": "...",
        "timestamp": "...",
        "metadata": {}
    },
    "assistant_output": {
        "role": "assistant",
        "content": "...",
        "timestamp": "...",
        "metadata": {}
    },
    "tool_calls": [
        {
            "id": "call_001",
            "name": "search_web",
            "arguments": {"query": "..."},
            "timestamp": "..."
        }
    ],
    "tool_results": [
        {
            "tool_call_id": "call_001",
            "content": "...",
            "status": "success",
            "timestamp": "..."
        }
    ],
    "observations": [
        {"type": "environment", "content": "...", "timestamp": "..."}
    ],
    "attachments": [
        {"type": "file|image|url", "uri": "...", "metadata": {}}
    ],
    "trace": {
        "run_id": "...",
        "model": "...",
        "latency_ms": 1234
    },
    "metadata": {
        "conversation_id": "...",
        "session_id": "...",
        "date": "..."
    }
}
```

未来兼容原则：

- `user_input` 和 `assistant_output` 是常见最小输入。
- tool 信息不一定进入长期事实，但应允许作为 provenance 或 event context。
- `trace` 默认只用于 debug/provenance，不应直接变成用户画像事实。
- `metadata` 可以保存外部系统字段，但核心 schema 不依赖特定 benchmark。

## 5. Schema 与 ID 策略

### 5.1 系统生成 ID

节点、超边、实体、局部三元组的 canonical id 必须由 C-HyperMem 系统生成，不应由模型控制生成。

推荐由 `utils/ids.py` 提供统一 ID 工具：

```python
make_node_id(namespace, fingerprint) -> str
make_fingerprint(canonical_text, disambiguation_hint=None) -> str
make_edge_id(namespace, edge_fingerprint) -> str
make_cluster_id(namespace, cluster_fingerprint) -> str
make_triple_id(namespace, owner_node_id, subject, predicate, object, qualifiers=None) -> str
```

ID 可以采用确定性 hash：

```text
fingerprint:{hash(normalized_canonical_text + disambiguation_hint)}
node:{hash(namespace + fingerprint)}
edge:{hash(namespace + edge_fingerprint)}
cluster:{hash(namespace + cluster_fingerprint)}
triple:{hash(namespace + owner_node_id + normalized_spo + qualifiers)}
```

`node_labels` 不参与 `node_id`。同一个对象在不同上下文中可能带有 entity、fact、event participant、tool reference 等多个标签，但如果 canonical fingerprint 对齐，应尽量复用同一个共享节点。

也可以在需要保留插入顺序时使用 ULID/UUIDv7，但应由系统生成，并保存 `fingerprint` / `dedupe_key` 支持合并。

### 5.2 统一节点身份与 node_labels

C-HyperMem 内部只维护一个统一 `MemoryNode` schema。`fact`、`entity`、`event`、`tool` 等都只是 `node_labels`，不是不同存储表、不同 ID 策略或不同内部三元组结构。

节点身份由统一 canonical fingerprint 决定：

```text
canonical_text = normalize extracted memory object
fingerprint = hash(normalized_canonical_text + disambiguation_hint)
node_id = hash(namespace + fingerprint)
```

其中 `disambiguation_hint` 用来区分同名不同对象，例如 owner、conversation scope、source cluster 或实体消歧结果。它不是 `node_labels`，也不应把 fact/entity/event/tool 这类标签放进 `node_id`。

推荐配置：

```yaml
node_identity:
  strategy: canonical_fingerprint
  include_namespace: true
  include_node_labels: false
  disambiguation:
    enabled: true
    hint_sources: [aliases, local_graph, source_scope, metadata]

node_labels:
  entity:
    enabled: true
    description: "Named or referential objects that can be reused across memories, such as people, organizations, places, projects, products, files, tools, and other referents that may appear again."
    alias_resolution: true
    local_graph:
      enabled: true
      allow_triples: true
      allow_attributes: true
      allow_roles: true
    indexing:
      lexical: true
      vector: true
      alias_index: true

  fact:
    enabled: true
    description: "Atomic assertions or claims that may be queried, updated, contradicted, or supported by evidence, such as preferences, states, relationships, decisions, plans, or outcomes. Do not duplicate the same claim as both an attribute and a triple."
    property_index: true
    local_graph:
      enabled: true
      allow_triples: true
      allow_attributes: true
      allow_roles: true
    indexing:
      lexical: true
      vector: true

  event:
    enabled: true
    description: "Time-bound interactions, actions, observations, or episodes in a conversation, real-world timeline, tool run, meeting, task step, or observation. Include participants and roles when available."
    time:
      prefer_world_time: true
    local_graph:
      enabled: true
      allow_triples: true
      allow_attributes: true
      allow_roles: true
    indexing:
      lexical: true
      vector: true

  instruction:
    enabled: true
    description: "User-provided rules, constraints, or behavioral requirements that the Agent should follow across turns. Current version treats this as a normal node label; future retrieval policy may prioritize it or place it near the top of the System Prompt."
    local_graph:
      enabled: true
      allow_triples: true
      allow_attributes: true
      allow_roles: true
    indexing:
      lexical: true
      vector: true

  tool:
    enabled: false
    description: "Tool calls, tool results, observations, or external execution artifacts from a real agent run, including tool call inputs, outputs, status, returned evidence, or environment observations."
    time:
      prefer_world_time: true
    local_graph:
      enabled: true
      allow_triples: true
      allow_attributes: true
      allow_roles: true
    indexing:
      lexical: true
      vector: false
```

设计原则：

- 新增 `tool`、`observation`、`attachment`、`trace` 标签时优先扩展配置，不修改核心节点 schema。
- 所有节点共享同一个 `LocalNodeGraph` 结构：`triples`、`attributes`、`roles`、`qualifiers`。
- 不同标签的差异通过索引策略、时间要求、alias/property index 开关表达，不通过 ID 字段列表表达。
- LLM 可以输出候选标签，但系统负责规范化、追加和合并 `node_labels`。
- `node_labels` 会作为抽取偏好传入 prompt，但不是入库限制；未知标签正常写入 `MemoryNode`，并使用默认 fallback 策略。

### 5.3 实体别名对齐先于 ID 生成

实体标签节点复用前必须先做轻量级 entity resolution。系统不应在模型抽取到新实体名称后立刻 hash 创建新节点。

在统一 `MemoryNode` schema 下，实体不需要独立于节点的主键。`entity_alias_index` 应把别名映射到共享 `node_id`。如果未来需要区分外部实体 ID 和内部节点 ID，再通过 `attributes.external_entity_id` 扩展。

推荐流程：

```text
candidate entity name from LLM
  -> normalize name
  -> search existing MemoryNode pool by alias / normalized text / canonical fingerprint
  -> if exact / alias / normalized match:
       reuse existing node_id
       append "entity" to node_labels if needed
       optionally append new alias/source
     else:
       generate fingerprint from canonical_name + disambiguation_hint
       create new MemoryNode with node_labels += ["entity"]
```

第一版可以只做轻量字符串匹配：

- `canonical_name` 精确匹配。
- `display_name` 规范化后匹配。
- `aliases` 规范化后匹配。
- 同一 conversation / namespace 下的大小写、空格、标点归一。
- 可选加入 `entity_type` 约束，避免同名不同类型实体误合并。

建议维护实体别名索引：

```text
entity_alias_index
  namespace
  normalized_alias
  entity_type
  node_id
```

伪代码：

```python
def resolve_entity_node_id(namespace, name, entity_type=None, aliases=None):
    normalized_names = normalize_aliases([name, *(aliases or [])])
    candidates = entity_alias_index.lookup(namespace, normalized_names, entity_type)
    if candidates:
        return choose_best_candidate(candidates)

    canonical_name = choose_canonical_name(name, aliases)
    fingerprint = make_fingerprint(canonical_name, disambiguation_hint={"entity_type": entity_type})
    node_id = make_node_id(namespace, fingerprint)
    create_memory_node(
        node_id=node_id,
        canonical_text=canonical_name,
        fingerprint=fingerprint,
        node_labels=["entity"],
        attributes={
            "canonical_name": canonical_name,
            "entity_type": entity_type,
            "aliases": aliases or [],
        },
    )
    return node_id
```

### 5.4 HyperEdge

`HyperEdge` 是一条具体高阶关系实例。它保存成员节点、成员角色、关系、来源、时间和状态。HyperEdge 应保守维护，不因为成员子集、近子集或描述相似就直接合并。

```python
{
    "edge_id": "edge:...",
    "edge_fingerprint": "sha256:...",
    "edge_type": "evidence|state|temporal|correction|aggregation|task",
    "relation": "describes_entity_state",
    "description": "Andrew's pet profile around Toby.",
    "polarity": "positive|negative|neutral|unknown",
    "member_policy": "immutable|appendable|versioned",
    "member_signature": "sha256:...",
    "member_version": 3,
    "node_ids": ["entity:andrew", "fact:andrew_has_pet_toby", "event:andrew_mentions_toby"],
    "roles": {
        "entity:andrew": "subject",
        "fact:andrew_has_pet_toby": "state_fact",
        "event:andrew_mentions_toby": "evidence"
    }
}
```

`edge_type` 是普通语义类型。第一版可以支持少量通用类型：

- `evidence`：连接来源 turn / event / fact / tool result。
- `state`：连接实体、属性事实和状态节点。
- `temporal`：连接同一时间桶或时间段内的 event / fact。
- `correction`：连接新旧冲突事实，记录 invalidates / supersedes。
- `aggregation`：连接语义上长期相关的一组节点。
- `task`：连接目标、计划、动作、结果和状态变化。

这些类型可以逐步扩展，但不需要让用户或 LLM 维护固定分类表。

### 5.5 EdgeCluster

`EdgeCluster` 是多条相关 HyperEdge 的聚合对象。它不是固定 view，也不是把多条边物理合并成一条边，而是把可能相关、近似重复、互相补充或互相冲突的 HyperEdge 放进同一个工作集。

```python
{
    "cluster_id": "cluster:...",
    "cluster_fingerprint": "sha256:...",
    "canonical_description": "Toby's species and pet status.",
    "cluster_labels": ["entity_state", "pet_profile"],
    "aliases": ["toby_species", "toby_pet_profile"],
    "conflict_state": "none|contains_conflict|needs_review"
}
```

HyperEdge 与 EdgeCluster 的关系单独保存：

```text
supports
elaborates
updates
contradicts
duplicate_candidate
```

这样冲突边不会被压成一条边，但检索时仍然可以被一起召回。

### 5.6 统一超边 ID 策略

超边 ID 建议统一为“稳定边实例 ID”，不要从当前成员集合计算。成员集合是边的状态，可能追加、修正或退役；如果 `edge_id` 依赖 `sorted(member_ids)`，任何成员变化都会导致 ID 变化，进而丢失访问次数、生命周期时间、历史 metadata 和调试链路。

统一规则：

```text
edge_id = hash(namespace + edge_fingerprint)
```

`edge_fingerprint` 可以由 normalized relation、polarity、roles、members、source hint、time hint 等生成，但成员集合只用于签名和版本，不单独决定合并：

```text
member_signature = hash(sorted(member_ids + roles))
member_version = member_version + 1 when membership changes
```

边更新时：

```text
edge_id 不变
hyper_edge_members 增删成员
hyper_edges.member_signature 更新
hyper_edges.member_version 增加
hyper_edges.updated_at 更新
edge access_count / historical metadata 保留
```

插入新 HyperEdge 前可以做轻量检索：

```text
candidate HyperEdge
  -> normalize description / relation / roles / member ids
  -> retrieve existing HyperEdges by text, alias, member overlap, relation, roles, source, time
  -> if clearly duplicate:
       reuse or merge HyperEdge
     else:
       keep as a new HyperEdge
  -> retrieve or create related EdgeCluster
```

成员子集、近子集和高重叠率只能作为召回信号，不能单独决定合并。只有在 relation、polarity、roles、source scope 和时间都兼容，且没有冲突信号时，才允许合并 HyperEdge。否则应保留多条 HyperEdge，并通过 EdgeCluster 建立关系。

`member_policy` 只控制成员是否允许变化，不影响 ID 策略：

```text
immutable:
  一般不追加成员；如果发现抽取错误，可创建新 member_version 或记录 correction

appendable:
  可持续追加成员，例如长期主题、计划、画像、证据集合

versioned:
  成员变化需要保留历史版本，适合状态变化敏感的关系
```

### 5.7 MemoryNode

推荐节点 schema：

```python
{
    "namespace": "sample_001",
    "node_id": "node:...",
    "canonical_text": "Alice prefers morning interviews.",
    "normalized_text": "alice prefers morning interviews",
    "fingerprint": "sha256:...",
    "node_labels": ["fact"],
    "status": "active|retired|invalidated|uncertain",
    "content": "Alice prefers morning interviews.",
    "summary": "Alice prefers morning interviews.",
    "attributes": {},
    "absolute_time": {},
    "relative_time": {},
    "local_graph": {},
    "metadata": {}
}
```

`node_labels` 的允许值、默认字段、索引策略和局部图谱开关来自配置。核心存储层不需要知道 fact、entity、event 的专门内部结构；它只保存统一的 `MemoryNode`。

未来真实 agent 场景可以增加：

```text
tool
observation
attachment
trace
environment
```

这些标签仍然复用同一个 `MemoryNode` schema 和同一个 `LocalNodeGraph` schema。

状态字段用于增量更新和冲突处理：

```python
{
    "status": "retired",
    "superseded_by": "fact:new_fact_id",
    "invalidated_by": "fact:new_fact_id",
    "status_reason": "newer conflicting fact for the same entity/property",
    "status_updated_at": "2026-05-22T12:00:00"
}
```

### 5.8 LocalNodeGraph

复合节点内部的小型知识结构：

```python
{
    "triples": [
        {
            "subject": "Andrew",
            "predicate": "has_pet",
            "object": "Toby",
            "qualifiers": {
                "valid_time": {"start": "2023-07-11", "end": null},
                "scope_edge_id": "edge:andrew_pet_profile",
                "scope_cluster_id": "cluster:andrew_toby_context",
                "role_in_edge": "pet",
                "edge_relation": "pet_ownership",
                "source_event_id": "event:S1:0",
                "status": "active"
            }
        }
    ],
    "attributes": {
        "entity_type": "person"
    },
    "roles": {
        "Andrew": "owner",
        "Toby": "pet"
    }
}
```

外层 `HyperEdge` 表达多个节点之间的高阶关联；内层 `LocalNodeGraph` 表达节点自身的属性、角色、三元组和局部语义。

局部图谱中的三元组可以携带 `qualifiers.valid_time`，但不建议复制节点的完整生命周期时间。生命周期由 owner node 管理；三元组只保存自身语义成立所需的限定信息。

局部图谱中的三元组也可以携带高阶关系上下文：

```text
scope_edge_id
scope_cluster_id
role_in_edge
edge_relation
```

这些字段用于表达“三元组在哪个 HyperEdge / EdgeCluster 语境中成立”，只作为检索回上下文时的语义隔离、过滤和排序信息，不参与 `MemoryNode.node_id`，也不参与 `triple_id`。例如同一个 Alice 节点可以在同事超边中是 `employee`，在诉讼超边中是 `plaintiff`；检索时可以按 scope 组织上下文，避免边界不清，但不改变节点或三元组的 ID 策略。

`LocalNodeGraph` 不为 fact、entity、event、tool 分别设计不同 schema。差异通过 triple 内容和 `node_labels` 配置表达：

```text
label=fact:
  Alice --prefers--> morning interviews

label=entity:
  Alice --has_preference--> morning interviews

label=event:
  Alice --speaker_in--> interview scheduling discussion

label=tool:
  search_web_call --returned--> result_snippet
```

## 6. 时间模型

C-HyperMem 同时维护绝对时间和相对时间，但它们回答不同问题。

```text
absolute/world time:
  事实或事件在真实世界何时发生、何时有效

relative/activation time:
  记忆在系统内何时创建、插入、更新、访问，以及距离当前轮次多远
```

推荐拆成三组字段：

```python
{
    "world": {
        "event_time": "2023-07-11",
        "valid_time": {"start": "2023-07-11", "end": null, "as_of": "2023-09-01"},
        "source_timestamp": "2023-07-11T10:15:00"
    },
    "lifecycle": {
        "created_at": "2026-05-22T10:00:00",
        "inserted_at": "2026-05-22T10:00:01",
        "updated_at": "2026-05-22T10:12:00",
        "deleted_at": null
    },
    "activation": {
        "created_turn": 17,
        "inserted_turn": 17,
        "updated_turn": 21,
        "last_access_turn": 24,
        "access_count": 3
    }
}
```

挂载策略：

- 节点级时间：保存节点自身的 world / lifecycle / activation 时间。
- 超边级时间：保存这组节点的关联何时形成、何时更新、成员何时变化、是否有真实世界有效期。
- 局部图谱时间：只保存某条 triple / attribute 自身需要的 qualifier，例如 `valid_time`、`source_event_id`、`status`。

自动更新策略：

```text
创建节点:
  写入 created_at / created_turn
  从对话内容或 metadata 抽取 world time

插入存储:
  写入 inserted_at / inserted_turn

更新节点内容、局部图谱或超边成员:
  写入 updated_at / updated_turn
  只有真实世界事实发生变化时才更新 valid_time

检索访问:
  更新 last_access_turn 和 access_count
  不更新 updated_at
```

`turn_distance` 和 `decay_weight` 不建议作为永久权威字段保存，因为它们依赖当前对话轮次。推荐按需计算：

```text
turn_distance = current_turn - inserted_turn
decay_weight = exp(-decay_lambda * turn_distance)
```

如果为了调试或加速需要保存，也应作为 cache，而不是事实本身。

## 7. 写入 Pipeline

`Memory.add_memory()` 的推荐流程：

```text
add_memory(user_input, assistant_output, namespace, metadata, tool_calls, ...)
  1. normalize AgentInteraction
  2. write raw interaction messages to the separate turns table
  3. fetch recent turns as Context and mark the current interaction as Target
  4. call memory_extraction.md once with Context/Target input
  5. normalize extracted candidates
  6. resolve entity aliases against existing MemoryNode pool
  7. retrieve existing assertions/triples with same subject_node_id + property key
  8. detect duplicate / update / conflict
  9. call maintenance prompts only when ambiguous
  10. retire or invalidate old assertion nodes when needed
  11. generate system-controlled ids for unresolved nodes / edges / triples
  12. build or reuse MemoryNode[] according to canonical fingerprint
  13. append extracted semantic labels to node_labels
  14. build LocalNodeGraph from assertions and event participants using one shared schema
  15. build conservative HyperEdges from candidates and existing graph context
  16. resolve related EdgeClusters without forcing HyperEdge merge
  17. write MemoryNodes
  18. write HyperEdges, HyperEdge members, EdgeClusters and cluster members
  19. update lexical / vector indexes
```

这里的第 15 步不是固定规则表投影，而是系统内部的 graph assembly。第一版可以先构建最基础的 evidence / state / correction 边，再逐步加入更复杂的语义聚合。成员重叠只用于召回候选 EdgeCluster，不直接决定 HyperEdge 合并。

写入链路必须把交互日志和知识图谱分离：原始 user/assistant/tool/observation 消息进入 `turns` 表，用于微型滑动窗口和审计；`nodes`、`hyper_edges`、`edge_clusters` 只保存抽取后的结构化图谱对象。当前 Target 对应的 `turn_id` 应写入组装 metadata，并作为 `source_turn_ids` 落到相关 Node / Edge / Cluster 的 metadata 中，便于图数据溯源。

### 7.1 输入规范化

外部调用方可以传入 `add_memory(user_input, assistant_output, ...)`，也可以通过 `add(messages, ...)` 批量导入历史对话。C-HyperMem 内部统一成 `AgentInteraction` 或 `MemoryImportBatch`。

`MemoryImportBatch`：

```python
{
    "type": "memory_import_batch",
    "messages": [
        {"role": "...", "content": "...", "timestamp": "...", "metadata": {...}}
    ],
    "metadata": {
        "session_id": "S1",
        "date": "2024-01-03"
    }
}
```

注意：这里的 adapter 是外部调用方角色，不是 C-HyperMem 核心依赖。C-HyperMem 只接受通用事件或批量导入数据，不感知 `agent_memory_eval` 的 dataclass。

### 7.2 事件驱动的增量抽取（Event-Driven Incremental Extraction）

放弃在应用层维护复杂的 Hash 缓存状态机（如 `prefix_hash`、游标等）。现代大模型 API 底层已原生支持 Prompt Caching，因此应用层应全面拥抱“事件驱动”的天然增量设计。

目标：- 每次交互（`add_memory`）仅对当前最新轮次进行记忆抽取。- 不重复抽取历史轮次的事实，避免 Token 浪费与图谱冗余。- 利用大模型 API 的原生 KV Cache 降低 System Prompt 的反复处理成本。**微型滑动窗口（Sliding Window Context）策略**：

在调用 `memory_extraction.md` 时，不应仅传入当前单句话（会导致严重的代词指代不明），也不应传入全量历史。应构造如下 Payload 传给大模型：

```text
[Stable System Prompt & Extraction Schema]
... (这部分保持绝对稳定，由大模型服务商底层进行 Prompt Caching) ...

[Context: Recent History (最近 2-3 轮，仅供理解语境，不要从中抽取)]
User(N-2): 我明天要去北京出差。
Assistant(N-2): 好的，需要帮您看机票吗？
User(N-1): 要的。

[Target to Extract: 当前最新轮次 (仅对这部分进行抽取)]
Assistant(N): 已经为您查询到航班。
User(N): 帮我订早上 8 点那班，另外我是素食主义者，帮我备注一下航空餐。
```

通过明确区分 [Context] 和 [Target]，模型既能完成代词消解（知道“早上 8 点”是去北京的航班），又严格只输出第 N 轮新增的记忆对象。

Context 不应来自 `nodes` 表，也不应把原始聊天流水账建模为 MemoryNode。系统应维护独立的 `turns` / interaction log 表：写入 Target 前先把当前交互保存为 turn log，抽取时从该表读取最近 K 条消息作为 Context，并将当前 Target 的 `turn_id` 注入写入 metadata。这样既能重启后继续构造滑动窗口，又能保证知识图谱只保存结构化记忆对象。

### 7.3 批量导入与长会话的分块处理

在处理 agent_memory_eval 传入的完整历史 MemorySession（即调用 add(messages) 批量导入）时，系统不能将几十轮对话一次性作为 Target 送入 LLM（会导致严重的 Attention 衰减和信息遗漏）。

分块滑动抽取（Chunked Sliding Extraction）：

将长会话按轮次（例如每 4 轮为一个 Chunk）切片，模拟在线流式交互的顺序，依次调用增量抽取。

```text
Chunk 1: Extract(Target=Turns 1-4, Context=None)
Chunk 2: Extract(Target=Turns 5-8, Context=Turns 3-4)
...
```


这种方式在工程实现上极为简单一致，无论是实时单次对话，还是历史数据灌库，底层都复用同一套微型滑动窗口抽取逻辑。

### 7.4 增量抽取下的图谱更新风险

增量抽取意味着我们“只读新消息”，但这绝不等于“图谱只进行 Append-only 的追加写入”。新消息极有可能改变旧事实的解释、有效期或实体消歧结果。

例如用户纠正了旧信息：


```text
Turn 10 抽取 (旧事实): [Toby, is_a, dog]
Turn 50 抽取 (新事实): [Toby, is_a, cat]
```

如果系统只管追加，图谱中将同时存在两条矛盾事实。因此，写入阶段必须包含确定性的图谱维护逻辑：

事实防重与退役：系统必须在写入新 assertion 前，构造 property_key 检索旧事实。若发现冲突，不物理覆盖旧节点，而是将旧 fact 标记为 retired / invalidated，保留新 fact，并生成 correction 类型的 HyperEdge。

局部图谱更新：新事实补充旧事件时，可以更新旧 event 节点的 local_graph。

动态聚类：新语义关联形成后，可以将相关 HyperEdge 挂载到现有的 EdgeCluster 中。

结论：抽取是增量的（减少 LLM 的重复阅读），但图谱维护是全局的（确保认知的一致性和最新状态）。


### 7.5 冲突事实退役策略

对于增量写入，系统必须在写入新事实前检索旧事实和局部三元组，尤其是同一实体、同一属性或同一谓词的记录。

示例：

```text
旧事实:
  [Toby, is_a, dog]

新消息:
  "Toby my cat"

新事实:
  [Toby, is_a, cat]
```

如果系统只处理新增消息并直接写入，就会同时保留 `[Toby, is_a, dog]` 和 `[Toby, is_a, cat]` 两条矛盾事实。因此写入前需要构造 property key：

```text
property_key = subject_node_id + ":" + normalized_predicate_or_attribute
```

然后检索：

```text
existing facts where property_key = "node:toby:is_a"
existing triples where subject_node_id = "node:toby" and predicate = "is_a"
```

推荐决策：

```text
same value:
  merge source / extraction_count，不创建重复事实

compatible value:
  append as additional fact，例如 aliases、多个爱好、多个参与者

conflicting value:
  create or reuse MemoryNode and append label = fact
  mark old fact-labeled MemoryNode status = retired 或 invalidated
  set old.valid_time.end if new fact has effective time
  create correction HyperEdge: new_fact invalidates old_fact
  update LocalNodeGraph: old triple retired, new triple active
```

不建议物理覆盖旧节点，原因：

- 保留历史可追溯性。
- 支持 temporal / as-of 问题。
- 保留原始来源和调试证据。
- 避免误判冲突时不可恢复。

LLM 可以参与“是否矛盾”的二选一判断，但系统必须提供候选旧事实；不能只让 LLM 看新增上下文就直接写库。

### 7.6 第一版降级策略

为尽快跑通评测，M1 可以采用保守实现：

- 一个 session 或 import batch 生成或复用带 `event` 标签的 `MemoryNode`。
- 每个 event 抽取若干带 `fact` 标签的 `MemoryNode`。
- 抽取实体并做轻量 alias resolution，生成或复用带 `entity` 标签的 `MemoryNode`。
- 构建基础 `evidence` 超边，连接 turn / event / fact。
- 构建基础 `state` 超边，连接 entity / fact / state。
- 构建基础 `correction` 超边，连接新旧冲突事实。
- 暂缓复杂语义聚合，避免一开始引入固定分类表。

## 8. 检索 Pipeline

`Memory.search()` 返回可进入 reader prompt 的结果。检索不是本文最核心部分；详细算法设计放在 `retrieval_design.md`，真实实现状态放在 `current_implementation.md`。本节只保留架构级 pipeline。

带有 `instruction` 标签的节点当前与普通节点平级处理。未来可以增加独立策略：在任意 query 下优先召回，或常驻拼接到 reader System Prompt 顶部，用于保存用户对 Agent 的长期强制规则。这个优先级策略暂不作为 M1 强制要求。

```text
search(query, namespace, top_k)
  1. optional query analysis
  2. lexical + vector recall over MemoryNodes
  3. node-level fusion
  4. graph ripple through incident HyperEdges and EdgeClusters
  5. edge coherence scoring
  6. edge-level ranking
  7. return top-k HyperEdges with member nodes
```

### 8.1 检索结果内容

最终结果应围绕 HyperEdge 组织，而不是只返回孤立节点。`content` 应保持简洁，优先返回关系边描述、成员节点和必要来源：

```text
[state] Alice prefers morning interviews
Nodes:
- Alice
- Alice prefers morning interviews.
```

多证据问题可以返回聚合证据：

```text
[Evidence: Nate's tournament wins | edge_type=aggregation]
- 2022-01-21: Nate won his first video game tournament.
- 2022-05-02: Nate won his second tournament.
```

### 8.2 评分组成

评分应保持可解释。当前架构预留以下分量：

```text
score =
  node_fusion_score
+ edge_coherence
+ optional temporal / recency / rerank signals
```

其中：

- `node_fusion_score` 来自 lexical / vector 等 node recall 通道融合。
- `edge_coherence` 来自同一超边中多个候选节点的共同命中。
- `temporal / recency / rerank` 属于后续可插拔策略，当前不应和基础召回耦合。

## 9. 存储设计

第一版推荐 SQLite + 本地向量索引，简单、可 debug、便于 namespace 隔离。

### 9.1 SQLite 表

建议逻辑表：

```text
turns
  namespace
  turn_id
  turn_index
  message_index
  role
  content
  timestamp
  message_metadata_json
  turn_metadata_json
  inserted_at

nodes
  namespace
  node_id
  canonical_text
  normalized_text
  fingerprint
  node_labels_json
  status
  superseded_by
  invalidated_by
  content
  summary
  attributes_json
  absolute_time_json
  relative_time_json
  local_graph_json
  metadata_json

hyper_edges
  namespace
  edge_id
  edge_fingerprint
  edge_type
  relation
  description
  polarity
  status
  member_policy
  member_signature
  member_version
  absolute_time_json
  relative_time_json
  metadata_json

hyper_edge_members
  namespace
  edge_id
  node_id
  role
  weight

edge_clusters
  namespace
  cluster_id
  cluster_fingerprint
  canonical_description
  cluster_labels_json
  aliases_json
  conflict_state
  status
  metadata_json

edge_cluster_members
  namespace
  cluster_id
  edge_id
  relation_to_cluster
  status
  metadata_json

triples
  namespace
  triple_id
  owner_node_id
  subject
  predicate
  object
  status
  scope_edge_id
  scope_cluster_id
  role_in_edge
  edge_relation
  superseded_by
  invalidated_by
  qualifiers_json
  metadata_json

fact_property_index
  namespace
  property_key
  subject_node_id
  predicate
  fact_node_id
  status
  updated_at

entity_alias_index
  namespace
  normalized_alias
  entity_type
  node_id
  source_count
  updated_at
```

`turns` 是原始交互日志表，用于 Context 滑动窗口、调试和审计；它不是知识图谱节点表。`nodes`、`hyper_edges`、`edge_clusters` 等表只保存抽取和组装后的结构化记忆对象。应用层不再维护 `ingestion_cache` / `prefix_hash` / cursor 状态机，增量边界由当前 Target 和持久化 turn log 明确表达。

### 9.2 索引对象

- Lexical index：BM25 或 SQLite FTS。
- Vector index：Qdrant local mode 作为默认实现；SQLite 仍是 canonical graph store，Qdrant 是可重建的旁路向量索引。
- HyperEdge index：`node_id -> incident edge_ids`。
- EdgeCluster index：`edge_id -> cluster_ids`、`node_id -> related cluster_ids`。
- Entity alias index：`normalized_alias -> node_id`。
- Fact property index：`subject_node_id + predicate -> active fact node_ids`。

每个 namespace 必须隔离，避免 benchmark 样本之间信息泄漏。

## 10. 配置草案

在评测仓库新增：

```text
configs/memory/c_hypermem.yaml
```

示例：

```yaml
backend: c_hypermem
package_path: C-HyperMem
storage_path: runs/vectorstores/c_hypermem
default_top_k: 10

ingestion:
  event_mode: session
  pass_recent_context: true
  context_window_messages: 3
  max_facts_per_event: 12

extraction:
  prompt: extraction/memory_extraction.md
  output_schema: minimal_memory_candidates
  forbid_model_ids: true
  forbid_confidence: true
  pass_node_labels_to_prompt: true
  allow_unknown_node_labels: true

node_identity:
  strategy: canonical_fingerprint
  include_namespace: true
  include_node_labels: false
  disambiguation:
    enabled: true
    hint_sources: [aliases, local_graph, source_scope, metadata]

node_labels:
  entity:
    enabled: true
    description: "Named or referential objects that can be reused across memories, such as people, organizations, places, projects, products, files, tools, and other referents that may appear again."
    alias_resolution: true
    local_graph: {enabled: true, allow_triples: true, allow_attributes: true, allow_roles: true}
    indexing: {lexical: true, vector: true, alias_index: true}
  fact:
    enabled: true
    description: "Atomic assertions or claims that may be queried, updated, contradicted, or supported by evidence, such as preferences, states, relationships, decisions, plans, or outcomes. Do not duplicate the same claim as both an attribute and a triple."
    property_index: true
    local_graph: {enabled: true, allow_triples: true, allow_attributes: true, allow_roles: true}
    indexing: {lexical: true, vector: true}
  event:
    enabled: true
    description: "Time-bound interactions, actions, observations, or episodes in a conversation, real-world timeline, tool run, meeting, task step, or observation. Include participants and roles when available."
    time: {prefer_world_time: true}
    local_graph: {enabled: true, allow_triples: true, allow_attributes: true, allow_roles: true}
    indexing: {lexical: true, vector: true, time_index: true}
  instruction:
    enabled: true
    description: "User-provided rules, constraints, or behavioral requirements that the Agent should follow across turns. Current version treats this as a normal node label; future retrieval policy may prioritize it or place it near the top of the System Prompt."
    local_graph: {enabled: true, allow_triples: true, allow_attributes: true, allow_roles: true}
    indexing: {lexical: true, vector: true}
  tool:
    enabled: false
    description: "Tool calls, tool results, observations, or external execution artifacts from a real agent run, including tool call inputs, outputs, status, returned evidence, or environment observations."
    time: {prefer_world_time: true}
    local_graph: {enabled: true, allow_triples: true, allow_attributes: true, allow_roles: true}
    indexing: {lexical: true, vector: false}

hyperedges:
  enabled: true
  build_from_extraction: true
  merge_policy: conservative
  member_policy_default: appendable
  basic_edge_types:
    - evidence
    - state
    - correction
  resolution:
    use_member_overlap_as_recall_signal: true
    require_relation_role_polarity_compatibility_for_merge: true

edge_clusters:
  enabled: true
  create_from_related_hyperedges: true
  allow_conflict_clusters: true
  maintenance_prompts:
    edge_merge: maintenance/edge_merge.md
    edge_cluster_merge: maintenance/edge_cluster_merge.md
    edge_conflict_check: maintenance/edge_conflict_check.md

local_graph:
  enabled: true
  schema: uniform
  configured_by_node_labels: true

time:
  relative_decay:
    enabled: true
    unit: turn
    decay_lambda: 0.03
    access_boost: 0.05

index:
  lexical: sqlite_fts
  vector: qdrant
  use_embedding: true
  vector_store:
    backend: qdrant
    path: runs/c_hypermem/vector_index
    collection_name: c_hypermem_memory

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
  cluster_periphery_edge_limit: 20
  cluster_periphery_node_limit: 50
  edge_coherence_alpha: 0.5
  edge_coherence_beta: 2.0
  final_top_k: 10

extraction_llm:
  model: ${LLM_MODEL}
  api_key_env: LLM_API_KEY
  base_url: ${LLM_BASE_URL}
```

配置原则：

- 不出现 `agent_memory_eval` 内部类名。
- 不以 benchmark 字段作为核心必需项。
- 所有 prompt 路径都相对于 `c_hypermem/prompts/`。

## 11. agent_memory_eval Adapter

新增：

```text
agent_memory_eval/backends/c_hypermem_backend.py
```

该文件属于评测仓库，不属于 C-HyperMem 核心算法包。它可以导入 `c_hypermem.Memory`，但 C-HyperMem 不能导入这个 adapter 或任何 `agent_memory_eval` 模块。

职责：

- 读取 `package_path`，必要时加入 `sys.path`。
- 创建 `c_hypermem.Memory`。
- `reset(sample_id)` 中生成安全 namespace，并调用 `memory.reset(namespace)`。
- `ingest_session(session)` 中转换 `MemorySession` 为 C-HyperMem 输入。
- `retrieve(query, top_k)` 中调用 `memory.search(...)`，再转成 `MemoryItem`。
- `get_debug_info()` 返回 namespace 和 `memory.stats(...)`。
- 不实现 extraction、hyperedge building、local graph building、ranking、storage 等核心算法。
- 不把 `MemorySession` / `MemoryItem` 传入 C-HyperMem 内部，只做边界转换。

说明：`agent_memory_eval.ingest_session()` 是历史会话回放场景，适合调用 `memory.add(messages, ...)` 做批量导入；真实在线 agent 的运行时写入应在回答后调用 `memory.add_memory(user_input, assistant_output, ...)`。

示例骨架：

```python
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

from .base import MemoryBackend
from ..models import MemoryItem, MemorySession


class CHyperMemBackend(MemoryBackend):
    backend_name = "c_hypermem"
    default_top_k = 10

    def __init__(self, config: dict[str, Any], llm_config: dict[str, Any]):
        super().__init__()
        package_path = config.get("package_path")
        if package_path:
            sys.path.insert(0, str(Path(package_path).resolve()))

        from c_hypermem import Memory

        self.config = config
        self.llm_config = llm_config
        self.memory = Memory.from_config(config)
        self.namespace = ""
        self.sample_id: str | None = None

    def reset(self, sample_id: str) -> None:
        super().reset(sample_id)
        self.sample_id = sample_id
        self.namespace = _safe_namespace(f"{self.backend_name}_{sample_id}")
        self.memory.reset(namespace=self.namespace)

    def ingest_session(self, session: MemorySession) -> None:
        messages = [
            {
                "role": turn.role,
                "content": turn.content,
                "timestamp": turn.timestamp,
                "metadata": turn.metadata,
            }
            for turn in session.turns
        ]
        metadata = {
            "session_id": session.session_id,
            "date": session.date,
            **session.metadata,
        }
        self.token_usage.record_build(
            "\n".join(f"{m['role']}: {m['content']}" for m in messages),
            event="c_hypermem.ingest_session",
            metadata={"session_id": session.session_id, "turn_count": len(session.turns)},
        )
        self.memory.add(messages, namespace=self.namespace, metadata=metadata)

    def retrieve(
        self,
        query: str,
        top_k: int | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> list[MemoryItem]:
        k = top_k if top_k is not None else self.default_top_k
        self.token_usage.record_memory_query(
            query,
            event="c_hypermem.retrieve",
            metadata={"top_k": k},
        )
        results = self.memory.search(query, namespace=self.namespace, top_k=k, metadata=metadata)
        return [
            MemoryItem(
                id=str(result.get("id", f"c_hypermem_{idx}")),
                content=str(result.get("content", "")),
                score=_float_or_none(result.get("score")),
                source_session_id=(result.get("metadata") or {}).get("source_session_id"),
                metadata=result,
            )
            for idx, result in enumerate(results)
        ]

    def get_debug_info(self) -> dict[str, Any]:
        return {
            "namespace": self.namespace,
            "sample_id": self.sample_id,
            "stats": self.memory.stats(namespace=self.namespace),
        }

    def close(self) -> None:
        self.memory.close()


def _safe_namespace(value: str) -> str:
    return "".join(ch if ch.isalnum() or ch in {"_", "-"} else "_" for ch in value)


def _float_or_none(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
```

## 12. Factory 注册

在 `agent_memory_eval/backends/factory.py` 添加：

```python
if backend in {"c_hypermem", "c-hypermem"}:
    from .c_hypermem_backend import CHyperMemBackend

    return CHyperMemBackend(config, llm_config)
```

三个名称需要一致：

```text
configs/memory/c_hypermem.yaml -> backend: c_hypermem
configs/suites/*.yaml -> name: c_hypermem
factory.py -> if backend in {"c_hypermem", "c-hypermem"}
```

## 13. Suite 接入

在 suite 中加入：

```yaml
suite:
  backends:
    - name: c_hypermem
      config_path: configs/memory/c_hypermem.yaml
```

优先验证：

```powershell
python -m agent_memory_eval validate configs\suites\longmemeval_smoke.yaml --backend c_hypermem
python -m agent_memory_eval run configs\suites\longmemeval_smoke.yaml --backend c_hypermem --dry-run
python -m agent_memory_eval run configs\suites\longmemeval_smoke.yaml --backend c_hypermem --limit 1 --no-eval
```

再跑 LOCOMO 小样本：

```powershell
python -m agent_memory_eval validate configs\suites\locomo.yaml --backend c_hypermem
python -m agent_memory_eval run configs\suites\locomo.yaml --backend c_hypermem --limit 5
```

## 14. 开发里程碑

本节记录截至当前代码的真实进度。实现状态以 `c_hypermem/`、`tests/` 和 `docs/current_implementation.md` 为准；长期设计仍可继续作为后续路线参考。

### M1：最小可接入（基本完成）

已完成：

- 建立 `c_hypermem` 独立包结构，保留 `pyproject.toml`、README、configs、examples 和 tests。
- 实现 `Memory.from_config/reset/add/add_memory/search/stats/close`，当前推荐入口收敛到 `c_hypermem.Memory`。
- 实现统一 `MemoryNode` schema、系统生成 ID、canonical fingerprint、`node_labels` 配置加载和 prompt 注入。
- 默认启用并测试 `event` / `fact` / `entity` 等核心标签；`state/preference/task/instruction/tool` 也已作为可配置标签存在。
- 实现 entity alias 精确对齐、基础去重、`EntityResolver`、`NodeBuilder`、`LocalGraphBuilder`、`BasicHyperEdgeBuilder`、`BasicEdgeClusterBuilder`、`GraphMaintenance` 等模块拆分；`GraphAssembler` 只保留编排职责。
- 实现基础 `evidence` / `state` / `correction` HyperEdge。
- 实现基础 EdgeCluster，并按 `cluster_fingerprint` 查库复用已有 cluster，新增边通过 `EdgeClusterMember` 追加，不再每条边新建一个簇。
- 实现 SQLite 持久化：`nodes`、`triples`、`hyper_edges`、`hyper_edge_members`、`edge_clusters`、`edge_cluster_members`、`entity_alias_index`、`fact_property_index`、`turns`。
- 删除应用层 `ingestion_cache` / hash / cursor 方案，改为独立 `turns` 表 + Context/Target 增量抽取。
- `list_recent_turn_messages` 的窗口限制已按最近 K 个 `turn_index` 计算，而不是按消息行数计算，避免 tool logs 挤掉用户历史。
- `memory_extraction.md` 已显式包含 `{{INTERACTION_METADATA}}`、`{{RECENT_CONTEXT}}`、`{{TARGET_MESSAGES}}`、`{{STRICT_JSON_SHAPE}}` 变量。
- `search` 返回可序列化结果，并带基础 `score_parts`。
- 已增加 LongMemEval 单段连续对话 smoke runner 和样本，用于观察真实 LLM 图谱构建效果。

仍未完成 / 待接入：

- 尚未正式接入 `agent_memory_eval` 的 `c_hypermem` backend；因此 `longmemeval_smoke --limit 1 --no-eval` 还不是标准 suite 入口跑通。
- LongMemEval 当前只做了手工 smoke：真实 LLM 能构建答案事实图节点，但检索侧尚未稳定召回答案事实。

### M2：复合节点增强（部分完成）

已完成：

- 为 `event`、`entity`、`fact` 构建基础统一 `LocalNodeGraph`。
- 持久化 LocalGraph triples 到 `triples` 表。
- `assertions` 已作为事实节点、property index 和基础 triple 的唯一主输入。
- 支持 LLM 驱动的冲突事实判断：同一 `subject_node_id + predicate` 下存在旧 fact 时调用 `contradiction_check.md`，无硬编码多值谓词或规则兜底。
- 支持旧 fact `retired/superseded_by/invalidated_by`、valid time end 更新、`correction` HyperEdge 和 retired fact property index。
- `IngestionOutput` 已返回 `retired_nodes`；退役旧 fact 时会同步删除其 triple 对应的 Qdrant 向量点，避免退役事实被向量召回。
- `turns` 表与知识图谱表分离，Node / Edge / Cluster metadata 写入 `source_turn_ids`，支持图数据溯源。
- `tool` / `instruction` 等标签已在配置中预留，可由 LLM 输出并通过统一 `MemoryNode` 承载。

仍未完成 / 待增强：

- `LocalNodeGraph` 仍是轻量实现，主要覆盖 event participants、entity attributes 和 assertion SPO；尚未系统表达工具调用、任务状态、事件内部关系、qualifiers 的复杂结构。
- `fact_merge.md`、`edge_merge.md`、`edge_conflict_check.md`、`edge_cluster_merge.md` 已存在但未接入主流程。
- EdgeCluster 仅做确定性 topic/fingerprint 聚合和 description variants 保存；尚未实现 `contains_conflict` / 复杂 conflict state 维护、LLM cluster merge 和后台宏观整理。
- search result metadata 尚未返回“命中的 triple / local_graph 摘要”作为一等调试字段。
- fact 去重和 entity linking 当前是轻量精确匹配；尚未做 LLM 候选确认、复杂实体消歧或跨样本同名实体隔离策略。

### M3：高阶关联检索（部分完成）

已完成：

- `Retriever` 已拆出独立 retrieval 组件，当前包括 lexical recall、dense vector recall、RRF fusion 和 graph ripple expansion。
- 实现 SQLite FTS lexical recall。
- 实现 node_content、node_summary、node-local-graph 三路向量召回。
- 实现 RRF 融合 lexical / vector 初始结果。
- 实现 HyperEdge / EdgeCluster graph ripple expansion。
- 实现 `edge_coherence` 非线性结构化加分。
- `Memory.search()` 当前返回 top-k HyperEdges；每条 edge 在 metadata 中携带成员 nodes、node triples、cluster description variants 和可解释 score parts。

仍未完成 / 待增强：

- 尚未接入 EdgeCluster canonical / variant 向量召回。
- 尚未接入 turn_dialogue 向量召回。
- 尚未接入 entity alias recall。
- 尚未实现 multi-hop expansion 和 semantic aggregation HyperEdge。
- temporal filter 仍是轻量化评分/metadata 方案，尚未形成完整时间条件解析与过滤。
- 尚未完成 ablation：no local graph、no graph ripple、no entity alias resolution、no vector recall。

### M4：正式评测与发布（未完成）

已完成：

- 已有独立包骨架、README、pyproject、默认配置、测试集和手工 smoke example。
- 当前自动化测试覆盖配置加载、prompt 渲染、Context/Target 增量抽取、turns 表、基础图写入、冲突退役、EdgeCluster 复用、Qdrant 写入侧接口等核心路径。

仍未完成 / 待发布前处理：

- 尚未跑 LongMemEval S cleaned 正式评测。
- 尚未跑 LOCOMO。
- 尚未输出系统化 retrieval failure 分析、token cost、ablation 报告。
- 尚未把 C-HyperMem 接入 `agent_memory_eval` 作为标准 backend。
- 尚未完成发布前安装验证、可选依赖组合验证和包发布流程。
