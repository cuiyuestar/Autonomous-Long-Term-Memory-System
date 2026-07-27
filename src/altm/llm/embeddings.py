"""OpenAI-compatible embeddings client."""

from __future__ import annotations

import json
import os
from typing import Any, Sequence
from urllib import error, request

from altm.contracts import EmbeddingConfig


class OpenAICompatibleEmbeddingClient:
    def __init__(self, config: EmbeddingConfig) -> None:
        self.config = config

    def embed_text(self, text: str) -> list[float]:
        return self.embed_texts([text])[0]

    def embed_texts(self, texts: Sequence[str]) -> list[list[float]]:
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
        try:
            with request.urlopen(req, timeout=self.config.timeout_seconds) as response:
                data: dict[str, Any] = json.loads(response.read().decode("utf-8"))
        except error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError("Embedding HTTP error %s: %s" % (exc.code, detail)) from exc
        except error.URLError as exc:
            raise RuntimeError("Embedding request failed: %s" % exc) from exc

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
