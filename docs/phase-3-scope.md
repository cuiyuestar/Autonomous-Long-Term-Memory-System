# Phase 3 Scope: 真实 L2 抽取闭环

## 目标

Phase 3 建立 L2 原子事实的最小真实闭环：

1. 从 L1 ContextCapsule 调用 OpenAI-compatible LLM 抽取 L2 atoms。
2. 将 L2 同时写入 MemoryUnit 和按类型拆分的 L2 表。
3. L2 默认 `review_status=pending`。
4. FTS recall 支持 layer/session/status 过滤。
5. CLI 和 MCP 暴露 L2 抽取能力。
6. LLM 调用失败时不写 L2。

## 非目标

本阶段不做：

1. L2 去重、合并、冲突检测。
2. L2 人工审批界面。
3. L2 晋升长期记忆。
4. L3 场景聚类。
5. L4 画像写入。
6. 向量检索和 PPR。

## 环境变量

```bash
export ALTM_LLM_BASE_URL="https://example.com/v1"
export ALTM_LLM_API_KEY="..."
export ALTM_LLM_MODEL="your-model"
export ALTM_LLM_TIMEOUT_SECONDS="60"
```

## CLI 验证路径

```bash
.venv/bin/python -m altm.cli init-db --db /tmp/altm.sqlite3
.venv/bin/python -m altm.cli capture \
  --db /tmp/altm.sqlite3 \
  --session-id demo \
  --message-id u1 \
  --role user \
  --content "我们决定采用 OpenAI 兼容接口进行真实 L2 抽取。"
.venv/bin/python -m altm.cli fold-l1 \
  --db /tmp/altm.sqlite3 \
  --session-id demo
.venv/bin/python -m altm.cli extract-l2 \
  --db /tmp/altm.sqlite3 \
  --session-id demo
.venv/bin/python -m altm.cli search \
  --db /tmp/altm.sqlite3 \
  --query OpenAI \
  --layer L2 \
  --session-id demo
```

## MCP 工具

新增工具：

```text
memory_extract_l2(session_id: str)
```

更新工具：

```text
memory_recall(query, limit, layers, session_id, statuses)
```

## 当前验证

已通过：

```text
python -m compileall src tests
python -m unittest discover -s tests
sqlite3 :memory: < schemas/sqlite/001_initial.sql
CLI fake OpenAI-compatible L2 extraction
MCP stdio fake OpenAI-compatible L2 extraction
CLI real DeepSeek L2 extraction
MCP stdio real DeepSeek L2 extraction
```

真实模型验证细节见 `docs/real-llm-validation.md`。

## 已知边界

当前 SQLite FTS5 使用 `unicode61` tokenizer，英文关键词召回稳定，中文短语子串召回不稳定。后续需要确认中文检索路线：中文分词、trigram/ngram FTS、向量召回或混合检索。
