"""Autonomous governance engine for runtime memory self-management.

This module deliberately treats model review as an internal autonomous audit
signal, not as a human approval gate. Rule fallback remains executable when
optional model evaluators are unavailable.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass, field
from math import sqrt
from typing import cast

from altm.contracts import (
    EvidenceRef,
    EvidenceRelation,
    LifecycleMeta,
    LifecycleState,
    MemoryLayer,
    MemoryStatus,
    MemoryUnit,
)
from altm.folding.l4_persona import PERSONA_ATOM_TYPES
from altm.governance.semantic_dedup import SemanticDeduper, SemanticDedupPolicy
from altm.llm import OpenAICompatibleClient, llm_config_from_env
from altm.storage import SQLiteMemoryStore
from altm.utils import sha256_text, stable_id, utc_now_iso

AUTONOMOUS_EVENT_EVALUATED = "autonomous_governance_evaluated"
AUTONOMOUS_EVENT_DECIDED = "autonomous_governance_decided"
AUTONOMOUS_EVENT_APPLIED = "autonomous_governance_applied"
AUTONOMOUS_EVENT_DEGRADED = "autonomous_governance_degraded"
AUTONOMOUS_EVENT_ROLLED_BACK = "autonomous_governance_rolled_back"
AUTONOMOUS_POLICY_VERSION = "autonomous_governance_v1"
CROSS_SESSION_L3_EDGE = "cross_session_l3_candidate"


@dataclass(frozen=True)
class AutonomousGovernanceDecision:
    id: str
    target_type: str
    target_id: str
    action_type: str
    risk_tier: str
    decision: str
    confidence: float
    rule_score: float
    reasons: tuple[str, ...] = ()
    evidence_memory_ids: tuple[str, ...] = ()
    small_model_score: float | None = None
    llm_judge_score: float | None = None
    model_outputs: dict[str, object] = field(
        default_factory=lambda: dict[str, object]()
    )
    fallback_mode: str = "llm_unavailable"
    rollback_payload: dict[str, object] | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "id": self.id,
            "target_type": self.target_type,
            "target_id": self.target_id,
            "action_type": self.action_type,
            "risk_tier": self.risk_tier,
            "decision": self.decision,
            "confidence": self.confidence,
            "rule_score": self.rule_score,
            "small_model_score": self.small_model_score,
            "llm_judge_score": self.llm_judge_score,
            "reasons": list(self.reasons),
            "evidence_memory_ids": list(self.evidence_memory_ids),
            "model_outputs": self.model_outputs,
            "fallback_mode": self.fallback_mode,
            "rollback_payload": self.rollback_payload or {},
        }


class AutonomousGovernanceEngine:
    def __init__(self, store: SQLiteMemoryStore) -> None:
        self.store = store

    def run(
        self,
        embedding_model: str | None = None,
        semantic_threshold: float = 0.92,
        semantic_auto_merge_threshold: float = 0.97,
        semantic_auto_tombstone_threshold: float = 0.97,
        l3_threshold: float = 0.82,
        persona_min_support: int = 2,
        include_p0: bool = True,
        include_p1: bool = True,
        model_mode: str = "auto",
        rule_fallback: bool = False,
        dry_run: bool = False,
        limit: int = 1000,
    ) -> dict[str, object]:
        normalized_model_mode = _model_mode(model_mode)
        steps: dict[str, object] = {}
        if not include_p0:
            steps["p0"] = {"status": "skipped", "reason": "include_p0_false"}
            return _cycle_result(
                dry_run=dry_run,
                model_mode=normalized_model_mode,
                rule_fallback=rule_fallback,
                steps=steps,
            )
        steps["cross_session_l3"] = self._run_cross_session_l3(
            embedding_model=embedding_model,
            threshold=l3_threshold,
            model_mode=normalized_model_mode,
            dry_run=dry_run,
            limit=limit,
        )
        steps["l4_persona"] = self._run_l4_persona(
            min_support=persona_min_support,
            model_mode=normalized_model_mode,
            dry_run=dry_run,
            limit=limit,
        )
        steps["semantic_dedup"] = self._run_semantic_dedup(
            embedding_model=embedding_model,
            threshold=semantic_threshold,
            auto_merge_threshold=semantic_auto_merge_threshold,
            auto_tombstone_threshold=semantic_auto_tombstone_threshold,
            model_mode=normalized_model_mode,
            dry_run=dry_run,
            limit=limit,
        )
        return _cycle_result(
            dry_run=dry_run,
            model_mode=normalized_model_mode,
            rule_fallback=rule_fallback,
            steps=steps,
            include_p1=include_p1,
        )

    def rollback(
        self,
        target_type: str,
        target_id: str,
        reason: str = "autonomous_rollback",
    ) -> dict[str, object]:
        if target_type == "graph_edge":
            restored = self.store.restore_semantic_l2_merge(edge_id=target_id, reason=reason)
            rolled_back = bool(restored.get("restored"))
            self._log_rollback(
                target_type=target_type,
                target_id=target_id,
                action_type="merge_duplicate",
                rolled_back=rolled_back,
                reason=reason,
                metadata=restored,
            )
            return {"rolled_back": rolled_back, "target_type": target_type, "target_id": target_id, "result": restored}

        if target_type != "memory_unit":
            return {
                "rolled_back": False,
                "target_type": target_type,
                "target_id": target_id,
                "reason": "unsupported_target_type",
            }
        memory = self.store.get_memory_unit(target_id)
        if memory is None:
            return {
                "rolled_back": False,
                "target_type": target_type,
                "target_id": target_id,
                "reason": "missing_memory",
            }
        if memory.metadata.get("builder") != "autonomous_governance_engine":
            return {
                "rolled_back": False,
                "target_type": target_type,
                "target_id": target_id,
                "reason": "not_autonomous_memory",
            }
        metadata = dict(memory.metadata)
        metadata.update(
            {
                "autonomous_rollback_at": utc_now_iso(),
                "autonomous_rollback_reason": reason,
                "autonomous_decision": "rolled_back",
            }
        )
        rolled_back_memory = memory.model_copy(
            update={
                "status": MemoryStatus.TOMBSTONED,
                "metadata": metadata,
                "updated_at": utc_now_iso(),
            }
        )
        self.store.put_memory_unit(rolled_back_memory)
        self._log_rollback(
            target_type=target_type,
            target_id=target_id,
            action_type=str(memory.metadata.get("autonomous_action_type") or "created_memory"),
            rolled_back=True,
            reason=reason,
            metadata={
                "previous_status": memory.status.value,
                "previous_metadata": memory.metadata,
                "new_status": MemoryStatus.TOMBSTONED.value,
            },
        )
        return {
            "rolled_back": True,
            "target_type": target_type,
            "target_id": target_id,
            "reason": reason,
        }

    def _run_semantic_dedup(
        self,
        embedding_model: str | None,
        threshold: float,
        auto_merge_threshold: float,
        auto_tombstone_threshold: float,
        model_mode: str,
        dry_run: bool,
        limit: int,
    ) -> dict[str, object]:
        if not embedding_model:
            self._log_degraded(
                target_type="governance_batch",
                target_id="semantic_dedup",
                action_type="merge_duplicate",
                reason="missing_embedding_model",
                model_mode=model_mode,
                dry_run=dry_run,
            )
            return {
                "status": "degraded",
                "reason": "missing_embedding_model",
                "decision_count": 0,
                "applied_count": 0,
            }

        deduper = SemanticDeduper(
            self.store,
            SemanticDedupPolicy(
                similarity_threshold=threshold,
                auto_merge_threshold=auto_merge_threshold,
                auto_tombstone_threshold=auto_tombstone_threshold,
                require_review_approved=False,
                block_high_risk_atom_types=False,
            ),
        )
        candidates = deduper.mark_candidates(
            embedding_model=embedding_model,
            limit=limit,
            dry_run=dry_run,
        )
        decisions: list[AutonomousGovernanceDecision] = []
        for candidate in candidates:
            source = self.store.get_memory_unit(candidate.source_memory_id)
            target = self.store.get_memory_unit(candidate.target_memory_id)
            texts = [
                source.summary or source.content if source is not None else candidate.source_memory_id,
                target.summary or target.content if target is not None else candidate.target_memory_id,
            ]
            evaluation = _evaluate_action(
                action_type="merge_duplicate",
                texts=texts,
                rule_score=min(1.0, candidate.similarity),
                model_mode=model_mode,
            )
            decisions.append(
                _decision(
                    target_type="graph_edge",
                    target_id=candidate.edge_id,
                    action_type="merge_duplicate",
                    risk_tier="P0",
                    decision=str(evaluation["decision"]),
                    confidence=_optional_float(evaluation.get("llm_judge_score")) or 0.0,
                    rule_score=min(1.0, candidate.similarity),
                    reasons=(
                        "embedding_similarity:%0.4f" % candidate.similarity,
                        "llm_judge_required",
                    ),
                    evidence_memory_ids=(
                        candidate.source_memory_id,
                        candidate.target_memory_id,
                    ),
                    model_mode=model_mode,
                    fallback_mode=_text_value(evaluation.get("fallback_mode")),
                    small_model_score=_optional_float(evaluation.get("small_model_score")),
                    llm_judge_score=_optional_float(evaluation.get("llm_judge_score")),
                    model_outputs=_object_dict(evaluation.get("model_outputs")),
                    rollback_payload={
                        "edge_id": candidate.edge_id,
                        "rollback_method": "restore_semantic_l2_merge",
                    },
                )
            )
        self._log_decisions(decisions, dry_run=dry_run)
        executable_edge_ids = {
            decision.target_id for decision in decisions if decision.decision == "execute"
        }
        resolutions = deduper.auto_resolve_candidates(
            [
                candidate
                for candidate in candidates
                if candidate.edge_id in executable_edge_ids
            ],
            auto_merge=True,
            auto_tombstone=True,
            dry_run=dry_run,
        )
        applied = [resolution for resolution in resolutions if resolution.merged]
        for resolution in resolutions:
            matching = [
                decision for decision in decisions if decision.target_id == resolution.edge_id
            ]
            decision = matching[0] if matching else None
            self._log_applied_decision(
                decision=decision,
                target_type="graph_edge",
                target_id=resolution.edge_id,
                action_type="merge_duplicate",
                applied=resolution.merged and not dry_run,
                dry_run=dry_run,
                metadata={
                    "canonical_memory_id": resolution.canonical_memory_id,
                    "duplicate_memory_id": resolution.duplicate_memory_id,
                    "merged": resolution.merged,
                    "tombstoned": resolution.tombstoned,
                    "reason": resolution.reason,
                    "details": resolution.details,
                },
            )
        return {
            "status": "preview" if dry_run else "applied",
            "candidate_count": len(candidates),
            "decision_count": len(decisions),
            "applied_count": len(applied),
            "resolution_count": len(resolutions),
            "decisions": [decision.to_dict() for decision in decisions],
            "resolutions": [
                {
                    "edge_id": resolution.edge_id,
                    "canonical_memory_id": resolution.canonical_memory_id,
                    "duplicate_memory_id": resolution.duplicate_memory_id,
                    "merged": resolution.merged,
                    "tombstoned": resolution.tombstoned,
                    "reason": resolution.reason,
                    "dry_run": resolution.dry_run,
                    "details": resolution.details,
                }
                for resolution in resolutions
            ],
        }

    def _run_cross_session_l3(
        self,
        embedding_model: str | None,
        threshold: float,
        model_mode: str,
        dry_run: bool,
        limit: int,
    ) -> dict[str, object]:
        if not embedding_model:
            self._log_degraded(
                target_type="governance_batch",
                target_id="cross_session_l3",
                action_type="materialize_l3_scene",
                reason="missing_embedding_model",
                model_mode=model_mode,
                dry_run=dry_run,
            )
            return {
                "status": "degraded",
                "reason": "missing_embedding_model",
                "decision_count": 0,
                "applied_count": 0,
            }

        candidates = _cross_session_l3_candidates(
            self.store,
            embedding_model=embedding_model,
            threshold=threshold,
            limit=limit,
        )
        decisions: list[AutonomousGovernanceDecision] = []
        applied_memory_ids: list[str] = []
        for candidate in candidates:
            evaluation = _evaluate_action(
                action_type="materialize_l3_scene",
                texts=[str(candidate["source_summary"]), str(candidate["target_summary"])],
                rule_score=_float_value(candidate.get("similarity")),
                model_mode=model_mode,
            )
            decision = _decision(
                target_type="graph_edge",
                target_id=_text_value(candidate.get("edge_id")),
                action_type="materialize_l3_scene",
                risk_tier="P0",
                decision=str(evaluation["decision"]),
                confidence=_optional_float(evaluation.get("llm_judge_score")) or 0.0,
                rule_score=_float_value(candidate.get("similarity")),
                reasons=(
                    "cross_session_similarity:%0.4f" % candidate["similarity"],
                    "same_atom_type:%s" % candidate["atom_type"],
                    "different_sessions",
                ),
                evidence_memory_ids=(
                    str(candidate["source_memory_id"]),
                    str(candidate["target_memory_id"]),
                ),
                model_mode=model_mode,
                fallback_mode=_text_value(evaluation.get("fallback_mode")),
                small_model_score=_optional_float(evaluation.get("small_model_score")),
                llm_judge_score=_optional_float(evaluation.get("llm_judge_score")),
                model_outputs=_object_dict(evaluation.get("model_outputs")),
                rollback_payload={
                    "edge_id": candidate["edge_id"],
                    "rollback_target": "created_l3_memory",
                },
            )
            decisions.append(decision)
            self._log_decision(decision, dry_run=dry_run)
            if dry_run or decision.decision != "execute":
                self._log_applied_decision(
                    decision=decision,
                    target_type="graph_edge",
                    target_id=str(candidate["edge_id"]),
                    action_type="materialize_l3_scene",
                    applied=False,
                    dry_run=dry_run,
                    metadata={"reason": "governance_decision_%s" % decision.decision},
                )
                continue
            scene = _materialize_l3_scene(self.store, candidate)
            if scene is not None:
                applied_memory_ids.append(scene.id)
                self._log_applied_decision(
                    decision=decision,
                    target_type="memory_unit",
                    target_id=scene.id,
                    action_type="materialize_l3_scene",
                    applied=True,
                    dry_run=False,
                    metadata={
                        "edge_id": candidate["edge_id"],
                        "source_memory_id": candidate["source_memory_id"],
                        "target_memory_id": candidate["target_memory_id"],
                        "l3_memory_id": scene.id,
                    },
                )
        return {
            "status": "preview" if dry_run else "applied",
            "candidate_count": len(candidates),
            "decision_count": len(decisions),
            "applied_count": len(applied_memory_ids),
            "memory_ids": applied_memory_ids,
            "decisions": [decision.to_dict() for decision in decisions],
        }

    def _run_l4_persona(
        self,
        min_support: int,
        model_mode: str,
        dry_run: bool,
        limit: int,
    ) -> dict[str, object]:
        candidates = _l4_persona_candidates(self.store, min_support=min_support, limit=limit)
        decisions: list[AutonomousGovernanceDecision] = []
        applied_memory_ids: list[str] = []
        for candidate in candidates:
            evaluation = _evaluate_action(
                action_type="activate_l4_persona",
                texts=[candidate.summary or candidate.content],
                rule_score=float(candidate.metadata.get("autonomous_confidence", 0.8)),
                model_mode=model_mode,
            )
            decision = _decision(
                target_type="memory_unit",
                target_id=candidate.id,
                action_type="activate_l4_persona",
                risk_tier="P0",
                decision=str(evaluation["decision"]),
                confidence=_optional_float(evaluation.get("llm_judge_score")) or 0.0,
                rule_score=float(candidate.metadata.get("autonomous_confidence", 0.8)),
                reasons=(
                    "support_count:%s" % candidate.metadata.get("support_count"),
                    "persona_atom_type:%s" % candidate.metadata.get("atom_type"),
                    "llm_judge_required",
                ),
                evidence_memory_ids=_string_tuple(
                    candidate.metadata.get("source_memory_ids")
                ),
                model_mode=model_mode,
                fallback_mode=_text_value(evaluation.get("fallback_mode")),
                small_model_score=_optional_float(evaluation.get("small_model_score")),
                llm_judge_score=_optional_float(evaluation.get("llm_judge_score")),
                model_outputs=_object_dict(evaluation.get("model_outputs")),
                rollback_payload={
                    "memory_id": candidate.id,
                    "rollback_target": "created_l4_memory",
                },
            )
            decisions.append(decision)
            self._log_decision(decision, dry_run=dry_run)
            if dry_run or decision.decision != "execute":
                self._log_applied_decision(
                    decision=decision,
                    target_type="memory_unit",
                    target_id=candidate.id,
                    action_type="activate_l4_persona",
                    applied=False,
                    dry_run=dry_run,
                    metadata={"reason": "governance_decision_%s" % decision.decision},
                )
                continue
            self.store.put_memory_unit(candidate)
            applied_memory_ids.append(candidate.id)
            self._log_applied_decision(
                decision=decision,
                target_type="memory_unit",
                target_id=candidate.id,
                action_type="activate_l4_persona",
                applied=True,
                dry_run=False,
                metadata={
                    "memory_id": candidate.id,
                    "source_memory_ids": candidate.metadata.get("source_memory_ids", []),
                    "support_count": candidate.metadata.get("support_count"),
                    "atom_type": candidate.metadata.get("atom_type"),
                },
            )
        return {
            "status": "preview" if dry_run else "applied",
            "candidate_count": len(candidates),
            "decision_count": len(decisions),
            "applied_count": len(applied_memory_ids),
            "memory_ids": applied_memory_ids,
            "decisions": [decision.to_dict() for decision in decisions],
        }

    def _log_decisions(
        self,
        decisions: Sequence[AutonomousGovernanceDecision],
        dry_run: bool,
    ) -> None:
        for decision in decisions:
            self._log_decision(decision, dry_run=dry_run)

    def _log_decision(
        self,
        decision: AutonomousGovernanceDecision,
        dry_run: bool,
    ) -> None:
        if dry_run:
            return
        self.store.append_review_event(
            event_type=AUTONOMOUS_EVENT_EVALUATED,
            target_type=decision.target_type,
            target_id=decision.target_id,
            status="evaluated",
            metadata=_event_metadata(decision),
        )
        self.store.append_review_event(
            event_type=AUTONOMOUS_EVENT_DECIDED,
            target_type=decision.target_type,
            target_id=decision.target_id,
            status=decision.decision,
            metadata=_event_metadata(decision),
        )

    def _log_applied_decision(
        self,
        decision: AutonomousGovernanceDecision | None,
        target_type: str,
        target_id: str,
        action_type: str,
        applied: bool,
        dry_run: bool,
        metadata: dict[str, object],
    ) -> None:
        if dry_run:
            return
        event_metadata = dict(metadata)
        if decision is not None:
            event_metadata.update(_event_metadata(decision))
        else:
            event_metadata.update(
                {
                    "policy_version": AUTONOMOUS_POLICY_VERSION,
                    "action_type": action_type,
                    "fallback_mode": "not_evaluated",
                }
            )
        self.store.append_review_event(
            event_type=AUTONOMOUS_EVENT_APPLIED,
            target_type=target_type,
            target_id=target_id,
            status="applied" if applied else "not_applied",
            metadata=event_metadata,
        )

    def _log_degraded(
        self,
        target_type: str,
        target_id: str,
        action_type: str,
        reason: str,
        model_mode: str,
        dry_run: bool,
    ) -> None:
        if dry_run:
            return
        self.store.append_review_event(
            event_type=AUTONOMOUS_EVENT_DEGRADED,
            target_type=target_type,
            target_id=target_id,
            status="degraded",
            metadata={
                "policy_version": AUTONOMOUS_POLICY_VERSION,
                "action_type": action_type,
                "reason": reason,
                "model_mode": model_mode,
                "fallback_mode": "not_applicable",
            },
        )

    def _log_rollback(
        self,
        target_type: str,
        target_id: str,
        action_type: str,
        rolled_back: bool,
        reason: str,
        metadata: dict[str, object],
    ) -> None:
        event_metadata = dict(metadata)
        event_metadata.update(
            {
                "policy_version": AUTONOMOUS_POLICY_VERSION,
                "action_type": action_type,
                "reason": reason,
                "fallback_mode": "not_applicable",
            }
        )
        self.store.append_review_event(
            event_type=AUTONOMOUS_EVENT_ROLLED_BACK,
            target_type=target_type,
            target_id=target_id,
            status="rolled_back" if rolled_back else "not_rolled_back",
            metadata=event_metadata,
        )


def _decision(
    target_type: str,
    target_id: str,
    action_type: str,
    risk_tier: str,
    decision: str,
    confidence: float,
    rule_score: float,
    reasons: Sequence[str],
    evidence_memory_ids: Sequence[str],
    model_mode: str,
    fallback_mode: str,
    small_model_score: float | None = None,
    llm_judge_score: float | None = None,
    model_outputs: dict[str, object] | None = None,
    rollback_payload: dict[str, object] | None = None,
) -> AutonomousGovernanceDecision:
    decision_id = stable_id(
        "autonomous_decision",
        AUTONOMOUS_POLICY_VERSION,
        target_type,
        target_id,
        action_type,
        decision,
    )
    resolved_model_outputs = model_outputs or {
        "rule_gate": {"available": True, "score": rule_score},
        "small_model": {"available": False, "reason": "not_configured"},
        "llm_judge": {"available": False, "reason": "not_configured"},
        "model_mode": model_mode,
    }
    return AutonomousGovernanceDecision(
        id=decision_id,
        target_type=target_type,
        target_id=target_id,
        action_type=action_type,
        risk_tier=risk_tier,
        decision=decision,
        confidence=confidence,
        rule_score=rule_score,
        small_model_score=small_model_score,
        llm_judge_score=llm_judge_score,
        reasons=tuple(reasons),
        evidence_memory_ids=tuple(evidence_memory_ids),
        model_outputs=resolved_model_outputs,
        fallback_mode=fallback_mode,
        rollback_payload=rollback_payload,
    )


def _event_metadata(decision: AutonomousGovernanceDecision) -> dict[str, object]:
    return {
        "decision_id": decision.id,
        "risk_tier": decision.risk_tier,
        "action_type": decision.action_type,
        "decision": decision.decision,
        "confidence": decision.confidence,
        "rule_score": decision.rule_score,
        "small_model_score": decision.small_model_score,
        "llm_judge_score": decision.llm_judge_score,
        "policy_version": AUTONOMOUS_POLICY_VERSION,
        "model_chain": decision.model_outputs,
        "fallback_mode": decision.fallback_mode,
        "evidence_memory_ids": list(decision.evidence_memory_ids),
        "reasons": list(decision.reasons),
        "rollback_payload": decision.rollback_payload or {},
    }


def _cycle_result(
    dry_run: bool,
    model_mode: str,
    rule_fallback: bool,
    steps: dict[str, object],
    include_p1: bool = True,
) -> dict[str, object]:
    summary = {
        "decision_count": 0,
        "applied_count": 0,
        "degraded_count": 0,
        "semantic_applied_count": 0,
        "cross_session_l3_applied_count": 0,
        "l4_persona_applied_count": 0,
    }
    for name, step in steps.items():
        if not isinstance(step, dict):
            continue
        step_data = _object_dict(cast(object, step))
        summary["decision_count"] += _int_value(step_data.get("decision_count"))
        summary["applied_count"] += _int_value(step_data.get("applied_count"))
        if step_data.get("status") == "degraded":
            summary["degraded_count"] += 1
        if name == "semantic_dedup":
            summary["semantic_applied_count"] = _int_value(
                step_data.get("applied_count")
            )
        elif name == "cross_session_l3":
            summary["cross_session_l3_applied_count"] = _int_value(
                step_data.get("applied_count")
            )
        elif name == "l4_persona":
            summary["l4_persona_applied_count"] = _int_value(
                step_data.get("applied_count")
            )
    return {
        "status": "complete",
        "dry_run": dry_run,
        "model_mode": model_mode,
        "rule_fallback": rule_fallback,
        "include_p1": include_p1,
        "steps": steps,
        "summary": summary,
    }


def _model_mode(value: str) -> str:
    normalized = value.strip().lower()
    if normalized not in {"auto", "llm", "off"}:
        raise ValueError("Unsupported autonomous governance model_mode: %s" % value)
    return normalized


def _evaluate_action(
    action_type: str,
    texts: Sequence[str],
    rule_score: float,
    model_mode: str,
) -> dict[str, object]:
    small_model = {
        "available": False,
        "reason": "real_small_model_not_configured",
    }
    llm_judge = {"available": False, "reason": "not_requested"}
    if model_mode in {"auto", "llm"}:
        llm_judge = _llm_judge_action(
            action_type=action_type,
            texts=texts,
            rule_score=rule_score,
            small_model_score=None,
        )
    if llm_judge.get("available") is True:
        fallback_mode = "none"
    else:
        fallback_mode = "llm_unavailable"
    return {
        "decision": (
            str(llm_judge["decision"])
            if llm_judge.get("available") is True
            else "defer"
        ),
        "fallback_mode": fallback_mode,
        "small_model_score": None,
        "llm_judge_score": llm_judge.get("score") if llm_judge.get("available") else None,
        "model_outputs": {
            "rule_gate": {"available": True, "score": rule_score},
            "small_model": small_model,
            "llm_judge": llm_judge,
            "model_mode": model_mode,
        },
    }


def _llm_judge_action(
    action_type: str,
    texts: Sequence[str],
    rule_score: float,
    small_model_score: float | None,
) -> dict[str, object]:
    try:
        client = OpenAICompatibleClient(llm_config_from_env("governance"))
        result = client.chat_json(
            [
                {
                    "role": "system",
                    "content": (
                        "你是 ALTM 自治记忆治理审计器。只输出 JSON，字段包括 "
                        "score(0-1), decision(execute/defer/keep), reason。"
                    ),
                },
                {
                    "role": "user",
                    "content": json.dumps(
                        {
                            "action_type": action_type,
                            "texts": list(texts),
                            "rule_score": rule_score,
                            "small_model_score": small_model_score,
                        },
                        ensure_ascii=False,
                    ),
                },
            ]
        )
        score = _optional_float(result.get("score"))
        decision = str(result.get("decision", "")).strip().lower()
        if score is None or not 0.0 <= score <= 1.0:
            raise ValueError("Governance judge requires score between 0 and 1")
        if decision not in {"execute", "defer", "keep"}:
            raise ValueError("Governance judge returned unsupported decision: %s" % decision)
        return {
            "available": True,
            "kind": "openai_compatible_llm_judge",
            "score": score,
            "decision": decision,
            "raw": result,
        }
    except Exception as exc:
        return {
            "available": False,
            "reason": "llm_unavailable:%s" % exc,
            "score": None,
        }


def _optional_float(value: object) -> float | None:
    if isinstance(value, (int, float)):
        return float(value)
    return None


def _float_value(value: object) -> float:
    return float(value) if isinstance(value, (int, float)) else 0.0


def _int_value(value: object) -> int:
    return value if isinstance(value, int) else 0


def _text_value(value: object) -> str:
    return value if isinstance(value, str) else ""


def _object_dict(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        return {}
    return {
        str(key): item
        for key, item in cast(dict[object, object], value).items()
    }


def _string_tuple(value: object) -> tuple[str, ...]:
    if not isinstance(value, list):
        return ()
    return tuple(str(item) for item in cast(list[object], value))


def _cross_session_l3_candidates(
    store: SQLiteMemoryStore,
    embedding_model: str,
    threshold: float,
    limit: int,
) -> list[dict[str, object]]:
    memories = [
        memory
        for memory in store.list_memory_units(layer=MemoryLayer.L2, limit=limit)
        if memory.status not in {MemoryStatus.DELETED, MemoryStatus.TOMBSTONED}
        and isinstance(memory.metadata.get("atom_type"), str)
        and bool(memory.metadata.get("session_id"))
        and memory.metadata.get("review_status") != "rejected"
    ]
    embeddings: dict[str, list[float]] = {}
    for memory in memories:
        cached = store.get_memory_embedding(memory.id, embedding_model)
        if cached is not None and cached[0] == memory.content_hash:
            embeddings[memory.id] = cached[1]

    candidates: list[dict[str, object]] = []
    for index, left in enumerate(memories):
        left_vector = embeddings.get(left.id)
        if left_vector is None:
            continue
        for right in memories[index + 1 :]:
            if left.metadata.get("atom_type") != right.metadata.get("atom_type"):
                continue
            left_session = str(left.metadata.get("session_id") or "")
            right_session = str(right.metadata.get("session_id") or "")
            if not left_session or not right_session or left_session == right_session:
                continue
            right_vector = embeddings.get(right.id)
            if right_vector is None:
                continue
            similarity = _cosine(left_vector, right_vector)
            if similarity < threshold:
                continue
            source, target = _stable_session_pair(left, right)
            edge_id = stable_id("graph_edge", source.id, target.id, CROSS_SESSION_L3_EDGE)
            existing_edge = store.get_graph_edge(edge_id)
            existing_metadata = _object_dict(
                existing_edge.get("metadata")
                if existing_edge is not None
                else None
            )
            if (
                existing_edge is not None
                and existing_metadata.get("autonomous_l3_memory_id")
            ):
                continue
            candidates.append(
                {
                    "edge_id": edge_id,
                    "source_memory_id": source.id,
                    "target_memory_id": target.id,
                    "source_session_id": str(source.metadata.get("session_id") or "global"),
                    "target_session_id": str(target.metadata.get("session_id") or "global"),
                    "atom_type": str(source.metadata.get("atom_type") or "unknown"),
                    "similarity": similarity,
                    "embedding_model": embedding_model,
                    "source_summary": source.summary or source.content[:200],
                    "target_summary": target.summary or target.content[:200],
                }
            )
    return candidates


def _materialize_l3_scene(
    store: SQLiteMemoryStore,
    candidate: dict[str, object],
) -> MemoryUnit | None:
    source = store.get_memory_unit(str(candidate["source_memory_id"]))
    target = store.get_memory_unit(str(candidate["target_memory_id"]))
    if source is None or target is None:
        return None
    if source.status in {MemoryStatus.DELETED, MemoryStatus.TOMBSTONED}:
        return None
    if target.status in {MemoryStatus.DELETED, MemoryStatus.TOMBSTONED}:
        return None

    edge_id = store.put_memory_graph_edge(
        source_memory_id=source.id,
        target_memory_id=target.id,
        edge_type=CROSS_SESSION_L3_EDGE,
        weight=_float_value(candidate.get("similarity")),
        confidence=_float_value(candidate.get("similarity")),
        metadata={
            "atom_type": candidate["atom_type"],
            "embedding_model": candidate["embedding_model"],
            "similarity": candidate["similarity"],
            "source_session_id": candidate["source_session_id"],
            "target_session_id": candidate["target_session_id"],
            "candidate_status": "autonomous_materialized",
            "autonomous_decision": "execute",
            "autonomous_policy_version": AUTONOMOUS_POLICY_VERSION,
        },
    )
    scene_id = stable_id(
        "l3_autonomous_cross_session",
        edge_id,
        source.id,
        target.id,
    )
    existing = store.get_memory_unit(scene_id)
    if existing is not None:
        return existing
    source_summary = source.summary or source.content
    target_summary = target.summary or target.content
    atom_type = str(candidate["atom_type"])
    scene_key = "autonomous_cross_session:%s:%s:%s" % (
        atom_type,
        candidate["source_session_id"],
        candidate["target_session_id"],
    )
    content_data = {
        "scene_key": scene_key,
        "edge_id": edge_id,
        "atom_type": atom_type,
        "source_session_id": candidate["source_session_id"],
        "target_session_id": candidate["target_session_id"],
        "source_memory_ids": [source.id, target.id],
        "summaries": [source_summary, target_summary],
        "similarity": candidate["similarity"],
    }
    content = json.dumps(content_data, ensure_ascii=False, sort_keys=True)
    now = utc_now_iso()
    confidence = min(
        max(_float_value(candidate.get("similarity")), 0.0),
        1.0,
    )
    scene = MemoryUnit(
        id=scene_id,
        scope=source.scope,
        layer=MemoryLayer.L3,
        lifecycle_state=LifecycleState.LONG,
        status=MemoryStatus.ACTIVE,
        content=content,
        content_hash=sha256_text(content),
        summary="Autonomous cross-session L3 scene for %s: %s；%s"
        % (atom_type, source_summary, target_summary),
        created_at=now,
        updated_at=now,
        evidence_refs=[
            EvidenceRef(
                target_id=source.id,
                target_layer=MemoryLayer.L2,
                relation=EvidenceRelation.DERIVED_FROM,
                confidence=confidence,
            ),
            EvidenceRef(
                target_id=target.id,
                target_layer=MemoryLayer.L2,
                relation=EvidenceRelation.DERIVED_FROM,
                confidence=confidence,
            ),
        ],
        metadata={
            "scene_key": scene_key,
            "builder": "autonomous_governance_engine",
            "autonomous_decision": "execute",
            "autonomous_policy_version": AUTONOMOUS_POLICY_VERSION,
            "candidate_status": "autonomous_materialized",
            "source_edge_id": edge_id,
            "source_memory_ids": [source.id, target.id],
            "source_session_ids": [
                str(candidate["source_session_id"]),
                str(candidate["target_session_id"]),
            ],
            "atom_type": atom_type,
            "similarity": candidate["similarity"],
        },
    )
    store.put_memory_unit(scene)
    store.update_graph_edge_metadata(
        edge_id,
        {
            "autonomous_l3_memory_id": scene.id,
            "candidate_status": "autonomous_materialized",
            "materialized_at": now,
        },
    )
    return scene


def _l4_persona_candidates(
    store: SQLiteMemoryStore,
    min_support: int,
    limit: int,
) -> list[MemoryUnit]:
    groups: dict[str, list[MemoryUnit]] = {}
    for memory in store.list_memory_units(layer=MemoryLayer.L2, limit=limit):
        atom_type = str(memory.metadata.get("atom_type") or "")
        if atom_type not in PERSONA_ATOM_TYPES:
            continue
        if memory.status in {MemoryStatus.DELETED, MemoryStatus.TOMBSTONED}:
            continue
        if memory.metadata.get("review_status") == "rejected":
            continue
        groups.setdefault(atom_type, []).append(memory)

    candidates: list[MemoryUnit] = []
    for atom_type, memories in groups.items():
        if len(memories) < min_support:
            continue
        sorted_memories = sorted(memories, key=lambda memory: (memory.created_at, memory.id))
        source_ids = [memory.id for memory in sorted_memories]
        persona_key = "persona:%s" % atom_type
        persona_id = stable_id("l4_autonomous_persona", persona_key, *source_ids)
        existing = store.get_memory_unit(persona_id)
        if existing is not None and existing.status == MemoryStatus.ACTIVE:
            continue
        summaries = [memory.summary or memory.content for memory in sorted_memories]
        confidence = min(0.99, 0.70 + 0.08 * len(sorted_memories))
        content_data = {
            "persona_key": persona_key,
            "title": "Autonomous L4 persona for %s" % atom_type,
            "atom_type": atom_type,
            "source_memory_ids": source_ids,
            "summaries": summaries,
            "confidence": confidence,
        }
        content = json.dumps(content_data, ensure_ascii=False, sort_keys=True)
        now = utc_now_iso()
        candidates.append(
            MemoryUnit(
                id=persona_id,
                scope=sorted_memories[0].scope,
                layer=MemoryLayer.L4,
                lifecycle_state=LifecycleState.PERMANENT,
                status=MemoryStatus.ACTIVE,
                content=content,
                content_hash=sha256_text(content),
                summary="Autonomous L4 persona for %s: %s"
                % (atom_type, "；".join(summaries[:3])),
                created_at=now,
                updated_at=now,
                lifecycle=LifecycleMeta(protection_tier=5),
                evidence_refs=[
                    EvidenceRef(
                        target_id=memory.id,
                        target_layer=MemoryLayer.L2,
                        relation=EvidenceRelation.DERIVED_FROM,
                        confidence=confidence,
                    )
                    for memory in sorted_memories
                ],
                metadata={
                    "persona_key": persona_key,
                    "atom_type": atom_type,
                    "candidate_status": "autonomous_activated",
                    "autonomous_decision": "execute",
                    "autonomous_policy_version": AUTONOMOUS_POLICY_VERSION,
                    "source_memory_ids": source_ids,
                    "support_count": len(sorted_memories),
                    "builder": "autonomous_governance_engine",
                    "autonomous_confidence": confidence,
                },
            )
        )
    return candidates


def _stable_session_pair(left: MemoryUnit, right: MemoryUnit) -> tuple[MemoryUnit, MemoryUnit]:
    left_key = (str(left.metadata.get("session_id") or ""), left.created_at, left.id)
    right_key = (str(right.metadata.get("session_id") or ""), right.created_at, right.id)
    return (left, right) if left_key <= right_key else (right, left)


def _cosine(left: Sequence[float], right: Sequence[float]) -> float:
    if len(left) != len(right):
        return 0.0
    left_norm = sqrt(sum(float(value) * float(value) for value in left))
    right_norm = sqrt(sum(float(value) * float(value) for value in right))
    if left_norm == 0 or right_norm == 0:
        return 0.0
    return sum(
        float(lv) * float(rv) for lv, rv in zip(left, right, strict=True)
    ) / (
        left_norm * right_norm
    )
