# Vector Embedding Integration

## Problem

Phase 4 的本地 lexical vector 解决了中文短语和关键词召回，但它不是语义 embedding。用户已经提供阿里云百炼 OpenAI-compatible 向量服务，因此当前目标是把真实向量模型接入召回链路，同时不引入新的向量数据库和额外基础设施。

## Intuition

真实 embedding 适合做相似历史任务、相似决策和经验片段召回；SQLite FTS 与本地 lexical vector 仍适合精确关键词和中文短语兜底。当前实现把两类能力叠加：

```text
remote_vector -> local_vector -> fts_trigram -> fts_unicode -> like_fallback
```

这样可以先获得真实语义召回能力，又保留离线可运行和远端失败时的基础召回能力。

## Mechanism

### 配置

向量模型使用独立环境变量，不复用聊天 LLM 的 `ALTM_LLM_*`：

```bash
export ALTM_EMBEDDING_BASE_URL="https://example.com/compatible-mode/v1"
export ALTM_EMBEDDING_API_KEY="..."
export ALTM_EMBEDDING_MODEL="text-embedding-v4"
export ALTM_EMBEDDING_TIMEOUT_SECONDS="60"
```

`.env.local` 已写入本地真实配置，并被 `.gitignore` 排除。

### 缓存表

SQLite 新增 `memory_embeddings`：

```text
memory_unit_id + embedding_model -> content_hash, dimension, vector_json
```

`content_hash` 用于判断缓存是否过期。MemoryUnit 内容变化后，下一次 `index-embeddings` 会重建该条向量。

### 索引命令

```bash
.venv/bin/python -m altm.cli index-embeddings \
  --db /tmp/altm.sqlite3 \
  --limit 100
```

该命令会：

1. 找出缺失 embedding 或 `content_hash` 已变化的 MemoryUnit。
2. 批量调用 OpenAI-compatible `/embeddings`。
3. 将向量写入 `memory_embeddings`。
4. 输出本次索引数量和 memory ids，不输出密钥。

## Algorithm Flow

1. `OpenAICompatibleEmbeddingClient` 根据 `ALTM_EMBEDDING_BASE_URL` 调用 `/embeddings`。
2. `EmbeddingIndexer` 将 MemoryUnit 的 `summary`、`content` 和字符串 metadata 拼成 embedding text。
3. `SQLiteMemoryStore.put_memory_embedding` 缓存向量。
4. `FTSRetrievalEngine` 在配置完整时创建 `RemoteVectorRetriever`。
5. 查询时先将 query embed 成向量，再在 SQLite 中对缓存向量做 cosine similarity。
6. remote 命中与本地 lexical/FTS 命中融合，`matched_by` 标记 `remote_vector`。

## Design Trade-offs

1. 当前不接向量数据库：避免在验证阶段引入额外服务，先用 SQLite JSON 缓存和内存 cosine 完成端到端闭环。
2. 当前不自动边写边索引：避免每次 L0/L1/L2 写入都阻塞在远程模型请求上，先由显式 `index-embeddings` 管理成本和失败边界。
3. MCP 只暴露显式索引工具：`memory_index_embeddings` 复用 CLI 的成本和失败边界，不在写入链路自动调用远程模型。
4. 远程失败走本地回退：召回链路优先保持可用性，真正的连通性和索引错误由 `index-embeddings` 显式暴露。

## Failure Modes

1. 环境变量不完整：`search` 和 MCP `memory_recall` 只使用本地召回；`index-embeddings` / `memory_index_embeddings` 会直接失败并提示缺失变量。
2. 远程 embedding 请求失败：`search` 和 MCP `memory_recall` 回退到本地召回；`index-embeddings` / `memory_index_embeddings` 失败，不写入不完整缓存。
3. 缓存为空：`remote_vector` 不产生候选，本地召回继续工作。
4. 模型切换：缓存主键包含 `embedding_model`，不同模型的向量不会互相覆盖。
5. 大库性能：当前 SQLite JSON + Python cosine 是 MVP，数据量扩大后需要确认是否接 sqlite-vec、FAISS、Milvus 或其他向量后端。

## Validation

已覆盖的验证类型：

1. fake OpenAI-compatible embedding server。
2. `/embeddings` 路径拼接和 Bearer 鉴权头。
3. `memory_embeddings` 缓存写入与 cosine recall。
4. `remote_vector` 与本地召回融合。
5. 远程 embedding 失败时的本地回退。
6. MCP `memory_index_embeddings` 能显式刷新 embedding 缓存。

真实服务连通性验证使用用户提供的 OpenAI-compatible endpoint，模型为 `text-embedding-v4`，返回向量维度为 1024。

当前验证结果：

```text
.venv/bin/python -m compileall src tests
.venv/bin/python -m unittest discover -s tests
sqlite3 :memory: < schemas/sqlite/001_initial.sql

Ran 13 tests in 2.387s
OK

REAL_EMBEDDING_MODEL text-embedding-v4
REAL_EMBEDDING_DIMENSION 1024
CLI_INDEXED_COUNT 1
CLI_SEARCH_COUNT 1
CLI_MATCHED_BY [['remote_vector', 'local_vector']]
```

补充说明：`ruff` 与 `pyright` 是 `dev` optional dependency，当前 `.venv` 未安装，因此本次未执行静态 lint/type check。
