"""Simple context assembly before agent injection."""

from __future__ import annotations

from collections.abc import Sequence

from altm.context.headroom import ContentRouter
from altm.context.token_budget import ContextBudgeter
from altm.contracts import (
    ContextBand,
    ContextBundle,
    ContextItem,
    MemoryLayer,
    RecallCandidate,
)
from altm.storage import SQLiteMemoryStore


class SimpleContextGateway:
    def __init__(
        self,
        budgeter: ContextBudgeter | None = None,
        store: SQLiteMemoryStore | None = None,
    ) -> None:
        self.budgeter = budgeter or ContextBudgeter.from_env()
        self.router = ContentRouter(store) if store is not None else None

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
            routed_metadata: dict[str, object] = {}
            retrieval_marker = "memory://%s" % candidate.memory.id
            if self.router is not None:
                target_tokens = max(
                    24,
                    min(remaining_tokens, max(64, token_budget // max(1, len(candidates)))),
                )
                routed = self.router.compress(candidate.memory, target_tokens)
                content = _candidate_content(
                    candidate,
                    routed.rendered,
                    marker_only=not routed.rendered,
                )
                retrieval_marker = routed.marker
                routed_metadata = {
                    "content_type": routed.content_type,
                    "compression_strategy": routed.strategy,
                    "compressed": routed.compressed,
                    "original_token_estimate": routed.original_token_estimate,
                    "compressed_token_estimate": routed.compressed_token_estimate,
                }
            else:
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
                    retrieval_marker=retrieval_marker,
                    metadata={
                        "layer": candidate.memory.layer.value,
                        "matched_by": candidate.matched_by,
                        "retrieval_score": candidate.score.retrieval_score,
                        "resident_score": candidate.score.resident_score,
                        "truncated": budgeted.truncated,
                        "token_count_estimate": budgeted.consumed_tokens,
                        **routed_metadata,
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


def _candidate_content(
    candidate: RecallCandidate,
    rendered_body: str | None = None,
    marker_only: bool = False,
) -> str:
    memory = candidate.memory
    heading = "[%s] %s" % (memory.layer.value, memory.summary or memory.id)
    if marker_only:
        return "%s\n[full content available through retrieval marker]" % heading
    if rendered_body is not None:
        return "%s\n%s" % (heading, rendered_body)
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
