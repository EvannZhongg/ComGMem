# 复合节点高阶关联 Memory 构思

本文档只记录当前关于 C-HyperMem memory 结构的概念构思。这里的“高阶关联 / HyperEdge”是系统内部数据结构，不要求 LLM 直接生成，也不要求先定义一组固定“视角”。

暂不在本文档展开检索流程、代码组织、评测接入和长期维护策略；这些内容放在 `development_architecture.md` 中。

## 1. 核心想法

同一批长期记忆先沉淀为一组可共享节点，再由多个高阶边把相关节点组织成不同的记忆单元。

当前结构可以概括为：

```text
Memory = MemoryNodes + HyperEdges + LocalNodeGraphs
```

其中：

- `MemoryNodes`：长期记忆节点池。节点使用统一 schema，通过 `node_labels` 表达 fact、entity、event、tool 等可累积语义标签。
- `HyperEdges`：连接多个节点的高阶关联边。一个节点可以同时属于多个超边。
- `LocalNodeGraphs`：某些节点内部的小型知识图谱，用来描述属性、角色、语义三元组和局部状态。

设计重点：

- 不再显式维护固定多视角列表。
- 不让 LLM 判断“这个事实属于哪个视角”。
- 同一个 `MemoryNode` 可以挂到多个 `HyperEdge` 上；`fact`、`entity`、`event` 等只是节点标签，不参与节点身份。
- 超边表示“这些节点在某个语义锚点下应该被一起看”，但这个锚点由系统根据抽取结果和已有记忆上下文生成。
- 节点和超边的权重、访问次数、衰减等指标由系统后续计算，不由 LLM 输出。

示意：

```text
entity:andrew
fact:andrew_has_pet_toby
event:andrew_mentions_toby
fact:toby_is_cat

  -> edge:evidence:andrew_toby_mention
  -> edge:state:andrew_pet_profile
  -> edge:correction:toby_species_update
```

这里不是三个固定视角，而是三个可独立增长、可共享节点的高阶关系实例。

## 2. 共享节点池

共享节点池保存长期记忆中的基本对象。节点只表达可复用的记忆内容，不绑定唯一组织方式。

核心 schema 只有一个 `MemoryNode`：

```python
{
    "node_id": "node:...",
    "canonical_text": "Toby",
    "normalized_text": "toby",
    "fingerprint": "sha256:...",
    "node_labels": ["entity", "fact_subject"],
    "content": "...",
    "attributes": {},
    "local_graph": {
        "triples": [],
        "attributes": {},
        "roles": {}
    },
    "time": {},
    "metadata": {}
}
```

`fact`、`entity`、`event` 不是三套不同内部结构，而是默认 `node_labels`。后续接入真实 agent 场景时，可以通过配置扩展出 `tool`、`observation`、`attachment`、`trace` 等标签。

默认节点类型可以包括：

- `turn`：原始对话轮次，用于保留来源。
- `event`：带真实世界时间锚点的事件、经历或会话片段。
- `fact`：可查询的原子事实。
- `entity`：人物、地点、项目、宠物、组织等主体。
- `state`：某个主体在某个时间段内的状态。
- `preference`：长期偏好、倾向、习惯或稳定画像。
- `task`：计划、目标、任务或进度状态。
- `tool`：真实 agent 中的 tool call / tool result / observation。

这些标签应来自配置，而不是写死在存储 schema 中。不同 `node_labels` 的差异主要体现在检索、维护和提示偏好上，而不体现在 `node_id` 生成上：

- 是否启用 alias resolution。
- 是否进入 property index。
- 是否要求或偏好 world time。
- 是否允许作为 HyperEdge anchor。
- 是否启用 lexical / vector index。

节点内部的 `LocalNodeGraph` 统一使用同一套结构，不为 fact、entity、event、tool 分别设计不同三元组 schema。

`node_labels` 配置会作为抽取偏好传入 LLM prompt，但不是严格入库白名单。如果模型抽取出配置外的节点标签，系统仍应按统一 `MemoryNode` schema 正常入库，并使用默认 fallback 策略处理。

