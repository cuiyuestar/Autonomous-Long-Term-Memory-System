"""Lifecycle scoring and candidate marking.

This module keeps resident scoring separate from query-time retrieval scoring.
Governance cycles update durable resident signals; retrieval uses those durable
signals only as a bounded adjustment to task relevance.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from math import exp
from typing import Sequence

from altm.contracts import (
    AccessSignal,
    LifecycleMeta,
    MemoryLayer,
    MemoryStatus,
    MemoryUnit,
    ScoreBreakdown,
)
from altm.storage import SQLiteMemoryStore
from altm.utils import utc_now_iso


@dataclass(frozen=True)
class LifecycleGovernancePolicy:
    promotion_threshold: float = 0.70
    demotion_threshold: float = 0.22
    recency_half_life_days: float = 30.0
    pending_review_multiplier: float = 0.75
    rejected_review_multiplier: float = 0.15
    resident_retrieval_boost: float = 0.15


LAYER_STRUCTURAL_BASE = {
    MemoryLayer.L0: 0.35,
    MemoryLayer.L1: 0.45,
    MemoryLayer.L2: 0.60,
    MemoryLayer.L3: 0.75,
    MemoryLayer.L4: 0.90,
}


def score_memory_unit(
    memory: MemoryUnit,
    now: str | None = None,
    policy: LifecycleGovernancePolicy | None = None,
) -> MemoryUnit:
    policy = policy or LifecycleGovernancePolicy()
    now = now or utc_now_iso()

    structural = _clamp(LAYER_STRUCTURAL_BASE[memory.layer] + _evidence_bonus(memory))
    recency = _recency_score(memory, now, policy)
    access = _access_score(memory)
    evidence_quality = _evidence_quality(memory)
    resident_score = _clamp(
        (
            0.30 * structural
            + 0.30 * access
            + 0.25 * recency
            + 0.15 * evidence_quality
        )
        * _review_multiplier(memory, policy)
    )

    promotion_candidate_since = memory.lifecycle.promotion_candidate_since
    demotion_candidate_since = memory.lifecycle.demotion_candidate_since
    if resident_score >= policy.promotion_threshold:
        promotion_candidate_since = promotion_candidate_since or now
        demotion_candidate_since = None
    elif resident_score <= policy.demotion_threshold:
        demotion_candidate_since = demotion_candidate_since or now
        promotion_candidate_since = None
    else:
        promotion_candidate_since = None
        demotion_candidate_since = None

    score = ScoreBreakdown(
        resident_score=resident_score,
        structural=structural,
        recency=recency,
        access=access,
        evidence_quality=evidence_quality,
    )
    lifecycle = LifecycleMeta(
        age=memory.lifecycle.age + 1,
        protection_tier=_protection_tier(resident_score),
        compression_tier=memory.lifecycle.compression_tier,
        observation_until=memory.lifecycle.observation_until,
        promotion_candidate_since=promotion_candidate_since,
        demotion_candidate_since=demotion_candidate_since,
    )
    return memory.model_copy(
        update={
            "score": score,
            "lifecycle": lifecycle,
            "updated_at": now,
        }
    )


def adjust_retrieval_score(
    memory: MemoryUnit,
    base_score: float,
    policy: LifecycleGovernancePolicy | None = None,
) -> float:
    policy = policy or LifecycleGovernancePolicy()
    if memory.status in {MemoryStatus.DELETED, MemoryStatus.TOMBSTONED}:
        return 0.0

    multiplier = _review_multiplier(memory, policy)
    if memory.status == MemoryStatus.COMPRESSED:
        multiplier *= 0.65
    elif memory.status == MemoryStatus.OBSERVING:
        multiplier *= 0.90
    if memory.lifecycle.demotion_candidate_since is not None:
        multiplier *= 0.75

    resident_boost = min(1.0, memory.score.resident_score) * policy.resident_retrieval_boost
    return _clamp(base_score * multiplier + resident_boost)


class LifecycleGovernor:
    def __init__(
        self,
        store: SQLiteMemoryStore,
        policy: LifecycleGovernancePolicy | None = None,
    ) -> None:
        self.store = store
        self.policy = policy or LifecycleGovernancePolicy()

    def record_access_signal(self, memory_id: str, signal: AccessSignal) -> None:
        self.store.record_access_signal(memory_id, signal)

    def run_cycle(
        self,
        limit: int = 1000,
        layer: MemoryLayer | None = None,
    ) -> Sequence[MemoryUnit]:
        now = utc_now_iso()
        updated: list[MemoryUnit] = []
        for memory in self.store.list_memory_units(layer=layer, limit=limit):
            governed = score_memory_unit(memory, now=now, policy=self.policy)
            self.store.put_memory_unit(governed)
            updated.append(governed)
        return updated


def _review_multiplier(memory: MemoryUnit, policy: LifecycleGovernancePolicy) -> float:
    review_status = memory.metadata.get("review_status")
    if review_status == "pending":
        return policy.pending_review_multiplier
    if review_status == "rejected":
        return policy.rejected_review_multiplier
    return 1.0


def _evidence_bonus(memory: MemoryUnit) -> float:
    if memory.layer == MemoryLayer.L0:
        return 0.0
    return min(0.20, 0.05 * len(memory.evidence_refs))


def _evidence_quality(memory: MemoryUnit) -> float:
    if memory.layer == MemoryLayer.L0:
        return 1.0
    if not memory.evidence_refs:
        return 0.10
    return _clamp(sum(ref.confidence for ref in memory.evidence_refs) / len(memory.evidence_refs))


def _access_score(memory: MemoryUnit) -> float:
    return _clamp(memory.access_count * 0.05 + memory.useful_access_count * 0.20)


def _recency_score(
    memory: MemoryUnit,
    now: str,
    policy: LifecycleGovernancePolicy,
) -> float:
    anchor = memory.last_accessed_at or memory.updated_at or memory.created_at
    age_days = max(0.0, (_parse_iso(now) - _parse_iso(anchor)).total_seconds() / 86400.0)
    if policy.recency_half_life_days <= 0:
        return 1.0
    return _clamp(exp(-age_days / policy.recency_half_life_days))


def _protection_tier(resident_score: float) -> int:
    if resident_score >= 0.85:
        return 5
    if resident_score >= 0.65:
        return 4
    if resident_score >= 0.45:
        return 3
    if resident_score >= 0.25:
        return 2
    return 1


def _parse_iso(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _clamp(value: float) -> float:
    return max(0.0, min(1.0, value))
