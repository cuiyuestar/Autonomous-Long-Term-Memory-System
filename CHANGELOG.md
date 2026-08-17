# Changelog

All notable changes to this project are documented in this file.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project follows [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [1.1.0] - 2026-08-17

### Added

- Layer-aware cross-session Query Recall: L0/L1 remain session-local while L2-L4 can be retrieved across sessions inside their existing MemoryScope.
- Windows CI coverage for SQLite temporary-database cleanup and the DeepSeek Harness adapter build.

### Changed

- Updated the Python package, TypeScript SDK, DeepSeek Harness adapter, OpenClaw adapter, and Hermes plugin to version 1.1.0.
- Made the local Harness stack discover the generated adapter package instead of embedding a versioned tarball filename.

### Fixed

- Applied `strict_session` consistently to direct FTS, local vector, remote vector, graph, and fallback recall, including contexts built with the Active Window disabled.
- Converted adapter build URLs with `fileURLToPath()` so Windows drive-letter paths and encoded filesystem characters are handled correctly.
- Closed SQLite connections deterministically when transaction contexts exit, preventing Windows `WinError 32` failures during temporary-directory cleanup.

## [1.0.0] - 2026-08-09

### Added

- Initial ALTM release with L0-L4 memory evolution, hybrid retrieval, heterogeneous graph emergence, lifecycle governance, CCR context management, MCP, TypeScript, and Agent adapters.

[Unreleased]: https://github.com/cuiyuestar/Autonomous-Long-Term-Memory-System/compare/v1.1.0...HEAD
[1.1.0]: https://github.com/cuiyuestar/Autonomous-Long-Term-Memory-System/compare/v1.0.0...v1.1.0
[1.0.0]: https://github.com/cuiyuestar/Autonomous-Long-Term-Memory-System/releases/tag/v1.0.0
