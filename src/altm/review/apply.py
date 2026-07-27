"""Guarded application of reviewed action plans."""

from __future__ import annotations

from altm.config import high_risk_flags
from altm.contracts import (
    LifecycleMeta,
    LifecycleState,
    MemoryStatus,
    ReviewActionPlan,
    ReviewActionType,
    ReviewApplyResult,
)
from altm.review.actions import ReviewActionPlanner
from altm.storage import SQLiteMemoryStore
from altm.utils import utc_now_iso


class ReviewActionExecutor:
    def __init__(self, store: SQLiteMemoryStore) -> None:
        self.store = store

    def apply(
        self,
        plan_id: str,
        confirm: bool = False,
        second_confirm: bool = False,
    ) -> ReviewApplyResult | None:
        plan = self._find_plan(plan_id)
        if plan is None:
            return None
        if not confirm:
            return _result(plan, False, "Dry run only. Pass confirm=true to apply.")
        if plan.requires_second_confirmation and not second_confirm:
            return _result(
                plan,
                False,
                "Second confirmation required for this action.",
                {"requires_second_confirmation": True},
            )

        if plan.action_type == ReviewActionType.CONFIRM_L2:
            result = self._mark_memory_applied(plan, "Confirmed approved L2 atom.")
        elif plan.action_type == ReviewActionType.SUPPRESS_L2:
            result = self._mark_memory_applied(plan, "Suppressed rejected L2 atom from trusted use.")
        elif plan.action_type == ReviewActionType.PROMOTE_TO_LONG:
            result = self._promote_to_long(plan)
        elif plan.action_type == ReviewActionType.CLEAR_PROMOTION_CANDIDATE:
            result = self._clear_lifecycle_candidate(plan, promotion=True)
        elif plan.action_type == ReviewActionType.MARK_OBSERVING:
            result = self._mark_observing(plan)
        elif plan.action_type == ReviewActionType.CLEAR_DEMOTION_CANDIDATE:
            result = self._clear_lifecycle_candidate(plan, promotion=False)
        elif plan.action_type == ReviewActionType.PREPARE_DUPLICATE_RESOLUTION:
            result = self._prepare_duplicate_resolution(plan)
        elif plan.action_type == ReviewActionType.MARK_NOT_DUPLICATE:
            result = self._mark_graph_edge_applied(plan, "Marked duplicate candidate as not duplicate.")
        elif plan.action_type == ReviewActionType.ACTIVATE_L3_SCENE:
            result = self._activate_l3_scene(plan)
        elif plan.action_type == ReviewActionType.REJECT_L3_SCENE:
            result = self._mark_memory_applied(plan, "Rejected L3 scene remains non-authoritative.")
        elif plan.action_type == ReviewActionType.CONFIRM_CROSS_SESSION_L3_CANDIDATE:
            result = self._mark_cross_session_l3_candidate(plan, approved=True)
        elif plan.action_type == ReviewActionType.REJECT_CROSS_SESSION_L3_CANDIDATE:
            result = self._mark_cross_session_l3_candidate(plan, approved=False)
        elif plan.action_type == ReviewActionType.ACTIVATE_L4_PERSONA:
            result = self._activate_l4_persona(plan)
        elif plan.action_type == ReviewActionType.REJECT_L4_PERSONA:
            result = self._reject_l4_persona(plan)
        else:
            raise ValueError("Unsupported review action type: %s" % plan.action_type)
        if result.applied and high_risk_flags().enable_review_event_sourcing:
            self.store.append_review_event(
                event_type="review_apply",
                target_type=result.target_type,
                target_id=result.target_id,
                review_item_id=plan.review_item_id,
                plan_id=plan.id,
                status="applied",
                metadata={
                    "action_type": plan.action_type.value,
                    "risk": plan.risk.value,
                    "requires_second_confirmation": plan.requires_second_confirmation,
                    "message": result.message,
                },
            )
        return result

    def _find_plan(self, plan_id: str) -> ReviewActionPlan | None:
        for plan in ReviewActionPlanner(self.store).plan(include_rejected=True, limit=5000):
            if plan.id == plan_id:
                return plan
        return None

    def _mark_memory_applied(self, plan: ReviewActionPlan, message: str) -> ReviewApplyResult:
        updated = self.store.update_memory_metadata(
            plan.target_id,
            _applied_metadata(plan),
        )
        return _result(
            plan,
            updated is not None,
            message if updated is not None else "Target memory not found.",
        )

    def _promote_to_long(self, plan: ReviewActionPlan) -> ReviewApplyResult:
        memory = self.store.get_memory_unit(plan.target_id)
        if memory is None:
            return _result(plan, False, "Target memory not found.")
        metadata = dict(memory.metadata)
        metadata.update(_applied_metadata(plan))
        metadata["governance_review_status"] = "approved"
        lifecycle = LifecycleMeta(
            age=memory.lifecycle.age,
            protection_tier=max(memory.lifecycle.protection_tier, 4),
            compression_tier=memory.lifecycle.compression_tier,
            observation_until=memory.lifecycle.observation_until,
            promotion_candidate_since=None,
            demotion_candidate_since=memory.lifecycle.demotion_candidate_since,
        )
        updated = memory.model_copy(
            update={
                "lifecycle_state": LifecycleState.LONG,
                "lifecycle": lifecycle,
                "metadata": metadata,
                "updated_at": utc_now_iso(),
            }
        )
        self.store.put_memory_unit(updated)
        return _result(plan, True, "Promoted memory to long lifecycle.")

    def _clear_lifecycle_candidate(
        self,
        plan: ReviewActionPlan,
        promotion: bool,
    ) -> ReviewApplyResult:
        memory = self.store.get_memory_unit(plan.target_id)
        if memory is None:
            return _result(plan, False, "Target memory not found.")
        metadata = dict(memory.metadata)
        metadata.update(_applied_metadata(plan))
        lifecycle = LifecycleMeta(
            age=memory.lifecycle.age,
            protection_tier=memory.lifecycle.protection_tier,
            compression_tier=memory.lifecycle.compression_tier,
            observation_until=memory.lifecycle.observation_until,
            promotion_candidate_since=None if promotion else memory.lifecycle.promotion_candidate_since,
            demotion_candidate_since=None if not promotion else memory.lifecycle.demotion_candidate_since,
        )
        updated = memory.model_copy(
            update={
                "lifecycle": lifecycle,
                "metadata": metadata,
                "updated_at": utc_now_iso(),
            }
        )
        self.store.put_memory_unit(updated)
        return _result(plan, True, "Cleared lifecycle candidate marker.")

    def _mark_observing(self, plan: ReviewActionPlan) -> ReviewApplyResult:
        memory = self.store.get_memory_unit(plan.target_id)
        if memory is None:
            return _result(plan, False, "Target memory not found.")
        metadata = dict(memory.metadata)
        metadata.update(_applied_metadata(plan))
        metadata["governance_review_status"] = "approved"
        updated = memory.model_copy(
            update={
                "status": MemoryStatus.OBSERVING,
                "metadata": metadata,
                "updated_at": utc_now_iso(),
            }
        )
        self.store.put_memory_unit(updated)
        return _result(plan, True, "Marked memory as observing.")

    def _activate_l3_scene(self, plan: ReviewActionPlan) -> ReviewApplyResult:
        memory = self.store.get_memory_unit(plan.target_id)
        if memory is None:
            return _result(plan, False, "Target memory not found.")
        metadata = dict(memory.metadata)
        metadata.update(_applied_metadata(plan))
        metadata["governance_review_status"] = "approved"
        updated = memory.model_copy(
            update={
                "status": MemoryStatus.ACTIVE,
                "metadata": metadata,
                "updated_at": utc_now_iso(),
            }
        )
        self.store.put_memory_unit(updated)
        return _result(plan, True, "Activated L3 scene.")

    def _prepare_duplicate_resolution(self, plan: ReviewActionPlan) -> ReviewApplyResult:
        edge = self.store.update_graph_edge_metadata(
            plan.target_id,
            {
                **_applied_metadata(plan),
                "resolution_status": "pending_canonical_selection",
                "destructive_action_allowed": False,
            },
        )
        return _result(
            plan,
            edge is not None,
            (
                "Prepared duplicate resolution; choose canonical memory before merge/tombstone."
                if edge is not None
                else "Target graph edge not found."
            ),
        )

    def _mark_graph_edge_applied(self, plan: ReviewActionPlan, message: str) -> ReviewApplyResult:
        edge = self.store.update_graph_edge_metadata(
            plan.target_id,
            _applied_metadata(plan),
        )
        return _result(plan, edge is not None, message if edge is not None else "Target edge not found.")

    def _mark_cross_session_l3_candidate(
        self,
        plan: ReviewActionPlan,
        approved: bool,
    ) -> ReviewApplyResult:
        edge = self.store.update_graph_edge_metadata(
            plan.target_id,
            {
                **_applied_metadata(plan),
                "review_status": "approved" if approved else "rejected",
                "candidate_status": "confirmed" if approved else "rejected",
            },
        )
        message = (
            "Confirmed cross-session L3 candidate."
            if approved
            else "Rejected cross-session L3 candidate."
        )
        return _result(plan, edge is not None, message if edge is not None else "Target edge not found.")

    def _activate_l4_persona(self, plan: ReviewActionPlan) -> ReviewApplyResult:
        memory = self.store.get_memory_unit(plan.target_id)
        if memory is None:
            return _result(plan, False, "Target memory not found.")
        metadata = dict(memory.metadata)
        metadata.update(_applied_metadata(plan))
        metadata["governance_review_status"] = "approved"
        metadata["candidate_status"] = "activated"
        lifecycle = LifecycleMeta(
            age=memory.lifecycle.age,
            protection_tier=max(memory.lifecycle.protection_tier, 5),
            compression_tier=memory.lifecycle.compression_tier,
            observation_until=memory.lifecycle.observation_until,
            promotion_candidate_since=None,
            demotion_candidate_since=None,
        )
        updated = memory.model_copy(
            update={
                "lifecycle_state": LifecycleState.PERMANENT,
                "status": MemoryStatus.ACTIVE,
                "metadata": metadata,
                "lifecycle": lifecycle,
                "updated_at": utc_now_iso(),
            }
        )
        self.store.put_memory_unit(updated)
        return _result(plan, True, "Activated L4 persona candidate.")

    def _reject_l4_persona(self, plan: ReviewActionPlan) -> ReviewApplyResult:
        updated = self.store.update_memory_metadata(
            plan.target_id,
            {
                **_applied_metadata(plan),
                "governance_review_status": "rejected",
                "candidate_status": "rejected",
            },
        )
        return _result(
            plan,
            updated is not None,
            "Rejected L4 persona candidate." if updated is not None else "Target memory not found.",
        )


def _applied_metadata(plan: ReviewActionPlan) -> dict[str, object]:
    return {
        "review_action_applied_at": utc_now_iso(),
        "review_action_id": plan.id,
        "review_action_type": plan.action_type.value,
    }


def _result(
    plan: ReviewActionPlan,
    applied: bool,
    message: str,
    metadata: dict[str, object] | None = None,
) -> ReviewApplyResult:
    return ReviewApplyResult(
        plan=plan,
        applied=applied,
        message=message,
        target_type=plan.target_type,
        target_id=plan.target_id,
        metadata=metadata or {},
    )
