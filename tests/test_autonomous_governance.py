from contextlib import redirect_stdout
from io import StringIO
import json
from pathlib import Path
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from altm.application import AltmApplication  # noqa: E402
from altm.cli import main as cli_main  # noqa: E402
from altm.contracts import LifecycleState, MemoryLayer, MemoryStatus, MemoryUnit  # noqa: E402
from altm.governance import (  # noqa: E402
    AUTONOMOUS_EVENT_APPLIED,
    AUTONOMOUS_EVENT_DECIDED,
    AUTONOMOUS_EVENT_EVALUATED,
    AUTONOMOUS_EVENT_ROLLED_BACK,
)
from altm.storage import SQLiteMemoryStore  # noqa: E402
from altm.utils import sha256_text, utc_now_iso  # noqa: E402


class AutonomousGovernanceTest(unittest.TestCase):
    def test_autonomous_governance_activates_l4_without_review_mark(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "memory.sqlite3"
            store = SQLiteMemoryStore(db_path)
            store.initialize()
            store.put_memory_unit(_l2("pref-a", "用户偏好中文技术深度解释。", "preference"))
            store.put_memory_unit(_l2("pref-b", "用户偏好直接推进开发。", "preference"))

            result = AltmApplication(db_path).autonomous_governance_cycle(
                model_mode="auto",
                rule_fallback=True,
            )
            l4_units = SQLiteMemoryStore(db_path).list_memory_units(layer=MemoryLayer.L4)
            events = SQLiteMemoryStore(db_path).list_review_events(
                event_type=AUTONOMOUS_EVENT_APPLIED,
                limit=20,
            )

            self.assertEqual(result["summary"]["l4_persona_applied_count"], 1)
            self.assertEqual(len(l4_units), 1)
            self.assertEqual(l4_units[0].status, MemoryStatus.ACTIVE)
            self.assertEqual(l4_units[0].lifecycle_state, LifecycleState.PERMANENT)
            self.assertEqual(l4_units[0].metadata["autonomous_decision"], "execute")
            self.assertEqual(events[0].event_type, AUTONOMOUS_EVENT_APPLIED)
            self.assertEqual(events[0].metadata["fallback_mode"], "small_model_only")
            self.assertTrue(events[0].metadata["model_chain"]["small_model"]["available"])

    def test_autonomous_governance_materializes_cross_session_l3_without_review(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "memory.sqlite3"
            store = SQLiteMemoryStore(db_path)
            store.initialize()
            first = _l2("l2-s1", "两个项目都采用 SQLite 本地记忆存储。", "decision", "s1")
            second = _l2("l2-s2", "另一个会话也选择 SQLite 作为本地记忆存储。", "decision", "s2")
            for memory, vector in ((first, [1.0, 0.0]), (second, [0.99, 0.05])):
                store.put_memory_unit(memory)
                store.put_memory_embedding(memory.id, "test-embedding", memory.content_hash, vector)

            result = AltmApplication(db_path).autonomous_governance_cycle(
                model="test-embedding",
                l3_threshold=0.95,
            )
            l3_units = SQLiteMemoryStore(db_path).list_memory_units(layer=MemoryLayer.L3)
            edges = SQLiteMemoryStore(db_path).list_graph_edges("cross_session_l3_candidate")

            self.assertEqual(result["summary"]["cross_session_l3_applied_count"], 1)
            self.assertEqual(len(l3_units), 1)
            self.assertEqual(l3_units[0].status, MemoryStatus.ACTIVE)
            self.assertEqual(l3_units[0].metadata["builder"], "autonomous_governance_engine")
            self.assertEqual(edges[0]["metadata"]["candidate_status"], "autonomous_materialized")

    def test_autonomous_governance_semantic_dedup_uses_rule_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "memory.sqlite3"
            store = SQLiteMemoryStore(db_path)
            store.initialize()
            first = _l2("decision-a", "系统采用 SQLite 作为本地记忆存储。", "decision")
            second = _l2("decision-b", "项目选择 SQLite 承载本地记忆数据。", "decision")
            for memory, vector in ((first, [1.0, 0.0]), (second, [0.99, 0.05])):
                store.put_memory_unit(memory)
                store.put_memory_embedding(memory.id, "test-embedding", memory.content_hash, vector)

            result = AltmApplication(db_path).autonomous_governance_cycle(
                model="test-embedding",
                semantic_threshold=0.95,
                semantic_auto_merge_threshold=0.95,
            )
            canonical = SQLiteMemoryStore(db_path).get_memory_unit(first.id)
            duplicate = SQLiteMemoryStore(db_path).get_memory_unit(second.id)
            decisions = SQLiteMemoryStore(db_path).list_review_events(
                event_type=AUTONOMOUS_EVENT_DECIDED,
                limit=20,
            )
            evaluations = SQLiteMemoryStore(db_path).list_review_events(
                event_type=AUTONOMOUS_EVENT_EVALUATED,
                limit=20,
            )

            self.assertEqual(result["summary"]["semantic_applied_count"], 1)
            self.assertIsNotNone(canonical)
            self.assertIsNotNone(duplicate)
            self.assertEqual(duplicate.status, MemoryStatus.TOMBSTONED)
            self.assertIn(second.id, canonical.metadata["merged_duplicate_ids"])
            self.assertTrue(any(event.metadata["action_type"] == "merge_duplicate" for event in decisions))
            self.assertTrue(any(event.metadata["action_type"] == "merge_duplicate" for event in evaluations))

    def test_autonomous_governance_rollback_tombstones_created_l4(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "memory.sqlite3"
            store = SQLiteMemoryStore(db_path)
            store.initialize()
            store.put_memory_unit(_l2("pref-a", "用户偏好中文技术深度解释。", "preference"))
            store.put_memory_unit(_l2("pref-b", "用户偏好直接推进开发。", "preference"))

            result = AltmApplication(db_path).autonomous_governance_cycle()
            memory_id = result["steps"]["l4_persona"]["memory_ids"][0]
            rollback = AltmApplication(db_path).autonomous_governance_rollback(
                target_type="memory_unit",
                target_id=memory_id,
                reason="test_rollback",
            )
            rolled_back = SQLiteMemoryStore(db_path).get_memory_unit(memory_id)
            events = SQLiteMemoryStore(db_path).list_review_events(
                event_type=AUTONOMOUS_EVENT_ROLLED_BACK,
                limit=20,
            )

            self.assertTrue(rollback["rolled_back"])
            self.assertIsNotNone(rolled_back)
            self.assertEqual(rolled_back.status, MemoryStatus.TOMBSTONED)
            self.assertEqual(events[0].target_id, memory_id)

    def test_cli_autonomous_governance_cycle_outputs_summary(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "memory.sqlite3"
            store = SQLiteMemoryStore(db_path)
            store.initialize()
            store.put_memory_unit(_l2("pref-a", "用户偏好中文技术深度解释。", "preference"))
            store.put_memory_unit(_l2("pref-b", "用户偏好直接推进开发。", "preference"))

            stdout = StringIO()
            with redirect_stdout(stdout):
                exit_code = cli_main(
                    [
                        "autonomous-governance-cycle",
                        "--db",
                        str(db_path),
                    ]
                )

            payload = json.loads(stdout.getvalue())
            self.assertEqual(exit_code, 0)
            self.assertEqual(payload["status"], "complete")
            self.assertEqual(payload["summary"]["l4_persona_applied_count"], 1)

    def test_cli_autonomous_governance_rollback_outputs_result(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "memory.sqlite3"
            store = SQLiteMemoryStore(db_path)
            store.initialize()
            store.put_memory_unit(_l2("pref-a", "用户偏好中文技术深度解释。", "preference"))
            store.put_memory_unit(_l2("pref-b", "用户偏好直接推进开发。", "preference"))
            result = AltmApplication(db_path).autonomous_governance_cycle()
            memory_id = result["steps"]["l4_persona"]["memory_ids"][0]

            stdout = StringIO()
            with redirect_stdout(stdout):
                exit_code = cli_main(
                    [
                        "autonomous-governance-rollback",
                        "--db",
                        str(db_path),
                        "--target-type",
                        "memory_unit",
                        "--target-id",
                        memory_id,
                    ]
                )

            payload = json.loads(stdout.getvalue())
            self.assertEqual(exit_code, 0)
            self.assertTrue(payload["rolled_back"])


def _l2(
    memory_id: str,
    summary: str,
    atom_type: str,
    session_id: str | None = None,
) -> MemoryUnit:
    now = utc_now_iso()
    content = '{"text": "%s"}' % summary
    metadata = {
        "atom_type": atom_type,
        "review_status": "pending",
    }
    if session_id is not None:
        metadata["session_id"] = session_id
    return MemoryUnit(
        id=memory_id,
        layer=MemoryLayer.L2,
        lifecycle_state=LifecycleState.SHORT,
        status=MemoryStatus.ACTIVE,
        content=content,
        content_hash=sha256_text(content),
        summary=summary,
        created_at=now,
        updated_at=now,
        metadata=metadata,
    )


if __name__ == "__main__":
    unittest.main()
