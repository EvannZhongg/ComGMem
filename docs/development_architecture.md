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

注意：开发过程禁止使用任何规则化抽取策略或是兜底策略。在项目架构更新时无需考虑旧数据的兼容。

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
    -> Hybrid indexes
    -> Search results
```

C-HyperMem 的核心数据结构保持：

```text
Memory = MemoryNodes + HyperEdges + LocalNodeGraphs
```

其中：

- `MemoryNodes`：长期记忆共享节点池。节点使用统一 schema，通过可配置 `node_type` 表达 fact、entity、event、tool 等语义类型。
- `HyperEdges`：稳定语义锚点下的高阶关联边。一个节点可以属于多个超边。
- `LocalNodeGraphs`：复合节点内部挂载的属性、角色、三元组和局部状态。

重要调整：

- 放弃显式多视角架构，不再维护固定的关系视角列表。
- 不让 LLM 判断事实属于哪个视角。
- 系统根据抽取出的实体、事件、事实、属性、角色、三元组和来源，构建或更新 `HyperEdges`。
- 超边可以表达来源证据、实体状态、时间聚合、修正关系、任务进度、语义聚合等关系，但这些是普通 `edge_type`，不是一组固定投影视角。
- `fact`、`entity`、`event` 不作为不同内部 schema 维护，而是统一 `MemoryNode` 的默认 `node_type` 配置。

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
      entity_resolution.py     # entity alias 对齐
      node_builder.py          # 根据 node_type 配置实例化 MemoryNode
      node_type_registry.py    # node_type 配置注册与校验
      hyperedge_builder.py     # HyperEdge 构建与更新
      local_graph_builder.py   # 复合节点内部三元组/子图构建
      maintenance.py           # 去重、冲突、退役、状态更新

    retrieval/
      query_analysis.py
      recall.py
      expansion.py             # 基于 incident HyperEdges 扩展
      ranking.py
      context.py

    stores/
      base.py
      sqlite_store.py          # nodes / hyper_edges / triples / metadata
      vector_store.py
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
vector = ["faiss-cpu>=1.7.4", "chromadb>=0.4"]
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
```

这些 prompt 的作用：

- `extraction/memory_extraction.md`：默认写入抽取 prompt。对新增交互调用一次，输出实体、事件、事实、属性、角色、三元组和来源片段。
- `maintenance/fact_merge.md`：只有新旧事实高度相似且系统无法确定是否合并时调用。
- `maintenance/contradiction_check.md`：只有同一实体/属性下出现候选冲突时调用。
- `retrieval/query_analysis.md`：检索阶段按需分析 query，不参与写入抽取。

默认不要让同一段上下文反复经过多个 prompt 做不同颗粒度抽取。推荐“一次语义抽取，系统组装”：

```text
AgentInteraction
  -> memory_extraction.md 只调用一次
  -> 得到 entities / events / facts / attributes / roles / triples / sources
  -> 系统根据 node_type 配置生成 MemoryNodes
  -> 系统构建 LocalNodeGraphs
  -> 系统构建或更新 HyperEdges
```

`node_types` 配置需要参与 prompt 渲染。调用 `memory_extraction.md` 前，系统应把当前配置中的启用类型、类型说明和偏好字段作为 prompt context 传入，让 LLM 知道当前更希望抽取哪些节点类型。

但 `node_types` 不是严格白名单。如果 LLM 抽取出了配置之外的 `node_type`，系统不需要拦截或丢弃；应正常按统一 `MemoryNode` schema 入库，并使用默认 ID、索引、local graph 和时间策略处理。这样可以保留真实 agent 场景中的新型记忆对象，后续再通过配置补充专门策略。

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
  - facts
  - attributes
  - roles
  - triples
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

### 3.1 LLM-facing Prompt 原则

给模型的任务描述应使用自然语言信息抽取语义，而不是让模型“构建超图”或“构建高阶边”。

推荐：

