# Phase 6 Scope: Human Review Queue

## 目标

Phase 5 已经能生成多类候选：

1. L2 pending atom。
2. promotion/demotion lifecycle candidate。
3. semantic duplicate candidate graph edge。
4. observing L3 scene。

这些候选都不应该被系统自动处置。Phase 6A 的目标是补齐 human-in-loop 审查入口：统一列出候选，并允许人工标记 `pending` / `approved` / `rejected`。

## 当前能力

### Review Queue

新增 CLI：

```bash
.venv/bin/python -m altm.cli review-queue \
  --db /tmp/altm.sqlite3 \
  --limit 100
```

可按类型过滤：

```bash
.venv/bin/python -m altm.cli review-queue \
  --db /tmp/altm.sqlite3 \
  --kind semantic_duplicate_candidate
```

支持的 review item kind：

```text
l2_pending
promotion_candidate
demotion_candidate
semantic_duplicate_candidate
l3_observing
```

### Review Mark

新增 CLI：

```bash
.venv/bin/python -m altm.cli review-mark \
  --db /tmp/altm.sqlite3 \
  --target-type memory_unit \
  --target-id l2_xxx \
  --kind l2_pending \
  --status approved \
  --note "verified by user"
```

graph edge candidate：

```bash
.venv/bin/python -m altm.cli review-mark \
  --db /tmp/altm.sqlite3 \
  --target-type graph_edge \
  --target-id graph_edge_xxx \
  --status rejected
```

MCP 新增：

```text
memory_review_queue(kind, include_reviewed, limit)
memory_review_mark(target_type, target_id, status, kind, note)
```

`kind` 用于区分同一个 MemoryUnit 上的不同审查事项。例如某个 L2 既是 `l2_pending`，又是 `promotion_candidate`，审批长期晋升时应传入 `kind=promotion_candidate`。

### MCP Management Tools

Phase 6B 新增管理型 MCP 工具：

```text
memory_index_embeddings(limit)
memory_govern_lifecycle(limit, layer)
memory_semantic_dedup(model, limit, threshold)
```

这些工具沿用已有 CLI 能力，定位是“显式触发索引、评分和候选生成”：

1. `memory_index_embeddings` 读取 `ALTM_EMBEDDING_*` 环境变量，写入或刷新 `memory_embeddings`。
2. `memory_govern_lifecycle` 运行 residentScore 周期，标记 promotion/demotion candidate。
3. `memory_semantic_dedup` 读取缓存 embedding，写入 `semantic_duplicate_candidate` graph edge。

Phase 6B 仍不自动执行 destructive 或不可逆动作。

### Review Action Plan

Phase 6C 新增只读执行计划预览：

```bash
.venv/bin/python -m altm.cli review-plan \
  --db /tmp/altm.sqlite3 \
  --limit 100
```

默认只为 `approved` review item 生成 action plan；如需查看 rejected 的后续建议：

```bash
.venv/bin/python -m altm.cli review-plan \
  --db /tmp/altm.sqlite3 \
  --include-rejected
```

MCP 新增：

```text
memory_review_plan(include_rejected, limit)
```

`review-plan` 输出：

1. `action_type`：建议动作，例如 `promote_to_long`、`prepare_duplicate_resolution`。
2. `risk`：`low` / `medium` / `high`。
3. `requires_second_confirmation`：是否必须二次确认。
4. `proposed_changes`：拟变更字段。
5. `source_memory_ids`：涉及的证据或候选记忆。

Phase 6C 是 dry-run only，不修改数据库。

### Review Apply

Phase 6D 新增受保护执行器：

```bash
.venv/bin/python -m altm.cli review-apply \
  --db /tmp/altm.sqlite3 \
  --plan-id review_action_xxx
```

不带 `--confirm` 时只返回 dry-run 结果，不修改数据库。真正执行需要：

```bash
.venv/bin/python -m altm.cli review-apply \
  --db /tmp/altm.sqlite3 \
  --plan-id review_action_xxx \
  --confirm
```

如果 action plan 标记了 `requires_second_confirmation=true`，还必须传入：

```bash
--second-confirm
```

MCP 新增：

```text
memory_review_apply(plan_id, confirm, second_confirm)
```

当前允许执行的都是受保护动作：

1. promotion：可将 approved promotion candidate 转为 `lifecycle_state=long`，并清除 promotion marker。
2. demotion：只进入 observing，不做 compression/tombstone。
3. L3 activation：只把 reviewed L3 scene 转为 active。
4. duplicate resolution：只标记 `pending_canonical_selection`，不合并、不 tombstone。
5. rejected candidate：只记录 action applied，不做物理删除。

### Review Events

Phase 6E 新增 append-only 审计事件：

```bash
.venv/bin/python -m altm.cli review-events \
  --db /tmp/altm.sqlite3 \
  --limit 100
```

可按目标过滤：

```bash
.venv/bin/python -m altm.cli review-events \
  --db /tmp/altm.sqlite3 \
  --target-type memory_unit \
  --target-id l2_xxx
```

MCP 新增：

```text
memory_review_events(target_type, target_id, event_type, limit)
```

当前记录两类事件：

1. `review_mark`：人工标记 review item 状态时写入。
2. `review_apply`：确认执行成功后写入。

边界：

