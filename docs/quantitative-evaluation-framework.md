# ALTM 全量量化评测体系

更新时间：2026-08-15

## 1. 结论

ALTM 不应只用一个问答正确率证明能力。它的差异化来自五个连续环节：

1. 将原始交互稳定形成 L0、L1、L2、L3、L4 多层记忆。
2. 使用词法、向量、异构图和生命周期信号完成混合召回。
3. 通过 Query Emergence 找到字面和向量均未直接命中的关联经验。
4. 通过 CCR 在固定 token 预算内压缩上下文，并允许回到完整证据。
5. 根据真实引用、反馈、冲突和时间执行晋升、降级、覆盖、遗忘和隔离。

因此，完整评测必须由三层组成：

| 层级 | 目的 | 输出 |
|---|---|---|
| 公共可比层 | 与公开系统和论文横向比较 | LongMemEval、LoCoMo、BEAM、Memora、AMA-Bench 等原始分数 |
| ALTM 诊断层 | 证明每个架构组件是否产生独立增益 | L0-L4、Graph、CCR、Active Window、Lifecycle 的指标与消融 |
| 真实运行层 | 证明离线分数能转化为产品收益 | 匿名真实轨迹、Harness 任务成功率、延迟、成本、安全和可靠性 |

公共 benchmark 的原始分数不可直接相加。ALTM 可以额外提供一个内部综合指数，但必须同时公开各分项、原始分数、置信区间和安全门禁。

## 2. 当前实现与评测缺口

### 2.1 已实现的可测能力

| 能力 | 主要实现 | 应测对象 |
|---|---|---|
| L0 原文 | append-only capture、evidence locator | 写入完整性、顺序、幂等性 |
| L1 胶囊 | 增量 LLM summarization、checkpoint | 忠实度、覆盖率、压缩率 |
| L2 原子事实 | typed atom extraction、evidence ref | 精确率、召回率、原子性、类型正确率 |
| L3 场景 | embedding candidate、semantic gate、scene synthesis | 场景边界、跨 session 聚合、事实一致性 |
| L4 Persona | 跨 session/Agent 证据、facet、supersede | 稳定画像精度、误画像率、更新正确率 |
| 混合召回 | FTS unicode/trigram、local/remote vector、Graph PPR、RRF | evidence recall、排序、通道增益 |
| Query Emergence | seed recall、有限跳 PPR-style 扩散 | graph-only recall、路径正确性 |
| Active Window | resident/lifecycle/layer/session signals | 主动注入收益、干扰率 |
| CCR | 类型路由、压缩、稳定 marker、drilldown | 保真度、压缩率、下钻恢复率 |
| Lifecycle | citation/feedback、promotion/demotion、retention | 状态迁移精度、遗忘、陈旧记忆泄漏 |
| Runtime | prepare/commit/abort、scope、持久队列 | 端到端一致性、延迟、失败恢复、隔离 |

### 2.2 当前 `altm-benchmark` 已覆盖

当前实现已经支持：

- LongMemEval、LoCoMo 和匿名轨迹导入。
- `l0`、`embedding`、`l2`、`full` 四种 enrichment。
- `Recall-any@K`、`Recall-all@K`、`nDCG@K`、`MRR`。
- recall p50、p95、p99 延迟。
- dataset SHA-256、环境和逐题 evidence ID。

### 2.3 当前缺口

以下缺口会使现有分数低估或误判 ALTM：

1. Runner 只调用 `FTSRetrievalEngine.recall()`，没有评估真实 `build_context()`、token budget、Active Window、CCR 和最终注入内容。
2. 没有固定 reader 生成答案，因此没有 answer accuracy、groundedness、abstention 和 citation correctness。
3. 无 gold evidence 的问题会被标记为 `no_gold_evidence` 并跳过，导致 abstention、adversarial 和安全负例没有进入主分数。
4. `QueryEmergenceEngine` 没有独立进入 benchmark 路径，无法证明 graph-only 关联召回增益。
5. 没有记录 L1-L4 中间产物质量，无法区分写入、抽取、召回、上下文选择和 reader 的错误。
6. 延迟只覆盖 recall，不覆盖写入、模型抽取、embedding、Graph、context assembly、drilldown 和回答。
7. 没有 token、API 调用、美元成本、SQLite 体积和 storage amplification。
8. 没有 stale-memory、supersede、deletion、scope leakage、poisoning 和 over-personalization 指标。
9. 报告没有完整固定 answer model、judge、prompt hash、embedding、随机种子和每个阶段的配置。
10. 没有同模型、同 embedding、同预算的一次只改一个变量的受控基线。

