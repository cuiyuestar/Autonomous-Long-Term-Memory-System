from pathlib import Path
from contextlib import redirect_stdout
import json
import sys
import tempfile
import unittest
from io import StringIO


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from altm.contracts import (  # noqa: E402
    LifecycleState,
    MemoryLayer,
    MemoryStatus,
    MemoryUnit,
    RecallQuery,
)
from altm.application import AltmApplication  # noqa: E402
from altm.cli import main as cli_main  # noqa: E402
from altm.folding import CrossSessionL3CandidateFinder, RuleBasedL3SceneBuilder  # noqa: E402
from altm.retrieval import FTSRetrievalEngine  # noqa: E402
from altm.storage import SQLiteMemoryStore  # noqa: E402
from altm.utils import sha256_text, utc_now_iso  # noqa: E402


class L3SceneBuilderTest(unittest.TestCase):
    def test_builds_l3_scene_from_l2_group_with_evidence_refs(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store = SQLiteMemoryStore(Path(tmpdir) / "memory.sqlite3")
            store.initialize()
            first = _l2("l2-a", "项目决定接入真实向量模型。", "decision", "s1")
            second = _l2("l2-b", "项目决定保留本地 FTS 回退。", "decision", "s1")
            unrelated = _l2("l2-c", "单独的约束不足以形成场景。", "constraint", "s1")
            for memory in (first, second, unrelated):
                store.put_memory_unit(memory)

            scenes = RuleBasedL3SceneBuilder(store).build(session_id="s1", min_group_size=2)

            self.assertEqual(len(scenes), 1)
            scene = scenes[0]
            self.assertEqual(scene.layer, MemoryLayer.L3)
            self.assertEqual(scene.status, MemoryStatus.OBSERVING)
            self.assertEqual(scene.metadata["scene_key"], "s1:decision")
            self.assertEqual(scene.metadata["source_memory_ids"], ["l2-a", "l2-b"])
            self.assertEqual(len(scene.evidence_refs), 2)

            recalled = FTSRetrievalEngine(store).recall(
                RecallQuery(text="真实向量模型", top_k=5, preferred_layers=[MemoryLayer.L3])
            )
            self.assertTrue(any(candidate.memory.id == scene.id for candidate in recalled))

    def test_finds_cross_session_l3_candidates_from_cached_embeddings(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "memory.sqlite3"
            store = SQLiteMemoryStore(db_path)
            store.initialize()
            first = _l2("l2-s1", "两个项目都采用 SQLite 本地记忆存储。", "decision", "s1")
            second = _l2("l2-s2", "另一个会话也选择 SQLite 作为本地记忆存储。", "decision", "s2")
            same_session = _l2("l2-s1b", "同会话不应生成跨会话候选。", "decision", "s1")
            other_type = _l2("l2-s3", "SQLite 本地记忆存储约束。", "constraint", "s3")
            for memory, vector in (
                (first, [1.0, 0.0]),
                (second, [0.99, 0.05]),
                (same_session, [0.0, 1.0]),
                (other_type, [1.0, 0.0]),
            ):
                store.put_memory_unit(memory)
                store.put_memory_embedding(memory.id, "test-embedding", memory.content_hash, vector)

            candidates = CrossSessionL3CandidateFinder(store).find(
                embedding_model="test-embedding",
                threshold=0.95,
            )
            edges = store.list_graph_edges("cross_session_l3_candidate")

            self.assertEqual(len(candidates), 1)
            self.assertEqual(candidates[0].source_memory_id, first.id)
            self.assertEqual(candidates[0].target_memory_id, second.id)
            self.assertEqual(candidates[0].source_session_id, "s1")
            self.assertEqual(candidates[0].target_session_id, "s2")
            self.assertEqual(len(edges), 1)
            self.assertEqual(edges[0]["metadata"]["candidate_status"], "candidate")

    def test_application_cross_session_l3_candidates_supports_dry_run(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "memory.sqlite3"
            store = SQLiteMemoryStore(db_path)
            store.initialize()
            first = _l2("l2-s1", "两个项目都采用 SQLite 本地记忆存储。", "decision", "s1")
            second = _l2("l2-s2", "另一个会话也选择 SQLite 作为本地记忆存储。", "decision", "s2")
            for memory, vector in ((first, [1.0, 0.0]), (second, [0.99, 0.05])):
                store.put_memory_unit(memory)
                store.put_memory_embedding(memory.id, "test-embedding", memory.content_hash, vector)

            result = AltmApplication(db_path).cross_session_l3_candidates(
                model="test-embedding",
                threshold=0.95,
                dry_run=True,
            )

            self.assertTrue(result["enabled"])
            self.assertTrue(result["dry_run"])
            self.assertEqual(result["candidate_count"], 1)
            self.assertEqual(store.list_graph_edges("cross_session_l3_candidate"), [])

    def test_cli_cross_session_l3_candidates_outputs_candidates(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "memory.sqlite3"
            store = SQLiteMemoryStore(db_path)
            store.initialize()
            first = _l2("l2-s1", "两个项目都采用 SQLite 本地记忆存储。", "decision", "s1")
            second = _l2("l2-s2", "另一个会话也选择 SQLite 作为本地记忆存储。", "decision", "s2")
            for memory, vector in ((first, [1.0, 0.0]), (second, [0.99, 0.05])):
                store.put_memory_unit(memory)
                store.put_memory_embedding(memory.id, "test-embedding", memory.content_hash, vector)

            stdout = StringIO()
            with redirect_stdout(stdout):
                exit_code = cli_main(
                    [
                        "cross-session-l3-candidates",
                        "--db",
                        str(db_path),
                        "--model",
                        "test-embedding",
                        "--threshold",
                        "0.95",
                    ]
                )

            payload = json.loads(stdout.getvalue())
            self.assertEqual(exit_code, 0)
            self.assertEqual(payload["candidate_count"], 1)
            self.assertEqual(len(store.list_graph_edges("cross_session_l3_candidate")), 1)


def _l2(memory_id: str, summary: str, atom_type: str, session_id: str) -> MemoryUnit:
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
        metadata={
            "atom_type": atom_type,
            "review_status": "approved",
            "session_id": session_id,
        },
    )


if __name__ == "__main__":
    unittest.main()
