# ALTM（Autonomous Long-Term Memory）开发方案

状态：方案阶段，不进入开发实现。

## 0. 最高优先级设计哲学：运行时完全自治

**硬约束：ALTM 的记忆系统内部链路必须完全自动化、自主管理、对用户透明。**

这不是一个可选体验优化，而是系统成立的核心前提：

1. 用户不参与记忆系统内部的审核、确认、晋升、合并、沉淀、画像更新、场景物化等治理链路。
2. 记忆系统必须自主完成捕获、折叠、召回、整合、冲突处理、证据沉淀、生命周期治理和审计记录。
3. 所谓 Human-in-the-loop 只允许存在于开发协作、架构决策、调试排障和显式用户产品操作中，不允许被实现为 ALTM 运行时主链路的人工审核门。
4. 用户反馈可以作为信号输入，例如纠错、显式偏好、pin/unpin、删除请求，但反馈不是内部治理流程继续运转的前置条件。
5. 高风险治理动作必须通过自动策略、置信度阈值、证据链校验、dry-run 审计、可回滚事务和后台观察期来控制，而不是要求用户逐条审批。
6. 任何 `review_status`、`pending_review`、`Review Queue`、`second_confirm`、`Review Gate` 等机制，如果存在，只能作为内部自动审计/策略状态或开发调试接口；不得阻断默认自治链路。

**反模式：把记忆候选交给用户审核后才能进入 L3/L4、把自动维护周期卡在人工确认、把二次确认设计成默认晋升前置条件。这些都违反 ALTM 的终极设想。**

## 1. 目标与设计边界

ALTM 的目标不是“把历史都存起来”，而是为 Agent 管理有限注意力：让它在当前任务中看到最值得看的信息，并在需要时能沿证据链下钻到原始事实。

核心目标：

1. 保留完整原始证据，避免不可逆总结导致的事实漂移。
2. 将原始对话逐层折叠为摘要、事实、场景、画像，支持宏观认知与细节追溯共存。
3. 将存储抽象层级和生命周期管理彻底解耦。
4. 用动态权重、晋升、降级、压缩、观察期和删除机制管理注意力资源。
5. 将召回排序和存储层级解耦，让任务相关性决定最终进入上下文的内容。
6. 在注入上下文前通过 Headroom 式压缩控制 token，同时保留可逆回取能力。
7. 让记忆系统在运行时完全自主，对用户透明，不把内部治理负担转嫁给用户。

非目标：

1. MVP 不追求复杂分布式图数据库，先用 SQLite/FTS/向量索引和可替换接口实现闭环。
2. 不把 L4 画像当成不可质疑的真理，任何高层画像都必须能回到证据链。
3. 不让“被检索到”自动等同于“有价值”，生命周期更新必须区分候选召回、实际注入、被 Agent 使用、被用户确认四种信号。

## 2. 参考项目吸收点

### 2.1 TencentDB-Agent-Memory

吸收：

1. L0 Conversation -> L1 Summary Capsule -> L2 Atom -> L3 Scenario -> L4 Persona 的语义金字塔。
2. 在 TencentDB 原 L0-L3 范式中插入独立摘要层：L0 保真、L1 局部摘要、L2 结构化事实、L3 场景组织、L4 稳定画像。
3. L0/L1/L2 同时支持 FTS 与向量检索，并允许 embedding 失败时降级到关键词检索。
4. 文件证据与数据库索引分离：索引不是唯一事实来源。
5. L3/L4 可用 Markdown 或可读格式承载，便于调试、审计和必要时的显式编辑；默认运行链路不得要求人工审阅。
6. Mermaid 任务画布和 node_id 下钻机制，用少量符号表达长期任务状态。

### 2.2 CogniFold

吸收：

1. 将事件流折叠成 typed graph：event、concept、intent、time。
2. 用 PageRank、时间衰减、访问次数、紧急度构成上下文评分。
3. immediate / working / background 三段式上下文窗口。
4. BM25、semantic、hybrid、agentic retrieval 多路径召回。
5. PPR 从查询相关入口点扩散，解决多跳任务中的结构召回问题。
6. UpdatePlan + Executor 的原子更新和失败回滚思想。

### 2.3 Headroom

吸收：

1. 放在“召回之后、注入之前”的压缩层定位。
2. ContentRouter 按内容类型选择压缩策略，而不是统一摘要。
3. CCR，Compress-Cache-Retrieve：压缩后的上下文带 retrieval marker，原文保存在本地缓存，可按 hash 或 query 回取。
4. 对 JSON、日志、代码、HTML、自然语言使用不同压缩器。
5. 被压缩内容不等于被遗忘，压缩只改变上下文呈现形态。

### 2.4 JDK 分代 GC

吸收的是治理模型，不照搬 JVM 实现：

1. 年轻代对象先进入 Eden，经历多次 Survivor 考验后才晋升老年代。
2. 晋升阈值不是固定常量，而是根据 Survivor 空间压力动态计算。
3. 晋升前需要做安全检查，老年代空间不足时要处理 promotion failure。
4. 通过 age table 统计不同年龄对象占用，决定下一轮晋升阈值。
5. 老年代也不是永远不治理，只是治理频率更低、代价更高、策略更谨慎。

映射到记忆系统：

1. 短期记忆类似年轻代，必须经受多轮任务和时间检验。
2. 长期记忆类似老年代，代表稳定价值，但仍可降级。
3. 晋升阈值由注意力预算、长期区增长率、近期任务收益共同决定。
4. 晋升失败时不删除记忆，而是留在短期区、延长观察或合并同类项。
5. Major GC 对应低频全局重整：冲突合并、失效画像降级、场景重新聚类。

## 3. 总体架构

系统分为六个子系统：

```text
+--------------------+
| Host Adapter       | OpenClaw / Hermes / MCP / HTTP / SDK
+---------+----------+
          |
          v
+---------+----------+      +-------------------+
| Capture Pipeline   | ---> | L0 Raw Archive    |
+---------+----------+      +-------------------+
          |
          v
+---------+----------+      +-------------------+
| Folding Pipeline   | ---> | L1/L2/L3/L4 Store |
| L0->L1->L2->L3->L4 |      | Graph + Evidence  |
+---------+----------+      +-------------------+
          |
          v
+---------+----------+
| Lifecycle Manager  | short/long, age, promotion, demotion, compression
+---------+----------+
          |
          v
+---------+----------+
| Retrieval Engine   | BM25 + vector + graph + PPR + fusion + rerank
+---------+----------+
          |
          v
+---------+----------+
| Context Gateway    | Headroom compression + context bands + drilldown markers
+--------------------+
```

关键分离：

1. L0-L4 是抽象形态维度，回答“记忆以什么形式存在”。
2. short/long 是生命周期维度，回答“记忆如何被保护、压缩、删除或晋升”。
3. retrieval 是任务相关性维度，回答“当前要看什么”。
4. context 是表达预算维度，回答“怎么把选中的东西塞进窗口”。

## 4. 核心概念模型

### 4.1 MemoryUnit

所有可被召回、治理、打分的记忆块统一抽象为 MemoryUnit。

