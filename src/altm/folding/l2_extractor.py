"""L2 atom extraction from L1 context capsules."""

from __future__ import annotations

import json
from collections.abc import Sequence
from typing import Any, cast

from altm.contracts import (
    EvidenceRef,
    EvidenceRelation,
    L2Atom,
    L2AtomType,
    LifecycleState,
    MemoryLayer,
    MemoryScope,
    MemoryStatus,
    MemoryUnit,
    ReviewStatus,
)
from altm.llm import OpenAICompatibleClient
from altm.storage import SQLiteMemoryStore
from altm.utils import sha256_text, stable_id, utc_now_iso

SYSTEM_PROMPT = """You extract L2 atomic memories from an L1 context capsule.

Return only a JSON object with this schema:
{
  "atoms": [
    {
      "atom_type": "preference|constraint|project_fact|decision|issue|resolution|task_state|temporal_fact|lesson",
      "text": "short verifiable memory",
      "subject": "optional subject",
      "predicate": "optional predicate",
      "object": "optional object",
      "scope": "optional project/task/user scope",
      "confidence": 0.0,
      "extraction_reason": "why this is reusable and grounded"
    }
  ]
}

Rules:
- Only extract reusable, verifiable facts supported by the capsule.
- Do not infer stable persona traits from a single temporary instruction.
- Prefer precise constraints, decisions, issues, resolutions, and task states.
- If evidence is weak, omit the atom.
- Use Chinese for text when source content is Chinese.
"""


class L2Extractor:
    def __init__(self, store: SQLiteMemoryStore, llm_client: OpenAICompatibleClient) -> None:
        self.store = store
        self.llm_client = llm_client

    def extract_from_session(self, session_id: str) -> Sequence[MemoryUnit]:
        scope = self.store.scope or MemoryScope()
        checkpoint_scope = stable_id(
            "checkpoint",
            "l2",
            *scope.key_parts(),
            session_id,
        )
        l1_units, next_cursor = self.store.list_unprocessed_session_memories(
            layer=MemoryLayer.L1,
            session_id=session_id,
            checkpoint_scope=checkpoint_scope,
            limit=100,
        )
        created: list[MemoryUnit] = []
        for l1_unit in l1_units:
            created.extend(self.extract_from_l1(l1_unit))
        if l1_units:
            self.store.put_checkpoint(
                checkpoint_scope,
                str(next_cursor),
                metadata={
                    "stage": "l2",
                    "session_id": session_id,
                    "source_count": len(l1_units),
                    "created_count": len(created),
                },
            )
        return created

    def extract_from_l1(self, l1_unit: MemoryUnit) -> Sequence[MemoryUnit]:
        if l1_unit.layer != MemoryLayer.L1:
            raise ValueError("L2 extraction requires an L1 MemoryUnit")

        response = self.llm_client.chat_json(
            [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": l1_unit.content},
            ]
        )
        atoms = self._parse_atoms(response, l1_unit)
        created: list[MemoryUnit] = []
        for atom in atoms:
            if self.store.find_l2_duplicate(atom) is not None:
                continue
            memory = self._atom_to_memory_unit(atom, l1_unit)
            self.store.put_l2_atom(atom, memory)
            created.append(memory)
        return created

    def _parse_atoms(self, response: dict[str, Any], l1_unit: MemoryUnit) -> Sequence[L2Atom]:
        raw_atoms_value: object = response.get("atoms", [])
        if not isinstance(raw_atoms_value, list):
            raise ValueError("LLM response field `atoms` must be a list")
        raw_atoms = cast(list[object], raw_atoms_value)

        parsed: list[L2Atom] = []
        for raw_atom_value in raw_atoms:
            if not isinstance(raw_atom_value, dict):
                raise ValueError("Each L2 atom must be an object")
            raw_atom = {
                str(key): value
                for key, value in cast(
                    dict[object, object],
                    raw_atom_value,
                ).items()
            }
            atom_type = L2AtomType(str(raw_atom["atom_type"]))
            text = str(raw_atom["text"]).strip()
            atom_id = stable_id("l2", l1_unit.id, atom_type.value, text)
            parsed.append(
                L2Atom(
                    id=atom_id,
                    atom_type=atom_type,
                    text=text,
                    subject=_optional_text(raw_atom.get("subject")),
                    predicate=_optional_text(raw_atom.get("predicate")),
                    object=_optional_text(raw_atom.get("object")),
                    scope=_optional_text(raw_atom.get("scope")),
                    confidence=_confidence(raw_atom.get("confidence")),
                    extraction_reason=str(raw_atom["extraction_reason"]),
                    source_memory_id=l1_unit.id,
                    review_status=ReviewStatus.PENDING,
                    metadata={
                        "source_layer": l1_unit.layer.value,
                        "source_session_id": l1_unit.metadata.get("session_id"),
                    },
                )
            )
        return parsed

    def _atom_to_memory_unit(self, atom: L2Atom, l1_unit: MemoryUnit) -> MemoryUnit:
        now = utc_now_iso()
        content = json.dumps(atom.model_dump(mode="json"), ensure_ascii=False, sort_keys=True)
        return MemoryUnit(
            id=atom.id,
            scope=l1_unit.scope,
            layer=MemoryLayer.L2,
            lifecycle_state=LifecycleState.SHORT,
            status=MemoryStatus.ACTIVE,
            content=content,
            content_hash=sha256_text(content),
            summary=atom.text,
            created_at=now,
            updated_at=now,
            evidence_refs=[
                EvidenceRef(
                    target_id=l1_unit.id,
                    target_layer=MemoryLayer.L1,
                    relation=EvidenceRelation.DERIVED_FROM,
                    confidence=atom.confidence,
                    fallback_locator=(
                        l1_unit.evidence_refs[0].fallback_locator
                        if l1_unit.evidence_refs
                        else None
                    ),
                )
            ],
            metadata={
                "atom_type": atom.atom_type.value,
                "review_status": atom.review_status.value,
                "source_memory_id": l1_unit.id,
                "session_id": l1_unit.metadata.get("session_id"),
            },
        )


def _optional_text(value: object) -> str | None:
    return str(value) if value is not None else None


def _confidence(value: object) -> float:
    return float(value) if isinstance(value, (int, float)) else 0.5
