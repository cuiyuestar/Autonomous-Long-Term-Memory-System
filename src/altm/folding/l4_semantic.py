"""Semantic, cross-Agent L4 persona distillation with observation gates."""

from __future__ import annotations

import json
import math
import os
import re
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from typing import cast

from altm.contracts import (
    EvidenceRef,
    EvidenceRelation,
    LifecycleMeta,
    LifecycleState,
    MemoryLayer,
    MemoryStatus,
    MemoryUnit,
    MemoryVisibility,
    PersonaFacet,
    PersonaStatus,
    ScoreBreakdown,
    SemanticGateResult,
)
from altm.folding.l4_persona import PERSONA_ATOM_TYPES
from altm.llm import OpenAICompatibleClient, SemanticModelChain, llm_config_from_env
from altm.storage import SQLiteMemoryStore
from altm.utils import sha256_text, stable_id, utc_now_iso

PERSONA_SYNTHESIS_PROMPT = """Create one grounded L4 persona facet.

Return only JSON:
{
  "facet_key": "stable_lower_snake_case_slot",
  "facet_type": "preference|constraint|lesson|workflow|value",
  "statement": "precise stable user trait",
  "workspace_scope": "short scope description"
}

The facet must be supported across sessions or Agents in one user workspace.
Do not convert a temporary request or project-only rule into a persona.
Do not hide counter-evidence.
"""

PERSONA_SUPERSESSION_PROMPT = """Resolve a possible L4 persona change.

Return only JSON:
{
  "decision": "supersede|coexist|defer",
  "supersedes_persona_id": "existing current persona id or empty",
  "facet_key": "stable_lower_snake_case_slot",
  "facet_type": "preference|constraint|lesson|workflow|value",
  "statement": "precise current user trait",
  "workspace_scope": "short scope description",
  "confidence": 0.0,
  "reason": "grounded resolution reason",
  "supporting_memory_ids": ["ids from supplied evidence"]
}

Supersede only when repeated newer evidence clearly replaces the same semantic
facet in the same user workspace. Use coexist for different scopes or facets.
Use defer for unresolved conflict. Never invent memory or persona ids.
"""


