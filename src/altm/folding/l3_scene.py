"""Rule-based L3 scene construction from L2 atoms."""

from __future__ import annotations

import json
from collections import defaultdict
from dataclasses import dataclass
from math import sqrt
from typing import Sequence

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

CROSS_SESSION_L3_CANDIDATE_EDGE = "cross_session_l3_candidate"


@dataclass(frozen=True)
class CrossSessionL3Candidate:
    source_memory_id: str
    target_memory_id: str
    source_session_id: str
    target_session_id: str
    atom_type: str
    similarity: float
    edge_id: str


class RuleBasedL3SceneBuilder:
    def __init__(self, store: SQLiteMemoryStore) -> None:
        self.store = store

    def build(
        self,
        session_id: str | None = None,
        min_group_size: int = 2,
        limit: int = 1000,
    ) -> Sequence[MemoryUnit]:
        l2_units = [
            memory
            for memory in self.store.list_memory_units(layer=MemoryLayer.L2, limit=limit)
            if session_id is None or memory.metadata.get("session_id") == session_id
        ]
        groups: dict[tuple[str, str], list[MemoryUnit]] = defaultdict(list)
        for memory in l2_units:
            group_session = str(memory.metadata.get("session_id") or "global")
            atom_type = str(memory.metadata.get("atom_type") or "unknown")
            groups[(group_session, atom_type)].append(memory)

        created: list[MemoryUnit] = []
        for (group_session, atom_type), memories in groups.items():
            if len(memories) < min_group_size:
                continue
            scene = self._group_to_scene(group_session, atom_type, memories)
            self.store.put_memory_unit(scene)
            created.append(scene)
        return created

    def _group_to_scene(
        self,
        session_id: str,
        atom_type: str,
        memories: Sequence[MemoryUnit],
    ) -> MemoryUnit:
        sorted_memories = sorted(memories, key=lambda memory: (memory.created_at, memory.id))
        source_ids = [memory.id for memory in sorted_memories]
        scene_key = "%s:%s" % (session_id, atom_type)
        scene_id = stable_id("l3", scene_key, *source_ids)
        title = "L3 scene for %s in %s" % (atom_type, session_id)
        summaries = [memory.summary or memory.content for memory in sorted_memories]
        content_data = {
            "scene_key": scene_key,
            "title": title,
            "session_id": session_id,
            "atom_type": atom_type,
            "source_memory_ids": source_ids,
            "summaries": summaries,
        }
        content = json.dumps(content_data, ensure_ascii=False, sort_keys=True)
        now = utc_now_iso()
        return MemoryUnit(
            id=scene_id,
            layer=MemoryLayer.L3,
            lifecycle_state=LifecycleState.SHORT,
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
                    confidence=0.8,
                )
                for memory in sorted_memories
            ],
            metadata={
                "scene_key": scene_key,
                "session_id": session_id,
                "atom_type": atom_type,
                "source_memory_ids": source_ids,
                "builder": "rule_based_l3_scene_builder",
            },
        )


class CrossSessionL3CandidateFinder:
    def __init__(self, store: SQLiteMemoryStore) -> None:
        self.store = store

    def find(
        self,
        embedding_model: str,
        threshold: float = 0.82,
        limit: int = 1000,
        dry_run: bool = False,
    ) -> Sequence[CrossSessionL3Candidate]:
        memories = [
            memory
            for memory in self.store.list_memory_units(layer=MemoryLayer.L2, limit=limit)
            if _is_l3_candidate_source(memory)
        ]
        embeddings: dict[str, list[float]] = {}
        for memory in memories:
            cached = self.store.get_memory_embedding(memory.id, embedding_model)
            if cached is None or cached[0] != memory.content_hash:
                continue
            embeddings[memory.id] = cached[1]

        candidates: list[CrossSessionL3Candidate] = []
        for index, left in enumerate(memories):
            left_vector = embeddings.get(left.id)
            if left_vector is None:
                continue
            for right in memories[index + 1 :]:
                if not _compatible_cross_session_pair(left, right):
                    continue
                right_vector = embeddings.get(right.id)
                if right_vector is None:
                    continue
                similarity = _cosine(left_vector, right_vector)
                if similarity < threshold:
                    continue
                source, target = _stable_pair_order(left, right)
                edge_id = stable_id(
                    "graph_edge",
                    source.id,
                    target.id,
                    CROSS_SESSION_L3_CANDIDATE_EDGE,
                )
                source_session = str(source.metadata.get("session_id") or "global")
                target_session = str(target.metadata.get("session_id") or "global")
                metadata = {
                    "atom_type": source.metadata.get("atom_type"),
                    "embedding_model": embedding_model,
                    "similarity": similarity,
                    "threshold": threshold,
                    "source_session_id": source_session,
                    "target_session_id": target_session,
                    "source_summary": source.summary or source.content[:200],
                    "target_summary": target.summary or target.content[:200],
                    "candidate_status": "candidate",
                    "candidate_last_seen_at": utc_now_iso(),
                }
                if not dry_run:
                    edge_id = self.store.put_memory_graph_edge(
                        source_memory_id=source.id,
                        target_memory_id=target.id,
                        edge_type=CROSS_SESSION_L3_CANDIDATE_EDGE,
                        weight=similarity,
                        confidence=similarity,
                        metadata=metadata,
                    )
                candidates.append(
                    CrossSessionL3Candidate(
                        source_memory_id=source.id,
                        target_memory_id=target.id,
                        source_session_id=source_session,
                        target_session_id=target_session,
                        atom_type=str(source.metadata["atom_type"]),
                        similarity=similarity,
                        edge_id=edge_id,
                    )
                )
        return candidates


def _is_l3_candidate_source(memory: MemoryUnit) -> bool:
    return (
        memory.status not in {MemoryStatus.DELETED, MemoryStatus.TOMBSTONED}
        and memory.metadata.get("review_status") == "approved"
        and isinstance(memory.metadata.get("atom_type"), str)
        and bool(memory.metadata.get("session_id"))
    )


def _compatible_cross_session_pair(left: MemoryUnit, right: MemoryUnit) -> bool:
    if left.metadata.get("atom_type") != right.metadata.get("atom_type"):
        return False
    left_session = str(left.metadata.get("session_id") or "")
    right_session = str(right.metadata.get("session_id") or "")
    return bool(left_session and right_session and left_session != right_session)


def _stable_pair_order(left: MemoryUnit, right: MemoryUnit) -> tuple[MemoryUnit, MemoryUnit]:
    left_key = (str(left.metadata.get("session_id") or ""), left.created_at, left.id)
    right_key = (str(right.metadata.get("session_id") or ""), right.created_at, right.id)
    return (left, right) if left_key <= right_key else (right, left)


def _cosine(left: Sequence[float], right: Sequence[float]) -> float:
    if len(left) != len(right):
        return 0.0
    left_norm = sqrt(sum(float(value) * float(value) for value in left))
    right_norm = sqrt(sum(float(value) * float(value) for value in right))
    if left_norm == 0 or right_norm == 0:
        return 0.0
    dot = sum(float(left_value) * float(right_value) for left_value, right_value in zip(left, right))
    return dot / (left_norm * right_norm)