```ts
type MemoryLayer = "L0" | "L1" | "L2" | "L3" | "L4";
type LifecycleState = "permanent" | "long" | "short";
type MemoryStatus = "active" | "compressed" | "observing" | "tombstoned" | "deleted";

interface MemoryUnit {
  id: string;
  layer: MemoryLayer;
  lifecycleState: LifecycleState;
  status: MemoryStatus;

  content: string;
  contentHash: string;
  summary?: string;

  createdAt: string;
  updatedAt: string;
  lastAccessedAt?: string;
  accessCount: number;
  usefulAccessCount: number;

  score: {
    residentScore: number;
    retrievalScore?: number;
    structural: number;
    recency: number;
    access: number;
    semantic?: number;
    taskAffinity?: number;
    urgency?: number;
    evidenceQuality: number;
  };

  lifecycle: {
    age: number;
    protectionTier: 1 | 2 | 3 | 4 | 5;
    compressionTier: 0 | 1 | 2 | 3 | 4;
    observationUntil?: string;
    demotionCandidateSince?: string;
    promotionCandidateSince?: string;
  };

  evidenceRefs: EvidenceRef[];
  graphRefs: string[];
  metadata: Record<string, unknown>;
}
```

### 4.2 L0-L4 层级边界

五层结构的核心是让每一层只承担一种抽象职责，避免“摘要、事实、场景、画像”混在同一层。

| 层级 | 名称 | 职责 | 典型内容 | 证据来源 |
| --- | --- | --- | --- | --- |
| L0 | 原始记忆层 | 保真档案 | 原始对话、工具输出、运行事件 | 无上游，作为最终证据 |
| L1 | 上下文摘要层 | 局部压缩 | 会话片段摘要、任务阶段摘要、转折点、未解决点 | source_message_ids -> L0 |
| L2 | 原子记忆层 | 结构化事实 | 偏好、约束、决策、问题、解决方案、项目事实 | evidence -> L1，fallback -> L0 |
| L3 | 场景归纳层 | 跨事实组织 | 项目场景、任务场景、工作流、主题块 | evidence -> L2/L1，fallback -> L0 |
| L4 | 用户画像层 | 稳定人格与长期偏好 | 长期目标、沟通风格、技术偏好、核心价值观 | evidence -> L3/L2，fallback -> L0 |

这一调整刻意让 L1 不再承担“原子事实”职责，而只做 L0 的可读压缩层。L2 才负责结构化事实。这样可以降低 L0 原文和结构化事实之间的跨度，也让后续删除、压缩、召回和证据下钻更清晰。

标准追溯路径：

```text
L4 画像
  -> L3 场景
  -> L2 原子事实
  -> L1 上下文摘要
  -> L0 原始对话
```

允许跳级追溯：

1. L2 可以直接保留 L0 fallback locator，避免 L1 删除后证据断裂。
2. L3 可以同时引用 L2 事实和 L1 摘要，用于保留场景语境。
3. L4 可以引用 L3 场景，也可以引用少量高置信 L2 事实作为直接证据。

### 4.3 L0 的特殊规则

L0 是永久原始档案。它可以拥有生命周期元数据，但 lifecycleState 对 L0 只影响召回权重、索引优先级和上下文呈现，不允许触发物理删除。

因此：

1. L0.status 不进入 deleted。
2. L0 可以被降权、冷藏、从热索引移入冷索引，但原始文本保留。
3. L0 的向量索引或全文索引可以重建、降级或分片，不等同于 L0 原文删除。
4. L0 是所有证据链的最终锚点。

这样解决了“L0 永久保存”和“所有层级都有短期/长期状态”的表面冲突：L0 的生命周期是注意力生命周期，不是物理生命周期。

### 4.4 EvidenceRef

证据链不能只存 ID，否则下层记忆删除后，上层摘要、事实、场景或画像会出现悬空引用。需要同时保存强引用和降级定位信息。

```ts
interface EvidenceRef {
  targetId: string;
  targetLayer: MemoryLayer;
  relation: "source" | "derived_from" | "supports" | "conflicts" | "supersedes";
  confidence: number;

  fallbackLocator?: {
    sessionId?: string;
    messageIds?: string[];
    timeRange?: [string, string];
    topicTags?: string[];
    textHash?: string;
    excerpt?: string;
  };
}
```

如果 L1 或 L2 被删除，上层记忆中指向它的 evidenceRef 不直接失效，而是退化为 fallbackLocator，从 L0 独立检索恢复证据。

### 4.5 图模型

节点类型：

1. User
2. Session
3. Message
4. Event
5. Fact
6. Object
7. Task
8. Scene
9. Persona
10. Time
11. Summary

边类型：

1. PARTICIPATE
2. DERIVED_FROM
3. SUPPORTS
4. CONFLICTS
5. RELATED_TO
6. CAUSES
7. TRIGGERS
8. PART_OF
9. SEQUENTIAL
10. DEADLINE_FOR
11. SUPERSEDES

超边建议在 MVP 中先用 Event 节点表达：一个完整事件本身作为节点，用户、对象、地点、时间、动作都作为边连接到该 Event。后续如果需要高性能 n-ary relation，再引入 hyperedge table。

## 5. 写入路径

### 5.1 L0 捕获

流程：

1. Host Adapter 接收用户消息、Agent 回复、工具调用摘要和关键运行上下文。
2. L0Recorder 将原始消息 append-only 写入 L0。
3. 为 L0 建立全文索引和可选向量索引。
4. 写入 checkpoint，保证后续 L1 摘要生成可断点续跑。

规则：

1. L0 不降噪、不改写、不删除。
2. 对敏感字段只做外部可配置脱敏策略，原始保真和安全合规需要通过策略开关权衡。
3. embedding 失败不阻断写入，只标记 index_degraded。

### 5.2 L0 -> L1 上下文摘要

L1 只负责把一段 L0 原文压缩成局部上下文胶囊，不直接承担结构化事实职责。

L1 摘要胶囊结构：

```ts
interface ContextCapsule {
  id: string;
  title: string;
  timeRange: [string, string];
  sessionId: string;
  sourceMessageIds: string[];
  taskGoal?: string;
  localContext: string;
  keyTurns: string[];
  decisionsMentioned: string[];
  unresolvedQuestions: string[];
  emotionalOrPragmaticTone?: string;
  topicTags: string[];
  confidence: number;
}
```

L1 的目标是降低 L0 和 L2 之间的语义跨度：

1. 保留局部语境，而不是只留下事实。
2. 记录任务阶段、转折点、用户纠正和上下文动机。
3. 支持后续 L2 抽取事实时判断“这是临时要求还是长期偏好”。
4. 支持 L2/L3/L4 下钻时先读摘要，再决定是否回到 L0 原文。

### 5.3 L1 -> L2 原子抽取

L2 记录可复用、可验证、可结构化的事实。

L2 类型：

1. preference：用户偏好
2. constraint：约束
3. project_fact：项目事实
4. decision：阶段性决策
5. issue：问题或故障
6. resolution：解决方案
7. task_state：任务状态
8. temporal_fact：时间相关事实
9. lesson：经验、教训、失败模式

抽取要求：

1. 每条 L2 必须带 evidence 指向 L1 摘要胶囊。
2. 每条 L2 必须保留 L0 fallback locator。
3. 每条 L2 必须带 confidence 和 extraction_reason。
4. 相似 L2 先 dedup，再决定 merge、supersede、conflict 或 coexist。
5. 对用户稳定偏好和一次性指令做区分，避免把临时要求写成长期画像。

### 5.4 L2 -> L3 场景归纳

L3 是围绕项目、任务、主题、关系网络形成的场景块。

场景块结构：

```ts
interface SceneBlock {
  id: string;
  title: string;
  scope: "project" | "task" | "topic" | "relationship" | "workflow";
  summary: string;
  activeFacts: string[];
  historicalFacts: string[];
  openQuestions: string[];
  knownRisks: string[];
  evidence: EvidenceRef[];
  l0Locators: EvidenceRef["fallbackLocator"][];
}
```

