"""Context Gateway 的可选 token 预算控制。"""

from __future__ import annotations

from dataclasses import dataclass
from math import ceil
import os
from typing import Callable

from altm.config import high_risk_flags


TokenCounter = Callable[[str], int]


@dataclass(frozen=True)
class BudgetedText:
    rendered: str
    consumed_tokens: int
    truncated: bool


class ContextBudgeter:
    def __init__(
        self,
        token_counter: TokenCounter | None = None,
        strategy: str = "char_estimate_4_chars_per_token",
        degraded: bool = False,
        degraded_reason: str | None = None,
    ) -> None:
        self.token_counter = token_counter
        self.strategy = strategy
        self.degraded = degraded
        self.degraded_reason = degraded_reason

    @classmethod
    def from_env(cls) -> "ContextBudgeter":
        flags = high_risk_flags()
        if not flags.enable_optional_context_tokenizer:
            return cls(degraded=True, degraded_reason="disabled_by_flag")

        tokenizer = os.environ.get("ALTM_CONTEXT_TOKENIZER", "").strip().lower()
        if tokenizer in {"", "char", "off", "false"}:
            return cls(degraded=True, degraded_reason="not_configured")
        if tokenizer != "tiktoken":
            return cls(degraded=True, degraded_reason="unsupported_tokenizer:%s" % tokenizer)

        counter = _tiktoken_counter(
            os.environ.get("ALTM_CONTEXT_TOKENIZER_MODEL")
            or os.environ.get("ALTM_LLM_MODEL")
        )
        if counter is None:
            return cls(degraded=True, degraded_reason="tiktoken_unavailable")
        return cls(token_counter=counter, strategy="optional_tokenizer:tiktoken")

    def clip(self, text: str, token_budget: int) -> BudgetedText:
        budget = max(0, token_budget)
        if budget <= 0 or not text:
            return BudgetedText("", 0, bool(text))
        if self.token_counter is None:
            return self._clip_by_char_estimate(text, budget)
        return self._clip_by_token_counter(text, budget)

    def _clip_by_char_estimate(self, text: str, token_budget: int) -> BudgetedText:
        char_budget = token_budget * 4
        rendered = text[:char_budget]
        return BudgetedText(
            rendered=rendered,
            consumed_tokens=max(1, ceil(len(rendered) / 4)) if rendered else 0,
            truncated=len(rendered) < len(text),
        )

    def _clip_by_token_counter(self, text: str, token_budget: int) -> BudgetedText:
        total_tokens = self.token_counter(text)
        if total_tokens <= token_budget:
            return BudgetedText(text, max(1, total_tokens), truncated=False)

        low = 0
        high = len(text)
        best = ""
        while low <= high:
            mid = (low + high) // 2
            candidate = text[:mid]
            candidate_tokens = self.token_counter(candidate)
            if candidate_tokens <= token_budget:
                best = candidate
                low = mid + 1
            else:
                high = mid - 1

        consumed = self.token_counter(best) if best else 0
        return BudgetedText(best, consumed, truncated=True)


def _tiktoken_counter(model: str | None) -> TokenCounter | None:
    try:
        import tiktoken  # type: ignore[import-not-found]
    except ModuleNotFoundError:
        return None

    try:
        encoding = tiktoken.encoding_for_model(model) if model else tiktoken.get_encoding("cl100k_base")
    except KeyError:
        encoding = tiktoken.get_encoding("cl100k_base")
    return lambda text: len(encoding.encode(text))
