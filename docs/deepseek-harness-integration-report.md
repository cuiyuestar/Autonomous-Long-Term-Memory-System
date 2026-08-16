# DeepSeek Harness 接入测试报告

测试日期：2026-08-14；Embedding 补充验证：2026-08-16

> 后续可插拔改造已将 adapter 拆分为 Service Definition、ALTM Provider 和 Harness Consumer，并增加 `abort_turn`、热启停和完整卸载。当前结果见[插件生命周期改造报告](deepseek-harness-plugin-lifecycle-report.md)。

## 结论

ALTM 已作为独立树外 Cordis bundle 接入 DeepSeek Harness。适配器没有修改 Harness agent loop、ALTM 核心逻辑或 OpenClaw、Hermes 等现有适配器。

真实跨仓组装测试通过：bundle 能安装到隔离 profile，Harness Loader 能加载发布产物，首轮与后续轮次都调用真实 ALTM Streamable HTTP MCP 服务，召回上下文以持久 `user/message` 进入 Harness session log，最终回复在 `turn/end` 后提交，真实 marker 引用写回 `cited_by_agent`，四级 scope 阻断跨用户读取，缺失客户端凭证时 Harness 轮次保持可用。

ALTM 全量 169 个测试通过。它们覆盖 L0-L4、后台 worker、混合召回、图检索与查询诱导涌现、CCR、生命周期、自治治理、scope 隔离、MCP、评估、回滚、runtime cycle abort 和只读 UI 查询。全量测试的外部模型边界使用仓库已有的本地真实 HTTP server fixture；另有针对性验证使用真实 DeepSeek Chat 完成 Harness 两轮召回与 Graph 涌现，结果见[自主记忆涌现真实验证报告](altm-autonomous-emergence-verification-report.md)。后续已接入独立 OpenAI-compatible Embedding Provider，生产记忆索引和公开数据集向量消融均通过；这证明远程语义召回链路，不等同于 L3/L4 形成质量已完成真实标注评测。

## 测试版本

| 项目 | 版本 |
|---|---|
| ALTM | `848b1b0` 基线及本报告所述适配变更 |
| DeepSeek Harness | `47f943859b`，工作树干净 |
| Node.js | `v22.23.1` |
| pnpm | `11.21.0` |
| Python | `3.11.15` |
| SQLite | `3.51.0` |

## 接入实现

`@altm/deepseek-harness` 由 [`adapters/deepseek-harness`](../adapters/deepseek-harness) 提供，manifest 的 `dsh.bundle.patch` 指向 `cordis.patch.yml`。用户通过 `dsh plugin --profile <name> add <package>` 安装，profile 仍可覆盖完整配置。

生命周期映射：

```text
首个已接纳的 agent/pre-step
  -> memory_prepare_turn
  -> 同批追加带 source 的持久 user/message

最终 turn/end {completed|max-tokens}
  -> 提取该 turn 最后一条文本 AssistantMessage
  -> memory_commit_turn
  -> 从最终回复的 memory:// marker 推导真实引用
```

适配器不在 `agent/turn-stopping` 提交，因为其他插件仍可在该事件中追加 steering 并继续同一 turn。`turn/end` 是实际终点。

Harness `Agent.id` 等于 session id，不能作为跨会话稳定 ALTM `agent_id`。适配器将 `agentId` 作为显式配置，默认 `deepseek-harness`；`workspaceId` 未配置时使用 session `cwd`，再回退到 `default`。

API Key 通过 `apiKeyEnv` 引用解析。存在 `ctx.credentials` 时由该服务负责；否则读取同名进程环境变量。每个 prepare/commit 操作重新解析凭证并创建独立 MCP 连接，因此下一次操作能看到轮换后的 key。

## 真实跨仓组装测试

命令：

```bash
.venv/bin/python adapters/deepseek-harness/tests/run_e2e.py
```

测试脚本执行以下真实路径：

1. 构建并 `npm pack` 适配器。
2. 使用 Harness `dsh plugin` 将 tarball 安装到临时 profile。
3. 运行 `dsh --profile altm-e2e --dump-config`，确认 bundle 层与 `altm-memory` row。
4. 生成一次性 API Key，只把 SHA-256 交给 ALTM 服务端。
5. 启动真实 `altm mcp-server --transport streamable-http --profile runtime`。
6. 使用真实 Harness `boot()`、Loader、Agent、agent loop、session log 和 JSONL persistence。
7. 使用确定性 LLM adapter 代替唯一的非确定性外部边界；记忆、MCP、鉴权、SQLite、召回和生命周期逻辑均为生产实现。
8. 退出时删除临时 profile、数据库、JSONL、tarball、Corepack shim 和服务日志。

最终输出：

```json
{
  "bundle_install": true,
  "invalid_auth_rejected": true,
  "same_session_recall": true,
  "citation_feedback": true,
  "scope_isolation": true,
  "missing_credential_fail_open": true,
  "database": {
    "cycle_count": 3,
    "committed_count": 3,
    "l0_count": 6,
    "recall_citation_count": 1,
    "isolated_citation_count": 0,
    "cited_signal_count": 1
  },
  "harness_logs": {
    "session_count": 3,
    "recall_context_count": 2,
    "recall_context_has_cobalt": true,
    "isolated_context_has_cobalt": false
  },
  "status": "passed"
}
```

验证项：

