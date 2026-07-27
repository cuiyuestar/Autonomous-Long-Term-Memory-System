"""L2 atom extraction from L1 context capsules."""

from __future__ import annotations

import json
from typing import Any, Dict, List, Sequence

from altm.contracts import (
    EvidenceRef,
    EvidenceRelation,
    L2Atom,
    L2AtomType,
    LifecycleState,
    MemoryLayer,
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
        l1_units = [
            unit
            for unit in self.store.list_memory_units(layer=MemoryLayer.L1)
            if unit.metadata.get("session_id") == session_id
        ]
        created: List[MemoryUnit] = []
        for l1_unit in l1_units:
            created.extend(self.extract_from_l1(l1_unit))
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
        created: List[MemoryUnit] = []
        for atom in atoms:
            if self.store.find_l2_duplicate(atom) is not None:
                continue
            memory = self._atom_to_memory_unit(atom, l1_unit)
            self.store.put_l2_atom(atom, memory)
            created.append(memory)
        return created

    def _parse_atoms(self, response: Dict[str, Any], l1_unit: MemoryUnit) -> Sequence[L2Atom]:
        raw_atoms = response.get("atoms", [])
        if not isinstance(raw_atoms, list):
            raise ValueError("LLM response field `atoms` must be a list")

        parsed: List[L2Atom] = []
        for raw_atom in raw_atoms:
            if not isinstance(raw_atom, dict):
                raise ValueError("Each L2 atom must be an object")
            atom_type = L2AtomType(raw_atom["atom_type"])
            text = str(raw_atom["text"]).strip()
            atom_id = stable_id("l2", l1_unit.id, atom_type.value, text)
            parsed.append(
                L2Atom(
                    id=atom_id,
                    atom_type=atom_type,
                    text=text,
                    subject=raw_atom.get("subject"),
                    predicate=raw_atom.get("predicate"),
                    object=raw_atom.get("object"),
                    scope=raw_atom.get("scope"),
                    confidence=float(raw_atom.get("confidence", 0.5)),
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
