"""Semantic L3 scene discovery, synthesis, and typed persistence."""

from __future__ import annotations

import json
import os
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
    SceneBlock,
    SceneType,
    ScoreBreakdown,
    SemanticGateResult,
)
from altm.folding.l3_scene import CrossSessionL3CandidateFinder
from altm.llm import OpenAICompatibleClient, SemanticModelChain, llm_config_from_env
from altm.storage import SQLiteMemoryStore
from altm.utils import sha256_text, stable_id, utc_now_iso

SCENE_SYNTHESIS_PROMPT = """Create one grounded L3 SceneBlock from the supplied L2 memories.

Return only JSON:
{
  "title": "short title",
  "scene_type": "project|task|topic|relationship|workflow",
  "summary": "coherent explanation of why the evidence forms one scene",
  "active_facts": ["currently valid fact"],
  "historical_facts": ["historical or superseded fact"],
  "open_questions": ["unresolved question"],
  "known_risks": ["risk grounded in evidence"]
}

Do not merely concatenate facts. Do not invent facts. Preserve project, time, actor,
scope, and workflow boundaries.
"""


class SemanticL3SceneBuilder:
    def __init__(
        self,
        store: SQLiteMemoryStore,
        embedding_model: str,
        chain: SemanticModelChain | None = None,
        similarity_threshold: float = 0.82,
    ) -> None:
        if store.scope is None:
            raise RuntimeError("Semantic L3 building requires an explicit memory scope")
        self.store = store
        self.embedding_model = embedding_model
        self.chain = chain or SemanticModelChain.for_scene()
        self.similarity_threshold = similarity_threshold
        self.synthesizer = OpenAICompatibleClient(llm_config_from_env("l3"))

    def build(self, limit: int = 1000) -> Sequence[MemoryUnit]:
        candidates = CrossSessionL3CandidateFinder(self.store).find(
            embedding_model=self.embedding_model,
            threshold=self.similarity_threshold,
            limit=limit,
            dry_run=True,
        )
        accepted_pairs: list[tuple[str, str, SemanticGateResult]] = []
        for candidate in candidates:
            source = self.store.get_memory_unit(candidate.source_memory_id)
            target = self.store.get_memory_unit(candidate.target_memory_id)
            if source is None or target is None:
                continue
            gate = self.chain.evaluate_scene(
                payload={
                    "source": _memory_payload(source),
                    "target": _memory_payload(target),
                    "embedding_similarity": candidate.similarity,
                },
                evidence_memory_ids=[source.id, target.id],
            )
            self._record_gate(candidate.edge_id, gate)
            if gate.decision == "write_observing":
                accepted_pairs.append((source.id, target.id, gate))
            elif gate.decision == "weak_edge":
                self.store.put_memory_graph_edge(
                    source_memory_id=source.id,
                    target_memory_id=target.id,
                    edge_type="weakly_related",
                    weight=max(0.0, gate.confidence),
                    confidence=gate.confidence,
                    metadata={
                        "semantic_gate": gate.model_dump(mode="json"),
                        "embedding_model": self.embedding_model,
                    },
                )
            elif gate.decision == "keep_both":
                self.store.put_memory_graph_edge(
                    source_memory_id=source.id,
                    target_memory_id=target.id,
                    edge_type="conflicts",
                    weight=1.0,
                    confidence=gate.confidence,
                    metadata={"semantic_gate": gate.model_dump(mode="json")},
                )

        created: list[MemoryUnit] = []
        for source_ids, gates in _connected_components(accepted_pairs):
            memories = [
                memory
                for memory_id in sorted(source_ids)
                if (memory := self.store.get_memory_unit(memory_id)) is not None
            ]
            if len(memories) < 2:
                continue
            scene, memory = self._synthesize(memories, gates)
            self.store.put_l3_scene(scene, memory)
            for source in memories:
                self.store.put_memory_graph_edge(
                    source_memory_id=source.id,
                    target_memory_id=memory.id,
                    edge_type="part_of_scene",
                    weight=scene.confidence,
                    confidence=scene.confidence,
                    metadata={"scene_id": scene.id},
                )
            created.append(memory)
        return created

    def _synthesize(
        self,
        memories: Sequence[MemoryUnit],
        gates: Sequence[SemanticGateResult],
    ) -> tuple[SceneBlock, MemoryUnit]:
        scope = self.store.scope
        if scope is None:
            raise RuntimeError("Semantic L3 synthesis requires a scoped store")
        response = self.synthesizer.chat_json(
            [
                {"role": "system", "content": SCENE_SYNTHESIS_PROMPT},
                {
                    "role": "user",
                    "content": json.dumps(
                        {"memories": [_memory_payload(memory) for memory in memories]},
                        ensure_ascii=False,
                        sort_keys=True,
                    ),
                },
            ]
        )
        source_ids = [memory.id for memory in memories]
        source_sessions = sorted(
            {
                str(memory.metadata.get("session_id"))
                for memory in memories
                if memory.metadata.get("session_id")
            }
        )
        confidence = min(gate.confidence for gate in gates)
        boundary_risk = max(
            (
                1.0 - evaluation.score
                for gate in gates
                for evaluation in gate.evaluations
                if evaluation.evaluator == "scope"
            ),
            default=0.0,
        )
        scene_id = stable_id(
            "l3_scene",
            *scope.key_parts(),
            *sorted(source_ids),
        )
        existing = self.store.get_memory_unit(scene_id)
        existing_cycles = (
            int(existing.metadata.get("observation_cycles", 0))
            if existing is not None
            else 0
        )
        scene = SceneBlock(
            id=scene_id,
            title=_required_text(response, "title"),
            scene_type=SceneType(_required_text(response, "scene_type")),
            summary=_required_text(response, "summary"),
            active_facts=_string_list(response.get("active_facts")),
            historical_facts=_string_list(response.get("historical_facts")),
            open_questions=_string_list(response.get("open_questions")),
            known_risks=_string_list(response.get("known_risks")),
            source_memory_ids=source_ids,
            source_session_ids=source_sessions,
            confidence=confidence,
            boundary_risk=boundary_risk,
            observation_cycles=existing_cycles + 1,
            metadata={
                "builder": "semantic_l3_scene_builder",
                "embedding_model": self.embedding_model,
                "semantic_gates": [
                    gate.model_dump(mode="json") for gate in gates
                ],
            },
        )
        content = scene.model_dump_json()
        now = utc_now_iso()
        memory = MemoryUnit(
            id=scene.id,
            scope=memories[0].scope,
            layer=MemoryLayer.L3,
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
            summary=scene.summary,
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
                    protection_tier=3,
                )
            ),
            evidence_refs=[
                EvidenceRef(
                    target_id=source.id,
                    target_layer=MemoryLayer.L2,
                    relation=EvidenceRelation.DERIVED_FROM,
                    confidence=scene.confidence,
                )
                for source in memories
            ],
            metadata={
                "scene_type": scene.scene_type.value,
                "source_memory_ids": source_ids,
                "source_session_ids": source_sessions,
                "builder": "semantic_l3_scene_builder",
                "semantic_confidence": scene.confidence,
                "boundary_risk": scene.boundary_risk,
                "observation_cycles": scene.observation_cycles,
            },
        )
        return scene, memory

    def _record_gate(self, target_id: str, gate: SemanticGateResult) -> None:
        self.store.append_review_event(
            event_type="autonomous_l3_semantic_gate",
            target_type="graph_edge",
            target_id=target_id,
            status=gate.decision,
            metadata=gate.model_dump(mode="json"),
        )


