"""Semantic duplicate candidate marking and guarded resolution for L2 memories."""

from __future__ import annotations

import json
import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from math import sqrt
from typing import cast

from altm.config import high_risk_flags
from altm.contracts import MemoryLayer, MemoryStatus, MemoryUnit
from altm.storage import SQLiteMemoryStore
from altm.utils import stable_id, utc_now_iso

SEMANTIC_DEDUP_MODES = {"auto", "mark-only", "auto-merge", "auto-tombstone"}
_TERMINAL_EDGE_RESOLUTION_STATUSES = {"auto_merged", "manual_merged", "rolled_back"}
_BLOCKED_EDGE_REVIEW_STATUSES = {"rejected"}
_HIGH_RISK_AUTO_ATOM_TYPES = {"task_state", "temporal_fact"}
_STRUCTURED_FIELDS = ("subject", "predicate", "object", "scope")
_NEGATION_TERMS = (
    "不",
    "不是",
    "不能",
    "不得",
    "禁止",
    "严禁",
    "no",
    "not",
    "never",
    "disable",
    "disabled",
)
_HARD_MODALITY_TERMS = ("必须", "禁止", "严禁", "强制", "required", "must", "never")
_SOFT_MODALITY_TERMS = ("偏好", "建议", "倾向", "prefer", "recommended", "suggest")
_RISK_TOKEN_PATTERN = re.compile(
    r"(?:https?://\S+|/[A-Za-z0-9._~:/-]+|`[^`]+`|\b[A-Z][A-Z0-9_]{2,}\b|\b[A-Za-z]+[-_]?v?\d[\w.-]*\b|\b\d+(?:\.\d+)+\b)"
)


@dataclass(frozen=True)
class SemanticDedupPolicy:
    similarity_threshold: float = 0.92
    auto_merge_threshold: float = 0.97
    auto_tombstone_threshold: float = 0.97
    edge_type: str = "semantic_duplicate_candidate"
    require_review_approved: bool = True
    block_high_risk_atom_types: bool = True


@dataclass(frozen=True)
class SemanticDedupCandidate:
    source_memory_id: str
    target_memory_id: str
    similarity: float
    atom_type: str
    edge_id: str
    edge_metadata: dict[str, object] = field(
        default_factory=lambda: dict[str, object]()
    )


@dataclass(frozen=True)
class SemanticDedupResolution:
    edge_id: str
    canonical_memory_id: str
    duplicate_memory_id: str
    merged: bool
    tombstoned: bool
    reason: str
    dry_run: bool = False
    details: dict[str, object] = field(
        default_factory=lambda: dict[str, object]()
    )


