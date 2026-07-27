# ADR 0005: OpenAI-compatible 真实向量模型接入

状态：已确认，执行中。

日期：2026-06-29

## 背景

Phase 4 的本地 lexical vector 只能覆盖词面相似，不能支撑“相似历史经验涌现”所需的语义相似召回。用户已提供阿里云百炼 OpenAI-compatible embedding endpoint，并要求配置和接入向量模型。

## 决策

采用 OpenAI-compatible `/embeddings` 作为当前真实向量模型接口，模型配置使用独立的 `ALTM_EMBEDDING_*` 环境变量。

当前实现不引入向量数据库，先将 embedding 缓存在 SQLite `memory_embeddings` 表中，并由 `index-embeddings` CLI 显式构建索引。

## 影响范围

1. 新增 embedding 配置契约 `EmbeddingConfig`。
2. 新增 OpenAI-compatible embedding client。
3. SQLite schema 新增 `memory_embeddings`。
4. CLI 新增 `index-embeddings`。
5. CLI `search` 和 MCP `memory_recall` 自动使用完整 embedding 配置。
6. MCP 新增 `memory_index_embeddings`，用于显式刷新 embedding 缓存。
7. 召回融合新增 `remote_vector` 通道。

## 取舍

1. 显式索引优先于写入时自动索引，避免 L0/L1/L2 写入被远程模型阻塞。
2. SQLite JSON 向量缓存优先于向量数据库，降低当前阶段部署成本。
3. 本地 lexical/FTS 回退保留，避免远程服务故障导致召回不可用。
4. MCP 索引工具保持显式触发，不随 L0/L1/L2 写入自动调用远程模型。

## 后续确认门

1. 是否引入 sqlite-vec、FAISS、Milvus 或其他向量后端。
2. 是否让 embedding 索引自动跟随写入链路。
3. 是否基于 embedding 做 conflict/supersede 或 L3 clustering。
