import type {
  AbortedTurn,
  AbortTurnInput,
  CommitTurnInput,
  CommittedTurn,
  ContextBundle,
  ContextItem,
  MemoryScope,
  PrepareTurnInput,
  PreparedTurn
} from "./contracts.js";
import type { RuntimeToolCaller } from "./transport.js";

export class AltmRuntimeClient {
  constructor(private readonly caller: RuntimeToolCaller) {}

  async prepareTurn(input: PrepareTurnInput): Promise<PreparedTurn> {
    const payload = await this.caller.callTool("memory_prepare_turn", {
      ...scopeToWire(input.scope),
      session_id: input.sessionId,
      turn_id: input.turnId,
      content: input.content,
      ...(input.messageId ? { message_id: input.messageId } : {}),
      ...(input.query ? { query: input.query } : {}),
      token_budget: input.tokenBudget ?? 1200,
      recall_limit: input.recallLimit ?? 10,
      ...(input.activeWindowMode
        ? { active_window_mode: input.activeWindowMode }
        : {}),
      active_limit: input.activeLimit ?? 5,
      strict_session: input.strictSession ?? false
    });
    return preparedTurnFromWire(extractToolPayload(payload));
  }

  async commitTurn(input: CommitTurnInput): Promise<CommittedTurn> {
    const payload = await this.caller.callTool("memory_commit_turn", {
      ...scopeToWire(input.scope),
      cycle_id: input.cycleId,
      assistant_content: input.assistantContent,
      cited_memory_ids: input.citedMemoryIds ?? [],
      ...(input.assistantMessageId
        ? { assistant_message_id: input.assistantMessageId }
        : {})
    });
    return committedTurnFromWire(extractToolPayload(payload));
  }

  async abortTurn(input: AbortTurnInput): Promise<AbortedTurn> {
    const payload = await this.caller.callTool("memory_abort_turn", {
      ...scopeToWire(input.scope),
      cycle_id: input.cycleId,
      reason: input.reason
    });
    return abortedTurnFromWire(extractToolPayload(payload));
  }

  async close(): Promise<void> {
    await this.caller.close();
  }
}

function scopeToWire(scope: MemoryScope): Record<string, string> {
  return {
    tenant_id: scope.tenantId,
    workspace_id: scope.workspaceId,
    user_id: scope.userId,
    agent_id: scope.agentId
  };
}

function preparedTurnFromWire(value: unknown): PreparedTurn {
  const record = objectValue(value, "PreparedTurn");
  return {
    cycleId: stringValue(record.cycle_id, "cycle_id"),
    scope: scopeFromWire(record.scope),
    sessionId: stringValue(record.session_id, "session_id"),
    turnId: stringValue(record.turn_id, "turn_id"),
    userMemoryId: stringValue(record.user_memory_id, "user_memory_id"),
    query: stringValue(record.query, "query"),
    context: contextBundleFromWire(record.context),
    enqueuedJobIds: stringArray(record.enqueued_job_ids, "enqueued_job_ids"),
    status: stringValue(record.status, "status"),
    metadata: optionalRecord(record.metadata)
  };
}

function committedTurnFromWire(value: unknown): CommittedTurn {
  const record = objectValue(value, "CommittedTurn");
  return {
    cycleId: stringValue(record.cycle_id, "cycle_id"),
    scope: scopeFromWire(record.scope),
    sessionId: stringValue(record.session_id, "session_id"),
    turnId: stringValue(record.turn_id, "turn_id"),
    assistantMemoryId: stringValue(
      record.assistant_memory_id,
      "assistant_memory_id"
    ),
    citedMemoryIds: stringArray(record.cited_memory_ids, "cited_memory_ids"),
    enqueuedJobIds: stringArray(record.enqueued_job_ids, "enqueued_job_ids"),
    status: stringValue(record.status, "status"),
    metadata: optionalRecord(record.metadata)
  };
}

function abortedTurnFromWire(value: unknown): AbortedTurn {
  const record = objectValue(value, "AbortedTurn");
  return {
    cycleId: stringValue(record.cycle_id, "cycle_id"),
    scope: scopeFromWire(record.scope),
    sessionId: stringValue(record.session_id, "session_id"),
    turnId: stringValue(record.turn_id, "turn_id"),
    reason: stringValue(record.reason, "reason"),
    status: stringValue(record.status, "status"),
    metadata: optionalRecord(record.metadata)
  };
}

function scopeFromWire(value: unknown): MemoryScope {
  const record = objectValue(value, "MemoryScope");
  return {
    tenantId: stringValue(record.tenant_id, "tenant_id"),
    workspaceId: stringValue(record.workspace_id, "workspace_id"),
    userId: stringValue(record.user_id, "user_id"),
    agentId: stringValue(record.agent_id, "agent_id")
  };
}

function contextBundleFromWire(value: unknown): ContextBundle {
  const record = objectValue(value, "ContextBundle");
  const rawItems = Array.isArray(record.items) ? record.items : [];
  return {
    items: rawItems.map(contextItemFromWire),
    ...(typeof record.token_budget === "number"
      ? { tokenBudget: record.token_budget }
      : {}),
    metadata: optionalRecord(record.metadata)
  };
}

function contextItemFromWire(value: unknown): ContextItem {
  const record = objectValue(value, "ContextItem");
  const band = stringValue(record.band, "band");
  if (
    band !== "immediate" &&
    band !== "working" &&
    band !== "background" &&
    band !== "drilldown_marker"
  ) {
    throw new TypeError(`Invalid context band: ${band}`);
  }
  return {
    band,
    content: stringValue(record.content, "content"),
    sourceMemoryIds: stringArray(
      record.source_memory_ids,
      "source_memory_ids"
    ),
    ...(typeof record.retrieval_marker === "string"
      ? { retrievalMarker: record.retrieval_marker }
      : {}),
    metadata: optionalRecord(record.metadata)
  };
}

function extractToolPayload(value: unknown): unknown {
  const result = optionalRecord(value);
  const structured = optionalRecord(result.structuredContent);
  if (Object.keys(structured).length > 0) {
    return "result" in structured ? structured.result : structured;
  }
  if ("toolResult" in result) {
    return result.toolResult;
  }
  const content = result.content;
  if (Array.isArray(content)) {
    for (const item of content) {
      const record = optionalRecord(item);
      if (record.type !== "text" || typeof record.text !== "string") {
        continue;
      }
      try {
        const parsed: unknown = JSON.parse(record.text);
        const parsedRecord = optionalRecord(parsed);
        return "result" in parsedRecord ? parsedRecord.result : parsed;
      } catch {
        continue;
      }
    }
  }
  throw new TypeError("ALTM MCP tool returned no structured JSON payload");
}

function objectValue(
  value: unknown,
  label: string
): Record<string, unknown> {
  if (value === null || typeof value !== "object" || Array.isArray(value)) {
    throw new TypeError(`${label} must be an object`);
  }
  return value as Record<string, unknown>;
}

function optionalRecord(value: unknown): Record<string, unknown> {
  if (value === null || typeof value !== "object" || Array.isArray(value)) {
    return {};
  }
  return value as Record<string, unknown>;
}

function stringValue(value: unknown, field: string): string {
  if (typeof value !== "string") {
    throw new TypeError(`${field} must be a string`);
  }
  return value;
}

function stringArray(value: unknown, field: string): string[] {
  if (!Array.isArray(value) || value.some((item) => typeof item !== "string")) {
    throw new TypeError(`${field} must be an array of strings`);
  }
  return [...value] as string[];
}
