export type MemoryLayer = "L0" | "L1" | "L2" | "L3" | "L4";

export type LifecycleState = "permanent" | "long" | "short";

export type MemoryStatus =
  | "active"
  | "compressed"
  | "observing"
  | "tombstoned"
  | "deleted";

export type MemoryVisibility = "agent" | "user_workspace";

export type SceneType =
  | "project"
  | "task"
  | "topic"
  | "relationship"
  | "workflow";

export type PersonaStatus = "observing" | "active" | "superseded";

export type EvidenceRelation =
  | "source"
  | "derived_from"
  | "supports"
  | "conflicts"
  | "supersedes";

export type AccessSignal =
  | "candidate_hit"
  | "injected"
  | "cited_by_agent"
  | "user_confirmed"
  | "user_rejected";

export type ContextBand = "immediate" | "working" | "background" | "drilldown_marker";

export type MessageRole = "user" | "assistant" | "system" | "tool" | "other";

export type L2AtomType =
  | "preference"
  | "constraint"
  | "project_fact"
  | "decision"
  | "issue"
  | "resolution"
  | "task_state"
  | "temporal_fact"
  | "lesson";

export type ReviewStatus = "pending" | "approved" | "rejected";

export interface MemoryScope {
  tenantId: string;
  workspaceId: string;
  userId: string;
  agentId: string;
}

export interface FallbackLocator {
  sessionId?: string;
  messageIds?: string[];
  timeRange?: [string, string];
  topicTags?: string[];
  textHash?: string;
  excerpt?: string;
}

export interface EvidenceRef {
  targetId: string;
  targetLayer: MemoryLayer;
  relation: EvidenceRelation;
  confidence: number;
  fallbackLocator?: FallbackLocator;
}

export interface ScoreBreakdown {
  residentScore: number;
  retrievalScore?: number;
  structural: number;
  recency: number;
  access: number;
  semantic?: number;
  taskAffinity?: number;
  urgency?: number;
  evidenceQuality: number;
}

export interface LifecycleMeta {
  age: number;
  protectionTier: 1 | 2 | 3 | 4 | 5;
  compressionTier: 0 | 1 | 2 | 3 | 4;
  observationUntil?: string;
  demotionCandidateSince?: string;
  promotionCandidateSince?: string;
}

export interface MemoryUnit {
  id: string;
  scope: MemoryScope;
  visibility: MemoryVisibility;
  layer: MemoryLayer;
  lifecycleState: LifecycleState;
  status: MemoryStatus;
  content: string;
  contentHash: string;
  createdAt: string;
  updatedAt: string;
  summary?: string;
  lastAccessedAt?: string;
  accessCount: number;
  usefulAccessCount: number;
  score: ScoreBreakdown;
  lifecycle: LifecycleMeta;
  evidenceRefs: EvidenceRef[];
  graphRefs: string[];
  metadata: Record<string, unknown>;
}

export interface CaptureInput {
  sessionId: string;
  content: string;
  role: MessageRole;
  messageId?: string;
  createdAt?: string;
  metadata: Record<string, unknown>;
}

export interface ContextCapsule {
  id: string;
  title: string;
  timeRange: [string, string];
  sessionId: string;
  sourceMessageIds: string[];
  taskGoal?: string;
  localContext: string;
  keyTurns: string[];
  decisionsMentioned: string[];
  unresolvedQuestions: string[];
  emotionalOrPragmaticTone?: string;
  topicTags: string[];
  confidence: number;
}

export interface L2Atom {
  id: string;
  atomType: L2AtomType;
  text: string;
  subject?: string;
  predicate?: string;
  object?: string;
  scope?: string;
  confidence: number;
  extractionReason: string;
  sourceMemoryId: string;
  reviewStatus: ReviewStatus;
  metadata: Record<string, unknown>;
}

export interface LLMConfig {
  baseUrl: string;
  apiKey: string;
  model: string;
  timeoutSeconds: number;
}

export interface RecallQuery {
  text: string;
  topK: number;
  preferredLayers: MemoryLayer[];
  sessionId?: string;
  statuses: MemoryStatus[];
  metadata: Record<string, unknown>;
}

export interface RecallCandidate {
  memory: MemoryUnit;
  score: ScoreBreakdown;
  matchedBy: string[];
  explanation?: string;
  metadata: Record<string, unknown>;
}

export interface ContextItem {
  band: ContextBand;
  content: string;
  sourceMemoryIds: string[];
  retrievalMarker?: string;
  metadata: Record<string, unknown>;
}

export interface ContextBundle {
  items: ContextItem[];
  tokenBudget?: number;
  metadata: Record<string, unknown>;
}

export interface PrepareTurnInput {
  scope: MemoryScope;
  sessionId: string;
  turnId: string;
  content: string;
  messageId?: string;
  query?: string;
  tokenBudget?: number;
  recallLimit?: number;
  activeWindowMode?: string;
  activeLimit?: number;
  strictSession?: boolean;
}

export interface PreparedTurn {
  cycleId: string;
  scope: MemoryScope;
  sessionId: string;
  turnId: string;
  userMemoryId: string;
  query: string;
  context: ContextBundle;
  enqueuedJobIds: string[];
  status: string;
  metadata: Record<string, unknown>;
}

export interface CommitTurnInput {
  scope: MemoryScope;
  cycleId: string;
  assistantContent: string;
  citedMemoryIds?: string[];
  assistantMessageId?: string;
}

export interface CommittedTurn {
  cycleId: string;
  scope: MemoryScope;
  sessionId: string;
  turnId: string;
  assistantMemoryId: string;
  citedMemoryIds: string[];
  enqueuedJobIds: string[];
  status: string;
  metadata: Record<string, unknown>;
}

export interface AbortTurnInput {
  scope: MemoryScope;
  cycleId: string;
  reason: string;
}

export interface AbortedTurn {
  cycleId: string;
  scope: MemoryScope;
  sessionId: string;
  turnId: string;
  reason: string;
  status: string;
  metadata: Record<string, unknown>;
}

export interface SceneBlock {
  id: string;
  title: string;
  sceneType: SceneType;
  summary: string;
  activeFacts: string[];
  historicalFacts: string[];
  openQuestions: string[];
  knownRisks: string[];
  sourceMemoryIds: string[];
  sourceSessionIds: string[];
  confidence: number;
  boundaryRisk: number;
  observationCycles: number;
  metadata: Record<string, unknown>;
}

export interface PersonaFacet {
  id: string;
  facetKey: string;
  facetType: string;
  statement: string;
  workspaceScope: string;
  confidence: number;
  stabilityScore: number;
  status: PersonaStatus;
  sourceMemoryIds: string[];
  sourceAgentIds: string[];
  counterEvidenceMemoryIds: string[];
  firstObservedAt: string;
  lastObservedAt: string;
  observationCycles: number;
  metadata: Record<string, unknown>;
}

export interface SemanticEvaluation {
  evaluator: string;
  task: string;
  label: string;
  score: number;
  reason: string;
  model?: string;
  evidenceMemoryIds: string[];
  output: Record<string, unknown>;
}

export interface SemanticGateResult {
  decision: string;
  confidence: number;
  reason: string;
  evaluations: SemanticEvaluation[];
  metadata: Record<string, unknown>;
}
