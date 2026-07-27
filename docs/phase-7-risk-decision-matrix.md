# Phase 7 Risk Decision Matrix

本文档记录 Phase 7 进入默认主链路前必须由 Human-in-the-loop 确认的高风险点。

当前原则：继续按用户选择推进；遇到新的具体破坏性动作或不可逆 schema 变更时再提供具体决策选项。

## 用户当前选择

用户已选择：

1. Global Active Window：D，完全默认进入 `build-context`。
2. 生命周期反馈：C，分级记录。
3. L4/persona：C，低配额高阈值进入窗口。
4. L3 跨 session 聚类：B，只生成候选。
5. 高相似 L2：D，自动合并。
6. Tokenizer：B，可选 tokenizer + fallback。
7. Review audit：C，事件溯源。

执行进展：

1. `build-context` 已默认使用 `active-window-mode=full`，可通过 `--active-window-mode off` 回退；默认值现在由 `configs/high_risk_flags.env` 中的 `ALTM_ENABLE_DEFAULT_ACTIVE_WINDOW_IN_BUILD_CONTEXT` 控制。
2. L4/persona 已采用低配额高阈值策略，并接入 `ALTM_ENABLE_L4_PERSONA_ACTIVE_WINDOW` 开关。
3. Active Window 实际注入到 `build-context` 后会写入 `injected` 生命周期弱反馈事件，不增加 `useful_access_count`。
4. 高风险行为开关已集中记录在 `configs/high_risk_flags.env`，默认值均为 `true`，运行时会读取该文件，并允许同名环境变量覆盖。
5. 高相似 L2 自动合并/tombstone 已接入 `semantic-dedup` 主链路，由 `ALTM_ENABLE_AUTO_L2_SEMANTIC_MERGE` 和 `ALTM_ENABLE_AUTO_L2_TOMBSTONE` 控制。
6. Review audit 已落地 append-only `review_events` + `review_audit_projections` target 级投影，由 `ALTM_ENABLE_REVIEW_EVENT_SOURCING` 和 `ALTM_ENABLE_REVIEW_AUDIT_PROJECTIONS` 控制。
7. Context Gateway 已接入可选 tokenizer 预算控制，由 `ALTM_ENABLE_OPTIONAL_CONTEXT_TOKENIZER` 与 `ALTM_CONTEXT_TOKENIZER` 控制，未配置或依赖不可用时回退 char estimate。
8. 跨 session L3 embedding 候选发现已落地，并已接入 `AutonomousGovernanceEngine`，高置信候选可自动物化为 active L3。
9. L4/persona 已接入自治证据门，高置信多证据偏好/约束/经验可自动写入 `active + permanent` L4，不再依赖二次确认。
10. 自主维护周期 `maintenance-cycle` / `memory_maintenance_cycle` 已落地，统一编排 lifecycle、semantic dedup、cross-session L3、L4 persona、autonomous governance 和 review audit。
11. `AutonomousGovernanceEngine` 已落地：复用 `review_events` 写入 `autonomous_governance_*` 事件，三线并行接入语义去重、跨 session L3、L4/persona 三条 P0 链路。
12. 旧 Review Action Apply 已降级为兼容路径，默认维护周期不再依赖 `review_mark/review_apply/second_confirm`。

## 1. Global Active Window 是否并入默认 `build-context`

风险：

1. 上下文污染：无 query 的全局记忆可能压过当前 query 相关记忆。
2. token budget 挤占：长期背景占用预算，导致直接召回证据被截断。
3. 行为回归：所有调用 `build-context` 的 Agent 行为都会改变。
4. 调试成本升高：错误答案可能来自 query recall，也可能来自主动窗口。
5. 过期记忆放大：高 residentScore 但语境不匹配的记忆会被反复注入。

选项：