同一个带有 `fact` 标签的节点可以被多个超边共享。例如：

```text
fact:nate_won_first_tournament
  belongs to:
    edge:evidence:session_s1_fact_bundle
    edge:aggregation:nate_tournament_history
    edge:temporal:2022_01_memory_bucket
```

这些超边可以共享同一个 `MemoryNode`，不需要复制多份事实。

## 3. ID 生成原则

节点、超边、实体和局部三元组的 canonical id 应由系统自动生成，而不是由LLM生成。

LLM 可以帮助抽取：

- 实体名称、别名、类型。
- 事件摘要和时间表达。
- 事实、属性、角色。
- subject / predicate / object 三元组。
- 来源片段或 source reference。

LLM 不应控制：

- `node_id`
- `edge_id`
- `entity_id` / external entity id
- `triple_id`
- namespace
- storage primary key
- 节点权重或边权重
- 置信度、重要性分数
- 外层高阶关联结构

推荐流程：

```text
LLM 抽取候选语义单元
  -> 系统规范化文本和字段
  -> 对实体候选先做别名对齐
  -> 生成 canonical_text / normalized_text
  -> 生成 canonical fingerprint
  -> 根据 namespace + fingerprint 生成 node_id
```

示例：

```text
fingerprint = hash(normalized_canonical_text + disambiguation_hint)
node_id = hash(namespace + fingerprint)
triple_id = hash(namespace + owner_node_id + normalized_spo + qualifiers)
```

注意：`node_labels` 不参与 `node_id`。同一个对象在不同上下文中可能被抽取为 entity、fact subject、event participant 或 tool reference，但只要 canonical fingerprint 对齐，就应该尽量复用同一个共享节点。节点在某条超边里的功能由 `HyperEdge.roles` 表达，而不是靠创建不同节点表达。

### 3.1 实体别名对齐先于 ID 生成

实体标签节点复用前必须先做轻量级别名对齐。系统不应在模型抽取到一个新实体名称后立刻 hash 创建新节点。

推荐流程：

```text
模型抽取实体名称
  -> 规范化 name / aliases
  -> 在已有带 entity 标签或 alias 记录的 MemoryNode 池中检索
  -> 命中 canonical_name / display_name / aliases:
       复用已有 node_id
     未命中:
       用当前 canonical_name 生成新的 fingerprint / node_id
```

第一版可以只做轻量字符串匹配：

- 大小写归一。
- 去除多余空格和标点。
- 匹配 `canonical_name`。
- 匹配 `display_name`。
- 匹配 `aliases`。
- 可选使用 `entity_type` 限制候选范围。

在统一 `MemoryNode` schema 下，实体没有必要使用独立于 `node_id` 的主键。`entity_alias_index` 应把别名映射到共享 `node_id`。实体节点需要区分系统 ID 和名称字段：

```python
{
    "node_id": "node:...",
    "node_labels": ["entity"],
    "canonical_name": "Andrew",
    "display_name": "Andrew",
    "aliases": ["Andy"],
    "entity_type": "person"
}
```

其中 `canonical_name` 和 `aliases` 可以由模型辅助抽取，但 `node_id` 必须由系统在别名对齐后复用或生成。

## 4. 高阶边

`HyperEdge` 表示一条具体高阶关系实例。它不再属于预定义 view，而是由 `edge_type`、`relation`、成员节点和成员角色描述。`HyperEdge` 应尽量保守维护，不因为成员重叠或文本相似就直接合并。

示例：

```python
{
    "edge_id": "edge:...",
    "edge_type": "state",
    "relation": "describes_entity_state",
    "edge_fingerprint": "sha256:...",
    "description": "Andrew's pet profile around Toby.",
    "member_policy": "appendable",
    "member_signature": "sha256:...",
    "member_version": 3,
    "node_ids": [
        "entity:andrew",
        "fact:andrew_has_pet_toby",
        "event:andrew_mentions_toby"
    ],
    "roles": {
        "entity:andrew": "subject",
        "fact:andrew_has_pet_toby": "state_fact",
        "event:andrew_mentions_toby": "evidence"
    }
}
```