class SemanticDeduper:
    def __init__(
        self,
        store: SQLiteMemoryStore,
        policy: SemanticDedupPolicy | None = None,
    ) -> None:
        self.store = store
        self.policy = policy or SemanticDedupPolicy()

    def mark_candidates(
        self,
        embedding_model: str,
        limit: int = 1000,
        dry_run: bool = False,
    ) -> Sequence[SemanticDedupCandidate]:
        memories = [
            memory
            for memory in self.store.list_memory_units(layer=MemoryLayer.L2, limit=limit)
            if isinstance(memory.metadata.get("atom_type"), str)
            and memory.status not in {MemoryStatus.DELETED, MemoryStatus.TOMBSTONED}
        ]
        embeddings: dict[str, list[float]] = {}
        for memory in memories:
            cached = self.store.get_memory_embedding(memory.id, embedding_model)
            if cached is None or cached[0] != memory.content_hash:
                continue
            embeddings[memory.id] = cached[1]

        candidates: list[SemanticDedupCandidate] = []
        for index, left in enumerate(memories):
            left_vector = embeddings.get(left.id)
            if left_vector is None:
                continue
            for right in memories[index + 1 :]:
                if left.metadata.get("atom_type") != right.metadata.get("atom_type"):
                    continue
                right_vector = embeddings.get(right.id)
                if right_vector is None:
                    continue
                similarity = _cosine(left_vector, right_vector)
                if similarity < self.policy.similarity_threshold:
                    continue
                source, target = _stable_pair_order(left, right)
                edge_id = stable_id("graph_edge", source.id, target.id, self.policy.edge_type)
                edge_metadata: dict[str, object] = {
                    "atom_type": source.metadata.get("atom_type"),
                    "embedding_model": embedding_model,
                    "similarity": similarity,
                    "threshold": self.policy.similarity_threshold,
                    "candidate_last_seen_at": utc_now_iso(),
                    "candidate_status": "candidate",
                }
                if not dry_run:
                    before = self.store.get_graph_edge(edge_id)
                    edge_id = self.store.put_memory_graph_edge(
                        source_memory_id=source.id,
                        target_memory_id=target.id,
                        edge_type=self.policy.edge_type,
                        weight=similarity,
                        confidence=similarity,
                        metadata=edge_metadata,
                    )
                    edge = self.store.get_graph_edge(edge_id)
                    edge_metadata = (
                        _object_dict(edge.get("metadata"))
                        if edge is not None
                        else edge_metadata
                    )
                    if before is None and high_risk_flags().enable_review_event_sourcing:
                        self.store.append_review_event(
                            event_type="semantic_duplicate_marked",
                            target_type="graph_edge",
                            target_id=edge_id,
                            status="pending",
                            metadata={
                                "source_memory_id": source.id,
                                "target_memory_id": target.id,
                                "atom_type": source.metadata.get("atom_type"),
                                "embedding_model": embedding_model,
                                "similarity": similarity,
                                "threshold": self.policy.similarity_threshold,
                                "dry_run": False,
                            },
                        )
                candidates.append(
                    SemanticDedupCandidate(
                        source_memory_id=source.id,
                        target_memory_id=target.id,
                        similarity=similarity,
                        atom_type=str(source.metadata["atom_type"]),
                        edge_id=edge_id,
                        edge_metadata=edge_metadata,
                    )
                )
        return candidates

    def auto_resolve_candidates(
        self,
        candidates: Sequence[SemanticDedupCandidate],
        auto_merge: bool,
        auto_tombstone: bool,
        dry_run: bool = False,
    ) -> Sequence[SemanticDedupResolution]:
        if not auto_merge:
            return []

        resolutions: list[SemanticDedupResolution] = []
        component_canonical_ids = self._component_canonical_ids(candidates)
        for candidate in candidates:
            source = self._root_memory(candidate.source_memory_id)
            target = self._root_memory(candidate.target_memory_id)
            if source is None or target is None:
                resolutions.append(
                    SemanticDedupResolution(
                        edge_id=candidate.edge_id,
                        canonical_memory_id=candidate.source_memory_id,
                        duplicate_memory_id=candidate.target_memory_id,
                        merged=False,
                        tombstoned=False,
                        reason="missing_memory",
                    )
                )
                continue
            if source.id == target.id:
                resolutions.append(
                    SemanticDedupResolution(
                        edge_id=candidate.edge_id,
                        canonical_memory_id=source.id,
                        duplicate_memory_id=target.id,
                        merged=False,
                        tombstoned=False,
                        reason="already_same_root",
                    )
                )
                continue
            if source.status in {MemoryStatus.DELETED, MemoryStatus.TOMBSTONED}:
                resolutions.append(
                    SemanticDedupResolution(
                        edge_id=candidate.edge_id,
                        canonical_memory_id=source.id,
                        duplicate_memory_id=target.id,
                        merged=False,
                        tombstoned=False,
                        reason="source_already_terminal",
                    )
                )
                continue
            if target.status in {MemoryStatus.DELETED, MemoryStatus.TOMBSTONED}:
                resolutions.append(
                    SemanticDedupResolution(
                        edge_id=candidate.edge_id,
                        canonical_memory_id=source.id,
                        duplicate_memory_id=target.id,
                        merged=False,
                        tombstoned=False,
                        reason="target_already_terminal",
                    )
                )
                continue

            edge_metadata = self._edge_metadata(candidate, dry_run=dry_run)
            edge_blocker = _edge_auto_resolution_blocker(edge_metadata)
            if edge_blocker is not None:
                resolutions.append(
                    SemanticDedupResolution(
                        edge_id=candidate.edge_id,
                        canonical_memory_id=source.id,
                        duplicate_memory_id=target.id,
                        merged=False,
                        tombstoned=False,
                        reason=edge_blocker,
                        details={"edge_metadata": edge_metadata},
                    )
                )
                continue

            component_canonical_id = component_canonical_ids.get(candidate.edge_id)
            if component_canonical_id not in {source.id, target.id}:
                resolutions.append(
                    SemanticDedupResolution(
                        edge_id=candidate.edge_id,
                        canonical_memory_id=component_canonical_id or source.id,
                        duplicate_memory_id=target.id,
                        merged=False,
                        tombstoned=False,
                        reason="component_requires_direct_canonical_edge",
                        details={
                            "source_memory_id": source.id,
                            "target_memory_id": target.id,
                            "component_canonical_memory_id": component_canonical_id,
                        },
                    )
                )
                continue

            canonical, duplicate = (
                (source, target) if source.id == component_canonical_id else (target, source)
            )
            guard = _auto_resolution_guard(
                canonical=canonical,
                duplicate=duplicate,
                candidate=candidate,
                policy=self.policy,
                auto_tombstone=auto_tombstone,
            )
            if guard is not None:
                resolutions.append(
                    SemanticDedupResolution(
                        edge_id=candidate.edge_id,
                        canonical_memory_id=canonical.id,
                        duplicate_memory_id=duplicate.id,
                        merged=False,
                        tombstoned=False,
                        reason=str(guard["reason"]),
                        details=guard,
                    )
                )
                continue

            if dry_run:
                resolutions.append(
                    SemanticDedupResolution(
                        edge_id=candidate.edge_id,
                        canonical_memory_id=canonical.id,
                        duplicate_memory_id=duplicate.id,
                        merged=False,
                        tombstoned=False,
                        reason="dry_run",
                        dry_run=True,
                        details={
                            "would_merge": True,
                            "would_tombstone": auto_tombstone,
                            "similarity": candidate.similarity,
                            "auto_merge_threshold": self.policy.auto_merge_threshold,
                            "auto_tombstone_threshold": self.policy.auto_tombstone_threshold,
                        },
                    )
                )
                continue

            result = self.store.apply_semantic_l2_merge(
                edge_id=candidate.edge_id,
                canonical_memory_id=canonical.id,
                duplicate_memory_id=duplicate.id,
                similarity=candidate.similarity,
                threshold=self.policy.similarity_threshold,
                auto_merge_threshold=self.policy.auto_merge_threshold,
                auto_tombstone_threshold=self.policy.auto_tombstone_threshold,
                embedding_model=str(edge_metadata.get("embedding_model", "")),
                auto_tombstone=auto_tombstone,
                reason="auto_l2_semantic_merge",
            )
            resolutions.append(
                SemanticDedupResolution(
                    edge_id=candidate.edge_id,
                    canonical_memory_id=canonical.id,
                    duplicate_memory_id=duplicate.id,
                    merged=bool(result.get("applied")),
                    tombstoned=bool(result.get("tombstoned")),
                    reason=str(result.get("reason", "auto_merged")),
                    details=result,
                )
            )
        return resolutions

    def _edge_metadata(
        self,
        candidate: SemanticDedupCandidate,
        dry_run: bool,
    ) -> dict[str, object]:
        if dry_run:
            return dict(candidate.edge_metadata)
        edge = self.store.get_graph_edge(candidate.edge_id)
        return (
            _object_dict(edge.get("metadata"))
            if edge is not None
            else dict(candidate.edge_metadata)
        )

    def _root_memory(self, memory_id: str) -> MemoryUnit | None:
        current = self.store.get_memory_unit(memory_id)
        visited: set[str] = set()
        while current is not None:
            if current.id in visited:
                return current
            visited.add(current.id)
            superseded_by = current.metadata.get("superseded_by")
            if not isinstance(superseded_by, str) or not superseded_by:
                return current
            parent = self.store.get_memory_unit(superseded_by)
            if parent is None or parent.status in {MemoryStatus.DELETED, MemoryStatus.TOMBSTONED}:
                return current
            current = parent
        return None

    def _component_canonical_ids(
        self,
        candidates: Sequence[SemanticDedupCandidate],
    ) -> dict[str, str]:
        groups: list[set[str]] = []
        for candidate in candidates:
            pair = {candidate.source_memory_id, candidate.target_memory_id}
            merged: set[str] = set(pair)
            remaining: list[set[str]] = []
            for group in groups:
                if group & pair:
                    merged |= group
                else:
                    remaining.append(group)
            remaining.append(merged)
            groups = remaining

        canonical_by_memory: dict[str, str] = {}
        for group in groups:
            roots = {
                root.id: root
                for memory_id in group
                if (root := self._root_memory(memory_id)) is not None
                and root.status not in {MemoryStatus.DELETED, MemoryStatus.TOMBSTONED}
            }
            if not roots:
                continue
            canonical = _choose_canonical(list(roots.values()))
            for memory_id in group | set(roots):
                canonical_by_memory[memory_id] = canonical.id

        return {
            candidate.edge_id: canonical_by_memory.get(
                candidate.source_memory_id,
                canonical_by_memory.get(candidate.target_memory_id, candidate.source_memory_id),
            )
            for candidate in candidates
        }


