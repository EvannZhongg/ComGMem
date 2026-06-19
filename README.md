# ComGMem

ComGMem 是一个面向长期对话 Agent 的复合超图记忆包。

它把抽取到的长期记忆保存为可复用的 `MemoryNode`，再用 description-only 的 `HyperEdge` 连接相关节点，并通过轻量 `EdgeCluster` 在检索时带出相邻上下文。节点内部的 `LocalTriple` 用来表达紧凑事实；系统负责生成所有 ID、来源追踪、时间、维护元数据和索引。

当前写入路径保持保守：

- LLM 只做一次抽取，输出 `nodes` 和 `edge_summaries`；
- 系统组装节点、局部三元组、超边、簇和来源 metadata；
- 同一节点内 subject/predicate 相同但 object 不同的 triples 会进入 `maintenance/local_triple_merge.md` 做语义路由；
- 检索融合 SQLite FTS、node content 向量、node-local-graph 向量和 HyperEdge description 向量。

## 安装

在 `ComGMem` 项目根目录执行：

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -e ".[llms,embeddings,vector]"
```

如果需要运行测试或开发检查，再安装开发依赖：

```powershell
pip install -e ".[dev]"
```

如果需要启用 `retrieval.query_analysis: nlp`，再安装 spaCy 依赖和英文模型：

```powershell
pip install -e ".[nlp]"
python -m pip install https://github.com/explosion/spacy-models/releases/download/en_core_web_sm-3.8.0/en_core_web_sm-3.8.0-py3-none-any.whl --target models/en_core_web_sm --no-deps
```

`configs/models.yaml` 默认把 `nlp.model_path` 指向 `models/en_core_web_sm`。建议从 `ComGMem` 根目录运行脚本，避免相对路径解析偏移。

## 模型配置

`configs/default.yaml` 会 include `configs/models.yaml` 和 `configs/node_labels.yaml`。默认配置会读取 `ComGMem/.env` 中的环境变量。

创建 `.env`，示例：

```powershell
COMGMEM_LLM_MODEL=your-chat-model
COMGMEM_LLM_BASE_URL=https://your-openai-compatible-endpoint/v1
COMGMEM_LLM_API_KEY=your-api-key

COMGMEM_EMBEDDING_MODEL=your-embedding-model
COMGMEM_EMBEDDING_BASE_URL=https://your-openai-compatible-endpoint/v1
COMGMEM_EMBEDDING_API_KEY=your-api-key
```

LLM 和 embedding 客户端都使用 OpenAI-compatible API。默认 SQLite 主存储路径是 `runs/comgmem/memory.sqlite3`，本地嵌入式 Qdrant 向量索引路径是 `runs/comgmem/vector_index`。

## 快速自检

配置好 `.env` 后，可以运行 quickstart 脚本做一次最小本地链路测试：

```powershell
python examples\quickstart.py
```

该脚本会真实调用 LLM 抽取、embedding、SQLite 写入、Qdrant 向量索引和检索，并输出每次模型调用的日志。脚本只写入 `runs/quickstart/`，结束时会自动删除该目录下的 SQLite 和向量索引文件。全部完成后会打印：

```text
[quickstart] all checks passed; model, embedding, storage, indexing, and retrieval configs look OK.
```

## 基础使用

```python
from comgmem import Memory

memory = Memory.from_config("configs/default.yaml")

namespace = "demo_user"
memory.reset(namespace)

memory.add_memory(
    user_input="I prefer morning interviews and I live in San Francisco.",
    assistant_output="Got it, I will remember that.",
    namespace=namespace,
    metadata={"session_id": "demo-session"},
)

results = memory.search(
    "Where does the user live and when do they prefer interviews?",
    namespace=namespace,
    top_k=5,
)

for item in results:
    print(item["content"])

memory.close()
```

`add_memory(...)` 是推荐的 Agent 写入入口。它支持 user/assistant 消息，也支持可选的 `tool_calls`、`tool_results`、`observations`、`attachments`、`trace` 和 metadata。同一次 `add_memory(...)` 中的 user/assistant 消息共享同一个 `turn_id`。

`add(...)` 是更低层的导入接口。传入字符串时会作为一条 user message 写入；传入 message dict 列表时会按顺序逐条导入。

## 检索结果

`search(...)` 返回可 JSON 序列化的字典列表。每条结果以一个 `HyperEdge` 为中心：

```python
{
    "id": "edge:...",
    "content": "memory1：...，current_turn_id=turn:1, source_turn id=turn:0\nUser -prefers- morning interviews [source_turn id=turn:0]",
    "score": 0.03,
    "metadata": {
        "edge_id": "edge:...",
        "edge_description": "...",
        "edge_nodes": [...],
        "periphery_edges": [...],
        "score_parts": {...},
    },
}
```

`content` 可以直接放进下游 reader prompt。`metadata.edge_nodes` 会包含成员节点及其 active local triples；如果核心边属于某个 `EdgeCluster`，结果还会按配置带出有限数量的 sibling edges 和 periphery nodes。

## 常用配置

- `ingestion.pass_recent_context`：是否把最近 turn 作为抽取上下文传入，当前默认配置为 `false`。
- `retrieval.query_analysis`：可设为 `false`、`"llm"` 或 `"nlp"`，默认是 `false`。
- `recall.cluster_periphery_edge_limit` / `recall.cluster_periphery_node_limit`：控制每条核心边最多带出的旁路 sibling edges 和 periphery nodes；截断前会优先保留最新 turn 的旁路上下文。
- `recall.node_triple_limit`：控制每个返回 node 最多输出多少条 active triples；当前 edge scope 或 source turn 命中的 triples 会优先保留，然后按 triple 来源 turn 越新越优先。
- `recall.include_turn_ids_in_context`：控制 `search(...)` 返回的 `content` 是否在 edge 行和 triple 行标注 `turn_ids`，默认开启；关闭后只影响上下文文本，metadata 中的来源字段仍保留。
- `node_labels.yaml`：定义 `entity`、`fact`、`state`、`preference`、`task`、`event`、`instruction` 等标签的抽取偏好。
- `maintenance.local_triples.enabled`：控制同 subject/predicate 的 triple 语义维护。若出现同 S/P 多值候选且没有维护 LLM，写入会显式失败，不做规则兜底。
- `edge_clusters.enabled`：控制是否构建由共享成员节点和 eligible local-triple anchors 形成的确定性 cluster context。

## 自定义抽取器

如果希望完全控制抽取结果，可以传入自定义 extractor。extractor 只需要返回 `MemoryExtraction`，后续 ID 生成、来源追踪、维护、存储和索引仍由 ComGMem 负责。

```python
from comgmem import Memory
from comgmem.schema import MemoryExtraction


class StaticExtractor:
    def extract(self, window, context):
        return MemoryExtraction.model_validate(
            {
                "edge_summaries": [
                    {"ref": "e1", "description": "User profile preferences."}
                ],
                "nodes": [
                    {
                        "ref": "n1",
                        "labels": ["entity", "preference"],
                        "canonical_text": "User",
                        "summaries": ["The user prefers morning interviews."],
                        "triples": [
                            {
                                "subject": "User",
                                "predicate": "prefers",
                                "object": "morning interviews",
                            }
                        ],
                        "edge_summary_refs": ["e1"],
                    }
                ],
            }
        )


memory = Memory.from_config(
    {
        "storage": {"path": "runs/demo/memory.sqlite3"},
        "index": {"use_embedding": False},
    },
    extractor=StaticExtractor(),
)
```
