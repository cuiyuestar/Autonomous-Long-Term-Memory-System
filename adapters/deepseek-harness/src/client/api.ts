/** Same-origin ALTM UI API with neighborhood prefetch caching. */

import {
  ALTM_UI_API_PATH,
  type UiEmbeddingConfigInput,
  type UiEmbeddingStatus,
  type UiGraphNeighborhood,
  type UiGraphNode,
  type UiMemoryLayers,
} from "../ui-contract.ts";

const NEIGHBORHOOD_CACHE_LIMIT = 24;

/** Plain callback face injected into the Memory view. */
export interface MemoryUiPort {
  available(): Promise<boolean>;
  graphSeeds(sessionId: string, query?: string): Promise<UiGraphNode[]>;
  neighborhood(sessionId: string, seedNodeId: string): Promise<UiGraphNeighborhood>;
  prefetch(sessionId: string, seedNodeIds: readonly string[]): void;
  layers(sessionId: string): Promise<UiMemoryLayers>;
  embeddingStatus(): Promise<UiEmbeddingStatus>;
  configureEmbedding(input: UiEmbeddingConfigInput): Promise<UiEmbeddingStatus>;
}

/** Browser-side read client. No MCP credential crosses this interface. */
export class MemoryUiClient implements MemoryUiPort {
  private readonly neighborhoods = new Map<string, Promise<UiGraphNeighborhood>>();

  async available(): Promise<boolean> {
    try {
      const result = await request<{ available: boolean }>(
        `${ALTM_UI_API_PATH}/health`,
      );
      return result.available === true;
    } catch {
      return false;
    }
  }

  async graphSeeds(sessionId: string, query = ""): Promise<UiGraphNode[]> {
    const params = new URLSearchParams({ sessionId });
    if (query.trim()) params.set("query", query.trim());
    return request<UiGraphNode[]>(
      `${ALTM_UI_API_PATH}/graph/seeds?${params.toString()}`,
    );
  }

  neighborhood(
    sessionId: string,
    seedNodeId: string,
  ): Promise<UiGraphNeighborhood> {
    const key = `${sessionId}\u0000${seedNodeId}`;
    let pending = this.neighborhoods.get(key);
    if (pending !== undefined) {
      this.neighborhoods.delete(key);
      this.neighborhoods.set(key, pending);
      return pending;
    }
    pending = request<UiGraphNeighborhood>(
      `${ALTM_UI_API_PATH}/graph/neighborhood?sessionId=${encodeURIComponent(sessionId)}`,
      {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ seedNodeIds: [seedNodeId] }),
      },
    ).catch((error: unknown) => {
      this.neighborhoods.delete(key);
      throw error;
    });
    this.neighborhoods.set(key, pending);
    while (this.neighborhoods.size > NEIGHBORHOOD_CACHE_LIMIT) {
      const oldest = this.neighborhoods.keys().next().value as string | undefined;
      if (oldest === undefined) break;
      this.neighborhoods.delete(oldest);
    }
    return pending;
  }

  prefetch(sessionId: string, seedNodeIds: readonly string[]): void {
    for (const id of seedNodeIds.slice(0, 4)) {
      void this.neighborhood(sessionId, id).catch(() => undefined);
    }
  }

  layers(sessionId: string): Promise<UiMemoryLayers> {
    const params = new URLSearchParams({ sessionId });
    return request<UiMemoryLayers>(
      `${ALTM_UI_API_PATH}/layers?${params.toString()}`,
    );
  }

  embeddingStatus(): Promise<UiEmbeddingStatus> {
    return request<UiEmbeddingStatus>(`${ALTM_UI_API_PATH}/embedding`);
  }

  configureEmbedding(
    input: UiEmbeddingConfigInput,
  ): Promise<UiEmbeddingStatus> {
    return request<UiEmbeddingStatus>(`${ALTM_UI_API_PATH}/embedding`, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify(input),
    });
  }
}

async function request<T>(url: string, init?: RequestInit): Promise<T> {
  const response = await fetch(url, {
    ...init,
    headers: {
      accept: "application/json",
      ...init?.headers,
    },
  });
  const payload: unknown = await response.json().catch(() => null);
  if (!response.ok) {
    const message = payload !== null
      && typeof payload === "object"
      && !Array.isArray(payload)
      && typeof (payload as Record<string, unknown>).error === "string"
      ? String((payload as Record<string, unknown>).error)
      : `HTTP ${response.status}`;
    throw new Error(message);
  }
  return payload as T;
}
