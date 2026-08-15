/** Browser plugin for the global ALTM Memory panel. */

import type { Context } from "@deepseek-ai/cordis";
import type {} from "@deepseek-ai/dsh-client-locale/client";
import type {} from "@deepseek-ai/dsh-client-ui-sidebar/client";
import { MemoryUiClient, type MemoryUiPort } from "./api.ts";
import { en, NS, zh } from "./locales.ts";
import { MemoryView, type MemoryViewInjected } from "./MemoryView.tsx";

/** Required Client services for locale and slot composition. */
export const inject = ["slots", "locale"];

/** Register bilingual copy and the capability-driven global Memory entry. */
export function apply(ctx: Context): void {
  ctx.effect(
    () => ctx.locale.register(NS, { zh, en }),
    "altm-memory-ui: dictionaries",
  );
  const t = ctx.locale.bind(NS);
  const client = new MemoryUiClient();
  const api: MemoryUiPort = {
    available: () => client.available(),
    graphSeeds: (sessionId, query) => client.graphSeeds(sessionId, query),
    neighborhood: (sessionId, seedNodeId) =>
      client.neighborhood(sessionId, seedNodeId),
    prefetch: (sessionId, seedNodeIds) =>
      client.prefetch(sessionId, seedNodeIds),
    layers: sessionId => client.layers(sessionId),
    embeddingStatus: () => client.embeddingStatus(),
    configureEmbedding: input => client.configureEmbedding(input),
  };
  ctx.effect(() => {
    let closed = false;
    let syncing = false;
    let disposeView: (() => void) | undefined;
    const sync = async (): Promise<void> => {
      if (closed || syncing) return;
      syncing = true;
      const available = await client.available();
      syncing = false;
      if (closed) return;
      if (available && disposeView === undefined) {
        disposeView = ctx.slots.inject(
          "sidebar.footer.action",
          () => ctx.slots.register({
            name: "sidebar.footer.action",
            id: "altm-memory",
            order: 20,
            locale: NS,
            label: () => t("view.memory"),
            inject: (): MemoryViewInjected => ({
              hooks: { language: ctx.locale },
              api,
              setLanguage: language => { ctx.locale.setLocale(language); },
            }),
          }, MemoryView),
        );
      } else if (!available && disposeView !== undefined) {
        disposeView();
        disposeView = undefined;
      }
    };
    void sync();
    const timer = globalThis.setInterval(() => { void sync(); }, 1500);
    return () => {
      closed = true;
      globalThis.clearInterval(timer);
      disposeView?.();
    };
  }, "altm-memory-ui: capability-driven Memory view");
}
