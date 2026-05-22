# 多视角复合节点超图 Memory 构思(Composite Hypergraph Memory)

本文档只记录当前关于 memory 结构的概念构思。暂不讨论检索流程、代码组织、评测接入、配置文件和后续维护策略。

## 1. 核心想法

同一批长期记忆不只按一种结构组织，而是同时按多个“关系视角”构建不同类型的高阶关联边。

基本原则：

- 长期记忆先沉淀为一批共享节点。
- 同一个事实节点可以被多个视角复用，不必复制多份。
- 不同视角通过不同类型的高阶边组织同一批节点。
- 某些节点本身可以是复合节点，节点内部挂载三元组集合或小型知识图谱，用来补充语义、属性、角色和局部关系。

可以把整体结构理解为：

```text
共享节点池
  + 多视角高阶边
  + 部分节点内部的局部知识结构
```

## 2. 共享节点池

共享节点池保存长期记忆中的基本对象。节点只表达可复用的记忆内容，不绑定唯一组织方式。

候选节点类型：

- `TurnNode`：原始对话轮次，用于保留来源。
- `EventNode`：带真实世界时间锚点的事件、经历或会话片段。
- `FactNode`：可查询的原子事实。
- `EntityNode`：人物、地点、项目、宠物、组织等主体。
- `StateNode`：某个主体在某个时间段内的状态。
- `PreferenceNode`：长期偏好、倾向、习惯或稳定画像。
- `TaskNode`：计划、目标、任务或进度状态。

其中 `FactNode` 可以被多个视角共享。例如：

```text
fact:john_wants_to_join_military
  同时属于:
    entity_state_view
    temporal_view
    preference_profile_view
    topic_or_intent_view
```

## 3. 多视角高阶边

高阶边表示：在某个关系视角下，一组节点共同形成一个记忆单元。

```python
{
    "id": "edge:...",
    "view": "entity_state_view",
    "relation": "state_of_entity",
    "node_ids": [
        "entity:andrew",
        "fact:andrew_has_pet_toby",
        "event:andrew_mentions_toby"
    ],
    "roles": {
        "entity:andrew": "subject",
        "fact:andrew_has_pet_toby": "state_fact",
        "event:andrew_mentions_toby": "evidence_event"
    },
    "weights": {
        "entity:andrew": 1.0,
        "fact:andrew_has_pet_toby": 0.9,
        "event:andrew_mentions_toby": 0.6
    }
}
```

关键点：

- 边属于某个视角。
- 节点属于共享节点池。
- 同一节点可以挂到多个高阶边。
- 高阶边可以保存成员角色和权重。
- 不同视角允许对同一节点产生不同解释。

## 4. 候选关系视角

这些视角只是候选，后续可以增删或重新命名。

### 4.1 provenance_view

来源视角，把事实、事件和原始对话连接起来。

```text
event:S1
  -> {turn:S1:0, turn:S1:1, fact:A, fact:B}
```

用途：

- 记录事实从哪里来。
- 保留回溯原文的能力。
- 避免事实脱离来源上下文。

### 4.2 entity_state_view

主体状态视角，围绕同一实体组织属性、状态、关系和变化。

```text
entity:andrew
  -> {fact:has_pet_toby, fact:adopted_second_dog, state:pet_ownership}
```

用途：

- 主体消歧。
- 状态聚合。
- 描述某个主体在不同时间的变化。

### 4.3 temporal_view

时间视角，按真实世界时间组织事件和事实。

```text
time_bucket:2023_09
  -> {event:A, fact:B, fact:C}
```

用途：

- 保留真实时间线。
- 表达 before / after / as-of 关系。
- 组织状态变化的先后顺序。

### 4.4 topic_or_intent_view

主题或意图视角，把时间上分散但语义上相关的记忆聚合起来。

```text
topic:nate_tournaments
  -> {fact:win_1, fact:win_2, fact:win_3, entity:nate}
```

用途：

- 表达长期主题。
- 聚合分散事实。
- 支持多跳、计数、整体叙事类记忆。

### 4.5 preference_profile_view

画像视角，把多个弱证据组织成偏好、倾向或稳定判断。

```text
profile:john_us_commitment
  -> {fact:military_goal, fact:running_for_office, fact:local_service}
```

