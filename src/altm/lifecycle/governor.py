"""Lifecycle scoring and candidate marking.

This module keeps resident scoring separate from query-time retrieval scoring.
Governance cycles update durable resident signals; retrieval uses those durable
signals only as a bounded adjustment to task relevance.
"""

from __future__ import annotations

import os
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from math import exp

from altm.contracts import (
    AccessSignal,
    LifecycleMeta,
    LifecycleState,
    MemoryLayer,
    MemoryStatus,
    MemoryUnit,
    ScoreBreakdown,
)
from altm.storage import SQLiteMemoryStore
from altm.utils import random_id, utc_now_iso


@dataclass(frozen=True)
class LifecycleGovernancePolicy:
    promotion_threshold: float = 0.70
    demotion_threshold: float = 0.22
    recency_half_life_days: float = 30.0
    pending_review_multiplier: float = 1.0
    rejected_review_multiplier: float = 0.15
    resident_retrieval_boost: float = 0.15
    promotion_min_cycles: int = 3
    demotion_min_cycles: int = 5
    promotion_min_useful_accesses: int = 2
    promotion_min_evidence_quality: float = 0.60
    long_budget_tokens: int = 100_000


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
    weighted_score = (
            0.30 * structural
            + 0.30 * access
            + 0.25 * recency
            + 0.15 * evidence_quality
        ) * _review_multiplier(memory, policy)
    pinned_boost = 0.20 if memory.metadata.get("pinned") is True else 0.0
    resident_score = _clamp(
        weighted_score + pinned_boost
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
        memories = list(self.store.list_memory_units(layer=layer, limit=limit))
        long_tokens = sum(
            _token_estimate(memory)
            for memory in memories
            if memory.lifecycle_state
            in {LifecycleState.LONG, LifecycleState.PERMANENT}
        )
        configured_budget = int(
            os.environ.get(
                "ALTM_LONG_MEMORY_TOKEN_BUDGET",
                str(self.policy.long_budget_tokens),
            )
        )
        if configured_budget <= 0:
            raise ValueError("ALTM_LONG_MEMORY_TOKEN_BUDGET must be positive")
        pressure = long_tokens / configured_budget
        dynamic_threshold = _clamp(
            self.policy.promotion_threshold
            + max(0.0, pressure - 0.70) * 0.35
        )
        scoring_policy = replace(
            self.policy,
            promotion_threshold=dynamic_threshold,
        )
        updated: list[MemoryUnit] = []
        for memory in memories:
            governed = score_memory_unit(memory, now=now, policy=scoring_policy)
            transitioned = _apply_transition(
                governed,
                policy=scoring_policy,
                long_pressure=pressure,
                now=now,
            )
            updated.append(transitioned)
        self.store.put_memory_units(updated)
        self._record_age_stats(updated, now)
        return updated

    def _record_age_stats(
        self,
        memories: Sequence[MemoryUnit],
        created_at: str,
    ) -> None:
        if not memories:
            return
        grouped: dict[tuple[str, str, int], tuple[int, int]] = defaultdict(
            lambda: (0, 0)
        )
        for memory in memories:
            key = (
                memory.layer.value,
                memory.lifecycle_state.value,
                min(100, (memory.lifecycle.age // 5) * 5),
            )
            count, tokens = grouped[key]
            grouped[key] = (count + 1, tokens + _token_estimate(memory))
        cycle_id = random_id("lifecycle_cycle")
        scope = self.store.scope
        if scope is None:
            return
        with self.store.connect() as connection:
            connection.executemany(
                """
                INSERT INTO lifecycle_age_stats(
                  cycle_id, tenant_id, workspace_id, user_id, agent_id,
                  layer, lifecycle_state, age_bucket, memory_count,
                  token_estimate, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        cycle_id,
                        *scope.key_parts(),
                        layer,
                        lifecycle_state,
                        age_bucket,
                        count,
                        tokens,
                        created_at,
                    )
                    for (layer, lifecycle_state, age_bucket), (count, tokens)
                    in grouped.items()
                ],
            )
            connection.commit()


def _apply_transition(
    memory: MemoryUnit,
    policy: LifecycleGovernancePolicy,
    long_pressure: float,
    now: str,
) -> MemoryUnit:
    if memory.layer == MemoryLayer.L0:
        return memory
    metadata = dict(memory.metadata)
    pinned = metadata.get("pinned") is True
    has_conflict = (
        metadata.get("unresolved_conflict") is True
        or metadata.get("review_status") == "rejected"
    )
    promotion_streak = int(metadata.get("promotion_streak", 0))
    demotion_streak = int(metadata.get("demotion_streak", 0))
    if memory.score.resident_score >= policy.promotion_threshold:
        promotion_streak += 1
        demotion_streak = 0
    elif memory.score.resident_score <= policy.demotion_threshold:
        demotion_streak += 1
        promotion_streak = 0
    else:
        promotion_streak = 0
        demotion_streak = 0
    metadata.update(
        {
            "promotion_streak": promotion_streak,
            "demotion_streak": demotion_streak,
            "dynamic_promotion_threshold": policy.promotion_threshold,
            "long_memory_pressure": long_pressure,
        }
    )

    lifecycle_state = memory.lifecycle_state
    status = memory.status
    lifecycle = memory.lifecycle
    if (
        lifecycle_state == LifecycleState.SHORT
        and promotion_streak >= policy.promotion_min_cycles
    ):
        failure_reasons: list[str] = []
        if (
            memory.useful_access_count < policy.promotion_min_useful_accesses
            and not pinned
        ):
            failure_reasons.append("insufficient_useful_access")
        if (
            memory.score.evidence_quality
            < policy.promotion_min_evidence_quality
            and not pinned
        ):
            failure_reasons.append("insufficient_evidence_quality")
        if has_conflict:
            failure_reasons.append("unresolved_conflict")
        if long_pressure >= 1.0 and not pinned:
            failure_reasons.append("long_budget_exhausted")
        if failure_reasons:
            metadata.update(
                {
                    "promotion_failure": True,
                    "promotion_failure_reasons": failure_reasons,
                    "promotion_failure_at": now,
                }
            )
        else:
            lifecycle_state = LifecycleState.LONG
            status = (
                MemoryStatus.ACTIVE
                if memory.status == MemoryStatus.OBSERVING
                else memory.status
            )
            metadata.update(
                {
                    "promoted_at": now,
                    "promotion_failure": False,
                    "promotion_streak": 0,
                }
            )
            lifecycle = lifecycle.model_copy(
                update={
                    "promotion_candidate_since": None,
                    "demotion_candidate_since": None,
                }
            )
    elif (
        lifecycle_state == LifecycleState.LONG
        and demotion_streak >= policy.demotion_min_cycles
        and not pinned
    ):
        lifecycle_state = LifecycleState.SHORT
        status = MemoryStatus.OBSERVING
        metadata.update(
            {
                "demoted_at": now,
                "demotion_streak": 0,
            }
        )
        lifecycle = lifecycle.model_copy(
            update={
                "observation_until": (
                    _parse_iso(now) + timedelta(days=30)
                ).isoformat(timespec="seconds"),
                "promotion_candidate_since": None,
                "demotion_candidate_since": None,
            }
        )

    return memory.model_copy(
        update={
            "lifecycle_state": lifecycle_state,
            "status": status,
            "lifecycle": lifecycle,
            "metadata": metadata,
            "updated_at": now,
        }
    )


def _token_estimate(memory: MemoryUnit) -> int:
    return max(1, (len(memory.content) + 3) // 4)


def _review_multiplier(memory: MemoryUnit, policy: LifecycleGovernancePolicy) -> float:
    review_status = memory.metadata.get("review_status")
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
    anchor = memory.last_accessed_at or memory.created_at
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
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _clamp(value: float) -> float:
    return max(0.0, min(1.0, value))
