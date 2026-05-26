# C-HyperMem: Composite Hypergraph Memory for Long-Term Conversational Agent Reasoning

## 快速开始

```powershell
git clone https://github.com/EvannZhongg/C-HyperMem.git
cd C-HyperMem
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -e ".[llms,embeddings,vector,dev]"
```

如果需要启用 `retrieval.query_analysis: nlp`，再单独安装 NLP 依赖和 spaCy 英文模型：

```powershell
pip install -e ".[nlp]"
python -m pip install https://github.com/explosion/spacy-models/releases/download/en_core_web_sm-3.8.0/en_core_web_sm-3.8.0-py3-none-any.whl --target models/en_core_web_sm --no-deps
```

`configs/models.yaml` 中的 `nlp.model_path` 默认是 `models/en_core_web_sm`。代码会按当前工作目录解析相对路径；默认从 `C-HyperMem` 根目录启动时，上面的命令会把模型安装到项目内的 `models/en_core_web_sm`，并能被 `retrieval.query_analysis: nlp` 加载。如果你希望使用当前 Python 环境里的 spaCy 模型包，也可以把 `nlp.model_path` 改成 `en_core_web_sm`，然后运行 `python -m spacy download en_core_web_sm`。

在 `C-HyperMem\.env` 中配置模型环境变量，例如：

```powershell
CHYPERMEM_LLM_MODEL=...
CHYPERMEM_LLM_BASE_URL=...
CHYPERMEM_LLM_API_KEY=...
CHYPERMEM_EMBEDDING_MODEL=...
CHYPERMEM_EMBEDDING_BASE_URL=...
CHYPERMEM_EMBEDDING_API_KEY=...
```

## Git 后续提交操作流程

假设你的本地仓库已经和远程仓库关联好了（即 `origin/main` 已经设置）。

```powershell
git status  # 看哪些文件被修改了、哪些文件还没被 Git 跟踪。
git add .  # 添加所有修改的文件
git commit -m "简短清晰的提交说明"  # 交说明最好说明“做了什么改动”
git push origin main  # 推送到仓库
```

如果多人协作，先拉取远程更新再推送：

```powershell
git pull origin main --rebase  # `--rebase` 可以避免多余的合并提交，让历史更干净。
git push origin main  # 推送到仓库
```
