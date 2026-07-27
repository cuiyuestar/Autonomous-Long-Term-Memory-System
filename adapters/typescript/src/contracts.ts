export type MemoryLayer = "L0" | "L1" | "L2" | "L3" | "L4";

export type LifecycleState = "permanent" | "long" | "short";

export type MemoryStatus =
  | "active"
  | "compressed"
  | "observing"
  | "tombstoned"
  | "deleted";

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