class SemanticL4PersonaDistiller:
    def __init__(
        self,
        store: SQLiteMemoryStore,
        embedding_model: str,
        chain: SemanticModelChain | None = None,
        similarity_threshold: float = 0.84,
    ) -> None:
        if store.scope is None:
            raise RuntimeError("Semantic L4 distillation requires an explicit memory scope")
        self.store = store
        self.embedding_model = embedding_model
        self.chain = chain or SemanticModelChain.for_persona()
        self.similarity_threshold = similarity_threshold
        self.synthesizer = OpenAICompatibleClient(llm_config_from_env("l4"))

    def distill(self, limit: int = 1000) -> Sequence[MemoryUnit]:
        scope = self.store.scope
        if scope is None:
            raise RuntimeError("Semantic L4 distillation requires a scoped store")
        memories = [
            memory
            for memory in self.store.list_user_workspace_memories(
                layer=MemoryLayer.L2,
                limit=limit,
            )
            if memory.metadata.get("atom_type") in PERSONA_ATOM_TYPES
            and memory.metadata.get("review_status") != "rejected"
        ]
        created: list[MemoryUnit] = []
        for component in _embedding_components(
            self.store,
            memories,
            embedding_model=self.embedding_model,
            threshold=self.similarity_threshold,
        ):
            if not _has_cross_context_support(component):
                continue
            gate = self.chain.evaluate_persona(
                payload={
                    "workspace": {
                        "tenant_id": scope.tenant_id,
                        "workspace_id": scope.workspace_id,
                        "user_id": scope.user_id,
                    },
                    "evidence": [_memory_payload(memory) for memory in component],
                },
                evidence_memory_ids=[memory.id for memory in component],
            )
            self._record_gate(component, gate)
            if gate.decision == "observe_conflict":
                replacement = self._resolve_supersession(
                    evidence=component,
                    gate=gate,
                    candidate=None,
                )
                if replacement is not None:
                    created.append(replacement)
                else:
                    _write_conflict_edges(self.store, component, gate)
                continue
            if gate.decision != "write_observing":
                continue
            persona, memory = self._synthesize(component, gate)
            current = _current_facet(
                self.store.list_current_l4_personas(),
                persona.facet_key,
            )
            if current is not None and current.id != persona.id:
                replacement = self._resolve_supersession(
                    evidence=component,
                    gate=gate,
                    candidate=(persona, memory),
                )
                if replacement is not None:
                    created.append(replacement)
                continue
            self.store.put_l4_persona(persona, memory)
            created.append(memory)
        return created

    def _synthesize(
        self,
        evidence: Sequence[MemoryUnit],
        gate: SemanticGateResult,
    ) -> tuple[PersonaFacet, MemoryUnit]:
        scope = self.store.scope
        if scope is None:
            raise RuntimeError("Semantic L4 synthesis requires a scoped store")
        response = self.synthesizer.chat_json(
            [
                {"role": "system", "content": PERSONA_SYNTHESIS_PROMPT},
                {
                    "role": "user",
                    "content": json.dumps(
                        {"evidence": [_memory_payload(memory) for memory in evidence]},
                        ensure_ascii=False,
                        sort_keys=True,
                    ),
                },
            ]
        )
        facet_key = _required_facet_key(response)
        facet_type = _required_text(response, "facet_type")
        statement = _required_text(response, "statement")
        workspace_scope = _required_text(response, "workspace_scope")
        source_ids = sorted(memory.id for memory in evidence)
        source_agents = sorted({memory.scope.agent_id for memory in evidence})
        now = utc_now_iso()
        persona_id = stable_id(
            "l4_persona",
            scope.tenant_id,
            scope.workspace_id,
            scope.user_id,
            facet_key,
            statement,
        )
        existing = self.store.get_memory_unit(persona_id)
        existing_cycles = (
            int(existing.metadata.get("observation_cycles", 0))
            if existing is not None
            else 0
        )
        first_observed = (
            str(existing.metadata.get("first_observed_at"))
            if existing is not None and existing.metadata.get("first_observed_at")
            else now
        )
        persona = PersonaFacet(
            id=persona_id,
            facet_key=facet_key,
            facet_type=facet_type,
            statement=statement,
            workspace_scope=workspace_scope,
            confidence=gate.confidence,
            stability_score=gate.confidence,
            status=(
                PersonaStatus.ACTIVE
                if existing is not None and existing.status == MemoryStatus.ACTIVE
                else PersonaStatus.OBSERVING
            ),
            source_memory_ids=source_ids,
            source_agent_ids=source_agents,
            counter_evidence_memory_ids=[],
            first_observed_at=first_observed,
            last_observed_at=now,
            observation_cycles=existing_cycles + 1,
            metadata={
                "facet_key": facet_key,
                "builder": "semantic_l4_persona_distiller",
                "embedding_model": self.embedding_model,
                "semantic_gate": gate.model_dump(mode="json"),
            },
        )
        content = persona.model_dump_json()
        source_scope = evidence[0].scope
        memory = MemoryUnit(
            id=persona.id,
            scope=source_scope,
            visibility=MemoryVisibility.USER_WORKSPACE,
            layer=MemoryLayer.L4,
            lifecycle_state=(
                existing.lifecycle_state
                if existing is not None
                else LifecycleState.SHORT
            ),
            status=(
                existing.status
                if existing is not None
                else MemoryStatus.OBSERVING
            ),
            content=content,
            content_hash=sha256_text(content),
            summary=persona.statement,
            created_at=existing.created_at if existing is not None else now,
            updated_at=now,
            last_accessed_at=(
                existing.last_accessed_at if existing is not None else None
            ),
            access_count=existing.access_count if existing is not None else 0,
            useful_access_count=(
                existing.useful_access_count if existing is not None else 0
            ),
            score=existing.score if existing is not None else ScoreBreakdown(),
            lifecycle=(
                existing.lifecycle
                if existing is not None
                else LifecycleMeta(
                    observation_until=_observation_until(),
                    protection_tier=4,
                )
            ),
            evidence_refs=[
                EvidenceRef(
                    target_id=source.id,
                    target_layer=MemoryLayer.L2,
                    relation=EvidenceRelation.DERIVED_FROM,
                    confidence=gate.confidence,
                )
                for source in evidence
            ],
            metadata={
                "facet_key": facet_key,
                "facet_type": facet_type,
                "workspace_scope": workspace_scope,
                "source_memory_ids": source_ids,
                "source_agent_ids": source_agents,
                "builder": "semantic_l4_persona_distiller",
                "first_observed_at": first_observed,
                "observation_cycles": existing_cycles + 1,
                "semantic_confidence": gate.confidence,
            },
        )
        return persona, memory

    def _resolve_supersession(
        self,
        evidence: Sequence[MemoryUnit],
        gate: SemanticGateResult,
        candidate: tuple[PersonaFacet, MemoryUnit] | None,
    ) -> MemoryUnit | None:
        scope = self.store.scope
        if scope is None:
            raise RuntimeError("Semantic L4 supersession requires a scoped store")
        current_facets = list(self.store.list_current_l4_personas())
        if not current_facets:
            return None
        response = self.synthesizer.chat_json(
            [
                {"role": "system", "content": PERSONA_SUPERSESSION_PROMPT},
                {
                    "role": "user",
                    "content": json.dumps(
                        {
                            "current_personas": [
                                persona.model_dump(mode="json")
                                for persona in current_facets
                            ],
                            "candidate": (
                                candidate[0].model_dump(mode="json")
                                if candidate is not None
                                else None
                            ),
                            "evidence": [
                                _memory_payload(memory)
                                for memory in evidence
                            ],
                            "semantic_gate": gate.model_dump(mode="json"),
                        },
                        ensure_ascii=False,
                        sort_keys=True,
                    ),
                },
            ]
        )
        decision = _required_choice(
            response,
            "decision",
            {"supersede", "coexist", "defer"},
        )
        resolver_confidence = _required_confidence(response)
        previous_id = str(
            response.get("supersedes_persona_id", "")
        ).strip()
        facet_key = _required_facet_key(response)
        reason = _required_text(response, "reason")
        supporting_ids = _required_string_list(
            response,
            "supporting_memory_ids",
        )
        resolution_confidence = min(gate.confidence, resolver_confidence)
        self._record_supersession_resolution(
            previous_id=previous_id,
            facet_key=facet_key,
            decision=decision,
            confidence=resolution_confidence,
            reason=reason,
            supporting_ids=supporting_ids,
        )
        if decision != "supersede":
            return None
        if resolution_confidence < _supersession_min_confidence():
            return None

        current = next(
            (
                persona
                for persona in current_facets
                if persona.id == previous_id
            ),
            None,
        )
        if current is None or current.facet_key != facet_key:
            return None
        evidence_by_id = {memory.id: memory for memory in evidence}
        if (
            not supporting_ids
            or any(memory_id not in evidence_by_id for memory_id in supporting_ids)
        ):
            return None
        supporting = [evidence_by_id[memory_id] for memory_id in supporting_ids]
        if not _has_cross_context_support(supporting):
            return None
        if not set(supporting_ids).difference(current.source_memory_ids):
            return None

        facet_type = _required_text(response, "facet_type")
        statement = _required_text(response, "statement")
        workspace_scope = _required_text(response, "workspace_scope")
        if candidate is not None:
            candidate_persona, _ = candidate
            if (
                candidate_persona.facet_key != facet_key
                or candidate_persona.facet_type != facet_type
                or candidate_persona.statement != statement
                or candidate_persona.workspace_scope != workspace_scope
            ):
                return None
        replacement_id = stable_id(
            "l4_persona",
            scope.tenant_id,
            scope.workspace_id,
            scope.user_id,
            facet_key,
            statement,
        )
        if replacement_id == current.id:
            return None
        previous_memory = self.store.get_memory_unit(current.id)
        if previous_memory is None:
            return None

        now = utc_now_iso()
        source_agents = sorted(
            {memory.scope.agent_id for memory in supporting}
        )
        replacement = PersonaFacet(
            id=replacement_id,
            facet_key=facet_key,
            facet_type=facet_type,
            statement=statement,
            workspace_scope=workspace_scope,
            confidence=resolution_confidence,
            stability_score=resolution_confidence,
            status=PersonaStatus.ACTIVE,
            source_memory_ids=sorted(supporting_ids),
            source_agent_ids=source_agents,
            counter_evidence_memory_ids=sorted(
                set(current.source_memory_ids).difference(supporting_ids)
            ),
            first_observed_at=current.first_observed_at,
            last_observed_at=now,
            observation_cycles=current.observation_cycles + 1,
            metadata={
                "facet_key": facet_key,
                "builder": "semantic_l4_persona_distiller",
                "embedding_model": self.embedding_model,
                "semantic_gate": gate.model_dump(mode="json"),
                "supersedes_persona_id": current.id,
                "supersession_reason": reason,
                "supersession_confidence": resolution_confidence,
            },
        )
        content = replacement.model_dump_json()
        replacement_memory = MemoryUnit(
            id=replacement.id,
            scope=supporting[0].scope,
            visibility=MemoryVisibility.USER_WORKSPACE,
            layer=MemoryLayer.L4,
            lifecycle_state=LifecycleState.LONG,
            status=MemoryStatus.ACTIVE,
            content=content,
            content_hash=sha256_text(content),
            summary=replacement.statement,
            created_at=now,
            updated_at=now,
            last_accessed_at=previous_memory.last_accessed_at,
            access_count=previous_memory.access_count,
            useful_access_count=previous_memory.useful_access_count,
            score=previous_memory.score,
            lifecycle=previous_memory.lifecycle.model_copy(
                update={
                    "protection_tier": max(
                        4,
                        previous_memory.lifecycle.protection_tier,
                    ),
                    "observation_until": None,
                    "promotion_candidate_since": None,
                    "demotion_candidate_since": None,
                }
            ),
            evidence_refs=[
                *[
                    EvidenceRef(
                        target_id=source.id,
                        target_layer=MemoryLayer.L2,
                        relation=EvidenceRelation.DERIVED_FROM,
                        confidence=resolution_confidence,
                    )
                    for source in supporting
                ],
                EvidenceRef(
                    target_id=current.id,
                    target_layer=MemoryLayer.L4,
                    relation=EvidenceRelation.SUPERSEDES,
                    confidence=resolution_confidence,
                ),
            ],
            metadata={
                "facet_key": facet_key,
                "facet_type": facet_type,
                "workspace_scope": workspace_scope,
                "source_memory_ids": sorted(supporting_ids),
                "source_agent_ids": source_agents,
                "builder": "semantic_l4_persona_distiller",
                "first_observed_at": current.first_observed_at,
                "observation_cycles": current.observation_cycles + 1,
                "semantic_confidence": resolution_confidence,
                "persona_status": PersonaStatus.ACTIVE.value,
                "supersedes_persona_id": current.id,
            },
        )
        self.store.supersede_l4_persona(
            previous_persona_id=current.id,
            replacement=replacement,
            replacement_memory=replacement_memory,
            reason=reason,
            confidence=resolution_confidence,
        )
        return replacement_memory

    def _record_supersession_resolution(
        self,
        previous_id: str,
        facet_key: str,
        decision: str,
        confidence: float,
        reason: str,
        supporting_ids: Sequence[str],
    ) -> None:
        self.store.append_review_event(
            event_type="autonomous_l4_supersession_gate",
            target_type="memory_unit",
            target_id=previous_id or stable_id("l4_facet", facet_key),
            status=decision,
            metadata={
                "facet_key": facet_key,
                "confidence": confidence,
                "reason": reason,
                "supporting_memory_ids": list(supporting_ids),
            },
        )

    def _record_gate(
        self,
        component: Sequence[MemoryUnit],
        gate: SemanticGateResult,
    ) -> None:
        target_id = stable_id(
            "persona_gate",
            *sorted(memory.id for memory in component),
        )
        self.store.append_review_event(
            event_type="autonomous_l4_semantic_gate",
            target_type="persona_candidate",
            target_id=target_id,
            status=gate.decision,
            metadata=gate.model_dump(mode="json"),
        )