1. A. 继续保持显式入口：只使用 `active-window`、`build-fused-context`、`compare-fused-context`。
2. B. 增加 opt-in 参数：新增 `build-context --include-active-window`，默认关闭。
3. C. 默认启用低配额融合：默认注入 active window，但限制为 1-2 条、排在 query recall 后。
4. D. 完全默认启用：所有 `build-context` 自动融合 Global Active Window。

推荐：

先选 B。等 `compare-fused-context-batch` 在代表性 query 上证明 fused-only 记忆稳定有益，再考虑 C。不建议直接 D。

## 2. Active Window 是否参与生命周期权重自动调整

风险：

1. 正反馈循环：被主动窗口选中的记忆更容易再次被选中。
2. 错误固化：一次误选可能持续抬高 residentScore。
3. 访问信号失真：候选展示、真正注入、Agent 引用、用户确认是不同强度信号。
4. 审计困难：如果自动加权没有事件记录，后续难以解释记忆为何升权。

选项：

1. A. 不记录任何生命周期反馈。
2. B. 只记录弱信号 `candidate_hit`，不提升 useful count。
3. C. 仅在实际注入时记录 `injected`，在 Agent 引用或用户确认后再记录强信号。
4. D. 只要进入 active window 就自动提升 residentScore。

推荐：

选 C。它区分“被系统选中”和“被证明有用”，能控制正反馈风险。

## 3. L4 / persona 记忆是否自动进入 Active Window

风险：

1. 画像漂移：临时偏好可能被误当作长期人格特征。
2. 过度个性化：Agent 可能过早依赖 persona，而不是当前任务证据。
3. 隐私和敏感信息风险：L4 可能承载更稳定、更敏感的用户特征。
4. 纠错成本高：错误 L4 一旦频繁注入，会持续影响输出风格和判断。

选项：

1. A. 默认排除 L4。
2. B. 只允许人工确认且 `permanent` 的 L4 进入 active window。
3. C. 允许 L4 进入，但设置独立低配额和更高阈值。
4. D. 自动生成和自动注入 L4/persona。

推荐：

选 B。后续若 L4 质量稳定，再考虑 C。不建议 D。

当前落地 C 的受控版本：`build-l4-persona-candidates` / `memory_build_l4_persona_candidates` 会从已确认 L2 preference/constraint/lesson 生成 L4 observing 候选；review approve 后需要二次确认才会激活为 permanent persona，Active Window 仍保持 L4 低配额高阈值。

## 4. L3 场景聚类是否从 session 内规则升级为跨 session embedding 聚类

风险：

1. 错误聚类：语义相似不等于同一经验场景。
2. session 边界泄漏：不同项目或对话的事实可能被错误混合。
3. 成本增加：跨 session embedding 扫描会提高索引和查询成本。
4. 后续处置复杂：聚类后是否生成 L3、是否合并 evidence、如何审查都需要治理。

选项：

1. A. 继续只做 session 内规则聚类。
2. B. 只生成跨 session 聚类候选，不写 L3。
3. C. 写入 observing L3，并进入 review queue。
4. D. 自动生成 active L3 scene。

推荐：

选 B 或 C。若目标是验证算法质量，先 B；若目标是让系统开始积累跨 session 场景，选 C。

当前落地 B：`cross-session-l3-candidates` / `memory_cross_session_l3_candidates` 只生成跨 session L3 候选 graph edge；review queue 可对候选 approve/reject，apply 只更新 candidate 状态，不自动创建 L3 MemoryUnit。

## 5. 高相似 L2 是否允许自动合并或 tombstone

风险：

1. 数据损失：错误合并会丢失细微但重要的事实差异。
2. evidence chain 断裂：canonical 选择错误会影响 L2 -> L1 -> L0 追溯。
3. 审计风险：自动 tombstone 是破坏性治理动作。
4. 回滚成本高：合并后的内容、引用和图边都需要恢复。

选项：

