# ADR 0003: 真实 L2 抽取、按类型拆表与过滤召回

状态：已确认，Phase 3 执行中。

日期：2026-06-29

## 背景

Phase 2 已完成 L0 capture、L1 mock、SQLite FTS、MCP stdio/SSE 验证。下一步进入 L2 原子事实闭环，让系统从“会保存和折叠上下文”进入“能抽取可复用事实”的阶段。

## 已确认决策

| 决策点 | 选择 | 影响 |
| --- | --- | --- |
| Phase 3 主目标 | 真实 L2 抽取 | L2 由 OpenAI-compatible LLM 生成，不再只做规则 mock |
| 模型接入 | OpenAI 兼容 `/v1/chat/completions` | 支持 OpenAI、DeepSeek、豆包兼容网关、本地兼容网关 |
| 配置来源 | 环境变量 | 使用 `ALTM_LLM_BASE_URL`、`ALTM_LLM_API_KEY`、`ALTM_LLM_MODEL`，不把密钥写入仓库 |
| L2 存储 | 按类型拆表 + MemoryUnit 双写 | 每条 L2 同时写入 L2 MemoryUnit 和对应类型表 |
| Review Gate | 默认待审 | L2 写入 `metadata.review_status=pending`，不自动进入长期画像 |
| 失败策略 | 不写 L2 | LLM 调用、JSON 解析或校验失败时抛错，避免污染记忆 |
| 检索能力 | 同步补过滤 | FTS 支持 layer/session/status 过滤 |

## L2 类型表

当前按方案文档中的九类 L2 原子记忆拆表：

1. `l2_preferences`
2. `l2_constraints`
3. `l2_project_facts`
4. `l2_decisions`
5. `l2_issues`
6. `l2_resolutions`
7. `l2_task_states`
8. `l2_temporal_facts`
9. `l2_lessons`

每张表使用同构字段：`id`、`memory_unit_id`、`text`、`subject`、`predicate`、`object`、`scope`、`confidence`、`extraction_reason`、`review_status`、`metadata_json`、`created_at`。

## 设计边界

1. L2 MemoryUnit 是召回和证据链统一入口。
2. 类型表用于结构化查询和后续治理，不替代 MemoryUnit。
3. L2 证据链指向 L1，fallback locator 继承 L1 到 L0 的定位信息。
4. `review_status=pending` 不阻止召回，但后续生命周期和上下文注入可以降权。
5. 本阶段不做 L2 dedup、conflict/supersede、人工审批 UI 或 L4 画像更新。

## 验证结果

已用本地 OpenAI-compatible fake server 验证：

1. CLI：`init-db -> capture -> fold-l1 -> extract-l2 -> search --layer L2 --session-id ...`
2. MCP stdio：`memory_remember -> memory_fold_l1 -> memory_extract_l2 -> memory_recall`
3. 单测：L2 双写、过滤召回、失败不写 L2。

真实外部模型调用需要设置：

```bash
export ALTM_LLM_BASE_URL="https://example.com/v1"
export ALTM_LLM_API_KEY="..."
export ALTM_LLM_MODEL="your-model"
```

## 下一确认门

1. L2 去重策略：merge、supersede、conflict 还是 coexist。
2. pending L2 的召回降权策略。
3. 是否引入 `memory_feedback`，开始记录 candidate_hit/injected/cited/user_confirmed。
4. 是否把 L1 ContextCapsule 单独拆表，降低 JSON content 查询成本。