def _embedding_components(
    store: SQLiteMemoryStore,
    memories: Sequence[MemoryUnit],
    embedding_model: str,
    threshold: float,
) -> list[list[MemoryUnit]]:
    by_type: dict[str, list[MemoryUnit]] = {}
    vectors: dict[str, list[float]] = {}
    for memory in memories:
        atom_type = str(memory.metadata.get("atom_type") or "")
        cached = store.get_user_workspace_embedding(memory.id, embedding_model)
        if cached is None or cached[0] != memory.content_hash:
            continue
        by_type.setdefault(atom_type, []).append(memory)
        vectors[memory.id] = cached[1]

    components: list[list[MemoryUnit]] = []
    for typed_memories in by_type.values():
        groups: list[set[str]] = []
        for index, left in enumerate(typed_memories):
            for right in typed_memories[index + 1 :]:
                if _cosine(vectors[left.id], vectors[right.id]) < threshold:
                    continue
                pair = {left.id, right.id}
                merged = set(pair)
                remaining: list[set[str]] = []
                for group in groups:
                    if group & pair:
                        merged.update(group)
                    else:
                        remaining.append(group)
                remaining.append(merged)
                groups = remaining
        memory_by_id = {memory.id: memory for memory in typed_memories}
        components.extend(
            [
                [memory_by_id[memory_id] for memory_id in sorted(group)]
                for group in groups
                if len(group) >= 2
            ]
        )
    return components


