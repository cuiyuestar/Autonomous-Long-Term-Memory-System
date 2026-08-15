import asyncio
import json
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
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
    def do_POST(self) -> None:
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
    def test_runtime_profile_only_exposes_safe_agent_tools(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            app = create_mcp_server(str(Path(tmpdir) / "memory.sqlite3"))
            tools = asyncio.run(app.list_tools())

            self.assertEqual(
                {tool.name for tool in tools},
                {
                    "memory_prepare_turn",
                    "memory_commit_turn",
                    "memory_abort_turn",
                    "memory_ui_graph_seeds",
                    "memory_ui_graph_neighborhood",
                    "memory_ui_layers",
                    "memory_ui_embedding_status",
                    "memory_ui_configure_embedding",
                    "memory_mvp_chat",
                    "memory_drilldown",
                    "memory_feedback",
                    "memory_pin",
                    "memory_unpin",
                    "memory_delete",
                },
            )

    def test_runtime_ui_tools_read_scoped_layers_and_graph(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "memory.sqlite3"
            store = SQLiteMemoryStore(db_path)
            store.initialize()
            first = _memory("ui-first", "First UI memory")
            second = _memory("ui-second", "Second UI memory")
            store.put_memory_unit(first)
            store.put_memory_unit(second)
            store.put_memory_graph_edge(
                source_memory_id=first.id,
                target_memory_id=second.id,
                edge_type="related_to",
                weight=0.8,
                confidence=0.8,
            )

            app = create_mcp_server(str(db_path))
            scope = {
                "tenant_id": "local",
                "workspace_id": "default",
                "user_id": "default",
                "agent_id": "default",
            }
            layers = _call_tool(app, "memory_ui_layers", scope)
            seeds = _call_tool_value(app, "memory_ui_graph_seeds", scope)

            self.assertEqual(layers["counts"]["L2"], 2)
            self.assertEqual(len(layers["layers"]["L2"]), 2)
            self.assertIsInstance(seeds, list)
            self.assertEqual(len(seeds), 2)
            neighborhood = _call_tool(
                app,
                "memory_ui_graph_neighborhood",
                {
                    **scope,
                    "seed_node_ids": [seeds[0]["id"]],
                    "max_hops": 2,
                },
            )
            self.assertEqual(len(neighborhood["nodes"]), 2)
            self.assertEqual(len(neighborhood["edges"]), 1)

    def test_runtime_ui_embedding_tools_configure_without_returning_the_key(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "memory.sqlite3"
            server = HTTPServer(("127.0.0.1", 0), FakeEmbeddingHandler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                with patch.dict("os.environ", {}, clear=True):
                    app = create_mcp_server(str(db_path))
                    initial = _call_tool(app, "memory_ui_embedding_status", {})
                    configured = _call_tool(
                        app,
                        "memory_ui_configure_embedding",
                        {
                            "base_url": "http://127.0.0.1:%s/v1" % server.server_port,
                            "model": "runtime-ui-embedding",
                            "api_key": "runtime-ui-secret",
                        },
                    )
                    current = _call_tool(app, "memory_ui_embedding_status", {})
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)

            self.assertFalse(initial["configured"])
            self.assertTrue(configured["configured"])
            self.assertEqual(current["model"], "runtime-ui-embedding")
            self.assertNotIn("runtime-ui-secret", json.dumps(configured))
            self.assertNotIn("api_key", current)

    def test_management_tools_are_registered(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            app = create_mcp_server(
                str(Path(tmpdir) / "memory.sqlite3"),
                profile="admin",
            )
            tools = asyncio.run(app.list_tools())

            self.assertTrue(
                {
                    "memory_runtime_cycle",
                    "memory_runtime_session_cycle",
                    "memory_mvp_chat",
                    "memory_build_semantic_l3",
                    "memory_distill_semantic_l4",
                    "memory_index_embeddings",
                    "memory_govern_lifecycle",
                    "memory_maintenance_cycle",
                    "memory_semantic_dedup",
                    "memory_autonomous_governance_cycle",
                    "memory_autonomous_governance_rollback",
                }.issubset({tool.name for tool in tools})
            )

    def test_mcp_runtime_cycle_runs_mvp_loop(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "memory.sqlite3"

            with patch.dict("os.environ", {}, clear=True):
                app = create_mcp_server(str(db_path), profile="admin")
                result = _call_tool(
                    app,
                    "memory_runtime_cycle",
                    {
                        "session_id": "mvp",
                        "content": "MCP runtime cycle should produce context.",
                        "run_maintenance": False,
                    },
                )

            self.assertEqual(result["status"], "complete")
            self.assertEqual(result["steps"]["capture_l0"]["status"], "applied")
            self.assertGreaterEqual(result["summary"]["context_included_count"], 1)

    def test_mcp_runtime_session_cycle_runs_mvp_loop(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "memory.sqlite3"

            with patch.dict("os.environ", {}, clear=True):
                app = create_mcp_server(str(db_path), profile="admin")
                result = _call_tool(
                    app,
                    "memory_runtime_session_cycle",
                    {
                        "session_id": "mvp-session",
                        "messages": [
                            {"role": "user", "content": "我们决定采用 SQLite。"},
                            {"role": "assistant", "content": "记录为 MVP 本地存储决策。"},
                        ],
                        "run_maintenance": False,
                    },
                )

            self.assertEqual(result["status"], "complete")
            self.assertEqual(result["captured_count"], 2)
            self.assertGreaterEqual(result["summary"]["context_included_count"], 1)

    def test_mcp_mvp_chat_runs_interactive_loop(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "memory.sqlite3"

            with patch.dict("os.environ", {}, clear=True):
                app = create_mcp_server(str(db_path), profile="admin")
                result = _call_tool(
                    app,
                    "memory_mvp_chat",
                    {
                        "tenant_id": "local",
                        "workspace_id": "default",
                        "user_id": "default",
                        "agent_id": "default",
                        "session_id": "mvp-chat",
                        "content": "MCP should expose the interactive memory loop.",
                        "assistant_content": "已记录 MCP 交互式记忆闭环。",
                        "run_maintenance": False,
                    },
                )

            self.assertEqual(result["status"], "complete")
            self.assertIn("assistant_response", result)
            self.assertEqual(result["committed_turn"]["status"], "committed")
            self.assertEqual(result["assistant_response"]["cited_memory_ids"], [])

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
                    app = create_mcp_server(str(db_path), profile="admin")
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

            app = create_mcp_server(str(db_path), profile="admin")
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

            app = create_mcp_server(str(db_path), profile="admin")
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

            app = create_mcp_server(str(db_path), profile="admin")
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

            app = create_mcp_server(str(db_path), profile="admin")
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
                app = create_mcp_server(str(db_path), profile="admin")
                result = _call_tool(app, "memory_maintenance_cycle", {"persona_min_support": 2})

            self.assertEqual(result["status"], "complete")
            self.assertEqual(result["steps"]["semantic_dedup"]["status"], "skipped")
            self.assertEqual(result["steps"]["build_l4_persona_candidates"]["status"], "skipped")
            self.assertEqual(result["summary"]["autonomous_l4_applied_count"], 0)

    def test_mcp_autonomous_governance_cycle_is_registered_and_callable(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "memory.sqlite3"
            store = SQLiteMemoryStore(db_path)
            store.initialize()
            store.put_memory_unit(_memory("pref-a", "用户偏好中文技术深度解释。", "preference"))
            store.put_memory_unit(_memory("pref-b", "用户偏好直接推进开发。", "preference"))

            app = create_mcp_server(str(db_path), profile="admin")
            tools = asyncio.run(app.list_tools())
            result = _call_tool(app, "memory_autonomous_governance_cycle", {})

            self.assertIn("memory_autonomous_governance_cycle", {tool.name for tool in tools})
            self.assertEqual(result["status"], "complete")
            self.assertEqual(result["summary"]["l4_persona_applied_count"], 0)

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
                app = create_mcp_server(str(db_path), profile="admin")
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
    result = _call_tool_value(app, name, arguments)
    if isinstance(result, dict):
        return result
    raise AssertionError("Unexpected MCP tool result: %r" % (result,))


def _call_tool_value(app: object, name: str, arguments: dict[str, object]) -> object:
    result = asyncio.run(app.call_tool(name, arguments))
    if isinstance(result, tuple) and len(result) == 2:
        result = result[1]
    if isinstance(result, dict) and set(result) == {"result"}:
        return result["result"]
    if isinstance(result, (dict, list)):
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