L3 不应只是 L2 拼接，而要完成三件事：

1. 聚合：将碎片事实组织成上下文。
2. 解释：说明这些事实为什么属于同一个场景。
3. 导航：提供下钻到 L2/L1/L0 的路径。

### 5.5 L3 -> L4 画像蒸馏

L4 只保留跨场景稳定成立的用户特征。

可进入 L4 的内容：

1. 稳定沟通偏好
2. 长期技术偏好
3. 工作流约束
4. 核心价值判断
5. 长期项目背景

禁止直接进入 L4 的内容：

1. 单次任务的临时要求
2. 未经多证据支持的推断
3. 与近期证据冲突但未解决的旧偏好
4. 只对某个项目有效却未标明 scope 的规则

L4 更新必须走自治证据门：

1. 自动生成候选画像片段。
2. 检查证据数量、跨场景一致性、冲突情况。
3. 低置信度候选停留在 L3 或 L2。
4. 高置信度候选写入 L4，并保留 evidence。
5. 用户不逐条审核 L4 候选；系统通过置信度、证据链、冲突检测、观察期和可回滚审计保证质量。

## 6. 生命周期管理

### 6.1 两类分数

必须分离 residentScore 和 retrievalScore。

residentScore 用于生命周期治理，反映记忆在长期系统中的保留价值：

```text
ResidentScore(m) =
  a * AccessUtility(m)
+ b * TimeDecay(m)
+ c * StructuralRank(m)
+ d * TaskAffinityStable(m)
+ e * EvidenceQuality(m)
+ f * UserPinnedBoost(m)
+ g * ConflictPenalty(m)
```

retrievalScore 用于当前任务召回，反映记忆对当前 query 的即时相关性：

```text
RetrievalScore(m, q) =
  r1 * BM25(m, q)
+ r2 * SemanticSimilarity(m, q)
+ r3 * PersonalizedPageRank(m, q)
+ r4 * RecencyForQuery(m, q)
+ r5 * LifecyclePrior(m)
+ r6 * AbstractionFit(m, q)
+ r7 * DiversityBonus(m)
- r8 * ObservationPenalty(m)
```

原因：

1. 当前任务相关不等于长期有价值。
2. 长期沉寂不等于当前不该召回。
3. 如果用同一个分数控制删除和召回，会放大马太效应。

### 6.2 访问信号分级

访问不能只计“被检索到”。

信号强度：

1. candidate_hit：只进入候选集，弱信号。
2. injected：进入上下文，中等信号。
3. cited_by_agent：Agent 实际引用，强信号。
4. user_confirmed：用户确认有用，最强信号。
5. user_rejected：用户指出无关或错误，负信号。

生命周期只应主要使用 cited_by_agent 和 user_confirmed，避免系统因为自己的召回结果不断强化自己。

### 6.3 短期记忆分代模型

短期记忆类似年轻代，但这里的资源不是存储空间，而是注意力预算。

短期状态字段：

1. age：成功存活的治理周期数。
2. compressionTier：压缩程度，0 最完整，4 最小胶囊。
3. protectionTier：保护等级，5 最高，1 最低。
4. observationUntil：观察期截止时间。

短期流转：

```text
new short memory
  -> active short
  -> survivor short, age + 1
  -> promotion candidate
  -> long memory
```

晋升条件：

1. residentScore 连续 N 个周期高于 promotion_threshold。
2. usefulAccessCount 达到最低要求。
3. evidenceQuality 达标。
4. 没有未解决高置信冲突。
5. long 区注意力预算安全。

动态晋升阈值：

```text
promotion_threshold = base
  + pressure(long_budget_usage)
  + pressure(recent_promotion_rate)
  - boost(user_pinned_or_confirmed)
```

对应 JDK 的 age table：系统每轮统计不同 age、不同 layer、不同 scene 的短期记忆数量和 token 成本。如果 survivor 区压力过大，提高晋升门槛；如果长期区增长缓慢且高价值短期过多，降低晋升门槛。

### 6.4 长期记忆降级

长期记忆不参与直接删除和压缩，但可以降级为短期。

降级条件：

1. residentScore 长期低于 demotion_threshold。
2. 最近 M 个周期没有 useful access。
3. 被新版本 supersede。
4. 与高置信新证据冲突且未被用户确认保留。
5. 所属场景被归档或项目结束。

降级不是跨 L0-L4 层级移动，而是在同一层级中改变 lifecycleState：

```text
L4 long persona -> L4 short persona_review_candidate
L3 long scene   -> L3 short scene
L2 long fact    -> L2 short fact
L1 long summary -> L1 short summary
```

L4 默认不自动删除，但如果发生明确冲突，应进入“待复核”状态，而不是静默保留。

### 6.5 压缩、观察期、删除

建议将用户草案里的 L1-1 到 L1-5 改名，避免和 L1 存储层混淆：

```text
C0: 原文或近原文，最高保护
C1: 结构化短摘要
C2: 摘要 + 关键字段
C3: 标签 + 时间 + 来源定位
C4: 极致胶囊，进入观察期
```

短期记忆沉寂时：

```text
C0 -> C1 -> C2 -> C3 -> C4 observing -> deleted
```

C4 胶囊必须保留：

1. keywords
2. time range
3. source locator
4. content hash
5. parent scene or graph node
6. one-line gist

观察期规则：

1. 观察期内仍参与检索。
2. observationPenalty 大幅压低排名。
3. 只有精确关键词、强语义相似、图路径强相关才能拉回 Top-K。
4. 一旦被 useful access，退出观察期，compressionTier 回升。
5. 观察期结束仍无 useful access，非 L0 记忆可 hard delete。

删除独立性：

1. 删除 L1 不删除 L0。
2. 删除 L1 不必删除 L2/L3/L4，但上层 evidenceRef 要降级为 fallbackLocator。
3. 删除 L2 不删除它引用的 L1/L0，也不必删除 L3/L4。
4. 删除 L3 不删除它包含的 L2，也不必删除 L4。
5. 删除图边不删除两端节点。
6. 所有删除先写 tombstone，再由后台清理物理数据，便于回滚和审计。

### 6.6 Promotion Failure 处理

借鉴 JDK promotion failure，晋升长期记忆可能失败。

失败原因：

1. long 区注意力预算超限。
2. 与现有长期记忆冲突。
3. 证据链不完整。
4. LLM 抽取置信度不足。
5. 同类长期记忆过多，应该合并而非新增。

处理方式：

1. 保持 short 状态，不丢弃。
2. 增加 needs_autonomous_resolution 或 needs_merge 标记。
3. 尝试合并到已有 L2/L3/L4。
4. 延长观察周期。
5. 如果用户明确 pin，则绕过部分自动阈值，但仍保留冲突标记。

## 7. 召回路径

### 7.1 候选生成

所有层级可并行召回：

1. L0：原文、错误、命令、精确细节。
2. L1：局部摘要、任务阶段、上下文转折。
3. L2：事实、偏好、约束、决策、经验。
4. L3：项目、任务、主题场景。
5. L4：用户画像、长期工作方式。

召回通道：

1. BM25/FTS：精确名词、命令、错误、路径、术语。
2. Vector：语义相似。
3. Graph：邻居扩展、最小连通子图、冲突边。
4. PPR：从 query entry points 出发做个性化 PageRank。
5. Temporal：按时间范围、最近会话、deadline。
6. Entity：用户、项目、文件、任务、对象实体匹配。

