# C-HyperMem 后续开发架构

本文档基于 `docs/hypergraph_memory_architecture.md` 的“多视角复合节点超图 Memory”构思，给出后续工程开发架构。目标是让 C-HyperMem 作为独立 Python 包开发，同时满足 `agent_memory_eval` 的自研 memory backend 接入要求。

核心边界：

- C-HyperMem 是独立 memory 包，负责核心算法、schema、存储、抽取、索引和检索。
- `agent_memory_eval` 只保留 thin adapter，负责 `MemorySession` / `MemoryItem` 格式转换、namespace 隔离和评测日志。
- 评测框架不直接依赖 C-HyperMem 内部模块，只依赖稳定对外 API：`Memory.from_config/reset/add/search/stats`。
- C-HyperMem 不允许依赖 `agent_memory_eval` 的任何模块、数据类、配置加载器或 runner。依赖方向只能是 `agent_memory_eval -> C-HyperMem`。

## 0. 独立发布边界

C-HyperMem 必须作为独立算法项目开发和发布，定位类似 `mem0` 或 `A-mem` 这类 memory 架构包，而不是 `agent_memory_eval` 的内部 backend 实现。

本地参考项目体现了这个边界：

- `mem0` 是完整 SDK 包，`pyproject.toml` 发布包名为 `mem0ai`，核心代码在 `mem0/` 包内，并通过 `mem0.__init__` 暴露 `Memory`、`AsyncMemory`、`MemoryClient` 等公开入口。
- `A-mem` 是轻量研究型独立包，`pyproject.toml` 发布包名为 `agentic-memory`，核心代码在 `agentic_memory/` 包内，示例和测试通过公开类使用它。

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

## 1. 总体分层

```text
agent_memory_eval
  -> C-HyperMem adapter
  -> C-HyperMem public API
  -> memory pipeline
  -> stores / llms / embeddings

C-HyperMem internal:
  MemorySession-like input
    -> Turn/Event/Fact/Entity extraction
    -> SharedNodes
    -> MultiViewEdges
    -> LocalNodeGraphs
    -> Hybrid indexes
    -> Search results
```

C-HyperMem 的核心数据结构保持：

```text
Memory = SharedNodes + MultiViewEdges + LocalNodeGraphs
```

其中：

- `SharedNodes`：长期记忆共享节点池。
- `MultiViewEdges`：多个关系视角下的高阶关联边。
- `LocalNodeGraphs`：复合节点内部挂载的三元组集合或小型知识图谱。

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
    memory.py                 # 对外主入口 Memory
    config.py                 # 配置加载与校验
    schema.py                 # dataclass / pydantic schema
    errors.py

    pipeline/
      ingestion.py            # add() 写入主流程
      extraction.py           # 事件、事实、实体抽取
      view_projection.py      # 多视角边构建
      local_graph_builder.py  # 复合节点内部三元组/子图构建
      maintenance.py          # 去重、合并、状态更新

    retrieval/
      query_analysis.py
      recall.py
      expansion.py
      ranking.py
      context.py

    stores/
      base.py
      sqlite_store.py         # nodes / edges / triples / metadata
      vector_store.py
      lexical_store.py

    llms/
      base.py
      openai_compatible.py
      prompts.py

    embeddings/
      base.py
      openai_compatible.py

    adapters/                 # 可选集成，不被核心模块依赖
      agent_memory_eval.py     # optional thin adapter helpers only
      langchain.py             # optional ecosystem integration

    utils/
      ids.py
      time.py
      text.py

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

`agent_memory_eval` 中只需要新增：

```text
agent_memory_eval/backends/c_hypermem_backend.py
configs/memory/c_hypermem.yaml
```

并修改：

