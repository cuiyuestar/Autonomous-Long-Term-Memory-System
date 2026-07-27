import asyncio
from pathlib import Path
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from altm.adapters.mcp.server import create_mcp_server  # noqa: E402
from altm.contracts import LifecycleState, MemoryLayer, MemoryStatus, MemoryUnit, RecallQuery  # noqa: E402
from altm.retrieval import FTSRetrievalEngine, QueryEmergenceEngine  # noqa: E402
from altm.storage import SQLiteMemoryStore  # noqa: E402
from altm.utils import sha256_text, utc_now_iso  # noqa: E402


class QueryEmergenceTest(unittest.TestCase):
    def test_query_emergence_expands_from_direct_hit_to_graph_neighbor(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store = SQLiteMemoryStore(Path(tmpdir) / "memory.sqlite3")
            store.initialize()
            seed = _memory("seed", "MCP outage primary query evidence")
            lesson = _memory("lesson", "Historical lesson: check the embedding env prefix first.")
            unrelated = _memory("unrelated", "unrelated background")
            for memory in (seed, lesson, unrelated):
                store.put_memory_unit(memory)
            store.put_memory_graph_edge(
                source_memory_id=seed.id,
                target_memory_id=lesson.id,
                edge_type="supports",
                weight=0.9,
                confidence=0.9,
            )

            candidates = QueryEmergenceEngine(store, FTSRetrievalEngine(store)).emerge(
                RecallQuery(text="MCP outage", top_k=3, preferred_layers=[MemoryLayer.L2]),
                seed_limit=2,
                max_hops=2,
            )

            by_id = {candidate.memory.id: candidate for candidate in candidates}
            self.assertIn("seed", by_id)
            self.assertIn("lesson", by_id)
            self.assertNotIn("unrelated", by_id)
            self.assertIn("query_emergence_seed", by_id["seed"].matched_by)
            self.assertIn("graph_ppr", by_id["lesson"].matched_by)

    def test_query_emergence_skips_rejected_graph_edges(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store = SQLiteMemoryStore(Path(tmpdir) / "memory.sqlite3")
            store.initialize()
            seed = _memory("seed", "MCP outage primary query evidence")
            rejected_neighbor = _memory("rejected-neighbor", "Rejected neighboring lesson")
            store.put_memory_unit(seed)
            store.put_memory_unit(rejected_neighbor)
            store.put_memory_graph_edge(
                source_memory_id=seed.id,
                target_memory_id=rejected_neighbor.id,
                edge_type="supports",
                weight=0.9,
                confidence=0.9,
                metadata={"review_status": "rejected"},
            )

            candidates = QueryEmergenceEngine(store, FTSRetrievalEngine(store)).emerge(
                RecallQuery(text="MCP outage", top_k=3),
                seed_limit=1,
                max_hops=2,
            )

            self.assertEqual([candidate.memory.id for candidate in candidates], ["seed"])

    def test_mcp_memory_emerge_is_registered_and_callable(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "memory.sqlite3"
            store = SQLiteMemoryStore(db_path)
            store.initialize()
            seed = _memory("seed", "MCP outage primary query evidence")
            lesson = _memory("lesson", "Historical lesson: check the embedding env prefix first.")
            store.put_memory_unit(seed)
            store.put_memory_unit(lesson)
            store.put_memory_graph_edge(
                source_memory_id=seed.id,
                target_memory_id=lesson.id,
                edge_type="supports",
                weight=0.9,
                confidence=0.9,
            )

            app = create_mcp_server(str(db_path))
            tools = asyncio.run(app.list_tools())
            result = _call_tool(
                app,
                "memory_emerge",
                {"query": "MCP outage", "limit": 3, "seed_limit": 2, "max_hops": 2},
            )

            self.assertIn("memory_emerge", {tool.name for tool in tools})
            self.assertIn("lesson", {item["memory"]["id"] for item in result})


def _call_tool(app: object, name: str, arguments: dict[str, object]) -> list[dict[str, object]]:
    result = asyncio.run(app.call_tool(name, arguments))
    if isinstance(result, tuple) and len(result) == 2:
        structured = result[1]
        if isinstance(structured, dict) and isinstance(structured.get("result"), list):
            return structured["result"]
        if isinstance(structured, list):
            return structured
    if isinstance(result, list):
        return result
    raise AssertionError("Unexpected MCP tool result: %r" % (result,))


def _memory(memory_id: str, content: str) -> MemoryUnit:
    now = utc_now_iso()
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
    )


if __name__ == "__main__":
    unittest.main()
