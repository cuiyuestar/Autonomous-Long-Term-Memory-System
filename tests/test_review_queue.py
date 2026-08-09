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
    ReviewItemKind,
    ReviewStatus,
)
from altm.review import ReviewQueue  # noqa: E402
from altm.storage import SQLiteMemoryStore  # noqa: E402
from altm.utils import sha256_text, utc_now_iso  # noqa: E402


class ReviewQueueTest(unittest.TestCase):
    def test_review_queue_lists_governance_candidates(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store = SQLiteMemoryStore(Path(tmpdir) / "memory.sqlite3")
            store.initialize()
            l2 = _memory(
                "l2-pending",
                MemoryLayer.L2,
                metadata={"atom_type": "decision", "review_status": "pending"},
            )
            l3 = _memory(
                "l3-observing",
                MemoryLayer.L3,
                status=MemoryStatus.OBSERVING,
                metadata={"source_memory_ids": ["l2-pending"]},
            )
            l4 = _memory(
                "l4-persona",
                MemoryLayer.L4,
                status=MemoryStatus.OBSERVING,
                metadata={
                    "builder": "l4_persona_candidate_builder",
                    "source_memory_ids": ["l2-pending", "promotion"],
                    "governance_review_status": "pending",
                },
            )
            promotion = _memory(
                "promotion",
                MemoryLayer.L2,
                metadata={"atom_type": "lesson", "review_status": "approved"},
                lifecycle=LifecycleMeta(promotion_candidate_since=utc_now_iso()),
            )
            for memory in (l2, l3, l4, promotion):
                store.put_memory_unit(memory)
            edge_id = store.put_memory_graph_edge(
                source_memory_id=l2.id,
                target_memory_id=promotion.id,
                edge_type="semantic_duplicate_candidate",
                weight=0.96,
                confidence=0.96,
                metadata={"similarity": 0.96},
            )
            l3_edge_id = store.put_memory_graph_edge(
                source_memory_id=l2.id,
                target_memory_id=promotion.id,
                edge_type="cross_session_l3_candidate",
                weight=0.88,
                confidence=0.88,
                metadata={
                    "similarity": 0.88,
                    "source_session_id": "s1",
                    "target_session_id": "s2",
                },
            )

            items = ReviewQueue(store).list_items(limit=10)

            self.assertEqual(
                {
                    ReviewItemKind.L2_PENDING,
                    ReviewItemKind.L3_OBSERVING,
                    ReviewItemKind.L4_PERSONA_CANDIDATE,
                    ReviewItemKind.PROMOTION_CANDIDATE,
                    ReviewItemKind.SEMANTIC_DUPLICATE_CANDIDATE,
                    ReviewItemKind.CROSS_SESSION_L3_CANDIDATE,
                },
                {item.kind for item in items},
            )
            duplicate = [
                item for item in items if item.kind == ReviewItemKind.SEMANTIC_DUPLICATE_CANDIDATE
            ][0]
            self.assertEqual(duplicate.target_id, edge_id)
            self.assertEqual(duplicate.source_memory_ids, ["l2-pending", "promotion"])
            cross_session = [
                item for item in items if item.kind == ReviewItemKind.CROSS_SESSION_L3_CANDIDATE
            ][0]
            self.assertEqual(cross_session.target_id, l3_edge_id)
            self.assertEqual(cross_session.source_memory_ids, ["l2-pending", "promotion"])
            persona = [item for item in items if item.kind == ReviewItemKind.L4_PERSONA_CANDIDATE][0]
            self.assertEqual(persona.target_id, "l4-persona")
            self.assertEqual(persona.source_memory_ids, ["l2-pending", "promotion"])

    def test_mark_memory_and_graph_edge_review_status(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store = SQLiteMemoryStore(Path(tmpdir) / "memory.sqlite3")
            store.initialize()
            l2 = _memory(
                "l2-pending",
                MemoryLayer.L2,
                metadata={"atom_type": "decision", "review_status": "pending"},
            )
            other = _memory("l2-other", MemoryLayer.L2, metadata={"atom_type": "decision"})
            store.put_l2_atom(_atom(l2.id), l2)
            store.put_memory_unit(other)
            edge_id = store.put_memory_graph_edge(
                source_memory_id=l2.id,
                target_memory_id=other.id,
                edge_type="semantic_duplicate_candidate",
                weight=0.96,
                confidence=0.96,
            )

            queue = ReviewQueue(store)
            reviewed_memory = queue.mark_memory(l2.id, ReviewStatus.APPROVED, note="verified")
            reviewed_edge = queue.mark_graph_edge(edge_id, ReviewStatus.REJECTED, note="not duplicate")

            stored_l2 = store.get_memory_unit(l2.id)
            edges = store.list_graph_edges("semantic_duplicate_candidate")
            memory_events = store.list_review_events(target_type="memory_unit", target_id=l2.id)
            edge_events = store.list_review_events(target_type="graph_edge", target_id=edge_id)

            self.assertIsNotNone(reviewed_memory)
            self.assertEqual(reviewed_memory.review_status, ReviewStatus.APPROVED)
            self.assertIsNotNone(stored_l2)
            self.assertEqual(stored_l2.metadata["review_status"], "approved")
            self.assertEqual(stored_l2.metadata["review_note"], "verified")
            with store.connect() as connection:
                row = connection.execute(
                    "SELECT review_status FROM l2_decisions WHERE memory_unit_id = ?",
                    (l2.id,),
                ).fetchone()
            self.assertEqual(row["review_status"], "approved")
            self.assertIsNotNone(reviewed_edge)
            self.assertEqual(reviewed_edge.review_status, ReviewStatus.REJECTED)
            self.assertEqual(edges[0]["metadata"]["review_status"], "rejected")
            self.assertEqual(edges[0]["metadata"]["review_note"], "not duplicate")
            self.assertEqual(memory_events[0].event_type, "review_mark")
            self.assertEqual(memory_events[0].status, "approved")
            self.assertEqual(edge_events[0].event_type, "review_mark")
            self.assertEqual(edge_events[0].status, "rejected")

    def test_mark_l2_fails_when_typed_row_is_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store = SQLiteMemoryStore(Path(tmpdir) / "memory.sqlite3")
            store.initialize()
            l2 = _memory(
                "l2-missing-typed-row",
                MemoryLayer.L2,
                metadata={"atom_type": "decision", "review_status": "pending"},
            )
            store.put_memory_unit(l2)

            with self.assertRaises(RuntimeError):
                ReviewQueue(store).mark_memory(l2.id, ReviewStatus.APPROVED)

            stored_l2 = store.get_memory_unit(l2.id)
            events = store.list_review_events(target_type="memory_unit", target_id=l2.id)

            self.assertIsNotNone(stored_l2)
            self.assertEqual(stored_l2.metadata["review_status"], "pending")
            self.assertEqual(events, [])


def _memory(
    memory_id: str,
    layer: MemoryLayer,
    status: MemoryStatus = MemoryStatus.ACTIVE,
    metadata: dict[str, object] | None = None,
    lifecycle: LifecycleMeta | None = None,
) -> MemoryUnit:
    now = utc_now_iso()
    content = "review target %s" % memory_id
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
        text="review target %s" % memory_id,
        confidence=0.8,
        extraction_reason="test fixture",
        source_memory_id="l1-source",
        review_status=review_status,
    )


if __name__ == "__main__":
    unittest.main()