```text
agent_memory_eval/backends/factory.py
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
description = "Composite hypergraph memory for long-term agents"
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

这和 mem0 通过包入口暴露 `Memory`、A-mem 通过独立包暴露 memory system 的方式一致：外部系统调用公开 API，不进入内部 pipeline 或 store。

## 3. 对外 API

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

### 3.1 Runtime 时序

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

### 3.2 Memory 类职责

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

### 3.3 AgentInteraction 输入模型

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

`search()` 返回值使用普通 dict，方便 adapter 转成 `MemoryItem`：

```python
{
    "id": "fact:...",
    "content": "...",
    "score": 0.83,
    "metadata": {
        "source_session_id": "S1",
        "node_type": "fact",
        "node_id": "fact:...",
        "event_ids": ["event:S1:0"],
        "view_edge_ids": ["edge:..."],
        "views": ["entity_state_view", "temporal_view"],
        "source_turn_ids": ["turn:S1:1"],
        "score_parts": {...}
    }
}
```

## 4. 核心 Schema

### 4.1 SharedNode

统一节点基础字段：

```python
{
    "id": "fact:...",
    "type": "turn|event|fact|entity|state|preference|task",
    "content": "...",
    "summary": "...",
    "metadata": {...},
    "absolute_time": {
        "event_time": "...",
        "valid_time": {"start": "...", "end": null, "as_of": null},
        "source_timestamp": "..."
    },
    "relative_time": {
        "created_turn": 17,
        "last_access_turn": null,
        "turn_distance": 0,
        "access_count": 0,
        "decay_weight": 1.0
    },
    "local_graph": {
        "triples": [],
        "attributes": {},
        "roles": {}
    }
}
```

双时间指标约束：

- `absolute_time` 用于真实世界时序、状态有效期和 as-of 问题。
- `relative_time` 用于记忆衰减、激活强度和遗忘策略。
- 两者不能混用；衰减不能替代事实有效期判断。

### 4.1.1 时间挂载策略

建议不要把所有时间都只挂在外层高阶结构上，也不要全部塞进局部图谱。更稳的策略是分层挂载：

```text
SharedNode.time
  记录节点自身的生命周期、来源时间、事实有效时间

ViewEdge.time
  记录这个视角关系何时形成、何时更新、当前是否有效

LocalNodeGraph.triples[].qualifiers
  记录节点内部某条三元组的时间限定
```

也就是说：

- 节点级时间回答“这个记忆对象是什么时候发生、写入、更新、访问的”。
- 边级时间回答“这组节点在这个视角下的关联是什么时候建立或变化的”。
- 局部三元组时间回答“节点内部这个属性/角色/关系在什么时间成立”。

### 4.1.2 节点级时间字段

节点级时间建议拆成三组，而不是只用 `absolute_time` / `relative_time` 两个粗字段：

```python
{
    "time": {
        "world": {
            "event_time": "2023-07-11",
            "valid_time": {"start": "2023-07-11", "end": null, "as_of": null},
            "source_timestamp": "2023-07-11T10:15:00"
        },
        "lifecycle": {
            "created_at": "2026-05-22T10:00:00",
            "inserted_at": "2026-05-22T10:00:02",
            "updated_at": "2026-05-22T10:03:18",
            "deleted_at": null
        },
        "activation": {
            "created_turn": 17,
            "inserted_turn": 17,
            "updated_turn": 18,
            "last_access_turn": 24,
            "access_count": 3
        }
    }
}
```

字段语义：

- `world` 是绝对时间，来自对话内容或数据集时间戳。
- `lifecycle` 是系统时间，表示节点在 C-HyperMem 中的创建、插入、更新、删除。
- `activation` 是相对对话轮次，表示记忆写入和访问距离当前轮次有多远。

`turn_distance` 和 `decay_weight` 不建议长期持久化为权威字段，因为它们依赖当前对话轮次。更好的方式是检索或维护时动态计算：

```text
turn_distance = current_turn - activation.inserted_turn
decay_weight = exp(-decay_lambda * turn_distance)
```

如果为了 debug 或加速需要缓存，也应视为 cache：

```python
{
    "activation_cache": {
        "computed_at_turn": 31,
        "turn_distance": 14,
        "decay_weight": 0.66
    }
}
```

### 4.1.3 自动更新时间规则

每个节点在创建、插入、更新、访问时自动维护时间：

```text
create node:
  lifecycle.created_at = now()
  activation.created_turn = current_turn
  world.* = extracted from content / metadata

insert node into store:
  lifecycle.inserted_at = now()
  activation.inserted_turn = current_turn

update node content or local graph:
  lifecycle.updated_at = now()
  activation.updated_turn = current_turn
  preserve original lifecycle.created_at / inserted_at
  update world.valid_time only if the real-world fact changed

access node during search:
  activation.last_access_turn = current_turn
  activation.access_count += 1
  do not change world.* or lifecycle.updated_at