def _stable_pair_order(left: MemoryUnit, right: MemoryUnit) -> tuple[MemoryUnit, MemoryUnit]:
    left_key = (left.created_at, left.id)
    right_key = (right.created_at, right.id)
    return (left, right) if left_key <= right_key else (right, left)


def _edge_auto_resolution_blocker(metadata: dict[str, object]) -> str | None:
    review_status = metadata.get("review_status")
    if isinstance(review_status, str) and review_status in _BLOCKED_EDGE_REVIEW_STATUSES:
        return "edge_review_rejected"
    resolution_status = metadata.get("resolution_status")
    if (
        isinstance(resolution_status, str)
        and resolution_status in _TERMINAL_EDGE_RESOLUTION_STATUSES
    ):
        return "edge_already_resolved"
    return None


def _auto_resolution_guard(
    canonical: MemoryUnit,
    duplicate: MemoryUnit,
    candidate: SemanticDedupCandidate,
    policy: SemanticDedupPolicy,
    auto_tombstone: bool,
) -> dict[str, object] | None:
    if candidate.similarity < policy.auto_merge_threshold:
        return {
            "reason": "below_auto_merge_threshold",
            "similarity": candidate.similarity,
            "auto_merge_threshold": policy.auto_merge_threshold,
        }
    if auto_tombstone and candidate.similarity < policy.auto_tombstone_threshold:
        return {
            "reason": "below_auto_tombstone_threshold",
            "similarity": candidate.similarity,
            "auto_tombstone_threshold": policy.auto_tombstone_threshold,
        }
    if policy.block_high_risk_atom_types and candidate.atom_type in _HIGH_RISK_AUTO_ATOM_TYPES:
        return {
            "reason": "atom_type_requires_autonomous_evidence_gate",
            "atom_type": candidate.atom_type,
        }
    if policy.require_review_approved and canonical.metadata.get("review_status") != "approved":
        return {"reason": "canonical_review_not_approved", "canonical_memory_id": canonical.id}
    if policy.require_review_approved and duplicate.metadata.get("review_status") != "approved":
        return {"reason": "duplicate_review_not_approved", "duplicate_memory_id": duplicate.id}

    canonical_fields = _l2_semantic_fields(canonical)
    duplicate_fields = _l2_semantic_fields(duplicate)
    for field_name in _STRUCTURED_FIELDS:
        left = _normalize_scalar(canonical_fields.get(field_name))
        right = _normalize_scalar(duplicate_fields.get(field_name))
        if left and right and left != right:
            return {
                "reason": "structured_field_mismatch",
                "field": field_name,
                "canonical_value": canonical_fields.get(field_name),
                "duplicate_value": duplicate_fields.get(field_name),
            }

    canonical_text = _text_for_guard(canonical_fields, canonical)
    duplicate_text = _text_for_guard(duplicate_fields, duplicate)
    if _has_negation(canonical_text) != _has_negation(duplicate_text):
        return {"reason": "negation_mismatch"}
    if _modality(canonical_text) != _modality(duplicate_text):
        return {"reason": "modality_mismatch"}
    canonical_tokens = _risk_tokens(canonical_text)
    duplicate_tokens = _risk_tokens(duplicate_text)
    if canonical_tokens != duplicate_tokens:
        return {
            "reason": "risk_token_mismatch",
            "canonical_tokens": sorted(canonical_tokens),
            "duplicate_tokens": sorted(duplicate_tokens),
        }
    return None


