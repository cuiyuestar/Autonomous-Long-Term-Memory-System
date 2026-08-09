# Agent Runtime Protocol

ALTM 使用两阶段回合协议。记忆系统不生成 Agent 回复，也不会根据“进入过上下文”推断“被 Agent 引用”。

## 1. 配置

复制 `.env.example` 中需要的变量到本地环境。所有模型使用 OpenAI-compatible API。

通用模型：

```bash
export ALTM_LLM_BASE_URL="https://provider.example/v1"
export ALTM_LLM_API_KEY="..."
export ALTM_LLM_MODEL="your-model"
```

L1、L2、Governance 可以分别覆盖模型；没有填写的字段继承通用配置：

```bash
export ALTM_L1_LLM_MODEL="summary-model"
export ALTM_L2_LLM_MODEL="extraction-model"
export ALTM_GOVERNANCE_LLM_MODEL="judge-model"
```

Governance 模型缺失、输出非法或返回低置信延后决策时，P0 语义动作保持 `defer`，不会规则兜底执行合并、L3 物化或 L4 永久化。

## 2. Prepare

Host Agent 收到用户消息后先调用：

```bash
altm prepare-turn \
  --db ./data/altm.sqlite3 \
  --tenant-id tenant-1 \
  --workspace-id workspace-1 \
  --user-id user-1 \
  --agent-id agent-1 \
  --session-id session-1 \
  --turn-id turn-1 \
  --content "我们决定采用 SQLite。"
```

返回：

```text
cycle_id
user_memory_id
ContextBundle
enqueued_job_ids
```

`prepare-turn` 保证：

1. 四级作用域隔离。
2. `turn_id` 幂等；相同 turn 修改 content/query 会失败。
3. L0 append-only。
4. ContextBundle 中的记忆只记录一次 `injected`。
5. L1/L2/embedding 通过持久化任务队列异步推进。

## 3. Agent 生成

Host Agent 将返回的 ContextBundle 注入自己的模型调用。ALTM 不提供默认回复、不输出模板回复，也不假装执行了 Agent 推理。

Host Agent 应保存实际使用的 `memory_id`。未使用的上下文不能作为引用上报。

## 4. Commit

模型生成完成后调用：

```bash
altm commit-turn \
  --db ./data/altm.sqlite3 \
  --tenant-id tenant-1 \
  --workspace-id workspace-1 \
  --user-id user-1 \
  --agent-id agent-1 \
  --cycle-id <cycle_id> \
  --assistant-content "SQLite 已确认为本地存储。" \
  --cited-memory-id <memory_id>
```

`commit-turn` 在一个 SQLite 事务内完成：

1. 写入真实 assistant L0。
2. 校验 cited memory 必须来自 prepare 阶段的 ContextBundle。
3. 只为显式 cited IDs 写 `cited_by_agent`。
4. 将 runtime cycle 标记为 committed。
5. 入队后续增量折叠任务。

相同内容和引用集合可以安全重试；改变内容或引用集合会触发幂等冲突。

## 5. Worker

后台 worker 使用 SQLite lease，进程异常后任务可重新领取：

```bash
altm worker \
  --db ./data/altm.sqlite3 \
  --worker-id local-worker-1
```

当前任务链：

```text
fold_l1 -> extract_l2 -> index_embeddings
        -> semantic_l3 -> semantic_l4
```

L1 使用真实结构化 LLM 摘要。某阶段配置缺失或调用失败时，任务记录错误并按租约队列重试，不写规则摘要或模拟结果。

L3/L4 使用并行语义 Gate：

```text
embedding candidates
  -> reranker
  -> NLI contradiction check
  -> scope/boundary classifier
  -> L3 or L4 LLM Rubric
```

L3/L4 首次写入均为 observing。只有重复观察、证据稳定和真实 useful feedback
达到策略门槛后才转为 active/long。共享 L4 仅对同一 tenant/workspace/user
可见，来源 L0-L3 仍按 agent_id 隔离。

随后 worker 执行通用 lifecycle 与 retention：

```text
semantic_l4 -> lifecycle -> retention
```

生命周期使用连续周期、useful access、证据质量、冲突、pin 和 long token
压力决定晋升/降级，并写入 age table。L0 默认永久；配置 TTL 或显式用户删除
会经过 deletion request、tombstone、索引清理和 evidence fallback 修复。

## 6. MCP

普通 Agent 使用安全工具面：

```bash
altm mcp-server --db ./data/altm.sqlite3 --profile runtime
```

工具：

```text
memory_prepare_turn
memory_commit_turn
memory_mvp_chat
memory_drilldown
memory_feedback
memory_pin
memory_unpin
memory_delete
```

治理、索引、回滚和 legacy review 工具只在显式管理模式中出现：

```bash
altm mcp-server --db ./data/altm.sqlite3 --profile admin
```

`memory_mvp_chat` 是弃用兼容包装，必须传入 Host Agent 的真实 `assistant_content` 和真实 `cited_memory_ids`。新接入必须使用 prepare/commit。

## 7. Headroom 与 CCR

Context Gateway 默认启用内置 ContentRouter：

1. JSON 按结构裁剪。
2. 代码保留声明、依赖和异常路径。
3. 日志保留错误、告警和去重信号。
4. 自然语言调用 `ALTM_HEADROOM_LLM_*`；模型缺失时只注入 marker。

压缩前原文写入 SQLite CCR。`memory_drilldown` 可直接接受
`memory://<id>#<hash>`，也可附带 query 只检索原文匹配行。

## 8. Streamable HTTP 认证

stdio 是本地可信模式。SSE 和 Streamable HTTP 必须配置：

```bash
export ALTM_MCP_API_KEY_SHA256="<sha256 hex>"
altm mcp-server \
  --db ./data/altm.sqlite3 \
  --transport streamable-http \
  --profile runtime
```

多 key 使用 `ALTM_MCP_API_KEYS_JSON` 分配 `altm:runtime` / `altm:admin`
scope。OIDC/JWT 使用 `ALTM_MCP_TOKEN_VERIFIER_FACTORY` 提供 FastMCP
`TokenVerifier`。
