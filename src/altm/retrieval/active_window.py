"""Global active window selection for query-before agent context."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

from altm.contracts import (
    ActiveWindowDecision,
    ActiveWindowReport,
    LifecycleState,
    MemoryLayer,
    MemoryStatus,
    MemoryUnit,
    RecallCandidate,
    ScoreBreakdown,
)
from altm.lifecycle import adjust_retrieval_score
from altm.storage import SQLiteMemoryStore


@dataclass(frozen=True)
class GlobalActiveWindowPolicy:
    """Policy for selecting proactive context without a user query."""

    default_layers: tuple[MemoryLayer, ...] = (
        MemoryLayer.L2,
        MemoryLayer.L3,
        MemoryLayer.L4,
    )
    default_statuses: tuple[MemoryStatus, ...] = (
        MemoryStatus.ACTIVE,
        MemoryStatus.OBSERVING,
    )
    scan_limit: int = 1000
    min_active_score: float = 0.20
    l4_min_active_score: float = 0.80
    l4_candidate_limit: int = 1
    allow_l4_persona: bool = True


class GlobalActiveWindowEngine:
    """Select stable, currently useful memories before query-time recall."""

    def __init__(
        self,
        store: SQLiteMemoryStore,
        policy: GlobalActiveWindowPolicy | None = None,
    ) -> None:
        self.store = store
        self.policy = policy or GlobalActiveWindowPolicy()

    def select(
        self,
        limit: int = 10,
        session_id: str | None = None,
        layers: Sequence[MemoryLayer] | None = None,
        statuses: Sequence[MemoryStatus] | None = None,
        strict_session: bool = False,
    ) -> Sequence[RecallCandidate]:
        return self.report(
            limit=limit,
            session_id=session_id,
            layers=layers,
            statuses=statuses,
            strict_session=strict_session,
            decision_limit=0,
        ).candidates

    def report(
        self,
        limit: int = 10,
        session_id: str | None = None,
        layers: Sequence[MemoryLayer] | None = None,
        statuses: Sequence[MemoryStatus] | None = None,
        strict_session: bool = False,
        decision_limit: int = 100,
    ) -> ActiveWindowReport:
        selected_layers = tuple(layers or self.policy.default_layers)
        selected_statuses = tuple(statuses or self.policy.default_statuses)
        candidates: list[RecallCandidate] = []
        filtered_decisions: list[ActiveWindowDecision] = []

        for memory in self.store.list_memory_units(limit=self.policy.scan_limit):
            exclusion_reason = _exclusion_reason(
                memory,
                layers=selected_layers,
                statuses=selected_statuses,
                session_id=session_id,
                strict_session=strict_session,
                allow_l4_persona=self.policy.allow_l4_persona,
            )
            if exclusion_reason is not None:
                filtered_decisions.append(
                    _decision(
                        memory,
                        selected=False,
                        reason=exclusion_reason,
                        session_id=session_id,
                    )
                )
                continue
            base_score = _base_active_score(memory, session_id=session_id)
            retrieval_score = adjust_retrieval_score(memory, base_score)
            matched_by = _matched_by(memory, session_id)
            if (
                memory.layer == MemoryLayer.L4
                and retrieval_score < self.policy.l4_min_active_score
            ):
                filtered_decisions.append(
                    _decision(
                        memory,
                        selected=False,
                        reason="l4_below_min_active_score",
                        session_id=session_id,
                        base_score=base_score,
                        active_score=retrieval_score,
                        matched_by=matched_by,
                    )
                )
                continue
            if retrieval_score < self.policy.min_active_score:
                filtered_decisions.append(
                    _decision(
                        memory,
                        selected=False,
                        reason="below_min_active_score",
                        session_id=session_id,
                        base_score=base_score,
                        active_score=retrieval_score,
                        matched_by=matched_by,
                    )
                )
                continue
            candidates.append(
                RecallCandidate(
                    memory=memory,
                    score=ScoreBreakdown(
                        retrieval_score=retrieval_score,
                        resident_score=memory.score.resident_score,
                        structural=memory.score.structural,
                        recency=memory.score.recency,
                        access=memory.score.access,
                        task_affinity=_session_affinity(memory, session_id),
                        evidence_quality=memory.score.evidence_quality,
                    ),
                    matched_by=matched_by,
                    explanation=(
                        "Global active window selected this memory from durable "
                        "resident, lifecycle, layer, and session-affinity signals."
                    ),
                )
            )

        candidates.sort(
            key=lambda candidate: (
                candidate.score.retrieval_score or 0.0,
                _lifecycle_weight(candidate.memory),
                candidate.memory.updated_at,
                candidate.memory.id,
            ),
            reverse=True,
        )
        selected_candidates = _select_with_policy_limits(candidates, max(0, limit), self.policy)
        selected_ids = {candidate.memory.id for candidate in selected_candidates}
        eligible_decisions = [
            _decision(
                candidate.memory,
                selected=candidate.memory.id in selected_ids,
                reason="selected" if candidate.memory.id in selected_ids else "outside_limit",
                session_id=session_id,
                base_score=_base_active_score(candidate.memory, session_id=session_id),
                active_score=candidate.score.retrieval_score,
                matched_by=candidate.matched_by,
            )
            for candidate in candidates
        ]
        decisions = (eligible_decisions + filtered_decisions)[: max(0, decision_limit)]
        return ActiveWindowReport(
            candidates=selected_candidates,
            decisions=decisions,
            selected_count=len(selected_candidates),
            filtered_count=len(candidates) - len(selected_candidates) + len(filtered_decisions),
            metadata={
                "scan_limit": self.policy.scan_limit,
                "min_active_score": self.policy.min_active_score,
                "l4_min_active_score": self.policy.l4_min_active_score,
                "l4_candidate_limit": self.policy.l4_candidate_limit,
                "allow_l4_persona": self.policy.allow_l4_persona,
                "decision_limit": max(0, decision_limit),
                "candidate_count_before_limit": len(candidates),
                "filtered_before_limit": len(filtered_decisions),
                "layers": [layer.value for layer in selected_layers],
                "statuses": [status.value for status in selected_statuses],
                "session_id": session_id,
                "strict_session": strict_session,
            },
        )


def _select_with_policy_limits(
    candidates: Sequence[RecallCandidate],
    limit: int,
    policy: GlobalActiveWindowPolicy,
) -> list[RecallCandidate]:
    selected: list[RecallCandidate] = []
    l4_count = 0
    for candidate in candidates:
        if len(selected) >= limit:
            break
        if candidate.memory.layer == MemoryLayer.L4:
            if l4_count >= policy.l4_candidate_limit:
                continue
            l4_count += 1
        selected.append(candidate)
    return selected


def _exclusion_reason(
    memory: MemoryUnit,
    layers: Sequence[MemoryLayer],
    statuses: Sequence[MemoryStatus],
    session_id: str | None,
    strict_session: bool,
    allow_l4_persona: bool,
) -> str | None:
    if memory.status in {MemoryStatus.DELETED, MemoryStatus.TOMBSTONED}:
        return "terminal_status"
    if memory.layer == MemoryLayer.L4 and not allow_l4_persona:
        return "l4_disabled_by_policy"
    if layers and memory.layer not in layers:
        return "layer_excluded"
    if statuses and memory.status not in statuses:
        return "status_excluded"
    if _review_state(memory, "review_status") == "rejected":
        return "review_rejected"
    if _review_state(memory, "governance_review_status") == "rejected":
        return "governance_rejected"
    if memory.layer == MemoryLayer.L2:
        review_status = _review_state(memory, "review_status")
        if review_status == "pending":
            return "l2_pending_review"
        if memory.metadata.get("atom_type") is not None and review_status != "approved":
            return "l2_not_approved"
    if session_id is not None:
        memory_session = memory.metadata.get("session_id")
        if strict_session:
            return None if memory_session == session_id else "strict_session_mismatch"
        if memory_session not in {None, "", "global", session_id} and memory.lifecycle_state not in {
            LifecycleState.LONG,
            LifecycleState.PERMANENT,
        }:
            return "session_mismatch"
    return None


def _base_active_score(memory: MemoryUnit, session_id: str | None) -> float:
    return _clamp(
        0.45 * min(1.0, memory.score.resident_score)
        + 0.20 * _lifecycle_weight(memory)
        + 0.15 * _layer_weight(memory)
        + 0.10 * _status_weight(memory)
        + 0.05 * min(1.0, memory.score.evidence_quality)
        + 0.05 * _session_affinity(memory, session_id)
    )


def _matched_by(memory: MemoryUnit, session_id: str | None) -> list[str]:
    matched_by = ["global_active_window"]
    if memory.lifecycle_state in {LifecycleState.LONG, LifecycleState.PERMANENT}:
        matched_by.append("long_term")
    if memory.score.resident_score >= 0.70:
        matched_by.append("high_resident_score")
    if _session_affinity(memory, session_id) >= 1.0:
        matched_by.append("session_affinity")
    return matched_by


def _decision(
    memory: MemoryUnit,
    selected: bool,
    reason: str,
    session_id: str | None,
    base_score: float | None = None,
    active_score: float | None = None,
    matched_by: Sequence[str] | None = None,
) -> ActiveWindowDecision:
    return ActiveWindowDecision(
        memory_id=memory.id,
        selected=selected,
        reason=reason,
        layer=memory.layer,
        lifecycle_state=memory.lifecycle_state,
        status=memory.status,
        summary=memory.summary,
        base_score=base_score,
        active_score=active_score,
        resident_score=memory.score.resident_score,
        task_affinity=_session_affinity(memory, session_id),
        matched_by=list(matched_by or []),
        metadata={
            "session_id": memory.metadata.get("session_id"),
            "review_status": memory.metadata.get("review_status"),
            "governance_review_status": memory.metadata.get("governance_review_status"),
            "atom_type": memory.metadata.get("atom_type"),
        },
    )


def _review_state(memory: MemoryUnit, key: str) -> str | None:
    value = memory.metadata.get(key)
    return str(value) if value is not None else None


def _lifecycle_weight(memory: MemoryUnit) -> float:
    if memory.lifecycle_state == LifecycleState.PERMANENT:
        return 1.0
    if memory.lifecycle_state == LifecycleState.LONG:
        return 0.85
    return 0.35


def _layer_weight(memory: MemoryUnit) -> float:
    weights = {
        MemoryLayer.L0: 0.10,
        MemoryLayer.L1: 0.35,
        MemoryLayer.L2: 0.65,
        MemoryLayer.L3: 0.80,
        MemoryLayer.L4: 1.00,
    }
    return weights[memory.layer]


def _status_weight(memory: MemoryUnit) -> float:
    if memory.status == MemoryStatus.ACTIVE:
        return 0.80
    if memory.status == MemoryStatus.OBSERVING:
        return 0.55
    if memory.status == MemoryStatus.COMPRESSED:
        return 0.25
    return 0.0


def _session_affinity(memory: MemoryUnit, session_id: str | None) -> float:
    memory_session = memory.metadata.get("session_id")
    if session_id is None:
        return 0.50 if memory_session in {None, "", "global"} else 0.20
    if memory_session == session_id:
        return 1.0
    if memory_session in {None, "", "global"}:
        return 0.60
    return 0.0


def _clamp(value: float) -> float:
    return max(0.0, min(1.0, value))