### 7.2 双主动涌现窗口

系统保留 CogniFold 的主动窗口思想，但扩展为 query 前和 query 后两条链路。

query 前的全局主动窗口：

```text
Global Active Window
= 当前图状态
+ 活跃任务
+ 近期上下文
+ 结构中心节点
+ 未完成 intent
+ 时间紧迫性
```

它不是围绕某个问题找答案，而是在用户输入 query 前，让 Agent 先获得当前任务态势、长期约束、活跃场景和潜在风险。

query 后的查询触发涌现窗口：

```text
Query-Induced Emergence Window
= query 语义入口点
+ PPR 图扩散
+ 相似历史任务
+ 失败模式
+ 可复用策略
+ 冲突和约束节点
```

它也不只是普通检索，而是让图结构主动浮现“可能有用的经验”。这条链路特别适合回答：过去有没有类似问题、之前踩过什么坑、哪些约束会影响当前决策。

最终进入重排的候选集：

```text
Candidates =
  Global Active Window
+ Query-Induced Emergence Window
+ Direct BM25/Vector Retrieval
+ Explicit Drilldown Results
```

### 7.3 融合与重排

候选融合用 RRF，避免 BM25 和向量分数不可比。

重排特征：

1. 当前任务相关性。
2. 语义相似度。
3. 图结构相关性。
4. 时效性。
5. 生命周期先验。
6. 抽象层适配度。
7. 证据质量。
8. 多样性和去重。
9. 观察期惩罚。

抽象层适配度示例：

1. 用户问“之前原话怎么说”，优先 L0。
2. 用户问“当时上下文是什么”，优先 L1。
3. 用户问“这个约束是什么”，优先 L2。
4. 用户问“这个项目背景”，优先 L3。
5. 用户问“我长期偏好什么”，优先 L4。
6. 用户问具体 bug，L2 事实可能排在 L3 场景前面。

### 7.4 探索与利用平衡

为防止高频记忆垄断上下文：

1. 对同一 scene 设置 Top-K 占比上限。
2. 对长期未召回但结构中心度高的节点给 exploration bonus。
3. 对被连续召回但未被使用的记忆降低 useful weight。
4. 对观察期胶囊保留极小探索概率。

## 8. 上下文注入与 Headroom 层

Headroom 位于 retrieval rerank 之后：

```text
候选召回 -> RRF 融合 -> 重排 -> Top-K -> Headroom 压缩 -> 注入上下文
```

上下文分带：

1. immediate：当前任务必须看的事实、约束、最近事件。
2. working：相关场景、活跃项目状态、正在进行的 intent。
3. background：低频但结构重要的背景。
4. drilldown markers：可回取的证据链和压缩原文 hash。

压缩策略：

1. L4：通常直接注入短画像。
2. L3：注入场景摘要和关键事实列表。
3. L2：注入结构化事实和经验条目，保留 evidence marker。
4. L1：注入局部上下文摘要，保留 source marker。
5. L0：默认不全文注入，只注入片段或 retrieval marker。
6. 工具日志、长 JSON、搜索结果交给内容类型路由器压缩。

CCR 规则：

1. 所有被压缩的长内容都生成 retrieval marker。
2. marker 可按 hash 完整回取，也可按 query 在原文内 BM25 检索。
3. 如果 Agent 对压缩内容不确定，应优先 drilldown，而不是凭摘要推断。

## 9. 存储与索引方案

MVP 推荐 local-first：

1. SQLite：主元数据、L0/L1/L2 表、tombstone、checkpoint。
2. FTS5：L0/L1/L2/L3/L4 文本索引。
3. sqlite-vec 或可替换向量后端：语义检索。
4. graph tables：nodes、edges、hyperedge_events。
5. Markdown/JSON：L3/L4 可读文件。
6. refs：大原文、工具日志、压缩前内容。

后续可替换：

1. Tencent Cloud VectorDB：向量、稀疏向量、混合检索。
2. Redis：热缓存和锁。
3. 图数据库：复杂子图检索和在线 PageRank。

接口必须能力驱动：

```ts
interface StoreCapabilities {
  ftsSearch: boolean;
  vectorSearch: boolean;
  nativeHybridSearch: boolean;
  graphSearch: boolean;
  deferredEmbedding: boolean;
}
```

任何能力缺失都不应阻断主流程，只降级召回质量。

## 10. 模块拆分

建议模块：

```text
src/
  capture/
    l0-recorder
    host-adapter
  folding/
    l1-summarizer
    l2-extractor
    l2-deduper
    scene-builder
    persona-distiller
  graph/
    graph-store
    entity-index
    ppr-ranker
    subgraph-retriever
  lifecycle/
    score-engine
    age-table
    promotion-policy
    demotion-policy
    compression-policy
    retention-worker
  retrieval/
    bm25-retriever
    vector-retriever
    graph-retriever
    fusion
    reranker
  context/
    context-selector
    headroom-router
    ccr-store
    prompt-assembler
  storage/
    sqlite-store
    vector-store
    profile-store
    checkpoint-store
  observability/
    metrics
    trace
    audit-log
```

## 11. API 与工具面

核心 API：

1. `capture(message)`：写入 L0。
2. `fold(sessionId)`：从 L0 生成 L1 摘要，抽取 L2 事实，更新 L3/L4。
3. `recall(query, context)`：召回并返回可注入上下文。
4. `drilldown(memoryId | marker)`：沿证据链回查。
5. `feedback(memoryId, signal)`：记录有用、无用、错误、确认。
6. `runLifecycleCycle(scope)`：执行一轮生命周期治理。
7. `inspect(memoryId)`：查看分数、证据链、生命周期状态。
8. `pin(memoryId)` / `unpin(memoryId)`：显式用户意图信号，不作为内部治理默认前置条件。
9. `supersede(oldId, newMemory)`：版本替换。

MCP 工具：

1. `memory_remember`
2. `memory_recall`
3. `memory_drilldown`
4. `memory_feedback`
5. `memory_inspect`

## 12. 一致性与可靠性

写入原则：

1. L0 append-only，优先保证落盘。
2. L1/L2/L3/L4 可异步生成，失败可重试。
3. 每个 pipeline stage 有 checkpoint。
4. 抽取、聚合、画像更新必须幂等。
5. 图更新使用 UpdatePlan，执行前校验，失败可回滚。

并发策略：

1. 同一 session 的 fold 串行。
2. 不同 session 可并行。
3. L4 profile 使用 optimistic lock，避免并发覆盖。
4. lifecycle worker 只处理稳定 checkpoint 之后的数据。

降级策略：

1. embedding 不可用：用 FTS。
2. 图计算失败：用 BM25 + vector。
3. LLM 抽取失败：保留 L0，等待下一轮。
4. Headroom 压缩失败：注入未压缩短摘要或跳过长内容。
5. store 部分不可用：进入 degraded mode，主对话不应崩溃。

## 13. 评估指标

记忆质量：

1. 事实准确率。
2. 证据链可回溯率。
3. L4 画像误写率。
4. 冲突检测召回率。
5. 删除后 L0 可恢复率。

召回质量：

1. Recall@K。
2. MRR/NDCG。
3. 多跳问题命中率。
4. 观察期胶囊恢复率。
5. 无关记忆注入率。

生命周期质量：

1. 晋升准确率。
2. 错误降级率。
3. 被删除后再次需要的比例。
4. 长期区增长率。
5. 各层级 age 分布。

上下文效率：

