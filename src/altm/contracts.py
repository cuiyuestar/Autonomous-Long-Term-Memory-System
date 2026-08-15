"""Core contracts shared by storage, retrieval, lifecycle, and adapters."""

from __future__ import annotations

from enum import Enum
from typing import Any

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


class MemoryVisibility(str, Enum):
    AGENT = "agent"
    USER_WORKSPACE = "user_workspace"


class SceneType(str, Enum):
    PROJECT = "project"
    TASK = "task"
    TOPIC = "topic"
    RELATIONSHIP = "relationship"
    WORKFLOW = "workflow"


class PersonaStatus(str, Enum):
    OBSERVING = "observing"
    ACTIVE = "active"
    SUPERSEDED = "superseded"


class NliRelation(str, Enum):
    ENTAILS = "entails"
    CONTRADICTS = "contradicts"
    NEUTRAL = "neutral"


class GraphNodeType(str, Enum):
    USER = "user"
    AGENT = "agent"
    SESSION = "session"
    EVENT = "event"
    ENTITY = "entity"
    TASK = "task"
    INTENT = "intent"
    TIME = "time"
    SCENE = "scene"
    PERSONA = "persona"


class GraphEdgeType(str, Enum):
    PARTICIPATES_IN = "participates_in"
    DERIVED_FROM = "derived_from"
    SUPPORTS = "supports"
    CONFLICTS = "conflicts"
    RELATED_TO = "related_to"
    CAUSES = "causes"
    TRIGGERS = "triggers"
    PART_OF = "part_of"
    SEQUENTIAL = "sequential"
    DEADLINE_FOR = "deadline_for"
    SUPERSEDES = "supersedes"
    HAS_INTENT = "has_intent"
    OCCURS_AT = "occurs_at"


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


class MemoryScope(MemoryModel):
    tenant_id: str = "local"
    workspace_id: str = "default"
    user_id: str = "default"
    agent_id: str = "default"

    @field_validator("tenant_id", "workspace_id", "user_id", "agent_id")
    @classmethod
    def scope_part_must_be_valid(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("memory scope identifiers must not be empty")
        if len(normalized) > 128:
            raise ValueError("memory scope identifiers must not exceed 128 characters")
        return normalized

    def key_parts(self) -> tuple[str, str, str, str]:
        return (self.tenant_id, self.workspace_id, self.user_id, self.agent_id)


class FallbackLocator(MemoryModel):
    session_id: str | None = None
    message_ids: list[str] = Field(default_factory=list)
    time_range: tuple[str, str] | None = None
    topic_tags: list[str] = Field(default_factory=list)
    text_hash: str | None = None
    excerpt: str | None = None


class EvidenceRef(MemoryModel):
    target_id: str
    target_layer: MemoryLayer
    relation: EvidenceRelation
    confidence: float = Field(ge=0.0, le=1.0)
    fallback_locator: FallbackLocator | None = None


class ScoreBreakdown(MemoryModel):
    resident_score: float = 0.0
    retrieval_score: float | None = None
    structural: float = 0.0
    recency: float = 0.0
    access: float = 0.0
    semantic: float | None = None
    task_affinity: float | None = None
    urgency: float | None = None
    evidence_quality: float = 0.0


class LifecycleMeta(MemoryModel):
    age: int = 0
    protection_tier: int = Field(default=1, ge=1, le=5)
    compression_tier: int = Field(default=0, ge=0, le=4)
    observation_until: str | None = None
    demotion_candidate_since: str | None = None
    promotion_candidate_since: str | None = None


class MemoryUnit(MemoryModel):
    id: str
    scope: MemoryScope = Field(default_factory=MemoryScope)
    visibility: MemoryVisibility = MemoryVisibility.AGENT
    layer: MemoryLayer
    lifecycle_state: LifecycleState
    status: MemoryStatus
    content: str
    content_hash: str
    created_at: str
    updated_at: str
    summary: str | None = None
    last_accessed_at: str | None = None
    access_count: int = 0
    useful_access_count: int = 0
    score: ScoreBreakdown = Field(default_factory=ScoreBreakdown)
    lifecycle: LifecycleMeta = Field(default_factory=LifecycleMeta)
    evidence_refs: list[EvidenceRef] = Field(
        default_factory=lambda: list[EvidenceRef]()
    )
    graph_refs: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)


