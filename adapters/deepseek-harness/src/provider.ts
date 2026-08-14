/**
 * ALTM Streamable HTTP implementation of the long-term-memory capability.
 *
 * @module @altm/deepseek-harness/provider
 */

import {
  AltmRuntimeClient,
  StreamableHttpToolCaller,
  renderContext,
} from "@altm/sdk";
import { Context, Service } from "@deepseek-ai/cordis";
import z from "@deepseek-ai/schemastery";
import {
  LongTermMemory,
  memoryTurnHandle,
  type AbortMemoryTurnInput,
  type CommitMemoryTurnInput,
  type MemoryTurnActivity,
  type PrepareMemoryTurnInput,
  type PreparedMemoryTurn,
} from "@altm/deepseek-harness/memory";

const DEFAULT_ENDPOINT = "http://127.0.0.1:8000/mcp";
const DEFAULT_API_KEY_ENV = "ALTM_MCP_API_KEY";
const DEFAULT_TIMEOUT_MS = 15_000;
const DEFAULT_REQUEST_ATTEMPTS = 2;
const CREDENTIAL_REF = /^[A-Za-z_][A-Za-z0-9_]*$/;
const HEADER_VALUE = /^[\x21-\x7E]+$/;

/** ALTM endpoint, credential reference, and request policy. */
export interface Config {
  endpoint?: string;
  apiKeyEnv?: string;
  timeoutMs?: number;
  requestAttempts?: number;
}

interface ResolvedConfig {
  endpoint: URL;
  apiKeyEnv: string;
  timeoutMs: number;
  requestAttempts: number;
}

interface CredentialResolver {
  resolve(ref: string): Promise<{ value: string } | undefined>;
}

class OperationDeadline {
  private readonly expiresAt: number;

  constructor(private readonly timeoutMs: number) {
    this.expiresAt = Date.now() + timeoutMs;
  }

  async wait<T>(
    promise: Promise<T>,
    signal: AbortSignal,
    label: string,
  ): Promise<T> {
    signal.throwIfAborted();
    const remaining = this.expiresAt - Date.now();
    if (remaining <= 0) {
      throw new Error(`altm-memory: ${label} exceeded ${this.timeoutMs}ms`);
    }
    return new Promise<T>((resolve, reject) => {
      let settled = false;
      const finish = (callback: () => void): void => {
        if (settled) return;
        settled = true;
        clearTimeout(timer);
        signal.removeEventListener("abort", onAbort);
        callback();
      };
      const onAbort = (): void => {
        finish(() => reject(abortReason(signal)));
      };
      const timer = setTimeout(() => {
        finish(() => reject(
          new Error(`altm-memory: ${label} exceeded ${this.timeoutMs}ms`),
        ));
      }, remaining);
      signal.addEventListener("abort", onAbort, { once: true });
      promise.then(
        (value) => finish(() => resolve(value)),
        (error: unknown) => finish(() => reject(error)),
      );
    });
  }

  hasTime(): boolean {
    return Date.now() < this.expiresAt;
  }
}

/** ALTM MCP provider for `ctx.longTermMemory`. */
export class AltmLongTermMemoryProvider extends LongTermMemory {
  static Config: z<Config> = z.object({
    endpoint: z.string().default(DEFAULT_ENDPOINT),
    apiKeyEnv: z.string().default(DEFAULT_API_KEY_ENV),
    timeoutMs: z.number().default(DEFAULT_TIMEOUT_MS),
    requestAttempts: z.number().default(DEFAULT_REQUEST_ATTEMPTS),
  });

  private readonly resolved: ResolvedConfig;
  private readonly active = new Set<Promise<unknown>>();
  private closed = false;

  constructor(ctx: Context, public config: Config) {
    super(ctx);
    this.resolved = resolveConfig(config);
  }

  async* [Service.init](): AsyncGenerator<() => Promise<void>, void, void> {
    yield async () => {
      this.closed = true;
      await Promise.allSettled([...this.active]);
    };
  }

