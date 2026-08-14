import asyncio
import io
import json
import sys
import tempfile
import threading
import unittest
from contextlib import redirect_stdout
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from altm.adapters.mcp.server import create_mcp_server  # noqa: E402
from altm.application import AltmApplication  # noqa: E402
from altm.cli import main as cli_main  # noqa: E402
from altm.contracts import MemoryLayer, MemoryScope  # noqa: E402
from altm.llm import llm_config_from_env  # noqa: E402
from altm.storage import SQLiteMemoryStore  # noqa: E402


class L1Handler(BaseHTTPRequestHandler):
    def do_POST(self) -> None:
        content_length = int(self.headers.get("Content-Length", "0"))
        self.rfile.read(content_length)
        content = json.dumps(
            {
                "title": "SQLite memory decision",
                "task_goal": "Build a durable Agent memory runtime",
                "local_context": "The user selected SQLite for local memory storage.",
                "key_turns": ["The user selected SQLite."],
                "decisions_mentioned": ["Use SQLite for local memory storage."],
                "unresolved_questions": ["Choose the production vector index."],
                "emotional_or_pragmatic_tone": "pragmatic",
                "topic_tags": ["SQLite", "memory"],
                "confidence": 0.94,
            }
        )
        encoded = json.dumps(
            {"choices": [{"message": {"content": content}}]}
        ).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def log_message(self, format: str, *args: object) -> None:
        return