1. 注入 token 数。
2. 压缩率。
3. drilldown 次数。
4. drilldown 成功率。
5. 因压缩导致的任务失败率。

## 14. 开发里程碑

### Phase 0：规格冻结

产物：

1. 数据模型定义。
2. 生命周期状态机图。
3. scoring 配置文档。
4. API 契约。
5. MVP 评估集。

验收：

1. 能解释任意 MemoryUnit 的层级、生命周期、证据链和状态。
2. L0 永久性、L1/L2 删除、L3/L4 fallback 规则明确。

### Phase 1：L0/L1 基础闭环

产物：

1. L0 append-only recorder。
2. L1 summarizer。
3. SQLite + FTS5。
4. source_message_ids 证据链。
5. 基础 recall。

验收：

1. 原始消息可写入、检索、下钻。
2. L1 删除不影响 L0 检索。
3. embedding 缺失时系统可用。

### Phase 2：L2 原子事实闭环

产物：

1. L2 extractor 和 deduper。
2. L2 conflict/supersede 处理。
3. L2 -> L1 -> L0 证据链。
4. L0/L1/L2 并行召回。

验收：

1. L1 摘要可生成 L2 结构化事实。
2. L2 删除不影响 L1/L0。
3. 用户临时指令和长期偏好能被区分。

### Phase 3：混合检索与证据下钻

产物：

1. BM25 + vector + RRF。
2. L0/L1/L2 并行召回。
3. reranker。
4. drilldown API。

验收：

1. 精确查询走 FTS 命中。
2. 同义查询走 vector 命中。
3. L2 -> L1 -> L0 证据链完整。

### Phase 4：L3/L4 折叠

产物：

1. Scene builder。
2. Persona distiller。
3. L3/L4 Markdown 或 JSON profile。
4. optimistic profile sync。

验收：

1. 多条 L2 能归并为 L3 场景。
2. L4 只吸收稳定跨场景特征。
3. L4 可下钻到 L3/L2/L1/L0。

### Phase 5：生命周期与分代治理

产物：

1. residentScore。
2. age table。
3. promotion/demotion policy。
4. compression/observation/delete worker。
5. tombstone 和 audit log。

验收：

1. 短期记忆能晋升长期。
2. 长期记忆能降级短期。
3. C4 观察期可被精确召回救回。
4. 删除不破坏上层概念和 L0。

### Phase 6：异构图与 PPR

产物：

1. graph nodes/edges。
2. entity index。
3. PPR ranker。
4. subgraph retriever。
5. conflict/supersede edges。

验收：

1. 多实体查询能召回最小相关子图。
2. 结构中心节点可影响 residentScore。
3. 冲突记忆可被定位和解释。

### Phase 7：Headroom 上下文网关

产物：

1. context bands。
2. content router。
3. CCR store。
4. retrieval marker。
5. prompt assembler。

验收：

1. Top-K 进入上下文前可压缩。
2. 长内容可按 marker 回取。
3. 压缩不破坏证据链。

### Phase 8：评估、可观测性与运维

产物：

1. benchmark scripts。
2. metrics dashboard。
3. diagnostic export。
4. lifecycle simulation。
5. regression test suite。

验收：

1. 每次策略调整能看到召回、生命周期和 token 成本变化。
2. 能导出单条记忆完整解释：为什么被召回、为什么被晋升、为什么被删除。

## 15. 关键风险与缓解

### 15.1 递归强化导致旧记忆永远赢

风险：被召回越多，权重越高，越容易继续被召回。

缓解：

1. 区分 candidate_hit 和 useful access。
2. 增加 exploration bonus。
3. 设置 scene 占比上限。
4. 对未被使用的重复召回施加惩罚。

### 15.2 L4 画像过度概括

风险：把一次性指令沉淀成长期偏好。

缓解：

1. L4 需要跨场景证据。
2. L4 写入自治证据门，不进入用户逐条审核队列。
3. L4 片段带 scope 和 confidence。
4. 冲突时进入 pending_autonomous_resolution，由后台策略继续处理并保留审计。

### 15.3 删除破坏证据链

风险：L1 或 L2 删除后，上层 L3/L4 指向空 ID。

缓解：

1. evidenceRef 同时存 targetId 和 fallbackLocator。
2. 删除先 tombstone。
3. L3/L4 下钻失败时自动转 L0 检索。
4. 定期跑 dangling evidence repair。

### 15.4 观察期记忆干扰正常召回

风险：低价值胶囊仍频繁进入 Top-K。

缓解：

1. observationPenalty 大幅压低。
2. 只允许精确匹配、强语义、强图路径救回。
3. 观察期召回默认只返回 marker，不返回完整内容。

### 15.5 图结构膨胀

风险：所有事实都建边，PPR 成本上升，噪声传播。

缓解：

1. 边有 confidence 和 decay。
2. 弱边不进入默认 PPR。
3. 场景内局部图优先，全局图低频重算。
4. 定期合并重复 entity。

### 15.6 压缩造成误判

风险：摘要遗漏关键细节，Agent 基于摘要做错决策。

缓解：

1. Headroom 只改变注入形态，不改变底层记忆。
2. 所有压缩内容有 retrieval marker。
3. 高风险任务要求自动 drilldown。
4. 对用户纠错回写 compression feedback。

## 16. 推荐 MVP 技术路线

优先选择 TypeScript/Node 作为产品 runtime，因为 TencentDB-Agent-Memory 已具备 OpenClaw/Hermes 接入、L0/L1 store、hook 和配置基础。新增 L1 摘要层后，原 TencentDB 的 L1 能力应整体迁移为 L2 原子事实能力。CogniFold 的图算法和 Headroom 的压缩思想先以接口和算法移植方式吸收，不在 MVP 引入多语言运行时依赖。

建议：

1. MVP 以 TypeScript 实现 core service。
2. SQLite + FTS5 + sqlite-vec 做默认本地后端。
3. VectorDB 和图数据库通过接口预留。
4. Python 只用于 benchmark、离线分析或策略实验。
5. Headroom 可先实现轻量 ContentRouter + CCR，后续再接完整压缩器。

## 17. 最小可用闭环

最小可用版本必须完成这一条链：

```text
用户对话
  -> L0 原文保存
  -> L1 摘要胶囊生成
  -> L2 事实抽取
  -> BM25/向量召回
  -> Top-K 重排
  -> 上下文注入
  -> Agent 使用
  -> feedback 更新 useful access
  -> 生命周期 worker 调整 age/protection/compression
  -> 证据链可下钻回 L0
```

只有这个闭环跑通之后，再上 L3/L4、PPR、观察期删除和 Headroom 深度压缩。否则系统容易先复杂化，但无法证明记忆真的改善了 Agent 行为。

## 18. 下一步方向点

以下是开发方向点，不是 ALTM 运行时人工审核点：

1. MVP 是否以 TypeScript 为主 runtime。
2. L0 是否绝对永久保留，还是允许用户配置物理保留周期。
3. L4 自动写入策略采用哪些自治阈值、证据门和回滚策略。
4. 生命周期 worker 的默认周期：每 N 次任务、每天、或两者结合。
5. 初版是否实现真正 hard delete，还是先只 tombstone。
6. Headroom 初版是轻量内置，还是直接外接已有 headroom 服务。

## 19. 人工审核误引入清单与纠偏要求

以下内容是此前因过度保守而引入或强化的人审/确认链路，后续必须从 ALTM 默认主链路中移除或改造成自治策略：

