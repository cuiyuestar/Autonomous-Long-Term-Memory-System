import tempfile
import unittest
from pathlib import Path

from altm.application import AltmApplication
from altm.contracts import (
    LifecycleState,
    MemoryLayer,
    MemoryScope,
    MemoryStatus,
    MemoryUnit,
    ScoreBreakdown,
)
from altm.storage import SQLiteMemoryStore
from altm.utils import sha256_text, utc_now_iso


class RecallSessionPolicyTest(unittest.TestCase):
    def test_non_strict_query_recall_crosses_sessions_only_for_l2_l4(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "memory.sqlite3"
            scope = MemoryScope()
            store = SQLiteMemoryStore(db_path, scope=scope)
            store.initialize()
            for memory in (
                _memory("current-l0", MemoryLayer.L0, "current"),
                _memory("other-l0", MemoryLayer.L0, "other"),
                _memory("other-l1", MemoryLayer.L1, "other"),
                _memory("other-l2", MemoryLayer.L2, "other"),
                _memory("other-l3", MemoryLayer.L3, "other"),
                _memory("other-l4", MemoryLayer.L4, "other"),
                _memory(
                    "deleted-l3",
                    MemoryLayer.L3,
                    "other",
                    status=MemoryStatus.DELETED,
                ),
                _memory(
                    "rejected-l2",
                    MemoryLayer.L2,
                    "other",
                    review_status="rejected",
                ),
            ):
                store.put_memory_unit(memory)

            bundle = AltmApplication(db_path).build_context(
                query="sessionpolicy",
                limit=20,
                session_id="current",
                strict_session=False,
                active_window_mode="off",
                scope=scope,
            )

            self.assertEqual(
                {item.source_memory_ids[0] for item in bundle.items},
                {"current-l0", "other-l2", "other-l3", "other-l4"},
            )

    def test_strict_query_recall_keeps_every_layer_in_current_session(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "memory.sqlite3"
            scope = MemoryScope()
            store = SQLiteMemoryStore(db_path, scope=scope)
            store.initialize()
            for memory in (
                _memory("current-l2", MemoryLayer.L2, "current"),
                _memory("other-l2", MemoryLayer.L2, "other"),
                _memory("other-l4", MemoryLayer.L4, "other"),
            ):
                store.put_memory_unit(memory)

            bundle = AltmApplication(db_path).build_context(
                query="sessionpolicy",
                limit=20,
                session_id="current",
                strict_session=True,
                active_window_mode="off",
                scope=scope,
            )

            self.assertEqual(
                [item.source_memory_ids[0] for item in bundle.items],
                ["current-l2"],
            )


def _memory(
    memory_id: str,
    layer: MemoryLayer,
    session_id: str,
    review_status: str = "approved",
    status: MemoryStatus = MemoryStatus.ACTIVE,
) -> MemoryUnit:
    now = utc_now_iso()
    content = "sessionpolicy memory %s" % memory_id
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
        score=ScoreBreakdown(
            resident_score=0.5,
            structural=0.5,
            recency=0.5,
            access=0.5,
            evidence_quality=0.8,
        ),
        metadata={
            "session_id": session_id,
            "review_status": review_status,
        },
    )


if __name__ == "__main__":
    unittest.main()
