# ADR 0006: Phase 5A 生命周期评分与召回治理

状态：已确认，Phase 5A 执行中。

日期：2026-06-29

## 背景

系统已经具备 L0/L1/L2、真实 LLM 抽取、真实 embedding 召回、feedback 信号和 hybrid retrieval。下一步需要让动态权重、晋升/降级候选和 human-in-loop 审查状态进入核心链路。

用户选择 Phase 5 四条线都做：生命周期治理、语义去重合并、Context Gateway、L3 场景聚类。为避免一次性大改，先实现共同前置能力：生命周期评分与召回治理。

## 决策

1. 新增 `LifecycleGovernor`，由显式 CLI 触发治理周期。
2. `residentScore` 作为长期驻留价值，持久化到 MemoryUnit。
3. `retrievalScore` 仍由 query-time retrieval 计算，只受 residentScore 小幅影响。
4. pending/rejected review 状态进入评分和召回降权。
5. 只标记 promotion/demotion candidate，不自动晋升、降级、删除或压缩。

## 数据模型

`memory_units` 新增：

```text
promotion_candidate_since TEXT
demotion_candidate_since TEXT
```

SQLite 初始化会对已有数据库做轻量兼容检查，缺失字段时执行 `ALTER TABLE`。

## 评分机制

当前 MVP 评分：

```text
residentScore =
  (0.30 * structural
 + 0.30 * access
 + 0.25 * recency
 + 0.15 * evidence_quality)
 * review_multiplier
```

review multiplier：

```text
approved / unset -> 1.00
pending          -> 0.75
rejected         -> 0.15
```

晋升候选阈值：

```text
residentScore >= 0.70
```

降级候选阈值：

```text
residentScore <= 0.22
```

## 召回治理

召回排序保持任务相关性优先。residentScore 只提供上限为 `0.15` 的小幅 boost，pending/rejected/observing/compressed/demotion candidate 会被降权。

这保持了两个原则：

1. 被检索到不等于长期有价值。
2. 长期有价值也不能覆盖当前 query 的实际相关性。

## 取舍

1. 显式 CLI 优先于后台自动治理，便于调试和回滚。
2. candidate 标记优先于自动状态转换，避免模型或启发式评分直接改变长期事实。
3. 简单启发式评分优先于复杂学习排序，先获得可解释、可测试的闭环。
4. 数据模型只补齐 contracts 中已有字段，避免引入额外状态机。

## 后续确认门

1. promotion/demotion candidate 是否需要人工审批工具。
2. residentScore 是否接入更多特征：semantic cluster centrality、graph rank、task affinity。
3. Phase 5B 是否允许自动合并高相似 L2，或只生成人工审查候选。

## Phase 6B 补充决策

`govern-lifecycle` 已以 MCP `memory_govern_lifecycle` 暴露为显式管理工具，语义重复候选生成也以 `memory_semantic_dedup` 暴露。两者只更新评分和待审候选，不自动晋升、降级、合并或删除。

## Phase 5B 补充决策

用户已选择 Phase 5 四条线都做。5B 先实现低风险版本：只标记语义重复候选，不自动合并。

新增 `semantic-dedup` CLI，读取 `memory_embeddings` 中的缓存向量，对相同 `atom_type` 的 L2 进行 pairwise cosine。超过阈值时写入 graph edge：

```text
edge_type = semantic_duplicate_candidate
```

该设计的边界：

1. embedding 相似度只能证明语义接近，不能证明事实等价。
2. 因此当前不改写 L2、不删除、不 tombstone。
3. `conflicts` 和 `supersedes` 需要更强的结构化事实判定，保留到 5B+。

## Phase 5C 补充决策

新增 `SimpleContextGateway`，将 recall candidates 组装为 `ContextBundle`，并按 immediate / working / background 分段。

当前实现选择：

1. 使用 `memory://<memory_id>` 作为 retrieval marker。
2. 用 `source_memory_ids` 保留下钻入口。
3. token budget 先用 `4 chars = 1 token` 粗估，不引入 tokenizer 依赖。
4. 超预算只裁剪上下文呈现，不修改 MemoryUnit。

CLI 暴露为 `build-context`，MCP 暴露为 `memory_build_context`。后续若接 Headroom 式压缩器或真实 tokenizer，需要单独确认。

## Phase 5D 补充决策

新增 `RuleBasedL3SceneBuilder`，先用规则版从 L2 构建 L3 scene：

```text
group key = session_id + atom_type
```

达到最小组大小后生成 L3 MemoryUnit：

1. `layer=L3`
2. `status=observing`
3. evidence refs 指向来源 L2。
4. content 保存 scene_key、source_memory_ids 和 summaries。

当前不使用 LLM 自动命名和摘要，也不做跨 session embedding 聚类。原因是 L3 属于更高抽象层，错误成本高，需要先保留可解释、可回滚的规则版闭环。