1. L2 `review_status=pending/approved/rejected` 被当作事实进入后续治理和召回的门槛。
2. `Review Queue` 被设计为 L2、L3、L4、语义去重、跨 session L3 候选的统一处理入口。
3. `review_mark` 需要外部把候选标记为 approved/rejected，导致内部治理依赖人工或外部操作推进。
4. `review_plan` / `review_apply` 把记忆治理拆成“人工审核后生成 action plan，再确认执行”的流程。
5. `requires_second_confirmation` 和 `second_confirm` 被用于 promotion、demotion、L3 激活、L4/persona 激活等动作，形成默认阻塞。
6. L4/persona 候选生成后停在 `observing + pending`，需要 approve + second confirm 才能成为 `active + permanent`。
7. 跨 session L3 候选先写 `cross_session_l3_candidate`，再经 review approve/apply 才能进入 confirmed/materialized 状态。
8. `maintenance-cycle` 默认接入 `apply_review_actions`，实质上把维护主循环建立在已审核 action plan 之上。
9. `Review audit` 本身可以保留为自动审计，但不应表达为用户审核模型，更不应成为治理前置条件。
10. 文档中的 `Review Gate`、`pending_review`、`Human-in-the-loop`、`人工确认` 等表述需要统一改为自治证据门、自动冲突处理、后台审计和可回滚策略。

纠偏原则：

1. 默认主链路必须自动推进，不等待用户审批。
2. 用户显式反馈只作为强信号或产品操作，不作为内部治理门锁。
3. 保留审计、解释、回滚和调试接口，但这些接口不阻塞自治维护。
4. 把所有 `approved/rejected/pending` 语义重新定义为系统内部策略状态，或替换为 `confidence/resolution_status/autonomous_decision`。

## 20. 审计必要性评估与模型替代策略

本节用于把此前误引入的人工审核点重新拆解为“自治审计点”。审计的目标不是让用户审批，而是让系统在完全自动化运行时具备可解释、可回滚、可诊断的治理能力。

审计分级：

1. P0 极高：会长期改变用户画像、跨会话场景、证据链、删除状态或 canonical 关系。必须自动审计，必须有模型或规则评估，必须可回滚。
2. P1 高：会改变召回权重、生命周期状态或默认注入行为。必须自动审计，建议有轻量模型或统计策略辅助。
3. P2 中：主要影响候选排序、观察期、诊断视图或调试入口。保留事件和指标即可，必要时异步评估。
4. P3 低：纯查询、只读报告、开发调试命令。默认不进入核心审计，只保留操作日志。

### 20.1 从高到低的审计必要性排序

| 排名 | 原被人审化的环节 | 审计必要性 | 为什么需要审计 | 是否适合 LLM/小模型替代人工 | 自治替代方案 |
| --- | --- | --- | --- | --- | --- |
| 1 | 语义去重合并、tombstone、restore | P0 极高 | 会改变 canonical/duplicate 关系，错误合并会损坏事实差异和证据链 | 适合，而且应使用模型组合 | embedding 相似度 + NLI/矛盾检测小模型 + LLM judge 解释差异 + 事务回滚 |
| 2 | L4/persona 写入、激活、永久化 | P0 极高 | 会长期影响 Agent 对用户偏好、约束、风格的理解 | 适合，优先 LLM judge + 小模型筛查 | 多证据支持计数 + scope 检测 + 时间稳定性 + LLM 画像抽取/反证检查 |
| 3 | 跨 session L3 场景物化与激活 | P0 极高 | 会把不同会话事实组织成稳定场景，错误聚类会造成上下文污染 | 适合，embedding 召回后必须二次语义判别 | embedding 聚类 + cross-encoder/reranker + LLM 场景一致性评分 + 观察期 |
| 4 | 冲突解决、supersede、旧记忆降权 | P0 极高 | 会决定新旧事实谁覆盖谁，影响后续召回和画像 | 适合，尤其适合 NLI/LLM | 时间戳优先级 + 来源可信度 + NLI 冲突分类 + LLM 生成裁决理由 |
| 5 | hard delete 或物理清理 | P0 极高 | 一旦物理删除，证据链和回滚能力可能永久受损 | 不建议只靠 LLM，应规则主导 | L0 默认不删；非 L0 先 tombstone；物理删除只由保留策略、TTL、用户显式删除请求触发 |
| 6 | L2 原子事实进入可信事实池 | P1 高 | L2 是 L3/L4 的基础，错误原子会被上层放大 | 适合，小模型优先，LLM 兜底 | 抽取置信度 + schema 校验 + 事实完整性检查 + LLM 自检 |
| 7 | promotion/demotion 生命周期转换 | P1 高 | 会改变长期保留、召回优先级和保护等级 | 部分适合，统计/规则优先 | residentScore 趋势 + 访问强度 + evidence quality + 异常检测模型 |
| 8 | 默认 Active Window 注入与生命周期反馈 | P1 高 | 错误注入会形成正反馈循环，影响上下文质量 | 适合轻量模型辅助 | 任务相关性 reranker + 注入后反馈衰减 + 重复注入惩罚 |
| 9 | maintenance-cycle 自动执行治理动作 | P1 高 | 是自治治理总入口，错误编排会放大多个步骤的风险 | 适合流程级策略模型 | action budget + dry-run summary + 异常检测 + 自动降级 |
| 10 | Review Queue / review_mark / review_plan / review_apply | P2 中 | 作为人工审核模型不该进主链路；作为调试/审计视图仍有价值 | 不需要作为决策模型 | 改名为 autonomous_decision_log / governance_plan_log，只做解释和回放 |
| 11 | Review audit projections / audit summary | P2 中 | 有助于观察系统行为，但不应阻塞治理 | 可选，用于异常摘要 | 保留 append-only event + projection；LLM 只生成巡检摘要 |
| 12 | 文档中的 Review Gate / pending_review / second_confirm 表述 | P3 低 | 概念污染高，但运行时风险来自实现，不来自文字本身 | 不需要模型 | 统一替换为自治证据门、自动冲突处理、后台审计 |

### 20.2 P0 环节的模型替代方案

P0 不允许回退到人工审核，也不允许单模型拍脑袋决策。推荐使用“规则硬门 + 小模型判别 + LLM 解释 + 可回滚事务”的组合。

#### 20.2.1 语义去重与 tombstone

目标：判断两条 L2 是否真的是同一事实、同一偏好、同一约束或同一经验。

推荐模型链：

1. embedding：召回高相似候选。
2. cross-encoder 或 reranker 小模型：判断两段文本是否语义等价。
3. NLI/contradiction 小模型：判断是否存在矛盾、强弱约束差异、否定词差异。
4. LLM judge：输出结构化裁决 `{same_fact, nuance_loss_risk, contradiction, canonical_reason}`。

自动决策：

1. 全部高置信一致：自动 protected merge。
2. 存在细微差异：保留双记忆，建立 related/similar edge。
3. 存在矛盾：进入 autonomous_conflict_resolution，不合并。
4. tombstone 必须保留 `superseded_by`、merge reason、rollback payload。

#### 20.2.2 L4/persona 自动写入

目标：把跨场景稳定偏好沉淀为长期画像，而不是把单次指令误写成长期人格。

推荐模型链：

1. evidence counter：要求来自多个 session、多个时间点或多个 L3/L2 支撑。
2. scope classifier 小模型：判断是全局偏好、项目偏好、临时任务要求还是一次性指令。
3. contradiction detector：检测与已有 L4 是否冲突。
4. LLM persona judge：输出 `{is_stable, scope, confidence, counter_evidence, rewrite}`。

