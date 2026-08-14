/** Browser-safe ALTM memory-inspection wire values. */

export const ALTM_UI_API_PATH = "/plugins/altm-memory/api";

export type UiMemoryLayer = "L1" | "L2" | "L3" | "L4";

export interface UiGraphNode {
  id: string;
  node_type: string;
  canonical_key: string;
  name: string;
  memory_unit_id: string | null;
  data: Record<string, unknown>;
  evidence_memory_ids: string[];
  depth?: number;
}

export interface UiGraphEdge {
  id: string;
  source_node_id: string;
  target_node_id: string;
  edge_type: string;
  weight: number;
  confidence: number;
  metadata: Record<string, unknown>;
  source_memory_id: string | null;
  target_memory_id: string | null;
}

export interface UiGraphNeighborhood {
  nodes: UiGraphNode[];
  edges: UiGraphEdge[];
}

export interface UiEvidenceRef {
  target_id: string;
  target_layer: string;
  relation: string;
  confidence: number;
}

export interface UiMemoryUnit {
  id: string;
  layer: UiMemoryLayer;
  lifecycle_state: string;
  status: string;
  content: string;
  created_at: string;
  updated_at: string;
  summary: string | null;
  last_accessed_at: string | null;
  access_count: number;
  useful_access_count: number;
  score: Record<string, unknown>;
  lifecycle: Record<string, unknown>;
  evidence_refs: UiEvidenceRef[];
  graph_refs: string[];
  metadata: Record<string, unknown>;
}

export interface UiMemoryLayers {
  counts: Record<UiMemoryLayer, number>;
  layers: Record<UiMemoryLayer, UiMemoryUnit[]>;
}

export interface UiError {
  error: string;
}
