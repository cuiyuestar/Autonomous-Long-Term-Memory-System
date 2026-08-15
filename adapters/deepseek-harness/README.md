# ALTM for DeepSeek Harness

`@altm/deepseek-harness` is an out-of-tree Cordis bundle. It adds ALTM recall and commit behavior to a DeepSeek Harness profile without changing the Harness agent loop or ALTM domain code.

## Capability Roles

The package exposes explicit Host roles plus one browser Client:

| Entry | Role |
|---|---|
| `@altm/deepseek-harness` | Top-level no-op Host marker whose `dsh.client` declaration loads the browser UI. |
| `@altm/deepseek-harness/memory` | Service Definition for `ctx.longTermMemory`. |
| `@altm/deepseek-harness/provider` | ALTM MCP Service Provider. |
| `@altm/deepseek-harness/consumer` | Harness lifecycle Consumer. |
| `@altm/deepseek-harness/ui-host` | Same-origin graph, L1-L4, and write-only embedding-configuration bridge. |
| `@altm/deepseek-harness/client` | Global Memory panel, spherical graph, layer browser, embedding setup, and bilingual controls. |

The shipped bundle keeps the top-level Client marker installed and places the Provider, Consumer, and UI Host inside one `cordis:group` with id `altm-memory`. Disabling that group hot-unloads all memory behavior. The browser Client polls the UI Host capability and removes or restores its slots without restarting Web. A different Provider can implement the Service Definition while reusing the same Harness Consumer.

ALTM does not replace the Harness SessionEvent log, persistence, replay, or compaction. It provides replaceable cross-turn and cross-session long-term memory. Recalled model-visible text remains a durable Harness `user/message`, so the SessionEvent log still reconstructs each model request.

## Browser UI

The installed Web profile gains a global `Memory` action at the bottom of the sidebar. It is available before any Session is opened and launches a full panel with three peer views:

- a Three.js spherical local view of the scoped heterogeneous graph;
- an L1-L4 abstraction ladder that renders one level at a time;
- an OpenAI-compatible embedding provider form with Base URL, model, and write-only API key fields.

Graph and layer reads use the current Session, then the most recent Session when none is selected. They show a no-Session state when neither exists; embedding setup remains available. The graph loads one 120-node neighborhood, prefetches up to four frontier neighborhoods while idle, and keeps at most 24 neighborhoods in its Client cache. Selecting a node recenters the sphere and promotes its cached neighborhood. The desktop canvas excludes the detail inspector; narrow layouts move that inspector below the graph. The layer browser initially renders 20 of the newest 80 memories per level and reveals more on demand. L0 remains available only through evidence references.

On the first unconfigured browser use, the Client opens a concise embedding setup dialog. Its action opens the embedding view directly. A browser-local acknowledgement prevents repeated first-use dialogs; the permanent Memory action remains available for later changes.

The browser never receives the MCP credential, SQLite path, or a stored embedding API key. It calls the same-origin `ui-host`, which derives tenant, user, Agent, and workspace scope from Host configuration plus the addressed live Harness Session before forwarding memory reads to the authenticated runtime MCP. Embedding saves travel write-only through the same route. ALTM validates the provider with a real `/embeddings` request before atomically writing `<database>.embedding.json` with mode `0600`; MCP and Worker operations read the managed configuration on every operation, so a successful save applies without a restart. Complete managed settings take precedence over `ALTM_EMBEDDING_*` environment variables.

Each recalled `user/message` carries structured activity metadata in its durable source. Harness renders it as one default-collapsed recall row with included-memory, L1-L4, graph-match, and token figures; no new unknown SessionEvent type is introduced.

## Lifecycle Mapping

The plugin wraps the first accepted `agent/pre-step` of each Harness turn. It sends the direct user message to `memory_prepare_turn`, then appends the returned ALTM context as a source-attributed `user/message` in the same entered batch. The Harness session log therefore reconstructs the exact memory text sent to the model.

The Consumer waits for the durable final `turn/end` event before calling `memory_commit_turn`. This avoids committing an intermediate answer when a tool call or another plugin continues the same turn. Only `completed` and `max-tokens` turns with assistant text are committed. Citations are inferred only from `memory://...` markers in the final assistant text and remain restricted to memories prepared for that turn.

Failed Host turns, missing final Assistant text, Consumer disposal, and commit failure settle a prepared cycle through `memory_abort_turn`. Abort is terminal and idempotent. It retains the captured user L0 and asynchronous folding work but does not create an Assistant L0.

Prepare failures are fail-open: the original Harness turn continues without recalled context. Commit failures are logged and do not alter the completed Harness turn. Each Provider operation uses a fresh authenticated MCP connection, retries idempotently within one configured deadline, and resolves the credential again so key rotation applies to the next operation. Consumer disposal stops event admission, drains active operations, and aborts every remaining prepared turn.

## Install

Build ALTM's TypeScript SDK and this adapter:

