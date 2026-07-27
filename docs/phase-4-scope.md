# Phase 4 Scope: 检索增强、反馈信号与最小治理

## 目标

Phase 4 同时推进四个最小能力：

1. 中文召回增强：本地 lexical vector 优先，trigram FTS 和 LIKE fallback 补位。
2. L2 exact dedup：避免真实模型重复抽取完全相同的 L2 文本。
3. feedback 信号：记录 `candidate_hit`、`injected`、`cited_by_agent`、`user_confirmed`、`user_rejected`。
4. L1 ContextCapsule 表：将 L1 JSON content 同步拆到结构化表。

## 非目标

本阶段不做：

1. 外部 embedding 模型。
2. 向量数据库。
3. 语义去重。
4. conflict/supersede 图边。
5. residentScore 计算和晋升降级。

## 新增能力

### 统一召回

`FTSRetrievalEngine` 现在合并多个通道：

```text
local_vector -> fts_trigram -> fts_unicode -> like_fallback
```

`matched_by` 会标明命中的通道，例如：

```text
['local_vector', 'fts_trigram']
```

### CLI feedback

```bash
.venv/bin/python -m altm.cli feedback \
  --db /tmp/altm.sqlite3 \
  --memory-id l2_xxx \
  --signal user_confirmed
```

### MCP feedback

```text
memory_feedback(memory_id: str, signal: str)
```

## 验证结果

已通过：

```text
.venv/bin/python -m compileall src tests
.venv/bin/python -m unittest discover -s tests
sqlite3 :memory: < schemas/sqlite/001_initial.sql
```

单测数量：

```text
9 tests OK
```

真实模型中文召回回归：

```text
PHASE4_REAL_SEARCH_COUNT 1
PHASE4_REAL_MATCHED_BY [['local_vector', 'fts_trigram']]
PHASE4_REAL_IDS ['l2_690a24b51380876e4b9c7fca']
PHASE4_REAL_SUMMARIES ['我们决定提升中文检索能力，关键词是类型拆表和中文召回。']
```

## 已知边界

当前 local vector 是 lexical vector，不是语义 embedding。它能解决中文短语和关键词召回问题，但不能替代真正的语义相似度检索。下一阶段如果要进入“经验涌现”和相似历史任务召回，应接真实 embedding 服务或本地 embedding 模型。