def _memory_payload(memory: MemoryUnit) -> dict[str, object]:
    return {
        "memory_id": memory.id,
        "summary": memory.summary or memory.content,
        "session_id": memory.metadata.get("session_id"),
        "atom_type": memory.metadata.get("atom_type"),
        "created_at": memory.created_at,
        "scope": memory.metadata.get("scope"),
    }


def _connected_components(
    pairs: Sequence[tuple[str, str, SemanticGateResult]],
) -> list[tuple[set[str], list[SemanticGateResult]]]:
    components: list[tuple[set[str], list[SemanticGateResult]]] = []
    for left, right, gate in pairs:
        pair = {left, right}
        merged_ids = set(pair)
        merged_gates = [gate]
        remaining: list[tuple[set[str], list[SemanticGateResult]]] = []
        for ids, gates in components:
            if ids & pair:
                merged_ids.update(ids)
                merged_gates.extend(gates)
            else:
                remaining.append((ids, gates))
        remaining.append((merged_ids, merged_gates))
        components = remaining
    return components


def _required_text(response: dict[str, object], field: str) -> str:
    value = str(response.get(field, "")).strip()
    if not value:
        raise ValueError("L3 synthesis requires non-empty %s" % field)
    return value


def _string_list(value: object) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise ValueError("L3 synthesis list fields must be arrays")
    return [
        text
        for item in cast(list[object], value)
        if (text := str(item).strip())
    ]


def _observation_until() -> str:
    days = int(os.environ.get("ALTM_L3_OBSERVATION_DAYS", "7"))
    if days < 1:
        raise ValueError("ALTM_L3_OBSERVATION_DAYS must be positive")
    return (
        datetime.now(UTC) + timedelta(days=days)
    ).isoformat(timespec="seconds")
