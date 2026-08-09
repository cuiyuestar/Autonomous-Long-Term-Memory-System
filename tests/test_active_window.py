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
from altm.cli import main as cli_main  # noqa: E402
from altm.contracts import (  # noqa: E402
    LifecycleState,
    MemoryLayer,
    MemoryStatus,
    MemoryUnit,
    ScoreBreakdown,
)
from altm.retrieval import GlobalActiveWindowEngine, GlobalActiveWindowPolicy  # noqa: E402
from altm.storage import SQLiteMemoryStore  # noqa: E402
from altm.utils import sha256_text, utc_now_iso  # noqa: E402


class GlobalActiveWindowTest(unittest.TestCase):
    def test_selects_stable_active_memories_and_filters_rejected_items(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store = SQLiteMemoryStore(Path(tmpdir) / "memory.sqlite3")
            store.initialize()
            memories = [
                _memory(
                    "approved",
                    "已确认的长期记忆系统约束。",
                    lifecycle_state=LifecycleState.LONG,
                    metadata={"atom_type": "constraint", "review_status": "approved", "session_id": "s1"},
                    resident_score=0.80,
                ),
                _memory(
                    "scene",
                    "观察中的 L3 场景。",
                    layer=MemoryLayer.L3,
                    status=MemoryStatus.OBSERVING,
                    metadata={"session_id": "s1"},
                    resident_score=0.65,
                ),
                _memory(
                    "global",
                    "跨会话长期工程约定。",
                    lifecycle_state=LifecycleState.LONG,
                    metadata={"atom_type": "lesson", "review_status": "approved"},
                    resident_score=0.70,
                ),
                _memory(
                    "pending",
                    "待自治治理的 L2 可以进入主动窗口。",
                    metadata={"atom_type": "decision", "review_status": "pending", "session_id": "s1"},
                    resident_score=0.95,
                ),
                _memory(
                    "rejected",
                    "已拒绝 L2 不应进入主动窗口。",
                    metadata={"atom_type": "decision", "review_status": "rejected", "session_id": "s1"},
                    resident_score=0.95,
                ),
                _memory(
                    "tombstoned",
                    "墓碑记忆不应进入主动窗口。",
                    layer=MemoryLayer.L3,
                    status=MemoryStatus.TOMBSTONED,
                    metadata={"session_id": "s1"},
                    resident_score=0.95,
                ),
                _memory(
                    "other-short-session",
                    "其他会话的短期记忆不应污染当前窗口。",
                    metadata={"atom_type": "lesson", "review_status": "approved", "session_id": "s2"},
                    resident_score=0.95,
                ),
            ]
            for memory in memories:
                store.put_memory_unit(memory)

            candidates = GlobalActiveWindowEngine(store).select(limit=10, session_id="s1")

            ids = [candidate.memory.id for candidate in candidates]
            self.assertEqual(ids, ["approved", "pending", "global", "scene"])
            self.assertIn("global_active_window", candidates[0].matched_by)
            self.assertIn("long_term", candidates[0].matched_by)
            self.assertIn("session_affinity", candidates[0].matched_by)

    def test_strict_session_only_keeps_exact_session_memories(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store = SQLiteMemoryStore(Path(tmpdir) / "memory.sqlite3")
            store.initialize()
            store.put_memory_unit(
                _memory(
                    "session-memory",
                    "当前会话记忆。",
                    metadata={"atom_type": "lesson", "review_status": "approved", "session_id": "s1"},
                    resident_score=0.70,
                )
            )
            store.put_memory_unit(
                _memory(
                    "global-memory",
                    "全局长期记忆。",
                    lifecycle_state=LifecycleState.LONG,
                    metadata={"atom_type": "lesson", "review_status": "approved"},
                    resident_score=0.95,
                )
            )

            candidates = GlobalActiveWindowEngine(store).select(
                limit=10,
                session_id="s1",
                strict_session=True,
            )

            self.assertEqual([candidate.memory.id for candidate in candidates], ["session-memory"])

    def test_l4_uses_high_threshold_and_low_quota(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store = SQLiteMemoryStore(Path(tmpdir) / "memory.sqlite3")
            store.initialize()
            for memory in (
                _memory(
                    "l4-high-a",
                    "高置信长期画像 A。",
                    layer=MemoryLayer.L4,
                    lifecycle_state=LifecycleState.LONG,
                    resident_score=0.95,
                ),
                _memory(
                    "l4-high-b",
                    "高置信长期画像 B。",
                    layer=MemoryLayer.L4,
                    lifecycle_state=LifecycleState.LONG,
                    resident_score=0.90,
                ),
                _memory(
                    "l4-low",
                    "低置信长期画像。",
                    layer=MemoryLayer.L4,
                    lifecycle_state=LifecycleState.LONG,
                    resident_score=0.50,
                ),
                _memory(
                    "l2-approved",
                    "已确认 L2。",
                    lifecycle_state=LifecycleState.LONG,
                    metadata={"atom_type": "lesson", "review_status": "approved"},
                    resident_score=0.80,
                ),
            ):
                store.put_memory_unit(memory)

            report = GlobalActiveWindowEngine(store).report(limit=10, decision_limit=10)

            selected_ids = [candidate.memory.id for candidate in report.candidates]
            decisions = {decision.memory_id: decision for decision in report.decisions}
            self.assertEqual(len([memory_id for memory_id in selected_ids if memory_id.startswith("l4-high")]), 1)
            self.assertIn("l2-approved", selected_ids)
            self.assertEqual(decisions["l4-low"].reason, "l4_below_min_active_score")
            self.assertEqual(report.metadata["l4_candidate_limit"], 1)

    def test_l4_can_be_disabled_by_policy(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store = SQLiteMemoryStore(Path(tmpdir) / "memory.sqlite3")
            store.initialize()
            store.put_memory_unit(
                _memory(
                    "l4-high",
                    "高置信长期画像。",
                    layer=MemoryLayer.L4,
                    lifecycle_state=LifecycleState.LONG,
                    resident_score=0.95,
                )
            )

            report = GlobalActiveWindowEngine(
                store,
                GlobalActiveWindowPolicy(allow_l4_persona=False),
            ).report(limit=10, decision_limit=10)

            decisions = {decision.memory_id: decision for decision in report.decisions}
            self.assertEqual(report.candidates, [])
            self.assertEqual(decisions["l4-high"].reason, "l4_disabled_by_policy")
            self.assertFalse(report.metadata["allow_l4_persona"])

    def test_report_explains_selected_and_filtered_candidates(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store = SQLiteMemoryStore(Path(tmpdir) / "memory.sqlite3")
            store.initialize()
            for memory in (
                _memory(
                    "approved",
                    "已确认记忆。",
                    lifecycle_state=LifecycleState.LONG,
                    metadata={"atom_type": "lesson", "review_status": "approved", "session_id": "s1"},
                    resident_score=0.80,
                ),
                _memory(
                    "pending",
                    "待审记忆。",
                    metadata={"atom_type": "lesson", "review_status": "pending", "session_id": "s1"},
                    resident_score=0.95,
                ),
                _memory(
                    "other-session",
                    "其他会话短期记忆。",
                    metadata={"atom_type": "lesson", "review_status": "approved", "session_id": "s2"},
                    resident_score=0.95,
                ),
            ):
                store.put_memory_unit(memory)

            report = GlobalActiveWindowEngine(store).report(
                limit=1,
                session_id="s1",
                decision_limit=10,
            )

            decisions = {decision.memory_id: decision for decision in report.decisions}
            self.assertEqual(report.selected_count, 1)
            self.assertEqual(report.filtered_count, 2)
            self.assertTrue(decisions["approved"].selected)
            self.assertEqual(decisions["approved"].reason, "selected")
            self.assertEqual(decisions["pending"].reason, "outside_limit")
            self.assertEqual(decisions["other-session"].reason, "session_mismatch")

    def test_cli_active_window_outputs_context_bundle(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "memory.sqlite3"
            store = SQLiteMemoryStore(db_path)
            store.initialize()
            store.put_memory_unit(
                _memory(
                    "approved",
                    "CLI 可输出主动窗口上下文。",
                    lifecycle_state=LifecycleState.LONG,
                    metadata={"atom_type": "lesson", "review_status": "approved", "session_id": "s1"},
                    resident_score=0.75,
                )
            )

            stdout = StringIO()
            with redirect_stdout(stdout):
                exit_code = cli_main(
                    [
                        "active-window",
                        "--db",
                        str(db_path),
                        "--session-id",
                        "s1",
                        "--limit",
                        "5",
                    ]
                )

            payload = json.loads(stdout.getvalue())
            self.assertEqual(exit_code, 0)
            self.assertEqual(payload["metadata"]["included_count"], 1)
            self.assertEqual(payload["items"][0]["retrieval_marker"], "memory://approved")

    def test_cli_active_window_report_outputs_decisions(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "memory.sqlite3"
            store = SQLiteMemoryStore(db_path)
            store.initialize()
            store.put_memory_unit(
                _memory(
                    "approved",
                    "CLI 可输出主动窗口报告。",
                    lifecycle_state=LifecycleState.LONG,
                    metadata={"atom_type": "lesson", "review_status": "approved", "session_id": "s1"},
                    resident_score=0.75,
                )
            )

            stdout = StringIO()
            with redirect_stdout(stdout):
                exit_code = cli_main(
                    [
                        "active-window-report",
                        "--db",
                        str(db_path),
                        "--session-id",
                        "s1",
                        "--limit",
                        "5",
                    ]
                )

            payload = json.loads(stdout.getvalue())
            self.assertEqual(exit_code, 0)
            self.assertEqual(payload["selected_count"], 1)
            self.assertEqual(payload["decisions"][0]["reason"], "selected")

    def test_mcp_memory_active_window_is_registered_and_callable(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "memory.sqlite3"
            store = SQLiteMemoryStore(db_path)
            store.initialize()
            store.put_memory_unit(
                _memory(
                    "approved",
                    "MCP 可输出主动窗口上下文。",
                    lifecycle_state=LifecycleState.LONG,
                    metadata={"atom_type": "lesson", "review_status": "approved", "session_id": "s1"},
                    resident_score=0.75,
                )
            )

            app = create_mcp_server(str(db_path), profile="admin")
            tools = asyncio.run(app.list_tools())
            result = _call_tool(app, "memory_active_window", {"session_id": "s1", "limit": 5})

            self.assertIn("memory_active_window", {tool.name for tool in tools})
            self.assertEqual(result["metadata"]["included_count"], 1)
            self.assertEqual(result["items"][0]["retrieval_marker"], "memory://approved")

    def test_mcp_memory_active_window_report_is_registered_and_callable(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "memory.sqlite3"
            store = SQLiteMemoryStore(db_path)
            store.initialize()
            store.put_memory_unit(
                _memory(
                    "approved",
                    "MCP 可输出主动窗口报告。",
                    lifecycle_state=LifecycleState.LONG,
                    metadata={"atom_type": "lesson", "review_status": "approved", "session_id": "s1"},
                    resident_score=0.75,
                )
            )

            app = create_mcp_server(str(db_path), profile="admin")
            tools = asyncio.run(app.list_tools())
            result = _call_tool(app, "memory_active_window_report", {"session_id": "s1", "limit": 5})

            self.assertIn("memory_active_window_report", {tool.name for tool in tools})
            self.assertEqual(result["selected_count"], 1)
            self.assertEqual(result["decisions"][0]["reason"], "selected")


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
