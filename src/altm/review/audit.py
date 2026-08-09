"""Read-only review audit summaries."""

from __future__ import annotations

from collections import Counter

from altm.config import high_risk_flags
from altm.contracts import ReviewActionRisk, ReviewAuditSummary, ReviewStatus
from altm.review.actions import ReviewActionPlanner
from altm.review.queue import ReviewQueue
from altm.storage import SQLiteMemoryStore


class ReviewAuditReporter:
    def __init__(self, store: SQLiteMemoryStore) -> None:
        self.store = store

    def summarize(
        self,
        event_limit: int = 1000,
        recent_limit: int = 10,
    ) -> ReviewAuditSummary:
        flags = high_risk_flags()
        events = list(self.store.list_review_events(limit=event_limit))
        projections: list[dict[str, object]] = []
        if flags.enable_review_audit_projections:
            projections = list(
                self.store.rebuild_review_audit_projections()
            )[: max(event_limit, recent_limit, 0)]
        review_items = list(
            ReviewQueue(self.store).list_items(include_reviewed=True, limit=max(event_limit, 1000))
        )
        plans = list(
            ReviewActionPlanner(self.store).plan(include_rejected=True, limit=max(event_limit, 1000))
        )
        applied_plan_ids = {
            str(event.plan_id)
            for event in events
            if event.event_type == "review_apply" and event.plan_id is not None
        }
        applied_events = [event for event in events if event.event_type == "review_apply"]

        event_type_counts = Counter(event.event_type for event in events)
        target_type_counts = Counter(event.target_type for event in events)
        status_counts = Counter(event.status or "unknown" for event in events)
        recent_events = events[-recent_limit:] if recent_limit > 0 else []
        current_plan_ids = {plan.id for plan in plans}

        return ReviewAuditSummary(
            total_events=len(events),
            event_type_counts=dict(event_type_counts),
            target_type_counts=dict(target_type_counts),
            status_counts=dict(status_counts),
            pending_review_items=sum(
                1 for item in review_items if item.review_status == ReviewStatus.PENDING
            ),
            reviewed_items=sum(
                1 for item in review_items if item.review_status != ReviewStatus.PENDING
            ),
            action_plan_count=len(current_plan_ids | applied_plan_ids),
            high_risk_plan_count=(
                sum(1 for plan in plans if plan.risk == ReviewActionRisk.HIGH)
                + sum(1 for event in applied_events if event.metadata.get("risk") == "high")
            ),
            second_confirmation_required_count=sum(
                1 for plan in plans if plan.requires_second_confirmation
            )
            + sum(
                1
                for event in applied_events
                if event.metadata.get("requires_second_confirmation") is True
            ),
            applied_action_count=len(applied_plan_ids),
            unapplied_action_plan_count=sum(1 for plan in plans if plan.id not in applied_plan_ids),
            recent_events=recent_events,
            metadata={
                "event_limit": event_limit,
                "recent_limit": recent_limit,
                "event_sourcing_enabled": flags.enable_review_event_sourcing,
                "audit_projection_enabled": flags.enable_review_audit_projections,
                "projection_count": len(projections),
                "projected_event_count": sum(
                    _int_value(projection.get("event_count"))
                    for projection in projections
                ),
                "recent_projections": projections[:recent_limit] if recent_limit > 0 else [],
            },
        )


def _int_value(value: object) -> int:
    return value if isinstance(value, int) else 0
