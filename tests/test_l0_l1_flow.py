import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from altm.capture import L0Recorder  # noqa: E402
from altm.contracts import (  # noqa: E402
    CaptureInput,
    LLMConfig,
    MemoryLayer,
    MemoryScope,
    MessageRole,
    RecallQuery,
)
from altm.folding import LLMContextCapsuleSummarizer  # noqa: E402
from altm.retrieval import FTSRetrievalEngine  # noqa: E402
from altm.storage import SQLiteMemoryStore  # noqa: E402


class L0L1FlowTest(unittest.TestCase):
    def test_capture_search_and_fold_l1(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store = SQLiteMemoryStore(
                Path(tmpdir) / "memory.sqlite3",
                scope=MemoryScope(),
            )
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
                    content="确认：先实现 L0 capture，然后生成真实 L1 上下文胶囊。",
                )
            )

            self.assertEqual(first.layer, MemoryLayer.L0)
            self.assertEqual(store.get_memory_unit(first.id).content_hash, first.content_hash)

            candidates = FTSRetrievalEngine(store).recall(RecallQuery(text="SQLite", top_k=5))
            self.assertGreaterEqual(len(candidates), 1)
            self.assertEqual(candidates[0].memory.layer, MemoryLayer.L0)
            self.assertTrue({"local_vector", "fts_trigram", "fts_unicode"} & set(candidates[0].matched_by))

            l1_memories = LLMContextCapsuleSummarizer(
                store,
                StubL1Client(),
            ).fold_session("session-a")
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

            l1_candidates = FTSRetrievalEngine(store).recall(
                RecallQuery(text="上下文", top_k=5)
            )
            self.assertTrue(any(candidate.memory.id == l1.id for candidate in l1_candidates))
            self.assertNotEqual(first.id, second.id)


class StubL1Client:
    config = LLMConfig(
        base_url="http://unused.invalid/v1",
        api_key="test",
        model="l1-test",
    )

    def chat_json(self, messages: list[dict[str, str]]) -> dict[str, object]:
        return {
            "title": "SQLite 与 MCP 记忆设计",
            "task_goal": "构建真实 L1 上下文胶囊",
            "local_context": "会话决定使用 SQLite FTS，并继续确认 MCP 接入。",
            "key_turns": ["采用 SQLite FTS", "生成真实 L1 上下文胶囊"],
            "decisions_mentioned": ["采用 SQLite FTS"],
            "unresolved_questions": ["MCP 接入仍需确认"],
            "topic_tags": ["SQLite", "MCP"],
            "confidence": 0.93,
        }


if __name__ == "__main__":
    unittest.main()
