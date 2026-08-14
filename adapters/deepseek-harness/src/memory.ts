/**
 * Host-neutral long-term-memory capability consumed by DeepSeek Harness.
 *
 * @module @altm/deepseek-harness/memory
 */

import { Context, Service } from "@deepseek-ai/cordis";

/** Stable identity scope applied by a long-term-memory provider. */
export interface LongTermMemoryScope {
  tenantId: string;
  workspaceId: string;
  userId: string;
  agentId: string;
}

/** Opaque provider handle for one prepared Host turn. */
export type MemoryTurnHandle = string & {
  readonly __memoryTurnHandle: unique symbol;
};

/** Brand one provider handle without changing its runtime representation. */
export function memoryTurnHandle(value: string): MemoryTurnHandle {
  return value as MemoryTurnHandle;
}

/** Input captured before the Host admits its first model step. */
export interface PrepareMemoryTurnInput {
  scope: LongTermMemoryScope;
  sessionId: string;
  turnId: string;
  content: string;
  messageId?: string;
  tokenBudget?: number;
  recallLimit?: number;
  activeWindowMode?: "off" | "limited" | "full";
  activeLimit?: number;
  strictSession?: boolean;
}

/** Compact replay-safe account of context selected for one Host turn. */
export interface MemoryTurnActivity {
  includedCount: number;
  tokenCountEstimate: number;
  graphMatchCount: number;
  layerCounts: Readonly<Partial<Record<"L0" | "L1" | "L2" | "L3" | "L4", number>>>;
  memoryIds: readonly string[];
}

/** Provider result retained until the Host turn commits or aborts. */
export interface PreparedMemoryTurn {
  handle: MemoryTurnHandle;
  scope: LongTermMemoryScope;
  sessionId: string;
  turnId: string;
  context: string;
  citationMemoryIds: readonly string[];
  activity: MemoryTurnActivity;
}

/** Final successful Host response for one prepared turn. */
export interface CommitMemoryTurnInput {
  prepared: PreparedMemoryTurn;
  assistantContent: string;
  assistantMessageId?: string;
}

/** Terminal non-commit outcome for one prepared turn. */
export interface AbortMemoryTurnInput {
  prepared: PreparedMemoryTurn;
  reason: string;
}

declare module "@deepseek-ai/cordis" {
  interface Context {
    longTermMemory: LongTermMemory;
  }
}

/**
 * Replaceable long-term-memory provider.
 *
 * Implementations own transport, storage identity, citation extraction, and
 * prepare/commit/abort idempotency. The Harness consumer owns only lifecycle
 * mapping and durable model-visible injection.
 */
export abstract class LongTermMemory extends Service {
  constructor(ctx: Context) {
    super(ctx, "longTermMemory");
  }

  /**
   * Capture one direct user turn and return model-visible recalled context.
   * @param input - Host turn identity, content, scope, and recall limits.
   * @param signal - cancellation for the open Host turn.
   * @returns the provider handle and context retained until settlement.
   */
  abstract prepare(
    input: PrepareMemoryTurnInput,
    signal: AbortSignal,
  ): Promise<PreparedMemoryTurn>;

  /**
   * Commit the final Assistant response after the durable Host turn ends.
   * @param input - prepared handle and final Assistant response.
   */
  abstract commit(input: CommitMemoryTurnInput): Promise<void>;

  /**
   * Close a prepared turn that cannot commit an Assistant response.
   * @param input - prepared handle and stable Host-owned reason.
   */
  abstract abort(input: AbortMemoryTurnInput): Promise<void>;
}

export default LongTermMemory;
