# Phase 5 Scope: 生命周期评分、语义治理、上下文网关与 L3 场景

## 目标

用户选择 Phase 5 四条线都做，但按依赖顺序拆成可验证子阶段：

1. Phase 5A：生命周期评分与召回治理。
2. Phase 5B：基于 embedding 的 L2 语义去重、合并、conflict/supersede 初版。
3. Phase 5C：Context Gateway，将召回结果转成可注入上下文包。
4. Phase 5D：L3 场景聚类，将 L2/L1 聚合为跨会话场景。

本文件记录 Phase 5 当前已实现的四条最小闭环，并保留后续增强确认门。

## Phase 5A 当前能力

### 生命周期评分

新增 `LifecycleGovernor`，显式运行一个治理周期：

```bash
.venv/bin/python -m altm.cli govern-lifecycle \
  --db /tmp/altm.sqlite3 \
  --limit 1000
```

治理周期会更新：

1. `score.resident_score`
2. `score.structural`
3. `score.recency`
4. `score.access`
5. `score.evidence_quality`
6. `lifecycle.age`
7. `lifecycle.protection_tier`
8. `lifecycle.promotion_candidate_since`
9. `lifecycle.demotion_candidate_since`

### 评分原则

`residentScore` 是长期驻留价值，不等同于当前 query 的命中分：

```text
residentScore = weighted(structural, access, recency, evidence_quality) * review_multiplier
```

当前权重：

```text
structural       0.30
access           0.30
recency          0.25
evidence_quality 0.15
```

`pending` L2 会降权，`rejected` L2 会显著降权。这样可以保留 human-in-loop 的审查边界：模型抽取出的 L2 可以参与召回，但不会被当作同等可信的长期事实。

### 召回治理

`FTSRetrievalEngine` 仍以任务相关性为主，但对候选做有界调整：

1. pending review 降权。
2. rejected review 显著降权。
3. compressed / observing / demotion candidate 降权。
4. residentScore 只提供小幅 boost，不覆盖 query relevance。

## 非目标

Phase 5A 不做：

1. 自动晋升为 long/permanent。
2. 自动物理删除或压缩。
3. 自动调用 embedding 重建索引。
4. 语义合并、冲突检测、L3 场景聚类。
5. 后台 daemon 或定时任务。

## Phase 5B 当前能力

### 语义去重候选

新增 `semantic-dedup` CLI：

```bash
.venv/bin/python -m altm.cli semantic-dedup \
  --db /tmp/altm.sqlite3 \
  --model text-embedding-v4 \
  --threshold 0.92
```

该命令会：

1. 读取 L2 MemoryUnit。
2. 使用 `memory_embeddings` 中同一模型的缓存向量。
3. 只比较相同 `atom_type` 的 L2。
4. cosine similarity 超过阈值时写入 graph edge。
5. edge type 为 `semantic_duplicate_candidate`。

当前不会自动合并、删除、tombstone 或改写 L2 内容。它只生成待审治理候选，后续由 human-in-loop 决定是否合并或标记 supersede。

### 图关系写入

新增 SQLite graph helper：

```text
memory_id -> graph_node
graph_edge(source_memory_id, target_memory_id, edge_type, weight, confidence, metadata)
```

当前 5B 只写 `semantic_duplicate_candidate`。`conflicts` 和 `supersedes` 保留到下一步，因为它们需要更强的事实判定策略，不能只靠 embedding 相似度决定。

## Phase 5C 当前能力

### Context Gateway

新增 `build-context` CLI：

```bash
.venv/bin/python -m altm.cli build-context \
  --db /tmp/altm.sqlite3 \
  --query "生命周期治理" \
  --token-budget 1200 \
  --limit 10
```

MCP 新增：

```text
memory_build_context(query, token_budget, limit, layers, session_id, statuses)
```

输出 `ContextBundle`：

1. `immediate`：最高优先级候选或高 retrieval score。
2. `working`：任务工作区上下文，通常来自 L1/L2/L3 或中等相关候选。
3. `background`：低优先级但仍可作为背景的候选。
4. `retrieval_marker`：形如 `memory://<memory_id>`，用于下钻回 MemoryUnit。
5. `source_memory_ids`：保留原始候选 ID。

当前 token budget 使用 `4 chars = 1 token` 的粗估策略，不引入 tokenizer 依赖。超出预算时只裁剪上下文呈现，不修改底层记忆。

## Phase 5D 当前能力

### L3 Scene Builder

新增 `cluster-l3` CLI：

```bash
.venv/bin/python -m altm.cli cluster-l3 \
  --db /tmp/altm.sqlite3 \
  --session-id demo \
  --min-group-size 2
```

MCP 新增：

```text
memory_cluster_l3(session_id, min_group_size, limit)
```

当前 L3 builder 是规则版：

1. 读取 L2 MemoryUnit。
2. 按 `session_id + atom_type` 分组。
3. 达到 `min_group_size` 后生成 L3 MemoryUnit。
4. L3 `status=observing`，不会直接进入长期稳定记忆。
5. L3 evidence refs 指向来源 L2。
6. L3 content 保存 scene_key、source_memory_ids 和 summaries。

当前不使用 LLM 自动命名或抽象场景，避免高层记忆在未审查前漂移。

## 后续增强

### Phase 5B+：conflict / supersede 关系治理

候选方向：

1. 同 subject/predicate 下 object 不一致时生成 conflict candidate。
2. 新事实明确替代旧事实时记录 supersedes candidate。
3. 高置信人工确认后才 tombstone 或降权被替代项。

确认门：相似阈值、冲突判定方式、是否自动 tombstone 被合并项。

### Phase 5C+：真实 tokenizer 与压缩策略

确认门：token budget 策略、是否接真实 tokenizer、压缩器范围。

### Phase 5D+：L3 语义聚类和命名

候选方向：

1. 基于 embedding 相似度做跨 session 聚类。
2. 用 LLM 对 L3 scene 命名和摘要。
3. L3 evidence refs 同时指向 L2/L1，fallback 到 L0。

确认门：聚类阈值、场景命名方式、是否需要 LLM 参与摘要。

## 验证

Phase 5A 需要验证：

```text
.venv/bin/python -m compileall src tests
.venv/bin/python -m unittest discover -s tests
sqlite3 :memory: < schemas/sqlite/001_initial.sql
```

重点测试：

1. useful feedback 能推动 promotion candidate。
2. rejected + stale memory 能进入 demotion candidate。
3. pending L2 在召回排序中被降权。
4. schema 持久化 promotion/demotion candidate 字段。
5. L2 高相似同类型记忆生成 `semantic_duplicate_candidate` 图边。
6. Context Gateway 生成 context bands、retrieval marker 并遵守预算裁剪。
7. L3 scene builder 从 L2 分组生成 observing L3，并保留 evidence refs。

当前验证结果：

```text
.venv/bin/python -m compileall src tests
.venv/bin/python -m unittest discover -s tests
sqlite3 :memory: < schemas/sqlite/001_initial.sql

Ran 20 tests in 2.129s
OK

PHASE5_SMOKE_UPDATED 1
PHASE5_SMOKE_RECALLED 1
PHASE5_SMOKE_MATCHED_BY [['local_vector', 'fts_trigram']]

PHASE5_DEDUP_CANDIDATES 1
PHASE5_L3_SCENES 1
PHASE5_CONTEXT_ITEMS 2
PHASE5_MCP_APP FastMCP
```