```

注意：检索访问只改变 activation，不应改变 `updated_at`。`updated_at` 表示记忆内容或结构被修改，不表示被读过。

### 4.2 ViewEdge

多视角高阶边：

```python
{
    "id": "edge:...",
    "namespace": "sample_001",
    "view": "entity_state_view",
    "relation": "state_of_entity",
    "node_ids": ["entity:andrew", "fact:andrew_has_pet_toby", "event:S1:0"],
    "roles": {
        "entity:andrew": "subject",
        "fact:andrew_has_pet_toby": "state_fact",
        "event:S1:0": "evidence_event"
    },
    "weights": {
        "entity:andrew": 1.0,
        "fact:andrew_has_pet_toby": 0.9,
        "event:S1:0": 0.6
    },
    "metadata": {
        "created_by": "llm|heuristic",
        "reason": "...",
        "created_turn": 17
    },
    "time": {
        "lifecycle": {
            "created_at": "2026-05-22T10:00:03",
            "updated_at": "2026-05-22T10:05:00"
        },
        "activation": {
            "created_turn": 17,
            "updated_turn": 19,
            "last_access_turn": 24,
            "access_count": 2
        },
        "world": {
            "valid_time": {"start": "2023-07-11", "end": null, "as_of": null}
        }
    }
}
```

边级 `world.valid_time` 是可选的，只在“这个视角关系本身有真实世界有效期”时使用。例如 `entity_state_view` 中“Andrew 拥有 Toby”可以有有效期；`provenance_view` 通常不需要世界有效期，只需要生命周期时间。

第一版建议实现这些视角：

- `provenance_view`：连接 turn、event、fact，保留来源。
- `entity_state_view`：围绕实体组织状态、属性、关系。
- `temporal_view`：按真实世界时间组织 event/fact。
- `topic_or_intent_view`：聚合分散但语义相关的长期议题。
- `preference_profile_view`：组织偏好、画像、稳定倾向。

### 4.3 LocalNodeGraph

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
                "confidence": 0.84
            }
        },
        {
            "subject": "Toby",
            "predicate": "is_a",
            "object": "dog",
            "qualifiers": {
                "confidence": 0.95
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

外层 `ViewEdge` 表达高阶关联；内层 `LocalNodeGraph` 表达节点自身的属性、角色、三元组和局部语义。

局部图谱中的三元组可以携带 `qualifiers.valid_time`，但不建议复制节点的完整生命周期时间。生命周期由 owner node 管理；三元组只保存自身语义成立所需的限定信息。

## 5. 写入 Pipeline

`Memory.add_memory()` 的推荐流程：

```text
add_memory(user_input, assistant_output, namespace, metadata, tool_calls, ...)
  1. normalize AgentInteraction
  2. resolve ingestion cache
  3. select changed/new interaction span
  4. build TurnNode[]
  5. build EventNode
  6. extract FactNode[]
  7. link / merge EntityNode[]
  8. build LocalNodeGraph for selected nodes
  9. project nodes into enabled views
  10. write SharedNodes
  11. write ViewEdges
  12. update lexical / vector indexes
  13. update ingestion cache cursor
```

### 5.1 输入规范化

外部调用方可以传入 `add_memory(user_input, assistant_output, ...)`，也可以通过 `add(messages, ...)` 批量导入历史对话。C-HyperMem 内部统一成 `AgentInteraction` 或 `MemoryImportBatch`。

`AgentInteraction`：

```python
{
    "type": "agent_interaction",
    "user_input": {"role": "user", "content": "...", "timestamp": "...", "metadata": {}},
    "assistant_output": {"role": "assistant", "content": "...", "timestamp": "...", "metadata": {}},
    "tool_calls": [],
    "tool_results": [],
    "observations": [],
    "attachments": [],
    "trace": {},
    "metadata": {
        "session_id": "S1",
        "date": "2024-01-03"
    }
}
```

`MemoryImportBatch`：

```python
{
    "type": "memory_import_batch",
    "messages": [
        {"role": "...", "content": "...", "timestamp": "...", "metadata": {...}}
    ],
    "metadata": {
        "session_id": "S1",
        "date": "2024-01-03",
        ...
    }
}
```

注意：这里的 adapter 是外部调用方角色，不是 C-HyperMem 核心依赖。C-HyperMem 只接受通用事件或批量导入数据，不感知 `agent_memory_eval` 的 dataclass。

### 5.2 增量构建缓存策略

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
  attach new nodes to existing SharedNodes / ViewEdges
  update cache cursor and prefix hash
```

这样不会额外增加模型调用，只多一次本地缓存读取和 hash 对比。真正的 LLM 调用仍然只发生在需要抽取新事实、构建局部图谱或投影视角时。

### 5.3 标志位建议

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

### 5.4 缓存风险与处理

增量构建的主要风险是：新消息可能改变旧事实的解释、有效期或视角归属。例如用户纠正了旧信息，或者新上下文让旧事件的主体发生消歧。

因此 append-only 不代表只写新节点，还需要允许维护旧节点：

- 新事实与旧事实冲突时，更新旧 fact 的 `status` 或 `valid_time.end`。
- 新事实补充旧事件时，可以更新旧 EventNode 的 `local_graph`。
- 新实体消歧后，可以重连相关 `ViewEdge`。
- 新主题形成后，可以把旧 fact 挂到新的 `topic_or_intent_view`。

也就是说，缓存策略减少“输入读取和重复抽取”，但不禁止“对旧图结构做必要维护”。

### 5.5 第一版降级策略

为尽快跑通评测，M1 可以采用保守实现：