  override async prepare(
    input: PrepareMemoryTurnInput,
    signal: AbortSignal,
  ): Promise<PreparedMemoryTurn> {
    const prepared = await this.run(
      false,
      signal,
      "prepare_turn",
      (client) => client.prepareTurn(input),
    );
    return {
      handle: memoryTurnHandle(prepared.cycleId),
      scope: prepared.scope,
      sessionId: prepared.sessionId,
      turnId: prepared.turnId,
      context: renderContext(prepared),
      citationMemoryIds: prepared.context.items.flatMap(
        (item) => item.sourceMemoryIds,
      ),
      activity: memoryActivity(prepared.context.items),
    };
  }

  override async commit(input: CommitMemoryTurnInput): Promise<void> {
    const citedMemoryIds = extractCitations(
      input.assistantContent,
      input.prepared.citationMemoryIds,
    );
    await this.run(
      false,
      new AbortController().signal,
      "commit_turn",
      (client) => client.commitTurn({
        scope: input.prepared.scope,
        cycleId: input.prepared.handle,
        assistantContent: input.assistantContent,
        citedMemoryIds,
        ...(input.assistantMessageId === undefined
          ? {}
          : { assistantMessageId: input.assistantMessageId }),
      }),
    );
  }

  override async abort(input: AbortMemoryTurnInput): Promise<void> {
    await this.run(
      true,
      new AbortController().signal,
      "abort_turn",
      (client) => client.abortTurn({
        scope: input.prepared.scope,
        cycleId: input.prepared.handle,
        reason: input.reason,
      }),
    );
  }

  private run<T>(
    allowClosed: boolean,
    signal: AbortSignal,
    label: string,
    operation: (client: AltmRuntimeClient) => Promise<T>,
  ): Promise<T> {
    if (this.closed && !allowClosed) {
      return Promise.reject(new Error("altm-memory provider is disposed"));
    }
    const task = this.call(signal, label, operation).finally(() => {
      this.active.delete(task);
    });
    this.active.add(task);
    return task;
  }

  private async call<T>(
    signal: AbortSignal,
    label: string,
    operation: (client: AltmRuntimeClient) => Promise<T>,
  ): Promise<T> {
    const deadline = new OperationDeadline(this.resolved.timeoutMs);
    const apiKey = await deadline.wait(
      this.resolveApiKey(),
      signal,
      `${label} credential resolution`,
    );
    let lastError: unknown;
    for (
      let attempt = 1;
      attempt <= this.resolved.requestAttempts && deadline.hasTime();
      attempt += 1
    ) {
      signal.throwIfAborted();
      try {
        return await this.callOnce(
          apiKey,
          signal,
          deadline,
          label,
          operation,
        );
      } catch (error: unknown) {
        lastError = error;
        if (signal.aborted) throw abortReason(signal);
      }
    }
    throw lastError ?? new Error(`altm-memory: ${label} exhausted its deadline`);
  }

  private async callOnce<T>(
    apiKey: string,
    signal: AbortSignal,
    deadline: OperationDeadline,
    label: string,
    operation: (client: AltmRuntimeClient) => Promise<T>,
  ): Promise<T> {
    const connecting = StreamableHttpToolCaller.connect({
      url: this.resolved.endpoint,
      apiKey,
      clientName: "altm-deepseek-harness",
    });
    let caller: StreamableHttpToolCaller;
    try {
      caller = await deadline.wait(connecting, signal, `${label} MCP connect`);
    } catch (error: unknown) {
      void connecting.then(
        (lateCaller) => lateCaller.close(),
        () => undefined,
      );
      throw error;
    }
    const client = new AltmRuntimeClient(caller);
    try {
      return await deadline.wait(
        operation(client),
        signal,
        `${label} MCP call`,
      );
    } finally {
      try {
        await client.close();
      } catch (error: unknown) {
        this.ctx.logger.warn("altm-memory: MCP transport close failed");
        this.ctx.logger.warn(error);
      }
    }
  }