自动决策：

1. 高证据 + 高稳定性：自动写入 L4，但先进入 observing。
2. 中等证据：保留 L3/L2，不写 L4。
3. 有冲突：写入 conflict edge，触发后台重评。
4. 激活为 permanent 必须由系统连续多周期验证，而不是用户二次确认。

#### 20.2.3 跨 session L3 场景物化

目标：把跨会话相似事实组织成“场景”，但避免把不同项目、不同语境硬合并。

推荐模型链：

1. embedding：发现跨 session 候选。
2. reranker：验证两条 L2 是否属于同一主题或同一工作流。
3. session boundary classifier：判断是否存在项目/时间/上下文边界冲突。
4. LLM scene judge：输出 `{same_scene, scene_type, boundary_risk, evidence_summary}`。

自动决策：

1. same_scene 高置信：自动物化为 L3 observing。
2. scene_type 相同但边界风险高：只建 weak edge，不物化。
3. 物化后的 L3 先通过观察期、召回反馈和证据稳定性自动晋升。

#### 20.2.4 冲突解决与 supersede

目标：当新旧记忆冲突时，让系统自主判断保留、降权、替代或并存。

推荐模型链：

1. NLI：entails / contradicts / neutral。
2. recency and source scorer：新旧证据时间、来源、置信度、使用次数。
3. LLM conflict resolver：输出 `{resolution, keep_old, keep_new, scope_split, reason}`。

自动决策：

1. 新证据更强且同 scope：旧记忆降权或 supersede。
2. scope 不同：拆分 scope 并保留两者。
3. 证据不足：两者保留，降低主动注入概率。

#### 20.2.5 hard delete

目标：清理无价值或过期数据，同时不破坏证据链。

模型定位：

1. LLM 不适合单独决定物理删除。
2. 小模型可用于识别敏感数据、低价值噪声、重复日志。
3. 删除决策应由策略、TTL、用户显式删除请求、法规约束和证据引用计数共同决定。

自动决策：

1. L0 默认永久保留或按用户配置 TTL。
2. 非 L0 先 tombstone，再后台清理。
3. 存在上游引用时禁止物理删除，只允许降权或压缩。

### 20.3 模型替代人工的工程原则

1. LLM/小模型是自治审计器，不是人工审批器。
2. 任何模型判断必须输出结构化结果、置信度、证据引用和失败原因。
3. 高风险动作不得只依赖单个 LLM 结论，至少要有规则硬门或第二模型交叉验证。
4. 模型低置信时不阻塞用户，而是选择更保守的自动动作：观察、降权、弱边、保留双版本、延后物理清理。
5. 所有 P0/P1 决策必须进入 append-only audit log，用于解释、调试、回滚和离线改进。
6. 审计结果不要求用户处理；用户只在显式打开调试面板或主动纠错时看到。

## 21. 重点开发任务：自动审核治理工程

自动审核治理工程是 ALTM 从“带有人审影子的治理系统”转向“完全自治记忆系统”的核心工程。它的目标不是增加新的审批流程，而是用规则、小模型、LLM judge、事务回滚和审计日志替代此前误引入的人工审核主链路。

当前实现状态：自动审核治理工程已完成首个完整闭环。已新增 `AutonomousGovernanceEngine`，复用 `review_events` 写入 `autonomous_governance_*` 事件，并接入语义去重、跨 session L3、L4/persona 三条 P0 链路。`maintenance-cycle` 默认调用自治治理，旧 `review_apply` 只保留为显式兼容路径。自治决策已包含规则硬门、本地小模型评分、可选 OpenAI-compatible LLM judge、evaluated/decided/applied/degraded/rolled_back 审计事件和统一 rollback 入口。

### 21.1 已确认架构决策

本阶段用户已明确选择：

1. 工程形态：新增独立 `Autonomous Governance Engine`，统一接管 L2/L3/L4/语义去重/生命周期治理的自动评估、决策、审计与执行。
2. 数据模型：复用现有 `review_events` 存储，但进行语义重铸；表名可暂时保留，事件类型和 metadata 改为 `autonomous_governance_*` 语义。
3. 模型栈：采用“规则硬门 + 小模型 + LLM judge”的组合，不使用人工审核。
4. 首批范围：直接搭建引擎骨架，并三线并行接入语义去重、跨 session L3、L4/persona 三条 P0 链路。
5. 旧语义处理：旧 `review/approve/reject/second_confirm` 逐步降级为兼容层，不再作为主链路概念。
6. 模型不可用策略：模型不可用或低置信时，规则兜底继续执行，不让维护周期停止。
7. P0 执行权：高置信自动决策允许直接改变长期状态，例如 active/permanent/tombstone/materialized。
8. 接入顺序：三线并行推进，减少分阶段保守开发造成的总体拖慢。

### 21.2 核心模块设计

```text
maintenance-cycle
  -> AutonomousGovernanceEngine
       -> CandidateCollector
       -> RuleGateEvaluator
       -> SmallModelEvaluator
       -> LLMJudgeEvaluator
       -> DecisionCombiner
       -> GovernanceExecutor
       -> AutonomousAuditLogger
       -> RollbackPayloadBuilder
```

模块职责：

1. `CandidateCollector`：从 L2、graph edge、L3/L4 候选、生命周期候选中收集待治理对象。
2. `RuleGateEvaluator`：执行硬规则，包括证据数量、scope、时间稳定性、风险 token、结构化字段一致性、否定/矛盾词检查。
3. `SmallModelEvaluator`：接入 embedding、reranker、NLI、scope classifier、session boundary classifier 等轻量模型。
4. `LLMJudgeEvaluator`：在 P0 动作上输出结构化判断和解释，例如 `same_fact`、`same_scene`、`is_stable_persona`、`conflict_resolution`。
5. `DecisionCombiner`：合并规则、小模型、LLM 输出，生成统一 `AutonomousGovernanceDecision`。
6. `GovernanceExecutor`：按决策直接执行 merge、tombstone、L3 物化/激活、L4 写入/永久化、supersede、降权等动作。
7. `AutonomousAuditLogger`：复用 `review_events` 写入自治事件，但事件语义改为系统自动治理。
8. `RollbackPayloadBuilder`：为 P0/P1 写入回滚 payload，并通过统一 rollback 入口回滚自治创建的 L3/L4 与语义合并。

### 21.3 统一决策对象

```ts
interface AutonomousGovernanceDecision {
  id: string;
  targetType: "memory_unit" | "graph_edge" | "lifecycle_candidate" | "governance_batch";
  targetId: string;
  actionType:
    | "merge_duplicate"
    | "tombstone_duplicate"
    | "materialize_l3_scene"
    | "activate_l3_scene"
    | "write_l4_persona"
    | "activate_l4_persona"
    | "supersede_memory"
    | "downgrade_memory"
    | "keep_both"
    | "create_weak_edge"
    | "defer";
  riskTier: "P0" | "P1" | "P2" | "P3";
  decision: "execute" | "defer" | "keep" | "downgrade" | "observe";
  confidence: number;
  ruleScore: number;
  smallModelScore?: number;
  llmJudgeScore?: number;
  evidenceRefs: EvidenceRef[];
  reasons: string[];
  modelOutputs: Record<string, unknown>;
  fallbackMode?: "none" | "rule_only" | "small_model_only" | "llm_unavailable";
  rollbackPayload?: Record<string, unknown>;
}
```

原则：

