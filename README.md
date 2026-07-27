# ALTM (Autonomous Long-Term Memory)

具备自主治理、自主涌现和长期演化能力的 Agent 长期记忆系统。

系统目标来自 `memory-system-development-plan.md`：用分层记忆、长期记忆、自主经验涌现、
动态升级降级和动态权重调整，帮助 Agent 管理有限注意力。

## Phase 1 边界

Phase 1 已完成契约和骨架：

1. Python 作为控制面和公共 API 源头。
2. TypeScript 作为未来 Agent adapter 层，声明 Node 22 工具链。
3. 本地存储先落 SQLite + FTS5，向量、图数据库和 Headroom 深度压缩只保留接口。
4. 先定义 L0-L4、生命周期、证据链、召回和上下文网关边界。
5. 所有重大技术选择保留 human-in-loop 确认门。

## Phase 2 当前能力

Phase 2 进入 `L0 + L1 mock` 最小闭环：

1. `capture` 将原始消息写入 L0 MemoryUnit。
2. SQLite store 支持 MemoryUnit upsert、按 ID 读取、FTS5 索引和 evidence refs。
3. `fold-l1` 对同一 session 的 L0 生成规则版 L1 ContextCapsule。
4. `search` 通过 SQLite FTS5 召回 L0/L1。
5. MCP adapter 提供 `memory_remember`、`memory_fold_l1`、`memory_recall`、`memory_drilldown` 工具入口。

Python 3.11 和 MCP 运行环境已经在项目内隔离配置完成，MCP stdio 与 SSE 都已验证通过。

## Phase 3 当前能力

Phase 3 进入 `真实 L2 抽取` 最小闭环：

1. `extract-l2` 从 L1 ContextCapsule 调用 OpenAI-compatible LLM 抽取 L2 atoms。
2. L2 同时写入 L2 MemoryUnit 和按类型拆分的结构化表。
3. L2 默认 `review_status=pending`，不会自动进入长期画像。
4. `search` 支持 `--layer`、`--session-id`、`--status` 过滤。
5. MCP adapter 新增 `memory_extract_l2`，并为 `memory_recall` 增加过滤参数。

模型配置通过环境变量提供：

```bash
export ALTM_LLM_BASE_URL="https://example.com/v1"
export ALTM_LLM_API_KEY="..."
export ALTM_LLM_MODEL="your-model"
```

## Phase 4 当前能力

Phase 4 进入 `检索增强 + 最小治理`：

1. 统一召回优先使用本地 lexical vector，再合并 trigram FTS、unicode FTS 和 LIKE fallback。
2. 中文短语召回已通过真实 DeepSeek L2 回归验证。
3. L1 ContextCapsule 已双写到结构化表 `l1_context_capsules`。
4. L2 exact dedup 已接入，完全相同的 L2 文本不会重复写入。
5. 新增 `feedback` CLI 和 MCP `memory_feedback`，记录生命周期访问/有用性信号。

Phase 4 细节见 [Phase 4 Scope](docs/phase-4-scope.md) 和 [ADR 0004](docs/adr/0004-retrieval-feedback-dedup.md)。

## 当前新增：真实向量模型接入

已接入 OpenAI-compatible `/embeddings` 向量模型，并保留本地 lexical/FTS 作为回退通道：

1. `.env.local` 使用独立的 `ALTM_EMBEDDING_*` 配置，不复用聊天模型配置。
2. `index-embeddings` 将缺失或内容变更的 MemoryUnit 写入 `memory_embeddings` 缓存表。
3. `search` 和 MCP `memory_recall` 在 embedding 环境变量完整时启用 `remote_vector` 通道。
4. 远程 embedding 查询失败时，召回链路会退回本地 lexical vector、trigram FTS、unicode FTS 和 LIKE fallback。

配置变量：

```bash
export ALTM_EMBEDDING_BASE_URL="https://example.com/compatible-mode/v1"
export ALTM_EMBEDDING_API_KEY="..."
export ALTM_EMBEDDING_MODEL="text-embedding-v4"
export ALTM_EMBEDDING_TIMEOUT_SECONDS="60"
```

