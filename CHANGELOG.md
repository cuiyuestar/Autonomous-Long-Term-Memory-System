# 更新日志

本文件记录项目的重要变更。

格式遵循 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)，版本号遵循 [Semantic Versioning](https://semver.org/lang/zh-CN/spec/v2.0.0.html)。

## [未发布]

## [1.1.0] - 2026-08-17

### 新增

- 分层跨会话 Query Recall：L0/L1 保持当前 session 内检索，L2-L4 可在既有 MemoryScope 内跨 session 召回。
- Windows CI 覆盖 SQLite 临时数据库清理和 DeepSeek Harness adapter 构建。

### 变更

- Python 包、TypeScript SDK、DeepSeek Harness adapter、OpenClaw adapter 和 Hermes plugin 统一升级到 1.1.0。
- 本地 Harness stack 改为发现构建生成的 adapter 包，不再硬编码带版本号的 tarball 文件名。

### 修复

- `strict_session` 现在一致作用于 direct FTS、local vector、remote vector、graph 和 fallback recall，包括关闭 Active Window 时构建的上下文。
- adapter 构建脚本改用 `fileURLToPath()` 转换文件 URL，正确处理 Windows 盘符路径和已编码的文件系统字符。
- SQLite transaction context 退出时确定性关闭连接，避免 Windows 临时目录清理时触发 `WinError 32`。

## [1.0.0] - 2026-08-09

### 新增

- ALTM 初始版本，包含 L0-L4 记忆演化、混合召回、异构图经验涌现、生命周期治理、CCR 上下文管理、MCP、TypeScript SDK 和 Agent adapters。

[未发布]: https://github.com/cuiyuestar/Autonomous-Long-Term-Memory-System/compare/v1.1.0...HEAD
[1.1.0]: https://github.com/cuiyuestar/Autonomous-Long-Term-Memory-System/compare/v1.0.0...v1.1.0
[1.0.0]: https://github.com/cuiyuestar/Autonomous-Long-Term-Memory-System/releases/tag/v1.0.0
