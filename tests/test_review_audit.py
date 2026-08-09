import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from altm.contracts import (  # noqa: E402
    LifecycleMeta,
    LifecycleState,
    MemoryLayer,
    MemoryStatus,
    MemoryUnit,
    ReviewItemKind,
    ReviewStatus,
)
from altm.review import (  # noqa: E402
    ReviewActionExecutor,
    ReviewActionPlanner,
    ReviewAuditReporter,
    ReviewQueue,
)
from altm.storage import SQLiteMemoryStore  # noqa: E402
from altm.utils import sha256_text, utc_now_iso  # noqa: E402


class ReviewAuditReporterTest(unittest.TestCase):
    def test_summarizes_review_events_and_action_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store = SQLiteMemoryStore(Path(tmpdir) / "memory.sqlite3")
            store.initialize()
            promotion = _memory(
                "promotion",
                metadata={"atom_type": "lesson", "review_status": "approved"},
                lifecycle=LifecycleMeta(promotion_candidate_since=utc_now_iso()),
            )
            pending = _memory(
                "pending",
                metadata={"atom_type": "decision", "review_status": "pending"},
            )
            store.put_memory_unit(promotion)
            store.put_memory_unit(pending)
            queue = ReviewQueue(store)
            queue.mark_memory(
                promotion.id,
                ReviewStatus.APPROVED,
                kind=ReviewItemKind.PROMOTION_CANDIDATE,
            )
            plan = ReviewActionPlanner(store).plan()[0]
            ReviewActionExecutor(store).apply(plan.id, confirm=True, second_confirm=True)

            summary = ReviewAuditReporter(store).summarize(event_limit=100, recent_limit=1)

            self.assertEqual(summary.total_events, 2)
            self.assertEqual(summary.event_type_counts["review_mark"], 1)
            self.assertEqual(summary.event_type_counts["review_apply"], 1)
            self.assertEqual(summary.target_type_counts["memory_unit"], 2)
            self.assertEqual(summary.status_counts["approved"], 1)
            self.assertEqual(summary.status_counts["applied"], 1)
            self.assertGreaterEqual(summary.pending_review_items, 1)
            self.assertGreaterEqual(summary.reviewed_items, 1)
            self.assertGreaterEqual(summary.action_plan_count, 1)
            self.assertGreaterEqual(summary.second_confirmation_required_count, 1)
            self.assertEqual(summary.applied_action_count, 1)
            self.assertEqual(len(summary.recent_events), 1)
            self.assertEqual(summary.recent_events[0].event_type, "review_apply")
            self.assertEqual(summary.metadata["projection_count"], 1)
            self.assertEqual(summary.metadata["projected_event_count"], 2)
            projection = summary.metadata["recent_projections"][0]
            self.assertEqual(projection["target_type"], "memory_unit")
            self.assertEqual(projection["target_id"], "promotion")
            self.assertEqual(projection["event_count"], 2)
            self.assertEqual(projection["review_mark_count"], 1)
            self.assertEqual(projection["review_apply_count"], 1)
            self.assertEqual(projection["last_status"], "applied")

    def test_review_event_sourcing_can_be_disabled_by_flag(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store = SQLiteMemoryStore(Path(tmpdir) / "memory.sqlite3")
            store.initialize()
            memory = _memory(
                "pending",
                metadata={},
            )
            store.put_memory_unit(memory)

            with patch.dict(
                "os.environ",
                {"ALTM_ENABLE_REVIEW_EVENT_SOURCING": "false"},
            ):
                ReviewQueue(store).mark_memory(memory.id, ReviewStatus.APPROVED)

            events = store.list_review_events(target_type="memory_unit", target_id=memory.id)
            projections = store.list_review_audit_projections()
            self.assertEqual(events, [])
            self.assertEqual(projections, [])

    def test_review_audit_projection_can_be_disabled_by_flag(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store = SQLiteMemoryStore(Path(tmpdir) / "memory.sqlite3")
            store.initialize()
            memory = _memory(
                "target",
                metadata={},
            )
            store.put_memory_unit(memory)

            with patch.dict(
                "os.environ",
                {"ALTM_ENABLE_REVIEW_AUDIT_PROJECTIONS": "false"},
            ):
                ReviewQueue(store).mark_memory(memory.id, ReviewStatus.APPROVED)
                summary = ReviewAuditReporter(store).summarize(event_limit=100, recent_limit=1)

            events = store.list_review_events(target_type="memory_unit", target_id=memory.id)
            projections = store.list_review_audit_projections()
            self.assertEqual(len(events), 1)
            self.assertEqual(projections, [])
            self.assertEqual(summary.metadata["projection_count"], 0)
            self.assertFalse(summary.metadata["audit_projection_enabled"])


def _memory(
    memory_id: str,
    metadata: dict[str, object],
    lifecycle: LifecycleMeta | None = None,
) -> MemoryUnit:
    now = utc_now_iso()
    content = "review audit target %s" % memory_id
    return MemoryUnit(
        id=memory_id,
        layer=MemoryLayer.L2,
        lifecycle_state=LifecycleState.SHORT,
        status=MemoryStatus.ACTIVE,
        content=content,
        content_hash=sha256_text(content),
        summary=content,
        created_at=now,
        updated_at=now,
        metadata=metadata,
        lifecycle=lifecycle or LifecycleMeta(),
    )


if __name__ == "__main__":
    unittest.main()
