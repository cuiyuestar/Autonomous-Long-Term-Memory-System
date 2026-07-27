# ADR 0001: 初始工程架构与确认门

状态：已确认，Phase 1 执行中。

日期：2026-06-29

## 背景

目标系统需要吸收四类设计：

1. TencentDB-Agent-Memory 的分层记忆、L0 保真、SQLite/FTS/向量后端和 Agent adapter 经验。
2. CogniFold 的 typed graph、UpdatePlan、PPR、BM25/vector/RRF 和上下文评分。
3. Headroom 的 ContextRouter、CCR 和 retrieval marker。
4. JDK GC 的分代治理、age table、动态晋升和 promotion failure。

但当前阶段不实现完整功能，只建立能长期演进的工程边界。

## 已确认决策

| 决策点 | 选择 | 原因 |
| --- | --- | --- |
| 工程形态 | ALTM 独立新工程 | 避免继承参考项目历史约束，便于重新定义 L0-L4 和生命周期边界 |
| 运行时 | 双运行时 | Python 承接图/检索/压缩算法生态，TypeScript 承接未来 Agent adapter |
| 控制面 | Python 控制面 | 更贴近 CogniFold/Headroom 的算法和服务形态，TS 暂作为 adapter 层 |
| TS 工具链 | 声明 Node 22 | 当前环境无 Node/npm/pnpm，不在本轮安装工具链 |
| Python 版本 | 目标 Python >= 3.11 | 贴合 CogniFold/Headroom 的现代依赖和类型体系 |
| 存储 | SQLite + FTS5 | Phase 1 先完成元数据、证据链和全文检索，向量能力保留接口 |
| 确认门 | 平衡确认 | 架构、依赖、数据模型、公共 API 等重大决策确认；小范围骨架实现由 Agent 推进 |
| 首期范围 | 契约 + 骨架 | 不抢跑完整业务能力，先稳定接口和目录边界 |

## 初始模块边界

```text
capture       -> L0 append-only 录制入口
folding       -> L0 -> L1 -> L2 -> L3 -> L4 折叠管线
storage       -> SQLite/FTS/向量/图存储适配
retrieval     -> BM25/vector/graph/PPR/RRF/rerank
lifecycle     -> residentScore、age table、晋升、降级、压缩、观察期
context       -> Headroom gateway、context bands、CCR marker
adapters      -> CLI、HTTP、MCP、OpenClaw、Hermes 等宿主接入
```

## 设计边界

1. L0 原文永久保留；生命周期只影响热度、索引和上下文呈现，不触发 L0 物理删除。
2. L1/L2/L3/L4 可以 tombstone 或删除，但证据链必须能 fallback 到 L0。
3. residentScore 和 retrievalScore 必须分离。
4. 召回排序和存储层级解耦；当前任务相关性决定进入上下文的内容。
5. Headroom 只改变注入形态，不改变底层记忆事实。

## 后续需要确认的重大事项

1. L0 capture 首个宿主入口：CLI、HTTP、MCP、OpenClaw 或 Hermes。
2. L1/L2 是否第一阶段接真实 LLM，还是先使用 deterministic mock。
3. Python 数据模型是否引入 Pydantic 作为运行时 schema。
4. TypeScript adapter 优先级：OpenClaw、Hermes、通用 MCP 或 SDK。
5. 是否启用 sqlite-vec 作为 Phase 1.5，而不是等到 Phase 3。
