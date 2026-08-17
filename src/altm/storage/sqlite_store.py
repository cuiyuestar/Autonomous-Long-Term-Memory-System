"""SQLite storage scaffold.

Phase 1 only guarantees schema initialization. CRUD, FTS writes, lifecycle
events, and tombstone behavior will be implemented behind the confirmed
MemoryStore port.
"""

from __future__ import annotations

import json
import math
import re
import sqlite3
from collections.abc import Iterable, Mapping, Sequence
from datetime import UTC, datetime, timedelta
from importlib.resources import files
from pathlib import Path
from types import TracebackType
from typing import Literal, cast

import sqlite_vec  # pyright: ignore[reportMissingTypeStubs]

from altm.config import high_risk_flags
from altm.contracts import (
    AccessSignal,
    ContextBundle,
    ContextCapsule,
    EvidenceRef,
    EvidenceRelation,
    FallbackLocator,
    GraphExtraction,
    L2Atom,
    L2AtomType,
    LifecycleMeta,
    LifecycleState,
    MemoryLayer,
    MemoryScope,
    MemoryStatus,
    MemoryUnit,
    MemoryVisibility,
    PersonaFacet,
    ReviewEvent,
    SceneBlock,
    ScoreBreakdown,
)
from altm.recall_policy import memory_matches_recall_session
from altm.utils import random_id, stable_id, utc_now_iso

