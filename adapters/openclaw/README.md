# @altm/openclaw

OpenClaw lifecycle adapter for ALTM.

It calls `memory_prepare_turn` from `before_prompt_build`, injects the returned
context through `prependContext`, then calls `memory_commit_turn` from
`agent_end`. Incognito sessions are skipped.

```bash
openclaw plugins install @altm/openclaw
openclaw gateway restart
```

Configure `plugins.entries.altm-memory.config` with the ALTM Streamable HTTP
MCP URL, plaintext runtime API key, and stable tenant/workspace/user IDs.
ALTM stores only the configured SHA-256 key hash on the server.
