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

from altm.contracts import (  # noqa: E402
    LifecycleState,
    MemoryLayer,
    MemoryScope,
    MemoryStatus,
    MemoryUnit,
)
from altm.folding import GraphLLMExtractor  # noqa: E402
from altm.storage import SQLiteMemoryStore  # noqa: E402
from altm.utils import sha256_text, utc_now_iso  # noqa: E402


class GraphHandler(BaseHTTPRequestHandler):
    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        payload = json.loads(self.rfile.read(length))
        memory_id = json.loads(payload["messages"][1]["content"])["memories"][0][
            "memory_id"
        ]
        result = {
            "nodes": [
                {
                    "local_id": "task",
                    "node_type": "task",
                    "name": "Release ALTM",
                    "canonical_key": "task:release-altm",
                    "memory_unit_id": memory_id,
                    "attributes": {"status": "active"},
                },
                {
                    "local_id": "deadline",
                    "node_type": "time",
                    "name": "2026-09-01",
                    "canonical_key": "time:2026-09-01",
                    "attributes": {"iso": "2026-09-01"},
                },
            ],
            "edges": [
                {
                    "source_local_id": "deadline",
                    "target_local_id": "task",
                    "edge_type": "deadline_for",
                    "confidence": 0.97,
                    "attributes": {},
                }
            ],
            "confidence": 0.96,
        }
        content = json.dumps(result)
        encoded = json.dumps(
            {"choices": [{"message": {"content": content}}]}
        ).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def log_message(self, format: str, *args: object) -> None:
        return


class GraphExtractionTest(unittest.TestCase):
    def test_graph_model_writes_scoped_idempotent_entity_time_graph(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "memory.sqlite3"
            store = SQLiteMemoryStore(db_path, scope=_scope("agent-a"))
            other = SQLiteMemoryStore(db_path, scope=_scope("agent-b"))
            store.initialize()
            other.initialize()
            memory = _l2(store.scope)
            store.put_memory_unit(memory)
            server = HTTPServer(("127.0.0.1", 0), GraphHandler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                with patch.dict(
                    "os.environ",
                    {
                        "ALTM_GRAPH_LLM_BASE_URL": "http://127.0.0.1:%s/v1"
                        % server.server_port,
                        "ALTM_GRAPH_LLM_API_KEY": "test-key",
                        "ALTM_GRAPH_LLM_MODEL": "graph-model",
                    },
                    clear=True,
                ):
                    result = GraphLLMExtractor(store).extract_session("session")
                    retried = GraphLLMExtractor(store).extract_session("session")
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)

            self.assertEqual(result["status"], "complete")
            self.assertEqual(len(result["node_ids"]), 2)
            self.assertEqual(len(result["edge_ids"]), 1)
            self.assertEqual(retried["status"], "idle")
            self.assertEqual(len(store.list_graph_edges()), 1)
            self.assertEqual(other.list_graph_edges(), [])
            with store.connect() as connection:
                nodes = connection.execute(
                    """
                    SELECT node_type, canonical_key
                    FROM graph_nodes
                    WHERE tenant_id = 'tenant' AND agent_id = 'agent-a'
                    ORDER BY node_type
                    """
                ).fetchall()
                extraction_count = connection.execute(
                    "SELECT COUNT(*) FROM graph_extractions"
                ).fetchone()[0]
            self.assertEqual(
                {(row["node_type"], row["canonical_key"]) for row in nodes},
                {
                    ("task", "task:release-altm"),
                    ("time", "time:2026-09-01"),
                },
            )
            self.assertEqual(extraction_count, 1)


def _scope(agent_id: str) -> MemoryScope:
    return MemoryScope(
        tenant_id="tenant",
        workspace_id="workspace",
        user_id="user",
        agent_id=agent_id,
    )


def _l2(scope: MemoryScope) -> MemoryUnit:
    content = "Release ALTM before 2026-09-01."
    now = utc_now_iso()
    return MemoryUnit(
        id="l2-deadline",
        scope=scope,
        layer=MemoryLayer.L2,
        lifecycle_state=LifecycleState.SHORT,
        status=MemoryStatus.ACTIVE,
        content=content,
        content_hash=sha256_text(content),
        summary=content,
        created_at=now,
        updated_at=now,
        metadata={
            "session_id": "session",
            "atom_type": "task_state",
            "review_status": "approved",
        },
    )


if __name__ == "__main__":
    unittest.main()