关键点：

- 超边连接共享节点池中的节点。
- 同一节点可以挂到多个超边。
- 超边可以保存成员角色。
- 超边可以是证据、状态、时间、修正、聚合、任务等不同语义关系，但这些类型不是固定视角表。
- `edge_id` 由系统生成的 `edge_fingerprint` 生成；成员集合变化不改变 `edge_id`。
- `member_policy` 可以是 `immutable`、`appendable` 或 `versioned`，只控制成员更新方式，不控制 ID 生成方式。

### 4.1 保守超边与 EdgeCluster

不建议把成员子集、近子集或描述相似作为直接合并 HyperEdge 的依据。两个超边成员高度重叠，也可能表达对立关系，例如“某实体属于某类别”和“某实体不再属于某类别”。直接合并会造成灾难性语义混合。

更稳的策略是引入上层 `EdgeCluster`：

```text
MemoryNode
  基础共享节点

HyperEdge
  具体高阶关系实例，保守创建和维护

EdgeCluster
  多条相关 HyperEdge 的聚合对象
```

`HyperEdge` 保留具体证据、成员、角色、时间和状态；`EdgeCluster` 负责把相关边组织到一起，允许同簇内存在支持、补充、更新或冲突关系。

示例：

```python
{
    "cluster_id": "cluster:...",
    "canonical_description": "Toby's species and pet status.",
    "cluster_labels": ["entity_state", "pet_profile"],
    "conflict_state": "contains_conflict",
    "edge_ids": ["edge:001", "edge:002"],
    "relations": {
        "edge:001": "supports",
        "edge:002": "contradicts"
    }
}
```

### 4.2 统一超边 ID 策略

`edge_id` 仍然应保持稳定，但不建议由动态主题名直接决定，也不建议由成员集合直接决定。推荐使用保守的 `edge_fingerprint`：

```text
edge_id = hash(namespace + edge_fingerprint)
```

`edge_fingerprint` 可以由 normalized relation、polarity、roles、members、source hint、time hint 等生成，但合并必须保守。成员集合只用于签名和版本：

```text
member_signature = hash(sorted(member_node_ids + roles))
member_version = member_version + 1 when membership changes
```

当成员变化时：

```text
edge_id 不变
node_ids / roles 更新
member_signature 更新
member_version 增加
updated_at 更新
```

这样无论边是一次性证据边，还是长期增长的聚合边，都使用同一套 ID 管理方式。

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

成员子集、近子集和高重叠率只能作为召回信号，不能单独决定合并。

## 5. 一次抽取，系统组装

不建议让同一段上下文反复经过多个 prompt，分别抽取实体、事实、局部图谱和高阶关系。这样会增加成本、延迟和不一致风险。

更好的方式是：

```text
原始上下文
  -> LLM 进行一次紧凑语义抽取
  -> 输出 entities / events / assertions / sources
  -> 系统生成共享节点
  -> 系统构建局部图谱
  -> 系统构建或更新 HyperEdges
```

面向 LLM 的 prompt 不应提及：

- 超图
- 高阶边
- 视角
- view
- 节点权重
- 边权重
- 置信度

LLM 只需要自然地抽取实体、事件、断言和来源片段。结构组装由系统完成。不要让同一信息同时出现在 facts、attributes 和 triples 中；第一版用 `assertions` 作为事实、属性和三元组的统一承载字段。

推荐最小输出形态：

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

## 6. 复合节点

某些节点不只是简单标识符，而是可以挂载一个局部知识结构。这个节点在外层高阶关联中仍然是一个节点，但节点内部可以保存三元组集合或小型知识图谱。

也就是说：

```text
外层:
  HyperEdges 连接多个 memory nodes

内层:
  某个 memory node 自身挂载 triples / local graph
```

### 6.1 为什么需要复合节点

