import sys
import tempfile
import unittest
from pathlib import Path

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
from altm.retrieval import FTSRetrievalEngine  # noqa: E402
from altm.storage import SQLiteMemoryStore  # noqa: E402
from altm.utils import sha256_text, utc_now_iso  # noqa: E402


class RetrievalAndFeedbackTest(unittest.TestCase):
    def test_chinese_recall_uses_local_vector_or_fallback_channels(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store = SQLiteMemoryStore(Path(tmpdir) / "memory.sqlite3")
            store.initialize()
            now = utc_now_iso()
            memory = MemoryUnit(
                id="l2_cn",
                layer=MemoryLayer.L2,
                lifecycle_state=LifecycleState.SHORT,
                status=MemoryStatus.ACTIVE,
                content="当前系统支持类型拆表双写，并需要验证中文召回。",
                content_hash=sha256_text("当前系统支持类型拆表双写，并需要验证中文召回。"),
                summary="类型拆表双写",
                created_at=now,
                updated_at=now,
                metadata={"session_id": "zh"},
            )
            store.put_memory_unit(memory)

            recalled = FTSRetrievalEngine(store).recall(
                RecallQuery(text="类型拆表", top_k=5, preferred_layers=[MemoryLayer.L2], session_id="zh")
            )

            self.assertEqual(len(recalled), 1)
            self.assertEqual(recalled[0].memory.id, "l2_cn")
            self.assertTrue(
                {"local_vector", "fts_trigram", "like_fallback"} & set(recalled[0].matched_by)
            )

    def test_feedback_records_useful_access(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store = SQLiteMemoryStore(Path(tmpdir) / "memory.sqlite3")
            store.initialize()
            now = utc_now_iso()
            memory = MemoryUnit(
                id="l2_feedback",
                layer=MemoryLayer.L2,
                lifecycle_state=LifecycleState.SHORT,
                status=MemoryStatus.ACTIVE,
                content="feedback target",
                content_hash=sha256_text("feedback target"),
                created_at=now,
                updated_at=now,
            )
            store.put_memory_unit(memory)
            store.record_access_signal(memory.id, AccessSignal.USER_CONFIRMED)

            updated = store.get_memory_unit(memory.id)
            self.assertIsNotNone(updated)
            self.assertEqual(updated.access_count, 1)
            self.assertEqual(updated.useful_access_count, 1)
            self.assertIsNotNone(updated.last_accessed_at)


if __name__ == "__main__":
    unittest.main()
