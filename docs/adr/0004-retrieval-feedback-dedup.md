# ADR 0004: Phase 4 组合治理与中文检索增强

状态：已确认，Phase 4 执行中。

日期：2026-06-29

## 背景

真实 DeepSeek 验证表明：LLM 接入、L2 抽取、类型表双写和 MCP 工具链已经可用，但当前 `unicode61` FTS 对中文短语子串召回不稳定。用户选择 Phase 4 “都做”，即同时推进检索增强、L2 去重、生命周期反馈和 L1 结构化表。

## 已确认决策

| 决策点 | 选择 | 实现边界 |
| --- | --- | --- |
| 阶段目标 | 全部推进 | 用最小可验证方式覆盖检索、去重、反馈和 L1 表 |
| 检索路线 | 向量优先，trigram FTS 和 jieba 兜底 | 当前先实现本地词/字 n-gram 向量，不接外部 embedding 服务 |
| 接入范围 | 统一接入 | CLI `search` 和 MCP `memory_recall` 自动使用增强召回 |
| 验证方式 | 少量真实验证 | 只跑一次真实 DeepSeek L2 抽取回归 |

## 实现策略

### 本地向量优先

本阶段的“向量”是本地 lexical vector，不是外部 embedding：

1. 英文/数字用词 token。
2. 中文用 jieba token。
3. 同时补中文 2-gram / 3-gram 字符 token。
4. 用 cosine similarity 排序。

这样可以在不引入 embedding 模型和向量库的情况下，先解决中文短语召回的 MVP 问题。

### trigram FTS

新增 `memory_units_fts_trigram`，与现有 `memory_units_fts` 并行写入。统一召回顺序：

```text
local_vector
  -> fts_trigram
  -> fts_unicode
  -> like_fallback
```

### L2 exact dedup

本阶段只做 exact dedup：同类型表中已有相同 `text` 时，不重复写入新的 L2 atom。暂不做语义合并、冲突检测、supersede。

### lifecycle feedback

新增 `feedback` CLI 和 `memory_feedback` MCP tool，写入 `lifecycle_events` 并更新：

1. `access_count`
2. `useful_access_count`
3. `last_accessed_at`

当前只有 `cited_by_agent` 和 `user_confirmed` 计为 useful access。

### L1 ContextCapsule 表

新增 `l1_context_capsules` 表，L1 仍作为 MemoryUnit 参与召回，同时将结构化 capsule 双写到独立表，便于后续 L2 抽取、审计和查询。

## 验证结果

单测覆盖：

1. 中文召回。
2. L1 capsule 表双写。
3. L2 exact dedup。
4. feedback 更新 access/useful access。

真实 DeepSeek 回归：

```text
PHASE4_REAL_SEARCH_COUNT 1
PHASE4_REAL_MATCHED_BY [['local_vector', 'fts_trigram']]
PHASE4_REAL_SUMMARIES ['我们决定提升中文检索能力，关键词是类型拆表和中文召回。']
```

## 后续确认门

1. 是否接真实 embedding 服务，替换本地 lexical vector。
2. L2 去重是否升级为 semantic dedup / conflict / supersede。
3. pending L2 是否应在召回时降权。
4. feedback 信号是否进入 residentScore 计算。