高阶边适合表达多个记忆对象之间的整体关联，但有些细节更适合放在节点内部：

- 实体属性，例如职业、家庭成员、宠物、所在地。
- 事件角色，例如谁发起、谁参与、发生在哪里、结果是什么。
- 事实语义分解，例如 subject、predicate、object、condition、polarity。
- 局部因果或状态变化，例如“旧状态 -> 新状态”。
- 某个事件内部的多角色关系。

如果所有细节都提升为外层高阶边，外层结构会过密；如果完全不保存这些细节，节点又会太像纯文本块。因此引入复合节点：外层保留高阶关联，内层保存局部语义结构。

### 6.2 复合节点示例

一个带有 `event` 标签的 `MemoryNode` 可以挂载事件内部角色图：

```python
{
    "node_id": "event:john_campaign_visit",
    "node_labels": ["event"],
    "summary": "John visited a veterans hospital and reflected on public service.",
    "event_time": "2023-07-17",
    "local_graph": {
        "triples": [
            ["John", "visited", "veterans hospital"],
            ["John", "heard_story_from", "Samuel"],
            ["Samuel", "is_a", "elderly veteran"],
            ["visit", "reinforced_goal", "join the military"],
            ["visit", "related_to", "public service"]
        ],
        "roles": {
            "John": "agent",
            "Samuel": "source_person",
            "veterans hospital": "location"
        }
    }
}
```

一个带有 `fact` 标签的 `MemoryNode` 可以挂载语义三元组：

```python
{
    "node_id": "fact:alice_prefers_morning_interviews",
    "node_labels": ["fact"],
    "content": "Alice prefers morning interviews.",
    "local_graph": {
        "triples": [
            ["Alice", "prefers", "morning interviews"],
            ["morning interviews", "is_a", "interview_schedule"]
        ],
        "attributes": {
            "polarity": "positive"
        }
    }
}
```

一个带有 `entity` 标签的 `MemoryNode` 可以挂载属性子图：

```python
{
    "node_id": "entity:andrew",
    "node_labels": ["entity"],
    "canonical_name": "Andrew",
    "local_graph": {
        "triples": [
            ["Andrew", "has_pet", "Toby"],
            ["Toby", "is_a", "cat"]
        ],
        "attributes": {
            "entity_type": "person"
        }
    }
}
```

### 6.3 外层高阶边与内层子图的分工

外层 `HyperEdge` 负责：

- 把多个记忆节点组织成一个高阶关联单元。
- 表达跨事件、跨时间、跨主体的关联。
- 让同一个事实节点被不同关系单元共享。

内层 `LocalNodeGraph` 负责：

- 描述节点自身的语义结构。
- 保存属性、角色、三元组和局部状态。
- 为外层高阶边提供更精细的语义支撑。

`LocalNodeGraph` 对所有节点保持统一结构。差异不通过三套内部图谱 schema 表达，而通过 `node_labels`、节点属性和 triple 内容表达。

局部三元组可以携带高阶关系上下文，用来在检索回上下文时隔离语义边界。这个上下文不参与 `node_id`，也不参与 `triple_id`，只作为解释、过滤和排序时的 qualifier / metadata。

例如：

```python
{
    "subject": "Alice",
    "predicate": "has_role",
    "object": "employee",
    "qualifiers": {
        "scope_edge_id": "edge:coworker_relation",
        "scope_cluster_id": "cluster:company_x_work_context",
        "role_in_edge": "employee",
        "edge_relation": "employment"
    }
}
```

这样同一个 Alice 节点可以在“同事关系”超边中是 `employee`，在“诉讼关系”超边中是 `plaintiff`。检索组织上下文时可以按 `scope_edge_id` / `scope_cluster_id` 隔离语义边界，但不会因此改变节点或三元组的 ID 策略。

示例：

```text
外层 HyperEdge:
  edge:state:andrew_pet_profile
    -> {entity:andrew, fact:andrew_has_pet_toby, event:andrew_mentions_toby}

内层 entity:andrew.local_graph:
  Andrew --has_pet--> Toby
  Toby --is_a--> cat
```

