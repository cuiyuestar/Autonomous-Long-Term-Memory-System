import asyncio
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from altm.adapters.mcp.server import create_mcp_server  # noqa: E402
from altm.contracts import (  # noqa: E402
    AccessSignal,
    LifecycleState,
    MemoryLayer,
    MemoryStatus,
    MemoryUnit,
    ReviewStatus,
)
from altm.review import ReviewQueue  # noqa: E402
from altm.storage import SQLiteMemoryStore  # noqa: E402
from altm.utils import sha256_text, utc_now_iso  # noqa: E402


class FakeEmbeddingHandler(BaseHTTPRequestHandler):
    def do_POST(self) -> None:  # noqa: N802
        content_length = int(self.headers.get("Content-Length", "0"))
        payload = json.loads(self.rfile.read(content_length).decode("utf-8"))
        inputs = payload["input"]
        texts = [inputs] if isinstance(inputs, str) else list(inputs)
        data = [
            {
                "object": "embedding",
                "index": index,
                "embedding": [1.0, 0.0] if "alpha" in text else [0.0, 1.0],
            }
            for index, text in enumerate(texts)
        ]
        encoded = json.dumps({"object": "list", "model": payload["model"], "data": data}).encode(
            "utf-8"
        )
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def log_message(self, format: str, *args: object) -> None:
        return


