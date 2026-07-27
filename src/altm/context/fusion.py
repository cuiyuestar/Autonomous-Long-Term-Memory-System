"""Explicit fusion of query recall and global active window candidates."""

from __future__ import annotations

from collections import OrderedDict
from typing import Sequence

from altm.context.gateway import SimpleContextGateway
from altm.contracts import (
    ContextBundle,
    ContextFusionComparisonReport,
    ContextFusionDecision,
    ContextFusionReport,
    RecallCandidate,
    ScoreBreakdown,
)


class SimpleContextFusion:
    """Merge query recall with proactive active-window candidates for explicit preview."""

    def __init__(self, gateway: SimpleContextGateway | None = None) -> None:
        self.gateway = gateway or SimpleContextGateway()

    def merge(
        self,
        recall_candidates: Sequence[RecallCandidate],
        active_candidates: Sequence[RecallCandidate],
        candidate_limit: int | None = None,
    ) -> Sequence[RecallCandidate]:
        merged: OrderedDict[str, RecallCandidate] = OrderedDict()
        for candidate in recall_candidates:
            merged[candidate.memory.id] = _tag_candidate(candidate, "query_recall")
        for candidate in active_candidates:
            existing = merged.get(candidate.memory.id)
            if existing is None:
                merged[candidate.memory.id] = _tag_candidate(candidate, "active_window")
                continue
            merged[candidate.memory.id] = _merge_duplicate(existing, candidate)

        candidates = list(merged.values())
        if candidate_limit is None:
            return candidates
        return candidates[: max(0, candidate_limit)]

    def assemble(
        self,
        recall_candidates: Sequence[RecallCandidate],
        active_candidates: Sequence[RecallCandidate],
        token_budget: int,
        candidate_limit: int | None = None,
    ) -> ContextBundle:
        return self.report(
            recall_candidates=recall_candidates,
            active_candidates=active_candidates,
            token_budget=token_budget,
            candidate_limit=candidate_limit,
        ).bundle

    def report(
        self,
        recall_candidates: Sequence[RecallCandidate],
        active_candidates: Sequence[RecallCandidate],
        token_budget: int,
        candidate_limit: int | None = None,
    ) -> ContextFusionReport:
        fused_candidates = self.merge(
            recall_candidates=recall_candidates,
            active_candidates=active_candidates,
            candidate_limit=None,
        )
        limited_candidates = _limit_candidates(fused_candidates, candidate_limit)
        bundle = self.gateway.assemble(limited_candidates, token_budget=token_budget)
        source_map = _source_map(recall_candidates, active_candidates)
        decisions = _decisions(
            fused_candidates=fused_candidates,
            limited_candidates=limited_candidates,
            bundle=bundle,
            source_map=source_map,
        )
        duplicate_count = len(recall_candidates) + len(active_candidates) - len(fused_candidates)
        metadata = dict(bundle.metadata)
        metadata.update(
            {
                "fusion_strategy": "query_recall_then_active_window",
                "recall_candidate_count": len(recall_candidates),
                "active_candidate_count": len(active_candidates),
                "fused_candidate_count": len(limited_candidates),
                "candidate_count_before_limit": len(fused_candidates),
                "duplicate_candidate_count": duplicate_count,
                "candidate_limit": candidate_limit,
            }
        )
        bundle = bundle.model_copy(update={"metadata": metadata})
        return ContextFusionReport(
            bundle=bundle,
            decisions=decisions,
            selected_count=sum(1 for decision in decisions if decision.selected),
            filtered_count=sum(1 for decision in decisions if not decision.selected),
            duplicate_candidate_count=duplicate_count,
            metadata={
                "fusion_strategy": "query_recall_then_active_window",
                "candidate_limit": candidate_limit,
                "token_budget": token_budget,
            },
        )

    def compare(
        self,
        recall_candidates: Sequence[RecallCandidate],
        active_candidates: Sequence[RecallCandidate],
        token_budget: int,
        candidate_limit: int | None = None,
    ) -> ContextFusionComparisonReport:
        baseline_bundle = self.gateway.assemble(recall_candidates, token_budget=token_budget)
        fused_report = self.report(
            recall_candidates=recall_candidates,
            active_candidates=active_candidates,
            token_budget=token_budget,
            candidate_limit=candidate_limit,
        )
        baseline_ids = _bundle_memory_ids(baseline_bundle)
        fused_ids = _bundle_memory_ids(fused_report.bundle)
        baseline_set = set(baseline_ids)
        fused_set = set(fused_ids)
        return ContextFusionComparisonReport(
            baseline_bundle=baseline_bundle,
            fused_report=fused_report,
            baseline_memory_ids=baseline_ids,
            fused_memory_ids=fused_ids,
            shared_memory_ids=[memory_id for memory_id in fused_ids if memory_id in baseline_set],
            baseline_only_memory_ids=[
                memory_id for memory_id in baseline_ids if memory_id not in fused_set
            ],
            fused_only_memory_ids=[memory_id for memory_id in fused_ids if memory_id not in baseline_set],
            metadata={
                "comparison_strategy": "baseline_build_context_vs_explicit_fusion",
                "token_budget": token_budget,
                "candidate_limit": candidate_limit,
                "baseline_included_count": len(baseline_ids),
                "fused_included_count": len(fused_ids),
                "included_delta": len(fused_ids) - len(baseline_ids),
            },
        )