1. A. 只生成 `semantic_duplicate_candidate` graph edge。
2. B. 允许 `review-plan` 生成 duplicate resolution 计划，但不执行合并。
3. C. 人工二次确认后执行受保护合并或 tombstone。
4. D. 高于阈值自动合并。

推荐：

当前按用户选择执行 D，但采用受保护合并与收敛后的安全边界：canonical 记忆只记录 `merged_duplicate_ids` 和 merge 溯源元数据，不直接改写正文；重复项写入 `superseded_by` 后 tombstone，默认召回会排除 tombstoned 记忆。自动执行前会经过高自动阈值、approved review 状态、结构化字段一致、否定/强弱约束一致、风险 token 一致等保护门，`task_state` / `temporal_fact` 默认进入人工治理。执行侧通过单个 SQLite 事务同时写入 canonical metadata、duplicate metadata、tombstone、graph edge metadata 和 `review_events`，并提供 `dry-run` 预览与 `restore-semantic-merge` / `memory_restore_semantic_merge` 回滚入口。

## 6. Context Gateway 是否接真实 tokenizer

风险：

1. 依赖风险：引入 tokenizer 会增加安装、版本和模型兼容成本。
2. 性能风险：上下文组装会从纯字符串裁剪变成模型相关计算。
3. 行为变化：同一 token budget 下实际注入内容会改变。
4. fallback 复杂：tokenizer 不可用时要保证 CLI/MCP 仍可运行。

选项：

1. A. 继续使用 `4 chars ~= 1 token` 的估算。
2. B. 增加可选 tokenizer，未配置时回退 char estimate。
3. C. 对不同模型配置不同 tokenizer。
4. D. 强制依赖真实 tokenizer。

推荐：

选 B。它能提升准确性，又不会破坏当前轻量运行环境。

当前落地 B：默认不强制安装 tokenizer；当 `ALTM_CONTEXT_TOKENIZER=tiktoken` 且依赖可用时按 tokenizer 计数裁剪，否则继续使用 `4 chars ~= 1 token` 的估算，并在 bundle metadata 中标记 fallback 原因。

## 7. Review audit 是否升级为更完整的审计模型

风险：

1. schema 变更：新增审计模型会引入迁移和兼容成本。
2. 数据膨胀：审计事件数量可能快速增长。
3. 隐私风险：审计内容如果记录过多上下文，可能泄露原文或模型输出。
4. 查询复杂：报告和问题排查会依赖更复杂的索引。

选项：

1. A. 保持当前 append-only `review_events` 元数据审计。
2. B. 新增只读 projection / summary 表，不改变原事件表。
3. C. 建立完整 event-sourcing 模型。
4. D. 接外部观测系统。

推荐：

当前落地 B：保留 append-only `review_events` 作为事件源，新增 `review_audit_projections` 只读投影表，用于按 target 汇总事件数量、最后状态、最近 action plan 等审计信息。自动 L2 语义合并会追加 `semantic_duplicate_marked`、`semantic_auto_merge_applied`、`semantic_auto_tombstone_applied` 和 `semantic_auto_merge_rolled_back` 事件，用于支撑事后追责与回滚验证。

## 当前可继续推进的工程工作

不需要额外确认即可继续：

1. 继续收敛 `maintenance-cycle` 的自治治理链路，例如批量计划摘要、失败重试报告、按 action type 的执行配额。
2. 为 CLI/MCP 编排继续收敛 Application Service，减少重复逻辑。
3. 增加测试、文档和回归验证。
4. 在保留回退开关的前提下，继续推进默认主链路融合的观测和调参。

需要 Human-in-loop 大方向确认后再做：

1. 将二次确认 action plan 改成维护循环默认自动执行。
2. 跨 session L3 候选直接物化为 `active` L3 MemoryUnit。
3. 对原始 L0/L1 或证据链执行不可逆删除。
4. 引入新的不可逆 schema 迁移或外部观测/审批系统依赖。
