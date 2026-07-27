"""Simple context assembly before agent injection."""

from __future__ import annotations

from typing import Sequence

from altm.contracts import (
    ContextBand,
    ContextBundle,
    ContextItem,
    MemoryLayer,
    RecallCandidate,
)
from altm.context.token_budget import ContextBudgeter


class SimpleContextGateway:
    def __init__(self, budgeter: ContextBudgeter | None = None) -> None:
        self.budgeter = budgeter or ContextBudgeter.from_env()

    def assemble(
        self,
        candidates: Sequence[RecallCandidate],
        token_budget: int,
    ) -> ContextBundle:
        remaining_tokens = max(0, token_budget)
        items: list[ContextItem] = []
        for index, candidate in enumerate(candidates):
            if remaining_tokens <= 0:
                break
            content = _candidate_content(candidate)
            if not content:
                continue
            budgeted = self.budgeter.clip(content, remaining_tokens)
            if not budgeted.rendered:
                break
            remaining_tokens -= budgeted.consumed_tokens
            items.append(
                ContextItem(
                    band=_band_for(index, candidate),
                    content=budgeted.rendered,
                    source_memory_ids=[candidate.memory.id],
                    retrieval_marker="memory://%s" % candidate.memory.id,
                    metadata={
                        "layer": candidate.memory.layer.value,
                        "matched_by": candidate.matched_by,
                        "retrieval_score": candidate.score.retrieval_score,
                        "resident_score": candidate.score.resident_score,
                        "truncated": budgeted.truncated,
                        "token_count_estimate": budgeted.consumed_tokens,
                    },
                )
            )

        return ContextBundle(
            items=items,
            token_budget=token_budget,
            metadata={
                "budget_strategy": self.budgeter.strategy,
                "budget_degraded": self.budgeter.degraded,
                "budget_degraded_reason": self.budgeter.degraded_reason,
                "candidate_count": len(candidates),
                "included_count": len(items),
                "remaining_token_budget": remaining_tokens,
            },
        )


def _candidate_content(candidate: RecallCandidate) -> str:
    memory = candidate.memory
    heading = "[%s] %s" % (memory.layer.value, memory.summary or memory.id)
    body = memory.summary or memory.content
    if memory.summary and memory.content != memory.summary:
        body = "%s\n%s" % (memory.summary, memory.content)
    return "%s\n%s" % (heading, body)


def _band_for(index: int, candidate: RecallCandidate) -> ContextBand:
    score = candidate.score.retrieval_score or 0.0
    if index == 0 or score >= 0.85:
        return ContextBand.IMMEDIATE
    if candidate.memory.layer in {MemoryLayer.L1, MemoryLayer.L2, MemoryLayer.L3} or score >= 0.45:
        return ContextBand.WORKING
    return ContextBand.BACKGROUND