  private async resolveApiKey(): Promise<string> {
    const credentials = this.ctx.get("credentials") as
      | CredentialResolver
      | undefined;
    const raw = credentials === undefined
      ? process.env[this.resolved.apiKeyEnv]
      : (await credentials.resolve(this.resolved.apiKeyEnv))?.value;
    const value = raw?.trim();
    if (!value) {
      throw new Error(
        `altm-memory: no API key for ${this.resolved.apiKeyEnv}; store it `
        + "through the Harness credentials service or export that variable",
      );
    }
    if (!HEADER_VALUE.test(value)) {
      throw new Error(
        `altm-memory: credential ${this.resolved.apiKeyEnv} contains `
        + "characters that cannot be used in an Authorization header",
      );
    }
    return value;
  }
}

function resolveConfig(config: Config): ResolvedConfig {
  const endpoint = new URL(nonEmpty(
    config.endpoint ?? DEFAULT_ENDPOINT,
    "endpoint",
  ));
  if (endpoint.protocol !== "http:" && endpoint.protocol !== "https:") {
    throw new TypeError(
      `altm-memory: endpoint must use http or https, got ${endpoint.protocol}`,
    );
  }
  const apiKeyEnv = nonEmpty(
    config.apiKeyEnv ?? DEFAULT_API_KEY_ENV,
    "apiKeyEnv",
  );
  if (!CREDENTIAL_REF.test(apiKeyEnv)) {
    throw new TypeError(
      `altm-memory: apiKeyEnv must be a POSIX environment variable name, got `
      + JSON.stringify(apiKeyEnv),
    );
  }
  return {
    endpoint,
    apiKeyEnv,
    timeoutMs: positiveInteger(
      config.timeoutMs ?? DEFAULT_TIMEOUT_MS,
      "timeoutMs",
    ),
    requestAttempts: positiveInteger(
      config.requestAttempts ?? DEFAULT_REQUEST_ATTEMPTS,
      "requestAttempts",
    ),
  };
}

function extractCitations(
  assistantContent: string,
  allowedMemoryIds: readonly string[],
): string[] {
  const allowed = new Set(allowedMemoryIds);
  const cited: string[] = [];
  for (const match of assistantContent.matchAll(
    /memory:\/\/([A-Za-z0-9._:-]+)/g,
  )) {
    const memoryId = match[1];
    if (memoryId && allowed.has(memoryId) && !cited.includes(memoryId)) {
      cited.push(memoryId);
    }
  }
  return cited;
}

function memoryActivity(
  items: readonly {
    sourceMemoryIds: readonly string[];
    metadata: Record<string, unknown>;
  }[],
): MemoryTurnActivity {
  const layerCounts: Partial<Record<"L0" | "L1" | "L2" | "L3" | "L4", number>> = {};
  let tokenCountEstimate = 0;
  let graphMatchCount = 0;
  const memoryIds: string[] = [];
  for (const item of items) {
    const layer = item.metadata.layer;
    if (
      layer === "L0"
      || layer === "L1"
      || layer === "L2"
      || layer === "L3"
      || layer === "L4"
    ) {
      layerCounts[layer] = (layerCounts[layer] ?? 0) + 1;
    }
    const tokens = item.metadata.token_count_estimate;
    if (typeof tokens === "number" && Number.isFinite(tokens)) {
      tokenCountEstimate += Math.max(0, Math.round(tokens));
    }
    const matchedBy = item.metadata.matched_by;
    if (
      Array.isArray(matchedBy)
      && matchedBy.some(
        (source) => source === "graph_ppr" || source === "graph_subgraph",
      )
    ) {
      graphMatchCount += 1;
    }
    for (const memoryId of item.sourceMemoryIds) {
      if (!memoryIds.includes(memoryId)) memoryIds.push(memoryId);
    }
  }
  return {
    includedCount: items.length,
    tokenCountEstimate,
    graphMatchCount,
    layerCounts,
    memoryIds,
  };
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

function abortReason(signal: AbortSignal): Error {
  return signal.reason instanceof Error
    ? signal.reason
    : new Error("altm-memory operation aborted");
}

export default AltmLongTermMemoryProvider;
