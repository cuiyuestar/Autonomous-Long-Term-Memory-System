# ADR 0007: Human Review Queue

状态：历史方案，已被 Phase 7 自动审核治理工程取代。

更新说明：

1. Human Review Queue 不再是 ALTM 运行时默认主链路。
2. `review_mark` / `review_apply` / `second_confirm` 仅保留为兼容和调试接口。
3. 默认维护周期改由 `AutonomousGovernanceEngine` 执行自治评估、自治决策、自治审计和回滚。
4. 新主链路复用 `review_events` 底层存储，但事件语义重铸为 `autonomous_governance_*`。

日期：2026-06-29

## 背景

Phase 5 已经能产生候选，但候选处置仍缺少统一 human-in-loop 入口：

1. L2 抽取默认 `review_status=pending`。
2. 生命周期治理只标记 promotion/demotion candidate。
3. 语义去重只写 `semantic_duplicate_candidate` graph edge。
4. L3 scene 默认 `status=observing`。

这些候选如果直接自动合并、晋升、降级或删除，会破坏用户要求的 human-in-loop 原则。

## 决策

新增 `ReviewQueue`，统一聚合可审查项，并提供 CLI/MCP 标记能力。

当前 review 只记录人工判断，不执行破坏性动作：

```text
pending -> approved / rejected
```

## 实现边界

不新增 review 表，先复用已有 metadata：

1. L2：`memory_units.metadata_json.review_status` + L2 typed table `review_status`。
2. lifecycle candidate：`memory_units.metadata_json.governance_review_status`。
3. L3 observing：`memory_units.metadata_json.governance_review_status`。
4. graph edge candidate：`graph_edges.metadata_json.review_status`。

这样做的原因：

1. 当前只需要最小闭环，不需要复杂审计表。
2. 不改变现有 schema，降低迁移成本。
3. 后续如果要支持审计历史、多人审批、回滚链路，再引入独立 review table。

## 公共入口

CLI：

```text
review-queue
review-mark
```

MCP：

```text
memory_review_queue
memory_review_mark
```

`review-mark` 支持可选 `kind`，用于区分同一个 MemoryUnit 上的多个审查事项。例如一个 L2 atom 同时也是 promotion candidate 时，审批 L2 内容和审批晋升候选必须分开记录。

## 取舍

1. 只标记，不自动执行：降低错误成本。
2. 复用 metadata，不新增表：适合当前 MVP。
3. L2 review status 同步 typed table：保证结构化 L2 查询和 MemoryUnit metadata 一致。
4. lifecycle/L3 的 approved/rejected 暂不改变生命周期状态：下一步再设计真正的 apply action。

## 后续确认门

1. 是否新增独立 `review_items` / `review_events` 审计表。
2. approved promotion candidate 是否自动转为 `lifecycle_state=long`。
3. approved duplicate candidate 是否自动 tombstone 低置信重复项。
4. approved L3 observing 是否转为 active/long。
5. 是否要求所有 destructive action 都必须二次确认。

## Phase 6C 补充决策

新增 `ReviewActionPlanner`，把已审查候选转换成 proposed action：

```text
review item -> action plan
```

当前 action plan 只读，不执行数据库变更。这样可以把“审查通过”和“实际执行长期状态改变或破坏性动作”分成两道门。

设计边界：

1. `review-plan` 默认只处理 approved item。
2. rejected item 只有显式 `--include-rejected` 时才生成建议动作。
3. `prepare_duplicate_resolution`、`promote_to_long`、`mark_observing`、`activate_l3_scene` 都要求二次确认。
4. duplicate resolution 只提示选择 canonical memory，不自动 tombstone。
5. L3 scene 激活只作为 proposed action，不直接把 observing 改成 active。

这延续了 human-in-loop 原则：系统可以提出可追责的下一步，但不会越权改变长期记忆状态。

## Phase 6D 补充决策

新增 `ReviewActionExecutor`，但执行器必须经过显式确认：

1. 不带 `confirm` 时只返回 dry-run 结果。
2. `requires_second_confirmation=true` 的 action 必须额外传入 `second_confirm`。
3. duplicate resolution 不执行 merge/tombstone，只把 graph edge 标记为 `pending_canonical_selection`。
4. demotion 不做压缩或删除，只把 memory 标为 observing。
5. rejected L2/L3 不物理删除，只记录 action applied。

这个阶段允许少量非破坏性状态推进，例如：

1. approved promotion candidate -> `lifecycle_state=long`。
2. approved L3 observing -> `status=active`。
3. rejected promotion/demotion candidate -> 清除 candidate marker。

仍然保留二次确认边界：任何合并、tombstone、物理删除、自动画像写入都不在本阶段执行。

## Phase 6E 补充决策

新增 `review_events` append-only 审计表，用于追踪 review mark / apply：

```text
review_mark
review_apply
```

设计边界：

1. `review-plan` 仍保持只读，不写审计事件。
2. `review-apply` dry-run 不写事件，避免“预览”改变数据库。
3. 缺少二次确认导致未执行时不写事件。
4. 成功执行的 `review-apply` 会记录 action type、risk、是否需要二次确认和结果消息。
5. 审计事件只保存结构化元数据，不保存密钥、完整模型响应或大段原文。

新增 CLI / MCP：

```text
review-events
memory_review_events
```

引入独立表的原因是 metadata 只能表达当前状态，不能可靠回答“谁在什么时候做过什么”。后续如果需要回滚、多人审批或治理报告，可以在这个事件表上继续扩展。

## Phase 6F 补充决策

新增 `ReviewAuditReporter`，基于 review queue、action plan 和 `review_events` 生成只读审计汇总。

当前汇总内容：

1. event type / target type / status 计数。
2. pending / reviewed review item 计数。
3. action plan 总数、高风险数量、需要二次确认数量。
4. 已执行 action 数量和未执行 action plan 数量。
5. 最近 review events。

新增 CLI / MCP：

```text
review-audit
memory_review_audit
```

设计边界：

1. audit summary 不写数据库。
2. audit summary 不新增事件。
3. audit summary 不替代独立审计表，只是其上的只读视图。
4. 后续如需可视化看板或导出报告，需要单独确认输出形态。
