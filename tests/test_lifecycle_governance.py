from pathlib import Path
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from altm.contracts import (  # noqa: E402
    AccessSignal,
    LifecycleState,
    MemoryLayer,
    MemoryStatus,
    MemoryUnit,
    RecallQuery,
)
from altm.lifecycle import LifecycleGovernor  # noqa: E402
from altm.retrieval import FTSRetrievalEngine  # noqa: E402
from altm.storage import SQLiteMemoryStore  # noqa: E402
from altm.utils import sha256_text, utc_now_iso  # noqa: E402


class LifecycleGovernanceTest(unittest.TestCase):
    def test_governance_marks_promotion_candidate_from_useful_feedback(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store = SQLiteMemoryStore(Path(tmpdir) / "memory.sqlite3")
            store.initialize()
            memory = _memory(
                "promote",
                "validated lifecycle memory",
                metadata={"review_status": "approved"},
            )
            store.put_memory_unit(memory)
            for _ in range(4):
                store.record_access_signal(memory.id, AccessSignal.USER_CONFIRMED)

            updated = LifecycleGovernor(store).run_cycle()
            governed = store.get_memory_unit(memory.id)

            self.assertEqual(len(updated), 1)
            self.assertIsNotNone(governed)
            self.assertGreaterEqual(governed.score.resident_score, 0.70)
            self.assertIsNotNone(governed.lifecycle.promotion_candidate_since)
            self.assertIsNone(governed.lifecycle.demotion_candidate_since)
            self.assertGreaterEqual(governed.lifecycle.protection_tier, 4)

    def test_governance_marks_rejected_stale_memory_as_demotion_candidate(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store = SQLiteMemoryStore(Path(tmpdir) / "memory.sqlite3")
            store.initialize()
            memory = _memory(
                "demote",
                "stale rejected lifecycle memory",
                updated_at="2020-01-01T00:00:00+00:00",
                metadata={"review_status": "rejected"},
            )
            store.put_memory_unit(memory)

            LifecycleGovernor(store).run_cycle()
            governed = store.get_memory_unit(memory.id)

            self.assertIsNotNone(governed)
            self.assertLessEqual(governed.score.resident_score, 0.22)
            self.assertIsNotNone(governed.lifecycle.demotion_candidate_since)
            self.assertIsNone(governed.lifecycle.promotion_candidate_since)

    def test_pending_l2_is_downweighted_in_retrieval(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store = SQLiteMemoryStore(Path(tmpdir) / "memory.sqlite3")
            store.initialize()
            query_text = "shared governance target"
            store.put_memory_unit(
                _memory(
                    "pending",
                    query_text,
                    metadata={"review_status": "pending"},
                )
            )
            store.put_memory_unit(
                _memory(
                    "approved",
                    query_text,
                    metadata={"review_status": "approved"},
                )
            )

            recalled = FTSRetrievalEngine(store).recall(RecallQuery(text=query_text, top_k=2))

            self.assertEqual([candidate.memory.id for candidate in recalled], ["approved", "pending"])
            self.assertGreater(recalled[0].score.retrieval_score, recalled[1].score.retrieval_score)


def _memory(
    memory_id: str,
    content: str,
    updated_at: str | None = None,
    metadata: dict[str, object] | None = None,
) -> MemoryUnit:
    now = utc_now_iso()
    return MemoryUnit(
        id=memory_id,
        layer=MemoryLayer.L2,
        lifecycle_state=LifecycleState.SHORT,
        status=MemoryStatus.ACTIVE,
        content=content,
        content_hash=sha256_text(content),
        summary=content,
        created_at=updated_at or now,
        updated_at=updated_at or now,
        metadata=metadata or {},
    )


if __name__ == "__main__":
    unittest.main()