def _has_cross_context_support(memories: Sequence[MemoryUnit]) -> bool:
    sessions = {
        str(memory.metadata.get("session_id"))
        for memory in memories
        if memory.metadata.get("session_id")
    }
    agents = {memory.scope.agent_id for memory in memories}
    return len(sessions) >= 2 or len(agents) >= 2


def _write_conflict_edges(
    store: SQLiteMemoryStore,
    memories: Sequence[MemoryUnit],
    gate: SemanticGateResult,
) -> None:
    for index, left in enumerate(memories):
        for right in memories[index + 1 :]:
            store.put_memory_graph_edge(
                source_memory_id=left.id,
                target_memory_id=right.id,
                edge_type="conflicts",
                weight=1.0,
                confidence=gate.confidence,
                metadata={"semantic_gate": gate.model_dump(mode="json")},
            )


def _memory_payload(memory: MemoryUnit) -> dict[str, object]:
    return {
        "memory_id": memory.id,
        "statement": memory.summary or memory.content,
        "agent_id": memory.scope.agent_id,
        "session_id": memory.metadata.get("session_id"),
        "atom_type": memory.metadata.get("atom_type"),
        "scope": memory.metadata.get("scope"),
        "created_at": memory.created_at,
    }


def _required_text(response: dict[str, object], field: str) -> str:
    value = str(response.get(field, "")).strip()
    if not value:
        raise ValueError("L4 synthesis requires non-empty %s" % field)
    return value