```text
Extract entities, events, facts, attributes, roles, simple triples, and source snippets from the text.
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
- `entity_id`
- `triple_id`
- `confidence`
- `salience`
- `weight`
- 外层图结构

推荐最小 JSON：

```json
{
  "entities": [
    {"name": "Alice", "type": "person", "aliases": []}
  ],
  "events": [
    {"summary": "Alice discussed interview scheduling.", "time": "2024-01-03"}
  ],
  "facts": [
    {"subject": "Alice", "predicate": "prefers", "object": "morning interviews"}
  ],
  "attributes": [
    {"entity": "Alice", "name": "preference", "value": "morning interviews"}
  ],
  "roles": [
    {"event": "Alice discussed interview scheduling.", "entity": "Alice", "role": "speaker"}
  ],
  "triples": [
    {"subject": "Alice", "predicate": "prefers", "object": "morning interviews"}
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
make_node_id(namespace, node_type, stable_key) -> str
make_entity_id(namespace, canonical_name, entity_type, disambiguators=None) -> str
make_edge_id(namespace, edge_type, relation, edge_key) -> str
make_triple_id(namespace, owner_node_id, subject, predicate, object, qualifiers=None) -> str
```

ID 可以采用确定性 hash：

```text
node:{type}:{hash(namespace + type + stable_key)}
entity:{hash(namespace + canonical_name + entity_type + disambiguators)}
edge:{hash(namespace + edge_type + relation + edge_key)}
triple:{hash(namespace + owner_node_id + normalized_spo + qualifiers)}
```

也可以在需要保留插入顺序时使用 ULID/UUIDv7，但应由系统生成，并保存 `dedupe_key` 支持合并。

### 5.2 可配置 node_type

C-HyperMem 内部只维护一个统一 `MemoryNode` schema。`fact`、`entity`、`event`、`tool` 等都只是 `node_type` 配置，不是不同存储表或不同内部三元组结构。

推荐配置：

```yaml
node_types:
  entity:
    enabled: true
    id_strategy: alias_resolution_then_hash
    stable_key_fields: [canonical_name, entity_type]
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
    id_strategy: content_hash
    stable_key_fields: [subject, predicate, object, valid_time]
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
    id_strategy: event_hash
    stable_key_fields: [summary, event_time, source_ref]
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

  tool:
    enabled: false
    id_strategy: external_or_hash
    stable_key_fields: [tool_call_id, tool_name, timestamp]
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

- 新增 `tool`、`observation`、`attachment`、`trace` 类型时优先扩展配置，不修改核心节点 schema。
- 所有节点类型共享同一个 `LocalNodeGraph` 结构：`triples`、`attributes`、`roles`、`qualifiers`。
- 不同节点类型的差异通过 ID 策略、索引策略、时间要求、alias/property index 开关表达。
- LLM 不输出 `node_type` 以外的系统控制字段；系统可以根据抽取候选和配置决定生成哪些节点。
- `node_types` 会作为抽取偏好传入 prompt，但不是入库限制；未知 `node_type` 正常创建 `MemoryNode`，并使用默认 fallback 策略。

### 5.3 实体别名对齐先于 ID 生成

实体 ID 生成前必须先做轻量级 entity resolution。系统不应在模型抽取到新实体名称后立刻 hash 生成新 `entity_id`。

在统一 `MemoryNode` schema 下，`entity_id` 建议直接作为 `node_type=entity` 节点的 `node_id` 使用；如果未来需要区分外部实体 ID 和内部节点 ID，再通过 `attributes.external_entity_id` 扩展。

推荐流程：

```text
candidate entity name from LLM
  -> normalize name
  -> search existing MemoryNode pool where node_type = entity
  -> if exact / alias / normalized match:
       reuse existing entity_id
       optionally append new alias/source
     else:
       generate new entity_id from canonical_name
       create new MemoryNode with node_type = entity
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
  entity_id
```

伪代码：

```python
def resolve_entity_id(namespace, name, entity_type=None, aliases=None):
    normalized_names = normalize_aliases([name, *(aliases or [])])
    candidates = entity_alias_index.lookup(namespace, normalized_names, entity_type)
    if candidates:
        return choose_best_candidate(candidates)

    canonical_name = choose_canonical_name(name, aliases)
    entity_id = make_entity_id(namespace, canonical_name, entity_type)
    create_memory_node(
        node_type="entity",
        node_id=entity_id,
        attributes={
            "canonical_name": canonical_name,
            "entity_type": entity_type,
            "aliases": aliases or [],
        },
    )
    return entity_id
```

### 5.4 HyperEdge

`HyperEdge` 是稳定语义锚点下的一组成员节点：

```python
{
    "edge_id": "edge:...",
    "edge_type": "evidence|state|temporal|correction|aggregation|task",
    "relation": "describes_entity_state",
    "edge_key": "entity:andrew:state:pet_profile",
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

`edge_type` 是普通语义类型，不是固定投影视角。第一版可以支持少量通用类型：

- `evidence`：连接来源 turn / event / fact / tool result。
- `state`：连接实体、属性事实和状态节点。
- `temporal`：连接同一时间桶或时间段内的 event / fact。
- `correction`：连接新旧冲突事实，记录 invalidates / supersedes。
- `aggregation`：连接语义上长期相关的一组节点。
- `task`：连接目标、计划、动作、结果和状态变化。

这些类型可以逐步扩展，但不需要让用户或 LLM 维护固定分类表。

### 5.5 统一超边 ID 策略

超边 ID 建议统一为“稳定边实例 ID”，不要从当前成员集合计算。成员集合是边的状态，可能追加、修正或退役；如果 `edge_id` 依赖 `sorted(member_ids)`，任何成员变化都会导致 ID 变化，进而丢失访问次数、生命周期时间、历史 metadata 和调试链路。

统一规则：

```text
edge_id = hash(namespace + edge_type + relation + edge_key)
```

其中 `edge_key` 是边的稳定锚点，而不是成员集合。

`edge_key` 示例：

```text
event:<event_id>:evidence
tool_call:<tool_call_id>:evidence
entity:<entity_id>:state:<state_name>
topic:<normalized_topic_name>
plan:<normalized_plan_name>
correction:<new_fact_id>:invalidates:<old_fact_id>
time:<bucket_or_interval>
```

成员集合用于单独的签名和版本，不参与 `edge_id`：

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

`member_policy` 只控制成员是否允许变化，不影响 ID 策略：

```text
immutable:
  一般不追加成员；如果发现抽取错误，可创建新 member_version 或记录 correction

appendable:
  可持续追加成员，例如长期主题、计划、画像、证据集合

versioned:
  成员变化需要保留历史版本，适合状态变化敏感的关系
```

### 5.6 MemoryNode

推荐节点 schema：

```python
{
    "namespace": "sample_001",
    "node_id": "fact:...",
    "node_type": "fact|event|entity|tool|state|preference|task|turn|...",
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

`node_type` 的允许值、默认字段、ID 策略、索引策略和局部图谱开关来自配置。核心存储层不需要知道 fact、entity、event 的专门内部结构；它只保存统一的 `MemoryNode`。

未来真实 agent 场景可以增加：

```text
tool
observation
attachment
trace
environment
```

这些类型仍然复用同一个 `MemoryNode` schema 和同一个 `LocalNodeGraph` schema。

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

### 5.7 LocalNodeGraph

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

`LocalNodeGraph` 不为 fact、entity、event、tool 分别设计不同 schema。差异通过 triple 内容和 `node_type` 配置表达：

```text
node_type=fact:
  Alice --prefers--> morning interviews

node_type=entity:
  Alice --has_preference--> morning interviews

node_type=event:
  Alice --speaker_in--> interview scheduling discussion

node_type=tool:
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
  2. resolve ingestion cache
  3. select changed/new interaction span
  4. call memory_extraction.md once for the changed span
  5. normalize extracted candidates
  6. resolve entity aliases against existing node_type=entity pool
  7. retrieve existing facts/triples with same entity + property key
  8. detect duplicate / update / conflict
  9. call maintenance prompts only when ambiguous
  10. retire or invalidate old facts when needed
  11. generate system-controlled ids for unresolved nodes / edges / triples
  12. build MemoryNode[] according to node_type config
  13. build LocalNodeGraph from extracted triples / attributes / roles using one shared schema
  14. build or update HyperEdges from candidates and existing graph context
  15. write MemoryNodes
  16. write HyperEdges and HyperEdge members
  17. update lexical / vector indexes
  18. update ingestion cache cursor
```

这里的第 14 步不是固定规则表投影，而是系统内部的 graph assembly。第一版可以先构建最基础的 evidence / state / correction 边，再逐步加入更复杂的语义聚合。

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

### 7.2 增量构建缓存策略

可以引入缓存策略，但建议设计成确定性的本地 cache lookup，而不是额外增加一轮 LLM 判断。

目标：

- 第一轮对话或首次写入时，全量构建当前输入。
- 后续写入时，如果 system prompt、构建配置和历史上下文前缀都没有变化，只处理新增上下文部分。
- 如果关键条件变化，则触发全量重建或局部重建。

缓存判断应基于 hash、版本号和 cursor：

```python
{
    "namespace": "sample_001",
    "conversation_id": "conv_001",
    "system_prompt_hash": "sha256:...",
    "memory_config_hash": "sha256:...",
    "prompt_template_hash": "sha256:...",
    "processed_prefix_hash": "sha256:...",
    "last_processed_turn_index": 12,
    "last_processed_message_id": "msg_012",
    "last_event_id": "event:S1:0",
    "updated_at": "2026-05-22T10:15:00"
}
```

推荐流程：

```text
resolve ingestion cache:
  if no cache:
    mode = full_build
  elif system_prompt_hash changed:
    mode = full_rebuild
  elif memory_config_hash or prompt_template_hash changed:
    mode = rebuild_affected
  elif processed_prefix_hash matches current message prefix:
    mode = append_only
  else:
    mode = conservative_full_rebuild

append_only:
  process interactions/messages after last_processed_turn_index
  attach new nodes to existing MemoryNodes / HyperEdges
  update cache cursor and prefix hash
```

这样不会额外增加模型调用，只多一次本地缓存读取和 hash 对比。真正的 LLM 调用仍然只发生在需要抽取新增内容，或维护 prompt 被触发时。

### 7.3 标志位建议

可以在 `metadata` 或内部 cache 中维护几个标志位：

```python
{
    "is_first_turn": false,
    "system_prompt_changed": false,
    "config_changed": false,
    "history_prefix_unchanged": true,
    "append_only": true,
    "requires_rebuild": false
}
```

这些标志位不建议由 LLM 判断，应由确定性逻辑产生：

- `system_prompt_changed`：比较 system prompt hash。
- `config_changed`：比较 memory 构建相关配置 hash。
- `history_prefix_unchanged`：比较已处理消息前缀 hash。
- `append_only`：当前消息列表是否只是在已处理前缀后追加。
- `requires_rebuild`：上述任一关键条件不满足时置为 true。

### 7.4 缓存风险与处理

增量构建的主要风险是：新消息可能改变旧事实的解释、有效期或实体消歧。例如用户纠正了旧信息，或者新上下文让旧事件的主体发生变化。

因此 append-only 不代表只写新节点，还需要允许维护旧节点：

- 新事实与旧事实冲突时，不物理覆盖旧 fact，而是将旧 fact 标记为 `retired` / `invalidated`，并保留新 fact。
- 新事实补充旧事件时，可以更新旧的 `node_type=event` 节点的 `local_graph`。
- 新实体消歧后，可以重连相关 `HyperEdge`。
- 新语义聚合形成后，可以把旧 fact 挂到新的 `aggregation` 超边。

也就是说，缓存策略减少“输入读取和重复抽取”，但不禁止“对旧图结构做必要维护”。

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
property_key = normalized_entity_id + ":" + normalized_predicate_or_attribute
```

然后检索：

```text
existing facts where property_key = "entity:toby:is_a"
existing triples where owner/entity = "entity:toby" and predicate = "is_a"
```

推荐决策：

```text
same value:
  merge source / extraction_count，不创建重复事实

compatible value:
  append as additional fact，例如 aliases、多个爱好、多个参与者

conflicting value:
  create new MemoryNode with node_type = fact
  mark old node_type=fact MemoryNode status = retired 或 invalidated
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

- 一个 session 或 import batch 生成一个 `node_type=event` 的 `MemoryNode`。
- 每个 event 抽取若干 `node_type=fact` 的 `MemoryNode`。
- 抽取实体并做轻量 alias resolution，生成或复用 `node_type=entity` 的 `MemoryNode`。
- 构建基础 `evidence` 超边，连接 turn / event / fact。
- 构建基础 `state` 超边，连接 entity / fact / state。
- 构建基础 `correction` 超边，连接新旧冲突事实。
- 暂缓复杂语义聚合，避免一开始引入固定分类表。

## 8. 检索 Pipeline

`Memory.search()` 返回可进入 reader prompt 的结果。检索不是本文最核心部分，但需要在开发架构中预留接口。

```text
search(query, namespace, top_k)
  1. analyze query intent / entity / time constraint if needed
  2. lexical + vector recall over nodes
  3. optional recall over hyper_edges
  4. expand through incident HyperEdges
  5. score by semantic, lexical, edge coherence, temporal compatibility, recency
  6. compose results
```

### 8.1 检索结果内容

`content` 应保持简洁，优先返回事实和必要来源：

```text
[Fact] Alice prefers morning interviews.
Source: session=S1 date=2024-01-03 edge_types=state,aggregation
```

多证据问题可以返回聚合证据：

```text
[Evidence: Nate's tournament wins | edge_type=aggregation]
- 2022-01-21: Nate won his first video game tournament.
- 2022-05-02: Nate won his second tournament.
```

### 8.2 评分组成

建议第一版使用可解释线性评分：

```text
score =
  dense_similarity
+ lexical_score
+ edge_coherence
+ entity_match
+ temporal_score
+ recency_bonus
+ salience_bonus
- contradiction_penalty
```

其中：

- `temporal_score` 来自绝对时间。
- `recency_bonus` 来自相对轮次间隔。
- `edge_coherence` 来自同一超边中多个候选节点的共同命中。

## 9. 存储设计

第一版推荐 SQLite + 本地向量索引，简单、可 debug、便于 namespace 隔离。

### 9.1 SQLite 表

建议逻辑表：

```text
nodes
  namespace
  node_id
  node_type
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
  edge_type
  relation
  edge_key
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

triples
  namespace
  triple_id
  owner_node_id
  subject
  predicate
  object
  status
  superseded_by
  invalidated_by
  qualifiers_json
  metadata_json

fact_property_index
  namespace
  property_key
  entity_id
  predicate
  fact_id
  status
  updated_at

entity_alias_index
  namespace
  normalized_alias
  entity_type
  entity_id
  source_count
  updated_at

ingestion_cache
  namespace
  conversation_id
  system_prompt_hash
  memory_config_hash
  prompt_template_hash
  processed_prefix_hash
  last_processed_turn_index
  last_processed_message_id
  last_event_id
  metadata_json
  updated_at
```

### 9.2 索引对象

- Lexical index：BM25 或 SQLite FTS。
- Vector index：Faiss / Chroma / numpy matrix。
- HyperEdge index：`node_id -> incident edge_ids`。
- Entity alias index：`normalized_alias -> entity_id`。
- Fact property index：`entity_id + predicate -> active fact ids`。

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
  incremental_build: true
  max_facts_per_event: 12
  cache:
    enabled: true
    compare_system_prompt: true
    compare_memory_config: true
    compare_prompt_template: true
    on_prefix_mismatch: conservative_full_rebuild
    hash_algorithm: sha256

extraction:
  prompt: extraction/memory_extraction.md
  output_schema: minimal_memory_candidates
  forbid_model_ids: true
  forbid_confidence: true
  pass_node_types_to_prompt: true
  allow_unknown_node_types: true

node_types:
  default_policy:
    id_strategy: content_hash
    local_graph: {enabled: true, allow_triples: true, allow_attributes: true, allow_roles: true}
    indexing: {lexical: true, vector: true}
  entity:
    enabled: true
    id_strategy: alias_resolution_then_hash
    stable_key_fields: [canonical_name, entity_type]
    alias_resolution: true
    local_graph: {enabled: true, allow_triples: true, allow_attributes: true, allow_roles: true}
    indexing: {lexical: true, vector: true, alias_index: true}
  fact:
    enabled: true
    id_strategy: content_hash
    stable_key_fields: [subject, predicate, object, valid_time]
    property_index: true
    local_graph: {enabled: true, allow_triples: true, allow_attributes: true, allow_roles: true}
    indexing: {lexical: true, vector: true}
  event:
    enabled: true
    id_strategy: event_hash
    stable_key_fields: [summary, event_time, source_ref]
    time: {prefer_world_time: true}
    local_graph: {enabled: true, allow_triples: true, allow_attributes: true, allow_roles: true}
  tool:
    enabled: false
    id_strategy: external_or_hash
    stable_key_fields: [tool_call_id, tool_name, timestamp]
    time: {prefer_world_time: true}
    local_graph: {enabled: true, allow_triples: true, allow_attributes: true, allow_roles: true}
    indexing: {lexical: true, vector: false}

hyperedges:
  enabled: true
  build_from_extraction: true
  member_policy_default: appendable
  basic_edge_types:
    - evidence
    - state
    - correction

local_graph:
  enabled: true
  schema: uniform
  configured_by_node_type: true

time:
  relative_decay:
    enabled: true
    unit: turn
    decay_lambda: 0.03
    access_boost: 0.05

index:
  lexical: sqlite_fts
  vector: faiss
  use_embedding: true

retrieval:
  lexical_top_n: 30
  vector_top_n: 30
  edge_top_n: 30
  rerank_top_n: 12
  use_hyperedge_expansion: true
  use_temporal_filter: true
  use_recency_decay: true

extraction_llm:
  model: ${LLM_MODEL}
  api_key_env: LLM_API_KEY
  base_url: ${LLM_BASE_URL}
```

配置原则：

- 不出现 `agent_memory_eval` 内部类名。
- 不以 benchmark 字段作为核心必需项。
- 不使用固定多视角配置块。
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

### M1：最小可接入

- 建立 `c_hypermem` 包结构。
- 实现 `Memory.from_config/reset/add/search/stats/close`。
- 实现统一 `MemoryNode` schema 和 `node_type` 配置加载。
- 默认启用 `event` / `fact` / `entity` 节点类型。
- 实现系统生成 ID、entity alias 对齐和基础去重。
- 实现基础 `evidence` / `state` / `correction` HyperEdges。
- 返回可转换为 `MemoryItem` 的 search result。
- 接入 `agent_memory_eval` 并跑通 `longmemeval_smoke --limit 1 --no-eval`。

### M2：复合节点增强

- 为默认节点类型构建统一 `LocalNodeGraph`。
- 持久化 triples。
- 支持冲突事实退役和 correction HyperEdge。
- 在 search result metadata 中返回命中的 triples / local_graph 摘要。
- 增加 fact 去重与 entity linking。
- 增加可选 `tool` / `observation` 节点类型配置，为真实 agent 场景预留。

### M3：高阶关联检索

- 实现 HyperEdge incident expansion。
- 支持可选 semantic aggregation HyperEdge。
- 实现可解释 `score_parts`。
- 支持 temporal filter 和 recency decay。
- 做 ablation：no local graph、no hyperedge expansion、no entity alias resolution。

### M4：正式评测与发布

- 跑 LongMemEval S cleaned。
- 跑 LOCOMO。
- 输出 retrieval failure 分析和 token cost。
- 准备独立包 README、examples、pyproject。
- 确保 C-HyperMem 脱离 `agent_memory_eval` 可安装、可测试、可发布。
