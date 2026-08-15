"""OpenAI-compatible embeddings client."""

from __future__ import annotations

import json
import os
import tempfile
import time
from collections.abc import Sequence
from contextlib import suppress
from http.client import IncompleteRead
from pathlib import Path
from typing import Any, cast
from urllib import error, request
from urllib.parse import urlparse

from altm.contracts import EmbeddingConfig

_CONFIG_PATH_ENV = "ALTM_EMBEDDING_CONFIG_PATH"
_GROUP_OTHER_BITS = 0o077


class OpenAICompatibleEmbeddingClient:
    def __init__(self, config: EmbeddingConfig) -> None:
        self.config = config

    def embed_text(self, text: str) -> list[float]:
        return self.embed_texts([text])[0]

    def embed_texts(self, texts: Sequence[str]) -> list[list[float]]:
        vectors: list[list[float]] = []
        for start in range(0, len(texts), self.config.batch_size):
            vectors.extend(
                self._embed_batch(texts[start : start + self.config.batch_size])
            )
        return vectors

    def _embed_batch(self, texts: Sequence[str]) -> list[list[float]]:
        for attempt in range(self.config.max_retries + 1):
            try:
                return self._request_batch(texts)
            except error.HTTPError as exc:
                detail = exc.read().decode("utf-8", errors="replace")
                if (
                    attempt >= self.config.max_retries
                    or (exc.code not in {408, 429} and exc.code < 500)
                ):
                    raise RuntimeError(
                        "Embedding HTTP error %s: %s" % (exc.code, detail)
                    ) from exc
            except (
                IncompleteRead,
                TimeoutError,
                ConnectionError,
                error.URLError,
            ) as exc:
                if attempt >= self.config.max_retries:
                    raise RuntimeError("Embedding request failed: %s" % exc) from exc
            time.sleep(self.config.retry_delay_seconds * (2**attempt))
        raise AssertionError("Embedding retry loop exhausted without returning or raising")

    def _request_batch(self, texts: Sequence[str]) -> list[list[float]]:
        url = self.config.base_url.rstrip("/") + "/embeddings"
        payload = {
            "model": self.config.model,
            "input": list(texts),
        }
        req = request.Request(
            url=url,
            data=json.dumps(payload).encode("utf-8"),
            method="POST",
            headers={
                "Authorization": "Bearer %s" % self.config.api_key,
                "Content-Type": "application/json",
            },
        )
        with request.urlopen(req, timeout=self.config.timeout_seconds) as response:
            data: dict[str, Any] = json.loads(response.read().decode("utf-8"))
        items = sorted(data["data"], key=lambda item: item["index"])
        return [[float(value) for value in item["embedding"]] for item in items]


def embedding_config_from_env() -> EmbeddingConfig:
    missing = [
        name
        for name in (
            "ALTM_EMBEDDING_BASE_URL",
            "ALTM_EMBEDDING_API_KEY",
            "ALTM_EMBEDDING_MODEL",
        )
        if not os.environ.get(name)
    ]
    if missing:
        raise RuntimeError("Missing embedding environment variables: %s" % ", ".join(missing))

    return EmbeddingConfig(
        base_url=os.environ["ALTM_EMBEDDING_BASE_URL"],
        api_key=os.environ["ALTM_EMBEDDING_API_KEY"],
        model=os.environ["ALTM_EMBEDDING_MODEL"],
        timeout_seconds=int(os.environ.get("ALTM_EMBEDDING_TIMEOUT_SECONDS", "60")),
        batch_size=int(os.environ.get("ALTM_EMBEDDING_BATCH_SIZE", "10")),
        max_retries=int(os.environ.get("ALTM_EMBEDDING_MAX_RETRIES", "3")),
        retry_delay_seconds=float(
            os.environ.get("ALTM_EMBEDDING_RETRY_DELAY_SECONDS", "0.5")
        ),
    )


def optional_embedding_client_from_env() -> OpenAICompatibleEmbeddingClient | None:
    names = (
        "ALTM_EMBEDDING_BASE_URL",
        "ALTM_EMBEDDING_API_KEY",
        "ALTM_EMBEDDING_MODEL",
    )
    if not all(os.environ.get(name) for name in names):
        return None
    return OpenAICompatibleEmbeddingClient(embedding_config_from_env())


def embedding_config_path(db_path: str | Path) -> Path:
    """Return the private managed configuration path for one ALTM database."""
    configured = os.environ.get(_CONFIG_PATH_ENV, "").strip()
    if configured:
        return Path(configured).expanduser()
    return Path("%s.embedding.json" % Path(db_path))


def embedding_config_from_sources(db_path: str | Path) -> EmbeddingConfig:
    """Resolve managed settings before the process environment."""
    managed = _managed_embedding_config(db_path)
    return managed if managed is not None else embedding_config_from_env()


def optional_embedding_client_from_sources(
    db_path: str | Path,
) -> OpenAICompatibleEmbeddingClient | None:
    """Resolve a managed or environment-backed client when fully configured."""
    managed = _managed_embedding_config(db_path)
    if managed is not None:
        return OpenAICompatibleEmbeddingClient(managed)
    return optional_embedding_client_from_env()


