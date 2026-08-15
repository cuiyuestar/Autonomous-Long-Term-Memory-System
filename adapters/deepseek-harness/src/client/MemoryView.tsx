/** Global ALTM Memory entry, panel, and first-use embedding guidance. */

import { useCallback, useEffect, useState } from "react";
import {
  IconCloseOutline16,
  IconDataOutline16,
  Modal,
} from "@deepseek-ai/dsh-client-ui-primitives";
import type {
  HostObservable,
  InjectFace,
  PropsLocale,
  PropsRuntime,
} from "@deepseek-ai/dsh-client-ui-slots";
import type {} from "@deepseek-ai/dsh-client-ui-sidebar/client";
import type { UiEmbeddingStatus } from "../ui-contract.ts";
import type { MemoryUiPort } from "./api.ts";
import { EmbeddingView } from "./EmbeddingView.tsx";
import { GraphView } from "./GraphView.tsx";
import { LayersView } from "./LayersView.tsx";
import css from "./MemoryView.module.css";

type Language = "zh" | "en";
type Mode = "graph" | "layers" | "embedding";
const ONBOARDING_KEY = "altm-memory:embedding-onboarding:v1";

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

type MemoryViewProps =
  PropsRuntime<"sidebar.footer.action">
  & InjectFace<MemoryViewInjected>
  & PropsLocale<"altm.memory">;

/** Sidebar entry and full-viewport Memory panel. */
export function MemoryView({
  wide,
  useSessions,
  useLanguage,
  api,
  setLanguage,
  t,
}: MemoryViewProps) {
  const sessions = useSessions(snapshot => snapshot);
  const sessionId = sessions.current ?? sessions.ids[0];
  const session = sessionId === undefined ? undefined : sessions.byId[sessionId];
  const usingRecent = sessions.current === undefined && session !== undefined;
  const [open, setOpen] = useState(false);
  const [mode, setMode] = useState<Mode>("graph");
  const [showOnboarding, setShowOnboarding] = useState(false);
  const active = useLanguage(snapshot => snapshot.active);
  const language: Language = active === "en" ? "en" : "zh";

  useEffect(() => {
    let current = true;
    void api.embeddingStatus().then((status) => {
      if (
        current
        && !status.configured
        && !onboardingSeen()
      ) {
        setShowOnboarding(true);
      }
    }).catch(() => undefined);
    return () => { current = false; };
  }, [api]);

  const dismissOnboarding = useCallback(() => {
    markOnboardingSeen();
    setShowOnboarding(false);
  }, []);
  const configureFromOnboarding = useCallback(() => {
    markOnboardingSeen();
    setShowOnboarding(false);
    setMode("embedding");
    setOpen(true);
  }, []);
  const configured = useCallback((_status: UiEmbeddingStatus) => {
    markOnboardingSeen();
    setShowOnboarding(false);
  }, []);

  return (
    <>
      <button
        type="button"
        className={css.trigger}
        data-rail={!wide || undefined}
        data-active={open || undefined}
        aria-label={t("view.memory")}
        aria-haspopup="dialog"
        aria-expanded={open}
        onClick={() => { setOpen(true); }}
      >
        <IconDataOutline16 size={wide ? 16 : 18} />
        {wide && <span>{t("view.memory")}</span>}
      </button>

      <Modal
        open={open}
        onClose={() => { setOpen(false); }}
        title={t("view.memory")}
        className={css.panel}
        headless
      >
        <div className={css.root}>
          <header className={css.header}>
            <div className={css.identity}>
              <strong>{t("view.memory")}</strong>
              <span>
                {session === undefined
                  ? t("scope.none")
                  : `${usingRecent ? t("scope.recent") : t("scope.current")}: ${session.displayTitle}`}
              </span>
            </div>
            <div className={css.headerActions}>
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
              <button
                type="button"
                className={css.close}
                aria-label={t("common.close")}
                onClick={() => { setOpen(false); }}
              >
                <IconCloseOutline16 size={14} />
              </button>
            </div>
          </header>
          <div
            className={css.segmented}
            role="tablist"
            aria-label={t("view.memory")}
          >
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
            <button
              type="button"
              role="tab"
              aria-selected={mode === "embedding"}
              data-active={mode === "embedding" || undefined}
              onClick={() => { setMode("embedding"); }}
            >
              {t("mode.embedding")}
            </button>
          </div>
          <main className={css.content}>
            {mode === "embedding"
              ? <EmbeddingView client={api} t={t} onConfigured={configured} />
              : sessionId === undefined
                ? <p className={css.noSession}>{t("scope.required")}</p>
                : mode === "graph"
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
      </Modal>

      <Modal
        open={showOnboarding && !open}
        onClose={dismissOnboarding}
        title={t("onboarding.title")}
        closeLabel={t("common.close")}
        description={t("embedding.description")}
        className={css.onboarding}
        footer={(
          <button
            type="button"
            className={css.configure}
            onClick={configureFromOnboarding}
          >
            {t("onboarding.configure")}
          </button>
        )}
      />
    </>
  );
}

function onboardingSeen(): boolean {
  try {
    return globalThis.localStorage.getItem(ONBOARDING_KEY) === "seen";
  } catch {
    return false;
  }
}

function markOnboardingSeen(): void {
  try {
    globalThis.localStorage.setItem(ONBOARDING_KEY, "seen");
  } catch {
    // Storage can be unavailable in a private or policy-restricted browser.
  }
}