接入说明见 [Vector Embedding Integration](docs/vector-embedding-integration.md) 和 [ADR 0005](docs/adr/0005-openai-compatible-embedding.md)。

## Phase 5 当前能力

Phase 5 用户选择四条线都做：生命周期治理、语义去重合并、Context Gateway、L3 场景聚类。当前已完成四条线的最小闭环：

1. 新增 `govern-lifecycle` CLI，显式运行 residentScore 评分周期。
2. `feedback` 产生的访问和有用性信号会进入下一次 residentScore 计算。
3. `pending` / `rejected` L2 会在长期评分和召回排序中降权。
4. 新增 promotion/demotion candidate 标记，不自动晋升、降级或删除。
5. `retrievalScore` 继续表示当前 query 相关性，`residentScore` 只做小幅排序调整。
6. 新增 `semantic-dedup` CLI，基于缓存 embedding 标记 L2 语义重复候选。
7. 语义重复只写 `semantic_duplicate_candidate` graph edge，不自动合并或删除。
8. 新增 `build-context` CLI 和 MCP `memory_build_context`，把 recall candidates 组装为上下文包。
9. Context Gateway 支持 immediate / working / background 分段、预算裁剪和 `memory://` 下钻标记。
10. 新增 `cluster-l3` CLI 和 MCP `memory_cluster_l3`，从 L2 分组生成 observing L3 scene。
11. L3 scene 保留到来源 L2 的 evidence refs，暂不使用 LLM 自动命名。

Phase 5 细节见 [Phase 5 Scope](docs/phase-5-scope.md) 和 [ADR 0006](docs/adr/0006-lifecycle-governance.md)。

## Phase 6A 当前能力

Phase 6A 补齐 human-in-loop 审查入口：

1. 新增 `review-queue` CLI 和 MCP `memory_review_queue`。
2. 新增 `review-mark` CLI 和 MCP `memory_review_mark`。
3. 新增 `review-plan` CLI 和 MCP `memory_review_plan`。
4. 新增 `review-apply` CLI 和 MCP `memory_review_apply`。
5. 新增 `review-events` CLI 和 MCP `memory_review_events`。
6. 新增 `review-audit` CLI 和 MCP `memory_review_audit`。
7. 统一列出 L2 pending、promotion/demotion candidate、semantic duplicate candidate、L3 observing。
8. 审查只标记 `pending` / `approved` / `rejected`。
9. L2 review status 会同步 MemoryUnit metadata 和 L2 typed table。
10. `review-plan` 只生成 proposed action，标出风险和是否需要二次确认。
11. `review-apply` 默认 dry-run；必须 `--confirm` 才会改库，高风险动作还需要 `--second-confirm`。
12. duplicate resolution 不自动 merge/tombstone，只标记为待选择 canonical memory。
13. `review-mark` 和确认执行成功的 `review-apply` 会写入 append-only audit event；dry-run 不写事件。
14. `review-audit` 汇总审查队列、action plan 和审计事件，不修改数据库。

Phase 6 细节见 [Phase 6 Scope](docs/phase-6-scope.md) 和 [ADR 0007](docs/adr/0007-human-review-queue.md)。

## Phase 6B 当前能力

Phase 6B 将只生成候选、不自动处置的管理能力开放到 MCP：

1. 新增 MCP `memory_index_embeddings`，显式构建或刷新 embedding 缓存。
2. 新增 MCP `memory_govern_lifecycle`，显式运行 residentScore 评分周期。
3. 新增 MCP `memory_semantic_dedup`，基于缓存 embedding 标记 L2 语义重复候选。
4. 这些工具只写入索引、评分和待审候选，不自动合并、删除、晋升或降级。

## Phase 7A 当前能力

Phase 7A 实现 Query-Induced Emergence Window 的最小闭环：

