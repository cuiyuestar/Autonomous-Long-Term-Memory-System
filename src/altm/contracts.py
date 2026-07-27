"""Core contracts shared by storage, retrieval, lifecycle, and adapters."""

from __future__ import annotations

from enum import Enum
from typing import Any, Dict, List, Optional, Tuple

from pydantic import BaseModel, ConfigDict, Field, field_validator


class MemoryLayer(str, Enum):
    L0 = "L0"
    L1 = "L1"
    L2 = "L2"
    L3 = "L3"
    L4 = "L4"


class LifecycleState(str, Enum):
    PERMANENT = "permanent"
    LONG = "long"
    SHORT = "short"


class MemoryStatus(str, Enum):
    ACTIVE = "active"
    COMPRESSED = "compressed"
    OBSERVING = "observing"
    TOMBSTONED = "tombstoned"
    DELETED = "deleted"


class EvidenceRelation(str, Enum):
    SOURCE = "source"
    DERIVED_FROM = "derived_from"
    SUPPORTS = "supports"
    CONFLICTS = "conflicts"
    SUPERSEDES = "supersedes"


class AccessSignal(str, Enum):
    CANDIDATE_HIT = "candidate_hit"
    INJECTED = "injected"
    CITED_BY_AGENT = "cited_by_agent"
    USER_CONFIRMED = "user_confirmed"
    USER_REJECTED = "user_rejected"


class ContextBand(str, Enum):
    IMMEDIATE = "immediate"
    WORKING = "working"
    BACKGROUND = "background"
    DRILLDOWN_MARKER = "drilldown_marker"


class MessageRole(str, Enum):
    USER = "user"
    ASSISTANT = "assistant"
    SYSTEM = "system"
    TOOL = "tool"
    OTHER = "other"


class L2AtomType(str, Enum):
    PREFERENCE = "preference"
    CONSTRAINT = "constraint"
    PROJECT_FACT = "project_fact"
    DECISION = "decision"
    ISSUE = "issue"
    RESOLUTION = "resolution"
    TASK_STATE = "task_state"
    TEMPORAL_FACT = "temporal_fact"
    LESSON = "lesson"