1. `review-plan` 是只读，不写审计事件。
2. `review-apply` dry-run 不写审计事件。
3. 缺少二次确认导致未执行时不写审计事件。
4. 审计事件不承载密钥和原始模型响应，只记录目标、计划、状态和少量结构化元数据。

### Review Audit

Phase 6F 新增只读审计汇总：

```bash
.venv/bin/python -m altm.cli review-audit \
  --db /tmp/altm.sqlite3 \
  --event-limit 1000 \
  --recent-limit 10
```

MCP 新增：

```text
memory_review_audit(event_limit, recent_limit)
```

`review-audit` 汇总：

1. review event 总数。
2. 按 event type / target type / status 的计数。
3. pending / reviewed review item 数量。
4. action plan 数量、高风险 plan 数量、需要二次确认的 plan 数量。
5. 已执行 action 数量和未执行 action plan 数量。
6. 最近若干条 review event。

边界：`review-audit` 是只读报告，不写审计事件，也不修改 MemoryUnit 或 graph edge 状态。

## 存储策略

审查状态写入现有位置：

1. L2 atom：更新 MemoryUnit metadata 的 `review_status`，同时同步对应 L2 typed table 的 `review_status`。
2. promotion/demotion/L3 observing：写入 MemoryUnit metadata 的 `governance_review_status`。
3. semantic duplicate candidate：写入 graph_edges metadata 的 `review_status`。

审计历史写入独立 `review_events` 表。该表是 append-only，用于追踪 review mark / apply 的执行轨迹。

## 非目标

Phase 6A/6B/6C/6D/6E/6F 不做：

1. 自动合并 L2 duplicate。
2. 自动 tombstone。
3. 自动把 promotion candidate 晋升为 long/permanent。
4. 自动把 demotion candidate 压缩或降级。
5. 自动把 L3 observing 变成 active/long。
6. 自动在写入链路同步调用远程 embedding。
7. 自动执行 review-plan 里的 proposed action。
8. 自动执行 duplicate merge / tombstone。
9. 审计事件回滚或压缩。
10. 审计汇总持久化。

这些动作需要后续单独确认策略。

## 验证

Phase 6A/6B/6C/6D/6E/6F 需要验证：

```text
.venv/bin/python -m compileall src tests
.venv/bin/python -m unittest discover -s tests
sqlite3 :memory: < schemas/sqlite/001_initial.sql
```

重点测试：

1. review queue 能列出 L2 pending、promotion candidate、semantic duplicate candidate、L3 observing。
2. review mark 能标记 MemoryUnit。
3. review mark 能标记 graph edge。
4. L2 review status 会同步到 MemoryUnit metadata。
5. MCP 管理工具会注册并能触发 embedding index、lifecycle governance、semantic dedup。
6. review-plan 会为 approved 候选生成 proposed action。
7. 高风险 proposed action 必须标记 `requires_second_confirmation=true`。
8. rejected 候选默认不进入 action plan，除非显式 `--include-rejected`。
9. review-apply 默认 dry-run，不带 `confirm` 不改库。
10. 需要二次确认的动作缺少 `second_confirm` 时不改库。
11. promotion apply 会进入 long lifecycle 并清除 promotion marker。
12. duplicate apply 只进入 pending canonical selection，不 tombstone 两侧 memory。
13. review-mark 会写入 `review_mark` 事件。
14. review-apply dry-run 不写事件，成功执行后写入 `review_apply` 事件。
15. review-events 能按 target 过滤审计事件。
16. review-audit 能汇总 event、review item 和 action plan 状态。

当前验证结果：

```text
.venv/bin/python -m compileall src tests
.venv/bin/python -m unittest discover -s tests
sqlite3 :memory: < schemas/sqlite/001_initial.sql

Ran 35 tests in 4.245s
OK

PHASE6_REVIEW_QUEUE_BEFORE 3
PHASE6_MARKED_MEMORY approved
PHASE6_MARKED_EDGE rejected
PHASE6_REVIEW_QUEUE_AFTER 3
PHASE6_MCP_APP FastMCP
PHASE6B_MCP_MANAGEMENT_TOOLS 3
PHASE6C_REVIEW_PLAN_COUNT 1
PHASE6C_REVIEW_PLAN_ACTION promote_to_long
PHASE6C_SECOND_CONFIRMATION True
PHASE6C_MCP_APP FastMCP
PHASE6D_DRY_RUN_APPLIED False
PHASE6D_NO_SECOND_APPLIED False
PHASE6D_APPLIED True
PHASE6D_LIFECYCLE long
PHASE6D_PROMOTION_MARKER None
PHASE6D_MCP_APP FastMCP
PHASE6E_EVENTS_AFTER_MARK 1
PHASE6E_EVENTS_AFTER_DRY_RUN 1
PHASE6E_EVENTS_AFTER_NO_SECOND 1
PHASE6E_EVENTS_AFTER_APPLY 2
PHASE6E_EVENT_TYPES ['review_mark', 'review_apply']
PHASE6E_MCP_APP FastMCP
PHASE6F_TOTAL_EVENTS 2
PHASE6F_EVENT_TYPES {'review_mark': 1, 'review_apply': 1}
PHASE6F_APPLIED_ACTIONS 1
PHASE6F_SECOND_CONFIRMATION 1
PHASE6F_RECENT_EVENTS 2
PHASE6F_MCP_APP FastMCP
```
