/** Write-only OpenAI-compatible embedding provider configuration. */

import { useCallback, useEffect, useState } from "react";
import type { FormEvent } from "react";
import type { TranslateNS } from "@deepseek-ai/dsh-client-locale/client";
import type { UiEmbeddingStatus } from "../ui-contract.ts";
import type { MemoryUiPort } from "./api.ts";
import css from "./EmbeddingView.module.css";

interface EmbeddingViewProps {
  client: MemoryUiPort;
  t: TranslateNS<"altm.memory">;
  onConfigured(status: UiEmbeddingStatus): void;
}

/** Provider form whose secret field is never populated from the server. */
export function EmbeddingView({
  client,
  t,
  onConfigured,
}: EmbeddingViewProps) {
  const [status, setStatus] = useState<UiEmbeddingStatus | null>(null);
  const [baseUrl, setBaseUrl] = useState("");
  const [model, setModel] = useState("");
  const [apiKey, setApiKey] = useState("");
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [saved, setSaved] = useState(false);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const next = await client.embeddingStatus();
      setStatus(next);
      setBaseUrl(next.base_url);
      setModel(next.model);
    } catch {
      setError(t("embedding.loadError"));
    } finally {
      setLoading(false);
    }
  }, [client, t]);

  useEffect(() => {
    void load();
  }, [load]);

  const submit = async (event: FormEvent) => {
    event.preventDefault();
    if (saving) return;
    if (!status?.configured && !apiKey.trim()) {
      setError(t("embedding.apiKeyRequired"));
      return;
    }
    setSaving(true);
    setSaved(false);
    setError(null);
    try {
      const next = await client.configureEmbedding({
        baseUrl: baseUrl.trim(),
        model: model.trim(),
        ...(apiKey.trim() ? { apiKey: apiKey.trim() } : {}),
      });
      setStatus(next);
      setApiKey("");
      setSaved(true);
      onConfigured(next);
    } catch {
      setError(t("embedding.saveError"));
    } finally {
      setSaving(false);
    }
  };

  return (
    <div className={css.root}>
      <div className={css.heading}>
        <div>
          <h2>{t("embedding.title")}</h2>
          <p>{t("embedding.description")}</p>
        </div>
        <span
          className={css.status}
          data-configured={status?.configured || undefined}
        >
          <span aria-hidden />
          {status?.configured
            ? t("embedding.configured")
            : t("embedding.notConfigured")}
        </span>
      </div>

      <form className={css.form} onSubmit={(event) => { void submit(event); }}>
        <label>
          <span>{t("embedding.baseUrl")}</span>
          <input
            type="url"
            value={baseUrl}
            placeholder="https://example.com/v1"
            required
            disabled={loading || saving}
            onChange={(event) => { setBaseUrl(event.target.value); }}
          />
        </label>
        <label>
          <span>{t("embedding.model")}</span>
          <input
            type="text"
            value={model}
            placeholder="text-embedding-v4"
            required
            disabled={loading || saving}
            onChange={(event) => { setModel(event.target.value); }}
          />
        </label>
        <label>
          <span>{t("embedding.apiKey")}</span>
          <input
            type="password"
            value={apiKey}
            placeholder={status?.configured
              ? t("embedding.apiKeyConfigured")
              : t("embedding.apiKeyPlaceholder")}
            autoComplete="off"
            disabled={loading || saving}
            onChange={(event) => { setApiKey(event.target.value); }}
          />
        </label>
        <p className={css.security}>{t("embedding.security")}</p>
        {error !== null && <p className={css.error} role="alert">{error}</p>}
        {saved && <p className={css.success} role="status">{t("embedding.saved")}</p>}
        <button
          type="submit"
          className={css.submit}
          disabled={loading || saving}
        >
          {saving ? t("embedding.saving") : t("embedding.save")}
        </button>
      </form>
    </div>
  );
}
