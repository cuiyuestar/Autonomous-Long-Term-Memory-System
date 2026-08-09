# ALTM Hermes Adapter

This Hermes plugin binds `pre_llm_call` and `post_llm_call` to the ALTM
runtime MCP tools already registered by Hermes.

Configure ALTM as a Hermes MCP server:

```yaml
mcp_servers:
  altm:
    url: "http://127.0.0.1:8000/mcp"
    headers:
      Authorization: "Bearer ${ALTM_API_KEY}"
    tools:
      include:
        - memory_prepare_turn
        - memory_commit_turn
```

Install this directory as `~/.hermes/plugins/altm-memory`, then enable
`altm-memory` in `plugins.enabled`.

The adapter derives the registered tool names from
`ALTM_HERMES_MCP_SERVER_NAME` (default `altm`). Configure stable scope IDs with
`ALTM_HERMES_TENANT_ID`, `ALTM_HERMES_WORKSPACE_ID`,
`ALTM_HERMES_USER_ID`, and `ALTM_HERMES_AGENT_ID`.
