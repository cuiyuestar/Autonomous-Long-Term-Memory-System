# Phase 7 Scope: Emergence And Active Window

## 目标

Phase 7 将记忆召回从“被动查询”扩展到两类主动经验涌现：

1. Query-Induced Emergence Window：query 后，从直接命中的记忆沿图边扩散，召回相邻经验。
2. Global Active Window：query 前，在任务开始阶段选择一组稳定、当前值得注入的活跃记忆。

当前实现保持 `search`、`emerge` 独立；`build-context` 已默认融合 Global Active Window，可用 `--active-window-mode off` 回退到历史行为。

高风险决策矩阵见 [Phase 7 Risk Decision Matrix](phase-7-risk-decision-matrix.md)。

## Phase 7A: Query-Induced Emergence

新增 CLI：

```bash
.venv/bin/python -m altm.cli emerge \
  --db /tmp/altm.sqlite3 \
  --query SQLite \
  --layer L2 \
  --limit 5 \
  --seed-limit 8 \
  --max-hops 2
```

MCP：

```text
memory_emerge(query, limit, seed_limit, max_hops, layers, session_id, statuses)
```

边界：

1. 先用普通 retrieval 找 query entry points。
2. 再沿 graph edge 做轻量 PPR 式扩散。
3. 已被人工拒绝的 graph edge 不参与扩散。
4. 输出 `RecallCandidate`，不直接写入上下文。

## Phase 7B: Global Active Window

新增 CLI：

```bash
.venv/bin/python -m altm.cli active-window \
  --db /tmp/altm.sqlite3 \
  --session-id demo \
  --token-budget 1200 \
  --limit 5
```

可选严格会话过滤：

```bash
.venv/bin/python -m altm.cli active-window \
  --db /tmp/altm.sqlite3 \
  --session-id demo \
  --strict-session
```

MCP：

```text
memory_active_window(limit, token_budget, session_id, layers, statuses, strict_session)
```

诊断报告 CLI：

```bash
.venv/bin/python -m altm.cli active-window-report \
  --db /tmp/altm.sqlite3 \
  --session-id demo \
  --decision-limit 100 \
  --limit 5
```

诊断报告 MCP：

```text
memory_active_window_report(limit, decision_limit, session_id, layers, statuses, strict_session)
```

显式融合预览 CLI：

```bash
.venv/bin/python -m altm.cli build-fused-context \
  --db /tmp/altm.sqlite3 \
  --query SQLite \
  --session-id demo \
  --recall-limit 5 \
  --active-limit 5 \
  --token-budget 1200
```

显式融合预览 MCP：

```text
memory_build_fused_context(query, token_budget, recall_limit, active_limit, candidate_limit, layers, session_id, statuses, strict_session)
```

显式融合报告 CLI：

```bash
.venv/bin/python -m altm.cli build-fused-context-report \
  --db /tmp/altm.sqlite3 \
  --query SQLite \
  --session-id demo \
  --recall-limit 5 \
  --active-limit 5 \
  --token-budget 1200
```

显式融合报告 MCP：

```text
memory_build_fused_context_report(query, token_budget, recall_limit, active_limit, candidate_limit, layers, session_id, statuses, strict_session)
```

默认/融合对比 CLI：

```bash
.venv/bin/python -m altm.cli compare-fused-context \
  --db /tmp/altm.sqlite3 \
  --query SQLite \
  --session-id demo \
  --recall-limit 5 \
  --active-limit 5 \
  --token-budget 1200
```

默认/融合对比 MCP：

```text
memory_compare_fused_context(query, token_budget, recall_limit, active_limit, candidate_limit, layers, session_id, statuses, strict_session)
```

批量默认/融合对比 CLI：

```bash
.venv/bin/python -m altm.cli compare-fused-context-batch \
  --db /tmp/altm.sqlite3 \
  --query SQLite \
  --query MCP \
  --session-id demo \
  --recall-limit 5 \
  --active-limit 5 \
  --token-budget 1200
```

批量默认/融合对比 MCP：

```text
memory_compare_fused_context_batch(queries, token_budget, recall_limit, active_limit, candidate_limit, layers, session_id, statuses, strict_session)
```