class CaptureInput(MemoryModel):
    session_id: str
    content: str
    scope: MemoryScope = Field(default_factory=MemoryScope)
    role: MessageRole = MessageRole.USER
    message_id: str | None = None
    created_at: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("content")
    @classmethod
    def content_must_not_be_empty(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("capture content must not be empty")
        return value


class ContextCapsule(MemoryModel):
    id: str
    title: str
    time_range: tuple[str, str]
    session_id: str
    source_message_ids: list[str]
    task_goal: str | None = None
    local_context: str
    key_turns: list[str] = Field(default_factory=list)
    decisions_mentioned: list[str] = Field(default_factory=list)
    unresolved_questions: list[str] = Field(default_factory=list)
    emotional_or_pragmatic_tone: str | None = None
    topic_tags: list[str] = Field(default_factory=list)
    confidence: float = Field(default=0.5, ge=0.0, le=1.0)


class L2Atom(MemoryModel):
    id: str
    atom_type: L2AtomType
    text: str
    subject: str | None = None
    predicate: str | None = None
    object: str | None = None
    scope: str | None = None
    confidence: float = Field(default=0.5, ge=0.0, le=1.0)
    extraction_reason: str
    source_memory_id: str
    review_status: ReviewStatus = ReviewStatus.PENDING
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("text")
    @classmethod
    def text_must_not_be_empty(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("L2 atom text must not be empty")
        return value


class L2ExtractionResult(MemoryModel):
    atoms: list[L2Atom] = Field(default_factory=lambda: list[L2Atom]())


class GraphNodeSpec(MemoryModel):
    local_id: str
    node_type: GraphNodeType
    name: str
    canonical_key: str
    memory_unit_id: str | None = None
    attributes: dict[str, Any] = Field(default_factory=dict)


class GraphEdgeSpec(MemoryModel):
    source_local_id: str
    target_local_id: str
    edge_type: GraphEdgeType
    confidence: float = Field(ge=0.0, le=1.0)
    attributes: dict[str, Any] = Field(default_factory=dict)


class GraphExtraction(MemoryModel):
    nodes: list[GraphNodeSpec] = Field(
        default_factory=lambda: list[GraphNodeSpec]()
    )
    edges: list[GraphEdgeSpec] = Field(
        default_factory=lambda: list[GraphEdgeSpec]()
    )
    evidence_memory_ids: list[str] = Field(default_factory=list)
    confidence: float = Field(ge=0.0, le=1.0)
    metadata: dict[str, Any] = Field(default_factory=dict)


class SceneBlock(MemoryModel):
    id: str
    title: str
    scene_type: SceneType
    summary: str
    active_facts: list[str] = Field(default_factory=list)
    historical_facts: list[str] = Field(default_factory=list)
    open_questions: list[str] = Field(default_factory=list)
    known_risks: list[str] = Field(default_factory=list)
    source_memory_ids: list[str] = Field(default_factory=list)
    source_session_ids: list[str] = Field(default_factory=list)
    confidence: float = Field(ge=0.0, le=1.0)
    boundary_risk: float = Field(ge=0.0, le=1.0)
    observation_cycles: int = Field(default=0, ge=0)
    metadata: dict[str, Any] = Field(default_factory=dict)


class PersonaFacet(MemoryModel):
    id: str
    facet_key: str
    facet_type: str
    statement: str
    workspace_scope: str
    confidence: float = Field(ge=0.0, le=1.0)
    stability_score: float = Field(ge=0.0, le=1.0)
    status: PersonaStatus = PersonaStatus.OBSERVING
    source_memory_ids: list[str] = Field(default_factory=list)
    source_agent_ids: list[str] = Field(default_factory=list)
    counter_evidence_memory_ids: list[str] = Field(default_factory=list)
    first_observed_at: str
    last_observed_at: str
    observation_cycles: int = Field(default=0, ge=0)
    metadata: dict[str, Any] = Field(default_factory=dict)


class SemanticEvaluation(MemoryModel):
    evaluator: str
    task: str
    label: str
    score: float = Field(ge=0.0, le=1.0)
    reason: str
    model: str | None = None
    evidence_memory_ids: list[str] = Field(default_factory=list)
    output: dict[str, Any] = Field(default_factory=dict)


class SemanticGateResult(MemoryModel):
    decision: str
    confidence: float = Field(ge=0.0, le=1.0)
    reason: str
    evaluations: list[SemanticEvaluation] = Field(
        default_factory=lambda: list[SemanticEvaluation]()
    )
    metadata: dict[str, Any] = Field(default_factory=dict)


class ReviewQueueItem(MemoryModel):
    id: str
    kind: ReviewItemKind
    target_type: str
    target_id: str
    review_status: ReviewStatus = ReviewStatus.PENDING
    title: str
    summary: str | None = None
    source_memory_ids: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)


class ReviewActionPlan(MemoryModel):
    id: str
    review_item_id: str
    action_type: ReviewActionType
    target_type: str
    target_id: str
    risk: ReviewActionRisk
    requires_second_confirmation: bool = False
    description: str
    proposed_changes: dict[str, Any] = Field(default_factory=dict)
    source_memory_ids: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)


class ReviewApplyResult(MemoryModel):
    plan: ReviewActionPlan
    applied: bool
    message: str
    target_type: str
    target_id: str
    metadata: dict[str, Any] = Field(default_factory=dict)


