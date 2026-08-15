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

DeepSeek Harness 插件的全局 Memory 面板提供 `Embedding 配置` 子页。用户填写 OpenAI-compatible Base URL、模型名和 API Key 后，ALTM 先调用真实 `/embeddings` 验证配置，成功后再原子写入 `<database>.embedding.json`。该文件权限为 `0600`，状态接口不返回 API Key，MCP 与 Worker 在每次操作时重新读取，因此无需重启。

未使用 Harness UI 时，向量模型仍可通过独立环境变量配置，不复用聊天 LLM 的 `ALTM_LLM_*`：

```bash
export ALTM_EMBEDDING_BASE_URL="https://example.com/compatible-mode/v1"
export ALTM_EMBEDDING_API_KEY="..."
export ALTM_EMBEDDING_MODEL="text-embedding-v4"
export ALTM_EMBEDDING_TIMEOUT_SECONDS="60"
export ALTM_EMBEDDING_BATCH_SIZE="10"
export ALTM_EMBEDDING_MAX_RETRIES="3"
export ALTM_EMBEDDING_RETRY_DELAY_SECONDS="0.5"
```

托管配置完整时优先于环境变量。可通过 `ALTM_EMBEDDING_CONFIG_PATH` 指定托管文件位置。

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
2. 按配置的批大小调用 OpenAI-compatible `/embeddings`。
3. 将向量写入 `memory_embeddings`。
4. 输出本次索引数量和 memory ids，不输出密钥。

## Algorithm Flow

1. `OpenAICompatibleEmbeddingClient` 根据 `ALTM_EMBEDDING_BASE_URL` 调用 `/embeddings`，按 `ALTM_EMBEDDING_BATCH_SIZE` 分批并保持输入顺序。
2. `EmbeddingIndexer` 将 MemoryUnit 的 `summary`、`content` 和字符串 metadata 拼成 embedding text。
3. `SQLiteMemoryStore.put_memory_embedding` 缓存向量。
4. `FTSRetrievalEngine` 在配置完整时创建 `RemoteVectorRetriever`。
5. 查询时先将 query embed 成向量，再在 SQLite 中对缓存向量做 cosine similarity。
6. remote 命中与本地 lexical/FTS 命中融合，`matched_by` 标记 `remote_vector`。

## Design Trade-offs

1. 当前不接向量数据库：避免在验证阶段引入额外服务，先用 SQLite JSON 缓存和内存 cosine 完成端到端闭环。
2. 索引不阻塞 L0/L1/L2 写入：持久化 Worker 队列在 `extract_l2` 后异步执行 `index_embeddings`，显式 CLI/MCP 索引仍可用于补建和诊断。
3. 配置保存与索引分离：保存只执行一个连通性向量请求，不在浏览器请求中同步回填历史记忆。
4. 远程失败走本地回退：召回链路优先保持可用性，真正的连通性和索引错误由 `index-embeddings` 显式暴露。
5. 瞬时传输错误、HTTP 429 和 5xx 按配置执行指数退避；鉴权、额度和请求参数等 4xx 业务错误立即失败。

## Failure Modes

1. 托管配置和环境变量均不完整：召回只使用本地路径；Worker 索引任务按持久化重试策略失败，显式索引直接报告缺失配置。
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
7. 托管配置保存前真实验证、权限为 `0600`、状态响应不含密钥，并在不重启的情况下被后续索引读取。

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
