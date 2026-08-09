"""Audited tombstone and physical deletion with evidence fallback repair."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Sequence
from typing import cast

from altm.contracts import MemoryUnit
from altm.storage import SQLiteMemoryStore
from altm.utils import random_id, utc_now_iso


class RetentionManager:
    def __init__(self, store: SQLiteMemoryStore) -> None:
        if store.scope is None:
            raise RuntimeError("Retention operations require an explicit memory scope")
        self.store = store

    def delete_memory(
        self,
        memory_id: str,
        reason: str,
        physical: bool,
    ) -> dict[str, object]:
        memory = self.store.get_memory_unit(memory_id)
        if memory is None:
            raise ValueError("Memory does not exist in the active scope: %s" % memory_id)
        request_id = random_id("deletion")
        mode = "physical" if physical else "tombstone"
        now = utc_now_iso()
        scope = self.store.scope
        if scope is None:
            raise RuntimeError("Retention operations require an explicit memory scope")
        with self.store.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                INSERT INTO deletion_requests(
                  id, tenant_id, workspace_id, user_id, agent_id,
                  target_type, target_id, mode, reason, status,
                  metadata_json, created_at
                )
                VALUES (?, ?, ?, ?, ?, 'memory_unit', ?, ?, ?, 'requested', ?, ?)
                """,
                (
                    request_id,
                    *scope.key_parts(),
                    memory_id,
                    mode,
                    reason,
                    json.dumps(
                        {
                            "layer": memory.layer.value,
                            "content_hash": memory.content_hash,
                        },
                        sort_keys=True,
                    ),
                    now,
                ),
            )
            repaired_ids = self._repair_evidence_refs(connection, memory)
            if physical:
                connection.execute(
                    """
                    DELETE FROM runtime_cycles
                    WHERE user_memory_id = ? OR assistant_memory_id = ?
                    """,
                    (memory_id, memory_id),
                )
                connection.execute(
                    "DELETE FROM memory_units_fts WHERE memory_id = ?",
                    (memory_id,),
                )
                connection.execute(
                    "DELETE FROM memory_units_fts_trigram WHERE memory_id = ?",
                    (memory_id,),
                )
                connection.execute(
                    """
                    INSERT INTO tombstones(
                      id, target_type, target_id, reason, created_at, metadata_json
                    )
                    VALUES (?, 'memory_unit', ?, ?, ?, ?)
                    ON CONFLICT(target_type, target_id) DO UPDATE SET
                      reason = excluded.reason,
                      created_at = excluded.created_at,
                      metadata_json = excluded.metadata_json
                    """,
                    (
                        random_id("tombstone"),
                        memory_id,
                        reason,
                        now,
                        json.dumps(
                            {
                                "physical": True,
                                "layer": memory.layer.value,
                                "content_hash": memory.content_hash,
                            },
                            sort_keys=True,
                        ),
                    ),
                )
                connection.execute(
                    "DELETE FROM memory_units WHERE id = ?",
                    (memory_id,),
                )
            else:
                connection.execute(
                    """
                    INSERT INTO tombstones(
                      id, target_type, target_id, reason, created_at, metadata_json
                    )
                    VALUES (?, 'memory_unit', ?, ?, ?, '{}')
                    ON CONFLICT(target_type, target_id) DO UPDATE SET
                      reason = excluded.reason,
                      created_at = excluded.created_at
                    """,
                    (random_id("tombstone"), memory_id, reason, now),
                )
                connection.execute(
                    """
                    UPDATE memory_units
                    SET status = 'tombstoned', updated_at = ?
                    WHERE id = ?
                    """,
                    (now, memory_id),
                )
            connection.execute(
                """
                UPDATE deletion_requests
                SET status = 'applied', applied_at = ?,
                    metadata_json = ?
                WHERE id = ?
                """,
                (
                    now,
                    json.dumps(
                        {
                            "layer": memory.layer.value,
                            "content_hash": memory.content_hash,
                            "repaired_evidence_memory_ids": repaired_ids,
                        },
                        sort_keys=True,
                    ),
                    request_id,
                ),
            )
            connection.commit()
        return {
            "request_id": request_id,
            "memory_id": memory_id,
            "mode": mode,
            "status": "applied",
            "repaired_evidence_memory_ids": repaired_ids,
        }

    def delete_expired_l0(self, limit: int = 100) -> Sequence[dict[str, object]]:
        scope = self.store.scope
        if scope is None:
            return []
        now = utc_now_iso()
        with self.store.connect() as connection:
            rows = connection.execute(
                """
                SELECT id FROM memory_units
                WHERE tenant_id = ? AND workspace_id = ?
                  AND user_id = ? AND agent_id = ?
                  AND layer = 'L0'
                  AND status != 'deleted'
                  AND json_extract(metadata_json, '$.retention_until') IS NOT NULL
                  AND json_extract(metadata_json, '$.retention_until') <= ?
                ORDER BY created_at ASC
                LIMIT ?
                """,
                (*scope.key_parts(), now, limit),
            ).fetchall()
        return [
            self.delete_memory(
                memory_id=str(row["id"]),
                reason="configured_l0_ttl_expired",
                physical=True,
            )
            for row in rows
        ]

    def _repair_evidence_refs(
        self,
        connection: sqlite3.Connection,
        target: MemoryUnit,
    ) -> list[str]:
        rows = connection.execute(
            """
            SELECT DISTINCT memory_unit_id, fallback_locator_json
            FROM evidence_refs
            WHERE target_id = ?
            """,
            (target.id,),
        ).fetchall()
        repaired: list[str] = []
        now = utc_now_iso()
        for row in rows:
            memory_row = connection.execute(
                "SELECT metadata_json FROM memory_units WHERE id = ?",
                (row["memory_unit_id"],),
            ).fetchone()
            if memory_row is None:
                continue
            metadata = _json_object(str(memory_row["metadata_json"] or "{}"))
            repairs_value = metadata.get("evidence_repairs")
            repairs: list[object] = (
                list(cast(list[object], repairs_value))
                if isinstance(repairs_value, list)
                else []
            )
            fallback = _json_object(
                str(row["fallback_locator_json"] or "{}")
            )
            repairs.append(
                {
                    "deleted_target_id": target.id,
                    "deleted_target_layer": target.layer.value,
                    "fallback_available": bool(fallback),
                    "repaired_at": now,
                }
            )
            metadata["evidence_repairs"] = repairs
            connection.execute(
                """
                UPDATE memory_units
                SET metadata_json = ?, updated_at = ?
                WHERE id = ?
                """,
                (
                    json.dumps(metadata, ensure_ascii=False, sort_keys=True),
                    now,
                    row["memory_unit_id"],
                ),
            )
            repaired.append(str(row["memory_unit_id"]))
        return repaired


def _json_object(value: str) -> dict[str, object]:
    parsed: object = json.loads(value)
    if not isinstance(parsed, dict):
        return {}
    return {
        str(key): item
        for key, item in cast(dict[object, object], parsed).items()
    }
