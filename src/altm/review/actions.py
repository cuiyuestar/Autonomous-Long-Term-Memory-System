"""Review action planning.

The planner is intentionally read-only. It converts human review decisions into
explicit proposed actions so destructive or long-lived state transitions can be
confirmed separately.
"""

from __future__ import annotations

from typing import Sequence

from altm.contracts import (
    ReviewActionPlan,
    ReviewActionRisk,
    ReviewActionType,
    ReviewItemKind,
    ReviewQueueItem,
    ReviewStatus,
)
from altm.review.queue import ReviewQueue
from altm.storage import SQLiteMemoryStore
from altm.utils import stable_id


class ReviewActionPlanner:
    def __init__(self, store: SQLiteMemoryStore) -> None:
        self.store = store

    def plan(
        self,
        include_rejected: bool = False,
        limit: int = 100,
    ) -> Sequence[ReviewActionPlan]:
        review_items = ReviewQueue(self.store).list_items(
            include_reviewed=True,
            limit=max(limit, 1000),
        )
        plans: list[ReviewActionPlan] = []
        for item in _prioritize_review_items(review_items):
            if item.review_status == ReviewStatus.PENDING:
                continue
            if item.review_status == ReviewStatus.REJECTED and not include_rejected:
                continue
            plans.append(_item_to_action_plan(item))
            if len(plans) >= limit:
                break
        return plans


def _item_to_action_plan(item: ReviewQueueItem) -> ReviewActionPlan:
    if item.kind == ReviewItemKind.L2_PENDING:
        return _l2_plan(item)
    if item.kind == ReviewItemKind.PROMOTION_CANDIDATE:
        return _promotion_plan(item)
    if item.kind == ReviewItemKind.DEMOTION_CANDIDATE:
        return _demotion_plan(item)
    if item.kind == ReviewItemKind.SEMANTIC_DUPLICATE_CANDIDATE:
        return _duplicate_plan(item)
    if item.kind == ReviewItemKind.CROSS_SESSION_L3_CANDIDATE:
        return _cross_session_l3_plan(item)
    if item.kind == ReviewItemKind.L3_OBSERVING:
        return _l3_plan(item)
    if item.kind == ReviewItemKind.L4_PERSONA_CANDIDATE:
        return _l4_persona_plan(item)
    raise ValueError("Unsupported review item kind: %s" % item.kind)


def _prioritize_review_items(items: Sequence[ReviewQueueItem]) -> Sequence[ReviewQueueItem]:
    priority = {
        ReviewItemKind.PROMOTION_CANDIDATE: 0,
        ReviewItemKind.DEMOTION_CANDIDATE: 0,
        ReviewItemKind.L3_OBSERVING: 1,
        ReviewItemKind.L4_PERSONA_CANDIDATE: 1,
        ReviewItemKind.CROSS_SESSION_L3_CANDIDATE: 1,
        ReviewItemKind.SEMANTIC_DUPLICATE_CANDIDATE: 1,
        ReviewItemKind.L2_PENDING: 2,
    }
    ordered = sorted(items, key=lambda item: priority[item.kind])
    seen_memory_targets: set[tuple[str, str]] = set()
    prioritized: list[ReviewQueueItem] = []
    for item in ordered:
        key = (item.target_type, item.target_id)
        if item.kind == ReviewItemKind.L2_PENDING and key in seen_memory_targets:
            continue
        if item.target_type == "memory_unit":
            seen_memory_targets.add(key)
        prioritized.append(item)
    return prioritized


def _l2_plan(item: ReviewQueueItem) -> ReviewActionPlan:
    if item.review_status == ReviewStatus.APPROVED:
        return _plan(
            item,
            ReviewActionType.CONFIRM_L2,
            ReviewActionRisk.LOW,
            "Keep the approved L2 atom eligible for retrieval and lifecycle governance.",
            proposed_changes={"metadata.review_status": "approved"},
        )
    return _plan(
        item,
        ReviewActionType.SUPPRESS_L2,
        ReviewActionRisk.MEDIUM,
        "Keep the rejected L2 atom out of trusted context; tombstone requires separate approval.",
        proposed_changes={"metadata.review_status": "rejected", "retrieval_multiplier": "low"},
    )


def _promotion_plan(item: ReviewQueueItem) -> ReviewActionPlan:
    if item.review_status == ReviewStatus.APPROVED:
        return _plan(
            item,
            ReviewActionType.PROMOTE_TO_LONG,
            ReviewActionRisk.MEDIUM,
            "Promote this approved candidate from short-term to long-term lifecycle.",
            proposed_changes={
                "lifecycle_state": "long",
                "metadata.governance_review_status": "approved",
            },
            requires_second_confirmation=True,
        )
    return _plan(
        item,
        ReviewActionType.CLEAR_PROMOTION_CANDIDATE,
        ReviewActionRisk.LOW,
        "Clear the rejected promotion candidate marker without deleting memory.",
        proposed_changes={
            "lifecycle.promotion_candidate_since": None,
            "metadata.governance_review_status": "rejected",
        },
    )