| 场景 | 结果 | 外部证据 |
|---|---|---|
| bundle 安装与配置展开 | 通过 | dump 中存在 `@altm/deepseek-harness` 和 `id: altm-memory` |
| 非法 MCP Key | 通过 | HTTP 401 |
| 首轮 capture | 通过 | `session-memory/1` cycle |
| 第二轮召回 | 通过 | 模型回复包含 `cobalt` 与 `memory://...` |
| 模型可见即持久 | 通过 | Harness JSONL 中有 `source.plugin = altm-memory`，文本含 `cobalt` |
| turn 最终提交 | 通过 | 3 个 cycle 全部 `committed` |
| 真实引用反馈 | 通过 | 1 个 citation，1 个 `cited_by_agent` |
| scope 隔离 | 通过 | `user-b` 回复未命中，citation 为 0，JSONL 不含 `cobalt` |
| prepare 故障降级 | 通过 | 缺失 key 时 Harness 正常回复，ALTM cycle 数不增加 |
| 清理 | 通过 | 所有 E2E 产物位于 `TemporaryDirectory` 并在退出时删除 |

## ALTM 能力测试

命令：

```bash
.venv/bin/python -m unittest discover -s tests -v
```

结果：`Ran 169 tests`，`OK`。

| 能力 | 代表性测试证据 |
|---|---|
| prepare/commit 幂等与引用 | `test_prepare_and_commit_are_idempotent_and_record_real_citations` |
| worker 与 L1 checkpoint | `test_worker_builds_real_incremental_l1_and_checkpoint` |
| L2 真实 HTTP 抽取与 dual write | `test_real_http_l2_extraction_dual_write_and_filtered_recall` |
| Graph 抽取 | `test_graph_model_writes_scoped_idempotent_entity_time_graph` |
| 查询诱导涌现 | `test_query_emergence_expands_from_direct_hit_to_graph_neighbor` |
| L3 场景与激活 | `test_l3_typed_scene_is_written_as_observing` |
| L4 Persona 与跨 Agent 共享 | `test_l4_typed_persona_is_shared_but_private_l2_is_not` |
| L4 supersede | `test_high_confidence_new_evidence_supersedes_active_l4_facet` |
| 自治治理 | `test_autonomous_governance_materializes_cross_session_l3_without_review` |
| 生命周期晋升/降级 | `test_repeated_evidence_cycles_promote_and_record_age_stats`、`test_repeated_low_score_cycles_demote_long_memory` |
| CCR 压缩与下钻 | `test_json_compression_persists_ccr_and_restores_original` |
| 混合召回与 embedding | `test_indexer_caches_vectors_and_remote_recall_uses_remote_channel` |
| scope 隔离 | `test_scope_isolation_prevents_cross_agent_reads_and_id_collisions` |
| MCP profile 与鉴权 | `test_runtime_profile_only_exposes_safe_agent_tools`、`test_hashed_api_key_verifier_accepts_valid_key` |
| Retention 与 evidence repair | `test_physical_l0_deletion_preserves_fallback_and_repairs_evidence` |
| LongMemEval/LoCoMo 评估路径 | `test_longmemeval_loader_and_native_retrieval_metrics`、`test_locomo_loader_preserves_turn_evidence_ids` |

## 静态、构建与发布检查

以下命令全部通过：

```bash
.venv/bin/python -m compileall -q src tests adapters/hermes adapters/deepseek-harness/tests
.venv/bin/ruff check src tests adapters/hermes adapters/deepseek-harness/tests/run_e2e.py
.venv/bin/pyright
sqlite3 :memory: '.read schemas/sqlite/001_initial.sql'
.venv/bin/python -m build

cd adapters/typescript
npm ci
npm run typecheck
npm run build

cd ../deepseek-harness
npm ci
npm run typecheck
npm run build
```

结果：

- Ruff：`All checks passed!`
- Strict Pyright：`0 errors, 0 warnings, 0 informations`
- Python：sdist 与 wheel 构建成功
- TypeScript SDK：干净安装、类型检查、构建成功
- DeepSeek Harness adapter：干净安装、类型检查、构建成功
- DeepSeek Harness 源码：未修改，工作树保持干净

## 实测发现并修复的问题

1. 静态 `inject: ['agents']` 会与 `agent-spine` 创建首个 Agent 形成激活竞态，导致首轮未被捕获。适配器不读取 `ctx.agents`，因此改为空依赖并在服务创建前注册事件监听。
2. 树外插件运行时导入 Harness package 会在源码检出中命中尚未构建的 `lib/`。适配器只保留类型依赖，运行时按公开消息字段创建冻结的 `UserMessage`；`schemastery` 按 Harness 规则作为普通 dependency 安装。
3. FastMCP 会把可解析为 JSON 的可选 `query` 字符串预解析成对象，随后拒绝 `str` 参数。适配器不再重复发送 `query = content`，由 ALTM 服务端按协议使用 `content` 作为默认 query。
4. `agent/turn-stopping` 不是最终终点，其他插件仍可继续当前 turn。commit 改由最终 `turn/end` 触发。
5. Corepack-only 环境没有 `pnpm` shim。E2E 在临时目录生成 shim，并把 pnpm state 定向到临时目录，不修改宿主安装。

## 验证边界

- 全量测试使用真实本地 HTTP server 验证外部请求、解析、失败和持久化。针对性验证另行使用真实 DeepSeek Chat 完成 Harness 两轮召回与 Graph 涌现，并使用真实 `text-embedding-v4` 完成生产记忆索引与 LongMemEval、LoCoMo 向量消融。该结果仍不代表 L3/L4 形成质量、线上费用或最终答案准确率。
- 没有运行 DeepSeek Harness 全仓测试，因为 Harness 源码零改动；发布产物通过真实 profile、Loader、agent loop 和 persistence 组装测试。
- 直接 L0 查询召回按 ALTM `session_id` 过滤。跨 session 记忆依赖 worker 形成的 L2/L3/L4 和 active window；该行为由 ALTM 专门的跨 session、L3/L4 和自治治理测试覆盖，适配器 E2E 不把短期 L0 误当成长记忆。
