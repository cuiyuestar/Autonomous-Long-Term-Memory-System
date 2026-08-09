# @altm/sdk

Typed TypeScript client for the ALTM runtime MCP profile.

```ts
import {
  AltmRuntimeClient,
  AltmTurnCoordinator,
  StreamableHttpToolCaller
} from "@altm/sdk";

const caller = await StreamableHttpToolCaller.connect({
  url: "http://127.0.0.1:8000/mcp",
  apiKey: process.env.ALTM_API_KEY
});
const coordinator = new AltmTurnCoordinator(
  new AltmRuntimeClient(caller)
);

const turn = await coordinator.prepare({
  scope: {
    tenantId: "local",
    workspaceId: "default",
    userId: "user-1",
    agentId: "agent-1"
  },
  sessionId: "session-1",
  turnId: crypto.randomUUID(),
  content: "What did we decide about the release date?"
});

// Inject turn.injectedContext into the Host Agent prompt.
const assistantContent = await hostAgent.generate(turn.injectedContext);

await coordinator.commit({
  prepared: turn.prepared,
  assistantContent
});
```

`commit()` only infers citations from `memory://...` markers present in the
assistant output. Hosts with structured citation data should pass explicit
`citedMemoryIds`.
