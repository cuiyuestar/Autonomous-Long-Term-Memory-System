import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from altm.contracts import (  # noqa: E402
    L2Atom,
    L2AtomType,
    LifecycleMeta,
    LifecycleState,
    MemoryLayer,
    MemoryStatus,
    MemoryUnit,
    ReviewActionRisk,
    ReviewActionType,
    ReviewItemKind,
    ReviewStatus,
)
from altm.review import ReviewActionPlanner, ReviewQueue  # noqa: E402
from altm.storage import SQLiteMemoryStore  # noqa: E402
from altm.utils import sha256_text, utc_now_iso  # noqa: E402


class ReviewActionPlannerTest(unittest.TestCase):
    def test_plans_approved_promotion_with_second_confirmation(self) -> None:
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

            plans = ReviewActionPlanner(store).plan()

            self.assertEqual(len(plans), 1)
            self.assertEqual(plans[0].action_type, ReviewActionType.PROMOTE_TO_LONG)
            self.assertEqual(plans[0].risk, ReviewActionRisk.MEDIUM)
            self.assertTrue(plans[0].requires_second_confirmation)
            self.assertEqual(plans[0].proposed_changes["lifecycle_state"], "long")

    def test_plans_approved_duplicate_as_high_risk_resolution(self) -> None:
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

            plans = ReviewActionPlanner(store).plan()

            self.assertEqual(len(plans), 1)
            self.assertEqual(plans[0].action_type, ReviewActionType.PREPARE_DUPLICATE_RESOLUTION)
            self.assertEqual(plans[0].risk, ReviewActionRisk.HIGH)
            self.assertTrue(plans[0].requires_second_confirmation)
            self.assertEqual(plans[0].source_memory_ids, ["first", "second"])

    def test_plans_approved_cross_session_l3_candidate(self) -> None:
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

            plans = ReviewActionPlanner(store).plan()

            self.assertEqual(len(plans), 1)
            self.assertEqual(
                plans[0].action_type,
                ReviewActionType.CONFIRM_CROSS_SESSION_L3_CANDIDATE,
            )
            self.assertEqual(plans[0].risk, ReviewActionRisk.MEDIUM)
            self.assertFalse(plans[0].requires_second_confirmation)

    def test_plans_approved_l4_persona_candidate_as_high_risk_activation(self) -> None:
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
                },
            )
            store.put_memory_unit(persona)
            ReviewQueue(store).mark_memory(
                persona.id,
                ReviewStatus.APPROVED,
                kind=ReviewItemKind.L4_PERSONA_CANDIDATE,
            )

            plans = ReviewActionPlanner(store).plan()

            self.assertEqual(len(plans), 1)
            self.assertEqual(plans[0].action_type, ReviewActionType.ACTIVATE_L4_PERSONA)
            self.assertEqual(plans[0].risk, ReviewActionRisk.HIGH)
            self.assertTrue(plans[0].requires_second_confirmation)

    def test_rejected_items_are_planned_only_when_requested(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store = SQLiteMemoryStore(Path(tmpdir) / "memory.sqlite3")
            store.initialize()
            memory = _memory(
                "rejected-l2",
                MemoryLayer.L2,
                metadata={"atom_type": "decision", "review_status": "pending"},
            )
            store.put_l2_atom(_atom(memory.id), memory)
            ReviewQueue(store).mark_memory(memory.id, ReviewStatus.REJECTED)

            default_plans = ReviewActionPlanner(store).plan()
            rejected_plans = ReviewActionPlanner(store).plan(include_rejected=True)

            self.assertEqual(default_plans, [])
            self.assertEqual(len(rejected_plans), 1)
            self.assertEqual(rejected_plans[0].action_type, ReviewActionType.SUPPRESS_L2)
            self.assertEqual(rejected_plans[0].risk, ReviewActionRisk.MEDIUM)


def _memory(
    memory_id: str,
    layer: MemoryLayer,
    status: MemoryStatus = MemoryStatus.ACTIVE,
    metadata: dict[str, object] | None = None,
    lifecycle: LifecycleMeta | None = None,
) -> MemoryUnit:
    now = utc_now_iso()
    content = "review action target %s" % memory_id
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


def _atom(
    memory_id: str,
    atom_type: L2AtomType = L2AtomType.DECISION,
    review_status: ReviewStatus = ReviewStatus.PENDING,
) -> L2Atom:
    return L2Atom(
        id=memory_id,
        atom_type=atom_type,
        text="review action target %s" % memory_id,
        confidence=0.8,
        extraction_reason="test fixture",
        source_memory_id="l1-source",
        review_status=review_status,
    )


if __name__ == "__main__":
    unittest.main()