def _limit_candidates(
    candidates: Sequence[RecallCandidate],
    candidate_limit: int | None,
) -> Sequence[RecallCandidate]:
    if candidate_limit is None:
        return list(candidates)
    return list(candidates)[: max(0, candidate_limit)]


def _source_map(
    recall_candidates: Sequence[RecallCandidate],
    active_candidates: Sequence[RecallCandidate],
) -> dict[str, list[str]]:
    sources: dict[str, list[str]] = {}
    for candidate in recall_candidates:
        sources.setdefault(candidate.memory.id, []).append("query_recall")
    for candidate in active_candidates:
        sources.setdefault(candidate.memory.id, []).append("active_window")
    return {memory_id: _unique(values) for memory_id, values in sources.items()}


def _decisions(
    fused_candidates: Sequence[RecallCandidate],
    limited_candidates: Sequence[RecallCandidate],
    bundle: ContextBundle,
    source_map: dict[str, list[str]],
) -> list[ContextFusionDecision]:
    limited_ids = {candidate.memory.id for candidate in limited_candidates}
    included_items = {
        source_id: item
        for item in bundle.items
        for source_id in item.source_memory_ids
    }
    decisions: list[ContextFusionDecision] = []
    for candidate in fused_candidates:
        memory_id = candidate.memory.id
        item = included_items.get(memory_id)
        if item is not None:
            reason = "included"
        elif memory_id not in limited_ids:
            reason = "outside_candidate_limit"
        else:
            reason = "budget_excluded"
        sources = source_map.get(memory_id, [])
        decisions.append(
            ContextFusionDecision(
                memory_id=memory_id,
                selected=item is not None,
                reason=reason,
                sources=sources,
                merged_duplicate=len(sources) > 1,
                retrieval_score=candidate.score.retrieval_score,
                resident_score=candidate.score.resident_score,
                band=item.band if item is not None else None,
                retrieval_marker=item.retrieval_marker if item is not None else None,
                metadata={
                    "layer": candidate.memory.layer.value,
                    "matched_by": candidate.matched_by,
                },
            )
        )
    return decisions


def _bundle_memory_ids(bundle: ContextBundle) -> list[str]:
    ids: list[str] = []
    for item in bundle.items:
        for memory_id in item.source_memory_ids:
            if memory_id not in ids:
                ids.append(memory_id)
    return ids


def _tag_candidate(candidate: RecallCandidate, source: str) -> RecallCandidate:
    matched_by = list(candidate.matched_by)
    if source not in matched_by:
        matched_by.append(source)
    return candidate.model_copy(update={"matched_by": matched_by})


def _merge_duplicate(
    recall_candidate: RecallCandidate,
    active_candidate: RecallCandidate,
) -> RecallCandidate:
    matched_by = _unique([*recall_candidate.matched_by, *active_candidate.matched_by])
    score = ScoreBreakdown(
        retrieval_score=max(
            recall_candidate.score.retrieval_score or 0.0,
            active_candidate.score.retrieval_score or 0.0,
        ),
        resident_score=max(
            recall_candidate.score.resident_score,
            active_candidate.score.resident_score,
        ),
        structural=max(recall_candidate.score.structural, active_candidate.score.structural),
        recency=max(recall_candidate.score.recency, active_candidate.score.recency),
        access=max(recall_candidate.score.access, active_candidate.score.access),
        semantic=recall_candidate.score.semantic or active_candidate.score.semantic,
        task_affinity=recall_candidate.score.task_affinity or active_candidate.score.task_affinity,
        urgency=recall_candidate.score.urgency or active_candidate.score.urgency,
        evidence_quality=max(
            recall_candidate.score.evidence_quality,
            active_candidate.score.evidence_quality,
        ),
    )
    explanation = recall_candidate.explanation
    if active_candidate.explanation:
        explanation = "%s Active window duplicate signals were merged." % (
            explanation or ""
        )
    return recall_candidate.model_copy(
        update={
            "score": score,
            "matched_by": matched_by,
            "explanation": explanation.strip() if explanation else None,
        }
    )


def _unique(values: Sequence[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        result.append(value)
    return result
