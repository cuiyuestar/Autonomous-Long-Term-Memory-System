# MCP Runtime Verification

日期：2026-06-29

## 目标

配置项目隔离的 Python 3.11 环境，并验证 MCP server 的完整工具链：

```text
memory_remember
  -> L0 MemoryUnit
  -> memory_fold_l1
  -> L1 ContextCapsule + evidence refs
  -> memory_recall
  -> SQLite FTS result
  -> memory_drilldown
```

## 环境配置结果

当前机器没有系统级 `python3.11`、`uv`、`conda`、`pyenv`、`asdf` 或 `brew`。本次采用项目隔离方案：

1. 下载 uv 预编译二进制到 `.tools/uv/uv`。
2. 使用 uv 安装 CPython 3.11.15 到 `.tools/python`。
3. 使用该 Python 创建 `.venv`。
4. 在 `.venv` 中安装项目依赖与 MCP extra。

实际版本：

```text
Python 3.11.15
pydantic 2.13.4
mcp 1.28.1
uv 0.11.25
```

## 关键命令

```bash
mkdir -p .tools/downloads .tools/uv
curl -L --fail --retry 3 --connect-timeout 20 \
  https://github.com/astral-sh/uv/releases/download/0.11.25/uv-aarch64-apple-darwin.tar.gz \
  -o .tools/downloads/uv-aarch64-apple-darwin.tar.gz
tar -xzf .tools/downloads/uv-aarch64-apple-darwin.tar.gz -C .tools/uv --strip-components=1

UV_PYTHON_INSTALL_DIR=.tools/python .tools/uv/uv python install 3.11
UV_PYTHON_INSTALL_DIR=.tools/python .tools/uv/uv venv --python 3.11 .venv
.tools/uv/uv pip install --python .venv/bin/python -e ".[mcp]"
```

如果 `cryptography` 通过 uv 下载卡住，可直接下载 wheel 后本地安装：

```bash
mkdir -p .tools/downloads/wheels
curl -L --fail --retry 3 --connect-timeout 20 \
  https://files.pythonhosted.org/packages/9b/22/adf66990e63584a68dfb50c24f48a125c07b1699899381c8151e63ed458c/cryptography-49.0.0-cp311-abi3-macosx_11_0_arm64.whl \
  -o .tools/downloads/wheels/cryptography-49.0.0-cp311-abi3-macosx_11_0_arm64.whl
.tools/uv/uv pip install --python .venv/bin/python \
  .tools/downloads/wheels/cryptography-49.0.0-cp311-abi3-macosx_11_0_arm64.whl
.tools/uv/uv pip install --python .venv/bin/python -e ".[mcp]"
```

## 验证结果

基础验证：

```bash
.venv/bin/python -m compileall src tests
.venv/bin/python -m unittest discover -s tests
.venv/bin/python -m altm.cli --help
```

结果：

```text
4 tests OK
CLI exposes: init-db, capture, fold-l1, search, mcp-server
```

MCP stdio 验证结果：

```text
TOOLS ['memory_remember', 'memory_fold_l1', 'memory_recall', 'memory_drilldown']
REMEMBER_IDS l0_u1 l0_a1
FOLDED_L1 l1_f375986c4e49bfa5be89ef6b L1 2
RECALL_COUNT 2
RECALL_IDS ['l0_a1', 'l1_f375986c4e49bfa5be89ef6b']
DRILLDOWN l1_f375986c4e49bfa5be89ef6b L1 2
```

MCP SSE 验证结果：

```text
TOOLS ['memory_remember', 'memory_fold_l1', 'memory_recall', 'memory_drilldown']
REMEMBER_IDS l0_u1 l0_a1
FOLDED_L1 l1_bb948a5e3135a395df8ba44f L1 2
RECALL_COUNT 3
RECALL_IDS ['l1_bb948a5e3135a395df8ba44f', 'l0_u1', 'l0_a1']
DRILLDOWN l1_bb948a5e3135a395df8ba44f L1 2
```

## 注意事项

1. `Path(".venv/bin/python").resolve()` 会解析到底层解释器，绕过 venv 的 site-packages。MCP stdio 子进程必须使用 `.venv/bin/python` 原路径。
2. MCP SDK 对 `list[dict]` 返回值可能拆成多个 text content item；客户端验证脚本需要按 `result.content` 逐项读取。
3. SSE 当前使用 FastMCP 默认地址 `http://127.0.0.1:8000/sse`。
