"""Evidence-driven L4 persona observation and activation."""

from __future__ import annotations

import json
from collections.abc import Sequence

from altm.contracts import (
    LifecycleMeta,
    LifecycleState,
    MemoryLayer,
    MemoryStatus,
    MemoryUnit,
    PersonaFacet,
    PersonaStatus,
)
from altm.storage import SQLiteMemoryStore
from altm.utils import utc_now_iso


class PersonaLifecycleManager:
    def __init__(
        self,
        store: SQLiteMemoryStore,
        min_observation_cycles: int = 3,
        min_useful_accesses: int = 2,
        min_source_sessions: int = 2,
    ) -> None:
        if store.scope is None:
            raise RuntimeError("Persona lifecycle requires an explicit memory scope")
        self.store = store
        self.min_observation_cycles = max(1, min_observation_cycles)
        self.min_useful_accesses = max(0, min_useful_accesses)
        self.min_source_sessions = max(2, min_source_sessions)

    def advance(self, limit: int = 100) -> Sequence[MemoryUnit]:
        facets = self._observing_facets(limit)
        workspace_l2 = {
            memory.id: memory
            for memory in self.store.list_user_workspace_memories(
                layer=MemoryLayer.L2,
                limit=5000,
            )
        }
        activated: list[MemoryUnit] = []
        for facet in facets:
            memory = self.store.get_memory_unit(facet.id)
            if memory is None:
                continue
            sessions = {
                str(source.metadata.get("session_id"))
                for memory_id in facet.source_memory_ids
                if (source := workspace_l2.get(memory_id)) is not None
                and source.metadata.get("session_id")
            }
            ready = (
                facet.observation_cycles >= self.min_observation_cycles
                and len(sessions) >= self.min_source_sessions
                and memory.useful_access_count >= self.min_useful_accesses
                and not facet.counter_evidence_memory_ids
                and facet.confidence >= 0.80
                and facet.stability_score >= 0.80
            )
            if not ready:
                continue
            now = utc_now_iso()
            active_facet = facet.model_copy(
                update={
                    "status": PersonaStatus.ACTIVE,
                    "last_observed_at": now,
                    "metadata": {
                        **facet.metadata,
                        "activated_at": now,
                        "activation_policy": "evidence_observation_v1",
                    },
                }
            )
            active_memory = memory.model_copy(
                update={
                    "status": MemoryStatus.ACTIVE,
                    "lifecycle_state": LifecycleState.LONG,
                    "updated_at": now,
                    "lifecycle": LifecycleMeta(
                        age=memory.lifecycle.age,
                        protection_tier=max(4, memory.lifecycle.protection_tier),
                        compression_tier=memory.lifecycle.compression_tier,
                        observation_until=None,
                        promotion_candidate_since=None,
                        demotion_candidate_since=None,
                    ),
                    "metadata": {
                        **memory.metadata,
                        "persona_status": PersonaStatus.ACTIVE.value,
                        "activated_at": now,
                    },
                }
            )
            self.store.put_l4_persona(active_facet, active_memory)
            self.store.append_review_event(
                event_type="autonomous_l4_activated",
                target_type="memory_unit",
                target_id=memory.id,
                status="active",
                metadata={
                    "observation_cycles": facet.observation_cycles,
                    "useful_access_count": memory.useful_access_count,
                    "source_session_count": len(sessions),
                },
            )
            activated.append(active_memory)
        return activated

    def _observing_facets(self, limit: int) -> list[PersonaFacet]:
        scope = self.store.scope
        if scope is None:
            return []
        with self.store.connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM l4_persona_facets
                WHERE tenant_id = ? AND workspace_id = ? AND user_id = ?
                  AND status = 'observing'
                ORDER BY updated_at ASC
                LIMIT ?
                """,
                (
                    scope.tenant_id,
                    scope.workspace_id,
                    scope.user_id,
                    limit,
                ),
            ).fetchall()
        return [
            PersonaFacet(
                id=row["id"],
                facet_key=row["facet_key"],
                facet_type=row["facet_type"],
                statement=row["statement"],
                workspace_scope=row["workspace_scope"],
                confidence=row["confidence"],
                stability_score=row["stability_score"],
                status=PersonaStatus(row["status"]),
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
            for row in rows
        ]
