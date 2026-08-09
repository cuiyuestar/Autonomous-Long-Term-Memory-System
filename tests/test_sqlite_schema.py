import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from altm.contracts import (  # noqa: E402
    EvidenceRef,
    EvidenceRelation,
    LifecycleState,
    MemoryLayer,
    MemoryStatus,
    MemoryUnit,
)
from altm.storage import SQLiteMemoryStore  # noqa: E402
from altm.utils import sha256_text, utc_now_iso  # noqa: E402


class SQLiteSchemaTest(unittest.TestCase):
    def test_initialize_creates_core_tables_and_fts(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "memory.sqlite3"
            store = SQLiteMemoryStore(db_path)
            store.initialize()

            with sqlite3.connect(str(db_path)) as connection:
                tables = {
                    row[0]
                    for row in connection.execute(
                        "SELECT name FROM sqlite_master WHERE type IN ('table', 'virtual table')"
                    )
                }

            self.assertIn("memory_units", tables)
            self.assertIn("memory_units_fts", tables)
            self.assertIn("memory_units_fts_trigram", tables)
            self.assertIn("l1_context_capsules", tables)
            self.assertIn("memory_embeddings", tables)
            self.assertIn("evidence_refs", tables)
            self.assertIn("graph_nodes", tables)
            self.assertIn("graph_edges", tables)
            self.assertIn("lifecycle_events", tables)
            self.assertIn("review_events", tables)
            self.assertIn("review_audit_projections", tables)
            self.assertIn("checkpoints", tables)
            self.assertIn("runtime_cycles", tables)
            self.assertIn("runtime_jobs", tables)
            self.assertIn("l3_scenes", tables)
            self.assertIn("l4_persona_facets", tables)
            self.assertIn("vector_index_registry", tables)
            self.assertIn("lifecycle_age_stats", tables)
            self.assertIn("deletion_requests", tables)
            self.assertIn("ccr_entries", tables)
            self.assertIn("tombstones", tables)
            self.assertIn("l2_preferences", tables)
            self.assertIn("l2_constraints", tables)
            self.assertIn("l2_project_facts", tables)
            self.assertIn("l2_decisions", tables)
            self.assertIn("l2_issues", tables)
            self.assertIn("l2_resolutions", tables)
            self.assertIn("l2_task_states", tables)
            self.assertIn("l2_temporal_facts", tables)
            self.assertIn("l2_lessons", tables)

            with sqlite3.connect(str(db_path)) as connection:
                memory_unit_columns = {
                    row[1] for row in connection.execute("PRAGMA table_info(memory_units)")
                }

            self.assertIn("promotion_candidate_since", memory_unit_columns)
            self.assertIn("demotion_candidate_since", memory_unit_columns)
            self.assertTrue(
                {
                    "tenant_id",
                    "workspace_id",
                    "user_id",
                    "agent_id",
                    "visibility",
                }
                <= memory_unit_columns
            )

    def test_initialize_migrates_legacy_memory_scope_columns(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "legacy.sqlite3"
            with sqlite3.connect(db_path) as connection:
                connection.execute(
                    """
                    CREATE TABLE memory_units (
                      id TEXT PRIMARY KEY,
                      layer TEXT NOT NULL,
                      lifecycle_state TEXT NOT NULL,
                      status TEXT NOT NULL,
                      content TEXT NOT NULL,
                      content_hash TEXT NOT NULL,
                      summary TEXT,
                      created_at TEXT NOT NULL,
                      updated_at TEXT NOT NULL,
                      last_accessed_at TEXT,
                      access_count INTEGER NOT NULL DEFAULT 0,
                      useful_access_count INTEGER NOT NULL DEFAULT 0,
                      resident_score REAL NOT NULL DEFAULT 0,
                      structural_score REAL NOT NULL DEFAULT 0,
                      recency_score REAL NOT NULL DEFAULT 0,
                      access_score REAL NOT NULL DEFAULT 0,
                      evidence_quality_score REAL NOT NULL DEFAULT 0,
                      lifecycle_age INTEGER NOT NULL DEFAULT 0,
                      protection_tier INTEGER NOT NULL DEFAULT 1,
                      compression_tier INTEGER NOT NULL DEFAULT 0,
                      observation_until TEXT,
                      metadata_json TEXT NOT NULL DEFAULT '{}'
                    )
                    """
                )
                connection.commit()

            SQLiteMemoryStore(db_path).initialize()

            with sqlite3.connect(db_path) as connection:
                columns = {
                    row[1] for row in connection.execute("PRAGMA table_info(memory_units)")
                }
                tables = {
                    row[0]
                    for row in connection.execute(
                        "SELECT name FROM sqlite_master WHERE type = 'table'"
                    )
                }
            self.assertTrue(
                {
                    "tenant_id",
                    "workspace_id",
                    "user_id",
                    "agent_id",
                    "visibility",
                    "promotion_candidate_since",
                    "demotion_candidate_since",
                }
                <= columns
            )
            self.assertTrue({"runtime_cycles", "runtime_jobs"} <= tables)

    def test_put_memory_unit_empty_evidence_refs_clears_existing_refs(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store = SQLiteMemoryStore(Path(tmpdir) / "memory.sqlite3")
            store.initialize()
            memory = _memory(
                "memory-with-evidence",
                evidence_refs=[
                    EvidenceRef(
                        target_id="source-l0",
                        target_layer=MemoryLayer.L0,
                        relation=EvidenceRelation.SOURCE,
                        confidence=1.0,
                    )
                ],
            )
            store.put_memory_unit(memory)

            stored = store.get_memory_unit(memory.id)
            self.assertIsNotNone(stored)
            self.assertEqual(len(stored.evidence_refs), 1)

            store.put_memory_unit(memory.model_copy(update={"evidence_refs": []}))

            stored_after_clear = store.get_memory_unit(memory.id)
            self.assertIsNotNone(stored_after_clear)
            self.assertEqual(stored_after_clear.evidence_refs, [])


def _memory(memory_id: str, evidence_refs: list[EvidenceRef]) -> MemoryUnit:
    now = utc_now_iso()
    content = "schema test memory %s" % memory_id
    return MemoryUnit(
        id=memory_id,
        layer=MemoryLayer.L1,
        lifecycle_state=LifecycleState.SHORT,
        status=MemoryStatus.ACTIVE,
        content=content,
        content_hash=sha256_text(content),
        summary=content,
        created_at=now,
        updated_at=now,
        evidence_refs=evidence_refs,
    )


if __name__ == "__main__":
    unittest.main()