def _choose_canonical(memories: Sequence[MemoryUnit]) -> MemoryUnit:
    return sorted(
        memories,
        key=lambda memory: (
            -_canonical_score(memory),
            memory.created_at,
            memory.id,
        ),
    )[0]


def _canonical_score(memory: MemoryUnit) -> float:
    fields = _l2_semantic_fields(memory)
    confidence = fields.get("confidence")
    confidence_score = float(confidence) if isinstance(confidence, (int, float)) else 0.0
    review_score = 10.0 if memory.metadata.get("review_status") == "approved" else 0.0
    evidence_score = memory.score.evidence_quality * 4.0
    resident_score = memory.score.resident_score * 2.0
    useful_score = min(float(memory.useful_access_count), 10.0) * 0.3
    access_score = min(float(memory.access_count), 20.0) * 0.05
    protection_score = float(memory.lifecycle.protection_tier) * 0.2
    return (
        review_score
        + confidence_score
        + evidence_score
        + resident_score
        + useful_score
        + access_score
        + protection_score
    )


def _l2_semantic_fields(memory: MemoryUnit) -> dict[str, object]:
    fields: dict[str, object] = {}
    try:
        content = json.loads(memory.content)
    except json.JSONDecodeError:
        content = {}
    if isinstance(content, dict):
        fields.update(_object_dict(cast(object, content)))
    for key in ("atom_type", "subject", "predicate", "object", "scope", "confidence"):
        if key in memory.metadata and key not in fields:
            fields[key] = memory.metadata[key]
    if "text" not in fields and memory.summary is not None:
        fields["text"] = memory.summary
    return fields