显式 build-context 融合模式 CLI：

```bash
.venv/bin/python -m altm.cli build-context \
  --db /tmp/altm.sqlite3 \
  --query SQLite \
  --active-window-mode full \
  --active-limit 5
```

`active-window-mode` 支持：

1. `full`：默认值，融合指定 `--active-limit` 数量的主动窗口候选。
2. `limited`：融合 active window，但最多取 2 条主动窗口候选。
3. `off`：回退到历史 `build-context` 行为。

选择策略：

1. 默认扫描 L2/L3/L4。
2. 默认只纳入 `active` / `observing` 记忆。
3. 排除 `deleted` / `tombstoned`。
4. 排除 `review_status=rejected`、`governance_review_status=rejected`。
5. 排除 `review_status=pending` 的 L2，避免未经人工确认的原子记忆进入主动窗口。
6. 无 `strict_session` 时，当前 session 记忆优先；无 session 的全局记忆和 long/permanent 跨会话记忆可以保留。
7. 有 `strict_session` 时，只保留 metadata 中 `session_id` 完全匹配的记忆。
8. L4/persona 使用独立保护：active score 需要达到更高阈值，且默认最多注入 1 条。

评分信号：

1. residentScore。
2. lifecycle state：`permanent` / `long` 高于 `short`。
3. layer：L4 / L3 高于 L2。
4. status：`active` 高于 `observing`。
5. evidence quality。
6. session affinity。

输出：

1. CLI 和 MCP 都返回 `ContextBundle`。
2. 每条 item 继续使用 `memory://<memory_id>` retrieval marker。
3. Context Gateway 仍负责 token budget 裁剪和 immediate / working / background 分段。
4. report 入口返回 `ActiveWindowReport`，包含 selected candidates、selected/filtered 计数和每条记忆的决策原因。
5. fused context 入口返回 `ContextBundle`，metadata 会标记 recall/active/fused/duplicate candidate 数量。
6. fused context report 入口返回 `ContextFusionReport`，包含最终 `ContextBundle`、每个融合候选的来源、是否重复合并、是否最终注入，以及过滤原因。
7. compare 入口返回 `ContextFusionComparisonReport`，包含 baseline bundle、fused report、shared/baseline-only/fused-only memory id。
8. batch compare 入口返回 `ContextFusionBatchComparisonReport`，包含每条 query 的对比报告和 aggregate 计数。
9. batch compare 的 metadata 包含 `recommendation`：`keep_explicit_only`、`consider_opt_in_fusion`、`collect_more_evidence` 或 `insufficient_queries`。

### 融合策略

`build-context` 默认使用 `active-window-mode=full`。`build-fused-context` 仍保留为显式预览和对照入口：

1. 先保留 query recall 顺序，确保当前问题相关性优先。
2. 再追加 Global Active Window 候选，用于注入稳定的长期背景。
3. 如果同一个 MemoryUnit 同时来自 query recall 和 active window，只保留一条，合并 `matched_by` 和分数信号。
4. 组装仍交给 `SimpleContextGateway`，不改变 immediate / working / background 分段规则。
5. 报告原因包括 `included`、`budget_excluded` 和 `outside_candidate_limit`。
6. 对比报告用于评估默认融合带来的增量，并辅助排查上下文差异。
7. 批量对比报告用于跨代表性 query 观察 fused-only 记忆出现频次，降低单 query 偶然性。
8. 默认 `build-context` 会为实际进入上下文的 Active Window 记忆写入 `injected` 生命周期弱反馈。

## Human-In-Loop 边界

Phase 7B 的上下文融合不改 schema、不自动晋升或删除记忆。默认 `build-context` 的 Active Window 注入会写入 `lifecycle_events` 中的 `injected` 弱反馈事件，但不增加 `useful_access_count`；强信号仍由 `cited_by_agent` 或 `user_confirmed` 等显式反馈提供。

`build-fused-context` 是显式只读预览入口，可用于对照默认 `build-context` 行为。

`compare-fused-context` 只读取同一批候选并并列返回 baseline/fused 差异，用作 human-in-loop 决策材料。

