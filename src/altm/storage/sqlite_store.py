"""SQLite storage scaffold.

Phase 1 only guarantees schema initialization. CRUD, FTS writes, lifecycle
events, and tombstone behavior will be implemented behind the confirmed
MemoryStore port.
"""

from __future__ import annotations

import json
import math
import sqlite3
from pathlib import Path
from typing import Iterable, List, Optional, Sequence, Union

from altm.config import high_risk_flags
from altm.contracts import (
    AccessSignal,
    ContextCapsule,
    EvidenceRef,
    EvidenceRelation,
    FallbackLocator,
    L2Atom,
    L2AtomType,
    LifecycleMeta,
    LifecycleState,
    MemoryLayer,
    MemoryStatus,
    MemoryUnit,
    ReviewEvent,
    ScoreBreakdown,
)
from altm.utils import random_id, stable_id, utc_now_iso


DEFAULT_SCHEMA_PATH = (
    Path(__file__).resolve().parents[3] / "schemas" / "sqlite" / "001_initial.sql"
)

L2_TABLES = {
    L2AtomType.PREFERENCE: "l2_preferences",
    L2AtomType.CONSTRAINT: "l2_constraints",
    L2AtomType.PROJECT_FACT: "l2_project_facts",
    L2AtomType.DECISION: "l2_decisions",
    L2AtomType.ISSUE: "l2_issues",
    L2AtomType.RESOLUTION: "l2_resolutions",
    L2AtomType.TASK_STATE: "l2_task_states",
    L2AtomType.TEMPORAL_FACT: "l2_temporal_facts",
    L2AtomType.LESSON: "l2_lessons",
}

_GRAPH_EDGE_PROTECTED_METADATA_KEYS = {
    "review_status",
    "resolution_status",
    "canonical_memory_id",
    "duplicate_memory_id",
    "resolved_at",
    "rollback_at",
    "destructive_action_allowed",
}


def _row_to_graph_edge(row: sqlite3.Row) -> dict[str, object]:
    return {
        "id": row["id"],
        "source_memory_id": row["source_memory_id"],
        "target_memory_id": row["target_memory_id"],
        "edge_type": row["edge_type"],
        "weight": row["weight"],
        "confidence": row["confidence"],
        "metadata": json.loads(row["metadata_json"] or "{}"),
        "created_at": row["created_at"],
    }


def _merged_graph_edge_metadata(
    existing: dict[str, object],
    incoming: dict[str, object],
) -> dict[str, object]:
    merged = dict(existing)
    merged.update(incoming)
    for key in _GRAPH_EDGE_PROTECTED_METADATA_KEYS:
        if key in existing:
            merged[key] = existing[key]
    if "candidate_first_seen_at" in existing:
        merged["candidate_first_seen_at"] = existing["candidate_first_seen_at"]
    elif "candidate_last_seen_at" in incoming:
        merged["candidate_first_seen_at"] = incoming["candidate_last_seen_at"]
    return merged


def _append_unique_metadata_list(value: object, item: str) -> list[str]:
    values = [str(existing) for existing in value] if isinstance(value, list) else []
    if item not in values:
        values.append(item)
    return values


def _remove_metadata_list_item(value: object, item: str) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(existing) for existing in value if str(existing) != item]


def _memory_status_from_value(value: object, default: MemoryStatus) -> MemoryStatus:
    if isinstance(value, MemoryStatus):
        return value
    if isinstance(value, str):
        try:
            return MemoryStatus(value)
        except ValueError:
            return default
    return default


def _vector_norm(vector: Sequence[float]) -> float:
    return math.sqrt(sum(float(value) * float(value) for value in vector))


def _cosine(query_vector: Sequence[float], query_norm: float, vector: Sequence[float]) -> float:
    norm = _vector_norm(vector)
    if query_norm == 0 or norm == 0:
        return 0.0
    dot = sum(float(left) * float(right) for left, right in zip(query_vector, vector))
    return dot / (query_norm * norm)


