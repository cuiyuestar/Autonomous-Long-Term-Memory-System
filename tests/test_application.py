import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from altm.application import AltmApplication  # noqa: E402
from altm.cli import main as cli_main  # noqa: E402
from altm.contracts import (  # noqa: E402
    L2Atom,
    L2AtomType,
    LifecycleState,
    MemoryLayer,
    MemoryStatus,
    MemoryUnit,
    ScoreBreakdown,
)
from altm.storage import SQLiteMemoryStore  # noqa: E402
from altm.utils import sha256_text, utc_now_iso  # noqa: E402


class ApplicationUseCaseTest(unittest.TestCase):
    def test_recall_and_build_context_share_filter_parsing(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "memory.sqlite3"
            store = SQLiteMemoryStore(db_path)
            store.initialize()
            store.put_memory_unit(
                _memory(
                    "alpha",
                    "alpha memory for shared application recall",
                    metadata={"review_status": "approved"},
                )
            )
            app = AltmApplication(db_path)

            candidates = app.recall(
                query="alpha",
                limit=5,
                layers=["L2"],
                statuses=["active"],
            )
            bundle = app.build_context(
                query="alpha",
                token_budget=200,
                limit=5,
                layers=["L2"],
                statuses=["active"],
            )

            self.assertEqual(candidates[0].memory.id, "alpha")
            self.assertEqual(bundle.items[0].source_memory_ids, ["alpha"])
            self.assertEqual(bundle.metadata["active_window_mode"], "full")

            legacy_bundle = app.build_context(
                query="alpha",
                token_budget=200,
                limit=5,
                layers=["L2"],
                statuses=["active"],
                active_window_mode="off",
            )
            self.assertNotIn("active_window_mode", legacy_bundle.metadata)

    def test_build_context_can_opt_into_active_window_fusion(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "memory.sqlite3"
            store = SQLiteMemoryStore(db_path)
            store.initialize()
            store.put_memory_unit(
                _memory(
                    "query-hit",
                    "SQLite query target memory",
                    metadata={"atom_type": "lesson", "review_status": "approved", "session_id": "s1"},
                )
            )
            store.put_memory_unit(
                _memory(
                    "active-only",
                    "Global active convention",
                    lifecycle_state=LifecycleState.LONG,
                    metadata={"atom_type": "lesson", "review_status": "approved"},
                    resident_score=0.90,
                )
            )

            bundle = AltmApplication(db_path).build_context(
                query="SQLite query target",
                token_budget=200,
                limit=5,
                session_id="s1",
                active_window_mode="full",
            )

            self.assertEqual(bundle.metadata["active_window_mode"], "full")
            self.assertEqual(bundle.metadata["fusion_strategy"], "query_recall_then_active_window")
            self.assertIn("active-only", {item.source_memory_ids[0] for item in bundle.items})

    def test_build_context_default_active_window_can_be_disabled_by_flag(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "memory.sqlite3"
            store = SQLiteMemoryStore(db_path)
            store.initialize()
            store.put_memory_unit(
                _memory(
                    "query-hit",
                    "SQLite query target memory",
                    metadata={"atom_type": "lesson", "review_status": "approved", "session_id": "s1"},
                )
            )
            store.put_memory_unit(
                _memory(
                    "active-only",
                    "Global active convention",
                    lifecycle_state=LifecycleState.LONG,
                    metadata={"atom_type": "lesson", "review_status": "approved"},
                    resident_score=0.90,
                )
            )

            with patch.dict(
                "os.environ",
                {"ALTM_ENABLE_DEFAULT_ACTIVE_WINDOW_IN_BUILD_CONTEXT": "false"},
            ):
                bundle = AltmApplication(db_path).build_context(
                    query="SQLite query target",
                    token_budget=200,
                    limit=5,
                    session_id="s1",
                )

            self.assertNotIn("active_window_mode", bundle.metadata)
            self.assertNotIn("active-only", {item.source_memory_ids[0] for item in bundle.items})

    def test_build_context_defers_feedback_until_prepare_turn(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "memory.sqlite3"
            store = SQLiteMemoryStore(db_path)
            store.initialize()
            store.put_memory_unit(
                _memory(
                    "active-only",
                    "Global active convention",
                    lifecycle_state=LifecycleState.LONG,
                    metadata={"atom_type": "lesson", "review_status": "approved"},
                    resident_score=0.90,
                )
            )

            bundle = AltmApplication(db_path).build_context(
                query="no lexical match",
                token_budget=200,
                limit=5,
                session_id="s1",
            )

            updated = SQLiteMemoryStore(db_path).get_memory_unit("active-only")
            self.assertIsNotNone(updated)
            self.assertEqual(bundle.metadata["active_window_feedback_memory_ids"], [])
            self.assertEqual(updated.access_count, 0)
            self.assertEqual(updated.useful_access_count, 0)

    def test_active_window_l4_persona_can_be_disabled_by_flag(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "memory.sqlite3"
            store = SQLiteMemoryStore(db_path)
            store.initialize()
            store.put_memory_unit(
                _memory(
                    "l4-persona",
                    "高置信长期画像。",
                    lifecycle_state=LifecycleState.LONG,
                    metadata={"session_id": "global"},
                    resident_score=0.95,
                ).model_copy(update={"layer": MemoryLayer.L4})
            )

            with patch.dict(
                "os.environ",
                {"ALTM_ENABLE_L4_PERSONA_ACTIVE_WINDOW": "false"},
            ):
                bundle = AltmApplication(db_path).active_window(limit=5, layers=["L4"])

            self.assertEqual(bundle.items, [])

    def test_runtime_cycle_runs_mvp_loop_with_degraded_l2_extraction(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "memory.sqlite3"
            app = AltmApplication(db_path)

            with patch.dict("os.environ", {}, clear=True):
                result = app.runtime_cycle(
                    session_id="mvp",
                    content="我们决定使用 SQLite 作为 MVP 本地记忆存储。",
                    token_budget=300,
                    recall_limit=5,
                    run_maintenance=True,
                )

            self.assertEqual(result["status"], "complete")
            self.assertEqual(result["steps"]["capture_l0"]["status"], "applied")
            self.assertEqual(result["steps"]["fold_l1"]["status"], "degraded")
            self.assertEqual(result["steps"]["extract_l2"]["status"], "degraded")
            self.assertEqual(result["steps"]["maintenance_cycle"]["status"], "applied")
            self.assertGreaterEqual(result["summary"]["context_included_count"], 1)
            self.assertGreaterEqual(len(result["context"]["items"]), 1)

    def test_runtime_session_cycle_processes_multiple_messages(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "memory.sqlite3"
            app = AltmApplication(db_path)

            with patch.dict("os.environ", {}, clear=True):
                result = app.runtime_session_cycle(
                    session_id="mvp-session",
                    messages=[
                        {"role": "user", "content": "我们决定采用 SQLite。"},
                        {"role": "assistant", "content": "记录为 MVP 本地存储决策。"},
                    ],
                    token_budget=300,
                    run_maintenance=False,
                )

            self.assertEqual(result["status"], "complete")
            self.assertEqual(result["message_count"], 2)
            self.assertEqual(result["captured_count"], 2)
            self.assertEqual(result["steps"]["fold_l1"]["status"], "degraded")
            self.assertEqual(result["steps"]["extract_l2"]["status"], "degraded")
            self.assertGreaterEqual(result["summary"]["context_included_count"], 1)

    def test_mvp_chat_persists_assistant_turn_and_feedback(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "memory.sqlite3"
            app = AltmApplication(db_path)

            with patch.dict("os.environ", {}, clear=True):
                result = app.mvp_chat(
                    session_id="mvp-chat",
                    content="我们决定让 Phase 8 优先跑通交互式记忆闭环。",
                    token_budget=300,
                    run_maintenance=False,
                    assistant_content="已基于 memory://turn 记录本轮 Phase 8 闭环目标。",
                )

            self.assertEqual(result["status"], "complete")
            self.assertTrue(result["deprecated"])
            self.assertEqual(result["committed_turn"]["status"], "committed")
            self.assertEqual(result["assistant_response"]["cited_memory_ids"], [])

            store = SQLiteMemoryStore(db_path)
            l0_units = store.list_l0_by_session("mvp-chat")
            self.assertEqual([unit.metadata["role"] for unit in l0_units], ["user", "assistant"])

    def test_maintenance_cycle_runs_non_embedding_steps_when_model_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "memory.sqlite3"
            store = SQLiteMemoryStore(db_path)
            store.initialize()
            store.put_memory_unit(
                _memory(
                    "pref-a",
                    "用户偏好中文技术深度解释。",
                    metadata={"atom_type": "preference", "review_status": "approved"},
                )
            )
            store.put_memory_unit(
                _memory(
                    "pref-b",
                    "用户偏好直接推进开发。",
                    metadata={"atom_type": "preference", "review_status": "approved"},
                )
            )

            with patch.dict("os.environ", {}, clear=True):
                result = AltmApplication(db_path).maintenance_cycle(dry_run=False)

            self.assertEqual(result["status"], "complete")
            self.assertEqual(result["steps"]["semantic_dedup"]["status"], "skipped")
            self.assertEqual(result["steps"]["cross_session_l3_candidates"]["status"], "skipped")
            self.assertEqual(result["steps"]["build_l4_persona_candidates"]["status"], "skipped")
            self.assertEqual(result["summary"]["autonomous_l4_applied_count"], 0)
            self.assertEqual(
                len(SQLiteMemoryStore(db_path).list_memory_units(layer=MemoryLayer.L4)),
                0,
            )

    def test_maintenance_cycle_can_still_run_legacy_review_actions_as_compat_path(self) -> None:
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
            app = AltmApplication(db_path)
            app.review_mark(
                target_type="graph_edge",
                target_id=edge_id,
                status="approved",
            )

            with patch.dict("os.environ", {}, clear=True):
                result = app.maintenance_cycle(
                    use_autonomous_governance=False,
                    apply_review_actions=True,
                    dry_run=False,
                )
                second_result = app.maintenance_cycle(
                    use_autonomous_governance=False,
                    apply_review_actions=True,
                    dry_run=False,
                )

            apply_result = result["steps"]["apply_review_actions"]["result"]
            second_apply_result = second_result["steps"]["apply_review_actions"]["result"]
            edge = SQLiteMemoryStore(db_path).get_graph_edge(edge_id)
            events = SQLiteMemoryStore(db_path).list_review_events(
                target_type="graph_edge",
                target_id=edge_id,
                event_type="review_apply",
            )

            self.assertEqual(apply_result["applied_count"], 1)
            self.assertEqual(result["summary"]["review_action_applied_count"], 1)
            self.assertEqual(second_apply_result["applied_count"], 0)
            self.assertEqual(second_apply_result["skipped_already_applied_count"], 1)
            self.assertIsNotNone(edge)
            self.assertEqual(edge["metadata"]["candidate_status"], "confirmed")
            self.assertEqual(len(events), 1)

    def test_maintenance_cycle_legacy_second_confirmation_is_compat_only(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "memory.sqlite3"
            store = SQLiteMemoryStore(db_path)
            store.initialize()
            persona = _memory(
                "l4-persona",
                "用户偏好高技术密度中文解释。",
                metadata={
                    "builder": "l4_persona_candidate_builder",
                    "governance_review_status": "pending",
                    "candidate_status": "candidate",
                },
            ).model_copy(
                update={
                    "layer": MemoryLayer.L4,
                    "status": MemoryStatus.OBSERVING,
                }
            )
            store.put_memory_unit(persona)
            app = AltmApplication(db_path)
            app.review_mark(
                target_type="memory_unit",
                target_id=persona.id,
                status="approved",
                kind="l4_persona_candidate",
            )

            with patch.dict("os.environ", {}, clear=True):
                skipped = app.maintenance_cycle(
                    use_autonomous_governance=False,
                    apply_review_actions=True,
                    dry_run=False,
                )
                applied = app.maintenance_cycle(
                    use_autonomous_governance=False,
                    apply_review_actions=True,
                    allow_second_confirm_review_actions=True,
                    dry_run=False,
                )

            skipped_apply = skipped["steps"]["apply_review_actions"]["result"]
            applied_apply = applied["steps"]["apply_review_actions"]["result"]
            updated = SQLiteMemoryStore(db_path).get_memory_unit(persona.id)

            self.assertEqual(skipped_apply["applied_count"], 0)
            self.assertEqual(skipped_apply["skipped_second_confirmation_count"], 1)
            self.assertEqual(applied_apply["applied_count"], 1)
            self.assertIsNotNone(updated)
            self.assertEqual(updated.status, MemoryStatus.ACTIVE)
            self.assertEqual(updated.lifecycle_state, LifecycleState.PERMANENT)
            self.assertEqual(updated.metadata["candidate_status"], "activated")

    def test_review_mark_updates_l2_typed_table_through_application(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "memory.sqlite3"
            store = SQLiteMemoryStore(db_path)
            store.initialize()
            memory = _memory(
                "l2-decision",
                "application review target",
                metadata={"atom_type": "decision", "review_status": "pending"},
            )
            store.put_l2_atom(_atom(memory.id), memory)

            item = AltmApplication(db_path).review_mark(
                target_type="memory_unit",
                target_id=memory.id,
                status="approved",
            )

            self.assertIsNotNone(item)
            self.assertEqual(item.review_status.value, "approved")
            stored = store.get_memory_unit(memory.id)
            self.assertIsNotNone(stored)
            self.assertEqual(stored.metadata["review_status"], "approved")
            with store.connect() as connection:
                row = connection.execute(
                    "SELECT review_status FROM l2_decisions WHERE memory_unit_id = ?",
                    (memory.id,),
                ).fetchone()
            self.assertEqual(row["review_status"], "approved")

    def test_cli_search_uses_application_service(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "memory.sqlite3"
            store = SQLiteMemoryStore(db_path)
            store.initialize()
            store.put_memory_unit(
                _memory(
                    "cli-alpha",
                    "cli alpha searchable memory",
                    metadata={"review_status": "approved"},
                )
            )
            output = io.StringIO()

            with redirect_stdout(output):
                exit_code = cli_main(
                    [
                        "search",
                        "--db",
                        str(db_path),
                        "--query",
                        "alpha",
                        "--layer",
                        "L2",
                    ]
                )

            payload = json.loads(output.getvalue())
            self.assertEqual(exit_code, 0)
            self.assertEqual(payload[0]["memory"]["id"], "cli-alpha")

    def test_cli_runtime_cycle_outputs_context(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "memory.sqlite3"
            output = io.StringIO()

            with patch.dict("os.environ", {}, clear=True), redirect_stdout(output):
                exit_code = cli_main(
                    [
                        "runtime-cycle",
                        "--db",
                        str(db_path),
                        "--session-id",
                        "mvp",
                        "--content",
                        "MVP runtime cycle should build usable context.",
                        "--skip-maintenance",
                    ]
                )

            payload = json.loads(output.getvalue())
            self.assertEqual(exit_code, 0)
            self.assertEqual(payload["status"], "complete")
            self.assertGreaterEqual(payload["summary"]["context_included_count"], 1)

    def test_cli_runtime_session_cycle_outputs_context(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "memory.sqlite3"
            output = io.StringIO()

            with patch.dict("os.environ", {}, clear=True), redirect_stdout(output):
                exit_code = cli_main(
                    [
                        "runtime-session-cycle",
                        "--db",
                        str(db_path),
                        "--session-id",
                        "mvp-session",
                        "--message",
                        "user:我们决定采用 SQLite。",
                        "--message",
                        "assistant:记录为 MVP 本地存储决策。",
                        "--skip-maintenance",
                    ]
                )

            payload = json.loads(output.getvalue())
            self.assertEqual(exit_code, 0)
            self.assertEqual(payload["captured_count"], 2)
            self.assertGreaterEqual(payload["summary"]["context_included_count"], 1)

    def test_cli_mvp_chat_outputs_assistant_response(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "memory.sqlite3"
            output = io.StringIO()

            with patch.dict("os.environ", {}, clear=True), redirect_stdout(output):
                exit_code = cli_main(
                    [
                        "mvp-chat",
                        "--db",
                        str(db_path),
                        "--tenant-id",
                        "local",
                        "--workspace-id",
                        "default",
                        "--user-id",
                        "default",
                        "--agent-id",
                        "default",
                        "--session-id",
                        "mvp-chat",
                        "--content",
                        "Phase 8 should provide an interactive memory loop.",
                        "--assistant-content",
                        "已记录 Phase 8 交互式记忆闭环。",
                        "--skip-maintenance",
                    ]
                )

            payload = json.loads(output.getvalue())
            self.assertEqual(exit_code, 0)
            self.assertEqual(payload["status"], "complete")
            self.assertIn("assistant_response", payload)
            self.assertEqual(payload["committed_turn"]["status"], "committed")

    def test_cli_mvp_chat_rejects_synthetic_repl_mode(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "memory.sqlite3"
            stderr = io.StringIO()

            with (
                patch.dict("os.environ", {}, clear=True),
                redirect_stderr(stderr),
                self.assertRaises(SystemExit),
            ):
                    cli_main(
                        [
                            "mvp-chat",
                            "--db",
                            str(db_path),
                            "--session-id",
                            "mvp-chat-repl",
                        ]
                    )

    def test_cli_maintenance_cycle_can_skip_review_actions(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "memory.sqlite3"
            SQLiteMemoryStore(db_path).initialize()
            output = io.StringIO()

            with patch.dict("os.environ", {}, clear=True), redirect_stdout(output):
                exit_code = cli_main(
                    [
                        "maintenance-cycle",
                        "--db",
                        str(db_path),
                        "--skip-review-actions",
                    ]
                )

            payload = json.loads(output.getvalue())
            self.assertEqual(exit_code, 0)
            self.assertEqual(payload["steps"]["apply_review_actions"]["status"], "skipped")


def _memory(
    memory_id: str,
    content: str,
    lifecycle_state: LifecycleState = LifecycleState.SHORT,
    metadata: dict[str, object] | None = None,
    resident_score: float = 0.0,
) -> MemoryUnit:
    now = utc_now_iso()
    return MemoryUnit(
        id=memory_id,
        layer=MemoryLayer.L2,
        lifecycle_state=lifecycle_state,
        status=MemoryStatus.ACTIVE,
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


def _atom(memory_id: str) -> L2Atom:
    return L2Atom(
        id=memory_id,
        atom_type=L2AtomType.DECISION,
        text="application review target",
        confidence=0.8,
        extraction_reason="test fixture",
        source_memory_id="l1-source",
    )


if __name__ == "__main__":
    unittest.main()
