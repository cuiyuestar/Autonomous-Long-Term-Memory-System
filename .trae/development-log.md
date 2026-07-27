# 开发日志

## 2026-07-05 - [架构优化日志] Phase 7 L2 自动语义合并风险收敛

- 目标：收敛 Phase 7 L2 自动合并/tombstone 的误合并、半事务、审计不足、canonical 选择机械、缺少 dry-run 与回滚能力等风险。
- 影响范围：`src/altm/governance/semantic_dedup.py`、`src/altm/storage/sqlite_store.py`、`src/altm/application.py`、CLI、MCP 管理工具、Phase 7 文档和语义去重测试。
- 关键决策：保留用户选择的高相似 L2 自动合并方向，但把执行条件升级为受控自动治理；候选发现和破坏性执行分层，人工 rejected / 已 resolved graph edge 不允许被候选刷新覆盖。
- 实现摘要：新增 `semantic-dedup` 的 `auto|mark-only|auto-merge|auto-tombstone` 模式与 `dry-run`；自动合并前增加高自动阈值、approved review、结构化字段一致、否定/强弱约束一致、风险 token 一致、`task_state` / `temporal_fact` 人工治理等保护门；canonical 选择改为 review、证据质量、resident score、有效访问、保护等级和置信度综合评分；合并执行改为单 SQLite 事务；新增自动合并、tombstone、回滚审计事件；新增 CLI `restore-semantic-merge` 与 MCP `memory_restore_semantic_merge`。
- 验证结果：`.venv/bin/python -m compileall src tests` 通过；`.venv/bin/python -m unittest discover -s tests` 通过，78 项测试 OK。
- 后续风险：当前结构化等价门仍是规则式防护，不能完全替代事实级 entailment/contradiction 判定；真实数据量增大后，pairwise embedding 比较仍需要分桶、增量索引或近邻检索优化。
