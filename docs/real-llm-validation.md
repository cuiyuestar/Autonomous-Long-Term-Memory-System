# Real LLM Validation

日期：2026-06-29

## 目标

验证已配置的真实 OpenAI-compatible 模型能支撑当前系统中所有需要 LLM 的能力：

1. LLM 基础连通性。
2. CLI `extract-l2` 真实 L2 抽取。
3. MCP stdio `memory_extract_l2` 真实 L2 抽取。
4. L2 写入后的过滤召回。

## 模型配置

配置文件：`.env.local`

```text
ALTM_LLM_BASE_URL=https://api.deepseek.com
ALTM_LLM_MODEL=deepseek-v4-flash
ALTM_LLM_TIMEOUT_SECONDS=60
ALTM_LLM_API_KEY=sk-e8d...1b17
```

密钥只保存在本地 `.env.local`，该文件已被 `.gitignore` 忽略。

## 连通性测试

验证命令通过 `OpenAICompatibleClient.chat_json()` 发送最小 JSON 请求。

结果：

```text
config {'base_url': 'https://api.deepseek.com', 'model': 'deepseek-v4-flash', 'timeout': 60, 'api_key': 'sk-e8d...1b17'}
result {'connected': True, 'component': 'llm'}
```

结论：

1. base URL 可用。
2. API key 鉴权通过。
3. `deepseek-v4-flash` 模型可用。
4. `response_format={"type":"json_object"}` 可用。

## CLI L2 抽取验证

验证路径：

```text
init-db
  -> capture
  -> capture
  -> fold-l1
  -> extract-l2
  -> search --layer L2 --session-id real-llm-cli
```

输入要点：

```text
我们决定采用 OpenAI 兼容接口接入 DeepSeek，并要求 L2 事实默认 pending review。
确认：真实模型抽取失败时不写入 L2，成功时按类型拆表双写。
```

结果：

```text
CLI_EXTRACT_COUNT 3
CLI_EXTRACT_TYPES ['decision', 'constraint', 'decision']
CLI_REVIEW_STATUS ['pending', 'pending', 'pending']
CLI_SEARCH_COUNT 1
CLI_L2_TABLE_COUNTS {'l2_constraints': 1, 'l2_decisions': 2}
CLI_L2_SUMMARY 采用 OpenAI 兼容接口接入 DeepSeek
CLI_L2_SUMMARY L2 事实默认 pending review
CLI_L2_SUMMARY 真实模型抽取失败时不写入 L2，成功时按类型拆表双写
```

结论：

1. 真实 LLM 能从 L1 ContextCapsule 抽取 L2 atoms。
2. L2 默认进入 `pending` review 状态。
3. 双写成功：L2 MemoryUnit + 类型拆表。
4. `search --layer L2 --session-id ...` 能召回英文关键词命中的 L2。

## MCP L2 抽取验证

验证路径：

```text
memory_remember
  -> memory_remember
  -> memory_fold_l1
  -> memory_extract_l2
  -> memory_recall
```

第一次 MCP 验证结果：

```text
MCP_TOOLS ['memory_remember', 'memory_fold_l1', 'memory_extract_l2', 'memory_recall', 'memory_drilldown']
MCP_L1_COUNT 1
MCP_L2_COUNT 1
MCP_L2_TYPES ['decision']
MCP_REVIEW_STATUS ['pending']
MCP_L2_SUMMARY MCP的memory_extract_l2应写入L2 MemoryUnit和类型拆表
MCP_L2_TABLE_COUNTS {'l2_decisions': 1}
```

结论：

1. MCP server 能读取 `.env.local` 导出的真实模型配置。
2. `memory_extract_l2` 能完成真实 L2 抽取。
3. MCP 路径下也能完成 L2 MemoryUnit + 类型拆表双写。

## 召回验证与发现

中文短语查询测试：

```text
query=类型拆表
MCP_RECALL_VALIDATION_L2_COUNT 2
MCP_RECALL_VALIDATION_RECALL_COUNT 0
```

英文 marker 查询测试：

```text
query=MCPRecallMarker
MCP_MARKER_L2_COUNT 1
MCP_MARKER_RECALL_COUNT 1
MCP_MARKER_RECALL_IDS ['l2_42976066ce526d7a9cda5f64']
MCP_MARKER_SUMMARIES ['MCPRecallMarker should remain searchable after memory_extract_l2']
```

结论：

1. L2 抽取后可通过 MCP `memory_recall` 召回。
2. 当前 SQLite FTS5 使用 `unicode61` tokenizer，对中文短语子串召回不稳定。
3. 中文检索需要后续引入更合适的中文分词、补充 trigram/ngram 索引，或增加 BM25/向量召回融合。

## 总体结论

真实模型链路可用：

```text
DeepSeek API
  -> OpenAICompatibleClient
  -> L2Extractor
  -> L2 MemoryUnit
  -> L2 typed tables
  -> CLI/MCP recall
```

当前唯一暴露的功能边界是中文 FTS 精确召回能力不足。这属于 retrieval 层问题，不是 LLM 接入或 L2 抽取问题。

## 后续建议

1. Phase 4 优先补中文检索能力：jieba tokenization、trigram FTS、或向量召回。
2. 为 L2 pending 记忆增加召回降权，而不是完全不召回。
3. 增加 `memory_feedback`，记录 L2 被使用、确认或拒绝的信号。
4. 增加 L2 dedup/conflict/supersede，避免真实模型多次抽取产生重复事实。

## Phase 4 回归

Phase 4 已补本地 lexical vector、trigram FTS、jieba token 和 LIKE fallback 的统一召回，并做少量真实模型回归：

```text
PHASE4_REAL_SEARCH_COUNT 1
PHASE4_REAL_MATCHED_BY [['local_vector', 'fts_trigram']]
PHASE4_REAL_SUMMARIES ['我们决定提升中文检索能力，关键词是类型拆表和中文召回。']
```

结论：真实 LLM 生成的中文 L2 现在可以通过中文短语召回。当前的 vector 是本地 lexical vector，不是外部语义 embedding。
