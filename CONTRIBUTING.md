# Contributing

## Development Setup

```bash
python3.11 -m venv .venv
.venv/bin/python -m pip install -e ".[dev,mcp]"
```

## Required Checks

```bash
.venv/bin/python -m compileall -q src tests
.venv/bin/python -m unittest discover -s tests
.venv/bin/ruff check src tests
.venv/bin/python -m build
```

Strict Pyright is intentionally not listed as a passing gate yet. The repository
contains legacy dynamic JSON/MCP typing debt; do not weaken `typeCheckingMode`,
and do not claim Pyright passes until that debt is removed.

## Engineering Rules

1. Production paths must not return synthetic model output.
2. L0 is append-only. Idempotent retries may return the existing value but
   must not rewrite source content.
3. Semantic governance is fail-closed. Missing or low-confidence evaluators
   defer destructive actions.
4. New memory reads must preserve tenant/workspace/user/agent isolation.
5. Shared L4 memories are limited to the same tenant/workspace/user.
6. Every L1-L4 object must retain evidence back to lower layers.
7. Public MCP tools belong in the `runtime` profile only when they are safe for
   an ordinary Host Agent.
8. Tests may use local fake HTTP servers, but production modules must call real
   configured providers.

## Pull Requests

- Keep changes scoped to one architectural concern.
- Include failure-path and idempotency tests.
- Update public contracts and documentation together.
- Document new environment variables in `.env.example`.
- Do not commit credentials, local databases, model responses, or user memory.
