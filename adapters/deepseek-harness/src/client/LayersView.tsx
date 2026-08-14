/** Compact L1-L4 browser with one visible abstraction level at a time. */

import { useCallback, useEffect, useMemo, useState } from "react";
import { Button } from "@deepseek-ai/dsh-client-ui-primitives";
import type { TranslateNS } from "@deepseek-ai/dsh-client-locale/client";
import type {
  UiMemoryLayer,
  UiMemoryLayers,
  UiMemoryUnit,
} from "../ui-contract.ts";
import type { MemoryUiPort } from "./api.ts";
import css from "./LayersView.module.css";

interface LayersViewProps {
  sessionId: string;
  client: MemoryUiPort;
  language: "zh" | "en";
  t: TranslateNS<"altm.memory">;
}

const LEVELS: readonly UiMemoryLayer[] = ["L4", "L3", "L2", "L1"];
const PAGE_SIZE = 20;

/** L1-L4 abstraction ladder, list, and selected-memory inspector. */
export function LayersView({
  sessionId,
  client,
  language,
  t,
}: LayersViewProps) {
  const [data, setData] = useState<UiMemoryLayers | null>(null);
  const [level, setLevel] = useState<UiMemoryLayer>("L2");
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [visibleCount, setVisibleCount] = useState(PAGE_SIZE);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const next = await client.layers(sessionId);
      setData(next);
    } catch (reason: unknown) {
      setError(reason instanceof Error ? reason.message : String(reason));
    } finally {
      setLoading(false);
    }
  }, [client, sessionId]);

  useEffect(() => {
    void load();
  }, [load]);

  const memories = data?.layers[level] ?? [];
  const selected = useMemo(
    () => memories.find(memory => memory.id === selectedId) ?? memories[0] ?? null,
    [memories, selectedId],
  );

  return (
    <div className={css.root}>
      <nav className={css.levels} aria-label={t("mode.layers")}>
        {LEVELS.map(candidate => (
          <button
            key={candidate}
            type="button"
            className={css.level}
            data-active={candidate === level || undefined}
            onClick={() => {
              setLevel(candidate);
              setSelectedId(null);
              setVisibleCount(PAGE_SIZE);
            }}
          >
            <span className={css.levelCode}>{candidate}</span>
            <span className={css.levelName}>{t(`layers.${candidate}`)}</span>
            <span className={css.levelCount}>{data?.counts[candidate] ?? 0}</span>
          </button>
        ))}
      </nav>

      <section className={css.list} aria-busy={loading}>
        <header className={css.listHeader}>
          <h2>{level} · {t(`layers.${level}`)}</h2>
          <span>{data?.counts[level] ?? 0}</span>
        </header>
        {loading && <p className={css.state}>{t("layers.loading")}</p>}
        {error !== null && (
          <div className={css.state}>
            <p>{t("layers.error")}: {error}</p>
            <Button variant="ghost" size="sm" onClick={() => { void load(); }}>
              {t("common.retry")}
            </Button>
          </div>
        )}
        {!loading && error === null && memories.length === 0 && (
          <p className={css.state}>{t("layers.empty")}</p>
        )}
        <div className={css.rows}>
          {memories.slice(0, visibleCount).map(memory => (
            <button
              key={memory.id}
              type="button"
              className={css.row}
              data-selected={memory.id === selected?.id || undefined}
              onClick={() => { setSelectedId(memory.id); }}
            >
              <span className={css.rowTitle}>
                {memory.summary?.trim() || excerpt(memory.content, 88)}
              </span>
              <span className={css.rowMeta}>
                {memory.status} · {t("layers.updated", {
                  time: formatDate(memory.updated_at, language),
                })}
              </span>
            </button>
          ))}
          {visibleCount < memories.length && (
            <div className={css.more}>
              <Button
                variant="ghost"
                size="sm"
                onClick={() => { setVisibleCount(count => count + PAGE_SIZE); }}
              >
                {t("layers.more")}
              </Button>
            </div>
          )}
        </div>
      </section>

      <aside className={css.details}>
        {selected === null
          ? <p className={css.state}>{t("layers.empty")}</p>
          : <MemoryDetails memory={selected} language={language} t={t} />}
      </aside>
    </div>
  );
}

function MemoryDetails({
  memory,
  language,
  t,
}: {
  memory: UiMemoryUnit;
  language: "zh" | "en";
  t: TranslateNS<"altm.memory">;
}) {
  const confidence = confidenceOf(memory);
  return (
    <div className={css.detailBody}>
      <div className={css.detailHeading}>
        <span>{memory.layer} · {memory.status}</span>
        <h3>{memory.summary?.trim() || excerpt(memory.content, 120)}</h3>
      </div>
      <dl className={css.metrics}>
        <dt>{t("layers.lifecycle")}</dt>
        <dd>{memory.lifecycle_state}</dd>
        <dt>{t("layers.confidence")}</dt>
        <dd>{confidence === null ? "—" : `${Math.round(confidence * 100)}%`}</dd>
        <dt>{t("layers.access")}</dt>
        <dd>{memory.useful_access_count} / {memory.access_count}</dd>
        <dt>{t("layers.evidence")}</dt>
        <dd>{memory.evidence_refs.length}</dd>
      </dl>
      <section className={css.content}>
        <h4>{t("layers.source")}</h4>
        <p>{memory.content}</p>
      </section>
      {memory.evidence_refs.length > 0 && (
        <section className={css.evidence}>
          <h4>{t("layers.evidence")}</h4>
          <ul>
            {memory.evidence_refs.slice(0, 12).map(reference => (
              <li key={`${reference.target_id}:${reference.relation}`}>
                <span>{reference.target_layer}</span>
                <code>{reference.target_id}</code>
                <small>{reference.relation} · {Math.round(reference.confidence * 100)}%</small>
              </li>
            ))}
          </ul>
        </section>
      )}
      <time className={css.timestamp}>
        {formatDate(memory.updated_at, language)}
      </time>
    </div>
  );
}

function confidenceOf(memory: UiMemoryUnit): number | null {
  const candidates = [
    memory.metadata.confidence,
    memory.score.retrieval_score,
    memory.score.evidence_quality,
  ];
  return candidates.find(
    (value): value is number => typeof value === "number" && Number.isFinite(value),
  ) ?? null;
}

function excerpt(value: string, limit: number): string {
  const compact = value.replace(/\s+/g, " ").trim();
  return compact.length <= limit ? compact : `${compact.slice(0, limit - 1)}…`;
}

function formatDate(value: string, language: "zh" | "en"): string {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return new Intl.DateTimeFormat(language === "zh" ? "zh-CN" : "en-US", {
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  }).format(date);
}