class RuntimeProtocolTest(unittest.TestCase):
    def test_prepare_and_commit_are_idempotent_and_record_real_citations(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "memory.sqlite3"
            app = AltmApplication(db_path)
            prepared = app.prepare_turn(
                tenant_id="tenant",
                workspace_id="workspace",
                user_id="user",
                agent_id="agent",
                session_id="session",
                turn_id="turn-1",
                content="Use SQLite for local memory.",
            )
            retried = app.prepare_turn(
                tenant_id="tenant",
                workspace_id="workspace",
                user_id="user",
                agent_id="agent",
                session_id="session",
                turn_id="turn-1",
                content="Use SQLite for local memory.",
            )

            self.assertEqual(retried.cycle_id, prepared.cycle_id)
            cited_id = prepared.context.items[0].source_memory_ids[0]
            committed = app.commit_turn(
                tenant_id="tenant",
                workspace_id="workspace",
                user_id="user",
                agent_id="agent",
                cycle_id=prepared.cycle_id,
                assistant_content="SQLite is now the selected local store.",
                cited_memory_ids=[cited_id],
            )
            retried_commit = app.commit_turn(
                tenant_id="tenant",
                workspace_id="workspace",
                user_id="user",
                agent_id="agent",
                cycle_id=prepared.cycle_id,
                assistant_content="SQLite is now the selected local store.",
                cited_memory_ids=[cited_id],
            )

            self.assertEqual(committed, retried_commit)
            store = SQLiteMemoryStore(db_path, scope=_scope("agent"))
            store.initialize()
            cited = store.get_memory_unit(cited_id)
            self.assertIsNotNone(cited)
            self.assertEqual(cited.access_count, 2)
            self.assertEqual(cited.useful_access_count, 1)
            with store.connect() as connection:
                signals = connection.execute(
                    """
                    SELECT signal, COUNT(*) AS count FROM lifecycle_events
                    WHERE memory_unit_id = ?
                    GROUP BY signal
                    ORDER BY signal
                    """,
                    (cited_id,),
                ).fetchall()
            self.assertEqual(
                [(row["signal"], row["count"]) for row in signals],
                [("cited_by_agent", 1), ("injected", 1)],
            )

    def test_scope_isolation_prevents_cross_agent_reads_and_id_collisions(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "memory.sqlite3"
            app = AltmApplication(db_path)
            first = app.prepare_turn(
                tenant_id="tenant",
                workspace_id="workspace",
                user_id="user",
                agent_id="agent-a",
                session_id="shared-session",
                turn_id="same-turn",
                content="agent A memory",
                message_id="1",
            )
            second = app.prepare_turn(
                tenant_id="tenant",
                workspace_id="workspace",
                user_id="user",
                agent_id="agent-b",
                session_id="shared-session",
                turn_id="same-turn",
                content="agent B memory",
                message_id="1",
            )

            self.assertNotEqual(first.user_memory_id, second.user_memory_id)
            first_store = SQLiteMemoryStore(db_path, scope=_scope("agent-a"))
            second_store = SQLiteMemoryStore(db_path, scope=_scope("agent-b"))
            first_store.initialize()
            second_store.initialize()
            self.assertIsNotNone(first_store.get_memory_unit(first.user_memory_id))
            self.assertIsNone(first_store.get_memory_unit(second.user_memory_id))
            self.assertIsNotNone(second_store.get_memory_unit(second.user_memory_id))

    def test_abort_is_idempotent_and_terminal(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "memory.sqlite3"
            app = AltmApplication(db_path)
            prepared = app.prepare_turn(
                tenant_id="tenant",
                workspace_id="workspace",
                user_id="user",
                agent_id="agent",
                session_id="session",
                turn_id="turn-1",
                content="cancel this turn",
            )

            aborted = app.abort_turn(
                tenant_id="tenant",
                workspace_id="workspace",
                user_id="user",
                agent_id="agent",
                cycle_id=prepared.cycle_id,
                reason="host-turn-aborted",
            )
            retried = app.abort_turn(
                tenant_id="tenant",
                workspace_id="workspace",
                user_id="user",
                agent_id="agent",
                cycle_id=prepared.cycle_id,
                reason="host-turn-aborted",
            )

            self.assertEqual(aborted, retried)
            self.assertEqual(aborted.status, "aborted")
            self.assertEqual(aborted.reason, "host-turn-aborted")
            with self.assertRaisesRegex(ValueError, "Cannot commit"):
                app.commit_turn(
                    tenant_id="tenant",
                    workspace_id="workspace",
                    user_id="user",
                    agent_id="agent",
                    cycle_id=prepared.cycle_id,
                    assistant_content="must not persist",
                )
            with self.assertRaisesRegex(ValueError, "already aborted"):
                app.prepare_turn(
                    tenant_id="tenant",
                    workspace_id="workspace",
                    user_id="user",
                    agent_id="agent",
                    session_id="session",
                    turn_id="turn-1",
                    content="cancel this turn",
                )
            store = SQLiteMemoryStore(db_path, scope=_scope("agent"))
            store.initialize()
            self.assertEqual(len(store.list_l0_by_session("session")), 1)

    def test_abort_rejects_committed_cycle_and_reason_conflict(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            app = AltmApplication(Path(tmpdir) / "memory.sqlite3")
            prepared = app.prepare_turn(
                tenant_id="tenant",
                workspace_id="workspace",
                user_id="user",
                agent_id="agent",
                session_id="session",
                turn_id="turn-1",
                content="hello",
            )
            app.abort_turn(
                tenant_id="tenant",
                workspace_id="workspace",
                user_id="user",
                agent_id="agent",
                cycle_id=prepared.cycle_id,
                reason="first-reason",
            )
            with self.assertRaisesRegex(ValueError, "idempotency conflict"):
                app.abort_turn(
                    tenant_id="tenant",
                    workspace_id="workspace",
                    user_id="user",
                    agent_id="agent",
                    cycle_id=prepared.cycle_id,
                    reason="different-reason",
                )

            committed_prepared = app.prepare_turn(
                tenant_id="tenant",
                workspace_id="workspace",
                user_id="user",
                agent_id="agent",
                session_id="session",
                turn_id="turn-2",
                content="commit me",
            )
            app.commit_turn(
                tenant_id="tenant",
                workspace_id="workspace",
                user_id="user",
                agent_id="agent",
                cycle_id=committed_prepared.cycle_id,
                assistant_content="committed",
            )
            with self.assertRaisesRegex(ValueError, "Cannot abort"):
                app.abort_turn(
                    tenant_id="tenant",
                    workspace_id="workspace",
                    user_id="user",
                    agent_id="agent",
                    cycle_id=committed_prepared.cycle_id,
                    reason="too-late",
                )

    def test_prepare_rejects_changed_content_or_query_for_same_turn(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            app = AltmApplication(Path(tmpdir) / "memory.sqlite3")
            arguments = {
                "tenant_id": "tenant",
                "workspace_id": "workspace",
                "user_id": "user",
                "agent_id": "agent",
                "session_id": "session",
                "turn_id": "turn-1",
                "content": "original",
            }
            app.prepare_turn(**arguments)
            with self.assertRaisesRegex(ValueError, "append-only"):
                app.prepare_turn(**{**arguments, "content": "changed"})
            with self.assertRaisesRegex(ValueError, "idempotency conflict"):
                app.prepare_turn(**{**arguments, "query": "changed query"})

    def test_commit_rejects_memory_not_in_prepared_context(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            app = AltmApplication(Path(tmpdir) / "memory.sqlite3")
            prepared = app.prepare_turn(
                tenant_id="tenant",
                workspace_id="workspace",
                user_id="user",
                agent_id="agent",
                session_id="session",
                turn_id="turn-1",
                content="hello",
            )
            with self.assertRaisesRegex(ValueError, "not present"):
                app.commit_turn(
                    tenant_id="tenant",
                    workspace_id="workspace",
                    user_id="user",
                    agent_id="agent",
                    cycle_id=prepared.cycle_id,
                    assistant_content="response",
                    cited_memory_ids=["foreign-memory"],
                )

    def test_runtime_session_validates_all_messages_before_atomic_write(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "memory.sqlite3"
            with self.assertRaises(ValueError):
                AltmApplication(db_path).runtime_session_cycle(
                    session_id="session",
                    messages=[
                        {"role": "user", "content": "valid"},
                        {"role": "invalid-role", "content": "invalid"},
                    ],
                    fold_l1=False,
                    extract_l2=False,
                    run_maintenance=False,
                )
            store = SQLiteMemoryStore(db_path)
            store.initialize()
            self.assertEqual(store.list_l0_by_session("session"), [])

    def test_worker_builds_real_incremental_l1_and_checkpoint(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "memory.sqlite3"
            app = AltmApplication(db_path)
            app.prepare_turn(
                tenant_id="tenant",
                workspace_id="workspace",
                user_id="user",
                agent_id="agent",
                session_id="session",
                turn_id="turn-1",
                content="We selected SQLite for local memory storage.",
            )
            server = HTTPServer(("127.0.0.1", 0), L1Handler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                with patch.dict(
                    "os.environ",
                    {
                        "ALTM_L1_LLM_BASE_URL": "http://127.0.0.1:%s/v1"
                        % server.server_port,
                        "ALTM_L1_LLM_API_KEY": "test-key",
                        "ALTM_L1_LLM_MODEL": "l1-test-model",
                    },
                    clear=True,
                ):
                    result = app.process_next_job("worker-1")
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)

            self.assertEqual(result["status"], "completed")
            self.assertEqual(result["job_type"], "fold_l1")
            store = SQLiteMemoryStore(db_path, scope=_scope("agent"))
            store.initialize()
            l1_units = store.list_memory_units(layer=MemoryLayer.L1)
            self.assertEqual(len(l1_units), 1)
            self.assertEqual(
                l1_units[0].metadata["builder"],
                "llm_context_capsule_summarizer",
            )
            self.assertEqual(l1_units[0].metadata["llm_model"], "l1-test-model")
            self.assertIsNotNone(
                store.get_checkpoint(l1_units[0].metadata["checkpoint_scope"])
            )

    def test_mcp_runtime_profile_executes_prepare_and_commit(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            app = create_mcp_server(str(Path(tmpdir) / "memory.sqlite3"))
            prepared = _call_tool(
                app,
                "memory_prepare_turn",
                {
                    "tenant_id": "tenant",
                    "workspace_id": "workspace",
                    "user_id": "user",
                    "agent_id": "agent",
                    "session_id": "session",
                    "turn_id": "turn-1",
                    "content": "hello",
                },
            )
            cited_id = prepared["context"]["items"][0]["source_memory_ids"][0]
            committed = _call_tool(
                app,
                "memory_commit_turn",
                {
                    "tenant_id": "tenant",
                    "workspace_id": "workspace",
                    "user_id": "user",
                    "agent_id": "agent",
                    "cycle_id": prepared["cycle_id"],
                    "assistant_content": "real host response",
                    "cited_memory_ids": [cited_id],
                },
            )
            self.assertEqual(committed["status"], "committed")
            self.assertEqual(committed["cited_memory_ids"], [cited_id])

    def test_mcp_runtime_profile_executes_prepare_and_abort(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            app = create_mcp_server(str(Path(tmpdir) / "memory.sqlite3"))
            prepared = _call_tool(
                app,
                "memory_prepare_turn",
                {
                    "tenant_id": "tenant",
                    "workspace_id": "workspace",
                    "user_id": "user",
                    "agent_id": "agent",
                    "session_id": "session",
                    "turn_id": "turn-1",
                    "content": "hello",
                },
            )
            aborted = _call_tool(
                app,
                "memory_abort_turn",
                {
                    "tenant_id": "tenant",
                    "workspace_id": "workspace",
                    "user_id": "user",
                    "agent_id": "agent",
                    "cycle_id": prepared["cycle_id"],
                    "reason": "host-disposed",
                },
            )
            self.assertEqual(aborted["status"], "aborted")
            self.assertEqual(aborted["reason"], "host-disposed")

    def test_cli_executes_abort_turn(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "memory.sqlite3"
            prepared = AltmApplication(db_path).prepare_turn(
                tenant_id="tenant",
                workspace_id="workspace",
                user_id="user",
                agent_id="agent",
                session_id="session",
                turn_id="turn-1",
                content="hello",
            )
            output = io.StringIO()
            with redirect_stdout(output):
                exit_code = cli_main(
                    [
                        "abort-turn",
                        "--db",
                        str(db_path),
                        "--tenant-id",
                        "tenant",
                        "--workspace-id",
                        "workspace",
                        "--user-id",
                        "user",
                        "--agent-id",
                        "agent",
                        "--cycle-id",
                        prepared.cycle_id,
                        "--reason",
                        "cli-abort",
                    ]
                )
            result = json.loads(output.getvalue())
            self.assertEqual(exit_code, 0)
            self.assertEqual(result["status"], "aborted")
            self.assertEqual(result["reason"], "cli-abort")

    def test_stage_llm_config_overrides_only_selected_fields(self) -> None:
        with patch.dict(
            "os.environ",
            {
                "ALTM_LLM_BASE_URL": "https://llm.example/v1",
                "ALTM_LLM_API_KEY": "global-key",
                "ALTM_LLM_MODEL": "global-model",
                "ALTM_L1_LLM_MODEL": "l1-model",
                "ALTM_L1_LLM_TIMEOUT_SECONDS": "90",
            },
            clear=True,
        ):
            config = llm_config_from_env("l1")

        self.assertEqual(config.base_url, "https://llm.example/v1")
        self.assertEqual(config.api_key, "global-key")
        self.assertEqual(config.model, "l1-model")
        self.assertEqual(config.timeout_seconds, 90)


def _scope(agent_id: str) -> MemoryScope:
    return MemoryScope(
        tenant_id="tenant",
        workspace_id="workspace",
        user_id="user",
        agent_id=agent_id,
    )


def _call_tool(
    app: object,
    name: str,
    arguments: dict[str, object],
) -> dict[str, object]:
    result = asyncio.run(app.call_tool(name, arguments))
    if isinstance(result, tuple) and len(result) == 2:
        return result[1]
    if isinstance(result, dict):
        return result
    raise AssertionError("Unexpected MCP tool result: %r" % (result,))


if __name__ == "__main__":
    unittest.main()
