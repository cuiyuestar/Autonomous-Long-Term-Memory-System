import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from altm.contracts import (  # noqa: E402
    LifecycleMeta,
    LifecycleState,
    MemoryLayer,
    MemoryStatus,
    MemoryUnit,
    ReviewActionType,
    ReviewItemKind,
    ReviewStatus,
)
from altm.review import ReviewActionExecutor, ReviewActionPlanner, ReviewQueue  # noqa: E402
from altm.storage import SQLiteMemoryStore  # noqa: E402
from altm.utils import sha256_text, utc_now_iso  # noqa: E402


class ReviewActionExecutorTest(unittest.TestCase):
    def test_apply_promotion_requires_confirm_and_second_confirm(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store = SQLiteMemoryStore(Path(tmpdir) / "memory.sqlite3")
            store.initialize()
            memory = _memory(
                "promotion",
                MemoryLayer.L2,
                metadata={"atom_type": "lesson", "review_status": "approved"},
                lifecycle=LifecycleMeta(promotion_candidate_since=utc_now_iso()),
            )
            store.put_memory_unit(memory)
            ReviewQueue(store).mark_memory(
                memory.id,
                ReviewStatus.APPROVED,
                kind=ReviewItemKind.PROMOTION_CANDIDATE,
            )
            events_after_mark = store.list_review_events(target_id=memory.id)
            plan = ReviewActionPlanner(store).plan()[0]
            executor = ReviewActionExecutor(store)

            dry_run = executor.apply(plan.id)
            events_after_dry_run = store.list_review_events(target_id=memory.id)
            no_second = executor.apply(plan.id, confirm=True)
            events_after_no_second = store.list_review_events(target_id=memory.id)
            applied = executor.apply(plan.id, confirm=True, second_confirm=True)
            updated = store.get_memory_unit(memory.id)
            events_after_apply = store.list_review_events(target_id=memory.id)

            self.assertIsNotNone(dry_run)
            self.assertFalse(dry_run.applied)
            self.assertIsNotNone(no_second)
            self.assertFalse(no_second.applied)
            self.assertIsNotNone(applied)
            self.assertTrue(applied.applied)
            self.assertIsNotNone(updated)
            self.assertEqual(updated.lifecycle_state, LifecycleState.LONG)
            self.assertIsNone(updated.lifecycle.promotion_candidate_since)
            self.assertEqual(updated.metadata["review_action_type"], ReviewActionType.PROMOTE_TO_LONG.value)
            self.assertEqual(len(events_after_mark), 1)
            self.assertEqual(len(events_after_dry_run), 1)
            self.assertEqual(len(events_after_no_second), 1)
            self.assertEqual(len(events_after_apply), 2)
            self.assertEqual(events_after_apply[-1].event_type, "review_apply")
            self.assertEqual(events_after_apply[-1].plan_id, plan.id)

    def test_apply_duplicate_resolution_does_not_tombstone_memories(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store = SQLiteMemoryStore(Path(tmpdir) / "memory.sqlite3")
            store.initialize()
            first = _memory("first", MemoryLayer.L2, metadata={"atom_type": "decision"})
            second = _memory("second", MemoryLayer.L2, metadata={"atom_type": "decision"})
            store.put_memory_unit(first)
            store.put_memory_unit(second)
            edge_id = store.put_memory_graph_edge(
                source_memory_id=first.id,
                target_memory_id=second.id,
                edge_type="semantic_duplicate_candidate",
                weight=0.97,
                confidence=0.97,
            )
            ReviewQueue(store).mark_graph_edge(edge_id, ReviewStatus.APPROVED)
            plan = ReviewActionPlanner(store).plan()[0]

            result = ReviewActionExecutor(store).apply(
                plan.id,
                confirm=True,
                second_confirm=True,
            )
            edges = store.list_graph_edges("semantic_duplicate_candidate")
            events = store.list_review_events(target_type="graph_edge", target_id=edge_id)
            first_after = store.get_memory_unit(first.id)
            second_after = store.get_memory_unit(second.id)

            self.assertIsNotNone(result)
            self.assertTrue(result.applied)
            self.assertEqual(edges[0]["metadata"]["resolution_status"], "pending_canonical_selection")
            self.assertFalse(edges[0]["metadata"]["destructive_action_allowed"])
            self.assertEqual(first_after.status, MemoryStatus.ACTIVE)
            self.assertEqual(second_after.status, MemoryStatus.ACTIVE)
            self.assertEqual([event.event_type for event in events], ["review_mark", "review_apply"])

    def test_apply_cross_session_l3_candidate_confirms_edge_only(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store = SQLiteMemoryStore(Path(tmpdir) / "memory.sqlite3")
            store.initialize()
            first = _memory("first", MemoryLayer.L2, metadata={"atom_type": "decision"})
            second = _memory("second", MemoryLayer.L2, metadata={"atom_type": "decision"})
            store.put_memory_unit(first)
            store.put_memory_unit(second)
            edge_id = store.put_memory_graph_edge(
                source_memory_id=first.id,
                target_memory_id=second.id,
                edge_type="cross_session_l3_candidate",
                weight=0.88,
                confidence=0.88,
                metadata={"similarity": 0.88},
            )
            ReviewQueue(store).mark_graph_edge(edge_id, ReviewStatus.APPROVED)
            plan = ReviewActionPlanner(store).plan()[0]

            result = ReviewActionExecutor(store).apply(plan.id, confirm=True)
            edge = store.get_graph_edge(edge_id)
            events = store.list_review_events(target_type="graph_edge", target_id=edge_id)

            self.assertIsNotNone(result)
            self.assertTrue(result.applied)
            self.assertIsNotNone(edge)
            self.assertEqual(edge["metadata"]["review_status"], "approved")
            self.assertEqual(edge["metadata"]["candidate_status"], "confirmed")
            self.assertEqual([event.event_type for event in events], ["review_mark", "review_apply"])

    def test_apply_l4_persona_candidate_activates_permanent_memory(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store = SQLiteMemoryStore(Path(tmpdir) / "memory.sqlite3")
            store.initialize()
            persona = _memory(
                "l4-persona",
                MemoryLayer.L4,
                status=MemoryStatus.OBSERVING,
                metadata={
                    "builder": "l4_persona_candidate_builder",
                    "governance_review_status": "pending",
                    "candidate_status": "candidate",
                },
            )
            store.put_memory_unit(persona)
            ReviewQueue(store).mark_memory(
                persona.id,
                ReviewStatus.APPROVED,
                kind=ReviewItemKind.L4_PERSONA_CANDIDATE,
            )
            plan = ReviewActionPlanner(store).plan()[0]

            no_second = ReviewActionExecutor(store).apply(plan.id, confirm=True)
            applied = ReviewActionExecutor(store).apply(
                plan.id,
                confirm=True,
                second_confirm=True,
            )
            updated = store.get_memory_unit(persona.id)

            self.assertIsNotNone(no_second)
            self.assertFalse(no_second.applied)
            self.assertIsNotNone(applied)
            self.assertTrue(applied.applied)
            self.assertIsNotNone(updated)
            self.assertEqual(updated.status, MemoryStatus.ACTIVE)
            self.assertEqual(updated.lifecycle_state, LifecycleState.PERMANENT)
            self.assertEqual(updated.metadata["candidate_status"], "activated")
            self.assertEqual(updated.lifecycle.protection_tier, 5)


def _memory(
    memory_id: str,
    layer: MemoryLayer,
    status: MemoryStatus = MemoryStatus.ACTIVE,
    metadata: dict[str, object] | None = None,
    lifecycle: LifecycleMeta | None = None,
) -> MemoryUnit:
    now = utc_now_iso()
    content = "review apply target %s" % memory_id
    return MemoryUnit(
        id=memory_id,
        layer=layer,
        lifecycle_state=LifecycleState.SHORT,
        status=status,
        content=content,
        content_hash=sha256_text(content),
        summary=content,
        created_at=now,
        updated_at=now,
        metadata=metadata or {},
        lifecycle=lifecycle or LifecycleMeta(),
    )


if __name__ == "__main__":
    unittest.main()