1. 新增 `emerge` CLI，从普通召回结果中选出 query entry points。
2. 新增 `QueryEmergenceEngine`，沿 SQLite graph edge 做轻量 PPR 式扩散。
3. 新增 MCP `memory_emerge`，让 Agent 可在 query 后主动涌现相邻经验。
4. graph edge 已被人工拒绝时不会参与扩散。
5. query 后涌现与 query 前 Global Active Window 保持为两个显式入口。

## Phase 7B 当前能力

Phase 7B 实现 query 前 Global Active Window，并已接入默认 `build-context`：

1. 新增 `GlobalActiveWindowEngine`，在没有用户 query 时选择主动工作集。
2. 新增 `active-window` CLI，输出可直接注入的 `ContextBundle`。
3. 新增 MCP `memory_active_window`，让 Agent 在任务开始前获取全局活跃记忆窗口。
4. 新增 `active-window-report` CLI 和 MCP `memory_active_window_report`，解释记忆入选或过滤原因。
5. 默认从 L2/L3/L4 中选择 active/observing、高 residentScore、长期生命周期或当前 session 相关记忆。
6. pending/rejected L2、rejected governance item、tombstoned/deleted memory 不进入主动窗口。
7. 新增 `build-fused-context` CLI 和 MCP `memory_build_fused_context`，显式预览 query recall + active window 的融合上下文。
8. 新增 `build-fused-context-report` CLI 和 MCP `memory_build_fused_context_report`，解释融合候选来源、去重和最终注入结果。
9. 新增 `compare-fused-context` CLI 和 MCP `memory_compare_fused_context`，对比默认 query context 与 fused context 的注入差异。
10. 新增 `compare-fused-context-batch` CLI 和 MCP `memory_compare_fused_context_batch`，跨多条 query 汇总融合增量。
11. `build-context` 默认使用 `active-window-mode=full`；如需历史行为，可显式传入 `--active-window-mode off`。

Phase 7 细节见 [Phase 7 Scope](docs/phase-7-scope.md)。

## 参考设计吸收

| 来源 | 吸收点 |
| --- | --- |
| TencentDB-Agent-Memory | L0 原文保真、分层记忆、SQLite/FTS/sqlite-vec 后端、OpenClaw/Hermes adapter 思路 |
| CogniFold | typed graph、UpdatePlan/Executor、PageRank/recency/access/urgency 评分、BM25/vector/RRF/PPR 检索 |
| Headroom | Headroom 压缩层、ContentRouter、CCR retrieval marker、压缩不等于删除 |
| JDK GC | 分代治理、age table、动态晋升阈值、promotion failure、低频 major governance |

## 工程结构

```text
.
├── adapters/typescript/       # Node 22 adapter 声明层
├── configs/                   # 配置样例
├── docs/                      # 架构决策与阶段说明
├── schemas/sqlite/            # SQLite + FTS5 schema
├── src/altm/                  # Python 控制面骨架
└── tests/                     # 骨架级验证
```

## 本地验证

当前机器未检测到 Node/npm/pnpm，因此 TypeScript 只提交声明性骨架，暂不运行构建。

Python 3.11 环境已用项目隔离方式配置在 `.venv`，底层解释器由 `.tools/uv/uv` 安装到 `.tools/python`。MCP stdio 与 SSE 完整工具链验证记录见 [MCP Runtime Verification](docs/mcp-runtime-verification.md)。

真实 DeepSeek LLM 接入、CLI L2 抽取和 MCP L2 抽取验证记录见 [Real LLM Validation](docs/real-llm-validation.md)。