def _object_dict(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        return {}
    return {
        str(key): item
        for key, item in cast(dict[object, object], value).items()
    }


def _text_for_guard(fields: dict[str, object], memory: MemoryUnit) -> str:
    text = fields.get("text")
    if isinstance(text, str) and text.strip():
        return text
    return memory.summary or memory.content


def _normalize_scalar(value: object) -> str:
    if value is None:
        return ""
    return " ".join(str(value).strip().lower().split())


def _has_negation(text: str) -> bool:
    normalized = text.lower()
    return any(term in normalized for term in _NEGATION_TERMS)


def _modality(text: str) -> str:
    normalized = text.lower()
    has_hard = any(term in normalized for term in _HARD_MODALITY_TERMS)
    has_soft = any(term in normalized for term in _SOFT_MODALITY_TERMS)
    if has_hard and not has_soft:
        return "hard"
    if has_soft and not has_hard:
        return "soft"
    return "neutral"


def _risk_tokens(text: str) -> set[str]:
    return {match.group(0).strip("`").lower() for match in _RISK_TOKEN_PATTERN.finditer(text)}


def _cosine(left: Sequence[float], right: Sequence[float]) -> float:
    if len(left) != len(right):
        return 0.0
    left_norm = sqrt(sum(float(value) * float(value) for value in left))
    right_norm = sqrt(sum(float(value) * float(value) for value in right))
    if left_norm == 0 or right_norm == 0:
        return 0.0
    dot = sum(
        float(left_value) * float(right_value)
        for left_value, right_value in zip(left, right, strict=True)
    )
    return dot / (left_norm * right_norm)
