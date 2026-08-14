/**
 * DeepSeek Harness lifecycle consumer for a replaceable long-term-memory provider.
 *
 * @module @altm/deepseek-harness
 */

import { randomUUID } from "node:crypto";
import type { Context } from "@deepseek-ai/cordis";
import type { Agent, PreStepDecision } from "@deepseek-ai/dsh-agent";
import type { AssistantMessage, UserMessage } from "@deepseek-ai/dsh-llm";
import type { Session, TurnEndReason } from "@deepseek-ai/dsh-session";
import z from "@deepseek-ai/schemastery";
import type {
  LongTermMemory,
  LongTermMemoryScope,
  MemoryTurnActivity,
  PreparedMemoryTurn,
} from "@altm/deepseek-harness/memory";

const DEFAULT_TENANT_ID = "local";
const DEFAULT_USER_ID = "local";
const DEFAULT_AGENT_ID = "deepseek-harness";
const DEFAULT_TOKEN_BUDGET = 1200;
const DEFAULT_RECALL_LIMIT = 10;
const DEFAULT_ACTIVE_LIMIT = 5;

/** Cordis plugin name used in durable message attribution and diagnostics. */
export const name = "altm-memory";

/** Listeners mount before the Agent service so the first turn cannot race setup. */
export const inject: string[] = [];

/** Host identity scope and recall limits forwarded to the active provider. */
export interface Config {
  tenantId?: string;
  workspaceId?: string;
  userId?: string;
  agentId?: string;
  tokenBudget?: number;
  recallLimit?: number;
  activeWindowMode?: "off" | "limited" | "full";
  activeLimit?: number;
  strictSession?: boolean;
}

/** Schemastery validation and defaults for {@link Config}. */
export const Config: z<Config> = z.object({
  tenantId: z.string().default(DEFAULT_TENANT_ID),
  workspaceId: z.string(),
  userId: z.string().default(DEFAULT_USER_ID),
  agentId: z.string().default(DEFAULT_AGENT_ID),
  tokenBudget: z.number().default(DEFAULT_TOKEN_BUDGET),
  recallLimit: z.number().default(DEFAULT_RECALL_LIMIT),
  activeWindowMode: z.union([
    z.const("off"),
    z.const("limited"),
    z.const("full"),
  ]),
  activeLimit: z.number().default(DEFAULT_ACTIVE_LIMIT),
  strictSession: z.boolean().default(false),
});

interface ResolvedConfig {
  tenantId: string;
  workspaceId?: string;
  userId: string;
  agentId: string;
  tokenBudget: number;
  recallLimit: number;
  activeWindowMode?: "off" | "limited" | "full";
  activeLimit: number;
  strictSession: boolean;
}

interface TurnInput {
  content: string;
  messageId?: string;
}

interface AssistantReply {
  content: string;
  messageId: string;
}

interface PendingTurn {
  provider: LongTermMemory;
  prepared: PreparedMemoryTurn;
}

declare module "@deepseek-ai/dsh-llm" {
  interface MessageSourceMap {
    /** Durable ALTM recall provenance and compact activity metadata. */
    altmMemory: {
      kind: "session-reference";
      plugin: "altm-memory";
      form: "notice";
      summary: string;
      references: readonly {
        label: string;
        retainedMessages: number;
        omittedMessages: number;
        truncated: boolean;
      }[];
      activity: MemoryTurnActivity;
    };
  }
}

/**
 * Map Harness turn lifecycle events onto the active `ctx.longTermMemory` provider.
 *
 * @param ctx - Cordis context carrying live Agent and Session events.
 * @param config - Host scope and recall limits.
 */
