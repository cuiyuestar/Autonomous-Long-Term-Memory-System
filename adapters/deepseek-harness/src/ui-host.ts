/**
 * Same-origin Host bridge for the ALTM browser plugin.
 *
 * @module @altm/deepseek-harness/ui-host
 */

import type { IncomingMessage, ServerResponse } from "node:http";
import { StreamableHttpToolCaller } from "@altm/sdk";
import type { Context } from "@deepseek-ai/cordis";
import type {} from "@deepseek-ai/dsh-host-webserver";
import type { SessionId } from "@deepseek-ai/dsh-session";
import type {} from "@deepseek-ai/dsh-session";
import z from "@deepseek-ai/schemastery";
import { ALTM_UI_API_PATH } from "./ui-contract.ts";

const DEFAULT_ENDPOINT = "http://127.0.0.1:8000/mcp";
const DEFAULT_API_KEY_ENV = "ALTM_MCP_API_KEY";
const MAX_BODY_BYTES = 64 * 1024;

/** MCP connection and memory identity used by the UI bridge. */
export interface Config {
  endpoint?: string;
  apiKeyEnv?: string;
  tenantId?: string;
  workspaceId?: string;
  userId?: string;
  agentId?: string;
}

/** Schemastery validation for the browser bridge configuration. */
export const Config: z<Config> = z.object({
  endpoint: z.string().default(DEFAULT_ENDPOINT),
  apiKeyEnv: z.string().default(DEFAULT_API_KEY_ENV),
  tenantId: z.string().default("local"),
  workspaceId: z.string(),
  userId: z.string().default("local"),
  agentId: z.string().default("deepseek-harness"),
});

interface CredentialResolver {
  resolve(ref: string): Promise<{ value: string } | undefined>;
}

interface ResolvedConfig {
  endpoint: URL;
  apiKeyEnv: string;
  tenantId: string;
  workspaceId?: string;
  userId: string;
  agentId: string;
}

class HttpError extends Error {
  constructor(
    readonly status: number,
    message: string,
  ) {
    super(message);
  }
}

/** Required Host services for the same-origin UI route. */
export const inject = ["webServer", "sessions"];

/**
 * Register scoped memory reads and write-only embedding configuration.
 *
 * @param ctx - Host context carrying the Web server and live Sessions.
 * @param config - ALTM endpoint, credential reference, and fixed identity.
 */
export function apply(ctx: Context, config: Config): void {
  const resolved = resolveConfig(config);
  ctx.effect(() => ctx.webServer.register({
    kind: "prefix",
    path: ALTM_UI_API_PATH,
    handler: async (req, res) => {
      try {
        const requestUrl = new URL(req.url ?? "/", "http://localhost");
        const operation = requestUrl.pathname.slice(ALTM_UI_API_PATH.length);
        if ((req.method ?? "GET") === "GET" && operation === "/health") {
          json(res, 200, { available: true });
          return;
        }
        if ((req.method ?? "GET") === "GET" && operation === "/embedding") {
          const payload = await callTool(
            ctx,
            resolved,
            "memory_ui_embedding_status",
            {},
          );
          json(res, 200, payload);
          return;
        }
        if ((req.method ?? "GET") === "POST" && operation === "/embedding") {
          const body = await readJsonBody(req);
          const baseUrl = requiredString(body, "baseUrl", 2048);
          const model = requiredString(body, "model", 256);
          const apiKey = optionalString(body, "apiKey", 4096);
          const payload = await callTool(
            ctx,
            resolved,
            "memory_ui_configure_embedding",
            {
              base_url: baseUrl,
              model,
              ...(apiKey === undefined ? {} : { api_key: apiKey }),
            },
          );
          json(res, 200, payload);
          return;
        }
        const sessionId = requestUrl.searchParams.get("sessionId");
        if (sessionId === null || sessionId.length === 0 || sessionId.length > 256) {
          throw new HttpError(400, "sessionId is required");
        }
        const session = ctx.sessions.get(sessionId as SessionId);
        if (session === undefined) {
          throw new HttpError(404, "session is not live");
        }
        const scope = {
          tenant_id: resolved.tenantId,
          workspace_id: resolved.workspaceId ?? session.header.cwd ?? "default",
          user_id: resolved.userId,
          agent_id: resolved.agentId,
        };
        switch (`${req.method ?? "GET"} ${operation}`) {
          case "GET /graph/seeds": {
            const query = requestUrl.searchParams.get("query")?.trim();
            const payload = await callTool(ctx, resolved, "memory_ui_graph_seeds", {
              ...scope,
              ...(query ? { query } : {}),
              limit: 24,
            });
            json(res, 200, payload);
            return;
          }
          case "POST /graph/neighborhood": {
            const body = await readJsonBody(req);
            const seeds = body.seedNodeIds;
            if (
              !Array.isArray(seeds)
              || seeds.length === 0
              || seeds.length > 8
              || seeds.some((seed) => typeof seed !== "string" || seed.length === 0)
            ) {
              throw new HttpError(400, "seedNodeIds must contain 1-8 ids");
            }
            const payload = await callTool(
              ctx,
              resolved,
              "memory_ui_graph_neighborhood",
              {
                ...scope,
                seed_node_ids: seeds,
                max_hops: 2,
                node_limit: 120,
              },
            );
            json(res, 200, payload);
            return;
          }
          case "GET /layers": {
            const payload = await callTool(ctx, resolved, "memory_ui_layers", {
              ...scope,
              limit_per_layer: 80,
            });
            json(res, 200, payload);
            return;
          }
          default:
            throw new HttpError(404, "unknown ALTM UI route");
        }
      } catch (error: unknown) {
        if (error instanceof HttpError) {
          json(res, error.status, { error: error.message });
          return;
        }
        ctx.logger.warn(
          `altm-memory-ui: ${error instanceof Error ? error.message : String(error)}`,
        );
        json(res, 502, { error: "ALTM memory service is unavailable" });
      }
    },
  }), "altm-memory-ui: browser route");
}