```bash
cd adapters/typescript
npm ci
npm run build

cd ../deepseek-harness
npm install
npm run build
```

Start the authenticated ALTM runtime server:

```bash
export ALTM_MCP_API_KEY="replace-with-a-random-secret"
export ALTM_MCP_API_KEY_SHA256="$(
  printf '%s' "$ALTM_MCP_API_KEY" | shasum -a 256 | awk '{print $1}'
)"

../../.venv/bin/altm mcp-server \
  --db ../../data/altm.sqlite3 \
  --transport streamable-http \
  --profile runtime \
  --host 127.0.0.1 \
  --port 8000
```

Install the bundle into a Harness profile from the ALTM checkout:

```bash
cd /path/to/deepseek-harness
pnpm dsh plugin --profile web add /path/to/ALTM/adapters/deepseek-harness
pnpm dsh --profile web --dump-config
pnpm dsh --profile web
```

For the verified sibling-repository checkout, use the management script:

```bash
./scripts/altm-harness-stack.sh install
./scripts/altm-harness-stack.sh disable
./scripts/altm-harness-stack.sh enable
./scripts/altm-harness-stack.sh uninstall
```

`disable` and `enable` update the watched profile patch and do not restart Harness. `uninstall` disables the live group before removing both the dependency and bundle layer. An explicit uninstall is remembered, so a later `start` refuses to reinstall the package implicitly.

The bundle reads the API key by reference. With the standard Harness credential provider, store `ALTM_MCP_API_KEY` in `$DSH_HOME/.credentials.yaml`; without that provider, export it in the process environment. Never put the secret itself in `cordis.patch.yml`.

## Configuration

| Field | Role | Default | Behavior |
|---|---|---|---|
| `endpoint` | Provider | `http://127.0.0.1:8000/mcp` | ALTM Streamable HTTP MCP endpoint. |
| `apiKeyEnv` | Provider | `ALTM_MCP_API_KEY` | Harness credential reference or environment variable name. |
| `timeoutMs` | Provider | `15000` | Total deadline for credential resolution, connection, and one MCP operation. |
| `requestAttempts` | Provider | `2` | Maximum idempotent MCP attempts inside the deadline. |
| `tenantId` | Consumer | `local` | Top-level long-term-memory tenant scope. |
| `workspaceId` | Consumer | session `cwd`, then `default` | Stable workspace scope. Set it explicitly when paths are not stable. |
| `userId` | Consumer | `local`; bundle uses `ALTM_USER_ID`, `$USER`, then `local` | Stable user scope and L4 sharing identity. |
| `agentId` | Consumer | `deepseek-harness` | Stable Agent scope across Harness sessions. Do not use the Harness session id here. |
| `tokenBudget` | Consumer | `1200` | Maximum recalled context tokens. |
| `recallLimit` | Consumer | `10` | Maximum direct recall candidates. |
| `activeWindowMode` | Consumer | omitted | Optional `off`, `limited`, or `full` active-window policy. |
| `activeLimit` | Consumer | `5` | Maximum active-window memories. |
| `strictSession` | Consumer | `false` | Restrict recall to the current Harness session. |

The shipped bundle also accepts `ALTM_MCP_ENDPOINT`, `ALTM_MCP_API_KEY_ENV`, `ALTM_TENANT_ID`, `ALTM_WORKSPACE_ID`, `ALTM_USER_ID`, and `ALTM_AGENT_ID`. A profile-level patch can replace the row's complete `config`.

## Scope Semantics

Harness session ids map to ALTM `session_id`; Harness turn numbers map to ALTM `turn_id`. `agentId` is deliberately configuration-owned because a Harness `Agent.id` is its session id and would prevent cross-session long-term recall.

Only direct user-source messages start an ALTM cycle. Tool-only continuation steps, injected plugin context, and turns rejected before entry are not captured as new user turns. Later user steering inside an already-open Harness turn remains part of Harness history but does not rewrite the ALTM prepare input.

## Verification

Run package lifecycle tests and the reusable cross-repository test:

```bash
npm test
npm run test:e2e
```

The package tests prove commit, failed-turn abort, unload abort, and durable recall activity metadata against a replaceable in-process Provider. The cross-repository test builds and packs every Host and Client entry, installs them into an isolated Harness profile, starts the real authenticated ALTM MCP server, boots real Loader compositions for multiple Harness sessions, and checks recall injection, citation feedback, scope isolation, fail-open credential handling, durable logs, and ALTM SQLite state. It removes all temporary profiles, databases, logs, packages, and server output when finished. Set `DSH_REPO` or `ALTM_ROOT` when the repositories are not sibling directories.

## Limitations

- User steering that arrives after the first step is not folded back into the already prepared ALTM cycle.
- The adapter proves transport and lifecycle integration; L1-L4 semantic quality still depends on the configured ALTM chat and embedding providers and the continuously running ALTM worker.