class SQLiteMemoryStore:
    def __init__(
        self,
        db_path: Union[str, Path],
        schema_path: Optional[Union[str, Path]] = None,
    ) -> None:
        self.db_path = Path(db_path)
        self.schema_path = Path(schema_path) if schema_path is not None else DEFAULT_SCHEMA_PATH

    def initialize(self) -> None:
        if not self.schema_path.exists():
            raise FileNotFoundError("SQLite schema not found: %s" % self.schema_path)

        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        schema = self.schema_path.read_text(encoding="utf-8")

        with sqlite3.connect(str(self.db_path)) as connection:
            connection.executescript(schema)
            self._ensure_schema_compatibility(connection)
            connection.commit()

    def connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(str(self.db_path))
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    def _ensure_schema_compatibility(self, connection: sqlite3.Connection) -> None:
        columns = {
            row[1]
            for row in connection.execute("PRAGMA table_info(memory_units)").fetchall()
        }
        if "promotion_candidate_since" not in columns:
            connection.execute("ALTER TABLE memory_units ADD COLUMN promotion_candidate_since TEXT")
        if "demotion_candidate_since" not in columns:
            connection.execute("ALTER TABLE memory_units ADD COLUMN demotion_candidate_since TEXT")
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS review_audit_projections (
              target_type TEXT NOT NULL,
              target_id TEXT NOT NULL,
              event_count INTEGER NOT NULL DEFAULT 0,
              review_mark_count INTEGER NOT NULL DEFAULT 0,
              review_apply_count INTEGER NOT NULL DEFAULT 0,
              last_event_id TEXT,
              last_event_type TEXT,
              last_status TEXT,
              last_review_item_id TEXT,
              last_plan_id TEXT,
              first_event_at TEXT NOT NULL,
              last_event_at TEXT NOT NULL,
              metadata_json TEXT NOT NULL DEFAULT '{}',
              PRIMARY KEY (target_type, target_id)
            )
            """
        )
        connection.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_review_audit_projections_status
              ON review_audit_projections(last_status)
            """
        )
        connection.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_review_audit_projections_last_event_at
              ON review_audit_projections(last_event_at)
            """
        )

    def put_memory_unit(self, memory: MemoryUnit) -> None:
        with self.connect() as connection:
            self._put_memory_unit(connection, memory)
            connection.commit()

    def _put_memory_unit(self, connection: sqlite3.Connection, memory: MemoryUnit) -> None:
        connection.execute(
            """
            INSERT INTO memory_units (
              id, layer, lifecycle_state, status, content, content_hash, summary,
              created_at, updated_at, last_accessed_at, access_count,
              useful_access_count, resident_score, structural_score, recency_score,
              access_score, evidence_quality_score, lifecycle_age, protection_tier,
              compression_tier, observation_until, promotion_candidate_since,
              demotion_candidate_since, metadata_json
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
              layer = excluded.layer,
              lifecycle_state = excluded.lifecycle_state,
              status = excluded.status,
              content = excluded.content,
              content_hash = excluded.content_hash,
              summary = excluded.summary,
              updated_at = excluded.updated_at,
              last_accessed_at = excluded.last_accessed_at,
              access_count = excluded.access_count,
              useful_access_count = excluded.useful_access_count,
              resident_score = excluded.resident_score,
              structural_score = excluded.structural_score,
              recency_score = excluded.recency_score,
              access_score = excluded.access_score,
              evidence_quality_score = excluded.evidence_quality_score,
              lifecycle_age = excluded.lifecycle_age,
              protection_tier = excluded.protection_tier,
              compression_tier = excluded.compression_tier,
              observation_until = excluded.observation_until,
              promotion_candidate_since = excluded.promotion_candidate_since,
              demotion_candidate_since = excluded.demotion_candidate_since,
              metadata_json = excluded.metadata_json
            """,
            (
                memory.id,
                memory.layer.value,
                memory.lifecycle_state.value,
                memory.status.value,
                memory.content,
                memory.content_hash,
                memory.summary,
                memory.created_at,
                memory.updated_at,
                memory.last_accessed_at,
                memory.access_count,
                memory.useful_access_count,
                memory.score.resident_score,
                memory.score.structural,
                memory.score.recency,
                memory.score.access,
                memory.score.evidence_quality,
                memory.lifecycle.age,
                memory.lifecycle.protection_tier,
                memory.lifecycle.compression_tier,
                memory.lifecycle.observation_until,
                memory.lifecycle.promotion_candidate_since,
                memory.lifecycle.demotion_candidate_since,
                json.dumps(memory.metadata, ensure_ascii=False, sort_keys=True),
            ),
        )

        connection.execute("DELETE FROM memory_units_fts WHERE memory_id = ?", (memory.id,))
        connection.execute(
            "DELETE FROM memory_units_fts_trigram WHERE memory_id = ?", (memory.id,)
        )
        connection.execute(
            """
            INSERT INTO memory_units_fts(memory_id, layer, content, summary)
            VALUES (?, ?, ?, ?)
            """,
            (memory.id, memory.layer.value, memory.content, memory.summary),
        )
        connection.execute(
            """
            INSERT INTO memory_units_fts_trigram(memory_id, layer, content, summary)
            VALUES (?, ?, ?, ?)
            """,
            (memory.id, memory.layer.value, memory.content, memory.summary),
        )
        self._replace_evidence_refs(connection, memory.id, memory.evidence_refs)

    def get_memory_unit(self, memory_id: str) -> Optional[MemoryUnit]:
        with self.connect() as connection:
            return self._get_memory_unit(connection, memory_id)

    def _get_memory_unit(
        self,
        connection: sqlite3.Connection,
        memory_id: str,
    ) -> Optional[MemoryUnit]:
        row = connection.execute(
            "SELECT * FROM memory_units WHERE id = ? AND status != 'deleted'",
            (memory_id,),
        ).fetchone()
        if row is None:
            return None
        evidence_refs = self._load_evidence_refs(connection, memory_id)
        return self._row_to_memory_unit(row, evidence_refs)

    def list_memory_units(
        self,
        layer: Optional[MemoryLayer] = None,
        limit: int = 100,
    ) -> Sequence[MemoryUnit]:
        sql = "SELECT * FROM memory_units WHERE status != 'deleted'"
        params: List[str] = []
        if layer is not None:
            sql += " AND layer = ?"
            params.append(layer.value)
        sql += " ORDER BY created_at ASC LIMIT ?"

        with self.connect() as connection:
            rows = connection.execute(sql, (*params, limit)).fetchall()
            return [
                self._row_to_memory_unit(row, self._load_evidence_refs(connection, row["id"]))
                for row in rows
            ]

    def list_l0_by_session(self, session_id: str, limit: int = 200) -> Sequence[MemoryUnit]:
        with self.connect() as connection:
            try:
                rows = connection.execute(
                    """
                    SELECT * FROM memory_units
                    WHERE layer = 'L0'
                      AND status != 'deleted'
                      AND json_extract(metadata_json, '$.session_id') = ?
                    ORDER BY created_at ASC
                    LIMIT ?
                    """,
                    (session_id, limit),
                ).fetchall()
            except sqlite3.OperationalError:
                rows = connection.execute(
                    """
                    SELECT * FROM memory_units
                    WHERE layer = 'L0' AND status != 'deleted'
                    ORDER BY created_at ASC
                    LIMIT ?
                    """,
                    (limit,),
                ).fetchall()

            units = [
                self._row_to_memory_unit(row, self._load_evidence_refs(connection, row["id"]))
                for row in rows
            ]
            return [unit for unit in units if unit.metadata.get("session_id") == session_id]

    def put_l1_context_capsule(self, capsule: ContextCapsule, memory_unit_id: str) -> None:
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO l1_context_capsules (
                  id, memory_unit_id, title, session_id, time_range_start, time_range_end,
                  source_message_ids_json, task_goal, local_context, key_turns_json,
                  decisions_mentioned_json, unresolved_questions_json, topic_tags_json,
                  confidence, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                  memory_unit_id = excluded.memory_unit_id,
                  title = excluded.title,
                  session_id = excluded.session_id,
                  time_range_start = excluded.time_range_start,
                  time_range_end = excluded.time_range_end,
                  source_message_ids_json = excluded.source_message_ids_json,
                  task_goal = excluded.task_goal,
                  local_context = excluded.local_context,
                  key_turns_json = excluded.key_turns_json,
                  decisions_mentioned_json = excluded.decisions_mentioned_json,
                  unresolved_questions_json = excluded.unresolved_questions_json,
                  topic_tags_json = excluded.topic_tags_json,
                  confidence = excluded.confidence
                """,
                (
                    capsule.id,
                    memory_unit_id,
                    capsule.title,
                    capsule.session_id,
                    capsule.time_range[0],
                    capsule.time_range[1],
                    json.dumps(capsule.source_message_ids, ensure_ascii=False),
                    capsule.task_goal,
                    capsule.local_context,
                    json.dumps(capsule.key_turns, ensure_ascii=False),
                    json.dumps(capsule.decisions_mentioned, ensure_ascii=False),
                    json.dumps(capsule.unresolved_questions, ensure_ascii=False),
                    json.dumps(capsule.topic_tags, ensure_ascii=False),
                    capsule.confidence,
                    utc_now_iso(),
                ),
            )
            connection.commit()

    def count_l1_context_capsules(self) -> int:
        with self.connect() as connection:
            row = connection.execute("SELECT COUNT(*) FROM l1_context_capsules").fetchone()
            return int(row[0])

    def put_memory_embedding(
        self,
        memory_id: str,
        embedding_model: str,
        content_hash: str,
        vector: Sequence[float],
    ) -> None:
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO memory_embeddings(
                  memory_unit_id, embedding_model, content_hash, dimension, vector_json, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(memory_unit_id, embedding_model) DO UPDATE SET
                  content_hash = excluded.content_hash,
                  dimension = excluded.dimension,
                  vector_json = excluded.vector_json,
                  created_at = excluded.created_at
                """,
                (
                    memory_id,
                    embedding_model,
                    content_hash,
                    len(vector),
                    json.dumps([float(value) for value in vector]),
                    utc_now_iso(),
                ),
            )
            connection.commit()

    def get_memory_embedding(
        self,
        memory_id: str,
        embedding_model: str,
    ) -> Optional[tuple[str, list[float]]]:
        with self.connect() as connection:
            row = connection.execute(
                """
                SELECT content_hash, vector_json FROM memory_embeddings
                WHERE memory_unit_id = ? AND embedding_model = ?
                """,
                (memory_id, embedding_model),
            ).fetchone()
            if row is None:
                return None
            return row["content_hash"], [float(value) for value in json.loads(row["vector_json"])]

    def list_embedding_targets(
        self,
        embedding_model: str,
        limit: int = 100,
    ) -> Sequence[MemoryUnit]:
        targets: list[MemoryUnit] = []
        for memory in self.list_memory_units(limit=1000):
            existing = self.get_memory_embedding(memory.id, embedding_model)
            if existing is not None and existing[0] == memory.content_hash:
                continue
            targets.append(memory)
            if len(targets) >= limit:
                break
        return targets

    def search_embeddings(
        self,
        query_vector: Sequence[float],
        embedding_model: str,
        limit: int = 10,
        layers: Optional[Sequence[MemoryLayer]] = None,
        session_id: Optional[str] = None,
        statuses: Optional[Sequence[MemoryStatus]] = None,
    ) -> Sequence[tuple[MemoryUnit, float]]:
        query_norm = _vector_norm(query_vector)
        if query_norm == 0:
            return []

        scored: list[tuple[MemoryUnit, float]] = []
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT memory_unit_id, vector_json FROM memory_embeddings
                WHERE embedding_model = ?
                """,
                (embedding_model,),
            ).fetchall()
            for row in rows:
                memory = self.get_memory_unit(row["memory_unit_id"])
                if memory is None:
                    continue
                if layers and memory.layer not in layers:
                    continue
                if statuses:
                    if memory.status not in statuses:
                        continue
                elif memory.status in {MemoryStatus.DELETED, MemoryStatus.TOMBSTONED}:
                    continue
                if session_id is not None and memory.metadata.get("session_id") != session_id:
                    continue
                vector = [float(value) for value in json.loads(row["vector_json"])]
                score = _cosine(query_vector, query_norm, vector)
                if score > 0:
                    scored.append((memory, score))
        scored.sort(key=lambda item: item[1], reverse=True)
        return scored[:limit]

    def search_fts(
        self,
        query: str,
        limit: int = 10,
        layers: Optional[Sequence[MemoryLayer]] = None,
        session_id: Optional[str] = None,
        statuses: Optional[Sequence[MemoryStatus]] = None,
    ) -> Sequence[MemoryUnit]:
        with self.connect() as connection:
            rows = self._search_fts_rows(connection, query, limit, layers, session_id, statuses)
            units: List[MemoryUnit] = []
            for row in rows:
                memory = self.get_memory_unit(row["memory_id"])
                if memory is not None:
                    units.append(memory)
            return units

    def search_fts_trigram(
        self,
        query: str,
        limit: int = 10,
        layers: Optional[Sequence[MemoryLayer]] = None,
        session_id: Optional[str] = None,
        statuses: Optional[Sequence[MemoryStatus]] = None,
    ) -> Sequence[MemoryUnit]:
        with self.connect() as connection:
            rows = self._search_fts_rows(
                connection,
                query,
                limit,
                layers,
                session_id,
                statuses,
                table="memory_units_fts_trigram",
            )
            units: List[MemoryUnit] = []
            for row in rows:
                memory = self.get_memory_unit(row["memory_id"])
                if memory is not None:
                    units.append(memory)
            return units

    def search_like(
        self,
        query: str,
        limit: int = 10,
        layers: Optional[Sequence[MemoryLayer]] = None,
        session_id: Optional[str] = None,
        statuses: Optional[Sequence[MemoryStatus]] = None,
    ) -> Sequence[MemoryUnit]:
        clauses = ["(content LIKE ? OR summary LIKE ?)"]
        params: list[str] = ["%%%s%%" % query, "%%%s%%" % query]
        if layers:
            placeholders = ", ".join("?" for _ in layers)
            clauses.append("layer IN (%s)" % placeholders)
            params.extend(layer.value for layer in layers)
        if statuses:
            placeholders = ", ".join("?" for _ in statuses)
            clauses.append("status IN (%s)" % placeholders)
            params.extend(status.value for status in statuses)
        else:
            clauses.append("status NOT IN ('deleted', 'tombstoned')")
        if session_id is not None:
            clauses.append("json_extract(metadata_json, '$.session_id') = ?")
            params.append(session_id)
        params.append(str(limit))
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM memory_units
                WHERE %s
                ORDER BY updated_at DESC
                LIMIT ?
                """
                % " AND ".join(clauses),
                params,
            ).fetchall()
            return [
                self._row_to_memory_unit(row, self._load_evidence_refs(connection, row["id"]))
                for row in rows
            ]

    def put_l2_atom(self, atom: L2Atom, memory: MemoryUnit) -> None:
        if memory.layer != MemoryLayer.L2:
            raise ValueError("L2 atom memory must use layer L2")
        if memory.id != atom.id:
            raise ValueError("L2 atom id and MemoryUnit id must match")

        metadata = dict(memory.metadata)
        metadata_atom_type = metadata.get("atom_type")
        if metadata_atom_type is not None and metadata_atom_type != atom.atom_type.value:
            raise ValueError("L2 MemoryUnit atom_type metadata conflicts with atom")
        metadata_review_status = metadata.get("review_status")
        if (
            metadata_review_status is not None
            and metadata_review_status != atom.review_status.value
        ):
            raise ValueError("L2 MemoryUnit review_status metadata conflicts with atom")
        metadata["atom_type"] = atom.atom_type.value
        metadata["review_status"] = atom.review_status.value
        memory_to_store = memory.model_copy(update={"metadata": metadata})

        table = L2_TABLES[atom.atom_type]
        with self.connect() as connection:
            self._put_memory_unit(connection, memory_to_store)
            connection.execute(
                """
                INSERT INTO %s (
                  id, memory_unit_id, text, subject, predicate, object, scope,
                  confidence, extraction_reason, review_status, metadata_json, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                  memory_unit_id = excluded.memory_unit_id,
                  text = excluded.text,
                  subject = excluded.subject,
                  predicate = excluded.predicate,
                  object = excluded.object,
                  scope = excluded.scope,
                  confidence = excluded.confidence,
                  extraction_reason = excluded.extraction_reason,
                  review_status = excluded.review_status,
                  metadata_json = excluded.metadata_json
                """
                % table,
                (
                    atom.id,
                    memory_to_store.id,
                    atom.text,
                    atom.subject,
                    atom.predicate,
                    atom.object,
                    atom.scope,
                    atom.confidence,
                    atom.extraction_reason,
                    atom.review_status.value,
                    json.dumps(atom.metadata, ensure_ascii=False, sort_keys=True),
                    memory_to_store.created_at,
                ),
            )
            connection.commit()

    def find_l2_duplicate(self, atom: L2Atom) -> Optional[MemoryUnit]:
        table = L2_TABLES[atom.atom_type]
        with self.connect() as connection:
            row = connection.execute(
                "SELECT memory_unit_id FROM %s WHERE text = ? LIMIT 1" % table,
                (atom.text,),
            ).fetchone()
            if row is None:
                return None
            return self.get_memory_unit(row["memory_unit_id"])

    def count_l2_atoms(self, atom_type: L2AtomType) -> int:
        table = L2_TABLES[atom_type]
        with self.connect() as connection:
            row = connection.execute("SELECT COUNT(*) FROM %s" % table).fetchone()
            return int(row[0])

    def update_memory_metadata(
        self,
        memory_id: str,
        updates: dict[str, object],
    ) -> Optional[MemoryUnit]:
        memory = self.get_memory_unit(memory_id)
        if memory is None:
            return None
        metadata = dict(memory.metadata)
        metadata.update(updates)
        updated = memory.model_copy(update={"metadata": metadata, "updated_at": utc_now_iso()})
        self.put_memory_unit(updated)
        return updated

    def update_l2_review_status(
        self,
        memory_id: str,
        review_status: str,
        note: Optional[str] = None,
    ) -> Optional[MemoryUnit]:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM memory_units WHERE id = ? AND status != 'deleted'",
                (memory_id,),
            ).fetchone()
            if row is None:
                return None
            memory = self._row_to_memory_unit(row, self._load_evidence_refs(connection, memory_id))
            if memory.layer != MemoryLayer.L2:
                raise ValueError("L2 review status can only be applied to L2 MemoryUnit")

            atom_type = memory.metadata.get("atom_type")
            if not isinstance(atom_type, str):
                raise ValueError("L2 review status requires atom_type metadata")
            try:
                table = L2_TABLES[L2AtomType(atom_type)]
            except ValueError as exc:
                raise ValueError("Unsupported L2 atom_type metadata: %s" % atom_type) from exc

            typed_row = connection.execute(
                "SELECT id FROM %s WHERE memory_unit_id = ? LIMIT 1" % table,
                (memory_id,),
            ).fetchone()
            if typed_row is None:
                raise RuntimeError(
                    "Missing L2 typed row for MemoryUnit %s in %s" % (memory_id, table)
                )

            now = utc_now_iso()
            metadata = dict(memory.metadata)
            metadata.update(
                {
                    "review_status": review_status,
                    "reviewed_at": now,
                }
            )
            if note is not None:
                metadata["review_note"] = note
            updated = memory.model_copy(update={"metadata": metadata, "updated_at": now})
            self._put_memory_unit(connection, updated)
            connection.execute(
                "UPDATE %s SET review_status = ? WHERE memory_unit_id = ?" % table,
                (review_status, memory_id),
            )
            connection.commit()
            return updated

    def put_memory_graph_edge(
        self,
        source_memory_id: str,
        target_memory_id: str,
        edge_type: str,
        weight: float,
        confidence: float,
        metadata: Optional[dict[str, object]] = None,
    ) -> str:
        with self.connect() as connection:
            source_node_id = self._ensure_memory_graph_node(connection, source_memory_id)
            target_node_id = self._ensure_memory_graph_node(connection, target_memory_id)
            edge_id = stable_id("graph_edge", source_memory_id, target_memory_id, edge_type)
            existing_row = connection.execute(
                "SELECT metadata_json FROM graph_edges WHERE id = ?",
                (edge_id,),
            ).fetchone()
            metadata_json = _merged_graph_edge_metadata(
                json.loads(existing_row["metadata_json"] or "{}")
                if existing_row is not None
                else {},
                metadata or {},
            )
            connection.execute(
                """
                INSERT INTO graph_edges(
                  id, source_node_id, target_node_id, edge_type, weight,
                  confidence, metadata_json, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                  weight = excluded.weight,
                  confidence = excluded.confidence,
                  metadata_json = excluded.metadata_json
                """,
                (
                    edge_id,
                    source_node_id,
                    target_node_id,
                    edge_type,
                    weight,
                    confidence,
                    json.dumps(metadata_json, ensure_ascii=False, sort_keys=True),
                    utc_now_iso(),
                ),
            )
            connection.commit()
            return edge_id

    def get_graph_edge(self, edge_id: str) -> Optional[dict[str, object]]:
        with self.connect() as connection:
            row = connection.execute(
                """
                SELECT
                  graph_edges.*,
                  source_node.memory_unit_id AS source_memory_id,
                  target_node.memory_unit_id AS target_memory_id
                FROM graph_edges
                JOIN graph_nodes AS source_node ON source_node.id = graph_edges.source_node_id
                JOIN graph_nodes AS target_node ON target_node.id = graph_edges.target_node_id
                WHERE graph_edges.id = ?
                """,
                (edge_id,),
            ).fetchone()
            return _row_to_graph_edge(row) if row is not None else None

    def list_graph_edges(self, edge_type: Optional[str] = None) -> Sequence[dict[str, object]]:
        clauses: list[str] = []
        params: list[str] = []
        if edge_type is not None:
            clauses.append("graph_edges.edge_type = ?")
            params.append(edge_type)
        where = "WHERE %s" % " AND ".join(clauses) if clauses else ""
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT
                  graph_edges.*,
                  source_node.memory_unit_id AS source_memory_id,
                  target_node.memory_unit_id AS target_memory_id
                FROM graph_edges
                JOIN graph_nodes AS source_node ON source_node.id = graph_edges.source_node_id
                JOIN graph_nodes AS target_node ON target_node.id = graph_edges.target_node_id
                %s
                ORDER BY graph_edges.created_at ASC
                """
                % where,
                params,
            ).fetchall()
            return [_row_to_graph_edge(row) for row in rows]

    def update_graph_edge_metadata(
        self,
        edge_id: str,
        updates: dict[str, object],
    ) -> Optional[dict[str, object]]:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT metadata_json FROM graph_edges WHERE id = ?",
                (edge_id,),
            ).fetchone()
            if row is None:
                return None
            metadata = json.loads(row["metadata_json"] or "{}")
            metadata.update(updates)
            connection.execute(
                "UPDATE graph_edges SET metadata_json = ? WHERE id = ?",
                (json.dumps(metadata, ensure_ascii=False, sort_keys=True), edge_id),
            )
            connection.commit()
        edges = [edge for edge in self.list_graph_edges() if edge["id"] == edge_id]
        return edges[0] if edges else None

    def apply_semantic_l2_merge(
        self,
        edge_id: str,
        canonical_memory_id: str,
        duplicate_memory_id: str,
        similarity: float,
        threshold: float,
        auto_merge_threshold: float,
        auto_tombstone_threshold: float,
        embedding_model: str,
        auto_tombstone: bool,
        reason: str,
    ) -> dict[str, object]:
        flags = high_risk_flags()
        with self.connect() as connection:
            edge_row = connection.execute(
                "SELECT metadata_json FROM graph_edges WHERE id = ?",
                (edge_id,),
            ).fetchone()
            if edge_row is None:
                return {"applied": False, "tombstoned": False, "reason": "missing_edge"}

            canonical = self._get_memory_unit(connection, canonical_memory_id)
            duplicate = self._get_memory_unit(connection, duplicate_memory_id)
            if canonical is None or duplicate is None:
                return {"applied": False, "tombstoned": False, "reason": "missing_memory"}
            if canonical.status in {MemoryStatus.DELETED, MemoryStatus.TOMBSTONED}:
                return {"applied": False, "tombstoned": False, "reason": "canonical_terminal"}
            if duplicate.status in {MemoryStatus.DELETED, MemoryStatus.TOMBSTONED}:
                return {"applied": False, "tombstoned": False, "reason": "duplicate_terminal"}

            now = utc_now_iso()
            canonical_previous_metadata = dict(canonical.metadata)
            duplicate_previous_metadata = dict(duplicate.metadata)
            canonical_metadata = dict(canonical.metadata)
            duplicate_metadata = dict(duplicate.metadata)
            merged_duplicate_ids = _append_unique_metadata_list(
                canonical_metadata.get("merged_duplicate_ids"),
                duplicate.id,
            )
            merge_edge_ids = _append_unique_metadata_list(
                canonical_metadata.get("semantic_merge_edge_ids"),
                edge_id,
            )
            canonical_metadata.update(
                {
                    "merged_duplicate_ids": merged_duplicate_ids,
                    "semantic_merge_edge_ids": merge_edge_ids,
                    "semantic_merge_count": len(merged_duplicate_ids),
                    "semantic_merge_updated_at": now,
                }
            )
            duplicate_metadata.update(
                {
                    "superseded_by": canonical.id,
                    "semantic_merge_edge_id": edge_id,
                    "semantic_merge_status": "auto_merged",
                    "semantic_merge_updated_at": now,
                }
            )

            updated_canonical = canonical.model_copy(
                update={"metadata": canonical_metadata, "updated_at": now}
            )
            updated_duplicate = duplicate.model_copy(
                update={
                    "metadata": duplicate_metadata,
                    "status": MemoryStatus.TOMBSTONED if auto_tombstone else duplicate.status,
                    "updated_at": now,
                }
            )
            self._put_memory_unit(connection, updated_canonical)
            self._put_memory_unit(connection, updated_duplicate)
            if auto_tombstone:
                connection.execute(
                    """
                    INSERT INTO tombstones(id, target_type, target_id, reason, created_at)
                    VALUES (?, ?, ?, ?, ?)
                    ON CONFLICT(target_type, target_id) DO UPDATE SET
                      reason = excluded.reason,
                      created_at = excluded.created_at
                    """,
                    (
                        random_id("tombstone"),
                        "memory_unit",
                        duplicate.id,
                        "%s:%s" % (reason, canonical.id),
                        now,
                    ),
                )

            edge_metadata = json.loads(edge_row["metadata_json"] or "{}")
            edge_metadata.update(
                {
                    "review_status": "approved",
                    "resolution_status": "auto_merged",
                    "canonical_memory_id": canonical.id,
                    "duplicate_memory_id": duplicate.id,
                    "auto_merge": True,
                    "auto_tombstone": auto_tombstone,
                    "resolved_at": now,
                    "semantic_merge_reason": reason,
                    "similarity": similarity,
                    "threshold": threshold,
                    "auto_merge_threshold": auto_merge_threshold,
                    "auto_tombstone_threshold": auto_tombstone_threshold,
                    "embedding_model": embedding_model,
                    "destructive_action_allowed": auto_tombstone,
                    "rollback_available": True,
                    "canonical_previous_status": canonical.status.value,
                    "duplicate_previous_status": duplicate.status.value,
                    "canonical_previous_metadata": canonical_previous_metadata,
                    "duplicate_previous_metadata": duplicate_previous_metadata,
                }
            )
            connection.execute(
                "UPDATE graph_edges SET metadata_json = ? WHERE id = ?",
                (json.dumps(edge_metadata, ensure_ascii=False, sort_keys=True), edge_id),
            )

            event_metadata = {
                "edge_id": edge_id,
                "canonical_memory_id": canonical.id,
                "duplicate_memory_id": duplicate.id,
                "similarity": similarity,
                "threshold": threshold,
                "auto_merge_threshold": auto_merge_threshold,
                "auto_tombstone_threshold": auto_tombstone_threshold,
                "embedding_model": embedding_model,
                "auto_tombstone": auto_tombstone,
                "canonical_previous_status": canonical.status.value,
                "duplicate_previous_status": duplicate.status.value,
                "canonical_previous_metadata": canonical_previous_metadata,
                "duplicate_previous_metadata": duplicate_previous_metadata,
            }
            if flags.enable_review_event_sourcing:
                self._append_review_event(
                    connection,
                    event_type="semantic_auto_merge_applied",
                    target_type="graph_edge",
                    target_id=edge_id,
                    status="approved",
                    metadata=event_metadata,
                )
                if auto_tombstone:
                    self._append_review_event(
                        connection,
                        event_type="semantic_auto_tombstone_applied",
                        target_type="memory_unit",
                        target_id=duplicate.id,
                        status="tombstoned",
                        metadata=event_metadata,
                    )

            connection.commit()
            return {
                "applied": True,
                "tombstoned": auto_tombstone,
                "reason": "auto_merged",
                "canonical_memory_id": canonical.id,
                "duplicate_memory_id": duplicate.id,
                "edge_id": edge_id,
            }

    def restore_semantic_l2_merge(
        self,
        edge_id: str,
        reason: str = "manual_rollback",
    ) -> dict[str, object]:
        flags = high_risk_flags()
        with self.connect() as connection:
            edge_row = connection.execute(
                "SELECT metadata_json FROM graph_edges WHERE id = ?",
                (edge_id,),
            ).fetchone()
            if edge_row is None:
                return {"restored": False, "reason": "missing_edge", "edge_id": edge_id}

            edge_metadata = json.loads(edge_row["metadata_json"] or "{}")
            canonical_id = edge_metadata.get("canonical_memory_id")
            duplicate_id = edge_metadata.get("duplicate_memory_id")
            if not isinstance(canonical_id, str) or not isinstance(duplicate_id, str):
                return {
                    "restored": False,
                    "reason": "missing_merge_metadata",
                    "edge_id": edge_id,
                }

            canonical = self._get_memory_unit(connection, canonical_id)
            duplicate = self._get_memory_unit(connection, duplicate_id)
            if canonical is None or duplicate is None:
                return {"restored": False, "reason": "missing_memory", "edge_id": edge_id}

            now = utc_now_iso()
            canonical_metadata = edge_metadata.get("canonical_previous_metadata")
            if not isinstance(canonical_metadata, dict):
                canonical_metadata = dict(canonical.metadata)
                canonical_metadata["merged_duplicate_ids"] = _remove_metadata_list_item(
                    canonical_metadata.get("merged_duplicate_ids"),
                    duplicate.id,
                )
                canonical_metadata["semantic_merge_edge_ids"] = _remove_metadata_list_item(
                    canonical_metadata.get("semantic_merge_edge_ids"),
                    edge_id,
                )
                canonical_metadata["semantic_merge_count"] = len(
                    canonical_metadata.get("merged_duplicate_ids", [])
                )

            duplicate_metadata = edge_metadata.get("duplicate_previous_metadata")
            if not isinstance(duplicate_metadata, dict):
                duplicate_metadata = dict(duplicate.metadata)
                for key in (
                    "superseded_by",
                    "semantic_merge_edge_id",
                    "semantic_merge_status",
                    "semantic_merge_updated_at",
                ):
                    duplicate_metadata.pop(key, None)

            duplicate_status = _memory_status_from_value(
                edge_metadata.get("duplicate_previous_status"),
                default=MemoryStatus.ACTIVE,
            )
            canonical_status = _memory_status_from_value(
                edge_metadata.get("canonical_previous_status"),
                default=canonical.status,
            )
            self._put_memory_unit(
                connection,
                canonical.model_copy(
                    update={
                        "metadata": canonical_metadata,
                        "status": canonical_status,
                        "updated_at": now,
                    }
                ),
            )
            self._put_memory_unit(
                connection,
                duplicate.model_copy(
                    update={
                        "metadata": duplicate_metadata,
                        "status": duplicate_status,
                        "updated_at": now,
                    }
                ),
            )
            connection.execute(
                "DELETE FROM tombstones WHERE target_type = ? AND target_id = ?",
                ("memory_unit", duplicate.id),
            )

            edge_metadata.update(
                {
                    "resolution_status": "rolled_back",
                    "rollback_at": now,
                    "rollback_reason": reason,
                    "destructive_action_allowed": False,
                    "rollback_available": False,
                }
            )
            connection.execute(
                "UPDATE graph_edges SET metadata_json = ? WHERE id = ?",
                (json.dumps(edge_metadata, ensure_ascii=False, sort_keys=True), edge_id),
            )
            if flags.enable_review_event_sourcing:
                self._append_review_event(
                    connection,
                    event_type="semantic_auto_merge_rolled_back",
                    target_type="graph_edge",
                    target_id=edge_id,
                    status="rolled_back",
                    metadata={
                        "edge_id": edge_id,
                        "canonical_memory_id": canonical.id,
                        "duplicate_memory_id": duplicate.id,
                        "reason": reason,
                    },
                )
            connection.commit()
            return {
                "restored": True,
                "reason": "rolled_back",
                "edge_id": edge_id,
                "canonical_memory_id": canonical.id,
                "duplicate_memory_id": duplicate.id,
            }

    def record_access_signal(
        self,
        memory_id: str,
        signal: AccessSignal,
        strength: float = 1.0,
        metadata: Optional[dict[str, object]] = None,
    ) -> None:
        useful = signal in {AccessSignal.CITED_BY_AGENT, AccessSignal.USER_CONFIRMED}
        accessed = signal != AccessSignal.USER_REJECTED
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO lifecycle_events(id, memory_unit_id, signal, strength, metadata_json, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    random_id("lifecycle"),
                    memory_id,
                    signal.value,
                    strength,
                    json.dumps(metadata or {}, ensure_ascii=False, sort_keys=True),
                    utc_now_iso(),
                ),
            )
            if accessed:
                connection.execute(
                    """
                    UPDATE memory_units
                    SET access_count = access_count + 1,
                        useful_access_count = useful_access_count + ?,
                        last_accessed_at = ?,
                        updated_at = ?
                    WHERE id = ?
                    """,
                    (1 if useful else 0, utc_now_iso(), utc_now_iso(), memory_id),
                )
            connection.commit()

    def append_review_event(
        self,
        event_type: str,
        target_type: str,
        target_id: str,
        review_item_id: Optional[str] = None,
        plan_id: Optional[str] = None,
        status: Optional[str] = None,
        metadata: Optional[dict[str, object]] = None,
    ) -> ReviewEvent:
        with self.connect() as connection:
            event = self._append_review_event(
                connection,
                event_type=event_type,
                target_type=target_type,
                target_id=target_id,
                review_item_id=review_item_id,
                plan_id=plan_id,
                status=status,
                metadata=metadata,
            )
            connection.commit()
        return event

    def _append_review_event(
        self,
        connection: sqlite3.Connection,
        event_type: str,
        target_type: str,
        target_id: str,
        review_item_id: Optional[str] = None,
        plan_id: Optional[str] = None,
        status: Optional[str] = None,
        metadata: Optional[dict[str, object]] = None,
    ) -> ReviewEvent:
        event = ReviewEvent(
            id=random_id("review_event"),
            event_type=event_type,
            target_type=target_type,
            target_id=target_id,
            review_item_id=review_item_id,
            plan_id=plan_id,
            status=status,
            metadata=metadata or {},
            created_at=utc_now_iso(),
        )
        connection.execute(
            """
            INSERT INTO review_events(
              id, event_type, target_type, target_id, review_item_id,
              plan_id, status, metadata_json, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                event.id,
                event.event_type,
                event.target_type,
                event.target_id,
                event.review_item_id,
                event.plan_id,
                event.status,
                json.dumps(event.metadata, ensure_ascii=False, sort_keys=True),
                event.created_at,
            ),
        )
        if high_risk_flags().enable_review_audit_projections:
            self._upsert_review_audit_projection(connection, event)
        return event

    def list_review_events(
        self,
        target_type: Optional[str] = None,
        target_id: Optional[str] = None,
        event_type: Optional[str] = None,
        limit: int = 100,
    ) -> Sequence[ReviewEvent]:
        clauses: list[str] = []
        params: list[str] = []
        if target_type is not None:
            clauses.append("target_type = ?")
            params.append(target_type)
        if target_id is not None:
            clauses.append("target_id = ?")
            params.append(target_id)
        if event_type is not None:
            clauses.append("event_type = ?")
            params.append(event_type)
        where = "WHERE %s" % " AND ".join(clauses) if clauses else ""
        params.append(str(limit))
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM review_events
                %s
                ORDER BY created_at ASC
                LIMIT ?
                """
                % where,
                params,
            ).fetchall()
        return [
            ReviewEvent(
                id=row["id"],
                event_type=row["event_type"],
                target_type=row["target_type"],
                target_id=row["target_id"],
                review_item_id=row["review_item_id"],
                plan_id=row["plan_id"],
                status=row["status"],
                metadata=json.loads(row["metadata_json"] or "{}"),
                created_at=row["created_at"],
            )
            for row in rows
        ]

    def rebuild_review_audit_projections(self) -> Sequence[dict[str, object]]:
        with self.connect() as connection:
            connection.execute("DELETE FROM review_audit_projections")
            rows = connection.execute(
                "SELECT * FROM review_events ORDER BY created_at ASC"
            ).fetchall()
            for row in rows:
                self._upsert_review_audit_projection(
                    connection,
                    ReviewEvent(
                        id=row["id"],
                        event_type=row["event_type"],
                        target_type=row["target_type"],
                        target_id=row["target_id"],
                        review_item_id=row["review_item_id"],
                        plan_id=row["plan_id"],
                        status=row["status"],
                        metadata=json.loads(row["metadata_json"] or "{}"),
                        created_at=row["created_at"],
                    ),
                )
            connection.commit()
        return self.list_review_audit_projections()

    def list_review_audit_projections(self, limit: int = 1000) -> Sequence[dict[str, object]]:
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM review_audit_projections
                ORDER BY last_event_at DESC, target_type ASC, target_id ASC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [
            {
                "target_type": row["target_type"],
                "target_id": row["target_id"],
                "event_count": row["event_count"],
                "review_mark_count": row["review_mark_count"],
                "review_apply_count": row["review_apply_count"],
                "last_event_id": row["last_event_id"],
                "last_event_type": row["last_event_type"],
                "last_status": row["last_status"],
                "last_review_item_id": row["last_review_item_id"],
                "last_plan_id": row["last_plan_id"],
                "first_event_at": row["first_event_at"],
                "last_event_at": row["last_event_at"],
                "metadata": json.loads(row["metadata_json"] or "{}"),
            }
            for row in rows
        ]

    def _upsert_review_audit_projection(
        self,
        connection: sqlite3.Connection,
        event: ReviewEvent,
    ) -> None:
        row = connection.execute(
            """
            SELECT * FROM review_audit_projections
            WHERE target_type = ? AND target_id = ?
            """,
            (event.target_type, event.target_id),
        ).fetchone()
        metadata = (
            json.loads(row["metadata_json"] or "{}")
            if row is not None
            else {
                "event_type_counts": {},
                "status_counts": {},
                "high_risk_apply_count": 0,
                "second_confirmation_required_count": 0,
            }
        )
        event_type_counts = dict(metadata.get("event_type_counts", {}))
        event_type_counts[event.event_type] = event_type_counts.get(event.event_type, 0) + 1
        status_key = event.status or "unknown"
        status_counts = dict(metadata.get("status_counts", {}))
        status_counts[status_key] = status_counts.get(status_key, 0) + 1
        high_risk_apply_count = int(metadata.get("high_risk_apply_count", 0))
        second_confirmation_required_count = int(
            metadata.get("second_confirmation_required_count", 0)
        )
        if event.event_type == "review_apply" and event.metadata.get("risk") == "high":
            high_risk_apply_count += 1
        if (
            event.event_type == "review_apply"
            and event.metadata.get("requires_second_confirmation") is True
        ):
            second_confirmation_required_count += 1
        metadata.update(
            {
                "event_type_counts": event_type_counts,
                "status_counts": status_counts,
                "high_risk_apply_count": high_risk_apply_count,
                "second_confirmation_required_count": second_confirmation_required_count,
            }
        )

        if row is None:
            connection.execute(
                """
                INSERT INTO review_audit_projections(
                  target_type, target_id, event_count, review_mark_count, review_apply_count,
                  last_event_id, last_event_type, last_status, last_review_item_id,
                  last_plan_id, first_event_at, last_event_at, metadata_json
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    event.target_type,
                    event.target_id,
                    1,
                    1 if event.event_type == "review_mark" else 0,
                    1 if event.event_type == "review_apply" else 0,
                    event.id,
                    event.event_type,
                    event.status,
                    event.review_item_id,
                    event.plan_id,
                    event.created_at,
                    event.created_at,
                    json.dumps(metadata, ensure_ascii=False, sort_keys=True),
                ),
            )
            return

        connection.execute(
            """
            UPDATE review_audit_projections
            SET event_count = event_count + 1,
                review_mark_count = review_mark_count + ?,
                review_apply_count = review_apply_count + ?,
                last_event_id = ?,
                last_event_type = ?,
                last_status = ?,
                last_review_item_id = ?,
                last_plan_id = ?,
                last_event_at = ?,
                metadata_json = ?
            WHERE target_type = ? AND target_id = ?
            """,
            (
                1 if event.event_type == "review_mark" else 0,
                1 if event.event_type == "review_apply" else 0,
                event.id,
                event.event_type,
                event.status,
                event.review_item_id or row["last_review_item_id"],
                event.plan_id or row["last_plan_id"],
                event.created_at,
                json.dumps(metadata, ensure_ascii=False, sort_keys=True),
                event.target_type,
                event.target_id,
            ),
        )

    def add_evidence_refs(self, memory_id: str, refs: Iterable[EvidenceRef]) -> None:
        with self.connect() as connection:
            self._replace_evidence_refs(connection, memory_id, list(refs))
            connection.commit()

    def tombstone(self, target_type: str, target_id: str, reason: str) -> None:
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO tombstones(id, target_type, target_id, reason, created_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(target_type, target_id) DO UPDATE SET
                  reason = excluded.reason,
                  created_at = excluded.created_at
                """,
                (random_id("tombstone"), target_type, target_id, reason, utc_now_iso()),
            )
            if target_type == "memory_unit":
                connection.execute(
                    "UPDATE memory_units SET status = 'tombstoned', updated_at = ? WHERE id = ?",
                    (utc_now_iso(), target_id),
                )
            connection.commit()

    def _search_fts_rows(
        self,
        connection: sqlite3.Connection,
        query: str,
        limit: int,
        layers: Optional[Sequence[MemoryLayer]] = None,
        session_id: Optional[str] = None,
        statuses: Optional[Sequence[MemoryStatus]] = None,
        table: str = "memory_units_fts",
    ) -> Sequence[sqlite3.Row]:
        def build_sql() -> tuple[str, list[str]]:
            clauses = ["%s MATCH ?" % table]
            params: list[str] = [query]

            if layers:
                placeholders = ", ".join("?" for _ in layers)
                clauses.append("memory_units.layer IN (%s)" % placeholders)
                params.extend(layer.value for layer in layers)

            if statuses:
                placeholders = ", ".join("?" for _ in statuses)
                clauses.append("memory_units.status IN (%s)" % placeholders)
                params.extend(status.value for status in statuses)
            else:
                clauses.append("memory_units.status NOT IN ('deleted', 'tombstoned')")

            if session_id is not None:
                clauses.append("json_extract(memory_units.metadata_json, '$.session_id') = ?")
                params.append(session_id)

            sql = """
                SELECT %s.memory_id FROM %s
                JOIN memory_units ON memory_units.id = %s.memory_id
                WHERE %s
                ORDER BY rank
                LIMIT ?
                """ % (table, table, table, " AND ".join(clauses))
            params.append(str(limit))
            return sql, params

        sql, params = build_sql()
        try:
            return connection.execute(sql, params).fetchall()
        except sqlite3.OperationalError:
            quoted = '"%s"' % query.replace('"', '""')
            params[0] = quoted
            return connection.execute(sql, params).fetchall()

    def _replace_evidence_refs(
        self,
        connection: sqlite3.Connection,
        memory_id: str,
        refs: Sequence[EvidenceRef],
    ) -> None:
        connection.execute("DELETE FROM evidence_refs WHERE memory_unit_id = ?", (memory_id,))
        for ref in refs:
            connection.execute(
                """
                INSERT INTO evidence_refs(
                  id, memory_unit_id, target_id, target_layer, relation,
                  confidence, fallback_locator_json, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    random_id("evidence"),
                    memory_id,
                    ref.target_id,
                    ref.target_layer.value,
                    ref.relation.value,
                    ref.confidence,
                    json.dumps(
                        ref.fallback_locator.model_dump(mode="json")
                        if ref.fallback_locator is not None
                        else {},
                        ensure_ascii=False,
                        sort_keys=True,
                    ),
                    utc_now_iso(),
                ),
            )

    def _ensure_memory_graph_node(
        self,
        connection: sqlite3.Connection,
        memory_id: str,
    ) -> str:
        node_id = stable_id("graph_node", memory_id)
        now = utc_now_iso()
        connection.execute(
            """
            INSERT INTO graph_nodes(
              id, node_type, memory_unit_id, data_json, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
              memory_unit_id = excluded.memory_unit_id,
              updated_at = excluded.updated_at
            """,
            (node_id, "memory_unit", memory_id, "{}", now, now),
        )
        return node_id

    def _load_evidence_refs(
        self,
        connection: sqlite3.Connection,
        memory_id: str,
    ) -> Sequence[EvidenceRef]:
        rows = connection.execute(
            "SELECT * FROM evidence_refs WHERE memory_unit_id = ? ORDER BY created_at ASC",
            (memory_id,),
        ).fetchall()

        refs: List[EvidenceRef] = []
        for row in rows:
            fallback_data = json.loads(row["fallback_locator_json"] or "{}")
            refs.append(
                EvidenceRef(
                    target_id=row["target_id"],
                    target_layer=MemoryLayer(row["target_layer"]),
                    relation=EvidenceRelation(row["relation"]),
                    confidence=row["confidence"],
                    fallback_locator=FallbackLocator(**fallback_data) if fallback_data else None,
                )
            )
        return refs

    def _row_to_memory_unit(
        self,
        row: sqlite3.Row,
        evidence_refs: Sequence[EvidenceRef],
    ) -> MemoryUnit:
        metadata = json.loads(row["metadata_json"] or "{}")
        return MemoryUnit(
            id=row["id"],
            layer=MemoryLayer(row["layer"]),
            lifecycle_state=LifecycleState(row["lifecycle_state"]),
            status=MemoryStatus(row["status"]),
            content=row["content"],
            content_hash=row["content_hash"],
            summary=row["summary"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            last_accessed_at=row["last_accessed_at"],
            access_count=row["access_count"],
            useful_access_count=row["useful_access_count"],
            score=ScoreBreakdown(
                resident_score=row["resident_score"],
                structural=row["structural_score"],
                recency=row["recency_score"],
                access=row["access_score"],
                evidence_quality=row["evidence_quality_score"],
            ),
            lifecycle=LifecycleMeta(
                age=row["lifecycle_age"],
                protection_tier=row["protection_tier"],
                compression_tier=row["compression_tier"],
                observation_until=row["observation_until"],
                promotion_candidate_since=row["promotion_candidate_since"],
                demotion_candidate_since=row["demotion_candidate_since"],
            ),
            evidence_refs=list(evidence_refs),
            metadata=metadata,
        )