用途：

- 表达隐含偏好。
- 聚合长期稳定特征。
- 支持开放式判断。

### 4.6 task_or_plan_view

任务计划视角，围绕目标、计划、进度和完成状态组织记忆。

```text
plan:alice_job_search
  -> {fact:applied, fact:interview_scheduled, fact:offer_received}
```

用途：

- 表达持续任务。
- 记录进度变化。
- 连接计划、行动和结果。

## 5. 复合节点

某些节点不只是简单标识符，而是可以挂载一个局部知识结构。这个节点在外层超图中仍然是一个节点，但节点内部可以保存三元组集合或小型知识图谱。

也就是说：

```text
外层:
  高阶边连接多个 memory nodes

内层:
  某个 memory node 自身挂载 triples / local graph
```

### 5.1 为什么需要复合节点

高阶边适合表达多个记忆对象之间的整体关联，但有些细节更适合放在节点内部：

- 实体属性，例如职业、家庭成员、宠物、所在地。
- 事件角色，例如谁发起、谁参与、发生在哪里、结果是什么。
- 事实的语义分解，例如 subject、predicate、object、condition、polarity。
- 局部因果或状态变化，例如 “旧状态 -> 新状态”。
- 某个事件内部的多角色关系。

如果所有细节都提升为外层高阶边，外层结构会变得过密；如果完全不保存这些细节，节点又会太像纯文本块。因此引入复合节点：外层保留高阶关联，内层保存局部语义结构。

### 5.2 复合节点示例

一个 `EventNode` 可以挂载事件内部角色图：

```python
{
    "id": "event:john_campaign_visit",
    "type": "event",
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

一个 `FactNode` 可以挂载语义三元组：

```python
{
    "id": "fact:alice_prefers_morning_interviews",
    "type": "fact",
    "content": "Alice prefers morning interviews.",
    "local_graph": {
        "triples": [
            ["Alice", "prefers", "morning interviews"],
            ["morning interviews", "is_a", "interview_schedule"]
        ],
        "attributes": {
            "polarity": "positive",
            "confidence": 0.82
        }
    }
}
```

一个 `EntityNode` 可以挂载画像或属性子图：

```python
{
    "id": "entity:andrew",
    "type": "entity",
    "name": "Andrew",
    "local_graph": {
        "triples": [
            ["Andrew", "has_pet", "Toby"],
            ["Toby", "is_a", "dog"],
            ["Toby", "became_pet_on", "2023-07-11"]
        ],
        "attributes": {
            "entity_type": "person"
        }
    }
}
```

### 5.3 外层高阶边与内层子图的分工

外层高阶边负责：

- 把多个记忆节点组织成一个高阶关联单元。
- 表达跨事件、跨时间、跨主体的关系视角。
- 连接同一个事实在不同视角中的位置。

内层子图负责：

- 描述节点自身的语义结构。
- 保存属性、角色、三元组和局部状态。
- 为外层视角边提供更精细的语义支撑。

示例：

```text
外层 entity_state_view:
  entity:andrew
    -> {fact:andrew_has_pet_toby, event:andrew_mentions_toby}

内层 entity:andrew.local_graph:
  Andrew --has_pet--> Toby
  Toby --is_a--> dog
  Toby --became_pet_on--> 2023-07-11
```

## 6. 双时间指标

一个节点需要同时保存两类时间指标。

### 6.1 绝对时间

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

事件节点必须存绝对时间。事实节点如果描述状态、偏好、计划或事件，也应尽量继承或抽取绝对时间。

### 6.2 相对时间

相对时间表示这条记忆被写入后，距离当前对话轮次的间隔。

典型字段：

```python
{
    "created_turn": 17,
    "last_access_turn": 24,
    "turn_distance": 7,
    "access_count": 3,
    "decay_weight": 0.81
}
```

用途：

- 记忆新鲜度衰减。
- 激活强度。
- 遗忘策略。

一个节点可以刚刚写入但描述很久以前的事件，也可以很久没被访问但仍然在真实世界中有效。因此，衰减指标只使用相对时间；时间推理只使用绝对时间。

### 6.3 时间应该挂在哪里

更合理的策略是分层挂载，而不是只挂在外层超图或只挂在局部图谱：

```text
节点级时间:
  描述这个记忆节点自身的真实世界时间、系统生命周期和访问激活状态