class ReviewEvent(MemoryModel):
    id: str
    event_type: str
    target_type: str
    target_id: str
    created_at: str
    review_item_id: str | None = None
    plan_id: str | None = None
    status: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class ReviewAuditSummary(MemoryModel):
    total_events: int = 0
    event_type_counts: dict[str, int] = Field(default_factory=dict)
    target_type_counts: dict[str, int] = Field(default_factory=dict)
    status_counts: dict[str, int] = Field(default_factory=dict)
    pending_review_items: int = 0
    reviewed_items: int = 0
    action_plan_count: int = 0
    high_risk_plan_count: int = 0
    second_confirmation_required_count: int = 0
    applied_action_count: int = 0
    unapplied_action_plan_count: int = 0
    recent_events: list[ReviewEvent] = Field(
        default_factory=lambda: list[ReviewEvent]()
    )
    metadata: dict[str, Any] = Field(default_factory=dict)


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
    batch_size: int = Field(default=10, ge=1)


class RecallQuery(MemoryModel):
    text: str
    top_k: int = 10
    scope: MemoryScope | None = None
    preferred_layers: list[MemoryLayer] = Field(
        default_factory=lambda: list[MemoryLayer]()
    )
    session_id: str | None = None
    statuses: list[MemoryStatus] = Field(
        default_factory=lambda: list[MemoryStatus]()
    )
    metadata: dict[str, Any] = Field(default_factory=dict)


class RecallCandidate(MemoryModel):
    memory: MemoryUnit
    score: ScoreBreakdown
    matched_by: list[str] = Field(default_factory=list)
    explanation: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class ActiveWindowDecision(MemoryModel):
    memory_id: str
    selected: bool
    reason: str
    layer: MemoryLayer
    lifecycle_state: LifecycleState
    status: MemoryStatus
    summary: str | None = None
    base_score: float | None = None
    active_score: float | None = None
    resident_score: float = 0.0
    task_affinity: float | None = None
    matched_by: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)


class ActiveWindowReport(MemoryModel):
    candidates: list[RecallCandidate] = Field(
        default_factory=lambda: list[RecallCandidate]()
    )
    decisions: list[ActiveWindowDecision] = Field(
        default_factory=lambda: list[ActiveWindowDecision]()
    )
    selected_count: int = 0
    filtered_count: int = 0
    metadata: dict[str, Any] = Field(default_factory=dict)


class ContextItem(MemoryModel):
    band: ContextBand
    content: str
    source_memory_ids: list[str] = Field(default_factory=list)
    retrieval_marker: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class ContextBundle(MemoryModel):
    items: list[ContextItem]
    token_budget: int | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class PreparedTurn(MemoryModel):
    cycle_id: str
    scope: MemoryScope
    session_id: str
    turn_id: str
    user_memory_id: str
    query: str
    context: ContextBundle
    enqueued_job_ids: list[str] = Field(default_factory=list)
    status: str = "prepared"
    metadata: dict[str, Any] = Field(default_factory=dict)


class CommittedTurn(MemoryModel):
    cycle_id: str
    scope: MemoryScope
    session_id: str
    turn_id: str
    assistant_memory_id: str
    cited_memory_ids: list[str] = Field(default_factory=list)
    enqueued_job_ids: list[str] = Field(default_factory=list)
    status: str = "committed"
    metadata: dict[str, Any] = Field(default_factory=dict)


class AbortedTurn(MemoryModel):
    cycle_id: str
    scope: MemoryScope
    session_id: str
    turn_id: str
    reason: str
    status: str = "aborted"
    metadata: dict[str, Any] = Field(default_factory=dict)


class ContextFusionDecision(MemoryModel):
    memory_id: str
    selected: bool
    reason: str
    sources: list[str] = Field(default_factory=list)
    merged_duplicate: bool = False
    retrieval_score: float | None = None
    resident_score: float = 0.0
    band: ContextBand | None = None
    retrieval_marker: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class ContextFusionReport(MemoryModel):
    bundle: ContextBundle
    decisions: list[ContextFusionDecision] = Field(
        default_factory=lambda: list[ContextFusionDecision]()
    )
    selected_count: int = 0
    filtered_count: int = 0
    duplicate_candidate_count: int = 0
    metadata: dict[str, Any] = Field(default_factory=dict)


class ContextFusionComparisonReport(MemoryModel):
    baseline_bundle: ContextBundle
    fused_report: ContextFusionReport
    baseline_memory_ids: list[str] = Field(default_factory=list)
    fused_memory_ids: list[str] = Field(default_factory=list)
    shared_memory_ids: list[str] = Field(default_factory=list)
    baseline_only_memory_ids: list[str] = Field(default_factory=list)
    fused_only_memory_ids: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)


class ContextFusionBatchComparisonItem(MemoryModel):
    query: str
    report: ContextFusionComparisonReport


class ContextFusionBatchComparisonReport(MemoryModel):
    items: list[ContextFusionBatchComparisonItem] = Field(
        default_factory=lambda: list[ContextFusionBatchComparisonItem]()
    )
    query_count: int = 0
    total_baseline_included: int = 0
    total_fused_included: int = 0
    total_shared: int = 0
    total_baseline_only: int = 0
    total_fused_only: int = 0
    baseline_only_memory_counts: dict[str, int] = Field(default_factory=dict)
    fused_only_memory_counts: dict[str, int] = Field(default_factory=dict)
    metadata: dict[str, Any] = Field(default_factory=dict)
