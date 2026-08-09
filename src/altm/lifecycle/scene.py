"""Evidence-driven L3 scene observation and activation."""

from __future__ import annotations

import json
from collections.abc import Sequence

from altm.contracts import (
    LifecycleMeta,
    LifecycleState,
    MemoryStatus,
    MemoryUnit,
    SceneBlock,
    SceneType,
)
from altm.storage import SQLiteMemoryStore
from altm.utils import utc_now_iso


class SceneLifecycleManager:
    def __init__(
        self,
        store: SQLiteMemoryStore,
        min_observation_cycles: int = 2,
        min_useful_accesses: int = 1,
    ) -> None:
        if store.scope is None:
            raise RuntimeError("Scene lifecycle requires an explicit memory scope")
        self.store = store
        self.min_observation_cycles = max(1, min_observation_cycles)
        self.min_useful_accesses = max(0, min_useful_accesses)

    def advance(self, limit: int = 100) -> Sequence[MemoryUnit]:
        scenes = self._observing_scenes(limit)
        activated: list[MemoryUnit] = []
        for scene in scenes:
            memory = self.store.get_memory_unit(scene.id)
            if memory is None:
                continue
            ready = (
                scene.observation_cycles >= self.min_observation_cycles
                and scene.confidence >= 0.80
                and scene.boundary_risk <= 0.20
                and memory.useful_access_count >= self.min_useful_accesses
            )
            if not ready:
                continue
            now = utc_now_iso()
            active_scene = scene.model_copy(
                update={
                    "metadata": {
                        **scene.metadata,
                        "activated_at": now,
                        "activation_policy": "scene_observation_v1",
                    }
                }
            )
            active_memory = memory.model_copy(
                update={
                    "status": MemoryStatus.ACTIVE,
                    "lifecycle_state": LifecycleState.LONG,
                    "updated_at": now,
                    "lifecycle": LifecycleMeta(
                        age=memory.lifecycle.age,
                        protection_tier=max(3, memory.lifecycle.protection_tier),
                        compression_tier=memory.lifecycle.compression_tier,
                        observation_until=None,
                        promotion_candidate_since=None,
                        demotion_candidate_since=None,
                    ),
                    "metadata": {
                        **memory.metadata,
                        "scene_status": "active",
                        "activated_at": now,
                    },
                }
            )
            self.store.put_l3_scene(active_scene, active_memory)
            self.store.append_review_event(
                event_type="autonomous_l3_activated",
                target_type="memory_unit",
                target_id=memory.id,
                status="active",
                metadata={
                    "observation_cycles": scene.observation_cycles,
                    "useful_access_count": memory.useful_access_count,
                    "confidence": scene.confidence,
                    "boundary_risk": scene.boundary_risk,
                },
            )
            activated.append(active_memory)
        return activated

    def _observing_scenes(self, limit: int) -> list[SceneBlock]:
        scope = self.store.scope
        if scope is None:
            return []
        with self.store.connect() as connection:
            rows = connection.execute(
                """
                SELECT l3_scenes.*
                FROM l3_scenes
                JOIN memory_units
                  ON memory_units.id = l3_scenes.memory_unit_id
                WHERE memory_units.tenant_id = ?
                  AND memory_units.workspace_id = ?
                  AND memory_units.user_id = ?
                  AND memory_units.agent_id = ?
                  AND memory_units.status = 'observing'
                ORDER BY l3_scenes.updated_at ASC
                LIMIT ?
                """,
                (*scope.key_parts(), limit),
            ).fetchall()
        return [
            SceneBlock(
                id=row["id"],
                title=row["title"],
                scene_type=SceneType(row["scene_type"]),
                summary=row["summary"],
                active_facts=json.loads(row["active_facts_json"] or "[]"),
                historical_facts=json.loads(
                    row["historical_facts_json"] or "[]"
                ),
                open_questions=json.loads(row["open_questions_json"] or "[]"),
                known_risks=json.loads(row["known_risks_json"] or "[]"),
                source_memory_ids=json.loads(
                    row["source_memory_ids_json"] or "[]"
                ),
                source_session_ids=json.loads(
                    row["source_session_ids_json"] or "[]"
                ),
                confidence=row["confidence"],
                boundary_risk=row["boundary_risk"],
                observation_cycles=row["observation_cycles"],
                metadata=json.loads(row["metadata_json"] or "{}"),
            )
            for row in rows
        ]
