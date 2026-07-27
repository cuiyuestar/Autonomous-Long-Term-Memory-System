# Phase 2 Scope: L0 + L1 Mock 闭环

## 目标

让记忆系统从“契约骨架”进入“最小可运行”：

1. L0 原文消息可被 capture。
2. L0 可写入 SQLite 并进入 FTS5。
3. FTS 可召回 L0/L1 MemoryUnit。
4. 同一 session 的 L0 可折叠为规则版 L1 ContextCapsule。
5. L1 能保留到 L0 的 evidence refs 和 fallback locator。
6. MCP adapter 具备标准 SDK 入口，并已在 Python 3.11 环境完成 stdio/SSE 验证。

## 非目标

本阶段仍不做：

1. 真实 LLM 摘要。
2. L2 原子事实抽取。
3. 向量检索。
4. PPR 和图扩散召回。
5. 生命周期晋升/降级策略。
6. Headroom 压缩网关。

## 验证命令

```bash
python3 -m compileall src tests
python3 -m unittest discover -s tests
sqlite3 :memory: < schemas/sqlite/001_initial.sql

PYTHONPATH=src python3 -m altm.cli init-db --db /tmp/altm.sqlite3
PYTHONPATH=src python3 -m altm.cli capture \
  --db /tmp/altm.sqlite3 \
  --session-id demo \
  --message-id u1 \
  --role user \
  --content "我们决定采用 SQLite FTS，并需要确认 MCP 双模式。"
PYTHONPATH=src python3 -m altm.cli fold-l1 \
  --db /tmp/altm.sqlite3 \
  --session-id demo
PYTHONPATH=src python3 -m altm.cli search \
  --db /tmp/altm.sqlite3 \
  --query SQLite \
  --limit 5
```

## MCP 运行

```bash
.venv/bin/python -m altm.cli mcp-server \
  --db ./data/altm.sqlite3 \
  --transport stdio
.venv/bin/python -m altm.cli mcp-server \
  --db ./data/altm.sqlite3 \
  --transport sse
```

环境配置与验证细节见 `docs/mcp-runtime-verification.md`。
