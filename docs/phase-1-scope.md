# Phase 1 Scope: 契约与骨架

## 目标

建立一个可演进的最小工程框架，让后续能力可以按确认门逐步落地：

1. L0/L1/L2 基础闭环。
2. SQLite + FTS5 本地检索。
3. 证据链和 fallback locator。
4. 生命周期分数和治理事件。
5. Retrieval -> Context Gateway 的接口边界。

## 非目标

本阶段不做：

1. 真实 LLM 摘要和事实抽取。
2. 真实向量索引和 embedding。
3. PPR、RRF、cross encoder rerank。
4. L3/L4 自动蒸馏。
5. Headroom 深度压缩。
6. hard delete。

## 成功标准

1. 工程目录能表达最终架构的模块边界。
2. 核心数据契约能覆盖 L0-L4、生命周期、证据链、召回候选和上下文分带。
3. SQLite schema 能初始化，且包含 FTS5 表。
4. Python 控制面有 CLI 入口和 Store 初始化骨架。
5. TypeScript adapter 有 Node 22 声明和契约镜像。
6. 后续每个重大能力点都有明确确认门。

## 第一批可验证事项

```bash
python3 -m compileall src tests
python3 -m unittest discover -s tests
sqlite3 :memory: < schemas/sqlite/001_initial.sql
PYTHONPATH=src python3 -m altm.cli init-db --db /tmp/altm.sqlite3
```

> 当前机器 Python 为 3.9.6；项目目标为 3.11。骨架验证能在 3.9 下通过，不代表长期目标降级。