高阶边时间:
  描述某个关系视角下，这组节点的关联何时形成、何时更新、是否有真实世界有效期

局部图谱时间:
  描述复合节点内部某条三元组或属性关系在什么时间成立
```

因此：

- `EventNode` / `FactNode` / `EntityNode` 等共享节点应保存节点级时间。
- `MultiViewEdge` 可以保存边级时间，尤其是 `entity_state_view`、`task_or_plan_view` 这类关系本身会随时间变化的视角。
- `LocalNodeGraph` 中的三元组可以保存 `valid_time`、`source_event_id`、`confidence` 等 qualifier，但不应复制整个节点生命周期。

### 6.4 自动更新规则

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

更新节点内容、局部图谱或视角成员:
  写入 updated_at / updated_turn
  只有真实世界事实发生变化时才更新 valid_time

检索访问:
  更新 last_access_turn 和 access_count
  不更新 updated_at
```

`turn_distance` 和 `decay_weight` 不建议作为永久权威字段保存，因为它们依赖当前对话轮次。更合理的做法是按需计算：

```text
turn_distance = current_turn - inserted_turn
decay_weight = exp(-decay_lambda * turn_distance)
```

如果为了调试或加速需要保存，也应当作为 cache，而不是事实本身。

## 7. 当前结构摘要

当前构思可以概括为：

```text
Memory = SharedNodes + MultiViewEdges + LocalNodeGraphs
```

其中：

- `SharedNodes` 是长期记忆的共享节点池。
- `MultiViewEdges` 是多个关系视角下的高阶关联。
- `LocalNodeGraphs` 是某些复合节点内部挂载的三元组集合或小型知识图谱。

外层结构表达“这些记忆为什么应该被一起看”；内层结构表达“这个节点自身到底包含哪些语义、属性和角色”。

## 8. 增量构建缓存构思

除了第一轮对话或首次构建，后续构建新的记忆结构时，可以引入增量缓存策略。

核心想法：

- 如果 system prompt 没有变化。
- 如果 memory 构建配置和 prompt template 没有变化。
- 如果上一轮已经处理过的历史上下文前缀没有变化。
- 那么本轮只读取新增上下文部分，用新增消息更新共享节点、高阶边和局部图谱。

这个判断不需要额外 LLM 调用，可以用本地 hash、版本号和 cursor 完成：

```text
system_prompt_hash
memory_config_hash
prompt_template_hash
processed_prefix_hash
last_processed_turn_index
last_processed_message_id
```

推荐判断：

```text
无缓存:
  全量构建

system prompt 改变:
  全量重建

构建配置或抽取 prompt 改变:
  重建受影响部分

历史前缀未变，仅追加新消息:
  只处理新增消息

历史前缀不匹配:
  保守全量重建
```

需要注意：只读取新增上下文，不等于只新增节点。新上下文可能纠正旧事实、补充旧事件、改变实体消歧或让旧事实挂入新的关系视角。因此增量模式仍然允许更新旧节点、旧边和旧局部图谱。

也就是说，缓存策略减少重复读取和重复抽取，但不限制图结构维护。

## 9. 读写记忆时序

真实 agent 运行时建议采用先读后写：

```text
Before answer:
  retrieve(memory, current_question)
  build reader prompt

After answer:
  add_memory(user_input, assistant_output, metadata)
```

含义：

- 回答前只读取已经存在的长期记忆。
- 回答后再把本轮 user input、assistant output 和相关 metadata 写入记忆。
- 不应在回答当前问题前把尚未生成的 assistant output 写入记忆。

`add_memory` 需要为未来真实 agent 交互预留扩展空间。除了最小的 `user_input` 和 `assistant_output`，后续还可能包含：

- tool calls
- tool results
- observations
- attachments
- environment state
- run trace
- model / policy metadata

因此 `add_memory` 不应只绑定为简单 QA 文本，而应被视为一次 agent interaction event。tool 信息不一定都进入长期事实，但可以作为 provenance、事件上下文或局部图谱的一部分。
