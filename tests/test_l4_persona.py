import json
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from altm.application import AltmApplication  # noqa: E402
from altm.cli import main as cli_main  # noqa: E402
from altm.contracts import LifecycleState, MemoryLayer, MemoryStatus, MemoryUnit  # noqa: E402
from altm.folding import L4PersonaCandidateBuilder  # noqa: E402
from altm.storage import SQLiteMemoryStore  # noqa: E402
from altm.utils import sha256_text, utc_now_iso  # noqa: E402


class L4PersonaCandidateBuilderTest(unittest.TestCase):
    def test_builds_observing_l4_persona_candidate_from_reviewed_l2(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store = SQLiteMemoryStore(Path(tmpdir) / "memory.sqlite3")
            store.initialize()
            first = _l2("pref-a", "用户偏好中文技术深度解释。", "preference")
            second = _l2("pref-b", "用户偏好直接推进开发。", "preference")
            rejected = _l2("pref-c", "被拒绝的偏好不应进入画像。", "preference", review_status="rejected")
            other = _l2("task-a", "临时任务状态不应进入画像。", "task_state")
            for memory in (first, second, rejected, other):
                store.put_memory_unit(memory)

            candidates = L4PersonaCandidateBuilder(store).build(min_support=2)

            self.assertEqual(len(candidates), 1)
            candidate = candidates[0]
            self.assertEqual(candidate.layer, MemoryLayer.L4)
            self.assertEqual(candidate.lifecycle_state, LifecycleState.LONG)
            self.assertEqual(candidate.status, MemoryStatus.OBSERVING)
            self.assertEqual(candidate.metadata["atom_type"], "preference")
            self.assertEqual(candidate.metadata["candidate_status"], "candidate")
            self.assertEqual(candidate.metadata["source_memory_ids"], ["pref-a", "pref-b"])
            self.assertEqual(len(candidate.evidence_refs), 2)

    def test_application_l4_persona_candidates_supports_dry_run(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "memory.sqlite3"
            store = SQLiteMemoryStore(db_path)
            store.initialize()
            store.put_memory_unit(_l2("pref-a", "用户偏好中文技术深度解释。", "preference"))
            store.put_memory_unit(_l2("pref-b", "用户偏好直接推进开发。", "preference"))

            result = AltmApplication(db_path).build_l4_persona_candidates(dry_run=True)

            self.assertTrue(result["enabled"])
            self.assertTrue(result["dry_run"])
            self.assertEqual(result["candidate_count"], 1)
            self.assertEqual(
                SQLiteMemoryStore(db_path).list_memory_units(layer=MemoryLayer.L4),
                [],
            )

    def test_cli_build_l4_persona_candidates_outputs_candidates(self) -> None:
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
                        "build-l4-persona-candidates",
                        "--db",
                        str(db_path),
                        "--min-support",
                        "2",
                    ]
                )

            payload = json.loads(stdout.getvalue())
            self.assertEqual(exit_code, 0)
            self.assertEqual(payload["candidate_count"], 1)
            self.assertEqual(len(SQLiteMemoryStore(db_path).list_memory_units(layer=MemoryLayer.L4)), 1)

    def test_cli_maintenance_cycle_runs_persona_step_without_embedding_model(self) -> None:
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
                        "maintenance-cycle",
                        "--db",
                        str(db_path),
                        "--persona-min-support",
                        "2",
                    ]
                )

            payload = json.loads(stdout.getvalue())
            self.assertEqual(exit_code, 0)
            self.assertEqual(payload["status"], "complete")
            self.assertEqual(payload["steps"]["semantic_dedup"]["status"], "skipped")
            self.assertEqual(payload["steps"]["build_l4_persona_candidates"]["status"], "skipped")
            self.assertEqual(payload["summary"]["autonomous_l4_applied_count"], 0)


def _l2(
    memory_id: str,
    summary: str,
    atom_type: str,
    review_status: str = "approved",
) -> MemoryUnit:
    now = utc_now_iso()
    content = '{"text": "%s"}' % summary
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
        metadata={"atom_type": atom_type, "review_status": review_status},
    )


if __name__ == "__main__":
    unittest.main()