class ReviewStatus(str, Enum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"


class ReviewItemKind(str, Enum):
    L2_PENDING = "l2_pending"
    PROMOTION_CANDIDATE = "promotion_candidate"
    DEMOTION_CANDIDATE = "demotion_candidate"
    SEMANTIC_DUPLICATE_CANDIDATE = "semantic_duplicate_candidate"
    CROSS_SESSION_L3_CANDIDATE = "cross_session_l3_candidate"
    L3_OBSERVING = "l3_observing"
    L4_PERSONA_CANDIDATE = "l4_persona_candidate"


class ReviewActionType(str, Enum):
    CONFIRM_L2 = "confirm_l2"
    SUPPRESS_L2 = "suppress_l2"
    PROMOTE_TO_LONG = "promote_to_long"
    CLEAR_PROMOTION_CANDIDATE = "clear_promotion_candidate"
    MARK_OBSERVING = "mark_observing"
    CLEAR_DEMOTION_CANDIDATE = "clear_demotion_candidate"
    PREPARE_DUPLICATE_RESOLUTION = "prepare_duplicate_resolution"
    MARK_NOT_DUPLICATE = "mark_not_duplicate"
    ACTIVATE_L3_SCENE = "activate_l3_scene"
    REJECT_L3_SCENE = "reject_l3_scene"
    CONFIRM_CROSS_SESSION_L3_CANDIDATE = "confirm_cross_session_l3_candidate"
    REJECT_CROSS_SESSION_L3_CANDIDATE = "reject_cross_session_l3_candidate"
    ACTIVATE_L4_PERSONA = "activate_l4_persona"
    REJECT_L4_PERSONA = "reject_l4_persona"


class ReviewActionRisk(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class MemoryModel(BaseModel):
    model_config = ConfigDict(frozen=True)


class FallbackLocator(MemoryModel):
    session_id: Optional[str] = None
    message_ids: List[str] = Field(default_factory=list)
    time_range: Optional[Tuple[str, str]] = None
    topic_tags: List[str] = Field(default_factory=list)
    text_hash: Optional[str] = None
    excerpt: Optional[str] = None


class EvidenceRef(MemoryModel):
    target_id: str
    target_layer: MemoryLayer
    relation: EvidenceRelation
    confidence: float = Field(ge=0.0, le=1.0)
    fallback_locator: Optional[FallbackLocator] = None


class ScoreBreakdown(MemoryModel):
    resident_score: float = 0.0
    retrieval_score: Optional[float] = None
    structural: float = 0.0
    recency: float = 0.0
    access: float = 0.0
    semantic: Optional[float] = None
    task_affinity: Optional[float] = None
    urgency: Optional[float] = None
    evidence_quality: float = 0.0


class LifecycleMeta(MemoryModel):
    age: int = 0
    protection_tier: int = Field(default=1, ge=1, le=5)
    compression_tier: int = Field(default=0, ge=0, le=4)
    observation_until: Optional[str] = None
    demotion_candidate_since: Optional[str] = None
    promotion_candidate_since: Optional[str] = None


class MemoryUnit(MemoryModel):
    id: str
    layer: MemoryLayer
    lifecycle_state: LifecycleState
    status: MemoryStatus
    content: str
    content_hash: str
    created_at: str
    updated_at: str
    summary: Optional[str] = None
    last_accessed_at: Optional[str] = None
    access_count: int = 0
    useful_access_count: int = 0
    score: ScoreBreakdown = Field(default_factory=ScoreBreakdown)
    lifecycle: LifecycleMeta = Field(default_factory=LifecycleMeta)
    evidence_refs: List[EvidenceRef] = Field(default_factory=list)
    graph_refs: List[str] = Field(default_factory=list)
    metadata: Dict[str, Any] = Field(default_factory=dict)


class CaptureInput(MemoryModel):
    session_id: str
    content: str
    role: MessageRole = MessageRole.USER
    message_id: Optional[str] = None
    created_at: Optional[str] = None
    metadata: Dict[str, Any] = Field(default_factory=dict)

    @field_validator("content")
    @classmethod
    def content_must_not_be_empty(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("capture content must not be empty")
        return value


class ContextCapsule(MemoryModel):
    id: str
    title: str
    time_range: Tuple[str, str]
    session_id: str
    source_message_ids: List[str]
    task_goal: Optional[str] = None
    local_context: str
    key_turns: List[str] = Field(default_factory=list)
    decisions_mentioned: List[str] = Field(default_factory=list)
    unresolved_questions: List[str] = Field(default_factory=list)
    emotional_or_pragmatic_tone: Optional[str] = None
    topic_tags: List[str] = Field(default_factory=list)
    confidence: float = Field(default=0.5, ge=0.0, le=1.0)


class L2Atom(MemoryModel):
    id: str
    atom_type: L2AtomType
    text: str
    subject: Optional[str] = None
    predicate: Optional[str] = None
    object: Optional[str] = None
    scope: Optional[str] = None
    confidence: float = Field(default=0.5, ge=0.0, le=1.0)
    extraction_reason: str
    source_memory_id: str
    review_status: ReviewStatus = ReviewStatus.PENDING
    metadata: Dict[str, Any] = Field(default_factory=dict)

    @field_validator("text")
    @classmethod
    def text_must_not_be_empty(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("L2 atom text must not be empty")
        return value


class L2ExtractionResult(MemoryModel):
    atoms: List[L2Atom] = Field(default_factory=list)


class ReviewQueueItem(MemoryModel):
    id: str
    kind: ReviewItemKind
    target_type: str
    target_id: str
    review_status: ReviewStatus = ReviewStatus.PENDING
    title: str
    summary: Optional[str] = None
    source_memory_ids: List[str] = Field(default_factory=list)
    metadata: Dict[str, Any] = Field(default_factory=dict)


class ReviewActionPlan(MemoryModel):
    id: str
    review_item_id: str
    action_type: ReviewActionType
    target_type: str
    target_id: str
    risk: ReviewActionRisk
    requires_second_confirmation: bool = False
    description: str
    proposed_changes: Dict[str, Any] = Field(default_factory=dict)
    source_memory_ids: List[str] = Field(default_factory=list)
    metadata: Dict[str, Any] = Field(default_factory=dict)


class ReviewApplyResult(MemoryModel):
    plan: ReviewActionPlan
    applied: bool
    message: str
    target_type: str
    target_id: str
    metadata: Dict[str, Any] = Field(default_factory=dict)


class ReviewEvent(MemoryModel):
    id: str
    event_type: str
    target_type: str
    target_id: str
    created_at: str
    review_item_id: Optional[str] = None
    plan_id: Optional[str] = None
    status: Optional[str] = None
    metadata: Dict[str, Any] = Field(default_factory=dict)


class ReviewAuditSummary(MemoryModel):
    total_events: int = 0
    event_type_counts: Dict[str, int] = Field(default_factory=dict)
    target_type_counts: Dict[str, int] = Field(default_factory=dict)
    status_counts: Dict[str, int] = Field(default_factory=dict)
    pending_review_items: int = 0
    reviewed_items: int = 0
    action_plan_count: int = 0
    high_risk_plan_count: int = 0
    second_confirmation_required_count: int = 0
    applied_action_count: int = 0
    unapplied_action_plan_count: int = 0
    recent_events: List[ReviewEvent] = Field(default_factory=list)
    metadata: Dict[str, Any] = Field(default_factory=dict)


class LLMConfig(MemoryModel):
    base_url: str
    api_key: str
    model: str
    timeout_seconds: int = 60


class EmbeddingConfig(MemoryModel):
    base_url: str
    api_key: str
    model: str
    timeout_seconds: int = 60


class RecallQuery(MemoryModel):
    text: str
    top_k: int = 10
    preferred_layers: List[MemoryLayer] = Field(default_factory=list)
    session_id: Optional[str] = None
    statuses: List[MemoryStatus] = Field(default_factory=list)
    metadata: Dict[str, Any] = Field(default_factory=dict)


class RecallCandidate(MemoryModel):
    memory: MemoryUnit
    score: ScoreBreakdown
    matched_by: List[str] = Field(default_factory=list)
    explanation: Optional[str] = None


class ActiveWindowDecision(MemoryModel):
    memory_id: str
    selected: bool
    reason: str
    layer: MemoryLayer
    lifecycle_state: LifecycleState
    status: MemoryStatus
    summary: Optional[str] = None
    base_score: Optional[float] = None
    active_score: Optional[float] = None
    resident_score: float = 0.0
    task_affinity: Optional[float] = None
    matched_by: List[str] = Field(default_factory=list)
    metadata: Dict[str, Any] = Field(default_factory=dict)


class ActiveWindowReport(MemoryModel):
    candidates: List[RecallCandidate] = Field(default_factory=list)
    decisions: List[ActiveWindowDecision] = Field(default_factory=list)
    selected_count: int = 0
    filtered_count: int = 0
    metadata: Dict[str, Any] = Field(default_factory=dict)


class ContextItem(MemoryModel):
    band: ContextBand
    content: str
    source_memory_ids: List[str] = Field(default_factory=list)
    retrieval_marker: Optional[str] = None
    metadata: Dict[str, Any] = Field(default_factory=dict)


class ContextBundle(MemoryModel):
    items: List[ContextItem]
    token_budget: Optional[int] = None
    metadata: Dict[str, Any] = Field(default_factory=dict)


class ContextFusionDecision(MemoryModel):
    memory_id: str
    selected: bool
    reason: str
    sources: List[str] = Field(default_factory=list)
    merged_duplicate: bool = False
    retrieval_score: Optional[float] = None
    resident_score: float = 0.0
    band: Optional[ContextBand] = None
    retrieval_marker: Optional[str] = None
    metadata: Dict[str, Any] = Field(default_factory=dict)


class ContextFusionReport(MemoryModel):
    bundle: ContextBundle
    decisions: List[ContextFusionDecision] = Field(default_factory=list)
    selected_count: int = 0
    filtered_count: int = 0
    duplicate_candidate_count: int = 0
    metadata: Dict[str, Any] = Field(default_factory=dict)


class ContextFusionComparisonReport(MemoryModel):
    baseline_bundle: ContextBundle
    fused_report: ContextFusionReport
    baseline_memory_ids: List[str] = Field(default_factory=list)
    fused_memory_ids: List[str] = Field(default_factory=list)
    shared_memory_ids: List[str] = Field(default_factory=list)
    baseline_only_memory_ids: List[str] = Field(default_factory=list)
    fused_only_memory_ids: List[str] = Field(default_factory=list)
    metadata: Dict[str, Any] = Field(default_factory=dict)


class ContextFusionBatchComparisonItem(MemoryModel):
    query: str
    report: ContextFusionComparisonReport


class ContextFusionBatchComparisonReport(MemoryModel):
    items: List[ContextFusionBatchComparisonItem] = Field(default_factory=list)
    query_count: int = 0
    total_baseline_included: int = 0
    total_fused_included: int = 0
    total_shared: int = 0
    total_baseline_only: int = 0
    total_fused_only: int = 0
    baseline_only_memory_counts: Dict[str, int] = Field(default_factory=dict)
    fused_only_memory_counts: Dict[str, int] = Field(default_factory=dict)
    metadata: Dict[str, Any] = Field(default_factory=dict)
