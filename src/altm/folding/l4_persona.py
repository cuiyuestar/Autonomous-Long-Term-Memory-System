"""L4/persona candidate synthesis from reviewed L2 memories."""

from __future__ import annotations

import json
from collections import defaultdict
from collections.abc import Sequence

from altm.contracts import (
    EvidenceRef,
    EvidenceRelation,
    LifecycleState,
    MemoryLayer,
    MemoryStatus,
    MemoryUnit,
)
from altm.storage import SQLiteMemoryStore
from altm.utils import sha256_text, stable_id, utc_now_iso

PERSONA_ATOM_TYPES = {"preference", "constraint", "lesson"}


class L4PersonaCandidateBuilder:
    def __init__(self, store: SQLiteMemoryStore) -> None:
        self.store = store

    def build(
        self,
        min_support: int = 2,
        limit: int = 1000,
        dry_run: bool = False,
    ) -> Sequence[MemoryUnit]:
        groups: dict[str, list[MemoryUnit]] = defaultdict(list)
        for memory in self.store.list_memory_units(layer=MemoryLayer.L2, limit=limit):
            atom_type = str(memory.metadata.get("atom_type") or "")
            if not _is_persona_source(memory, atom_type):
                continue
            groups[atom_type].append(memory)

        candidates: list[MemoryUnit] = []
        for atom_type, memories in groups.items():
            if len(memories) < min_support:
                continue
            candidate = self._group_to_persona(atom_type, memories)
            candidates.append(candidate)
            if not dry_run:
                self.store.put_memory_unit(candidate)
        return candidates

    def _group_to_persona(
        self,
        atom_type: str,
        memories: Sequence[MemoryUnit],
    ) -> MemoryUnit:
        sorted_memories = sorted(memories, key=lambda memory: (memory.created_at, memory.id))
        source_ids = [memory.id for memory in sorted_memories]
        summaries = [memory.summary or memory.content for memory in sorted_memories]
        persona_key = "persona:%s" % atom_type
        persona_id = stable_id("l4_persona", persona_key, *source_ids)
        title = "L4 persona candidate for %s" % atom_type
        content_data = {
            "persona_key": persona_key,
            "title": title,
            "atom_type": atom_type,
            "source_memory_ids": source_ids,
            "summaries": summaries,
        }
        content = json.dumps(content_data, ensure_ascii=False, sort_keys=True)
        now = utc_now_iso()
        support_count = len(sorted_memories)
        confidence = min(0.95, 0.60 + 0.08 * support_count)
        return MemoryUnit(
            id=persona_id,
            scope=sorted_memories[0].scope,
            layer=MemoryLayer.L4,
            lifecycle_state=LifecycleState.LONG,
            status=MemoryStatus.OBSERVING,
            content=content,
            content_hash=sha256_text(content),
            summary="%s: %s" % (title, "；".join(summaries[:3])),
            created_at=now,
            updated_at=now,
            evidence_refs=[
                EvidenceRef(
                    target_id=memory.id,
                    target_layer=MemoryLayer.L2,
                    relation=EvidenceRelation.DERIVED_FROM,
                    confidence=confidence,
                )
                for memory in sorted_memories
            ],
            metadata={
                "persona_key": persona_key,
                "atom_type": atom_type,
                "candidate_status": "candidate",
                "governance_review_status": "pending",
                "source_memory_ids": source_ids,
                "support_count": support_count,
                "builder": "l4_persona_candidate_builder",
            },
        )


def _is_persona_source(memory: MemoryUnit, atom_type: str) -> bool:
    return (
        atom_type in PERSONA_ATOM_TYPES
        and memory.status not in {MemoryStatus.DELETED, MemoryStatus.TOMBSTONED}
        and memory.metadata.get("review_status") == "approved"
    )