export function apply(ctx: Context, config: Config): void {
  const resolved = resolveConfig(config);
  const pending = new Map<Session, Map<number, PendingTurn>>();
  const active = new Set<Promise<unknown>>();
  let closing = false;

  const track = <T>(promise: Promise<T>): Promise<T> => {
    const tracked = promise.finally(() => {
      active.delete(tracked);
    });
    active.add(tracked);
    return tracked;
  };

  const drainActive = async (): Promise<void> => {
    while (active.size > 0) {
      await Promise.allSettled([...active]);
    }
  };

  // Register cleanup before listeners. Cordis removes later listener effects
  // first, then this finalizer aborts every turn no listener can settle.
  ctx.effect(() => async () => {
    closing = true;
    await drainActive();
    const unsettled = [...pending.values()].flatMap(
      (turns) => [...turns.values()],
    );
    pending.clear();
    for (const turn of unsettled) {
      void track(turn.provider.abort({
        prepared: turn.prepared,
        reason: "consumer-unloaded",
      })).catch((error: unknown) => {
        ctx.logger.warn(
          `altm-memory: abort_turn failed during unload for `
          + `${turn.prepared.sessionId}/${turn.prepared.turnId}: `
          + errorMessage(error),
        );
      });
    }
    await drainActive();
  }, "altm-memory: abort pending turns and drain provider operations");

  ctx.on("agent/pre-step", async (
    { agent, step, turn, signal },
    next,
  ): Promise<PreStepDecision> => {
    const decision = await next();
    if (
      decision.kind === "reject"
      || step !== 1
      || signal.aborted
      || closing
    ) {
      return decision;
    }
    const provider = ctx.get("longTermMemory");
    const input = directTurnInput(decision.messages);
    if (provider === undefined || input === undefined) {
      return decision;
    }
    try {
      const prepared = await track(provider.prepare({
        scope: memoryScope(agent, resolved),
        sessionId: String(agent.session.id),
        turnId: String(turn),
        content: input.content,
        ...(input.messageId === undefined
          ? {}
          : { messageId: input.messageId }),
        tokenBudget: resolved.tokenBudget,
        recallLimit: resolved.recallLimit,
        ...(resolved.activeWindowMode === undefined
          ? {}
          : { activeWindowMode: resolved.activeWindowMode }),
        activeLimit: resolved.activeLimit,
        strictSession: resolved.strictSession,
      }, signal));
      if (signal.aborted || closing) {
        await track(provider.abort({
          prepared,
          reason: closing
            ? "consumer-unloaded"
            : "host-turn-aborted",
        }));
        return decision;
      }
      let sessionTurns = pending.get(agent.session);
      if (sessionTurns === undefined) {
        sessionTurns = new Map();
        pending.set(agent.session, sessionTurns);
      }
      const replaced = sessionTurns.get(turn);
      if (
        replaced !== undefined
        && (
          replaced.provider !== provider
          || replaced.prepared.handle !== prepared.handle
        )
      ) {
        await track(replaced.provider.abort({
          prepared: replaced.prepared,
          reason: "prepared-turn-replaced",
        }));
      }
      sessionTurns.set(turn, { provider, prepared });
      if (!prepared.context) return decision;
      return {
        kind: "enter",
        messages: [
          ...decision.messages,
          createMemoryMessage(prepared.context, prepared.activity),
        ],
      };
    } catch (error: unknown) {
      if (!signal.aborted) {
        ctx.logger.warn(
          `altm-memory: prepare_turn failed for `
          + `${String(agent.session.id)}/${turn}: ${errorMessage(error)}`,
        );
      }
      return decision;
    }
  }, { prepend: true });

  ctx.on("session/event", (session, event) => {
    if (event.type !== "turn/end") return;
    const sessionTurns = pending.get(session);
    const turn = sessionTurns?.get(event.data.turn);
    if (turn === undefined) return;
    sessionTurns?.delete(event.data.turn);
    if (sessionTurns?.size === 0) pending.delete(session);
    void track(settleTurn(ctx, session, event.data.reason, turn)).catch(
      (error: unknown) => {
        ctx.logger.warn(
          `altm-memory: turn settlement failed for `
          + `${String(session.id)}/${event.data.turn}: ${errorMessage(error)}`,
        );
      },
    );
  });
}

async function settleTurn(
  ctx: Context,
  session: Session,
  reason: TurnEndReason,
  turn: PendingTurn,
): Promise<void> {
  if (reason.kind !== "completed" && reason.kind !== "max-tokens") {
    await turn.provider.abort({
      prepared: turn.prepared,
      reason: `host-turn-${reason.kind}`,
    });
    return;
  }
  const reply = latestAssistantReply(session, Number(turn.prepared.turnId));
  if (reply === undefined) {
    await turn.provider.abort({
      prepared: turn.prepared,
      reason: "host-turn-no-assistant-message",
    });
    return;
  }
  try {
    await turn.provider.commit({
      prepared: turn.prepared,
      assistantContent: reply.content,
      assistantMessageId: reply.messageId,
    });
  } catch (commitError: unknown) {
    try {
      await turn.provider.abort({
        prepared: turn.prepared,
        reason: "host-turn-commit-failed",
      });
    } catch (abortError: unknown) {
      ctx.logger.warn(
        `altm-memory: fallback abort failed for `
        + `${turn.prepared.sessionId}/${turn.prepared.turnId}: `
        + errorMessage(abortError),
      );
    }
    throw commitError;
  }
}

