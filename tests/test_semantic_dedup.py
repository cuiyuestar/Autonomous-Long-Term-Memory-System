from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from altm.application import AltmApplication  # noqa: E402
from altm.contracts import (  # noqa: E402
    LifecycleState,
    MemoryLayer,
    MemoryStatus,
    MemoryUnit,
    ScoreBreakdown,
)
from altm.governance import SemanticDedupPolicy, SemanticDeduper  # noqa: E402
from altm.storage import SQLiteMemoryStore  # noqa: E402
from altm.utils import sha256_text, utc_now_iso  # noqa: E402


class SemanticDedupTest(unittest.TestCase):
    def test_marks_same_type_high_similarity_l2_as_duplicate_candidate(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store = SQLiteMemoryStore(Path(tmpdir) / "memory.sqlite3")
            store.initialize()
            first = _l2("decision-a", "系统采用 SQLite 作为本地记忆存储。", "decision")
            second = _l2("decision-b", "项目选择 SQLite 承载本地记忆数据。", "decision")
            other_type = _l2("constraint-a", "系统采用 SQLite 作为本地记忆存储。", "constraint")
            far = _l2("decision-c", "Context Gateway 负责上下文注入。", "decision")
            for memory in (first, second, other_type, far):
                store.put_memory_unit(memory)

            store.put_memory_embedding(first.id, "test-embedding", first.content_hash, [1.0, 0.0])
            store.put_memory_embedding(second.id, "test-embedding", second.content_hash, [0.99, 0.05])
            store.put_memory_embedding(other_type.id, "test-embedding", other_type.content_hash, [1.0, 0.0])
            store.put_memory_embedding(far.id, "test-embedding", far.content_hash, [0.0, 1.0])

            candidates = SemanticDeduper(
                store,
                SemanticDedupPolicy(similarity_threshold=0.95),
            ).mark_candidates("test-embedding")
            edges = store.list_graph_edges("semantic_duplicate_candidate")

            self.assertEqual(len(candidates), 1)
            self.assertEqual(candidates[0].source_memory_id, first.id)
            self.assertEqual(candidates[0].target_memory_id, second.id)
            self.assertEqual(len(edges), 1)
            self.assertEqual(edges[0]["source_memory_id"], first.id)
            self.assertEqual(edges[0]["target_memory_id"], second.id)
            self.assertEqual(edges[0]["metadata"]["atom_type"], "decision")

    def test_application_semantic_dedup_auto_merges_and_tombstones_when_enabled(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "memory.sqlite3"
            store = SQLiteMemoryStore(db_path)
            store.initialize()
            first = _l2("decision-a", "系统采用 SQLite 作为本地记忆存储。", "decision")
            second = _l2("decision-b", "项目选择 SQLite 承载本地记忆数据。", "decision")
            for memory, vector in ((first, [1.0, 0.0]), (second, [0.99, 0.05])):
                store.put_memory_unit(memory)
                store.put_memory_embedding(memory.id, "test-embedding", memory.content_hash, vector)

            with patch.dict(
                "os.environ",
                {
                    "ALTM_ENABLE_HIGH_RISK_DEFAULTS": "true",
                    "ALTM_ENABLE_AUTO_L2_SEMANTIC_MERGE": "true",
                    "ALTM_ENABLE_AUTO_L2_TOMBSTONE": "true",
                },
            ):
                result = AltmApplication(db_path).semantic_dedup(
                    model="test-embedding",
                    threshold=0.95,
                )

            canonical = store.get_memory_unit(first.id)
            duplicate = store.get_memory_unit(second.id)
            edges = store.list_graph_edges("semantic_duplicate_candidate")

            self.assertEqual(result["candidate_count"], 1)
            self.assertEqual(result["auto_resolution_count"], 1)
            self.assertIsNotNone(canonical)
            self.assertIsNotNone(duplicate)
            self.assertEqual(canonical.metadata["merged_duplicate_ids"], [second.id])
            self.assertEqual(duplicate.status, MemoryStatus.TOMBSTONED)
            self.assertEqual(duplicate.metadata["superseded_by"], first.id)
            self.assertEqual(edges[0]["metadata"]["resolution_status"], "auto_merged")
            self.assertTrue(edges[0]["metadata"]["auto_tombstone"])

            recalled = AltmApplication(db_path).recall("项目选择 SQLite", limit=5)
            self.assertNotIn(second.id, {candidate.memory.id for candidate in recalled})

            events = store.list_review_events(target_type="graph_edge", target_id=edges[0]["id"])
            event_types = {event.event_type for event in events}
            self.assertIn("semantic_duplicate_marked", event_types)
            self.assertIn("semantic_auto_merge_applied", event_types)

    def test_application_semantic_dedup_can_keep_auto_resolution_disabled(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "memory.sqlite3"
            store = SQLiteMemoryStore(db_path)
            store.initialize()
            first = _l2("decision-a", "系统采用 SQLite 作为本地记忆存储。", "decision")
            second = _l2("decision-b", "项目选择 SQLite 承载本地记忆数据。", "decision")
            for memory, vector in ((first, [1.0, 0.0]), (second, [0.99, 0.05])):
                store.put_memory_unit(memory)
                store.put_memory_embedding(memory.id, "test-embedding", memory.content_hash, vector)

            with patch.dict(
                "os.environ",
                {
                    "ALTM_ENABLE_HIGH_RISK_DEFAULTS": "true",
                    "ALTM_ENABLE_AUTO_L2_SEMANTIC_MERGE": "false",
                    "ALTM_ENABLE_AUTO_L2_TOMBSTONE": "false",
                },
            ):
                result = AltmApplication(db_path).semantic_dedup(
                    model="test-embedding",
                    threshold=0.95,
                )

            duplicate = store.get_memory_unit(second.id)
            self.assertEqual(result["candidate_count"], 1)
            self.assertEqual(result["auto_resolution_count"], 0)
            self.assertIsNotNone(duplicate)
            self.assertEqual(duplicate.status, MemoryStatus.ACTIVE)

    def test_application_semantic_dedup_dry_run_does_not_write_edges_or_tombstones(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "memory.sqlite3"
            store = SQLiteMemoryStore(db_path)
            store.initialize()
            first = _l2("decision-a", "系统采用 SQLite 作为本地记忆存储。", "decision")
            second = _l2("decision-b", "项目选择 SQLite 承载本地记忆数据。", "decision")
            for memory, vector in ((first, [1.0, 0.0]), (second, [0.99, 0.05])):
                store.put_memory_unit(memory)
                store.put_memory_embedding(memory.id, "test-embedding", memory.content_hash, vector)

            result = AltmApplication(db_path).semantic_dedup(
                model="test-embedding",
                threshold=0.95,
                mode="auto-tombstone",
                dry_run=True,
            )

            duplicate = store.get_memory_unit(second.id)
            self.assertEqual(result["candidate_count"], 1)
            self.assertEqual(result["auto_resolution_count"], 0)
            self.assertEqual(result["would_resolution_count"], 1)
            self.assertEqual(store.list_graph_edges("semantic_duplicate_candidate"), [])
            self.assertIsNotNone(duplicate)
            self.assertEqual(duplicate.status, MemoryStatus.ACTIVE)

    def test_application_semantic_dedup_preserves_rejected_edge(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "memory.sqlite3"
            store = SQLiteMemoryStore(db_path)
            store.initialize()
            first = _l2("decision-a", "系统采用 SQLite 作为本地记忆存储。", "decision")
            second = _l2("decision-b", "项目选择 SQLite 承载本地记忆数据。", "decision")
            for memory, vector in ((first, [1.0, 0.0]), (second, [0.99, 0.05])):
                store.put_memory_unit(memory)
                store.put_memory_embedding(memory.id, "test-embedding", memory.content_hash, vector)
            edge_id = store.put_memory_graph_edge(
                source_memory_id=first.id,
                target_memory_id=second.id,
                edge_type="semantic_duplicate_candidate",
                weight=0.99,
                confidence=0.99,
                metadata={"review_status": "rejected", "human_note": "不是重复"},
            )

            result = AltmApplication(db_path).semantic_dedup(
                model="test-embedding",
                threshold=0.95,
                mode="auto-tombstone",
            )
            edge = store.get_graph_edge(edge_id)
            duplicate = store.get_memory_unit(second.id)

            self.assertEqual(result["auto_resolution_count"], 0)
            self.assertEqual(result["auto_resolutions"][0]["reason"], "edge_review_rejected")
            self.assertIsNotNone(edge)
            self.assertEqual(edge["metadata"]["review_status"], "rejected")
            self.assertEqual(edge["metadata"]["human_note"], "不是重复")
            self.assertIsNotNone(duplicate)
            self.assertEqual(duplicate.status, MemoryStatus.ACTIVE)

    def test_application_semantic_dedup_prefers_stronger_canonical(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "memory.sqlite3"
            store = SQLiteMemoryStore(db_path)
            store.initialize()
            weak = _l2("decision-a", "系统采用 SQLite 作为本地记忆存储。", "decision")
            strong = _l2(
                "decision-b",
                "项目选择 SQLite 承载本地记忆数据。",
                "decision",
                score=ScoreBreakdown(resident_score=1.0, evidence_quality=1.0),
                useful_access_count=3,
            )
            for memory, vector in ((weak, [1.0, 0.0]), (strong, [0.99, 0.05])):
                store.put_memory_unit(memory)
                store.put_memory_embedding(memory.id, "test-embedding", memory.content_hash, vector)

            result = AltmApplication(db_path).semantic_dedup(
                model="test-embedding",
                threshold=0.95,
                mode="auto-tombstone",
            )
            canonical = store.get_memory_unit(strong.id)
            duplicate = store.get_memory_unit(weak.id)

            self.assertEqual(result["auto_resolution_count"], 1)
            self.assertEqual(result["auto_resolutions"][0]["canonical_memory_id"], strong.id)
            self.assertIsNotNone(canonical)
            self.assertIsNotNone(duplicate)
            self.assertEqual(canonical.metadata["merged_duplicate_ids"], [weak.id])
            self.assertEqual(duplicate.status, MemoryStatus.TOMBSTONED)
            self.assertEqual(duplicate.metadata["superseded_by"], strong.id)

    def test_application_semantic_dedup_skips_risk_token_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "memory.sqlite3"
            store = SQLiteMemoryStore(db_path)
            store.initialize()
            first = _l2("decision-a", "使用 deepseek-v4-flash 模型。", "decision")
            second = _l2("decision-b", "使用 deepseek-v3 模型。", "decision")
            for memory, vector in ((first, [1.0, 0.0]), (second, [0.99, 0.05])):
                store.put_memory_unit(memory)
                store.put_memory_embedding(memory.id, "test-embedding", memory.content_hash, vector)

            result = AltmApplication(db_path).semantic_dedup(
                model="test-embedding",
                threshold=0.95,
                mode="auto-tombstone",
            )
            duplicate = store.get_memory_unit(second.id)

            self.assertEqual(result["candidate_count"], 1)
            self.assertEqual(result["auto_resolution_count"], 0)
            self.assertEqual(result["auto_resolutions"][0]["reason"], "risk_token_mismatch")
            self.assertIsNotNone(duplicate)
            self.assertEqual(duplicate.status, MemoryStatus.ACTIVE)

    def test_restore_semantic_merge_rolls_back_tombstone_and_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "memory.sqlite3"
            store = SQLiteMemoryStore(db_path)
            store.initialize()
            first = _l2("decision-a", "系统采用 SQLite 作为本地记忆存储。", "decision")
            second = _l2("decision-b", "项目选择 SQLite 承载本地记忆数据。", "decision")
            for memory, vector in ((first, [1.0, 0.0]), (second, [0.99, 0.05])):
                store.put_memory_unit(memory)
                store.put_memory_embedding(memory.id, "test-embedding", memory.content_hash, vector)

            result = AltmApplication(db_path).semantic_dedup(
                model="test-embedding",
                threshold=0.95,
                mode="auto-tombstone",
            )
            edge_id = result["edge_ids"][0]
            rollback = AltmApplication(db_path).restore_semantic_merge(
                edge_id=edge_id,
                reason="测试回滚",
            )
            canonical = store.get_memory_unit(first.id)
            duplicate = store.get_memory_unit(second.id)
            edge = store.get_graph_edge(edge_id)
            events = store.list_review_events(target_type="graph_edge", target_id=edge_id)

            self.assertTrue(rollback["restored"])
            self.assertIsNotNone(canonical)
            self.assertIsNotNone(duplicate)
            self.assertNotIn("merged_duplicate_ids", canonical.metadata)
            self.assertEqual(duplicate.status, MemoryStatus.ACTIVE)
            self.assertNotIn("superseded_by", duplicate.metadata)
            self.assertIsNotNone(edge)
            self.assertEqual(edge["metadata"]["resolution_status"], "rolled_back")
            self.assertIn("semantic_auto_merge_rolled_back", {event.event_type for event in events})


def _l2(
    memory_id: str,
    summary: str,
    atom_type: str,
    score: ScoreBreakdown | None = None,
    useful_access_count: int = 0,
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
        useful_access_count=useful_access_count,
        score=score or ScoreBreakdown(),
        metadata={"atom_type": atom_type, "review_status": "approved"},
    )


if __name__ == "__main__":
    unittest.main()
