import asyncio
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from altm.adapters.mcp.server import create_mcp_server  # noqa: E402
from altm.application import AltmApplication  # noqa: E402
from altm.cli import main as cli_main  # noqa: E402
from altm.context import SimpleContextFusion  # noqa: E402
from altm.contracts import (  # noqa: E402
    LifecycleState,
    MemoryLayer,
    MemoryStatus,
    MemoryUnit,
    RecallCandidate,
    ScoreBreakdown,
)
from altm.storage import SQLiteMemoryStore  # noqa: E402
from altm.utils import sha256_text, utc_now_iso  # noqa: E402


class ContextFusionTest(unittest.TestCase):
    def test_merge_keeps_query_order_and_merges_active_duplicate_signals(self) -> None:
        recall = [
            _candidate("query-hit", "query target", 0.90, ["fts_unicode"]),
            _candidate("query-second", "query second", 0.70, ["fts_trigram"]),
        ]
        active = [
            _candidate("query-hit", "query target", 0.60, ["global_active_window"]),
            _candidate("active-only", "active memory", 0.55, ["global_active_window"]),
        ]

        merged = SimpleContextFusion().merge(recall, active)

        self.assertEqual([candidate.memory.id for candidate in merged], ["query-hit", "query-second", "active-only"])
        self.assertEqual(merged[0].score.retrieval_score, 0.90)
        self.assertIn("fts_unicode", merged[0].matched_by)
        self.assertIn("global_active_window", merged[0].matched_by)
        self.assertIn("active_window", merged[2].matched_by)

    def test_assemble_adds_fusion_metadata_and_deduplicates_items(self) -> None:
        recall = [_candidate("shared", "query target", 0.90, ["fts_unicode"])]
        active = [
            _candidate("shared", "query target", 0.60, ["global_active_window"]),
            _candidate("active-only", "active memory", 0.55, ["global_active_window"]),
        ]

        bundle = SimpleContextFusion().assemble(
            recall_candidates=recall,
            active_candidates=active,
            token_budget=100,
        )

        self.assertEqual([item.source_memory_ids[0] for item in bundle.items], ["shared", "active-only"])
        self.assertEqual(bundle.metadata["fusion_strategy"], "query_recall_then_active_window")
        self.assertEqual(bundle.metadata["recall_candidate_count"], 1)
        self.assertEqual(bundle.metadata["active_candidate_count"], 2)
        self.assertEqual(bundle.metadata["duplicate_candidate_count"], 1)

    def test_report_explains_included_budget_and_candidate_limit_decisions(self) -> None:
        recall = [_candidate("included", "query target " + ("x" * 200), 0.90, ["fts_unicode"])]
        active = [
            _candidate("budget-excluded", "x" * 500, 0.70, ["global_active_window"]),
            _candidate("outside-limit", "active memory", 0.55, ["global_active_window"]),
        ]

        report = SimpleContextFusion().report(
            recall_candidates=recall,
            active_candidates=active,
            token_budget=8,
            candidate_limit=2,
        )

        decisions = {decision.memory_id: decision for decision in report.decisions}
        self.assertEqual(report.bundle.metadata["fusion_strategy"], "query_recall_then_active_window")
        self.assertEqual(decisions["included"].reason, "included")
        self.assertTrue(decisions["included"].selected)
        self.assertEqual(decisions["budget-excluded"].reason, "budget_excluded")
        self.assertEqual(decisions["outside-limit"].reason, "outside_candidate_limit")
        self.assertEqual(decisions["outside-limit"].sources, ["active_window"])

    def test_compare_reports_baseline_and_fused_memory_delta(self) -> None:
        recall = [_candidate("query-hit", "query target", 0.90, ["fts_unicode"])]
        active = [_candidate("active-only", "active memory", 0.70, ["global_active_window"])]

        report = SimpleContextFusion().compare(
            recall_candidates=recall,
            active_candidates=active,
            token_budget=100,
        )

        self.assertEqual(report.baseline_memory_ids, ["query-hit"])
        self.assertEqual(report.fused_memory_ids, ["query-hit", "active-only"])
        self.assertEqual(report.shared_memory_ids, ["query-hit"])
        self.assertEqual(report.baseline_only_memory_ids, [])
        self.assertEqual(report.fused_only_memory_ids, ["active-only"])
        self.assertEqual(report.metadata["included_delta"], 1)

    def test_cli_build_fused_context_outputs_context_bundle(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "memory.sqlite3"
            store = SQLiteMemoryStore(db_path)
            store.initialize()
            store.put_memory_unit(
                _memory(
                    "query-hit",
                    "SQLite query target memory",
                    metadata={"atom_type": "lesson", "review_status": "approved", "session_id": "s1"},
                    resident_score=0.50,
                )
            )
            store.put_memory_unit(
                _memory(
                    "active-only",
                    "Global active engineering convention",
                    lifecycle_state=LifecycleState.LONG,
                    metadata={"atom_type": "lesson", "review_status": "approved"},
                    resident_score=0.90,
                )
            )

            stdout = StringIO()
            with redirect_stdout(stdout):
                exit_code = cli_main(
                    [
                        "build-fused-context",
                        "--db",
                        str(db_path),
                        "--query",
                        "SQLite query target",
                        "--session-id",
                        "s1",
                        "--recall-limit",
                        "5",
                        "--active-limit",
                        "5",
                    ]
                )

            payload = json.loads(stdout.getvalue())
            self.assertEqual(exit_code, 0)
            self.assertEqual(payload["metadata"]["fusion_strategy"], "query_recall_then_active_window")
            self.assertGreaterEqual(payload["metadata"]["included_count"], 1)
            self.assertIn("memory://query-hit", {item["retrieval_marker"] for item in payload["items"]})

    def test_cli_build_context_active_window_mode_full_fuses_context(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "memory.sqlite3"
            store = SQLiteMemoryStore(db_path)
            store.initialize()
            store.put_memory_unit(
                _memory(
                    "query-hit",
                    "SQLite query target memory",
                    metadata={"atom_type": "lesson", "review_status": "approved", "session_id": "s1"},
                    resident_score=0.50,
                )
            )
            store.put_memory_unit(
                _memory(
                    "active-only",
                    "Global active engineering convention",
                    lifecycle_state=LifecycleState.LONG,
                    metadata={"atom_type": "lesson", "review_status": "approved"},
                    resident_score=0.90,
                )
            )

            stdout = StringIO()
            with redirect_stdout(stdout):
                exit_code = cli_main(
                    [
                        "build-context",
                        "--db",
                        str(db_path),
                        "--query",
                        "SQLite query target",
                        "--session-id",
                        "s1",
                        "--limit",
                        "5",
                        "--active-window-mode",
                        "full",
                    ]
                )

            payload = json.loads(stdout.getvalue())
            self.assertEqual(exit_code, 0)
            self.assertEqual(payload["metadata"]["active_window_mode"], "full")
            self.assertEqual(payload["metadata"]["fusion_strategy"], "query_recall_then_active_window")
            self.assertEqual(payload["metadata"]["budget_strategy"], "char_estimate_4_chars_per_token")
            self.assertIn("budget_degraded", payload["metadata"])
            self.assertTrue(
                any(
                    item["retrieval_marker"].startswith("memory://active-only#")
                    for item in payload["items"]
                )
            )

    def test_mcp_memory_build_context_exposes_budget_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "memory.sqlite3"
            store = SQLiteMemoryStore(db_path)
            store.initialize()
            store.put_memory_unit(
                _memory(
                    "query-hit",
                    "SQLite query target memory",
                    metadata={"atom_type": "lesson", "review_status": "approved", "session_id": "s1"},
                    resident_score=0.50,
                )
            )

            app = create_mcp_server(str(db_path), profile="admin")
            tools = asyncio.run(app.list_tools())
            result = _call_tool(
                app,
                "memory_build_context",
                {"query": "SQLite query target", "session_id": "s1", "limit": 5},
            )

            self.assertIn("memory_build_context", {tool.name for tool in tools})
            self.assertEqual(result["metadata"]["budget_strategy"], "char_estimate_4_chars_per_token")
            self.assertIn("budget_degraded", result["metadata"])
            self.assertTrue(
                any(
                    item["retrieval_marker"].startswith("memory://query-hit#")
                    for item in result["items"]
                )
            )

    def test_cli_build_fused_context_report_outputs_decisions(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "memory.sqlite3"
            store = SQLiteMemoryStore(db_path)
            store.initialize()
            store.put_memory_unit(
                _memory(
                    "query-hit",
                    "SQLite query target memory",
                    metadata={"atom_type": "lesson", "review_status": "approved", "session_id": "s1"},
                    resident_score=0.50,
                )
            )

            stdout = StringIO()
            with redirect_stdout(stdout):
                exit_code = cli_main(
                    [
                        "build-fused-context-report",
                        "--db",
                        str(db_path),
                        "--query",
                        "SQLite query target",
                        "--session-id",
                        "s1",
                        "--recall-limit",
                        "5",
                    ]
                )

            payload = json.loads(stdout.getvalue())
            self.assertEqual(exit_code, 0)
            self.assertEqual(payload["bundle"]["metadata"]["fusion_strategy"], "query_recall_then_active_window")
            self.assertEqual(payload["decisions"][0]["reason"], "included")

    def test_cli_compare_fused_context_outputs_delta(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "memory.sqlite3"
            store = SQLiteMemoryStore(db_path)
            store.initialize()
            store.put_memory_unit(
                _memory(
                    "query-hit",
                    "SQLite query target memory",
                    metadata={"atom_type": "lesson", "review_status": "approved", "session_id": "s1"},
                    resident_score=0.50,
                )
            )
            store.put_memory_unit(
                _memory(
                    "active-only",
                    "Global active engineering convention",
                    lifecycle_state=LifecycleState.LONG,
                    metadata={"atom_type": "lesson", "review_status": "approved"},
                    resident_score=0.90,
                )
            )

            stdout = StringIO()
            with redirect_stdout(stdout):
                exit_code = cli_main(
                    [
                        "compare-fused-context",
                        "--db",
                        str(db_path),
                        "--query",
                        "SQLite query target",
                        "--session-id",
                        "s1",
                        "--recall-limit",
                        "5",
                        "--active-limit",
                        "5",
                    ]
                )

            payload = json.loads(stdout.getvalue())
            self.assertEqual(exit_code, 0)
            self.assertIn("query-hit", payload["baseline_memory_ids"])
            self.assertIn("active-only", payload["fused_memory_ids"])
            self.assertIn("active-only", payload["fused_only_memory_ids"])

    def test_application_batch_compare_aggregates_memory_deltas(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "memory.sqlite3"
            store = SQLiteMemoryStore(db_path)
            store.initialize()
            store.put_memory_unit(
                _memory(
                    "query-hit",
                    "SQLite query target memory",
                    metadata={"atom_type": "lesson", "review_status": "approved", "session_id": "s1"},
                    resident_score=0.50,
                )
            )
            store.put_memory_unit(
                _memory(
                    "active-only",
                    "Global active engineering convention",
                    lifecycle_state=LifecycleState.LONG,
                    metadata={"atom_type": "lesson", "review_status": "approved"},
                    resident_score=0.90,
                )
            )

            report = AltmApplication(db_path).compare_fused_context_batch(
                queries=["SQLite query target", "SQLite"],
                session_id="s1",
                recall_limit=5,
                active_limit=5,
            )

            self.assertEqual(report.query_count, 2)
            self.assertEqual(len(report.items), 2)
            self.assertGreaterEqual(report.total_fused_included, report.total_baseline_included)
            self.assertGreaterEqual(report.fused_only_memory_counts["active-only"], 1)
            self.assertEqual(report.metadata["recommendation"], "consider_opt_in_fusion")

    def test_cli_compare_fused_context_batch_outputs_aggregate(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "memory.sqlite3"
            store = SQLiteMemoryStore(db_path)
            store.initialize()
            store.put_memory_unit(
                _memory(
                    "query-hit",
                    "SQLite query target memory",
                    metadata={"atom_type": "lesson", "review_status": "approved", "session_id": "s1"},
                    resident_score=0.50,
                )
            )

            stdout = StringIO()
            with redirect_stdout(stdout):
                exit_code = cli_main(
                    [
                        "compare-fused-context-batch",
                        "--db",
                        str(db_path),
                        "--query",
                        "SQLite query target",
                        "--query",
                        "SQLite",
                        "--session-id",
                        "s1",
                        "--recall-limit",
                        "5",
                    ]
                )

            payload = json.loads(stdout.getvalue())
            self.assertEqual(exit_code, 0)
            self.assertEqual(payload["query_count"], 2)
            self.assertEqual(len(payload["items"]), 2)
            self.assertGreaterEqual(payload["total_fused_included"], payload["total_baseline_included"])
            self.assertIn("recommendation", payload["metadata"])

    def test_mcp_memory_build_fused_context_is_registered_and_callable(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "memory.sqlite3"
            store = SQLiteMemoryStore(db_path)
            store.initialize()
            store.put_memory_unit(
                _memory(
                    "query-hit",
                    "SQLite query target memory",
                    metadata={"atom_type": "lesson", "review_status": "approved", "session_id": "s1"},
                    resident_score=0.50,
                )
            )

            app = create_mcp_server(str(db_path), profile="admin")
            tools = asyncio.run(app.list_tools())
            result = _call_tool(
                app,
                "memory_build_fused_context",
                {"query": "SQLite query target", "session_id": "s1", "recall_limit": 5},
            )

            self.assertIn("memory_build_fused_context", {tool.name for tool in tools})
            self.assertEqual(result["metadata"]["fusion_strategy"], "query_recall_then_active_window")
            self.assertIn("memory://query-hit", {item["retrieval_marker"] for item in result["items"]})

    def test_mcp_memory_build_fused_context_report_is_registered_and_callable(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "memory.sqlite3"
            store = SQLiteMemoryStore(db_path)
            store.initialize()
            store.put_memory_unit(
                _memory(
                    "query-hit",
                    "SQLite query target memory",
                    metadata={"atom_type": "lesson", "review_status": "approved", "session_id": "s1"},
                    resident_score=0.50,
                )
            )

            app = create_mcp_server(str(db_path), profile="admin")
            tools = asyncio.run(app.list_tools())
            result = _call_tool(
                app,
                "memory_build_fused_context_report",
                {"query": "SQLite query target", "session_id": "s1", "recall_limit": 5},
            )

            self.assertIn("memory_build_fused_context_report", {tool.name for tool in tools})
            self.assertEqual(result["bundle"]["metadata"]["fusion_strategy"], "query_recall_then_active_window")
            self.assertEqual(result["decisions"][0]["reason"], "included")

    def test_mcp_memory_compare_fused_context_is_registered_and_callable(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "memory.sqlite3"
            store = SQLiteMemoryStore(db_path)
            store.initialize()
            store.put_memory_unit(
                _memory(
                    "query-hit",
                    "SQLite query target memory",
                    metadata={"atom_type": "lesson", "review_status": "approved", "session_id": "s1"},
                    resident_score=0.50,
                )
            )

            app = create_mcp_server(str(db_path), profile="admin")
            tools = asyncio.run(app.list_tools())
            result = _call_tool(
                app,
                "memory_compare_fused_context",
                {"query": "SQLite query target", "session_id": "s1", "recall_limit": 5},
            )

            self.assertIn("memory_compare_fused_context", {tool.name for tool in tools})
            self.assertIn("query-hit", result["baseline_memory_ids"])
            self.assertIn("query-hit", result["fused_memory_ids"])

    def test_mcp_memory_compare_fused_context_batch_is_registered_and_callable(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "memory.sqlite3"
            store = SQLiteMemoryStore(db_path)
            store.initialize()
            store.put_memory_unit(
                _memory(
                    "query-hit",
                    "SQLite query target memory",
                    metadata={"atom_type": "lesson", "review_status": "approved", "session_id": "s1"},
                    resident_score=0.50,
                )
            )

            app = create_mcp_server(str(db_path), profile="admin")
            tools = asyncio.run(app.list_tools())
            result = _call_tool(
                app,
                "memory_compare_fused_context_batch",
                {"queries": ["SQLite query target", "SQLite"], "session_id": "s1", "recall_limit": 5},
            )

            self.assertIn("memory_compare_fused_context_batch", {tool.name for tool in tools})
            self.assertEqual(result["query_count"], 2)
            self.assertEqual(len(result["items"]), 2)


def _call_tool(app: object, name: str, arguments: dict[str, object]) -> dict[str, object]:
    result = asyncio.run(app.call_tool(name, arguments))
    if isinstance(result, tuple) and len(result) == 2:
        return result[1]
    if isinstance(result, dict):
        return result
    raise AssertionError("Unexpected MCP tool result: %r" % (result,))


def _candidate(
    memory_id: str,
    content: str,
    retrieval_score: float,
    matched_by: list[str],
) -> RecallCandidate:
    return RecallCandidate(
        memory=_memory(memory_id, content, resident_score=retrieval_score),
        score=ScoreBreakdown(
            retrieval_score=retrieval_score,
            resident_score=retrieval_score,
            structural=retrieval_score,
            recency=retrieval_score,
            access=retrieval_score,
            evidence_quality=0.70,
        ),
        matched_by=matched_by,
        explanation="test candidate",
    )


def _memory(
    memory_id: str,
    content: str,
    layer: MemoryLayer = MemoryLayer.L2,
    lifecycle_state: LifecycleState = LifecycleState.SHORT,
    status: MemoryStatus = MemoryStatus.ACTIVE,
    metadata: dict[str, object] | None = None,
    resident_score: float = 0.0,
) -> MemoryUnit:
    now = utc_now_iso()
    return MemoryUnit(
        id=memory_id,
        layer=layer,
        lifecycle_state=lifecycle_state,
        status=status,
        content=content,
        content_hash=sha256_text(content),
        summary=content,
        created_at=now,
        updated_at=now,
        score=ScoreBreakdown(
            resident_score=resident_score,
            structural=resident_score,
            recency=resident_score,
            access=resident_score,
            evidence_quality=0.70,
        ),
        metadata=metadata or {},
    )


if __name__ == "__main__":
    unittest.main()