- 一个 `MemorySession` 生成一个 `EventNode`。
- 每个 session 抽取若干 `FactNode`。
- 用规则/LLM 抽取实体，生成 `EntityNode`。
- 只构建 `provenance_view`、`entity_state_view`、`temporal_view`。
- `topic_or_intent_view` 和 `preference_profile_view` 可在 M2 加入。

## 6. 检索 Pipeline

`Memory.search()` 返回可进入 reader prompt 的结果。

```text
search(query, namespace, top_k)
  1. analyze query intent / entity / time constraint
  2. lexical + vector recall over nodes
  3. optional recall over view edges
  4. expand through incident ViewEdges
  5. score by semantic, lexical, view coherence, temporal compatibility, recency
  6. compose results
```

### 6.1 检索结果内容

`content` 应保持简洁，优先返回事实和必要来源：

```text
[Fact] Alice prefers morning interviews.
Source: session=S1 date=2024-01-03 views=entity_state_view,preference_profile_view
```

多证据问题可以返回聚合证据：

```text
[Evidence: Nate's tournament wins | view=topic_or_intent_view]
- 2022-01-21: Nate won his first video game tournament.
- 2022-05-02: Nate won his second tournament.
```

### 6.2 评分组成

建议第一版使用可解释线性评分：

```text
score =
  dense_similarity
+ lexical_score
+ view_coherence
+ entity_match
+ temporal_score
+ recency_bonus
+ salience_bonus
- contradiction_penalty
```

其中：

- `temporal_score` 来自绝对时间。
- `recency_bonus` 来自相对轮次间隔。
- `view_coherence` 来自同一视角边中多个候选节点的共同命中。

## 7. 存储设计

第一版推荐 SQLite + 本地向量索引，简单、可 debug、便于 namespace 隔离。

### 7.1 SQLite 表

建议逻辑表：

```text
nodes
  namespace
  node_id
  node_type
  content
  summary
  absolute_time_json
  relative_time_json
  local_graph_json
  metadata_json

view_edges
  namespace
  edge_id
  view
  relation
  metadata_json

view_edge_members
  namespace
  edge_id
  node_id
  role
  weight

triples
  namespace
  owner_node_id
  subject
  predicate
  object
  metadata_json

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

### 7.2 索引对象

- Lexical index：BM25 或 SQLite FTS。
- Vector index：Faiss / Chroma / numpy matrix。
- Edge index：`node_id -> incident edge_ids`。

每个 namespace 必须隔离，避免 benchmark 样本之间信息泄漏。

## 8. 配置草案

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
  extract_facts: true
  extract_entities: true
  build_local_graph: true
  max_facts_per_event: 12
  cache:
    enabled: true
    compare_system_prompt: true
    compare_memory_config: true
    compare_prompt_template: true
    on_prefix_mismatch: conservative_full_rebuild
    hash_algorithm: sha256

views:
  enabled:
    - provenance_view
    - entity_state_view
    - temporal_view
    - topic_or_intent_view
    - preference_profile_view

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
  use_view_expansion: true
  use_temporal_filter: true
  use_recency_decay: true

extraction_llm:
  model: ${LLM_MODEL}
  api_key_env: LLM_API_KEY
  base_url: ${LLM_BASE_URL}
```

## 9. agent_memory_eval Adapter

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
- 不实现 extraction、view projection、local graph building、ranking、storage 等核心算法。
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

## 10. Factory 注册

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

## 11. Suite 接入

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

## 12. 开发里程碑

### M1：最小可接入

- 建立 `c_hypermem` 包结构。
- 实现 `Memory.from_config/reset/add/search/stats/close`。
- 一个 session 生成一个 EventNode。
- 实现基础 FactNode / EntityNode。
- 实现 `provenance_view`、`entity_state_view`、`temporal_view`。
- 返回可转换为 `MemoryItem` 的 search result。
- 接入 `agent_memory_eval` 并跑通 `longmemeval_smoke --limit 1 --no-eval`。

### M2：复合节点增强

- 为 EventNode / FactNode / EntityNode 构建 `LocalNodeGraph`。
- 持久化 triples。
- 在 search result metadata 中返回命中的 triples / local_graph 摘要。
- 增加 fact 去重与 entity linking。

### M3：多视角检索

- 增加 `topic_or_intent_view`、`preference_profile_view`。
- 实现 view edge expansion。
- 实现可解释 `score_parts`。
- 支持 temporal filter 和 recency decay。

### M4：正式评测

- 跑 LongMemEval S cleaned。
- 跑 LOCOMO。
- 做 ablation：
  - no local graph
  - no view expansion
  - no entity_state_view
  - no temporal_view
  - shared fact nodes vs duplicated fact nodes