def embedding_config_status(db_path: str | Path) -> dict[str, object]:
    """Return non-secret provider state for configuration surfaces."""
    managed = _managed_embedding_config(db_path)
    if managed is not None:
        return _status(managed, "managed")
    try:
        configured = embedding_config_from_env()
    except RuntimeError:
        return {
            "configured": False,
            "source": None,
            "base_url": "",
            "model": "",
        }
    return _status(configured, "environment")


def embedding_config_candidate(
    db_path: str | Path,
    *,
    base_url: str,
    model: str,
    api_key: str | None,
) -> EmbeddingConfig:
    """Build a validated replacement while retaining an existing write-only key."""
    normalized_base_url = base_url.strip().rstrip("/")
    parsed = urlparse(normalized_base_url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("Embedding base URL must be an absolute HTTP or HTTPS URL")
    normalized_model = model.strip()
    if not normalized_model:
        raise ValueError("Embedding model is required")
    current: EmbeddingConfig | None
    try:
        current = embedding_config_from_sources(db_path)
    except RuntimeError:
        current = None
    normalized_api_key = api_key.strip() if api_key is not None else ""
    if not normalized_api_key and current is not None:
        normalized_api_key = current.api_key
    if not normalized_api_key:
        raise ValueError("Embedding API key is required")
    if "\r" in normalized_api_key or "\n" in normalized_api_key:
        raise ValueError("Embedding API key cannot contain line breaks")
    timeout_seconds = current.timeout_seconds if current is not None else int(
        os.environ.get("ALTM_EMBEDDING_TIMEOUT_SECONDS", "60")
    )
    batch_size = current.batch_size if current is not None else int(
        os.environ.get("ALTM_EMBEDDING_BATCH_SIZE", "10")
    )
    max_retries = current.max_retries if current is not None else int(
        os.environ.get("ALTM_EMBEDDING_MAX_RETRIES", "3")
    )
    retry_delay_seconds = (
        current.retry_delay_seconds
        if current is not None
        else float(os.environ.get("ALTM_EMBEDDING_RETRY_DELAY_SECONDS", "0.5"))
    )
    return EmbeddingConfig(
        base_url=normalized_base_url,
        api_key=normalized_api_key,
        model=normalized_model,
        timeout_seconds=timeout_seconds,
        batch_size=batch_size,
        max_retries=max_retries,
        retry_delay_seconds=retry_delay_seconds,
    )


def save_embedding_config(
    db_path: str | Path,
    config: EmbeddingConfig,
) -> None:
    """Atomically persist one validated configuration with owner-only permissions."""
    path = embedding_config_path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    payload = json.dumps(
        {
            "base_url": config.base_url,
            "api_key": config.api_key,
            "model": config.model,
            "timeout_seconds": config.timeout_seconds,
            "batch_size": config.batch_size,
            "max_retries": config.max_retries,
            "retry_delay_seconds": config.retry_delay_seconds,
        },
        ensure_ascii=False,
        sort_keys=True,
    )
    descriptor, temporary = tempfile.mkstemp(
        dir=path.parent,
        prefix="%s." % path.name,
        suffix=".tmp",
        text=True,
    )
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(payload)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        path.chmod(0o600)
    except BaseException:
        with suppress(OSError):
            os.close(descriptor)
        with suppress(FileNotFoundError):
            Path(temporary).unlink()
        raise


def _managed_embedding_config(db_path: str | Path) -> EmbeddingConfig | None:
    path = embedding_config_path(db_path)
    try:
        stat = path.stat()
    except FileNotFoundError:
        return None
    if os.name != "nt" and stat.st_mode & _GROUP_OTHER_BITS:
        raise PermissionError(
            "Embedding configuration is readable beyond its owner: %s" % path
        )
    try:
        value: object = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError("Invalid managed embedding configuration: %s" % path) from exc
    if not isinstance(value, dict):
        raise RuntimeError("Managed embedding configuration must be a JSON object")
    payload = cast(dict[str, object], value)
    try:
        base_url = payload["base_url"]
        api_key = payload["api_key"]
        model = payload["model"]
        timeout_seconds = payload.get("timeout_seconds", 60)
        batch_size = payload.get("batch_size", 10)
        max_retries = payload.get("max_retries", 3)
        retry_delay_seconds = payload.get("retry_delay_seconds", 0.5)
    except KeyError as exc:
        raise RuntimeError("Managed embedding configuration is incomplete") from exc
    if (
        not isinstance(base_url, str)
        or not isinstance(api_key, str)
        or not isinstance(model, str)
        or not isinstance(timeout_seconds, int)
        or not isinstance(batch_size, int)
        or not isinstance(max_retries, int)
        or not isinstance(retry_delay_seconds, int | float)
    ):
        raise RuntimeError("Managed embedding configuration has invalid field types")
    return EmbeddingConfig(
        base_url=base_url,
        api_key=api_key,
        model=model,
        timeout_seconds=timeout_seconds,
        batch_size=batch_size,
        max_retries=max_retries,
        retry_delay_seconds=float(retry_delay_seconds),
    )


def _status(config: EmbeddingConfig, source: str) -> dict[str, object]:
    return {
        "configured": True,
        "source": source,
        "base_url": config.base_url,
        "model": config.model,
    }