_PACKAGED_SCHEMA_PATH = Path(
    str(files("altm").joinpath("schemas", "sqlite", "001_initial.sql"))
)
_REPOSITORY_SCHEMA_PATH = (
    Path(__file__).resolve().parents[3] / "schemas" / "sqlite" / "001_initial.sql"
)
DEFAULT_SCHEMA_PATH = (
    _PACKAGED_SCHEMA_PATH
    if _PACKAGED_SCHEMA_PATH.exists()
    else _REPOSITORY_SCHEMA_PATH
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


class _ClosingConnection(sqlite3.Connection):
    """SQLite transaction context that also releases its file handle."""

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> Literal[False]:
        try:
            return super().__exit__(exc_type, exc_value, traceback)
        finally:
            self.close()


def _row_to_graph_edge(row: sqlite3.Row) -> dict[str, object]:
    return {
        "id": row["id"],
        "source_node_id": row["source_node_id"],
        "target_node_id": row["target_node_id"],
        "source_memory_id": row["source_memory_id"],
        "target_memory_id": row["target_memory_id"],
        "edge_type": row["edge_type"],
        "weight": row["weight"],
        "confidence": row["confidence"],
        "metadata": json.loads(row["metadata_json"] or "{}"),
        "created_at": row["created_at"],
    }


def _row_to_graph_node(
    row: sqlite3.Row,
    evidence_memory_ids: Sequence[str] = (),
) -> dict[str, object]:
    evidence_ids = list(evidence_memory_ids)
    if row["memory_unit_id"] is not None and row["memory_unit_id"] not in evidence_ids:
        evidence_ids.append(str(row["memory_unit_id"]))
    return {
        "id": row["id"],
        "scope": {
            "tenant_id": row["tenant_id"],
            "workspace_id": row["workspace_id"],
            "user_id": row["user_id"],
            "agent_id": row["agent_id"],
        },
        "node_type": row["node_type"],
        "canonical_key": row["canonical_key"],
        "name": row["name"],
        "memory_unit_id": row["memory_unit_id"],
        "evidence_memory_ids": evidence_ids,
        "data": json.loads(row["data_json"] or "{}"),
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def _graph_fts_query(query: str) -> str:
    terms = list(
        dict.fromkeys(
            term.casefold()
            for term in re.findall(r"[\w]+(?:[-:][\w]+)*", query, flags=re.UNICODE)
            if term.strip()
        )
    )[:24]
    if not terms:
        terms = [query.strip()]
    return " OR ".join(
        '"%s"' % term.replace('"', '""')
        for term in terms
    )


def _graph_evidence_map(
    connection: sqlite3.Connection,
    node_ids: Sequence[str],
) -> dict[str, list[str]]:
    if not node_ids:
        return {}
    placeholders = ", ".join("?" for _ in node_ids)
    rows = connection.execute(
        """
        SELECT node_id, memory_unit_id
        FROM graph_node_evidence
        WHERE node_id IN (%s)
        ORDER BY node_id ASC, created_at ASC, memory_unit_id ASC
        """
        % placeholders,
        node_ids,
    ).fetchall()
    result: dict[str, list[str]] = {}
    for row in rows:
        result.setdefault(str(row["node_id"]), []).append(
            str(row["memory_unit_id"])
        )
    return result


def _row_to_persona_facet(row: sqlite3.Row) -> PersonaFacet:
    return PersonaFacet(
        id=row["id"],
        facet_key=row["facet_key"],
        facet_type=row["facet_type"],
        statement=row["statement"],
        workspace_scope=row["workspace_scope"],
        confidence=row["confidence"],
        stability_score=row["stability_score"],
        status=row["status"],
        source_memory_ids=[
            str(value)
            for value in json.loads(row["source_memory_ids_json"] or "[]")
        ],
        source_agent_ids=[
            str(value)
            for value in json.loads(row["source_agent_ids_json"] or "[]")
        ],
        counter_evidence_memory_ids=[
            str(value)
            for value in json.loads(
                row["counter_evidence_memory_ids_json"] or "[]"
            )
        ],
        first_observed_at=row["first_observed_at"],
        last_observed_at=row["last_observed_at"],
        observation_cycles=row["observation_cycles"],
        metadata=json.loads(row["metadata_json"] or "{}"),
    )


def _merged_graph_edge_metadata(
    existing: dict[str, object],
    incoming: Mapping[str, object],
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
    values = (
        [str(existing) for existing in cast(list[object], value)]
        if isinstance(value, list)
        else []
    )
    if item not in values:
        values.append(item)
    return values


def _remove_metadata_list_item(value: object, item: str) -> list[str]:
    if not isinstance(value, list):
        return []
    return [
        str(existing)
        for existing in cast(list[object], value)
        if str(existing) != item
    ]


def _memory_status_from_value(value: object, default: MemoryStatus) -> MemoryStatus:
    if isinstance(value, MemoryStatus):
        return value
    if isinstance(value, str):
        try:
            return MemoryStatus(value)
        except ValueError:
            return default
    return default


def _object_dict(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        return {}
    return {
        str(key): item
        for key, item in cast(dict[object, object], value).items()
    }


def _string_list(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item) for item in cast(list[object], value)]


def _count_dict(value: object) -> dict[str, int]:
    result: dict[str, int] = {}
    for key, item in _object_dict(value).items():
        if isinstance(item, int):
            result[key] = item
    return result


def _int_value(value: object) -> int:
    return value if isinstance(value, int) else 0


def _vector_norm(vector: Sequence[float]) -> float:
    return math.sqrt(sum(float(value) * float(value) for value in vector))


def _recall_session_filter(
    session_id: str | None,
    cross_session_layers: Sequence[MemoryLayer],
    table: str | None = None,
) -> tuple[str | None, list[str]]:
    if session_id is None:
        return None, []
    prefix = "%s." % table if table else ""
    metadata = "%smetadata_json" % prefix
    if not cross_session_layers:
        return "json_extract(%s, '$.session_id') = ?" % metadata, [session_id]
    placeholders = ", ".join("?" for _ in cross_session_layers)
    return (
        (
            "(json_extract(%s, '$.session_id') = ? OR "
            "(%slayer IN (%s) "
            "AND COALESCE(json_extract(%s, '$.review_status'), '') != 'rejected' "
            "AND COALESCE(json_extract(%s, '$.governance_review_status'), '') "
            "!= 'rejected'))"
        )
        % (metadata, prefix, placeholders, metadata, metadata),
        [session_id, *(layer.value for layer in cross_session_layers)],
    )


class SQLiteMemoryStore:
    def __init__(
        self,
        db_path: str | Path,
        schema_path: str | Path | None = None,
        scope: MemoryScope | None = None,
    ) -> None:
        self.db_path = Path(db_path)
        self.schema_path = Path(schema_path) if schema_path is not None else DEFAULT_SCHEMA_PATH
        self.scope = scope

    def initialize(self) -> None:
        if not self.schema_path.exists():
            raise FileNotFoundError("SQLite schema not found: %s" % self.schema_path)

        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        schema = self.schema_path.read_text(encoding="utf-8")

        with sqlite3.connect(
            str(self.db_path),
            factory=_ClosingConnection,
        ) as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.execute("PRAGMA busy_timeout = 5000")
            connection.executescript(schema)
            self._ensure_schema_compatibility(connection)
            connection.commit()

    def connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            str(self.db_path),
            factory=_ClosingConnection,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 5000")
        connection.enable_load_extension(True)
        sqlite_vec.load(connection)
        connection.enable_load_extension(False)
        return connection

    def _ensure_runtime_cycle_statuses(
        self,
        connection: sqlite3.Connection,
    ) -> None:
        row = connection.execute(
            """
            SELECT sql FROM sqlite_master
            WHERE type = 'table' AND name = 'runtime_cycles'
            """
        ).fetchone()
        if row is None or "'aborted'" in str(row[0]):
            return
        connection.executescript(
            """
            CREATE TABLE runtime_cycles_next (
              id TEXT PRIMARY KEY,
              tenant_id TEXT NOT NULL,
              workspace_id TEXT NOT NULL,
              user_id TEXT NOT NULL,
              agent_id TEXT NOT NULL,
              session_id TEXT NOT NULL,
              turn_id TEXT NOT NULL,
              user_memory_id TEXT NOT NULL,
              user_content_hash TEXT NOT NULL,
              query TEXT NOT NULL,
              context_json TEXT NOT NULL,
              context_memory_ids_json TEXT NOT NULL DEFAULT '[]',
              status TEXT NOT NULL
                CHECK (status IN ('prepared', 'committed', 'aborted', 'failed')),
              assistant_memory_id TEXT,
              assistant_content_hash TEXT,
              cited_memory_ids_json TEXT NOT NULL DEFAULT '[]',
              metadata_json TEXT NOT NULL DEFAULT '{}',
              created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL,
              UNIQUE (
                tenant_id, workspace_id, user_id, agent_id,
                session_id, turn_id
              ),
              FOREIGN KEY (user_memory_id)
                REFERENCES memory_units(id) ON DELETE RESTRICT,
              FOREIGN KEY (assistant_memory_id)
                REFERENCES memory_units(id) ON DELETE RESTRICT
            );
            INSERT INTO runtime_cycles_next
            SELECT * FROM runtime_cycles;
            DROP TABLE runtime_cycles;
            ALTER TABLE runtime_cycles_next RENAME TO runtime_cycles;
            CREATE INDEX idx_runtime_cycles_scope_session
              ON runtime_cycles(
                tenant_id, workspace_id, user_id, agent_id,
                session_id, created_at
              );
            CREATE INDEX idx_runtime_cycles_status
              ON runtime_cycles(status, updated_at);
            """
        )

    def _ensure_schema_compatibility(self, connection: sqlite3.Connection) -> None:
        self._ensure_runtime_cycle_statuses(connection)
        columns = {
            row[1]
            for row in connection.execute("PRAGMA table_info(memory_units)").fetchall()
        }
        if "promotion_candidate_since" not in columns:
            connection.execute("ALTER TABLE memory_units ADD COLUMN promotion_candidate_since TEXT")
        if "demotion_candidate_since" not in columns:
            connection.execute("ALTER TABLE memory_units ADD COLUMN demotion_candidate_since TEXT")
        scope_columns = {
            "tenant_id": "TEXT NOT NULL DEFAULT 'local'",
            "workspace_id": "TEXT NOT NULL DEFAULT 'default'",
            "user_id": "TEXT NOT NULL DEFAULT 'default'",
            "agent_id": "TEXT NOT NULL DEFAULT 'default'",
            "visibility": "TEXT NOT NULL DEFAULT 'agent'",
        }
        for name, definition in scope_columns.items():
            if name not in columns:
                connection.execute(
                    "ALTER TABLE memory_units ADD COLUMN %s %s" % (name, definition)
                )
        connection.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_memory_units_scope
              ON memory_units(tenant_id, workspace_id, user_id, agent_id)
            """
        )
        connection.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_memory_units_scope_layer
              ON memory_units(tenant_id, workspace_id, user_id, agent_id, layer, status)
            """
        )
        connection.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_memory_units_shared
              ON memory_units(
                tenant_id, workspace_id, user_id, visibility, layer, status
              )
            """
        )
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
        persona_columns = {
            row[1]
            for row in connection.execute(
                "PRAGMA table_info(l4_persona_facets)"
            ).fetchall()
        }
        if "facet_key" not in persona_columns:
            connection.execute(
                "ALTER TABLE l4_persona_facets ADD COLUMN facet_key TEXT"
            )
        connection.execute(
            """
            UPDATE l4_persona_facets
            SET facet_key = COALESCE(
              NULLIF(json_extract(metadata_json, '$.facet_key'), ''),
              facet_type || ':' || id
            )
            WHERE facet_key IS NULL OR facet_key = ''
            """
        )
        connection.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS idx_l4_persona_current_facet
              ON l4_persona_facets(
                tenant_id, workspace_id, user_id, facet_key
              )
              WHERE status IN ('observing', 'active')
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS l4_persona_history (
              id TEXT PRIMARY KEY,
              persona_id TEXT NOT NULL,
              replacement_persona_id TEXT NOT NULL,
              snapshot_json TEXT NOT NULL,
              reason TEXT NOT NULL,
              confidence REAL NOT NULL
                CHECK (confidence >= 0 AND confidence <= 1),
              created_at TEXT NOT NULL,
              FOREIGN KEY (persona_id)
                REFERENCES l4_persona_facets(id) ON DELETE CASCADE,
              FOREIGN KEY (replacement_persona_id)
                REFERENCES l4_persona_facets(id) ON DELETE CASCADE
            )
            """
        )
        connection.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_l4_persona_history_persona
              ON l4_persona_history(persona_id, created_at)
            """
        )
        graph_columns = {
            row[1]
            for row in connection.execute("PRAGMA table_info(graph_nodes)").fetchall()
        }
        graph_scope_columns = {
            "tenant_id": "TEXT NOT NULL DEFAULT 'local'",
            "workspace_id": "TEXT NOT NULL DEFAULT 'default'",
            "user_id": "TEXT NOT NULL DEFAULT 'default'",
            "agent_id": "TEXT NOT NULL DEFAULT 'default'",
            "canonical_key": "TEXT",
            "name": "TEXT",
        }
        for name, definition in graph_scope_columns.items():
            if name not in graph_columns:
                connection.execute(
                    "ALTER TABLE graph_nodes ADD COLUMN %s %s" % (name, definition)
                )
        connection.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS idx_graph_nodes_scope_canonical
              ON graph_nodes(
                tenant_id, workspace_id, user_id, agent_id, canonical_key
              )
              WHERE canonical_key IS NOT NULL
            """
        )
        connection.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_graph_nodes_scope_type
              ON graph_nodes(
                tenant_id, workspace_id, user_id, agent_id, node_type
              )
            """
        )
        connection.execute(
            """
            CREATE VIRTUAL TABLE IF NOT EXISTS graph_nodes_fts USING fts5(
              node_id UNINDEXED,
              name,
              canonical_key,
              data,
              tokenize = 'unicode61 remove_diacritics 2'
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS graph_node_evidence (
              node_id TEXT NOT NULL,
              memory_unit_id TEXT NOT NULL,
              confidence REAL NOT NULL DEFAULT 1
                CHECK (confidence >= 0 AND confidence <= 1),
              created_at TEXT NOT NULL,
              PRIMARY KEY (node_id, memory_unit_id),
              FOREIGN KEY (node_id) REFERENCES graph_nodes(id) ON DELETE CASCADE,
              FOREIGN KEY (memory_unit_id) REFERENCES memory_units(id) ON DELETE CASCADE
            )
            """
        )
        connection.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_graph_node_evidence_memory
              ON graph_node_evidence(memory_unit_id, node_id)
            """
        )
        connection.executescript(
            """
            CREATE TRIGGER IF NOT EXISTS graph_nodes_fts_insert
            AFTER INSERT ON graph_nodes BEGIN
              INSERT INTO graph_nodes_fts(node_id, name, canonical_key, data)
              VALUES (
                new.id,
                COALESCE(new.name, ''),
                COALESCE(new.canonical_key, ''),
                COALESCE(new.data_json, '{}')
              );
            END;

            CREATE TRIGGER IF NOT EXISTS graph_nodes_fts_update
            AFTER UPDATE OF name, canonical_key, data_json ON graph_nodes BEGIN
              DELETE FROM graph_nodes_fts WHERE node_id = old.id;
              INSERT INTO graph_nodes_fts(node_id, name, canonical_key, data)
              VALUES (
                new.id,
                COALESCE(new.name, ''),
                COALESCE(new.canonical_key, ''),
                COALESCE(new.data_json, '{}')
              );
            END;

            CREATE TRIGGER IF NOT EXISTS graph_nodes_fts_delete
            AFTER DELETE ON graph_nodes BEGIN
              DELETE FROM graph_nodes_fts WHERE node_id = old.id;
            END;
            """
        )
        node_count = int(
            connection.execute("SELECT COUNT(*) FROM graph_nodes").fetchone()[0]
        )
        indexed_count = int(
            connection.execute(
                "SELECT COUNT(DISTINCT node_id) FROM graph_nodes_fts"
            ).fetchone()[0]
        )
        if indexed_count != node_count:
            connection.execute("DELETE FROM graph_nodes_fts")
            connection.execute(
                """
                INSERT INTO graph_nodes_fts(node_id, name, canonical_key, data)
                SELECT
                  id,
                  COALESCE(name, ''),
                  COALESCE(canonical_key, ''),
                  COALESCE(data_json, '{}')
                FROM graph_nodes
                """
            )

    def put_memory_unit(self, memory: MemoryUnit) -> None:
        self._require_memory_scope(memory)
        with self.connect() as connection:
            self._put_memory_unit(connection, memory)
            connection.commit()

    def put_memory_units(self, memories: Sequence[MemoryUnit]) -> None:
        for memory in memories:
            self._require_memory_scope(memory)
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            for memory in memories:
                self._put_memory_unit(connection, memory)
            connection.commit()

    def _put_memory_unit(self, connection: sqlite3.Connection, memory: MemoryUnit) -> None:
        self._require_memory_scope(memory)
        existing_row = connection.execute(
            "SELECT * FROM memory_units WHERE id = ?",
            (memory.id,),
        ).fetchone()
        if existing_row is not None and existing_row["layer"] == MemoryLayer.L0.value:
            existing_metadata = json.loads(existing_row["metadata_json"] or "{}")
            immutable_values_match = (
                memory.layer == MemoryLayer.L0
                and existing_row["content_hash"] == memory.content_hash
                and existing_row["created_at"] == memory.created_at
                and existing_row["tenant_id"] == memory.scope.tenant_id
                and existing_row["workspace_id"] == memory.scope.workspace_id
                and existing_row["user_id"] == memory.scope.user_id
                and existing_row["agent_id"] == memory.scope.agent_id
                and existing_row["visibility"] == memory.visibility.value
                and existing_metadata.get("session_id")
                == memory.metadata.get("session_id")
                and existing_metadata.get("role") == memory.metadata.get("role")
                and existing_metadata.get("message_id")
                == memory.metadata.get("message_id")
            )
            if not immutable_values_match:
                raise ValueError(
                    "L0 append-only invariant violation for memory %s" % memory.id
                )
        connection.execute(
            """
            INSERT INTO memory_units (
              id, tenant_id, workspace_id, user_id, agent_id,
              visibility, layer, lifecycle_state, status, content, content_hash, summary,
              created_at, updated_at, last_accessed_at, access_count,
              useful_access_count, resident_score, structural_score, recency_score,
              access_score, evidence_quality_score, lifecycle_age, protection_tier,
              compression_tier, observation_until, promotion_candidate_since,
              demotion_candidate_since, metadata_json
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
              tenant_id = excluded.tenant_id,
              workspace_id = excluded.workspace_id,
              user_id = excluded.user_id,
              agent_id = excluded.agent_id,
              visibility = excluded.visibility,
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
                memory.scope.tenant_id,
                memory.scope.workspace_id,
                memory.scope.user_id,
                memory.scope.agent_id,
                memory.visibility.value,
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

    def get_memory_unit(self, memory_id: str) -> MemoryUnit | None:
        with self.connect() as connection:
            return self._get_memory_unit(connection, memory_id)

    def _get_memory_unit(
        self,
        connection: sqlite3.Connection,
        memory_id: str,
    ) -> MemoryUnit | None:
        row = connection.execute(
            "SELECT * FROM memory_units WHERE id = ? AND status != 'deleted'%s"
            % self._memory_scope_sql(),
            (memory_id, *self._memory_scope_params()),
        ).fetchone()
        if row is None:
            return None
        evidence_refs = self._load_evidence_refs(connection, memory_id)
        return self._row_to_memory_unit(row, evidence_refs)

    def list_memory_units(
        self,
        layer: MemoryLayer | None = None,
        limit: int = 100,
    ) -> Sequence[MemoryUnit]:
        sql = "SELECT * FROM memory_units WHERE status != 'deleted'"
        params: list[str] = []
        if self.scope is not None:
            sql += self._memory_scope_sql()
            params.extend(self._memory_scope_params())
        if layer is not None:
            sql += " AND layer = ?"
            params.append(layer.value)
        sql += " ORDER BY created_at ASC, id ASC LIMIT ?"

        with self.connect() as connection:
            rows = connection.execute(sql, (*params, limit)).fetchall()
            return [
                self._row_to_memory_unit(row, self._load_evidence_refs(connection, row["id"]))
                for row in rows
            ]

    def list_recent_memory_units(
        self,
        layers: Sequence[MemoryLayer],
        limit_per_layer: int = 80,
    ) -> dict[str, Sequence[MemoryUnit]]:
        if limit_per_layer <= 0:
            return {layer.value: [] for layer in layers}
        result: dict[str, Sequence[MemoryUnit]] = {}
        with self.connect() as connection:
            for layer in dict.fromkeys(layers):
                rows = connection.execute(
                    """
                    SELECT * FROM memory_units
                    WHERE status != 'deleted'%s
                      AND layer = ?
                    ORDER BY updated_at DESC, id ASC
                    LIMIT ?
                    """
                    % self._memory_scope_sql(),
                    (*self._memory_scope_params(), layer.value, limit_per_layer),
                ).fetchall()
                result[layer.value] = [
                    self._row_to_memory_unit(
                        row,
                        self._load_evidence_refs(connection, row["id"]),
                    )
                    for row in rows
                ]
        return result

    def memory_layer_counts(
        self,
        layers: Sequence[MemoryLayer],
    ) -> dict[str, int]:
        requested = list(dict.fromkeys(layers))
        counts = {layer.value: 0 for layer in requested}
        if not requested:
            return counts
        placeholders = ", ".join("?" for _ in requested)
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT layer, COUNT(*) AS count
                FROM memory_units
                WHERE status != 'deleted'%s
                  AND layer IN (%s)
                GROUP BY layer
                """
                % (self._memory_scope_sql(), placeholders),
                (*self._memory_scope_params(), *(layer.value for layer in requested)),
            ).fetchall()
        for row in rows:
            counts[str(row["layer"])] = int(row["count"])
        return counts

    def list_l0_by_session(self, session_id: str, limit: int = 200) -> Sequence[MemoryUnit]:
        with self.connect() as connection:
            try:
                sql = """
                    SELECT * FROM memory_units
                    WHERE layer = 'L0'
                      AND status != 'deleted'%s
                      AND json_extract(metadata_json, '$.session_id') = ?
                    ORDER BY created_at ASC, id ASC
                    LIMIT ?
                    """ % self._scope_sql()
                rows = connection.execute(
                    sql,
                    (*self._scope_params(), session_id, limit),
                ).fetchall()
            except sqlite3.OperationalError:
                rows = connection.execute(
                    """
                    SELECT * FROM memory_units
                    WHERE layer = 'L0' AND status != 'deleted'%s
                    ORDER BY created_at ASC, id ASC
                    LIMIT ?
                    """
                    % self._scope_sql(),
                    (*self._scope_params(), limit),
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

    def put_l3_scene(self, scene: SceneBlock, memory: MemoryUnit) -> None:
        if memory.layer != MemoryLayer.L3 or memory.id != scene.id:
            raise ValueError("L3 scene and MemoryUnit identity must match")
        self._require_memory_scope(memory)
        now = utc_now_iso()
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._put_memory_unit(connection, memory)
            connection.execute(
                """
                INSERT INTO l3_scenes(
                  id, memory_unit_id, title, scene_type, summary,
                  active_facts_json, historical_facts_json, open_questions_json,
                  known_risks_json, source_memory_ids_json, source_session_ids_json,
                  confidence, boundary_risk, observation_cycles, metadata_json,
                  created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                  memory_unit_id = excluded.memory_unit_id,
                  title = excluded.title,
                  scene_type = excluded.scene_type,
                  summary = excluded.summary,
                  active_facts_json = excluded.active_facts_json,
                  historical_facts_json = excluded.historical_facts_json,
                  open_questions_json = excluded.open_questions_json,
                  known_risks_json = excluded.known_risks_json,
                  source_memory_ids_json = excluded.source_memory_ids_json,
                  source_session_ids_json = excluded.source_session_ids_json,
                  confidence = excluded.confidence,
                  boundary_risk = excluded.boundary_risk,
                  observation_cycles = excluded.observation_cycles,
                  metadata_json = excluded.metadata_json,
                  updated_at = excluded.updated_at
                """,
                (
                    scene.id,
                    memory.id,
                    scene.title,
                    scene.scene_type.value,
                    scene.summary,
                    json.dumps(scene.active_facts, ensure_ascii=False),
                    json.dumps(scene.historical_facts, ensure_ascii=False),
                    json.dumps(scene.open_questions, ensure_ascii=False),
                    json.dumps(scene.known_risks, ensure_ascii=False),
                    json.dumps(scene.source_memory_ids, ensure_ascii=False),
                    json.dumps(scene.source_session_ids, ensure_ascii=False),
                    scene.confidence,
                    scene.boundary_risk,
                    scene.observation_cycles,
                    json.dumps(scene.metadata, ensure_ascii=False, sort_keys=True),
                    memory.created_at,
                    now,
                ),
            )
            connection.commit()

    def put_l4_persona(self, persona: PersonaFacet, memory: MemoryUnit) -> None:
        self._validate_l4_persona(persona, memory)
        now = utc_now_iso()
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._put_l4_persona(connection, persona, memory, now)
            connection.commit()

    def _validate_l4_persona(
        self,
        persona: PersonaFacet,
        memory: MemoryUnit,
    ) -> None:
        if memory.layer != MemoryLayer.L4 or memory.id != persona.id:
            raise ValueError("L4 persona and MemoryUnit identity must match")
        if memory.visibility != MemoryVisibility.USER_WORKSPACE:
            raise ValueError("L4 shared persona must use user_workspace visibility")
        if not persona.facet_key.strip():
            raise ValueError("L4 persona facet_key must not be empty")
        self._require_memory_scope(memory)

    def _put_l4_persona(
        self,
        connection: sqlite3.Connection,
        persona: PersonaFacet,
        memory: MemoryUnit,
        now: str,
    ) -> None:
        self._put_memory_unit(connection, memory)
        connection.execute(
            """
            INSERT INTO l4_persona_facets(
              id, memory_unit_id, tenant_id, workspace_id, user_id,
              facet_key, facet_type, statement, workspace_scope, confidence,
              stability_score, status, source_memory_ids_json,
              source_agent_ids_json, counter_evidence_memory_ids_json,
              first_observed_at, last_observed_at, observation_cycles,
              metadata_json, created_at, updated_at
            )
            VALUES (
              ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
            )
            ON CONFLICT(id) DO UPDATE SET
              memory_unit_id = excluded.memory_unit_id,
              facet_key = excluded.facet_key,
              facet_type = excluded.facet_type,
              statement = excluded.statement,
              workspace_scope = excluded.workspace_scope,
              confidence = excluded.confidence,
              stability_score = excluded.stability_score,
              status = excluded.status,
              source_memory_ids_json = excluded.source_memory_ids_json,
              source_agent_ids_json = excluded.source_agent_ids_json,
              counter_evidence_memory_ids_json = excluded.counter_evidence_memory_ids_json,
              last_observed_at = excluded.last_observed_at,
              observation_cycles = excluded.observation_cycles,
              metadata_json = excluded.metadata_json,
              updated_at = excluded.updated_at
            """,
            (
                persona.id,
                memory.id,
                memory.scope.tenant_id,
                memory.scope.workspace_id,
                memory.scope.user_id,
                persona.facet_key,
                persona.facet_type,
                persona.statement,
                persona.workspace_scope,
                persona.confidence,
                persona.stability_score,
                persona.status.value,
                json.dumps(persona.source_memory_ids, ensure_ascii=False),
                json.dumps(persona.source_agent_ids, ensure_ascii=False),
                json.dumps(
                    persona.counter_evidence_memory_ids,
                    ensure_ascii=False,
                ),
                persona.first_observed_at,
                persona.last_observed_at,
                persona.observation_cycles,
                json.dumps(persona.metadata, ensure_ascii=False, sort_keys=True),
                memory.created_at,
                now,
            ),
        )

    def list_current_l4_personas(
        self,
        limit: int = 100,
    ) -> Sequence[PersonaFacet]:
        scope = self._required_active_scope()
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT *
                FROM l4_persona_facets
                WHERE tenant_id = ?
                  AND workspace_id = ?
                  AND user_id = ?
                  AND status IN ('observing', 'active')
                ORDER BY updated_at DESC, id ASC
                LIMIT ?
                """,
                (
                    scope.tenant_id,
                    scope.workspace_id,
                    scope.user_id,
                    limit,
                ),
            ).fetchall()
        return [_row_to_persona_facet(row) for row in rows]

    def supersede_l4_persona(
        self,
        previous_persona_id: str,
        replacement: PersonaFacet,
        replacement_memory: MemoryUnit,
        reason: str,
        confidence: float,
    ) -> str:
        self._validate_l4_persona(replacement, replacement_memory)
        if replacement.status.value != "active":
            raise ValueError("L4 replacement persona must be active")
        if replacement_memory.status != MemoryStatus.ACTIVE:
            raise ValueError("L4 replacement MemoryUnit must be active")
        if not 0.0 <= confidence <= 1.0:
            raise ValueError("L4 supersession confidence must be between 0 and 1")
        scope = self._required_active_scope()
        now = utc_now_iso()
        history_id = stable_id(
            "l4_persona_history",
            previous_persona_id,
            replacement.id,
        )
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            previous_row = connection.execute(
                """
                SELECT *
                FROM l4_persona_facets
                WHERE id = ?
                  AND tenant_id = ?
                  AND workspace_id = ?
                  AND user_id = ?
                  AND status IN ('observing', 'active')
                """,
                (
                    previous_persona_id,
                    scope.tenant_id,
                    scope.workspace_id,
                    scope.user_id,
                ),
            ).fetchone()
            if previous_row is None:
                raise ValueError("Current L4 persona to supersede was not found")
            previous_persona = _row_to_persona_facet(previous_row)
            if previous_persona.facet_key != replacement.facet_key:
                raise ValueError("L4 supersession requires the same facet_key")
            previous_memory = self._get_memory_unit(
                connection,
                previous_persona_id,
            )
            if previous_memory is None:
                raise RuntimeError("L4 persona MemoryUnit is missing")

            snapshot = {
                "persona": previous_persona.model_dump(mode="json"),
                "memory": previous_memory.model_dump(mode="json"),
            }
            previous_metadata = {
                **previous_persona.metadata,
                "superseded_by": replacement.id,
                "superseded_at": now,
                "supersession_reason": reason,
                "supersession_confidence": confidence,
            }
            connection.execute(
                """
                UPDATE l4_persona_facets
                SET status = 'superseded',
                    metadata_json = ?,
                    updated_at = ?
                WHERE id = ?
                """,
                (
                    json.dumps(
                        previous_metadata,
                        ensure_ascii=False,
                        sort_keys=True,
                    ),
                    now,
                    previous_persona_id,
                ),
            )
            previous_memory = previous_memory.model_copy(
                update={
                    "status": MemoryStatus.TOMBSTONED,
                    "updated_at": now,
                    "metadata": {
                        **previous_memory.metadata,
                        "persona_status": "superseded",
                        "superseded_by": replacement.id,
                        "superseded_at": now,
                        "retention_protected": True,
                    },
                }
            )
            self._put_memory_unit(connection, previous_memory)
            self._put_l4_persona(
                connection,
                replacement,
                replacement_memory,
                now,
            )
            connection.execute(
                """
                INSERT INTO l4_persona_history(
                  id, persona_id, replacement_persona_id, snapshot_json,
                  reason, confidence, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    history_id,
                    previous_persona_id,
                    replacement.id,
                    json.dumps(snapshot, ensure_ascii=False, sort_keys=True),
                    reason,
                    confidence,
                    now,
                ),
            )
            self._append_review_event(
                connection,
                event_type="autonomous_l4_superseded",
                target_type="memory_unit",
                target_id=previous_persona_id,
                status="superseded",
                metadata={
                    "replacement_persona_id": replacement.id,
                    "facet_key": replacement.facet_key,
                    "history_id": history_id,
                    "reason": reason,
                    "confidence": confidence,
                    "source_memory_ids": replacement.source_memory_ids,
                },
            )
            connection.commit()
        return history_id

    def list_user_workspace_memories(
        self,
        layer: MemoryLayer,
        limit: int = 1000,
    ) -> Sequence[MemoryUnit]:
        scope = self._required_active_scope()
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM memory_units
                WHERE tenant_id = ? AND workspace_id = ? AND user_id = ?
                  AND layer = ?
                  AND status NOT IN ('deleted', 'tombstoned')
                ORDER BY created_at ASC, id ASC
                LIMIT ?
                """,
                (
                    scope.tenant_id,
                    scope.workspace_id,
                    scope.user_id,
                    layer.value,
                    limit,
                ),
            ).fetchall()
            return [
                self._row_to_memory_unit(
                    row,
                    self._load_evidence_refs(connection, row["id"]),
                )
                for row in rows
            ]

    def list_unprocessed_session_memories(
        self,
        layer: MemoryLayer,
        session_id: str,
        checkpoint_scope: str,
        limit: int = 100,
    ) -> tuple[Sequence[MemoryUnit], int]:
        cursor = int(self.get_checkpoint(checkpoint_scope) or "0")
        sql = """
            SELECT rowid AS storage_rowid, * FROM memory_units
            WHERE rowid > ?
              AND layer = ?
              AND status != 'deleted'%s
              AND json_extract(metadata_json, '$.session_id') = ?
            ORDER BY rowid ASC
            LIMIT ?
            """ % self._scope_sql()
        with self.connect() as connection:
            rows = connection.execute(
                sql,
                (cursor, layer.value, *self._scope_params(), session_id, limit),
            ).fetchall()
            memories = [
                self._row_to_memory_unit(row, self._load_evidence_refs(connection, row["id"]))
                for row in rows
            ]
            next_cursor = int(rows[-1]["storage_rowid"]) if rows else cursor
            return memories, next_cursor

    def get_checkpoint(self, checkpoint_scope: str) -> str | None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT cursor FROM checkpoints WHERE scope = ?",
                (checkpoint_scope,),
            ).fetchone()
            return str(row["cursor"]) if row is not None else None

    def put_checkpoint(
        self,
        checkpoint_scope: str,
        cursor: str,
        metadata: dict[str, object] | None = None,
    ) -> None:
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO checkpoints(scope, cursor, updated_at, metadata_json)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(scope) DO UPDATE SET
                  cursor = excluded.cursor,
                  updated_at = excluded.updated_at,
                  metadata_json = excluded.metadata_json
                """,
                (
                    checkpoint_scope,
                    cursor,
                    utc_now_iso(),
                    json.dumps(metadata or {}, ensure_ascii=False, sort_keys=True),
                ),
            )
            connection.commit()

    def create_runtime_cycle(
        self,
        cycle_id: str,
        session_id: str,
        turn_id: str,
        user_memory_id: str,
        user_content_hash: str,
        query: str,
        context: ContextBundle,
        metadata: dict[str, object] | None = None,
    ) -> dict[str, object]:
        scope = self._required_active_scope()
        context_memory_ids = _context_memory_ids(context)
        now = utc_now_iso()
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = self._get_runtime_cycle_by_turn(
                connection,
                scope=scope,
                session_id=session_id,
                turn_id=turn_id,
            )
            if existing is not None:
                if (
                    existing["user_content_hash"] != user_content_hash
                    or existing["query"] != query
                ):
                    raise ValueError(
                        "Turn idempotency conflict: input changed for turn %s" % turn_id
                    )
                if existing["status"] == "aborted":
                    raise ValueError(
                        "Turn cycle was already aborted: %s" % turn_id
                    )
                connection.rollback()
                return existing

            for memory_id in context_memory_ids:
                memory = self._get_memory_unit(connection, memory_id)
                if memory is None:
                    raise ValueError(
                        "Context contains memory outside the active scope: %s" % memory_id
                    )
                self._record_access_signal(
                    connection,
                    memory_id=memory_id,
                    signal=AccessSignal.INJECTED,
                    strength=0.45,
                    metadata={
                        "source": "prepare_turn",
                        "cycle_id": cycle_id,
                        "query": query,
                        "session_id": session_id,
                    },
                )

            connection.execute(
                """
                INSERT INTO runtime_cycles(
                  id, tenant_id, workspace_id, user_id, agent_id,
                  session_id, turn_id, user_memory_id, user_content_hash,
                  query, context_json, context_memory_ids_json, status,
                  metadata_json, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'prepared', ?, ?, ?)
                """,
                (
                    cycle_id,
                    *scope.key_parts(),
                    session_id,
                    turn_id,
                    user_memory_id,
                    user_content_hash,
                    query,
                    context.model_dump_json(),
                    json.dumps(context_memory_ids, ensure_ascii=False),
                    json.dumps(metadata or {}, ensure_ascii=False, sort_keys=True),
                    now,
                    now,
                ),
            )
            connection.commit()
        cycle = self.get_runtime_cycle(cycle_id)
        if cycle is None:
            raise RuntimeError("Failed to persist runtime cycle: %s" % cycle_id)
        return cycle

    def get_runtime_cycle(self, cycle_id: str) -> dict[str, object] | None:
        scope = self._required_active_scope()
        with self.connect() as connection:
            row = connection.execute(
                """
                SELECT * FROM runtime_cycles
                WHERE id = ?
                  AND tenant_id = ? AND workspace_id = ?
                  AND user_id = ? AND agent_id = ?
                """,
                (cycle_id, *scope.key_parts()),
            ).fetchone()
            return self._runtime_cycle_row(row) if row is not None else None

    def commit_runtime_cycle(
        self,
        cycle_id: str,
        assistant_memory: MemoryUnit,
        cited_memory_ids: Sequence[str],
        metadata: dict[str, object] | None = None,
    ) -> dict[str, object]:
        scope = self._required_active_scope()
        self._require_scope(assistant_memory.scope)
        cited_ids = list(dict.fromkeys(cited_memory_ids))
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT * FROM runtime_cycles
                WHERE id = ?
                  AND tenant_id = ? AND workspace_id = ?
                  AND user_id = ? AND agent_id = ?
                """,
                (cycle_id, *scope.key_parts()),
            ).fetchone()
            if row is None:
                raise ValueError("Unknown runtime cycle: %s" % cycle_id)

            existing = self._runtime_cycle_row(row)
            if existing["status"] == "committed":
                if (
                    existing["assistant_content_hash"] != assistant_memory.content_hash
                    or existing["cited_memory_ids"] != cited_ids
                ):
                    raise ValueError(
                        "Turn commit idempotency conflict for cycle %s" % cycle_id
                    )
                connection.rollback()
                return existing
            if existing["status"] != "prepared":
                raise ValueError(
                    "Cannot commit runtime cycle %s with status %s"
                    % (cycle_id, existing["status"])
                )

            allowed_ids = set(_string_list(existing["context_memory_ids"]))
            invalid_ids = [memory_id for memory_id in cited_ids if memory_id not in allowed_ids]
            if invalid_ids:
                raise ValueError(
                    "Cited memories were not present in the prepared context: %s"
                    % ", ".join(invalid_ids)
                )

            self._put_memory_unit(connection, assistant_memory)
            for memory_id in cited_ids:
                if self._get_memory_unit(connection, memory_id) is None:
                    raise ValueError(
                        "Cited memory is outside the active scope: %s" % memory_id
                    )
                self._record_access_signal(
                    connection,
                    memory_id=memory_id,
                    signal=AccessSignal.CITED_BY_AGENT,
                    strength=1.0,
                    metadata={
                        "source": "commit_turn",
                        "cycle_id": cycle_id,
                        "assistant_memory_id": assistant_memory.id,
                    },
                )

            merged_metadata = _object_dict(existing["metadata"])
            merged_metadata.update(metadata or {})
            connection.execute(
                """
                UPDATE runtime_cycles
                SET status = 'committed',
                    assistant_memory_id = ?,
                    assistant_content_hash = ?,
                    cited_memory_ids_json = ?,
                    metadata_json = ?,
                    updated_at = ?
                WHERE id = ?
                """,
                (
                    assistant_memory.id,
                    assistant_memory.content_hash,
                    json.dumps(cited_ids, ensure_ascii=False),
                    json.dumps(merged_metadata, ensure_ascii=False, sort_keys=True),
                    utc_now_iso(),
                    cycle_id,
                ),
            )
            connection.commit()
        committed = self.get_runtime_cycle(cycle_id)
        if committed is None:
            raise RuntimeError("Failed to load committed runtime cycle: %s" % cycle_id)
        return committed

    def abort_runtime_cycle(
        self,
        cycle_id: str,
        reason: str,
        metadata: dict[str, object] | None = None,
    ) -> dict[str, object]:
        scope = self._required_active_scope()
        normalized_reason = reason.strip()
        if not normalized_reason:
            raise ValueError("Runtime cycle abort reason must not be empty")
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT * FROM runtime_cycles
                WHERE id = ?
                  AND tenant_id = ? AND workspace_id = ?
                  AND user_id = ? AND agent_id = ?
                """,
                (cycle_id, *scope.key_parts()),
            ).fetchone()
            if row is None:
                raise ValueError("Unknown runtime cycle: %s" % cycle_id)

            existing = self._runtime_cycle_row(row)
            existing_metadata = _object_dict(existing["metadata"])
            if existing["status"] == "aborted":
                if existing_metadata.get("abort_reason") != normalized_reason:
                    raise ValueError(
                        "Turn abort idempotency conflict for cycle %s" % cycle_id
                    )
                connection.rollback()
                return existing
            if existing["status"] != "prepared":
                raise ValueError(
                    "Cannot abort runtime cycle %s with status %s"
                    % (cycle_id, existing["status"])
                )

            existing_metadata.update(metadata or {})
            existing_metadata["abort_reason"] = normalized_reason
            connection.execute(
                """
                UPDATE runtime_cycles
                SET status = 'aborted',
                    metadata_json = ?,
                    updated_at = ?
                WHERE id = ?
                """,
                (
                    json.dumps(
                        existing_metadata,
                        ensure_ascii=False,
                        sort_keys=True,
                    ),
                    utc_now_iso(),
                    cycle_id,
                ),
            )
            connection.commit()
        aborted = self.get_runtime_cycle(cycle_id)
        if aborted is None:
            raise RuntimeError("Failed to load aborted runtime cycle: %s" % cycle_id)
        return aborted

    def enqueue_job(
        self,
        job_type: str,
        dedupe_key: str,
        payload: dict[str, object],
        session_id: str | None = None,
        max_attempts: int = 5,
    ) -> str:
        scope = self._required_active_scope()
        now = utc_now_iso()
        job_id = stable_id("job", *scope.key_parts(), job_type, dedupe_key)
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO runtime_jobs(
                  id, tenant_id, workspace_id, user_id, agent_id, session_id,
                  job_type, dedupe_key, payload_json, status, attempts,
                  max_attempts, available_at, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', 0, ?, ?, ?, ?)
                ON CONFLICT(tenant_id, workspace_id, user_id, agent_id, job_type, dedupe_key)
                DO NOTHING
                """,
                (
                    job_id,
                    *scope.key_parts(),
                    session_id,
                    job_type,
                    dedupe_key,
                    json.dumps(payload, ensure_ascii=False, sort_keys=True),
                    max(1, max_attempts),
                    now,
                    now,
                    now,
                ),
            )
            connection.commit()
        return job_id

    def claim_job(
        self,
        worker_id: str,
        lease_seconds: int = 120,
    ) -> dict[str, object] | None:
        now = utc_now_iso()
        lease_until = _future_iso(max(1, lease_seconds))
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT * FROM runtime_jobs
                WHERE attempts < max_attempts
                  AND available_at <= ?
                  AND (
                    status = 'pending'
                    OR (status = 'running' AND lease_until IS NOT NULL AND lease_until <= ?)
                  )
                ORDER BY created_at ASC
                LIMIT 1
                """,
                (now, now),
            ).fetchone()
            if row is None:
                connection.rollback()
                return None
            connection.execute(
                """
                UPDATE runtime_jobs
                SET status = 'running',
                    attempts = attempts + 1,
                    lease_owner = ?,
                    lease_until = ?,
                    updated_at = ?
                WHERE id = ?
                """,
                (worker_id, lease_until, now, row["id"]),
            )
            connection.commit()
        return self.get_job(str(row["id"]))

    def get_job(self, job_id: str) -> dict[str, object] | None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM runtime_jobs WHERE id = ?",
                (job_id,),
            ).fetchone()
            return self._job_row(row) if row is not None else None

    def complete_job(
        self,
        job_id: str,
        worker_id: str,
        result: dict[str, object],
    ) -> None:
        with self.connect() as connection:
            cursor = connection.execute(
                """
                UPDATE runtime_jobs
                SET status = 'completed', result_json = ?, last_error = NULL,
                    lease_owner = NULL, lease_until = NULL, updated_at = ?
                WHERE id = ? AND status = 'running' AND lease_owner = ?
                """,
                (
                    json.dumps(result, ensure_ascii=False, sort_keys=True),
                    utc_now_iso(),
                    job_id,
                    worker_id,
                ),
            )
            if cursor.rowcount != 1:
                raise RuntimeError("Worker does not own runtime job: %s" % job_id)
            connection.commit()

    def fail_job(
        self,
        job_id: str,
        worker_id: str,
        error_message: str,
        retry_delay_seconds: int = 30,
    ) -> None:
        with self.connect() as connection:
            row = connection.execute(
                """
                SELECT attempts, max_attempts FROM runtime_jobs
                WHERE id = ? AND status = 'running' AND lease_owner = ?
                """,
                (job_id, worker_id),
            ).fetchone()
            if row is None:
                raise RuntimeError("Worker does not own runtime job: %s" % job_id)
            terminal = int(row["attempts"]) >= int(row["max_attempts"])
            connection.execute(
                """
                UPDATE runtime_jobs
                SET status = ?, available_at = ?, last_error = ?,
                    lease_owner = NULL, lease_until = NULL, updated_at = ?
                WHERE id = ?
                """,
                (
                    "failed" if terminal else "pending",
                    _future_iso(max(0, retry_delay_seconds)),
                    error_message[:4000],
                    utc_now_iso(),
                    job_id,
                ),
            )
            connection.commit()

    def put_memory_embedding(
        self,
        memory_id: str,
        embedding_model: str,
        content_hash: str,
        vector: Sequence[float],
    ) -> None:
        if self.get_memory_unit(memory_id) is None:
            raise ValueError("Cannot index memory outside the active scope: %s" % memory_id)
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
            self._put_vector_index(
                connection,
                memory_id=memory_id,
                embedding_model=embedding_model,
                vector=vector,
            )
            connection.commit()

    def put_ccr_entry(
        self,
        marker: str,
        memory_id: str,
        content_hash: str,
        content_type: str,
        strategy: str,
        original_text: str,
        compressed_text: str,
        metadata: dict[str, object] | None = None,
    ) -> None:
        if self.get_memory_unit(memory_id) is None:
            raise ValueError("Cannot cache CCR content outside the active scope: %s" % memory_id)
        now = utc_now_iso()
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO ccr_entries(
                  marker, memory_unit_id, content_hash, content_type, strategy,
                  original_text, compressed_text, original_token_estimate,
                  compressed_token_estimate, metadata_json, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(marker) DO UPDATE SET
                  content_hash = excluded.content_hash,
                  content_type = excluded.content_type,
                  strategy = excluded.strategy,
                  original_text = excluded.original_text,
                  compressed_text = excluded.compressed_text,
                  original_token_estimate = excluded.original_token_estimate,
                  compressed_token_estimate = excluded.compressed_token_estimate,
                  metadata_json = excluded.metadata_json,
                  updated_at = excluded.updated_at
                """,
                (
                    marker,
                    memory_id,
                    content_hash,
                    content_type,
                    strategy,
                    original_text,
                    compressed_text,
                    _text_token_estimate(original_text),
                    _text_token_estimate(compressed_text),
                    json.dumps(metadata or {}, ensure_ascii=False, sort_keys=True),
                    now,
                    now,
                ),
            )
            connection.commit()

    def get_ccr_entry(self, marker: str) -> dict[str, object] | None:
        with self.connect() as connection:
            row = connection.execute(
                """
                SELECT ccr_entries.*
                FROM ccr_entries
                JOIN memory_units
                  ON memory_units.id = ccr_entries.memory_unit_id
                WHERE ccr_entries.marker = ?%s
                """
                % self._memory_scope_sql("memory_units"),
                (marker, *self._memory_scope_params()),
            ).fetchone()
            return _ccr_row(row) if row is not None else None

    def search_ccr(self, query: str, limit: int = 10) -> Sequence[dict[str, object]]:
        pattern = "%%%s%%" % query
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT ccr_entries.*
                FROM ccr_entries
                JOIN memory_units
                  ON memory_units.id = ccr_entries.memory_unit_id
                WHERE (
                  ccr_entries.original_text LIKE ?
                  OR ccr_entries.compressed_text LIKE ?
                )%s
                ORDER BY ccr_entries.updated_at DESC
                LIMIT ?
                """
                % self._memory_scope_sql("memory_units"),
                (
                    pattern,
                    pattern,
                    *self._memory_scope_params(),
                    limit,
                ),
            ).fetchall()
            return [_ccr_row(row) for row in rows]

    def get_memory_embedding(
        self,
        memory_id: str,
        embedding_model: str,
    ) -> tuple[str, list[float]] | None:
        if self.get_memory_unit(memory_id) is None:
            return None
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

    def get_user_workspace_embedding(
        self,
        memory_id: str,
        embedding_model: str,
    ) -> tuple[str, list[float]] | None:
        scope = self._required_active_scope()
        with self.connect() as connection:
            row = connection.execute(
                """
                SELECT memory_embeddings.content_hash, memory_embeddings.vector_json
                FROM memory_embeddings
                JOIN memory_units
                  ON memory_units.id = memory_embeddings.memory_unit_id
                WHERE memory_embeddings.memory_unit_id = ?
                  AND memory_embeddings.embedding_model = ?
                  AND memory_units.tenant_id = ?
                  AND memory_units.workspace_id = ?
                  AND memory_units.user_id = ?
                """,
                (
                    memory_id,
                    embedding_model,
                    scope.tenant_id,
                    scope.workspace_id,
                    scope.user_id,
                ),
            ).fetchone()
            if row is None:
                return None
            return row["content_hash"], [
                float(value) for value in json.loads(row["vector_json"])
            ]

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

    def _put_vector_index(
        self,
        connection: sqlite3.Connection,
        memory_id: str,
        embedding_model: str,
        vector: Sequence[float],
    ) -> None:
        registry = self._ensure_vector_index(
            connection,
            embedding_model,
            create_dimension=len(vector),
        )
        if registry is None:
            raise RuntimeError("Failed to initialize sqlite-vec index")
        table_name, dimension = registry
        if len(vector) != dimension:
            raise ValueError(
                "Embedding dimension changed for model %s: expected %s, got %s"
                % (embedding_model, dimension, len(vector))
            )
        connection.execute(
            'DELETE FROM "%s" WHERE memory_id = ?' % table_name,
            (memory_id,),
        )
        connection.execute(
            'INSERT INTO "%s"(memory_id, embedding) VALUES (?, ?)' % table_name,
            (memory_id, sqlite_vec.serialize_float32(list(vector))),
        )

    def _ensure_vector_index(
        self,
        connection: sqlite3.Connection,
        embedding_model: str,
        create_dimension: int | None = None,
    ) -> tuple[str, int] | None:
        row = connection.execute(
            """
            SELECT table_name, dimension FROM vector_index_registry
            WHERE embedding_model = ?
            """,
            (embedding_model,),
        ).fetchone()
        if row is not None:
            return str(row["table_name"]), int(row["dimension"])

        rows = connection.execute(
            """
            SELECT memory_unit_id, dimension, vector_json
            FROM memory_embeddings
            WHERE embedding_model = ?
            ORDER BY memory_unit_id
            """,
            (embedding_model,),
        ).fetchall()
        dimension = create_dimension or (
            int(rows[0]["dimension"]) if rows else None
        )
        if dimension is None:
            return None
        table_name = stable_id("vec_index", embedding_model)
        connection.execute(
            """
            CREATE VIRTUAL TABLE IF NOT EXISTS "%s"
            USING vec0(
              memory_id TEXT PRIMARY KEY,
              embedding FLOAT[%s] distance_metric=cosine
            )
            """
            % (table_name, dimension)
        )
        now = utc_now_iso()
        connection.execute(
            """
            INSERT INTO vector_index_registry(
              embedding_model, table_name, dimension, backend,
              created_at, updated_at
            )
            VALUES (?, ?, ?, 'sqlite-vec', ?, ?)
            """,
            (embedding_model, table_name, dimension, now, now),
        )
        for embedding_row in rows:
            if int(embedding_row["dimension"]) != dimension:
                raise ValueError(
                    "Inconsistent cached embedding dimension for model %s"
                    % embedding_model
                )
            vector = [
                float(value)
                for value in json.loads(embedding_row["vector_json"])
            ]
            connection.execute(
                'INSERT INTO "%s"(memory_id, embedding) VALUES (?, ?)'
                % table_name,
                (
                    embedding_row["memory_unit_id"],
                    sqlite_vec.serialize_float32(vector),
                ),
            )
        return table_name, dimension

    def search_embeddings(
        self,
        query_vector: Sequence[float],
        embedding_model: str,
        limit: int = 10,
        layers: Sequence[MemoryLayer] | None = None,
        session_id: str | None = None,
        statuses: Sequence[MemoryStatus] | None = None,
        cross_session_layers: Sequence[MemoryLayer] = (),
    ) -> Sequence[tuple[MemoryUnit, float]]:
        if not query_vector or _vector_norm(query_vector) == 0:
            return []
        with self.connect() as connection:
            registry = self._ensure_vector_index(connection, embedding_model)
            if registry is None:
                return []
            table_name, dimension = registry
            if len(query_vector) != dimension:
                raise ValueError(
                    "Embedding dimension mismatch for model %s: expected %s, got %s"
                    % (embedding_model, dimension, len(query_vector))
                )
            candidate_limit = max(limit * 20, 100)
            rows = connection.execute(
                """
                SELECT memory_id, distance
                FROM "%s"
                WHERE embedding MATCH ? AND k = ?
                ORDER BY distance
                """
                % table_name,
                (
                    sqlite_vec.serialize_float32(list(query_vector)),
                    candidate_limit,
                ),
            ).fetchall()
            scored: list[tuple[MemoryUnit, float]] = []
            for row in rows:
                memory = self._get_memory_unit(connection, str(row["memory_id"]))
                if memory is None:
                    continue
                if layers and memory.layer not in layers:
                    continue
                if statuses:
                    if memory.status not in statuses:
                        continue
                elif memory.status in {MemoryStatus.DELETED, MemoryStatus.TOMBSTONED}:
                    continue
                if not memory_matches_recall_session(
                    memory,
                    session_id,
                    cross_session_layers,
                ):
                    continue
                score = max(0.0, 1.0 - float(row["distance"]))
                scored.append((memory, score))
                if len(scored) >= limit:
                    break
            return scored

    def search_fts(
        self,
        query: str,
        limit: int = 10,
        layers: Sequence[MemoryLayer] | None = None,
        session_id: str | None = None,
        statuses: Sequence[MemoryStatus] | None = None,
        cross_session_layers: Sequence[MemoryLayer] = (),
    ) -> Sequence[MemoryUnit]:
        with self.connect() as connection:
            rows = self._search_fts_rows(
                connection,
                query,
                limit,
                layers,
                session_id,
                statuses,
                cross_session_layers,
            )
            units: list[MemoryUnit] = []
            for row in rows:
                memory = self.get_memory_unit(row["memory_id"])
                if memory is not None:
                    units.append(memory)
            return units

    def search_fts_trigram(
        self,
        query: str,
        limit: int = 10,
        layers: Sequence[MemoryLayer] | None = None,
        session_id: str | None = None,
        statuses: Sequence[MemoryStatus] | None = None,
        cross_session_layers: Sequence[MemoryLayer] = (),
    ) -> Sequence[MemoryUnit]:
        with self.connect() as connection:
            rows = self._search_fts_rows(
                connection,
                query,
                limit,
                layers,
                session_id,
                statuses,
                cross_session_layers,
                table="memory_units_fts_trigram",
            )
            units: list[MemoryUnit] = []
            for row in rows:
                memory = self.get_memory_unit(row["memory_id"])
                if memory is not None:
                    units.append(memory)
            return units

    def search_like(
        self,
        query: str,
        limit: int = 10,
        layers: Sequence[MemoryLayer] | None = None,
        session_id: str | None = None,
        statuses: Sequence[MemoryStatus] | None = None,
        cross_session_layers: Sequence[MemoryLayer] = (),
    ) -> Sequence[MemoryUnit]:
        clauses = ["(content LIKE ? OR summary LIKE ?)"]
        params: list[str] = ["%%%s%%" % query, "%%%s%%" % query]
        if self.scope is not None:
            clauses.append(self._memory_scope_predicate())
            params.extend(self._memory_scope_params())
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
        session_filter, session_params = _recall_session_filter(
            session_id,
            cross_session_layers,
        )
        if session_filter is not None:
            clauses.append(session_filter)
            params.extend(session_params)
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

    def find_l2_duplicate(self, atom: L2Atom) -> MemoryUnit | None:
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
    ) -> MemoryUnit | None:
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
        note: str | None = None,
    ) -> MemoryUnit | None:
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

    def put_graph_extraction(
        self,
        extraction: GraphExtraction,
        model: str,
    ) -> dict[str, object]:
        scope = self._required_active_scope()
        for memory_id in extraction.evidence_memory_ids:
            if self.get_memory_unit(memory_id) is None:
                raise ValueError(
                    "Graph evidence is outside the active scope: %s" % memory_id
                )
        local_to_node: dict[str, str] = {}
        node_ids: list[str] = []
        edge_ids: list[str] = []
        now = utc_now_iso()
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            for node in extraction.nodes:
                if (
                    node.memory_unit_id is not None
                    and self._get_memory_unit(connection, node.memory_unit_id)
                    is None
                ):
                    raise ValueError(
                        "Graph node memory is outside active scope: %s"
                        % node.memory_unit_id
                    )
                node_id = stable_id(
                    "graph_node",
                    *scope.key_parts(),
                    node.node_type.value,
                    node.canonical_key,
                )
                connection.execute(
                    """
                    INSERT INTO graph_nodes(
                      id, tenant_id, workspace_id, user_id, agent_id,
                      node_type, canonical_key, name, memory_unit_id,
                      data_json, created_at, updated_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(id) DO UPDATE SET
                      name = excluded.name,
                      memory_unit_id = COALESCE(
                        excluded.memory_unit_id,
                        graph_nodes.memory_unit_id
                      ),
                      data_json = excluded.data_json,
                      updated_at = excluded.updated_at
                    """,
                    (
                        node_id,
                        *scope.key_parts(),
                        node.node_type.value,
                        node.canonical_key,
                        node.name,
                        node.memory_unit_id,
                        json.dumps(
                            node.attributes,
                            ensure_ascii=False,
                            sort_keys=True,
                        ),
                        now,
                        now,
                    ),
                )
                local_to_node[node.local_id] = node_id
                node_ids.append(node_id)
                if node.memory_unit_id is not None:
                    connection.execute(
                        """
                        INSERT INTO graph_node_evidence(
                          node_id, memory_unit_id, confidence, created_at
                        )
                        VALUES (?, ?, ?, ?)
                        ON CONFLICT(node_id, memory_unit_id) DO UPDATE SET
                          confidence = MAX(
                            graph_node_evidence.confidence,
                            excluded.confidence
                          )
                        """,
                        (
                            node_id,
                            node.memory_unit_id,
                            extraction.confidence,
                            now,
                        ),
                    )

            for edge in extraction.edges:
                source_id = local_to_node.get(edge.source_local_id)
                target_id = local_to_node.get(edge.target_local_id)
                if source_id is None or target_id is None:
                    raise ValueError("Graph edge references an unknown local node")
                edge_id = stable_id(
                    "graph_edge",
                    source_id,
                    target_id,
                    edge.edge_type.value,
                )
                connection.execute(
                    """
                    INSERT INTO graph_edges(
                      id, source_node_id, target_node_id, edge_type,
                      weight, confidence, metadata_json, created_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(id) DO UPDATE SET
                      weight = excluded.weight,
                      confidence = excluded.confidence,
                      metadata_json = excluded.metadata_json
                    """,
                    (
                        edge_id,
                        source_id,
                        target_id,
                        edge.edge_type.value,
                        edge.confidence,
                        edge.confidence,
                        json.dumps(
                            edge.attributes,
                            ensure_ascii=False,
                            sort_keys=True,
                        ),
                        now,
                    ),
                )
                edge_ids.append(edge_id)

            extraction_id = stable_id(
                "graph_extraction",
                *scope.key_parts(),
                model,
                *sorted(extraction.evidence_memory_ids),
            )
            connection.execute(
                """
                INSERT INTO graph_extractions(
                  id, tenant_id, workspace_id, user_id, agent_id,
                  source_memory_ids_json, model, confidence,
                  node_ids_json, edge_ids_json, metadata_json, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                  confidence = excluded.confidence,
                  node_ids_json = excluded.node_ids_json,
                  edge_ids_json = excluded.edge_ids_json,
                  metadata_json = excluded.metadata_json
                """,
                (
                    extraction_id,
                    *scope.key_parts(),
                    json.dumps(
                        extraction.evidence_memory_ids,
                        ensure_ascii=False,
                    ),
                    model,
                    extraction.confidence,
                    json.dumps(node_ids, ensure_ascii=False),
                    json.dumps(edge_ids, ensure_ascii=False),
                    json.dumps(
                        extraction.metadata,
                        ensure_ascii=False,
                        sort_keys=True,
                    ),
                    now,
                ),
            )
            connection.commit()
        return {
            "extraction_id": extraction_id,
            "node_ids": node_ids,
            "edge_ids": edge_ids,
        }

    def search_graph_nodes(
        self,
        query: str,
        limit: int = 16,
        node_types: Sequence[str] = (),
    ) -> Sequence[dict[str, object]]:
        scope = self._required_active_scope()
        if not query.strip() or limit <= 0:
            return []
        clauses = [
            "graph_nodes_fts MATCH ?",
            "graph_nodes.tenant_id = ?",
            "graph_nodes.workspace_id = ?",
            "graph_nodes.user_id = ?",
            "graph_nodes.agent_id = ?",
        ]
        params: list[object] = [
            _graph_fts_query(query),
            *scope.key_parts(),
        ]
        if node_types:
            clauses.append(
                "graph_nodes.node_type IN (%s)"
                % ", ".join("?" for _ in node_types)
            )
            params.extend(node_types)
        params.append(limit)
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT graph_nodes.*, bm25(graph_nodes_fts) AS match_rank
                FROM graph_nodes_fts
                JOIN graph_nodes ON graph_nodes.id = graph_nodes_fts.node_id
                WHERE %s
                ORDER BY match_rank ASC, graph_nodes.id ASC
                LIMIT ?
                """
                % " AND ".join(clauses),
                params,
            ).fetchall()
            evidence = _graph_evidence_map(
                connection,
                [str(row["id"]) for row in rows],
            )
            result: list[dict[str, object]] = []
            for row in rows:
                node_id = str(row["id"])
                node = _row_to_graph_node(row, evidence.get(node_id, ()))
                node["match_rank"] = float(row["match_rank"])
                result.append(node)
            return result

    def list_recent_graph_nodes(
        self,
        limit: int = 24,
    ) -> Sequence[dict[str, object]]:
        if limit <= 0:
            return []
        scope = self._required_active_scope()
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT *
                FROM graph_nodes
                WHERE tenant_id = ?
                  AND workspace_id = ?
                  AND user_id = ?
                  AND agent_id = ?
                ORDER BY updated_at DESC, id ASC
                LIMIT ?
                """,
                (*scope.key_parts(), limit),
            ).fetchall()
            evidence = _graph_evidence_map(
                connection,
                [str(row["id"]) for row in rows],
            )
            return [
                _row_to_graph_node(row, evidence.get(str(row["id"]), ()))
                for row in rows
            ]

    def get_graph_subgraph(
        self,
        seed_node_ids: Sequence[str],
        max_hops: int = 3,
        node_limit: int = 512,
    ) -> dict[str, object]:
        scope = self._required_active_scope()
        seeds = list(dict.fromkeys(str(node_id) for node_id in seed_node_ids))
        if not seeds or node_limit <= 0:
            return {"nodes": [], "edges": []}
        seed_placeholders = ", ".join("?" for _ in seeds)
        reachable_params: list[object] = [
            *seeds,
            *scope.key_parts(),
            max(0, max_hops),
            node_limit,
        ]
        with self.connect() as connection:
            reachable_rows = connection.execute(
                """
                WITH RECURSIVE reachable(node_id, depth) AS (
                  SELECT id, 0
                  FROM graph_nodes
                  WHERE id IN (%s)
                    AND tenant_id = ?
                    AND workspace_id = ?
                    AND user_id = ?
                    AND agent_id = ?
                  UNION
                  SELECT
                    CASE
                      WHEN graph_edges.source_node_id = reachable.node_id
                      THEN graph_edges.target_node_id
                      ELSE graph_edges.source_node_id
                    END,
                    reachable.depth + 1
                  FROM reachable
                  JOIN graph_edges
                    ON graph_edges.source_node_id = reachable.node_id
                    OR graph_edges.target_node_id = reachable.node_id
                  WHERE reachable.depth < ?
                )
                SELECT node_id, MIN(depth) AS depth
                FROM reachable
                GROUP BY node_id
                ORDER BY depth ASC, node_id ASC
                LIMIT ?
                """
                % seed_placeholders,
                reachable_params,
            ).fetchall()
            node_depths = {
                str(row["node_id"]): int(row["depth"])
                for row in reachable_rows
            }
            if not node_depths:
                return {"nodes": [], "edges": []}
            node_ids = list(node_depths)
            placeholders = ", ".join("?" for _ in node_ids)
            node_rows = connection.execute(
                """
                SELECT *
                FROM graph_nodes
                WHERE id IN (%s)
                  AND tenant_id = ?
                  AND workspace_id = ?
                  AND user_id = ?
                  AND agent_id = ?
                """
                % placeholders,
                (*node_ids, *scope.key_parts()),
            ).fetchall()
            evidence = _graph_evidence_map(connection, node_ids)
            nodes: list[dict[str, object]] = []
            for row in sorted(
                node_rows,
                key=lambda item: (node_depths[str(item["id"])], str(item["id"])),
            ):
                node_id = str(row["id"])
                node = _row_to_graph_node(row, evidence.get(node_id, ()))
                node["depth"] = node_depths[node_id]
                nodes.append(node)

            edge_rows = connection.execute(
                """
                SELECT
                  graph_edges.*,
                  source_node.memory_unit_id AS source_memory_id,
                  target_node.memory_unit_id AS target_memory_id
                FROM graph_edges
                JOIN graph_nodes AS source_node
                  ON source_node.id = graph_edges.source_node_id
                JOIN graph_nodes AS target_node
                  ON target_node.id = graph_edges.target_node_id
                WHERE graph_edges.source_node_id IN (%s)
                  AND graph_edges.target_node_id IN (%s)
                ORDER BY graph_edges.id ASC
                """
                % (placeholders, placeholders),
                (*node_ids, *node_ids),
            ).fetchall()
            return {
                "nodes": nodes,
                "edges": [_row_to_graph_edge(row) for row in edge_rows],
            }

    def put_memory_graph_edge(
        self,
        source_memory_id: str,
        target_memory_id: str,
        edge_type: str,
        weight: float,
        confidence: float,
        metadata: Mapping[str, object] | None = None,
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
                _object_dict(json.loads(existing_row["metadata_json"] or "{}"))
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

    def get_graph_edge(self, edge_id: str) -> dict[str, object] | None:
        scope_filter = ""
        params: list[str] = [edge_id]
        if self.scope is not None:
            scope_filter = (
                " AND source_node.tenant_id = ?"
                " AND source_node.workspace_id = ?"
                " AND source_node.user_id = ?"
                " AND source_node.agent_id = ?"
                " AND target_node.tenant_id = ?"
                " AND target_node.workspace_id = ?"
                " AND target_node.user_id = ?"
                " AND target_node.agent_id = ?"
            )
            params.extend(self.scope.key_parts())
            params.extend(self.scope.key_parts())
        with self.connect() as connection:
            sql = (
                """
                SELECT
                  graph_edges.*,
                  source_node.memory_unit_id AS source_memory_id,
                  target_node.memory_unit_id AS target_memory_id
                FROM graph_edges
                JOIN graph_nodes AS source_node ON source_node.id = graph_edges.source_node_id
                JOIN graph_nodes AS target_node ON target_node.id = graph_edges.target_node_id
                WHERE graph_edges.id = ?%s
                """
                % scope_filter
            )
            row = connection.execute(sql, params).fetchone()
            return _row_to_graph_edge(row) if row is not None else None

    def list_graph_edges(self, edge_type: str | None = None) -> Sequence[dict[str, object]]:
        clauses: list[str] = []
        params: list[str] = []
        if edge_type is not None:
            clauses.append("graph_edges.edge_type = ?")
            params.append(edge_type)
        if self.scope is not None:
            clauses.extend(
                (
                    "source_node.tenant_id = ?",
                    "source_node.workspace_id = ?",
                    "source_node.user_id = ?",
                    "source_node.agent_id = ?",
                    "target_node.tenant_id = ?",
                    "target_node.workspace_id = ?",
                    "target_node.user_id = ?",
                    "target_node.agent_id = ?",
                )
            )
            params.extend(self.scope.key_parts())
            params.extend(self.scope.key_parts())
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
    ) -> dict[str, object] | None:
        if self.get_graph_edge(edge_id) is None:
            return None
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
        metadata: Mapping[str, object] | None = None,
    ) -> None:
        with self.connect() as connection:
            if self._get_memory_unit(connection, memory_id) is None:
                raise ValueError(
                    "Cannot record feedback for memory outside the active scope: %s"
                    % memory_id
                )
            self._record_access_signal(
                connection,
                memory_id=memory_id,
                signal=signal,
                strength=strength,
                metadata=metadata,
            )
            connection.commit()

    def _record_access_signal(
        self,
        connection: sqlite3.Connection,
        memory_id: str,
        signal: AccessSignal,
        strength: float,
        metadata: Mapping[str, object] | None,
    ) -> None:
        useful = signal in {AccessSignal.CITED_BY_AGENT, AccessSignal.USER_CONFIRMED}
        accessed = signal != AccessSignal.USER_REJECTED
        now = utc_now_iso()
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
                now,
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
                (1 if useful else 0, now, now, memory_id),
            )

    def append_review_event(
        self,
        event_type: str,
        target_type: str,
        target_id: str,
        review_item_id: str | None = None,
        plan_id: str | None = None,
        status: str | None = None,
        metadata: Mapping[str, object] | None = None,
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
        review_item_id: str | None = None,
        plan_id: str | None = None,
        status: str | None = None,
        metadata: Mapping[str, object] | None = None,
    ) -> ReviewEvent:
        event = ReviewEvent(
            id=random_id("review_event"),
            event_type=event_type,
            target_type=target_type,
            target_id=target_id,
            review_item_id=review_item_id,
            plan_id=plan_id,
            status=status,
            metadata=dict(metadata or {}),
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
        target_type: str | None = None,
        target_id: str | None = None,
        event_type: str | None = None,
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
        metadata: dict[str, object]
        if row is not None:
            metadata = _object_dict(
                json.loads(row["metadata_json"] or "{}")
            )
        else:
            metadata = {
                "event_type_counts": {},
                "status_counts": {},
                "high_risk_apply_count": 0,
                "second_confirmation_required_count": 0,
            }
        event_type_counts = _count_dict(metadata.get("event_type_counts"))
        event_type_counts[event.event_type] = event_type_counts.get(event.event_type, 0) + 1
        status_key = event.status or "unknown"
        status_counts = _count_dict(metadata.get("status_counts"))
        status_counts[status_key] = status_counts.get(status_key, 0) + 1
        high_risk_apply_count = _int_value(
            metadata.get("high_risk_apply_count")
        )
        second_confirmation_required_count = _int_value(
            metadata.get("second_confirmation_required_count")
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
        layers: Sequence[MemoryLayer] | None = None,
        session_id: str | None = None,
        statuses: Sequence[MemoryStatus] | None = None,
        cross_session_layers: Sequence[MemoryLayer] = (),
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

            session_filter, session_params = _recall_session_filter(
                session_id,
                cross_session_layers,
                table="memory_units",
            )
            if session_filter is not None:
                clauses.append(session_filter)
                params.extend(session_params)
            if self.scope is not None:
                clauses.append(self._memory_scope_predicate("memory_units"))
                params.extend(self._memory_scope_params())

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
        memory = self._get_memory_unit(connection, memory_id)
        if memory is None:
            raise ValueError(
                "Cannot create graph node for memory outside active scope: %s"
                % memory_id
            )
        node_id = stable_id("graph_node", memory_id)
        now = utc_now_iso()
        connection.execute(
            """
            INSERT INTO graph_nodes(
              id, tenant_id, workspace_id, user_id, agent_id,
              node_type, canonical_key, name, memory_unit_id,
              data_json, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
              memory_unit_id = excluded.memory_unit_id,
              name = excluded.name,
              updated_at = excluded.updated_at
            """,
            (
                node_id,
                *memory.scope.key_parts(),
                "memory_unit",
                "memory:%s" % memory_id,
                memory.summary or memory.id,
                memory_id,
                "{}",
                now,
                now,
            ),
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

        refs: list[EvidenceRef] = []
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

    def _get_runtime_cycle_by_turn(
        self,
        connection: sqlite3.Connection,
        scope: MemoryScope,
        session_id: str,
        turn_id: str,
    ) -> dict[str, object] | None:
        row = connection.execute(
            """
            SELECT * FROM runtime_cycles
            WHERE tenant_id = ? AND workspace_id = ?
              AND user_id = ? AND agent_id = ?
              AND session_id = ? AND turn_id = ?
            """,
            (*scope.key_parts(), session_id, turn_id),
        ).fetchone()
        return self._runtime_cycle_row(row) if row is not None else None

    def _runtime_cycle_row(self, row: sqlite3.Row) -> dict[str, object]:
        return {
            "id": row["id"],
            "scope": MemoryScope(
                tenant_id=row["tenant_id"],
                workspace_id=row["workspace_id"],
                user_id=row["user_id"],
                agent_id=row["agent_id"],
            ),
            "session_id": row["session_id"],
            "turn_id": row["turn_id"],
            "user_memory_id": row["user_memory_id"],
            "user_content_hash": row["user_content_hash"],
            "query": row["query"],
            "context": ContextBundle.model_validate_json(row["context_json"]),
            "context_memory_ids": [
                str(value) for value in json.loads(row["context_memory_ids_json"] or "[]")
            ],
            "status": row["status"],
            "assistant_memory_id": row["assistant_memory_id"],
            "assistant_content_hash": row["assistant_content_hash"],
            "cited_memory_ids": [
                str(value) for value in json.loads(row["cited_memory_ids_json"] or "[]")
            ],
            "metadata": json.loads(row["metadata_json"] or "{}"),
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    def _job_row(self, row: sqlite3.Row) -> dict[str, object]:
        return {
            "id": row["id"],
            "scope": MemoryScope(
                tenant_id=row["tenant_id"],
                workspace_id=row["workspace_id"],
                user_id=row["user_id"],
                agent_id=row["agent_id"],
            ),
            "session_id": row["session_id"],
            "job_type": row["job_type"],
            "dedupe_key": row["dedupe_key"],
            "payload": json.loads(row["payload_json"] or "{}"),
            "status": row["status"],
            "attempts": row["attempts"],
            "max_attempts": row["max_attempts"],
            "available_at": row["available_at"],
            "lease_owner": row["lease_owner"],
            "lease_until": row["lease_until"],
            "last_error": row["last_error"],
            "result": json.loads(row["result_json"]) if row["result_json"] else None,
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    def _row_to_memory_unit(
        self,
        row: sqlite3.Row,
        evidence_refs: Sequence[EvidenceRef],
    ) -> MemoryUnit:
        metadata = json.loads(row["metadata_json"] or "{}")
        return MemoryUnit(
            id=row["id"],
            scope=MemoryScope(
                tenant_id=row["tenant_id"],
                workspace_id=row["workspace_id"],
                user_id=row["user_id"],
                agent_id=row["agent_id"],
            ),
            visibility=MemoryVisibility(row["visibility"]),
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

    def _scope_sql(self, table: str | None = None) -> str:
        if self.scope is None:
            return ""
        prefix = "%s." % table if table else ""
        return (
            " AND %stenant_id = ?"
            " AND %sworkspace_id = ?"
            " AND %suser_id = ?"
            " AND %sagent_id = ?"
        ) % (prefix, prefix, prefix, prefix)

    def _scope_params(self) -> tuple[str, ...]:
        return self.scope.key_parts() if self.scope is not None else ()

    def _memory_scope_sql(self, table: str | None = None) -> str:
        if self.scope is None:
            return ""
        return " AND %s" % self._memory_scope_predicate(table)

    def _memory_scope_predicate(self, table: str | None = None) -> str:
        prefix = "%s." % table if table else ""
        return (
            "((%stenant_id = ? AND %sworkspace_id = ? AND %suser_id = ? "
            "AND %sagent_id = ?) OR "
            "(%stenant_id = ? AND %sworkspace_id = ? AND %suser_id = ? "
            "AND %svisibility = 'user_workspace'))"
        ) % (prefix, prefix, prefix, prefix, prefix, prefix, prefix, prefix)

    def _memory_scope_params(self) -> tuple[str, ...]:
        if self.scope is None:
            return ()
        return (
            *self.scope.key_parts(),
            self.scope.tenant_id,
            self.scope.workspace_id,
            self.scope.user_id,
        )

    def _require_scope(self, scope: MemoryScope) -> None:
        if self.scope is not None and scope != self.scope:
            raise ValueError(
                "Memory scope mismatch: store=%s memory=%s"
                % (self.scope.model_dump(mode="json"), scope.model_dump(mode="json"))
            )

    def _require_memory_scope(self, memory: MemoryUnit) -> None:
        if self.scope is None:
            return
        if memory.visibility == MemoryVisibility.USER_WORKSPACE:
            same_user_workspace = (
                memory.scope.tenant_id == self.scope.tenant_id
                and memory.scope.workspace_id == self.scope.workspace_id
                and memory.scope.user_id == self.scope.user_id
            )
            if not same_user_workspace:
                raise ValueError(
                    "Shared memory is outside the active user workspace: %s"
                    % memory.id
                )
            return
        self._require_scope(memory.scope)

    def _required_active_scope(self) -> MemoryScope:
        if self.scope is None:
            raise RuntimeError("This operation requires an explicit memory scope")
        return self.scope


def _context_memory_ids(context: ContextBundle) -> list[str]:
    values: list[str] = []
    seen: set[str] = set()
    for item in context.items:
        for memory_id in item.source_memory_ids:
            if memory_id in seen:
                continue
            seen.add(memory_id)
            values.append(memory_id)
    return values


def _future_iso(seconds: int) -> str:
    return (
        datetime.now(UTC) + timedelta(seconds=seconds)
    ).isoformat(timespec="seconds")


def _text_token_estimate(text: str) -> int:
    return max(1, (len(text) + 3) // 4) if text else 0


def _ccr_row(row: sqlite3.Row) -> dict[str, object]:
    return {
        "marker": row["marker"],
        "memory_unit_id": row["memory_unit_id"],
        "content_hash": row["content_hash"],
        "content_type": row["content_type"],
        "strategy": row["strategy"],
        "original_text": row["original_text"],
        "compressed_text": row["compressed_text"],
        "original_token_estimate": row["original_token_estimate"],
        "compressed_token_estimate": row["compressed_token_estimate"],
        "metadata": json.loads(row["metadata_json"] or "{}"),
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }
