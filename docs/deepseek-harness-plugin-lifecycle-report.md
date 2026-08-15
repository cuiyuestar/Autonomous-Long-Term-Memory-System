# DeepSeek Harness 可插拔长期记忆改造报告

验证日期：2026-08-15

## 结论

`@altm/deepseek-harness` 已从单体 adapter 拆分为完整的长期记忆能力角色：

```text
LongTermMemory Service Definition
             |
ALTM MCP Provider
             |
DeepSeek Harness Consumer
             |
UI Host + Web Client
```

默认 bundle 使用 `cordis:group` 组合 Provider、Consumer 与 UI Host。顶层包根只负责让 Harness 扫描 `dsh.client`；Client 根据 UI Host 健康状态动态注册或注销 Memory slots。用户可以整体安装、热启用、热停用和卸载，也可以保留 Consumer 并替换其他长期记忆 Provider。

该改造没有修改 DeepSeek Harness 源码，没有替换 Harness SessionEvent 日志、持久化或 compaction，没有修改 ALTM 的 L0-L4、Graph、检索、涌现和治理算法，也没有要求 OpenClaw、Hermes 等其他适配器迁移。

## 包入口

| 入口 | 角色 |
|---|---|
| `@altm/deepseek-harness` | 顶层 Client loader 标记 |
| `@altm/deepseek-harness/memory` | `ctx.longTermMemory` Service Definition |
| `@altm/deepseek-harness/provider` | ALTM Streamable HTTP MCP Provider |
| `@altm/deepseek-harness/consumer` | Harness `agent/pre-step` / `turn/end` Consumer |
| `@altm/deepseek-harness/ui-host` | Graph、L1-L4 与 write-only Embedding 配置桥接 |
| `@altm/deepseek-harness/client` | 全局 Memory 面板、球状异构图、分层记忆、Embedding 配置和双语 UI |

默认 bundle：

```yaml
- id: altm-memory-client
  name: '@altm/deepseek-harness'
- id: altm-memory
  name: cordis:group
  group: true
  config:
    - id: provider
      name: '@altm/deepseek-harness/provider'
    - id: consumer
      name: '@altm/deepseek-harness/consumer'
    - id: ui-host
      name: '@altm/deepseek-harness/ui-host'
```

## Web Client

Memory 不再注册到 Chat/Trajectory 同级的会话标签。插件改为向 `sidebar.footer.action` 注册全局入口，无需先进入或触发 Session 即可打开。面板包含三个同级模式：

- `Graph`：Three.js 球面局部图，每次最多加载 120 个节点；空闲时预取边界邻域，Client LRU 最多保存 24 个邻域。
- `Layers`：L4 Persona、L3 Scene、L2 Atom、L1 Session 单层切换；每层先显示 20 条，按需继续加载，L0 仅在证据引用中出现。
- `Embedding 配置`：填写 OpenAI-compatible Base URL、模型名和 write-only API Key；保存前执行真实向量请求验证，成功后无需重启即可供 MCP 与 Worker 使用。

Graph 与 Layers 优先读取当前 Session scope，没有当前 Session 时使用最近 Session；没有任何 Session 时显示空态。Embedding 配置始终可用。首次未配置的浏览器会显示简短引导，`点击配置` 直接打开 Embedding 子页。

每轮召回的持久 `user/message.source` 记录 included count、L1-L4 分布、Graph 命中、token 估算和 memory ids。Harness 原生 Context disclosure 将它呈现为默认折叠的一行，不引入外部未知 SessionEvent 类型。

UI 使用 Harness 主题 token 和图标，随全局 Locale 在中文与英文之间切换。浏览器只调用同源 UI Host，不获得 MCP Key、SQLite 路径或已保存的 Embedding API Key。托管配置原子写入 `<database>.embedding.json` 并保持 `0600` 权限。

## 浏览器验收

真实 Harness 会话完成一轮 DeepSeek 回复后，Chat 中出现默认折叠的 `跨会话召回 ALTM 1 · ~11 tok` 活动行。Memory 页读取同一 live Session 的固定 scope，Graph 与 Layers 均返回真实 SQLite/MCP 数据。

| 视口与场景 | 结果 |
|---|---|
| Desktop 1280×800 Graph | Canvas 832×659、缓冲区同尺寸、导出数据非空；6 个节点、7 条关系 |
| Desktop Embedding | 对话框 1122×754；保存按钮 132×40 完整可见；无横向溢出 |
| 首次引导 | 清空站点存储后自动弹出；`点击配置` 直接选中 Embedding 子页 |
| Desktop Layers | 150px 层级栏、480px 列表、586px 详情；首次 20 条并显示加载更多 |
| Mobile 390×844 Embedding | 面板 390×844 全屏；保存按钮 358×40 完整可见；页面和面板均无横向溢出 |
| Mobile 390×844 Graph | Canvas 与缓冲区均为 390×755，导出数据非空；详情位于图谱下方 |
| Mobile 390×844 Layers | 面板 390×844，层级栏 390×48；列表和详情纵向排列且无横向溢出 |
| 数据密度 | L2 首次 20 条，加载更多后 40 条；页面无横向溢出 |
| Locale | `中 | EN` 同步切换 Harness 与 Memory，刷新后保持选择 |
| 配置状态 API | HTTP 200；仅返回 `base_url/configured/model/source`，不含 API Key |

## Runtime Cycle

ALTM 新增终态 `aborted`：

```text
prepared -> committed
prepared -> aborted
```

`memory_abort_turn` 已贯通：

```text
SQLite store
-> AltmApplication
-> runtime MCP profile
-> TypeScript SDK
-> ALTM Provider
-> Harness Consumer
```

相同 reason 的 abort 幂等；不同 reason 冲突；committed 与 aborted 不能互相转换。abort 保留用户 L0、injected 信号和异步折叠任务，不产生 Assistant L0。

Consumer 在以下情况 abort：

- Host turn 非 completed/max-tokens；
- 最终没有 Assistant 文本；
- commit 最终失败；
- prepared turn 被替换；
- Consumer 热卸载。

## 管理命令

```bash
./scripts/altm-harness-stack.sh install
./scripts/altm-harness-stack.sh enable
./scripts/altm-harness-stack.sh disable
./scripts/altm-harness-stack.sh uninstall
./scripts/altm-harness-stack.sh status
```

`enable/disable` 修改 Harness 正在 watch 的 profile patch。`uninstall` 先禁用 live group，再删除 dependency 与 bundle layer。显式卸载状态会阻止后续 `start` 自动重装。

## 验证结果

| 验证 | 结果 |
|---|---|
| ALTM 全量测试 | 165 项通过 |
| Python prepare/commit/abort 状态机 | 通过 |
| 旧 SQLite runtime cycle CHECK 迁移 | 通过 |
| runtime MCP 暴露 `memory_abort_turn` | 通过 |
| TypeScript SDK typecheck/build | 通过 |
| Consumer 完成 turn commit | 通过 |
| Consumer 失败 turn abort | 通过 |
| Consumer 卸载 abort pending turn | 通过 |
| 发布 tarball 六个 Host/Client 入口 | 通过 |
| 隔离 Harness Loader + MCP E2E | 通过 |
| recall、citation、scope、缺凭证降级 | 通过 |
| Client boot manifest 与 UI Host | `client.js` 已加载，health HTTP 200 |
| Graph/Layers/Embedding 桌面与移动布局 | 通过 |
| 首次引导与直接配置跳转 | 通过 |
| 托管配置真实验证、`0600`、动态生效与密钥不回传 | 通过 |
| L2 首次 20 条与加载更多 | 通过，20 -> 40 |
| 中英文切换与刷新保持 | 通过 |
| 真实 profile `disable -> enable` | Memory 自动消失/恢复，Web PID 不变 |
| `uninstall` 删除 dependency、bundle 与 override | 通过，profile patch 为 `[]` |
| 卸载后 `start` 不自动恢复 | 通过 |
| `install` 恢复 group 并启动 Web | 通过 |
| Web 健康检查 | HTTP 200 |
| MCP 无效 Bearer | HTTP 401 |

真实运行状态：

```text
plugin=enabled
mcp=running
worker=running
web=running
web_url=http://127.0.0.1:3000
```

最终热开关复核中，Web PID 在 `disable -> enable` 前后保持不变，浏览器无需刷新即可注销和恢复全局 Memory 入口。实际 Loader 展开的配置包含 `altm-memory-client`、`altm-memory` group、Provider、`./consumer` 与 `./ui-host`。

正式 SQLite 已迁移为允许 `prepared|committed|aborted|failed`，已有 committed 数据保持不变。最终状态为 `committed=18`、`aborted=3`、`prepared=0`，并记录 `injected=97`、`cited_by_agent=2`；3 个旧 adapter 遗留 cycle 已以 `legacy-unsettled-cycle` 终止并保留用户 L0。

## 替换 Provider

其他 Provider 只需继承 `LongTermMemory` 并实现：

```ts
prepare(input, signal)
commit(input)
abort(input)
```

然后在 profile 中保留 Harness Consumer，替换 group 内的 provider row。Consumer 不依赖 ALTM MCP、L0-L4 或 Graph 类型。