`compare-fused-context-batch` 在多条 query 上重复同一只读对比流程，用作评估默认融合效果的汇总证据。

`build-context` 已默认使用 `active-window-mode=full`。如需回退历史行为，显式传入 `--active-window-mode off`，或将 `ALTM_ENABLE_DEFAULT_ACTIVE_WINDOW_IN_BUILD_CONTEXT=false`。

后续尚未实现的高风险事项：

1. 暂无 Phase 7 已决策范围内的未实现高风险事项。

## Phase 7C: Autonomous Maintenance Cycle

`maintenance-cycle` 是当前 Phase 7 的自治维护入口，负责把分散的治理能力收敛为单轮可审计执行链路：

1. 可选索引缺失 embedding。
2. 运行生命周期治理，标记 promotion / demotion 候选。
3. 在 embedding 可用时运行语义去重和跨 session L3 候选发现。
4. 默认调用 `AutonomousGovernanceEngine`，以规则兜底方式自动执行语义去重、跨 session L3 物化、L4/persona 激活。
5. 旧 `review_apply` 链路只保留为兼容和调试路径，不再进入默认主链路。
6. 生成 review audit 汇总，并把本轮自治治理事件纳入同轮报告。

CLI：

```bash
.venv/bin/python -m altm.cli maintenance-cycle \
  --db /tmp/altm.sqlite3 \
  --autonomous-model-mode auto
```

自治治理 CLI：

```bash
.venv/bin/python -m altm.cli autonomous-governance-cycle \
  --db /tmp/altm.sqlite3 \
  --model test-embedding

.venv/bin/python -m altm.cli autonomous-governance-rollback \
  --db /tmp/altm.sqlite3 \
  --target-type memory_unit \
  --target-id <memory-id>
```

MCP：

```text
memory_maintenance_cycle(model, index_embeddings, embedding_limit, governance_limit, semantic_threshold, semantic_mode, l3_threshold, persona_min_support, use_autonomous_governance, autonomous_model_mode, autonomous_rule_fallback, apply_review_actions, review_action_limit, include_rejected_review_actions, allow_second_confirm_review_actions, dry_run)
memory_autonomous_governance_cycle(model, semantic_threshold, semantic_auto_merge_threshold, semantic_auto_tombstone_threshold, l3_threshold, persona_min_support, include_p0, include_p1, model_mode, rule_fallback, dry_run, limit)
memory_autonomous_governance_rollback(target_type, target_id, reason)
```

自治治理执行边界：

1. `use_autonomous_governance=true` 为默认值，维护循环无需人工 mark/apply/confirm。
2. 复用 `review_events` 存储，但写入 `autonomous_governance_evaluated`、`autonomous_governance_decided`、`autonomous_governance_applied`、`autonomous_governance_degraded`、`autonomous_governance_rolled_back` 等自治事件。
3. 模型不可用时使用规则/本地小模型兜底继续执行，并在事件 metadata 中标记 `fallback_mode`。
4. 高置信 P0 决策可直接执行，例如 L2 语义合并/tombstone、跨 session L3 active 物化、L4 persona active/permanent。
5. 当 `ALTM_LLM_BASE_URL`、`ALTM_LLM_API_KEY`、`ALTM_LLM_MODEL` 可用时，`model_mode=auto|llm` 会调用 OpenAI-compatible LLM judge；不可用时不阻塞治理。
6. 旧 `apply_review_actions` 默认关闭；仅当显式传入 `--apply-review-actions-compat` 或 MCP `apply_review_actions=true` 时作为兼容路径运行。

## 验证

```text
.venv/bin/python -m compileall src tests
.venv/bin/python -m unittest discover -s tests  # Ran 107 tests in 8.152s OK
sqlite3 :memory: < schemas/sqlite/001_initial.sql
```

重点测试：