1. 不再使用 `approved/rejected/pending` 表达自治主链路状态。
2. 使用 `execute/defer/keep/downgrade/observe` 表达系统自动决策。
3. 所有 P0/P1 决策必须写审计事件。
4. 高置信 `execute` 可直接修改长期状态，不等待用户。
5. 低置信或模型不可用时，按用户选择启用规则兜底继续执行。

### 21.4 复用 review_events 的语义重铸

旧表可暂时保留，避免立即做大规模 schema 迁移，但事件语义必须改造：

1. `review_mark` -> 废弃为兼容事件，不进入默认主链路。
2. `review_apply` -> 废弃为兼容事件，不进入默认主链路。
3. 新增事件类型：
   - `autonomous_governance_evaluated`
   - `autonomous_governance_decided`
   - `autonomous_governance_applied`
   - `autonomous_governance_degraded`
   - `autonomous_governance_rolled_back`
4. metadata 必须包含：
   - `decision_id`
   - `risk_tier`
   - `action_type`
   - `decision`
   - `confidence`
   - `policy_version`
   - `model_chain`
   - `fallback_mode`
   - `evidence_refs`
   - `rollback_payload_ref`

这相当于复用底层 append-only event store，不复用人工审核语义。

### 21.5 三条 P0 链路并行设计

#### 21.5.1 语义去重自治链路

目标：替换 `semantic_duplicate_candidate -> Review Queue -> approve -> apply`。

新链路：

```text
L2 candidates
  -> embedding similarity
  -> RuleGateEvaluator
  -> reranker / NLI
  -> LLM duplicate judge
  -> AutonomousGovernanceDecision
  -> protected merge / tombstone / keep_both / weak_edge
  -> autonomous_governance_applied
```

高置信执行：

1. 等价事实：直接 protected merge。
2. 重复且低价值副本：直接 tombstone duplicate。
3. 有细微差异：保留双版本，创建 related edge。
4. 存在矛盾：进入 conflict resolution，不合并。

#### 21.5.2 跨 session L3 自治链路

目标：替换 `cross_session_l3_candidate -> review approve -> confirmed -> materialized`。

新链路：

```text
cross-session L2 embeddings
  -> candidate edge
  -> session boundary classifier
  -> reranker
  -> LLM scene judge
  -> AutonomousGovernanceDecision
  -> materialize_l3_scene / activate_l3_scene / weak_edge / defer
  -> autonomous_governance_applied
```

高置信执行：

1. same_scene 高置信：直接物化 L3。
2. 证据强且边界风险低：可直接 active L3。
3. 主题相似但边界不清：只建 weak edge。
4. 项目/时间/scope 冲突：defer 并写冲突审计。

#### 21.5.3 L4/persona 自治链路

目标：替换 `L4 observing candidate -> approve -> second_confirm -> active permanent`。

新链路：

```text
reviewed/observed L2-L3 evidence
  -> evidence counter
  -> scope classifier
  -> contradiction detector
  -> LLM persona judge
  -> AutonomousGovernanceDecision
  -> write_l4_persona / activate_l4_persona / keep_l3 / downgrade
  -> autonomous_governance_applied
```

高置信执行：

1. 多 session、多周期、多证据一致：直接写入 L4。
2. 稳定偏好且无冲突：直接 active/permanent。
3. scope 只属于项目：写入 scoped persona，不污染全局。
4. 与旧 L4 冲突：触发 supersede 或 scope split。

### 21.6 模型不可用时的规则兜底

用户已选择“规则兜底执行”，因此系统不能因为 LLM 或小模型不可用而停止自治治理。

兜底策略：

1. P0 语义去重：使用更高 embedding 阈值、结构化字段一致、否定词一致、scope 一致作为执行门。
2. P0 L3：使用更高相似度阈值、相同 atom_type、多证据 session 支持、无 session boundary 冲突 token 作为执行门。
3. P0 L4：使用多来源证据计数、时间稳定性、无冲突 L4、明确 scope 作为执行门。
4. 小模型/LLM 不可用时写入 `fallback_mode=rule_only` 或 `small_model_only`。
5. 规则兜底仍允许高置信直接执行，但阈值应高于模型齐全时。
6. 当 `ALTM_LLM_BASE_URL`、`ALTM_LLM_API_KEY`、`ALTM_LLM_MODEL` 可用时，`model_mode=auto|llm` 会调用 OpenAI-compatible LLM judge 生成结构化评分和理由。

### 21.7 接口设计

新增 Application Service：

```python
def autonomous_governance_cycle(
    scope: str = "all",
    include_p0: bool = True,
    include_p1: bool = True,
    model_mode: str = "auto",
    rule_fallback: bool = True,
    dry_run: bool = False,
    limit: int = 1000,
) -> dict[str, object]:
    ...
```

新增 CLI：

```bash
.venv/bin/python -m altm.cli autonomous-governance-cycle \
  --db /tmp/altm.sqlite3 \
  --scope all \
  --model-mode auto \
  --rule-fallback
```

新增 MCP：

```text
memory_autonomous_governance_cycle(scope, include_p0, include_p1, model_mode, rule_fallback, dry_run, limit)
memory_autonomous_governance_rollback(target_type, target_id, reason)
```

维护周期集成：

1. `maintenance-cycle` 不再调用 `apply_review_actions` 作为默认主链路。
2. `maintenance-cycle` 默认调用 `autonomous_governance_cycle`。
3. `review_queue/review_plan/review_apply` 保留为兼容与调试接口，但从主链路摘除。

### 21.8 首批实施任务

三线并行开发，但按提交粒度分层推进：

1. [已完成] 新增 `altm.governance.autonomous`，定义决策对象、规则兜底链路和自治事件写入器。
2. [已完成] 复用 `review_events` 写入 `autonomous_governance_decided`、`autonomous_governance_applied`、`autonomous_governance_degraded`。
3. [已完成] 把 semantic dedup 的自动 merge/tombstone 接到 `AutonomousGovernanceEngine`。
4. [已完成] 把 cross-session L3 candidate/materialization 改为自治决策，不再依赖 approve/apply。
5. [已完成] 把 L4/persona activation 改为自治证据门，不再依赖 second_confirm。
6. [已完成] `maintenance-cycle` 接入 `autonomous_governance_cycle`，并默认关闭旧 `apply_review_actions` 主链路。
7. [已完成] 增加规则兜底、自治事件、CLI/MCP、默认无人工链路的测试。
8. [已完成] 接入本地小模型式启发评分和可选 OpenAI-compatible LLM judge evaluator。
9. [已完成] 增加自治治理 rollback 入口、CLI、MCP 与 rolled_back 审计事件。
10. [后续增强] 接入真实 cross-encoder/NLI/scope classifier 模型包，替换当前本地启发式小模型。

### 21.9 成功标准

1. 默认维护周期无需任何人工 mark/apply/confirm 也能推进 P0 治理。
2. P0 高置信动作可以直接执行，并写入自治审计事件。
3. 模型不可用时规则/本地小模型兜底仍可继续执行，并在审计中标记 `fallback_mode`。
4. 旧 Review Queue 不再是默认主链路依赖。
5. 每个 P0 决策都能解释：为什么执行、引用了哪些证据、模型/规则分数是多少、如何回滚。
6. 自治创建的 L3/L4 和语义合并可以通过统一 rollback 入口回滚。

当前验证基线：

```text
.venv/bin/python -m compileall src tests
.venv/bin/python -m unittest discover -s tests  # Ran 107 tests in 8.152s OK
sqlite3 :memory: < schemas/sqlite/001_initial.sql
```