class MCPManagementToolsTest(unittest.TestCase):
    def test_management_tools_are_registered(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            app = create_mcp_server(str(Path(tmpdir) / "memory.sqlite3"))
            tools = asyncio.run(app.list_tools())

            self.assertTrue(
                {
                    "memory_index_embeddings",
                    "memory_govern_lifecycle",
                    "memory_maintenance_cycle",
                    "memory_semantic_dedup",
                    "memory_autonomous_governance_cycle",
                    "memory_autonomous_governance_rollback",
                }.issubset({tool.name for tool in tools})
            )

    def test_mcp_index_embeddings_uses_env_embedding_config(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "memory.sqlite3"
            store = SQLiteMemoryStore(db_path)
            store.initialize()
            store.put_memory_unit(_memory("alpha", "alpha memory"))
            store.put_memory_unit(_memory("beta", "beta memory"))

            server = HTTPServer(("127.0.0.1", 0), FakeEmbeddingHandler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                with patch.dict(
                    "os.environ",
                    {
                        "ALTM_EMBEDDING_BASE_URL": "http://127.0.0.1:%s/v1"
                        % server.server_port,
                        "ALTM_EMBEDDING_API_KEY": "test-key",
                        "ALTM_EMBEDDING_MODEL": "test-embedding",
                    },
                    clear=True,
                ):
                    app = create_mcp_server(str(db_path))
                    result = _call_tool(app, "memory_index_embeddings", {"limit": 10})
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)

            self.assertEqual(result["embedding_model"], "test-embedding")
            self.assertEqual(result["indexed_count"], 2)
            self.assertEqual(set(result["memory_ids"]), {"alpha", "beta"})
            self.assertEqual(
                store.get_memory_embedding("alpha", "test-embedding"),
                (sha256_text("alpha memory"), [1.0, 0.0]),
            )

    def test_mcp_govern_lifecycle_marks_candidates(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "memory.sqlite3"
            store = SQLiteMemoryStore(db_path)
            store.initialize()
            memory = _memory(
                "promote",
                "validated lifecycle memory",
                metadata={"review_status": "approved"},
            )
            store.put_memory_unit(memory)
            for _ in range(4):
                store.record_access_signal(memory.id, AccessSignal.USER_CONFIRMED)

            app = create_mcp_server(str(db_path))
            result = _call_tool(app, "memory_govern_lifecycle", {"limit": 10, "layer": "L2"})

            self.assertEqual(result["updated_count"], 1)
            self.assertEqual(result["promotion_candidates"], ["promote"])
            governed = store.get_memory_unit("promote")
            self.assertIsNotNone(governed)
            self.assertIsNotNone(governed.lifecycle.promotion_candidate_since)

    def test_mcp_semantic_dedup_marks_reviewable_edges(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "memory.sqlite3"
            store = SQLiteMemoryStore(db_path)
            store.initialize()
            first = _memory("decision-a", "系统采用 SQLite 作为本地记忆存储。", "decision")
            second = _memory("decision-b", "项目选择 SQLite 承载本地记忆数据。", "decision")
            for memory, vector in ((first, [1.0, 0.0]), (second, [0.99, 0.05])):
                store.put_memory_unit(memory)
                store.put_memory_embedding(memory.id, "test-embedding", memory.content_hash, vector)

            app = create_mcp_server(str(db_path))
            result = _call_tool(
                app,
                "memory_semantic_dedup",
                {"model": "test-embedding", "threshold": 0.95},
            )
            edges = store.list_graph_edges("semantic_duplicate_candidate")

            self.assertEqual(result["candidate_count"], 1)
            self.assertEqual(len(edges), 1)
            self.assertEqual(edges[0]["source_memory_id"], "decision-a")
            self.assertEqual(edges[0]["target_memory_id"], "decision-b")

    def test_mcp_cross_session_l3_candidates_is_registered_and_callable(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "memory.sqlite3"
            store = SQLiteMemoryStore(db_path)
            store.initialize()
            first = _memory(
                "decision-s1",
                "系统采用 SQLite 作为本地记忆存储。",
                "decision",
                metadata={"session_id": "s1"},
            )
            second = _memory(
                "decision-s2",
                "另一个会话也选择 SQLite 本地记忆存储。",
                "decision",
                metadata={"session_id": "s2"},
            )
            for memory, vector in ((first, [1.0, 0.0]), (second, [0.99, 0.05])):
                store.put_memory_unit(memory)
                store.put_memory_embedding(memory.id, "test-embedding", memory.content_hash, vector)

            app = create_mcp_server(str(db_path))
            tools = asyncio.run(app.list_tools())
            result = _call_tool(
                app,
                "memory_cross_session_l3_candidates",
                {"model": "test-embedding", "threshold": 0.95},
            )

            self.assertIn("memory_cross_session_l3_candidates", {tool.name for tool in tools})
            self.assertEqual(result["candidate_count"], 1)
            self.assertEqual(result["candidates"][0]["source_session_id"], "s1")
            self.assertEqual(result["candidates"][0]["target_session_id"], "s2")

    def test_mcp_build_l4_persona_candidates_is_registered_and_callable(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "memory.sqlite3"
            store = SQLiteMemoryStore(db_path)
            store.initialize()
            first = _memory("pref-a", "用户偏好中文技术深度解释。", "preference")
            second = _memory("pref-b", "用户偏好直接推进开发。", "preference")
            store.put_memory_unit(first)
            store.put_memory_unit(second)

            app = create_mcp_server(str(db_path))
            tools = asyncio.run(app.list_tools())
            result = _call_tool(app, "memory_build_l4_persona_candidates", {"min_support": 2})

            self.assertIn("memory_build_l4_persona_candidates", {tool.name for tool in tools})
            self.assertEqual(result["candidate_count"], 1)
            self.assertEqual(result["candidates"][0]["atom_type"], "preference")

    def test_mcp_maintenance_cycle_runs_without_embedding_model(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "memory.sqlite3"
            store = SQLiteMemoryStore(db_path)
            store.initialize()
            store.put_memory_unit(_memory("pref-a", "用户偏好中文技术深度解释。", "preference"))
            store.put_memory_unit(_memory("pref-b", "用户偏好直接推进开发。", "preference"))

            with patch.dict("os.environ", {}, clear=True):
                app = create_mcp_server(str(db_path))
                result = _call_tool(app, "memory_maintenance_cycle", {"persona_min_support": 2})

            self.assertEqual(result["status"], "complete")
            self.assertEqual(result["steps"]["semantic_dedup"]["status"], "skipped")
            self.assertEqual(result["steps"]["build_l4_persona_candidates"]["status"], "skipped")
            self.assertEqual(result["summary"]["autonomous_l4_applied_count"], 1)

    def test_mcp_autonomous_governance_cycle_is_registered_and_callable(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "memory.sqlite3"
            store = SQLiteMemoryStore(db_path)
            store.initialize()
            store.put_memory_unit(_memory("pref-a", "用户偏好中文技术深度解释。", "preference"))
            store.put_memory_unit(_memory("pref-b", "用户偏好直接推进开发。", "preference"))

            app = create_mcp_server(str(db_path))
            tools = asyncio.run(app.list_tools())
            result = _call_tool(app, "memory_autonomous_governance_cycle", {})

            self.assertIn("memory_autonomous_governance_cycle", {tool.name for tool in tools})
            self.assertEqual(result["status"], "complete")
            self.assertEqual(result["summary"]["l4_persona_applied_count"], 1)

    def test_mcp_maintenance_cycle_applies_reviewed_actions(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "memory.sqlite3"
            store = SQLiteMemoryStore(db_path)
            store.initialize()
            first = _memory("first", "first cross-session source")
            second = _memory("second", "second cross-session source")
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

            with patch.dict("os.environ", {}, clear=True):
                app = create_mcp_server(str(db_path))
                result = _call_tool(
                    app,
                    "memory_maintenance_cycle",
                    {"apply_review_actions": True},
                )

            edge = store.get_graph_edge(edge_id)
            self.assertEqual(result["summary"]["review_action_applied_count"], 1)
            self.assertIsNotNone(edge)
            self.assertEqual(edge["metadata"]["candidate_status"], "confirmed")


def _call_tool(app: object, name: str, arguments: dict[str, object]) -> dict[str, object]:
    result = asyncio.run(app.call_tool(name, arguments))
    if isinstance(result, tuple) and len(result) == 2:
        return result[1]
    if isinstance(result, dict):
        return result
    raise AssertionError("Unexpected MCP tool result: %r" % (result,))


def _memory(
    memory_id: str,
    content: str,
    atom_type: str | None = None,
    metadata: dict[str, object] | None = None,
) -> MemoryUnit:
    now = utc_now_iso()
    memory_metadata = dict(metadata or {})
    if atom_type is not None:
        memory_metadata.update({"atom_type": atom_type, "review_status": "approved"})
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
        metadata=memory_metadata,
    )


if __name__ == "__main__":
    unittest.main()