function resolveConfig(config: Config): ResolvedConfig {
  const endpoint = new URL(config.endpoint ?? DEFAULT_ENDPOINT);
  if (endpoint.protocol !== "http:" && endpoint.protocol !== "https:") {
    throw new TypeError("altm-memory-ui: endpoint must use HTTP or HTTPS");
  }
  return {
    endpoint,
    apiKeyEnv: nonEmpty(config.apiKeyEnv ?? DEFAULT_API_KEY_ENV, "apiKeyEnv"),
    tenantId: nonEmpty(config.tenantId ?? "local", "tenantId"),
    ...(config.workspaceId === undefined
      ? {}
      : { workspaceId: nonEmpty(config.workspaceId, "workspaceId") }),
    userId: nonEmpty(config.userId ?? "local", "userId"),
    agentId: nonEmpty(config.agentId ?? "deepseek-harness", "agentId"),
  };
}

async function callTool(
  ctx: Context,
  config: ResolvedConfig,
  name: string,
  args: Record<string, unknown>,
): Promise<unknown> {
  const apiKey = await resolveApiKey(ctx, config.apiKeyEnv);
  const caller = await StreamableHttpToolCaller.connect({
    url: config.endpoint,
    apiKey,
    clientName: "altm-deepseek-harness-ui",
  });
  try {
    return extractToolPayload(await caller.callTool(name, args));
  } finally {
    await caller.close();
  }
}

async function resolveApiKey(ctx: Context, ref: string): Promise<string> {
  const credentials = ctx.get("credentials") as CredentialResolver | undefined;
  const value = credentials === undefined
    ? process.env[ref]
    : (await credentials.resolve(ref))?.value;
  return nonEmpty(value ?? "", `credential ${ref}`);
}

function extractToolPayload(value: unknown): unknown {
  const result = record(value);
  const structured = record(result.structuredContent);
  if (Object.keys(structured).length > 0) {
    return "result" in structured ? structured.result : structured;
  }
  const content = result.content;
  if (Array.isArray(content)) {
    for (const item of content) {
      const entry = record(item);
      if (entry.type !== "text" || typeof entry.text !== "string") continue;
      try {
        const parsed: unknown = JSON.parse(entry.text);
        const parsedRecord = record(parsed);
        return "result" in parsedRecord ? parsedRecord.result : parsed;
      } catch {
        continue;
      }
    }
  }
  throw new TypeError("ALTM MCP tool returned no structured JSON payload");
}

async function readJsonBody(req: IncomingMessage): Promise<Record<string, unknown>> {
  const chunks: Buffer[] = [];
  let size = 0;
  for await (const chunk of req) {
    const buffer = Buffer.isBuffer(chunk) ? chunk : Buffer.from(chunk);
    size += buffer.length;
    if (size > MAX_BODY_BYTES) throw new HttpError(413, "request body is too large");
    chunks.push(buffer);
  }
  try {
    const value: unknown = JSON.parse(Buffer.concat(chunks).toString("utf8"));
    if (value === null || typeof value !== "object" || Array.isArray(value)) {
      throw new Error("body is not an object");
    }
    return value as Record<string, unknown>;
  } catch {
    throw new HttpError(400, "request body must be JSON");
  }
}

function json(res: ServerResponse, status: number, value: unknown): void {
  const body = JSON.stringify(value);
  res.writeHead(status, {
    "content-type": "application/json; charset=utf-8",
    "cache-control": "no-store",
    "content-length": Buffer.byteLength(body),
  });
  res.end(body);
}

function record(value: unknown): Record<string, unknown> {
  return value !== null && typeof value === "object" && !Array.isArray(value)
    ? value as Record<string, unknown>
    : {};
}

function requiredString(
  value: Record<string, unknown>,
  field: string,
  maxLength: number,
): string {
  const result = optionalString(value, field, maxLength);
  if (result === undefined) throw new HttpError(400, `${field} is required`);
  return result;
}

function optionalString(
  value: Record<string, unknown>,
  field: string,
  maxLength: number,
): string | undefined {
  const candidate = value[field];
  if (candidate === undefined) return undefined;
  if (typeof candidate !== "string") {
    throw new HttpError(400, `${field} must be a string`);
  }
  const result = candidate.trim();
  if (!result || result.length > maxLength) {
    throw new HttpError(
      400,
      `${field} must contain 1-${String(maxLength)} characters`,
    );
  }
  return result;
}

function nonEmpty(value: string, field: string): string {
  const result = value.trim();
  if (!result) throw new TypeError(`altm-memory-ui: ${field} must not be empty`);
  return result;
}

export default { apply, Config, inject };
