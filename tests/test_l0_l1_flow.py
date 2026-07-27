from pathlib import Path
import json
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from altm.capture import L0Recorder  # noqa: E402
from altm.contracts import CaptureInput, MemoryLayer, MessageRole, RecallQuery  # noqa: E402
from altm.folding import RuleBasedL1Summarizer  # noqa: E402
from altm.retrieval import FTSRetrievalEngine  # noqa: E402
from altm.storage import SQLiteMemoryStore  # noqa: E402


class L0L1FlowTest(unittest.TestCase):
    def test_capture_search_and_fold_l1(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store = SQLiteMemoryStore(Path(tmpdir) / "memory.sqlite3")
            store.initialize()

            recorder = L0Recorder(store)
            first = recorder.capture(
                CaptureInput(
                    session_id="session-a",
                    message_id="m1",
                    role=MessageRole.USER,
                    content="我们决定采用 SQLite FTS 作为第一阶段检索，并需要后续确认 MCP。",
                )
            )
            second = recorder.capture(
                CaptureInput(
                    session_id="session-a",
                    message_id="m2",
                    role=MessageRole.ASSISTANT,
                    content="确认：先实现 L0 capture，然后生成 L1 mock 胶囊。",
                )
            )

            self.assertEqual(first.layer, MemoryLayer.L0)
            self.assertEqual(store.get_memory_unit(first.id).content_hash, first.content_hash)

            candidates = FTSRetrievalEngine(store).recall(RecallQuery(text="SQLite", top_k=5))
            self.assertGreaterEqual(len(candidates), 1)
            self.assertEqual(candidates[0].memory.layer, MemoryLayer.L0)
            self.assertTrue({"local_vector", "fts_trigram", "fts_unicode"} & set(candidates[0].matched_by))

            l1_memories = RuleBasedL1Summarizer(store).fold_session("session-a")
            self.assertEqual(len(l1_memories), 1)
            self.assertEqual(store.count_l1_context_capsules(), 1)
            l1 = l1_memories[0]
            self.assertEqual(l1.layer, MemoryLayer.L1)
            self.assertEqual(len(l1.evidence_refs), 2)

            capsule = json.loads(l1.content)
            self.assertEqual(capsule["session_id"], "session-a")
            self.assertEqual(capsule["source_message_ids"], ["m1", "m2"])
            self.assertTrue(capsule["decisions_mentioned"])
            self.assertTrue(capsule["unresolved_questions"])

            l1_candidates = FTSRetrievalEngine(store).recall(RecallQuery(text="mock", top_k=5))
            self.assertTrue(any(candidate.memory.id == l1.id for candidate in l1_candidates))
            self.assertNotEqual(first.id, second.id)


if __name__ == "__main__":
    unittest.main()