1. Global Active Window 能选择 long/approved/high-resident 记忆。
2. pending/rejected/tombstoned/deleted 记忆不会进入主动窗口。
3. session affinity 能影响排序。
4. `--strict-session` 能只保留当前 session。
5. CLI `active-window` 输出 `ContextBundle`。
6. MCP `memory_active_window` 注册并可调用。
7. CLI `active-window-report` 输出可解释决策。
8. MCP `memory_active_window_report` 注册并可调用。
9. `SimpleContextFusion` 能保留 query recall 顺序并合并重复 memory。
10. CLI `build-fused-context` 输出融合 metadata。
11. MCP `memory_build_fused_context` 注册并可调用。
12. CLI `build-fused-context-report` 输出融合决策。
13. MCP `memory_build_fused_context_report` 注册并可调用。
14. CLI `compare-fused-context` 输出 baseline/fused 差异。
15. MCP `memory_compare_fused_context` 注册并可调用。
16. Application batch compare 能汇总多 query 的 fused-only memory 计数。
17. CLI `compare-fused-context-batch` 输出 aggregate。
18. MCP `memory_compare_fused_context_batch` 注册并可调用。
19. `build-context` 默认融合 active window，显式 `--active-window-mode off` 可回退。
20. L4/persona 采用高阈值低配额策略。
21. 高风险开关从 `configs/high_risk_flags.env` 读取，并支持同名环境变量覆盖。
22. `build-context` 对实际注入的 Active Window 记忆记录 `injected` 弱反馈。
23. L4/persona 入窗可通过 `ALTM_ENABLE_L4_PERSONA_ACTIVE_WINDOW=false` 禁用。
24. `semantic-dedup` 能按开关自动执行受保护 L2 语义合并，并对重复项 tombstone。
25. 默认召回会排除 tombstoned 记忆，避免已合并重复项继续进入上下文。
26. Review audit 写入 append-only `review_events` 时会同步维护 `review_audit_projections` target 投影。
27. `ALTM_ENABLE_REVIEW_EVENT_SOURCING=false` 可关闭 review mark/apply 的事件写入。
28. `review-audit` summary metadata 会返回 projection 计数、覆盖事件数和最近 target 投影。
29. `semantic-dedup` 支持 `--mode auto|mark-only|auto-merge|auto-tombstone` 和 `--dry-run`，可在写库前预览自动治理结果。
30. 自动 L2 合并在单个 SQLite 事务中完成 canonical metadata、duplicate metadata、tombstone、graph edge metadata 和 review event 写入。
31. 自动合并会保留人工 rejected / 已 resolved graph edge 状态，不会被后续候选刷新覆盖。
32. 自动合并引入更严格的保护门：高自动阈值、approved review 状态、结构化字段一致、否定/强弱约束一致、风险 token 一致，且 `task_state` / `temporal_fact` 默认进入人工治理。
33. canonical 选择由创建时间兜底升级为 review、证据质量、resident score、有效访问、保护等级和置信度综合评分。
34. CLI `restore-semantic-merge` 与 MCP `memory_restore_semantic_merge` 可按 graph edge 回滚自动合并和 tombstone，并追加回滚审计事件。
35. Context Gateway 支持可选 tokenizer 预算控制，未配置或依赖不可用时回退 char estimate。
36. `ALTM_ENABLE_OPTIONAL_CONTEXT_TOKENIZER=false` 可显式关闭 tokenizer 预算控制。
37. `cross-session-l3-candidates` 与 `memory_cross_session_l3_candidates` 能从缓存 embedding 生成跨 session L3 候选边。
38. `cross_session_l3_candidate` 会进入 review queue，approve/reject 后通过 review apply 更新候选状态，不自动写 L3。
39. `build-l4-persona-candidates` 与 `memory_build_l4_persona_candidates` 能从已确认 L2 生成 L4/persona observing 候选。
40. L4/persona 候选会进入 review queue，approve 后需二次确认才转为 active/permanent，reject 只标记治理拒绝。
41. `maintenance-cycle` 与 `memory_maintenance_cycle` 能统一执行生命周期治理、语义去重、跨 session L3 候选、L4 persona 候选和 review audit。

当前验证结果：

```text
.venv/bin/python -m compileall src tests
.venv/bin/python -m unittest discover -s tests
sqlite3 :memory: < schemas/sqlite/001_initial.sql

Ran 96 tests in 6.421s
OK
```