```bash
.venv/bin/python -m compileall src tests
.venv/bin/python -m unittest discover -s tests
sqlite3 :memory: < schemas/sqlite/001_initial.sql
.venv/bin/python -m altm.cli init-db --db /tmp/altm.sqlite3
.venv/bin/python -m altm.cli capture \
  --db /tmp/altm.sqlite3 \
  --session-id demo \
  --message-id u1 \
  --role user \
  --content "我们决定采用 SQLite FTS，并需要确认 MCP 双模式。"
.venv/bin/python -m altm.cli fold-l1 \
  --db /tmp/altm.sqlite3 \
  --session-id demo
.venv/bin/python -m altm.cli extract-l2 \
  --db /tmp/altm.sqlite3 \
  --session-id demo
.venv/bin/python -m altm.cli index-embeddings \
  --db /tmp/altm.sqlite3 \
  --limit 100
.venv/bin/python -m altm.cli govern-lifecycle \
  --db /tmp/altm.sqlite3 \
  --limit 1000
.venv/bin/python -m altm.cli semantic-dedup \
  --db /tmp/altm.sqlite3 \
  --model text-embedding-v4 \
  --threshold 0.92
.venv/bin/python -m altm.cli build-context \
  --db /tmp/altm.sqlite3 \
  --query SQLite \
  --token-budget 1200 \
  --limit 5
.venv/bin/python -m altm.cli cluster-l3 \
  --db /tmp/altm.sqlite3 \
  --session-id demo \
  --min-group-size 2
.venv/bin/python -m altm.cli review-queue \
  --db /tmp/altm.sqlite3 \
  --limit 100
.venv/bin/python -m altm.cli review-mark \
  --db /tmp/altm.sqlite3 \
  --target-type memory_unit \
  --target-id l2_xxx \
  --kind l2_pending \
  --status approved
.venv/bin/python -m altm.cli review-plan \
  --db /tmp/altm.sqlite3 \
  --limit 100
.venv/bin/python -m altm.cli review-apply \
  --db /tmp/altm.sqlite3 \
  --plan-id review_action_xxx \
  --confirm \
  --second-confirm
.venv/bin/python -m altm.cli review-events \
  --db /tmp/altm.sqlite3 \
  --target-id l2_xxx
.venv/bin/python -m altm.cli review-audit \
  --db /tmp/altm.sqlite3
.venv/bin/python -m altm.cli search \
  --db /tmp/altm.sqlite3 \
  --query SQLite \
  --layer L2 \
  --session-id demo \
  --limit 5
.venv/bin/python -m altm.cli emerge \
  --db /tmp/altm.sqlite3 \
  --query SQLite \
  --layer L2 \
  --limit 5 \
  --seed-limit 8 \
  --max-hops 2
.venv/bin/python -m altm.cli active-window \
  --db /tmp/altm.sqlite3 \
  --session-id demo \
  --token-budget 1200 \
  --limit 5
.venv/bin/python -m altm.cli active-window-report \
  --db /tmp/altm.sqlite3 \
  --session-id demo \
  --decision-limit 100 \
  --limit 5
.venv/bin/python -m altm.cli build-fused-context \
  --db /tmp/altm.sqlite3 \
  --query SQLite \
  --session-id demo \
  --recall-limit 5 \
  --active-limit 5 \
  --token-budget 1200
.venv/bin/python -m altm.cli build-fused-context-report \
  --db /tmp/altm.sqlite3 \
  --query SQLite \
  --session-id demo \
  --recall-limit 5 \
  --active-limit 5 \
  --token-budget 1200
.venv/bin/python -m altm.cli compare-fused-context \
  --db /tmp/altm.sqlite3 \
  --query SQLite \
  --session-id demo \
  --recall-limit 5 \
  --active-limit 5 \
  --token-budget 1200
.venv/bin/python -m altm.cli compare-fused-context-batch \
  --db /tmp/altm.sqlite3 \
  --query SQLite \
  --query MCP \
  --session-id demo \
  --recall-limit 5 \
  --active-limit 5 \
  --token-budget 1200
```

## 下一批确认点

1. 是否允许 Global Active Window 并入默认 `build-context` 主链路。
2. Phase 5B 是否允许自动合并高相似 L2，或只生成待审候选。
3. Context Gateway 是否接真实 tokenizer。
4. L3 场景聚类是否需要 LLM 参与命名和摘要。
5. L3 是否从 session 内规则聚类升级为跨 session embedding 聚类。
6. review audit 是否升级为独立更完整的审计模型。