function resolveConfig(config: Config): ResolvedConfig {
  return {
    tenantId: nonEmpty(
      config.tenantId ?? DEFAULT_TENANT_ID,
      "tenantId",
    ),
    ...(config.workspaceId === undefined
      ? {}
      : { workspaceId: nonEmpty(config.workspaceId, "workspaceId") }),
    userId: nonEmpty(config.userId ?? DEFAULT_USER_ID, "userId"),
    agentId: nonEmpty(config.agentId ?? DEFAULT_AGENT_ID, "agentId"),
    tokenBudget: positiveInteger(
      config.tokenBudget ?? DEFAULT_TOKEN_BUDGET,
      "tokenBudget",
    ),
    recallLimit: positiveInteger(
      config.recallLimit ?? DEFAULT_RECALL_LIMIT,
      "recallLimit",
    ),
    ...(config.activeWindowMode === undefined
      ? {}
      : { activeWindowMode: config.activeWindowMode }),
    activeLimit: positiveInteger(
      config.activeLimit ?? DEFAULT_ACTIVE_LIMIT,
      "activeLimit",
    ),
    strictSession: config.strictSession ?? false,
  };
}

function memoryScope(
  agent: Agent,
  config: ResolvedConfig,
): LongTermMemoryScope {
  return {
    tenantId: config.tenantId,
    workspaceId: config.workspaceId
      ?? agent.session.header.cwd
      ?? "default",
    userId: config.userId,
    agentId: config.agentId,
  };
}

function directTurnInput(
  messages: readonly UserMessage[],
): TurnInput | undefined {
  const direct = messages.filter(
    (message) => message.source.kind === "user",
  );
  const parts = direct.flatMap((message) => message.content.flatMap(
    (block) => block.type === "text" ? [block.text] : [],
  ));
  const content = parts.join("\n\n").trim();
  if (!content) return undefined;
  return {
    content,
    ...(direct.length === 1 && direct[0] !== undefined
      ? { messageId: String(direct[0].id) }
      : {}),
  };
}

function createMemoryMessage(
  text: string,
  activity: MemoryTurnActivity,
): UserMessage {
  const content: UserMessage["content"] = [{ type: "text", text }];
  Object.freeze(content[0]);
  Object.freeze(content);
  const source: UserMessage["source"] = {
    kind: "session-reference",
    plugin: name,
    form: "notice",
    summary: activitySummary(activity),
    references: [{
      label: "ALTM",
      retainedMessages: activity.includedCount,
      omittedMessages: 0,
      truncated: false,
    }],
    activity,
  };
  Object.freeze(source);
  return Object.freeze({
    id: randomUUID() as UserMessage["id"],
    role: "user",
    content,
    source,
  });
}

function activitySummary(activity: MemoryTurnActivity): string {
  const layers = (["L1", "L2", "L3", "L4"] as const).flatMap(
    (layer) => {
      const count = activity.layerCounts[layer];
      return count === undefined || count === 0 ? [] : [`${layer} ${count}`];
    },
  );
  return [
    String(activity.includedCount),
    ...layers,
    ...(activity.graphMatchCount === 0
      ? []
      : [`Graph ${activity.graphMatchCount}`]),
    `~${activity.tokenCountEstimate} tok`,
  ].join(" · ");
}

function latestAssistantReply(
  session: Session,
  turn: number,
): AssistantReply | undefined {
  for (const event of [...session.events].reverse()) {
    if (event.type === "turn/start" && event.data.turn === turn) break;
    if (
      event.type !== "assistant/message"
      || event.data.turn !== turn
    ) {
      continue;
    }
    const content = assistantText(event.data.message);
    if (content) {
      return {
        content,
        messageId: String(event.data.message.id),
      };
    }
  }
  return undefined;
}

function assistantText(message: AssistantMessage): string | undefined {
  const text = message.content.flatMap(
    (block) => block.type === "text" ? [block.text] : [],
  ).join("");
  return text.trim() ? text : undefined;
}

function positiveInteger(value: number, field: string): number {
  if (!Number.isSafeInteger(value) || value <= 0) {
    throw new TypeError(
      `altm-memory: ${field} must be a positive safe integer, got `
      + String(value),
    );
  }
  return value;
}

function nonEmpty(value: string, field: string): string {
  const result = value.trim();
  if (!result) {
    throw new TypeError(`altm-memory: ${field} must be a non-empty string`);
  }
  return result;
}

function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}
