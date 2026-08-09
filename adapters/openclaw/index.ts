import {
  AltmRuntimeClient,
  AltmTurnCoordinator,
  StreamableHttpToolCaller,
  type MemoryScope,
  type PreparedHostTurn
} from "@altm/sdk";
import { definePluginEntry } from "openclaw/plugin-sdk/plugin-entry";

interface PluginConfig {
  endpoint: string;
  apiKey: string;
  tenantId: string;
  workspaceId: string;
  userId: string;
  agentId?: string;
  tokenBudget: number;
  recallLimit: number;
  activeLimit: number;
}

export default definePluginEntry({
  id: "altm-memory",
  name: "ALTM Memory",
  description: "ALTM prepare/commit memory loop over runtime MCP",
  kind: "memory" as const,
  register(api) {
    const config = parseConfig(api.pluginConfig);
    const pendingBySession = new Map<string, PreparedHostTurn>();
    let coordinatorPromise: Promise<AltmTurnCoordinator> | undefined;

    const coordinator = (): Promise<AltmTurnCoordinator> => {
      coordinatorPromise ??= StreamableHttpToolCaller.connect({
        url: config.endpoint,
        apiKey: config.apiKey,
        clientName: "altm-openclaw"
      }).then(
        (caller) =>
          new AltmTurnCoordinator(new AltmRuntimeClient(caller))
      );
      return coordinatorPromise;
    };

    api.on("before_prompt_build", async (event, ctx) => {
      const sessionId = ctx.sessionId ?? ctx.sessionKey;
      if (
        !sessionId ||
        isIncognitoSessionKey(ctx.sessionKey) ||
        !event.prompt
      ) {
        return undefined;
      }
      const content =
        latestMessageText(event.messages, "user") ?? event.prompt;
      if (!content.trim()) {
        return undefined;
      }
      const turnId =
        optionalString(ctx.runId) ?? crypto.randomUUID();
      try {
        const prepared = await (await coordinator()).prepare({
          scope: resolveScope(config, ctx.agentId),
          sessionId,
          turnId,
          content,
          query: content,
          tokenBudget: config.tokenBudget,
          recallLimit: config.recallLimit,
          activeLimit: config.activeLimit
        });
        if (pendingBySession.has(sessionId)) {
          api.logger.warn(
            `altm-memory: replacing an uncommitted turn for ${sessionId}`
          );
        }
        pendingBySession.set(sessionId, prepared);
        return prepared.injectedContext
          ? { prependContext: prepared.injectedContext }
          : undefined;
      } catch (error) {
        api.logger.warn(
          `altm-memory: prepare_turn failed: ${errorMessage(error)}`
        );
        return undefined;
      }
    });

    api.on("agent_end", async (event, ctx) => {
      const sessionId = ctx.sessionId ?? ctx.sessionKey;
      if (!sessionId || isIncognitoSessionKey(ctx.sessionKey)) {
        return;
      }
      const prepared = pendingBySession.get(sessionId);
      if (!prepared) {
        return;
      }
      pendingBySession.delete(sessionId);
      if (!event.success) {
        return;
      }
      const assistantContent = latestMessageText(
        event.messages,
        "assistant"
      );
      if (!assistantContent) {
        api.logger.warn(
          `altm-memory: no final assistant message for ${sessionId}`
        );
        return;
      }
      try {
        await (await coordinator()).commit({
          prepared: prepared.prepared,
          assistantContent
        });
      } catch (error) {
        api.logger.warn(
          `altm-memory: commit_turn failed: ${errorMessage(error)}`
        );
      }
    });

    api.on("session_end", (_event, ctx) => {
      const sessionId = ctx.sessionId ?? ctx.sessionKey;
      if (sessionId) {
        pendingBySession.delete(sessionId);
      }
    });

    api.on("gateway_stop", async () => {
      if (coordinatorPromise) {
        await (await coordinatorPromise).close();
      }
    });
  }
});

function parseConfig(value: unknown): PluginConfig {
  const config = recordValue(value, "plugin config");
  return {
    endpoint: requiredString(config.endpoint, "endpoint"),
    apiKey: requiredString(config.apiKey, "apiKey"),
    tenantId: requiredString(config.tenantId, "tenantId"),
    workspaceId: requiredString(config.workspaceId, "workspaceId"),
    userId: requiredString(config.userId, "userId"),
    ...(typeof config.agentId === "string"
      ? { agentId: config.agentId }
      : {}),
    tokenBudget: positiveInteger(config.tokenBudget, 1200, "tokenBudget"),
    recallLimit: positiveInteger(config.recallLimit, 10, "recallLimit"),
    activeLimit: positiveInteger(config.activeLimit, 5, "activeLimit")
  };
}

function resolveScope(
  config: PluginConfig,
  runtimeAgentId: string | undefined
): MemoryScope {
  return {
    tenantId: config.tenantId,
    workspaceId: config.workspaceId,
    userId: config.userId,
    agentId: config.agentId ?? runtimeAgentId ?? "openclaw"
  };
}

function latestMessageText(
  value: unknown,
  role: "user" | "assistant"
): string | undefined {
  if (!Array.isArray(value)) {
    return undefined;
  }
  for (let index = value.length - 1; index >= 0; index -= 1) {
    const message = recordValue(value[index], "message", false);
    if (message.role !== role) {
      continue;
    }
    if (typeof message.content === "string") {
      return message.content;
    }
    if (!Array.isArray(message.content)) {
      continue;
    }
    const parts = message.content
      .map((part) => recordValue(part, "content part", false))
      .filter((part) => part.type === "text" && typeof part.text === "string")
      .map((part) => part.text as string);
    if (parts.length > 0) {
      return parts.join("\n");
    }
  }
  return undefined;
}

function recordValue(
  value: unknown,
  label: string,
  strict = true
): Record<string, unknown> {
  if (value !== null && typeof value === "object" && !Array.isArray(value)) {
    return value as Record<string, unknown>;
  }
  if (strict) {
    throw new TypeError(`${label} must be an object`);
  }
  return {};
}

function requiredString(value: unknown, field: string): string {
  if (typeof value !== "string" || !value.trim()) {
    throw new TypeError(`ALTM OpenClaw ${field} must be a non-empty string`);
  }
  return value.trim();
}

function optionalString(value: unknown): string | undefined {
  return typeof value === "string" && value ? value : undefined;
}

function isIncognitoSessionKey(value: unknown): boolean {
  return (
    typeof value === "string" &&
    /(?:^|:)incognito(?:$|:)/i.test(value)
  );
}

function positiveInteger(
  value: unknown,
  fallback: number,
  field: string
): number {
  if (value === undefined) {
    return fallback;
  }
  if (!Number.isInteger(value) || Number(value) <= 0) {
    throw new TypeError(`ALTM OpenClaw ${field} must be a positive integer`);
  }
  return Number(value);
}

function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}
