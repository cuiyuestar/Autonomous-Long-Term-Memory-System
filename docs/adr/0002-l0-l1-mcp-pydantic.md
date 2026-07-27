# ADR 0002: L0/L1 初始闭环、MCP 优先与 Pydantic 模型

状态：已确认，Phase 2 执行中。

日期：2026-06-29

## 背景

Phase 1 已建立工程骨架、SQLite + FTS5 schema、Python 控制面和 TypeScript adapter 声明层。下一阶段需要从“契约可表达”进入“最小闭环可运行”。

## 已确认决策

| 决策点 | 选择 | 影响 |
| --- | --- | --- |
| 阶段范围 | L0 + L1 mock | 先实现原文写入、FTS 召回和确定性 L1 胶囊，不接真实 LLM |
| 首个入口 | MCP 优先 | 以 MCP 工具作为主要 Agent 接入方向，同时保留 CLI 作为本地控制面 |
| MCP 依赖 | 立即安装 | 当前环境尝试安装失败，因为 MCP SDK 要求 Python >=3.10，本机只有 Python 3.9 |
| MCP 传输 | stdio + SSE 双模式 | 代码提供 `stdio` 与 `sse` transport 参数，真实运行待 Python 3.11 环境就绪 |
| 数据模型 | 立刻 Pydantic | 核心契约从 dataclass 切换为 Pydantic BaseModel |
| L1 mock 策略 | 规则抽取 | 用关键词规则生成 `decisions_mentioned`、`unresolved_questions` 和 `topic_tags` |

## 当前实现边界

1. `L0Recorder` 将单条消息写为 L0 `MemoryUnit`，生命周期为 `permanent`。
2. `SQLiteMemoryStore` 支持 schema 初始化、MemoryUnit upsert、FTS 索引、按 ID 读取、FTS 搜索、evidence refs 和 tombstone。
3. `RuleBasedL1Summarizer` 从同一 session 的 L0 生成一个 L1 `ContextCapsule`，并为每条 L0 写入 evidence ref 与 fallback locator。
4. `FTSRetrievalEngine` 提供最小召回候选，使用 SQLite FTS5 排名顺序映射为临时 `retrieval_score`。
5. `MCP adapter` 暴露 `memory_remember`、`memory_fold_l1`、`memory_recall`、`memory_drilldown`，但当前 Python 3.9 环境无法启动真实 MCP SDK。

## 风险与缓解

1. 规则 L1 可能把“需要确认”误判为未解决问题。
   - 缓解：L1 mock 的 `confidence` 固定为低置信度 0.45，且不生成 L2 事实。
2. FTS 目前只是关键词召回，不代表最终 retrieval fusion。
   - 缓解：保留 `RetrievalEngine` 端口，后续 BM25/vector/graph/RRF 都从这里扩展。
3. MCP 双模式扩大了 adapter 表面积。
   - 缓解：所有 MCP 工具只调用 Python 控制面，不复制业务逻辑。
4. 当前本机 Python 3.9 与项目目标 Python 3.11 不一致。
   - 缓解：核心测试用现有环境验证；MCP 真实启动作为 Python 3.11 环境就绪后的验证项。

## 下一确认门

1. 是否安装或配置 Python 3.11，解锁真实 MCP server 验证。
2. L1 mock 是否需要拆成独立 `ContextCapsule` 表，还是继续作为 L1 MemoryUnit content。
3. FTS 搜索是否需要按 layer、session、status 增加过滤参数。
4. 下一阶段优先 L2 mock/真实抽取，还是生命周期 access signal。
