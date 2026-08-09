import json
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from altm.capture import L0Recorder  # noqa: E402
from altm.contracts import (  # noqa: E402
    CaptureInput,
    L2AtomType,
    LLMConfig,
    MemoryLayer,
    MemoryScope,
    MessageRole,
    RecallQuery,
)
from altm.folding import L2Extractor, LLMContextCapsuleSummarizer  # noqa: E402
from altm.llm import OpenAICompatibleClient  # noqa: E402
from altm.retrieval import FTSRetrievalEngine  # noqa: E402
from altm.storage import SQLiteMemoryStore  # noqa: E402


class FakeLLMHandler(BaseHTTPRequestHandler):
    def do_POST(self) -> None:
        content_length = int(self.headers.get("Content-Length", "0"))
        self.rfile.read(content_length)
        response = {
            "choices": [
                {
                    "message": {
                        "content": json.dumps(
                            {
                                "atoms": [
                                    {
                                        "atom_type": "decision",
                                        "text": "项目决定采用 SQLite FTS 作为第一阶段检索能力。",
                                        "subject": "项目",
                                        "predicate": "决定采用",
                                        "object": "SQLite FTS",
                                        "scope": "ALTM (Autonomous Long-Term Memory)",
                                        "confidence": 0.86,
                                        "extraction_reason": "L1 capsule 明确记录了该技术决策。",
                                    },
                                    {
                                        "atom_type": "constraint",
                                        "text": "L2 事实默认进入待审状态，不能自动写入长期画像。",
                                        "subject": "L2 facts",
                                        "predicate": "review_status",
                                        "object": "pending",
                                        "scope": "memory governance",
                                        "confidence": 0.82,
                                        "extraction_reason": "用户明确选择默认待审策略。",
                                    },
                                ]
                            },
                            ensure_ascii=False,
                        )
                    }
                }
            ]
        }
        encoded = json.dumps(response).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def log_message(self, format: str, *args: object) -> None:
        return


class FailingClient:
    def chat_json(self, messages: list[dict[str, str]]) -> dict[str, object]:
        raise RuntimeError("forced failure")


class StubL1Client(OpenAICompatibleClient):
    def __init__(self) -> None:
        super().__init__(
            LLMConfig(
                base_url="http://unused.invalid/v1",
                api_key="test",
                model="l1-test",
            )
        )

    def chat_json(self, messages: list[dict[str, str]]) -> dict[str, object]:
        return {
            "title": "SQLite L2 extraction context",
            "task_goal": "Extract grounded L2 memories",
            "local_context": "The project selected SQLite FTS and requires grounded L2 extraction.",
            "key_turns": ["Select SQLite FTS", "Use the OpenAI-compatible extractor"],
            "decisions_mentioned": ["Use SQLite FTS"],
            "unresolved_questions": [],
            "topic_tags": ["SQLite", "L2"],
            "confidence": 0.95,
        }


class L2ExtractionTest(unittest.TestCase):
    def test_real_http_l2_extraction_dual_write_and_filtered_recall(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store = SQLiteMemoryStore(
                Path(tmpdir) / "memory.sqlite3",
                scope=MemoryScope(),
            )
            store.initialize()
            self._seed_l1(store)

            server = HTTPServer(("127.0.0.1", 0), FakeLLMHandler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                client = OpenAICompatibleClient(
                    LLMConfig(
                        base_url="http://127.0.0.1:%s/v1" % server.server_port,
                        api_key="test-key",
                        model="test-model",
                        timeout_seconds=10,
                    )
                )
                created = L2Extractor(store, client).extract_from_session("session-a")
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)

            self.assertEqual(len(created), 2)
            self.assertTrue(all(memory.layer == MemoryLayer.L2 for memory in created))
            self.assertEqual(store.count_l2_atoms(L2AtomType.DECISION), 1)
            self.assertEqual(store.count_l2_atoms(L2AtomType.CONSTRAINT), 1)

            recalled = FTSRetrievalEngine(store).recall(
                RecallQuery(
                    text="SQLite",
                    top_k=10,
                    preferred_layers=[MemoryLayer.L2],
                    session_id="session-a",
                )
            )
            self.assertEqual(len(recalled), 1)
            self.assertEqual(recalled[0].memory.metadata["review_status"], "pending")
            self.assertEqual(recalled[0].memory.metadata["atom_type"], "decision")

    def test_l2_extraction_failure_writes_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store = SQLiteMemoryStore(
                Path(tmpdir) / "memory.sqlite3",
                scope=MemoryScope(),
            )
            store.initialize()
            self._seed_l1(store)

            with self.assertRaises(RuntimeError):
                L2Extractor(store, FailingClient()).extract_from_session("session-a")  # type: ignore[arg-type]

            self.assertEqual(store.count_l2_atoms(L2AtomType.DECISION), 0)
            recalled = FTSRetrievalEngine(store).recall(
                RecallQuery(text="SQLite", top_k=10, preferred_layers=[MemoryLayer.L2])
            )
            self.assertEqual(recalled, [])

    def test_l2_exact_duplicate_is_not_written_twice(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store = SQLiteMemoryStore(
                Path(tmpdir) / "memory.sqlite3",
                scope=MemoryScope(),
            )
            store.initialize()
            self._seed_l1(store)

            server = HTTPServer(("127.0.0.1", 0), FakeLLMHandler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                client = OpenAICompatibleClient(
                    LLMConfig(
                        base_url="http://127.0.0.1:%s/v1" % server.server_port,
                        api_key="test-key",
                        model="test-model",
                        timeout_seconds=10,
                    )
                )
                extractor = L2Extractor(store, client)
                first = extractor.extract_from_session("session-a")
                second = extractor.extract_from_session("session-a")
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)

            self.assertEqual(len(first), 2)
            self.assertEqual(second, [])
            self.assertEqual(store.count_l2_atoms(L2AtomType.DECISION), 1)
            self.assertEqual(store.count_l2_atoms(L2AtomType.CONSTRAINT), 1)

    def _seed_l1(self, store: SQLiteMemoryStore) -> None:
        recorder = L0Recorder(store)
        recorder.capture(
            CaptureInput(
                session_id="session-a",
                message_id="m1",
                role=MessageRole.USER,
                content="我们决定采用 SQLite FTS，并要求 L2 默认待审。",
            )
        )
        recorder.capture(
            CaptureInput(
                session_id="session-a",
                message_id="m2",
                role=MessageRole.ASSISTANT,
                content="确认：L2 抽取使用真实 OpenAI 兼容接口，失败时不写 L2。",
            )
        )
        LLMContextCapsuleSummarizer(store, StubL1Client()).fold_session("session-a")


if __name__ == "__main__":
    unittest.main()
