# ALTM 自主记忆涌现真实验证报告

验证日期：2026-08-14

## 结论

ALTM 的记忆写入、召回、引用反馈、L0-L2 折叠、Graph 抽取和查询诱导的 Graph 涌现均已通过真实验证。

自主涌现的核心证据成立：查询只包含 `ORIONANCHOR9F3`，关联记忆正文完全不含该字符串；`max_hops=0` 时关联记忆不存在，`max_hops=2` 时它以 `matched_by=["graph_ppr"]` 出现。拒绝全部关系路径、切换 Agent scope 或 tombstone 关联记忆后，该结果均消失。

DeepSeek Harness 的真实模型链路也已通过：真实 `deepseek-v4-flash` 两轮对话中，第二轮从 ALTM 注入的持久上下文回答 `cobalt` 和 `2026-09-01`，并引用第一轮的原始 `memory://` marker。两轮 ALTM cycle 均为 committed。

## 当前运行环境

| 项目 | 状态 |
|---|---|
| ALTM MCP | `127.0.0.1:8000`，运行中 |
| ALTM Worker | 运行中 |
| DeepSeek Harness Web | `http://127.0.0.1:3000`，HTTP 200 |
| Harness 模型 | `deepseek-v4-flash` |
| ALTM Graph 模型 | `deepseek-v4-flash` |
| 正式数据库 | `data/deepseek-harness.sqlite3` |

API Key 只存在于 Git 忽略且权限为 `0600` 的本机环境文件中，没有写入本报告或仓库。

## 证据一：真实 Harness 记忆回合

真实 Harness workspace session：

```text
altm-live-1ae3c652-6a10-4ea4-a384-e0639132a8b5
```

第一轮向模型声明：

```text
ALTM-LIVE-20260814 的发布代号是 cobalt，发布日期是 2026-09-01。
```

第二轮要求不读取文件，仅根据记忆回答。模型返回：

```text
发布代号: cobalt
发布日期: 2026-09-01
memory://l0_8841ea17191df81be1b44fbf#4367e55949d0f3d4
```

SQLite 证据：

| 指标 | 结果 |
|---|---:|
| committed runtime cycle | 2 |
| cited_by_agent | 2 |
| injected | 4 |
| L0 | 4 |
| L1 | 2 |
| L2 | 5 |

Harness 持久 session log 中存在 `source.plugin = "altm-memory"` 的 `user/message`，其 `<altm_memory_context>` 包含 `cobalt`、日期和 marker。第二轮 AssistantMessage 同样包含该事实与 marker，因此“模型可见即持久”和引用反馈均有外部记录。

## 证据二：真实 DeepSeek Graph 抽取

受控数据由两条字面隔离的 L2 记忆组成：

```text
Seed:
ORIONANCHOR9F3 release is delayed because the packaging workflow is blocked.

Neighbor:
The supplier signing certificate is unavailable and directly causes the
packaging workflow block.
```

Neighbor 不包含查询锚点 `ORIONANCHOR9F3`。真实 DeepSeek Graph 抽取返回 `status=complete`，并在两条记忆之间形成 `causes` 关系。

该测试没有手写 Graph 关系，也没有 Mock Graph 模型。

## 证据三：ContextBundle Graph-only 增量

测试显式设置 `active_window_mode=off`，排除全局 active window 干扰。

Graph 抽取前：

```json
["seed-orion"]
```

Graph 抽取后：

```json
[
  {
    "id": "seed-orion",
    "matched_by": [
      "local_vector",
      "fts_trigram",
      "fts_unicode",
      "graph_ppr",
      "graph_subgraph"
    ]
  },
  {
    "id": "neighbor-certificate",
    "matched_by": [
      "graph_ppr",
      "graph_subgraph"
    ]
  }
]
```

Neighbor 只在 Graph 形成后进入生产 `build_context()` 返回的 ContextBundle。DeepSeek Harness adapter 使用的正是该 prepare/build-context 路径，因此 Graph-only 候选可以进入模型请求。

## 证据四：QueryEmergence A/B

固定 `seed_limit=1`，确保只有包含查询锚点的 seed 能成为入口。

`max_hops=0`：

```json
["seed-orion"]
```

`max_hops=2`：

```json
[
  {
    "id": "seed-orion",
    "contains_query": true,
    "matched_by": ["query_emergence_seed"]
  },
  {
    "id": "neighbor-certificate",
    "contains_query": false,
    "matched_by": ["graph_ppr"]
  }
]
```

这证明 Neighbor 不是关键词、trigram 或 local vector 的直接命中，而是沿 Graph 关系传播得到的关联经验。

## 负向对照

| 对照 | 预期 | 结果 |
|---|---|---|
| `max_hops=0` | Neighbor 消失 | 通过 |
| 将 Seed/Neighbor 间全部路径标为 rejected | Neighbor 消失 | 通过 |
| 使用不同 `agent_id` scope | 无结果 | 通过 |
| tombstone Neighbor | Neighbor 消失 | 通过 |
| 重复相同查询 | 顺序和结果稳定 | 通过 |
| 无关记忆 | 不进入涌现结果 | 通过 |

所有负向对照都通过，排除了无条件返回、跨 scope 泄漏、终态记忆继续传播和不稳定排序。

## 可重复命令

```bash
cd /path/to/Autonomous-Long-Term-Memory-System
set -a
source data/altm-harness.env
set +a

.venv/bin/python scripts/verify-altm-emergence.py \
  --output data/altm-emergence-verification.json
```

脚本使用 `TemporaryDirectory` 创建隔离数据库，退出后自动删除测试记忆、Graph 和 SQLite 文件，不污染正式长期记忆。

通过时输出：

```json
{
  "graph_extraction_status": "complete",
  "context_graph_only_neighbor": true,
  "positive_query_emergence": true,
  "neighbor_contains_query": false,
  "repeat_stable": true,
  "rejected_all_paths_excludes_neighbor": true,
  "cross_scope_empty": true,
  "tombstone_excludes_neighbor": true,
  "passed": true
}
```

## 当前边界

DeepSeek 当前 Key 没有提供 Embedding API，因此 remote embedding indexing 已按重试策略失败。Graph 抽取和 Graph-based query emergence 不依赖该 remote embedding，已经真实通过。

L3/L4 semantic scene/persona 形成依赖 embedding index，因此本报告不宣称 L3/L4 已通过。要验证完整的 L3/L4 长期晋升链，还需配置一个 OpenAI-compatible Embedding Provider，再执行跨 session 重复证据、观察周期、激活和 supersede 测试。