### 2.4 已完成的首项受控消融

2026-08-16 已完成 `L0` 与 `L0 + text-embedding-v4` 的 paired comparison，固定数据、问题、Top-K 和指标定义。LongMemEval 前 100 题的 Recall-any@10 提升 20.00 个百分点，LoCoMo 全集提升 29.26 个百分点；对应 p95 延迟分别变为基线的 2.21 倍和 15.82 倍。结果证明远程语义向量显著提升证据召回，同时暴露在线 query embedding 的延迟成本。完整数据见[向量评测报告](../reports/embedding-eval-20260816/README.md)。

## 3. 公开 Benchmark 选择

### 3.1 第一优先级

| Benchmark | 核心能力 | ALTM 对应能力 | 使用方式 |
|---|---|---|---|
| [LongMemEval](https://github.com/xiaowu0162/LongMemEval) | 信息抽取、多 session、时间、知识更新、拒答 | L1/L2、混合召回、Lifecycle | 保留现有 evidence 评测，新增官方 QA judge 和 abstention |
| [LoCoMo](https://github.com/snap-research/locomo) | single-hop、multi-hop、temporal、open-domain、adversarial | Graph、跨 session 场景、路径召回 | 重点报告 Graph A/B、multi-hop 和 temporal |
| [BEAM](https://github.com/mohammadtavakoli78/BEAM) | 128K 至 10M token，十类长期记忆能力 | 分层压缩、CCR、Lifecycle、容量曲线 | 先跑 128K/500K，再扩展 1M/10M |
| [Memora](https://github.com/geniesinc/Memora) | 周、月、季度级 consolidation、mutation、forgetting | L3/L4、supersede、降级和删除 | 使用 FAMA，直接验证当前状态而非累计事实 |
| [AMA-Bench](https://arxiv.org/abs/2602.22769) | Recall、Causal Inference、State Updating、State Abstraction | Graph 因果关系、L3、CCR 内容路由 | 作为 Agent 轨迹和机器内容主评测 |
| [MemoryArena](https://arxiv.org/abs/2602.16313) | 多 session 依赖任务中的实际行动 | Harness 组装、Active Window、经验复用 | 报告 Task Success、Progress 和 latency |

### 3.2 第二优先级

| Benchmark | 价值 | ALTM 专项用途 |
|---|---|---|
| [LongMemEval-V2](https://github.com/xiaowu0162/LongMemEval-V2) | 25M/115M token Agent 轨迹、workflow、gotcha、premise awareness | 验证 L3/L4 是否真正形成可复用环境经验 |
| [MemoryAgentBench](https://github.com/HUST-AI-HYZ/MemoryAgentBench) | Accurate Retrieval、Test-Time Learning、Long-Range Understanding、Selective Forgetting | 验证持续学习与选择性遗忘 |
| [MemBench](https://arxiv.org/abs/2506.21605) | participation/observation、factual/reflective、capacity、read/write time | 验证参与式与观察式写入，以及 L2/L4 差异 |
| [StructMemEval](https://arxiv.org/abs/2602.11243) | ledger、state、tree 等结构化记忆 | 验证多层和 Graph 是否优于 flat top-k |
| [PersonaMem-v2](https://arxiv.org/abs/2512.06688) | 隐式偏好和个性化 | 验证 L4 facet 的有效性与 token 效率 |
| [OP-Bench](https://arxiv.org/abs/2601.13722) | Irrelevance、Sycophancy、Repetition | 防止 L4 和 Active Window 过度注入 |
| [STALE](https://arxiv.org/abs/2605.06527) | 隐式冲突和陈旧记忆 | 验证 supersede 和 current-state adjudication |
| [Supersede](https://arxiv.org/abs/2606.27472) | 隔离事实覆盖能力 | 验证旧值抑制和新值选择 |

### 3.3 安全专项

| Benchmark | 核心指标 | ALTM 要求 |
|---|---|---|
| [MPBench](https://arxiv.org/abs/2606.04329) | Attack Success Rate、Retrieval Success Rate | 分别测毒化写入、召回和最终行为 |
| [MINJA](https://arxiv.org/abs/2503.03704) | Injection Success Rate、Attack Success Rate | 对话式恶意记忆写入 |
| [Trojan Hippo](https://arxiv.org/abs/2605.01970) | 长期触发 ASR、安全与效用权衡 | 测 100 个 benign session 后的持久触发 |

安全评测不是加分项，而是发布门禁。任何跨 tenant/user/workspace/agent 泄漏都直接判定该版本不合格。

## 4. ALTM 专属数据集

建议建立 `altm_eval_v1`，采用 ground-truth-first 生成方式：先定义事件、状态、关系、有效期、删除和作用域，再生成对话或 Agent 轨迹。这样能避免从生成文本中反推答案造成标签污染。

### 4.1 样本类型

| Slice | 场景 | 主要指标 |
|---|---|---|
| `direct_recall` | 原文包含明确事实 | Recall、MRR、answer accuracy |
| `paraphrase_recall` | 查询与证据无关键词重合 | semantic recall |
| `multi_session_join` | 答案分散在多个 session | Recall-all、multi-evidence coverage |
| `graph_only` | 关联记忆不含查询词和近义词 | Graph-only Recall、Emergence Gain |
| `causal_chain` | 原因、阻塞、结果跨多个节点 | Path Recall、causal answer accuracy |
| `temporal_update` | 同一事实多次更新 | Current-State Accuracy、Stale Use Rate |
| `implicit_conflict` | 新事实不显式否定旧事实 | conflict detection、supersede accuracy |
| `forget_delete` | 撤回、过期、tombstone、物理删除 | FAMA、Deletion Leakage Rate |
| `scene_formation` | 多 session 形成一个项目或工作流 | L3 Scene F1、boundary leakage |
| `persona_formation` | 多 session/Agent 支持稳定偏好 | L4 Facet F1、False Persona Rate |
| `persona_restraint` | 与画像无关或诱导相关的问题 | over-personalization rate |
| `active_window` | 查询本身不足以触发但当前任务需要 | proactive utility、distraction rate |
| `ccr_json_code_log` | JSON、代码、日志和自然语言长内容 | compression、literal retention、drilldown |
| `scope_isolation` | 四级 scope 中存在同名事实 | Cross-Scope Leakage Rate |
| `memory_poisoning` | 用户、工具、网页和历史内容注入 | ISR、RSR、ASR |
| `runtime_failure` | retry、abort、worker crash、卸载 | recovery、idempotency、orphan cycle rate |

### 4.2 样本字段

每个样本至少包含：

```json
{
  "case_id": "stable-id",
  "scope": {
    "tenant_id": "t1",
    "workspace_id": "w1",
    "user_id": "u1",
    "agent_id": "a1"
  },
  "events": [],
  "query": "",
  "answer_criteria": [],
  "gold_evidence_ids": [],
  "forbidden_evidence_ids": [],
  "gold_current_state": {},
  "gold_graph_nodes": [],
  "gold_graph_edges": [],
  "gold_support_paths": [],
  "expected_memory_layers": {},
  "expected_action": "answer|abstain|ignore|delete",
  "content_type": "natural|json|code|log",
  "difficulty": {
    "history_tokens": 0,
    "session_count": 0,
    "evidence_count": 0,
    "mutation_count": 0,
    "graph_hops": 0
  }
}
```

### 4.3 规模

| 阶段 | 规模 | 用途 |
|---|---:|---|
| CI smoke | 80-120 cases | 每个核心 slice 至少 5 个确定性样本 |
| Nightly | 1,000-2,000 cases | 受控消融、随机种子和长度分桶 |
| Release | 5,000+ cases + 公共全集 | 正式报告和回归门禁 |
| 真实轨迹 | 每月滚动 500+ query | 生产分布和匿名回放 |

中英文、代码、JSON 和日志必须分别报告。建议至少 40% 中文，避免英文公开数据集掩盖当前中文分词、抽取和压缩问题。

## 5. 指标定义

### 5.1 召回与排序

设 gold evidence 集合为 \(G\)，Top-K 返回证据集合为 \(R_K\)。

| 指标 | 公式或定义 |
|---|---|
| Evidence Recall@K | \(|G \cap R_K| / |G|\) |
| Evidence Precision@K | \(|G \cap R_K| / |R_K|\) |
| Recall-any@K | \(\mathbb{1}[G \cap R_K \ne \varnothing]\) |
| Recall-all@K | \(\mathbb{1}[G \subseteq R_K]\) |
| MRR | 第一个正确 evidence 的 reciprocal rank |
| nDCG@K | 按 gold relevance 计算 discounted gain |
| Negative Recall Rate | 不应召回时返回任意记忆的比例，越低越好 |
| Channel Coverage | FTS、vector、graph、active window 各通道贡献的有效 evidence 比例 |
| Retrieval Diversity | Top-K 中唯一 evidence group、session 和 layer 的覆盖率 |

现有 `Recall-any@K` 必须保留，但多证据问题的主指标应改为 Evidence Recall@K 和 Recall-all@K。

### 5.2 当前状态、冲突与遗忘

| 指标 | 定义 |
|---|---|
| Current-State Accuracy | 最终答案使用当前有效值的比例 |
| Stale Retrieval Rate | Top-K 中 forbidden/obsolete evidence 的比例 |
| Stale Use Rate | 最终答案实际引用或复述旧值的比例 |
| Supersession Precision | 被标为 superseded 的旧 facet 中真正应失效的比例 |
| Supersession Recall | 应失效旧 facet 中成功 supersede 的比例 |
| Deletion Leakage Rate | 删除后仍可 recall、inject、drilldown 或回答的比例 |
| Abstention Accuracy | 无有效证据时正确拒答的比例 |

采用 Memora 的 FAMA：

\[
\mathrm{FAMA}=\max(0,\mathrm{MPA}-\lambda(1-\mathrm{FAA}))
\]

\[
\lambda=\frac{N_{\mathrm{forget}}}{N_{\mathrm{presence}}+N_{\mathrm{forget}}}
\]

其中 MPA 是有效记忆条件满足率，FAA 是无效记忆未出现的条件满足率。

### 5.3 L0-L4 形成质量

| 层级 | 指标 |
|---|---|
| L0 | Capture Completeness、Order Accuracy、Idempotent Write Rate |
| L1 | Fact Coverage、Faithfulness、Correction Preservation、Open-Question Recall、Compression Ratio |
| L2 | Atom Precision/Recall/F1、Type Accuracy、Atomicity、Evidence Attribution Accuracy、Duplicate Rate |
| L3 | Scene Precision/Recall/F1、Cross-Session Support、Boundary Leakage、Active/Historical Fact Accuracy |
| L4 | Facet Precision/Recall/F1、False Persona Rate、Cross-Context Support、Facet Stability、Supersession Accuracy |

自动评分优先使用结构化 gold。开放文本使用 entailment + 人工校准 judge。L1/L3/L4 的每条生成事实都应分别检查：

- 是否被 source evidence 蕴含。
- 是否遗漏必需事实。
- 是否混入其他项目、用户或时间段。
- 是否把临时任务要求提升为稳定 persona。

### 5.4 Graph 与自主涌现

| 指标 | 定义 |
|---|---|
| Node Precision/Recall/F1 | gold 节点与抽取节点匹配 |
| Typed Edge Precision/Recall/F1 | 同时匹配端点和 edge type |
| Support Path Recall@K | gold path 是否出现在 Top-K 解释路径 |
| Path Faithfulness | 路径中每条 edge 是否有来源证据 |
| Graph-Only Recall@K | 仅由 graph 通道命中的 gold evidence 比例 |
| Emergence Gain | `Recall(graph enabled) - Recall(max_hops=0)` |
| Counterfactual Necessity | 删除或 reject 支持路径后，graph-only 结果正确消失的比例 |
| Irrelevant Expansion Rate | graph 扩散新增结果中无关记忆比例 |

Graph 优势必须使用字面隔离样本证明。查询、seed 和 neighbor 之间不能共享唯一实体、锚点或近义短语，否则不能归因给图扩散。

### 5.5 Context Gateway、Active Window 与 CCR

| 指标 | 定义 |
|---|---|
| Context Evidence Recall | 最终注入 bundle 覆盖 gold evidence 的比例 |
| Context Precision | 注入 item 中对答案有用的比例 |
| Budget Utilization | 实际使用 token / token budget |
| Token Efficiency | answer accuracy / 1K injected tokens |
| Active Incremental Gain | `score(recall + active) - score(recall only)` |
| Active Distraction Rate | active-only item 导致答案退化的比例 |
| Compression Ratio | compressed tokens / original tokens，越低越紧凑 |
| Answer Preservation | 压缩内容仍可回答原问题的比例 |
| Literal Retention | ID、路径、数字、错误码、时间等精确字段保留率 |
| Drilldown Success | marker 可定位正确原文的比例 |
| Drilldown Recovery Gain | 首次上下文不足时，下钻后恢复正确答案的比例 |
| Marker Integrity | marker 指向相同 memory 和 content hash 的比例 |

CCR 必须与以下三种基线比较：原文截断、通用摘要、ALTM 类型路由压缩。在相同 token 预算下报告 accuracy、literal retention 和 latency。

### 5.6 生命周期与反馈闭环

| 指标 | 定义 |
|---|---|
| Promotion Precision/Recall | 晋升决策与 gold durable memory 的一致性 |
| Time to Promotion | 从首次证据到 LONG/ACTIVE 的周期数 |
| Demotion Precision/Recall | 降级决策与 gold stale/low-value memory 的一致性 |
| Useful Retention@T | T 个周期后仍保留的有用记忆比例 |
| Harmful Retention@T | T 个周期后仍可见的有害或过期记忆比例 |
| Citation Attribution Precision | `cited_by_agent` 是否来自真实最终回复 |
| Feedback Responsiveness | helpful/harmful 信号后排名或状态变化幅度 |
| Rollback Success | 自治决策回滚后状态、证据和可见性恢复正确的比例 |
| Evidence Repair Success | 来源删除后 fallback repair 仍能追溯证据的比例 |

### 5.7 端到端能力

| 指标 | 定义 |
|---|---|
| Answer Accuracy | exact、structured、F1 和固定 LLM judge 分开报告 |
| Grounded Answer Rate | 回答中的事实均有注入或下钻证据 |
| Citation Precision/Recall | 回答 marker 与实际支持证据的一致性 |
| Task Success Rate | 完整任务成功比例 |
| Task Progress Score | 完成子任务比例 |
| Memory Benefit | `score(ALTM) - score(no-memory)` |
| Memory Harm Rate | no-memory 正确但 ALTM 变错的比例 |
| Horizon Retention Curve | score 随 session、token、时间、mutation、hop 增长的曲线 |

### 5.8 性能、成本和容量

每个阶段分别记录 p50、p95、p99：

- L0 write。
- L1/L2/Graph/L3/L4 build。
- embedding index。
- recall。
- context assembly。
- drilldown。
- prepare/commit。
- end-to-end answer。

同时报告：

| 指标 | 定义 |
|---|---|
| Read/Write Throughput | 每秒 query 和 memory write |
| Build Tokens | 每个 session 的抽取模型 token |
| Query Tokens | reader 输入与输出 token |
| API Calls | 每个 turn 和每个正确答案的模型调用数 |
| Dollar Cost | 每 1K turns、每 1K queries、每个正确答案 |
| Storage Amplification | SQLite bytes / L0 source bytes |
| Capacity Cliff | 分数首次下降超过 10% 的 history 规模 |
| Queue Lag | job enqueue 到完成的 p50/p95/p99 |

参考 LongMemEval-V2 的 LAFS 思路，应同时画出 accuracy-latency、accuracy-token 和 accuracy-cost Pareto frontier，而不是只给平均延迟。

### 5.9 安全与可靠性门禁

| 指标 | 发布要求 |
|---|---:|
| Cross-Tenant Leakage Rate | 0 |
| Cross-Workspace Leakage Rate | 0 |
| Cross-User Leakage Rate | 0 |
| Cross-Agent L0-L3 Leakage Rate | 0 |
| Tombstone/Deletion Leakage Rate | 0 |
| Invalid Citation Acceptance | 0 |
| Marker Cross-Scope Drilldown | 0 |
| Prepare/Commit Idempotency | 100% |
| Committed Cycle Completeness | 100% |
| Orphan Prepared Cycle after unload | 0 |
| Replay Determinism on fixed inputs | 100% |

Poisoning报告分别保留：

- ISR：恶意内容进入持久记忆的比例。
- RSR：恶意记忆被攻击查询召回的比例。
- ASR：最终行为被恶意记忆改变的比例。
- Benign Utility Delta：防御开启后正常任务的性能损失。

## 6. 基线与消融

### 6.1 公平基线

所有基线必须固定同一个 answer model、judge、embedding、top-k、token budget、prompt 和超时。

1. No memory。
2. Token-matched recent window。
3. Full context，限上下文可容纳的样本。
4. SQLite FTS/BM25 only。
5. Dense vector only。
6. Flat hybrid RAG。
7. GraphRAG 或 HippoRAG 类结构化 RAG。
8. Oracle evidence。
9. 至少一个公开 Agent Memory 系统，建议 Mem0；资源允许时增加 Letta/Zep/Hindsight。

### 6.2 ALTM 逐项消融

| 配置 | 唯一新增能力 |
|---|---|
| A0 | L0 + FTS |
| A1 | A0 + local vector |
| A2 | A1 + remote embedding |
| A3 | A2 + L1/L2 |
| A4 | A3 + typed Graph retrieval |
| A5 | A4 + Query Emergence |
| A6 | A5 + L3 |
| A7 | A6 + L4 |
| A8 | A7 + Active Window |
| A9 | A8 + CCR |
| A10 | A9 + Lifecycle feedback |

另外执行反事实消融：

- `max_hops=0/1/2/3`。
- reject edge 前后。
- tombstone 前后。
- Active Window `off/limited/full`。
- CCR 原文、截断、通用摘要、类型路由。
- L4 开关与 OP-Bench。
- Lifecycle 开关与 Memora/STALE。

每个结论必须来自同一批逐题 paired results，不允许引用不同模型或不同 embedding 的外部分数做直接差值。MemDelta 已证明单独替换 embedding 就可能改变系统排名。

## 7. 评分与统计

### 7.1 对外报告

公共 benchmark 保留各自官方原始指标，不生成跨 benchmark 的虚假总分。主表至少包含：

- overall 和 category。
- history length bucket。
- mutation count bucket。
- evidence count 和 graph hop bucket。
- answer、retrieval、latency、token、cost。
- 配置和 commit hash。

### 7.2 内部综合指数

内部可以建立五个 0-100 分柱：

| Pillar | 权重 |
|---|---:|
| Formation Quality | 15% |
| Retrieval and Reasoning | 25% |
| Dynamic Consistency and Lifecycle | 20% |
| Context Governance and Efficiency | 15% |
| End-to-End Utility | 25% |

单个高优指标相对 no-memory 和 oracle 归一化：

\[
P_m=100\cdot\frac{S_{\mathrm{ALTM}}-S_{\mathrm{no\ memory}}}
{S_{\mathrm{oracle}}-S_{\mathrm{no\ memory}}}
\]

低值更好的指标先转换方向。原始值和未截断的 \(P_m\) 必须保留，dashboard 可将展示值限制在 0-100。

综合指数使用加权几何平均，避免一个极强维度掩盖短板：

\[
\mathrm{ACI}=100\cdot\exp\left(\sum_j w_j
\log\left(\max(\epsilon, P_j/100)\right)\right)
\]

安全和可靠性不进入 ACI。任一硬门禁失败时，版本状态为 `blocked`，即使 ACI 很高也不能发布。

### 7.3 统计规范

- 所有 accuracy 类指标给出 95% paired bootstrap CI。
- 二元逐题 A/B 使用 McNemar test。
- 排序指标使用 paired bootstrap。
- 随机或 LLM 生成阶段至少 3 个 seed，报告均值和标准差。
- 报告 effect size 和绝对百分点，不只报告相对百分比。
- 多 category 比较执行 Holm correction。
- Judge 至少抽样 200 条与人工双标比较，报告 agreement 和 Cohen's kappa。
- Judge prompt、model、temperature、版本和 hash 固定。

## 8. 评测流水线

```text
dataset load
  -> isolated scope/database
  -> chronological ingest
  -> wait for required jobs/checkpoints
  -> snapshot L0-L4/Graph/Lifecycle
  -> recall and context assembly
  -> optional drilldown
  -> fixed reader answer
  -> deterministic scorer
  -> calibrated LLM judge for open text
  -> per-stage metrics and cost
  -> paired ablation comparison
  -> aggregate, CI, frontier, regression gate
```

每次运行生成不可变 manifest：

```json
{
  "altm_commit": "",
  "harness_commit": "",
  "dataset_name": "",
  "dataset_sha256": "",
  "runner_version": "",
  "config_sha256": "",
  "answer_model": {},
  "judge_models": [],
  "embedding_model": {},
  "prompt_hashes": {},
  "seed": 0,
  "platform": {},
  "started_at": "",
  "finished_at": ""
}
```

逐题 trace 必须保留阶段表：

| 阶段 | 输入 | 输出 | gold | latency | tokens | cost | error |
|---|---|---|---|---:|---:|---:|---|
| ingest | turn IDs | L0 IDs | expected L0 | | | | |
| fold | L0 IDs | L1-L4 IDs | expected facts | | | | |
| graph | memories | nodes/edges | gold graph | | | | |
| recall | query | ranked IDs | evidence IDs | | | | |
| context | candidates | injected IDs | useful IDs | | | | |
| answer | context | response | criteria | | | | |
| feedback | citations | state changes | expected state | | | | |

这张表应成为失败分析的标准产物，避免只看最终 aggregate 后猜测根因。

## 9. 实施顺序

### Phase 0：修正现有评测语义

1. 将无 evidence 的问题从 `skip` 改成 abstention/negative 正式样本。
2. 同时评估 `recall()` 和真实 `build_context()`。
3. 增加 fixed reader、answer scorer、citation scorer。
4. 记录 write/read/token/cost 和完整 manifest。
5. 确保每次 run 使用全新隔离数据库，避免历史运行影响 enrichment 和延迟。

### Phase 1：证明现有核心优势

1. LongMemEval cleaned 全集。
2. LoCoMo 全集，重点做 vector-only 对比 Graph、Query Emergence。
3. `altm_eval_v1` 的 graph-only、CCR、scope、runtime slices。
4. 发布 A0-A10 paired ablation。

### Phase 2：证明长期自治与画像

1. Memora + FAMA。
2. PersonaMem-v2 + OP-Bench。
3. STALE/Supersede。
4. L1-L4 formation gold 标注与生命周期曲线。

### Phase 3：证明 Agent 经验与规模

1. AMA-Bench。
2. MemoryArena。
3. BEAM 128K、500K、1M、10M。
4. LongMemEval-V2 small，再评估 medium。

### Phase 4：上线门禁

1. MPBench、MINJA、Trojan Hippo。
2. 匿名真实 Harness trace 回放。
3. 每月 drift 报告。
4. CI smoke、nightly 中集、release 全量三级门禁。

## 10. 最小可交付版本

第一版不需要同时接入所有公开 benchmark。最有性价比的闭环是：

1. LongMemEval cleaned：公共通用能力。
2. LoCoMo：Graph 和 multi-hop。
3. Memora：更新、遗忘和 L4。
4. AMA-Bench：Agent 轨迹和因果经验。
5. ALTM graph-only + CCR + scope 专项集：证明独有架构。
6. No-memory、recent-window、full-context、FTS、dense RAG、oracle 六个固定基线。
7. A0-A10 消融、95% CI、逐题 trace、commit/dataset/config hash。

完成这七项后，ALTM 的核心主张可以被分别量化：

- “记得更多”由 evidence recall 和 answer accuracy 证明。
- “能找到隐式关联”由 graph-only recall 和 counterfactual necessity 证明。
- “形成更高层经验”由 L3/L4 formation 和 AMA-Bench 证明。
- “长期保持当前状态”由 FAMA、stale use 和 supersession 证明。
- “用更少上下文工作”由 accuracy-token frontier 和 CCR 证明。
- “不会串用户、串 Agent 或复活已删记忆”由零泄漏硬门禁证明。

## 11. 主要参考资料

- [LongMemEval paper](https://arxiv.org/abs/2410.10813)
- [LongMemEval repository](https://github.com/xiaowu0162/LongMemEval)
- [LoCoMo paper](https://arxiv.org/abs/2402.17753)
- [BEAM repository](https://github.com/mohammadtavakoli78/BEAM)
- [MemoryAgentBench paper](https://arxiv.org/abs/2507.05257)
- [MemBench paper](https://arxiv.org/abs/2506.21605)
- [Memora paper](https://arxiv.org/abs/2604.20006)
- [PersonaMem-v2 paper](https://arxiv.org/abs/2512.06688)
- [OP-Bench paper](https://arxiv.org/abs/2601.13722)
- [StructMemEval paper](https://arxiv.org/abs/2602.11243)
- [AMA-Bench paper](https://arxiv.org/abs/2602.22769)
- [MemoryArena paper](https://arxiv.org/abs/2602.16313)
- [LongMemEval-V2 paper](https://arxiv.org/abs/2605.12493)
- [MemDelta paper](https://arxiv.org/abs/2606.29914)
- [MPBench paper](https://arxiv.org/abs/2606.04329)
- [Trojan Hippo paper](https://arxiv.org/abs/2605.01970)
