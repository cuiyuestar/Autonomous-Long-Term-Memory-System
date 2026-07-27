"""OpenAI-compatible chat completions client using the standard library."""

from __future__ import annotations

import json
import os
from typing import Any, Dict, List
from urllib import error, request

from altm.contracts import LLMConfig


class OpenAICompatibleClient:
    def __init__(self, config: LLMConfig) -> None:
        self.config = config

    def chat_json(self, messages: List[Dict[str, str]]) -> Dict[str, Any]:
        url = self.config.base_url.rstrip("/") + "/chat/completions"
        payload = {
            "model": self.config.model,
            "messages": messages,
            "temperature": 0,
            "response_format": {"type": "json_object"},
        }
        body = json.dumps(payload).encode("utf-8")
        req = request.Request(
            url=url,
            data=body,
            method="POST",
            headers={
                "Authorization": "Bearer %s" % self.config.api_key,
                "Content-Type": "application/json",
            },
        )

        try:
            with request.urlopen(req, timeout=self.config.timeout_seconds) as response:
                response_body = response.read().decode("utf-8")
        except error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError("LLM HTTP error %s: %s" % (exc.code, detail)) from exc
        except error.URLError as exc:
            raise RuntimeError("LLM request failed: %s" % exc) from exc

        data = json.loads(response_body)
        content = data["choices"][0]["message"]["content"]
        return json.loads(content)


def llm_config_from_env() -> LLMConfig:
    missing = [
        name
        for name in ("ALTM_LLM_BASE_URL", "ALTM_LLM_API_KEY", "ALTM_LLM_MODEL")
        if not os.environ.get(name)
    ]
    if missing:
        raise RuntimeError("Missing LLM environment variables: %s" % ", ".join(missing))

    return LLMConfig(
        base_url=os.environ["ALTM_LLM_BASE_URL"],
        api_key=os.environ["ALTM_LLM_API_KEY"],
        model=os.environ["ALTM_LLM_MODEL"],
        timeout_seconds=int(os.environ.get("ALTM_LLM_TIMEOUT_SECONDS", "60")),
    )