def _required_facet_key(response: dict[str, object]) -> str:
    value = _required_text(response, "facet_key").lower()
    if re.fullmatch(r"[a-z][a-z0-9_]{2,63}", value) is None:
        raise ValueError("L4 facet_key must be stable lower_snake_case")
    return value


def _required_choice(
    response: dict[str, object],
    field: str,
    allowed: set[str],
) -> str:
    value = _required_text(response, field).lower()
    if value not in allowed:
        raise ValueError("L4 %s must be one of %s" % (field, sorted(allowed)))
    return value


def _required_confidence(response: dict[str, object]) -> float:
    value = response.get("confidence")
    if not isinstance(value, (int, float)) or not 0.0 <= float(value) <= 1.0:
        raise ValueError("L4 supersession confidence must be between 0 and 1")
    return float(value)


def _required_string_list(
    response: dict[str, object],
    field: str,
) -> list[str]:
    value = response.get(field)
    if not isinstance(value, list):
        raise ValueError("L4 %s must be a list of non-empty strings" % field)
    values = cast(list[object], value)
    if any(
        not isinstance(item, str) or not item.strip()
        for item in values
    ):
        raise ValueError("L4 %s must be a list of non-empty strings" % field)
    return list(
        dict.fromkeys(
            item.strip()
            for item in cast(list[str], values)
        )
    )


def _current_facet(
    personas: Sequence[PersonaFacet],
    facet_key: str,
) -> PersonaFacet | None:
    return next(
        (persona for persona in personas if persona.facet_key == facet_key),
        None,
    )


def _supersession_min_confidence() -> float:
    value = float(os.environ.get("ALTM_L4_OVERWRITE_MIN_CONFIDENCE", "0.90"))
    if not 0.0 <= value <= 1.0:
        raise ValueError(
            "ALTM_L4_OVERWRITE_MIN_CONFIDENCE must be between 0 and 1"
        )
    return value


def _cosine(left: Sequence[float], right: Sequence[float]) -> float:
    if len(left) != len(right):
        return 0.0
    left_norm = math.sqrt(sum(value * value for value in left))
    right_norm = math.sqrt(sum(value * value for value in right))
    if left_norm == 0 or right_norm == 0:
        return 0.0
    return sum(
        lv * rv for lv, rv in zip(left, right, strict=True)
    ) / (left_norm * right_norm)


def _observation_until() -> str:
    days = int(os.environ.get("ALTM_L4_OBSERVATION_DAYS", "30"))
    if days < 1:
        raise ValueError("ALTM_L4_OBSERVATION_DAYS must be positive")
    return (
        datetime.now(UTC) + timedelta(days=days)
    ).isoformat(timespec="seconds")