def _demotion_plan(item: ReviewQueueItem) -> ReviewActionPlan:
    if item.review_status == ReviewStatus.APPROVED:
        return _plan(
            item,
            ReviewActionType.MARK_OBSERVING,
            ReviewActionRisk.MEDIUM,
            "Mark this approved demotion candidate as observing before compression or tombstone.",
            proposed_changes={
                "status": "observing",
                "metadata.governance_review_status": "approved",
            },
            requires_second_confirmation=True,
        )
    return _plan(
        item,
        ReviewActionType.CLEAR_DEMOTION_CANDIDATE,
        ReviewActionRisk.LOW,
        "Clear the rejected demotion candidate marker without changing memory status.",
        proposed_changes={
            "lifecycle.demotion_candidate_since": None,
            "metadata.governance_review_status": "rejected",
        },
    )


def _duplicate_plan(item: ReviewQueueItem) -> ReviewActionPlan:
    if item.review_status == ReviewStatus.APPROVED:
        return _plan(
            item,
            ReviewActionType.PREPARE_DUPLICATE_RESOLUTION,
            ReviewActionRisk.HIGH,
            "Prepare duplicate resolution; merge or tombstone still requires explicit confirmation.",
            proposed_changes={
                "graph_edge.review_status": "approved",
                "next_step": "choose canonical memory before merge/tombstone",
            },
            requires_second_confirmation=True,
        )
    return _plan(
        item,
        ReviewActionType.MARK_NOT_DUPLICATE,
        ReviewActionRisk.LOW,
        "Keep both memories and record that this duplicate candidate was rejected.",
        proposed_changes={"graph_edge.review_status": "rejected"},
    )


def _l3_plan(item: ReviewQueueItem) -> ReviewActionPlan:
    if item.review_status == ReviewStatus.APPROVED:
        return _plan(
            item,
            ReviewActionType.ACTIVATE_L3_SCENE,
            ReviewActionRisk.MEDIUM,
            "Activate this reviewed L3 scene while keeping evidence refs intact.",
            proposed_changes={
                "status": "active",
                "metadata.governance_review_status": "approved",
            },
            requires_second_confirmation=True,
        )
    return _plan(
        item,
        ReviewActionType.REJECT_L3_SCENE,
        ReviewActionRisk.MEDIUM,
        "Keep rejected L3 scene from context; tombstone requires separate approval.",
        proposed_changes={
            "metadata.governance_review_status": "rejected",
            "retrieval_multiplier": "low",
        },
    )


def _cross_session_l3_plan(item: ReviewQueueItem) -> ReviewActionPlan:
    if item.review_status == ReviewStatus.APPROVED:
        return _plan(
            item,
            ReviewActionType.CONFIRM_CROSS_SESSION_L3_CANDIDATE,
            ReviewActionRisk.MEDIUM,
            "Confirm this cross-session L3 candidate for later scene materialization.",
            proposed_changes={
                "graph_edge.review_status": "approved",
                "graph_edge.candidate_status": "confirmed",
            },
            requires_second_confirmation=False,
        )
    return _plan(
        item,
        ReviewActionType.REJECT_CROSS_SESSION_L3_CANDIDATE,
        ReviewActionRisk.LOW,
        "Reject this cross-session L3 candidate without changing source memories.",
        proposed_changes={
            "graph_edge.review_status": "rejected",
            "graph_edge.candidate_status": "rejected",
        },
    )


def _l4_persona_plan(item: ReviewQueueItem) -> ReviewActionPlan:
    if item.review_status == ReviewStatus.APPROVED:
        return _plan(
            item,
            ReviewActionType.ACTIVATE_L4_PERSONA,
            ReviewActionRisk.HIGH,
            "Activate this reviewed L4 persona candidate as permanent persona memory.",
            proposed_changes={
                "status": "active",
                "lifecycle_state": "permanent",
                "metadata.governance_review_status": "approved",
                "metadata.candidate_status": "activated",
            },
            requires_second_confirmation=True,
        )
    return _plan(
        item,
        ReviewActionType.REJECT_L4_PERSONA,
        ReviewActionRisk.MEDIUM,
        "Reject this L4 persona candidate without deleting source evidence.",
        proposed_changes={
            "metadata.governance_review_status": "rejected",
            "metadata.candidate_status": "rejected",
        },
    )


def _plan(
    item: ReviewQueueItem,
    action_type: ReviewActionType,
    risk: ReviewActionRisk,
    description: str,
    proposed_changes: dict[str, object],
    requires_second_confirmation: bool = False,
) -> ReviewActionPlan:
    plan_id = stable_id("review_action", item.id, action_type.value)
    return ReviewActionPlan(
        id=plan_id,
        review_item_id=item.id,
        action_type=action_type,
        target_type=item.target_type,
        target_id=item.target_id,
        risk=risk,
        requires_second_confirmation=requires_second_confirmation,
        description=description,
        proposed_changes=proposed_changes,
        source_memory_ids=item.source_memory_ids,
        metadata={
            "review_status": item.review_status.value,
            "review_kind": item.kind.value,
        },
    )
