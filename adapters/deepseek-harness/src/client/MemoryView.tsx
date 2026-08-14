/** ALTM session view containing only Graph and L1-L4 layers. */

import { useState } from "react";
import type { ConvViewProps } from "@deepseek-ai/dsh-client-ui-conversation/client";
import type {
  HostObservable,
  InjectFace,
  PropsLocale,
} from "@deepseek-ai/dsh-client-ui-slots";
import type { MemoryUiPort } from "./api.ts";
import { GraphView } from "./GraphView.tsx";
import { LayersView } from "./LayersView.tsx";
import css from "./MemoryView.module.css";

type Language = "zh" | "en";

interface LanguageSnapshot {
  active: string;
}

/** Browser callbacks and locale state supplied by the plugin body. */
export interface MemoryViewInjected {
  hooks: {
    language: HostObservable<LanguageSnapshot>;
  };
  api: MemoryUiPort;
  setLanguage(language: Language): void;
}

/** Full-height Memory tab. */
export function MemoryView({
  sessionId,
  useLanguage,
  api,
  setLanguage,
  t,
}: ConvViewProps & InjectFace<MemoryViewInjected> & PropsLocale<"altm.memory">) {
  const [mode, setMode] = useState<"graph" | "layers">("graph");
  const active = useLanguage(snapshot => snapshot.active);
  const language: Language = active === "en" ? "en" : "zh";

  return (
    <div className={css.root}>
      <header className={css.header}>
        <div className={css.segmented} role="tablist" aria-label={t("view.memory")}>
          <button
            type="button"
            role="tab"
            aria-selected={mode === "graph"}
            data-active={mode === "graph" || undefined}
            onClick={() => { setMode("graph"); }}
          >
            {t("mode.graph")}
          </button>
          <button
            type="button"
            role="tab"
            aria-selected={mode === "layers"}
            data-active={mode === "layers" || undefined}
            onClick={() => { setMode("layers"); }}
          >
            {t("mode.layers")}
          </button>
        </div>
        <div className={css.language} role="group" aria-label="Language">
          <button
            type="button"
            data-active={language === "zh" || undefined}
            aria-pressed={language === "zh"}
            title={t("language.zh")}
            onClick={() => { setLanguage("zh"); }}
          >
            中
          </button>
          <span aria-hidden />
          <button
            type="button"
            data-active={language === "en" || undefined}
            aria-pressed={language === "en"}
            title={t("language.en")}
            onClick={() => { setLanguage("en"); }}
          >
            EN
          </button>
        </div>
      </header>
      <main className={css.content}>
        {mode === "graph"
          ? <GraphView sessionId={String(sessionId)} client={api} t={t} />
          : (
            <LayersView
              sessionId={String(sessionId)}
              client={api}
              language={language}
              t={t}
            />
          )}
      </main>
    </div>
  );
}