## 7. 双时间指标

一个节点需要同时保存两类时间指标：

- 绝对时间：真实世界时间戳或有效期。
- 相对时间：这条记忆在系统对话轮次中的生命周期和激活状态。

这两类时间不要混用。事件节点可以描述很久以前发生的事，但刚刚被写入；某个事实也可以很久没有被访问，但在真实世界中仍然有效。

### 7.1 绝对时间

绝对时间表示事件在真实世界中发生或生效的时间。

典型字段：

```python
{
    "event_time": "2023-07-11",
    "valid_time": {"start": "2023-07-11", "end": null, "as_of": "2023-09-01"},
    "source_timestamp": "2023-07-11T10:15:00"
}
```

用途：

- 真实时间线。
- 事实有效期。
- 状态变化顺序。

带有 `event` 标签的节点必须尽量保存绝对时间。带有 `fact` 标签的节点如果描述状态、偏好、计划或事件，也应尽量继承或抽取绝对时间。未来带有 `tool` 标签的节点可以使用 tool call timestamp 或 tool result timestamp 作为来源时间。

### 7.2 相对时间

相对时间表示记忆写入、更新或访问与当前对话轮次之间的关系。

典型字段：

```python
{
    "created_turn": 17,
    "inserted_turn": 17,
    "updated_turn": 21,
    "last_access_turn": 24,
    "access_count": 3
}
```

用途：

- 记忆新鲜度衰减。
- 激活强度。
- 遗忘或压缩策略。

`turn_distance` 和 `decay_weight` 不建议作为永久权威字段保存，因为它们依赖当前对话轮次。更合理的做法是按需计算：

```text
turn_distance = current_turn - inserted_turn
decay_weight = exp(-decay_lambda * turn_distance)
```

如果为了调试或加速需要保存，也应作为 cache，而不是事实本身。

### 7.3 时间挂载位置

更合理的策略是分层挂载，而不是只挂在外层结构或只挂在局部图谱：

```text
节点级时间:
  描述这个记忆节点自身的真实世界时间、系统生命周期和访问激活状态

超边级时间:
  描述这组节点的关联何时形成、何时更新、是否有真实世界有效期

局部图谱时间:
  描述复合节点内部某条三元组或属性关系在什么时间成立
```

因此：

- 所有 `MemoryNode` 都应保存节点级时间；不同 `node_labels` 是否要求 world time 由配置决定。
- `HyperEdge` 可以保存边级时间，尤其是状态、任务、修正、聚合这类关系本身会变化的边。
- `LocalNodeGraph` 中的三元组可以保存 `valid_time`、`source_event_id` 等 qualifier，但不应复制整个节点生命周期。

### 7.4 自动更新规则

节点时间建议拆成三类：

```text
world time:
  真实世界时间，例如 event_time、valid_time、source_timestamp

lifecycle time:
  系统生命周期时间，例如 created_at、inserted_at、updated_at、deleted_at

activation time:
  相对对话轮次，例如 created_turn、inserted_turn、updated_turn、last_access_turn、access_count
```

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

超边也应保存自己的 lifecycle / activation 时间，以便成员追加时保留历史访问权重和调试信息。

## 8. 当前结构摘要

当前构思不依赖显式多视角，而是采用更轻的共享节点 + 稳定超边：

```text
Memory = MemoryNodes + HyperEdges + EdgeClusters + LocalNodeGraphs
```

其中：

- `MemoryNodes` 是长期记忆的共享节点池，具体语义标签由配置中的 `node_labels` 决定。
- `HyperEdges` 是具体高阶关系实例，保守维护，不轻易合并。
- `EdgeClusters` 是相关超边的聚合对象，用来承接主题漂移、近似重复、更新和冲突。
- `LocalNodeGraphs` 是复合节点内部挂载的三元组集合或小型知识图谱。

外层结构表达“这些记忆为什么应该被一起看”；内层结构表达“这个节点自身到底包含哪些语义、属性和角色”。
