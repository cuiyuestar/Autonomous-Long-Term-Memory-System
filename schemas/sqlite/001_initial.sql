PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS memory_units (
  id TEXT PRIMARY KEY,
  tenant_id TEXT NOT NULL DEFAULT 'local',
  workspace_id TEXT NOT NULL DEFAULT 'default',
  user_id TEXT NOT NULL DEFAULT 'default',
  agent_id TEXT NOT NULL DEFAULT 'default',
  visibility TEXT NOT NULL DEFAULT 'agent' CHECK (visibility IN ('agent', 'user_workspace')),
  layer TEXT NOT NULL CHECK (layer IN ('L0', 'L1', 'L2', 'L3', 'L4')),
  lifecycle_state TEXT NOT NULL CHECK (lifecycle_state IN ('permanent', 'long', 'short')),
  status TEXT NOT NULL CHECK (status IN ('active', 'compressed', 'observing', 'tombstoned', 'deleted')),
  content TEXT NOT NULL,
  content_hash TEXT NOT NULL,
  summary TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  last_accessed_at TEXT,
  access_count INTEGER NOT NULL DEFAULT 0,
  useful_access_count INTEGER NOT NULL DEFAULT 0,
  resident_score REAL NOT NULL DEFAULT 0,
  structural_score REAL NOT NULL DEFAULT 0,
  recency_score REAL NOT NULL DEFAULT 0,
  access_score REAL NOT NULL DEFAULT 0,
  evidence_quality_score REAL NOT NULL DEFAULT 0,
  lifecycle_age INTEGER NOT NULL DEFAULT 0,
  protection_tier INTEGER NOT NULL DEFAULT 1 CHECK (protection_tier BETWEEN 1 AND 5),
  compression_tier INTEGER NOT NULL DEFAULT 0 CHECK (compression_tier BETWEEN 0 AND 4),
  observation_until TEXT,
  promotion_candidate_since TEXT,
  demotion_candidate_since TEXT,
  metadata_json TEXT NOT NULL DEFAULT '{}'
);

CREATE INDEX IF NOT EXISTS idx_memory_units_layer ON memory_units(layer);
CREATE INDEX IF NOT EXISTS idx_memory_units_lifecycle ON memory_units(lifecycle_state, status);
CREATE INDEX IF NOT EXISTS idx_memory_units_updated_at ON memory_units(updated_at);

CREATE VIRTUAL TABLE IF NOT EXISTS memory_units_fts USING fts5(
  memory_id UNINDEXED,
  layer UNINDEXED,
  content,
  summary,
  tokenize = 'unicode61'
);

CREATE VIRTUAL TABLE IF NOT EXISTS memory_units_fts_trigram USING fts5(
  memory_id UNINDEXED,
  layer UNINDEXED,
  content,
  summary,
  tokenize = 'trigram'
);

CREATE TABLE IF NOT EXISTS l1_context_capsules (
  id TEXT PRIMARY KEY,
  memory_unit_id TEXT NOT NULL,
  title TEXT NOT NULL,
  session_id TEXT NOT NULL,
  time_range_start TEXT NOT NULL,
  time_range_end TEXT NOT NULL,
  source_message_ids_json TEXT NOT NULL DEFAULT '[]',
  task_goal TEXT,
  local_context TEXT NOT NULL,
  key_turns_json TEXT NOT NULL DEFAULT '[]',
  decisions_mentioned_json TEXT NOT NULL DEFAULT '[]',
  unresolved_questions_json TEXT NOT NULL DEFAULT '[]',
  topic_tags_json TEXT NOT NULL DEFAULT '[]',
  confidence REAL NOT NULL CHECK (confidence >= 0 AND confidence <= 1),
  created_at TEXT NOT NULL,
  FOREIGN KEY (memory_unit_id) REFERENCES memory_units(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_l1_context_capsules_session ON l1_context_capsules(session_id);
CREATE INDEX IF NOT EXISTS idx_l1_context_capsules_memory ON l1_context_capsules(memory_unit_id);

CREATE TABLE IF NOT EXISTS memory_embeddings (
  memory_unit_id TEXT NOT NULL,
  embedding_model TEXT NOT NULL,
  content_hash TEXT NOT NULL,
  dimension INTEGER NOT NULL,
  vector_json TEXT NOT NULL,
  created_at TEXT NOT NULL,
  PRIMARY KEY (memory_unit_id, embedding_model),
  FOREIGN KEY (memory_unit_id) REFERENCES memory_units(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_memory_embeddings_model ON memory_embeddings(embedding_model);
CREATE INDEX IF NOT EXISTS idx_memory_embeddings_hash ON memory_embeddings(content_hash);

CREATE TABLE IF NOT EXISTS vector_index_registry (
  embedding_model TEXT PRIMARY KEY,
  table_name TEXT NOT NULL UNIQUE,
  dimension INTEGER NOT NULL,
  backend TEXT NOT NULL,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS evidence_refs (
  id TEXT PRIMARY KEY,
  memory_unit_id TEXT NOT NULL,
  target_id TEXT NOT NULL,
  target_layer TEXT NOT NULL CHECK (target_layer IN ('L0', 'L1', 'L2', 'L3', 'L4')),
  relation TEXT NOT NULL CHECK (
    relation IN ('source', 'derived_from', 'supports', 'conflicts', 'supersedes')
  ),
  confidence REAL NOT NULL CHECK (confidence >= 0 AND confidence <= 1),
  fallback_locator_json TEXT NOT NULL DEFAULT '{}',
  created_at TEXT NOT NULL,
  FOREIGN KEY (memory_unit_id) REFERENCES memory_units(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_evidence_refs_memory_unit ON evidence_refs(memory_unit_id);
CREATE INDEX IF NOT EXISTS idx_evidence_refs_target ON evidence_refs(target_layer, target_id);

CREATE TABLE IF NOT EXISTS l2_preferences (
  id TEXT PRIMARY KEY,
  memory_unit_id TEXT NOT NULL,
  text TEXT NOT NULL,
  subject TEXT,
  predicate TEXT,
  object TEXT,
  scope TEXT,
  confidence REAL NOT NULL CHECK (confidence >= 0 AND confidence <= 1),
  extraction_reason TEXT NOT NULL,
  review_status TEXT NOT NULL CHECK (review_status IN ('pending', 'approved', 'rejected')),
  metadata_json TEXT NOT NULL DEFAULT '{}',
  created_at TEXT NOT NULL,
  FOREIGN KEY (memory_unit_id) REFERENCES memory_units(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS l2_constraints (
  id TEXT PRIMARY KEY,
  memory_unit_id TEXT NOT NULL,
  text TEXT NOT NULL,
  subject TEXT,
  predicate TEXT,
  object TEXT,
  scope TEXT,
  confidence REAL NOT NULL CHECK (confidence >= 0 AND confidence <= 1),
  extraction_reason TEXT NOT NULL,
  review_status TEXT NOT NULL CHECK (review_status IN ('pending', 'approved', 'rejected')),
  metadata_json TEXT NOT NULL DEFAULT '{}',
  created_at TEXT NOT NULL,
  FOREIGN KEY (memory_unit_id) REFERENCES memory_units(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS l2_project_facts (
  id TEXT PRIMARY KEY,
  memory_unit_id TEXT NOT NULL,
  text TEXT NOT NULL,
  subject TEXT,
  predicate TEXT,
  object TEXT,
  scope TEXT,
  confidence REAL NOT NULL CHECK (confidence >= 0 AND confidence <= 1),
  extraction_reason TEXT NOT NULL,
  review_status TEXT NOT NULL CHECK (review_status IN ('pending', 'approved', 'rejected')),
  metadata_json TEXT NOT NULL DEFAULT '{}',
  created_at TEXT NOT NULL,
  FOREIGN KEY (memory_unit_id) REFERENCES memory_units(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS l2_decisions (
  id TEXT PRIMARY KEY,
  memory_unit_id TEXT NOT NULL,
  text TEXT NOT NULL,
  subject TEXT,
  predicate TEXT,
  object TEXT,
  scope TEXT,
  confidence REAL NOT NULL CHECK (confidence >= 0 AND confidence <= 1),
  extraction_reason TEXT NOT NULL,
  review_status TEXT NOT NULL CHECK (review_status IN ('pending', 'approved', 'rejected')),
  metadata_json TEXT NOT NULL DEFAULT '{}',
  created_at TEXT NOT NULL,
  FOREIGN KEY (memory_unit_id) REFERENCES memory_units(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS l2_issues (
  id TEXT PRIMARY KEY,
  memory_unit_id TEXT NOT NULL,
  text TEXT NOT NULL,
  subject TEXT,
  predicate TEXT,
  object TEXT,
  scope TEXT,
  confidence REAL NOT NULL CHECK (confidence >= 0 AND confidence <= 1),
  extraction_reason TEXT NOT NULL,
  review_status TEXT NOT NULL CHECK (review_status IN ('pending', 'approved', 'rejected')),
  metadata_json TEXT NOT NULL DEFAULT '{}',
  created_at TEXT NOT NULL,
  FOREIGN KEY (memory_unit_id) REFERENCES memory_units(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS l2_resolutions (
  id TEXT PRIMARY KEY,
  memory_unit_id TEXT NOT NULL,
  text TEXT NOT NULL,
  subject TEXT,
  predicate TEXT,
  object TEXT,
  scope TEXT,
  confidence REAL NOT NULL CHECK (confidence >= 0 AND confidence <= 1),
  extraction_reason TEXT NOT NULL,
  review_status TEXT NOT NULL CHECK (review_status IN ('pending', 'approved', 'rejected')),
  metadata_json TEXT NOT NULL DEFAULT '{}',
  created_at TEXT NOT NULL,
  FOREIGN KEY (memory_unit_id) REFERENCES memory_units(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS l2_task_states (
  id TEXT PRIMARY KEY,
  memory_unit_id TEXT NOT NULL,
  text TEXT NOT NULL,
  subject TEXT,
  predicate TEXT,
  object TEXT,
  scope TEXT,
  confidence REAL NOT NULL CHECK (confidence >= 0 AND confidence <= 1),
  extraction_reason TEXT NOT NULL,
  review_status TEXT NOT NULL CHECK (review_status IN ('pending', 'approved', 'rejected')),
  metadata_json TEXT NOT NULL DEFAULT '{}',
  created_at TEXT NOT NULL,
  FOREIGN KEY (memory_unit_id) REFERENCES memory_units(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS l2_temporal_facts (
  id TEXT PRIMARY KEY,
  memory_unit_id TEXT NOT NULL,
  text TEXT NOT NULL,
  subject TEXT,
  predicate TEXT,
  object TEXT,
  scope TEXT,
  confidence REAL NOT NULL CHECK (confidence >= 0 AND confidence <= 1),
  extraction_reason TEXT NOT NULL,
  review_status TEXT NOT NULL CHECK (review_status IN ('pending', 'approved', 'rejected')),
  metadata_json TEXT NOT NULL DEFAULT '{}',
  created_at TEXT NOT NULL,
  FOREIGN KEY (memory_unit_id) REFERENCES memory_units(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS l2_lessons (
  id TEXT PRIMARY KEY,
  memory_unit_id TEXT NOT NULL,
  text TEXT NOT NULL,
  subject TEXT,
  predicate TEXT,
  object TEXT,
  scope TEXT,
  confidence REAL NOT NULL CHECK (confidence >= 0 AND confidence <= 1),
  extraction_reason TEXT NOT NULL,
  review_status TEXT NOT NULL CHECK (review_status IN ('pending', 'approved', 'rejected')),
  metadata_json TEXT NOT NULL DEFAULT '{}',
  created_at TEXT NOT NULL,
  FOREIGN KEY (memory_unit_id) REFERENCES memory_units(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_l2_preferences_memory ON l2_preferences(memory_unit_id);
CREATE INDEX IF NOT EXISTS idx_l2_constraints_memory ON l2_constraints(memory_unit_id);
CREATE INDEX IF NOT EXISTS idx_l2_project_facts_memory ON l2_project_facts(memory_unit_id);
CREATE INDEX IF NOT EXISTS idx_l2_decisions_memory ON l2_decisions(memory_unit_id);
CREATE INDEX IF NOT EXISTS idx_l2_issues_memory ON l2_issues(memory_unit_id);
CREATE INDEX IF NOT EXISTS idx_l2_resolutions_memory ON l2_resolutions(memory_unit_id);
CREATE INDEX IF NOT EXISTS idx_l2_task_states_memory ON l2_task_states(memory_unit_id);
CREATE INDEX IF NOT EXISTS idx_l2_temporal_facts_memory ON l2_temporal_facts(memory_unit_id);
CREATE INDEX IF NOT EXISTS idx_l2_lessons_memory ON l2_lessons(memory_unit_id);

CREATE TABLE IF NOT EXISTS l3_scenes (
  id TEXT PRIMARY KEY,
  memory_unit_id TEXT NOT NULL UNIQUE,
  title TEXT NOT NULL,
  scene_type TEXT NOT NULL CHECK (
    scene_type IN ('project', 'task', 'topic', 'relationship', 'workflow')
  ),
  summary TEXT NOT NULL,
  active_facts_json TEXT NOT NULL DEFAULT '[]',
  historical_facts_json TEXT NOT NULL DEFAULT '[]',
  open_questions_json TEXT NOT NULL DEFAULT '[]',
  known_risks_json TEXT NOT NULL DEFAULT '[]',
  source_memory_ids_json TEXT NOT NULL DEFAULT '[]',
  source_session_ids_json TEXT NOT NULL DEFAULT '[]',
  confidence REAL NOT NULL CHECK (confidence >= 0 AND confidence <= 1),
  boundary_risk REAL NOT NULL CHECK (boundary_risk >= 0 AND boundary_risk <= 1),
  observation_cycles INTEGER NOT NULL DEFAULT 0,
  metadata_json TEXT NOT NULL DEFAULT '{}',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  FOREIGN KEY (memory_unit_id) REFERENCES memory_units(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_l3_scenes_type ON l3_scenes(scene_type);
CREATE INDEX IF NOT EXISTS idx_l3_scenes_confidence
  ON l3_scenes(confidence, boundary_risk);

CREATE TABLE IF NOT EXISTS l4_persona_facets (
  id TEXT PRIMARY KEY,
  memory_unit_id TEXT NOT NULL UNIQUE,
  tenant_id TEXT NOT NULL,
  workspace_id TEXT NOT NULL,
  user_id TEXT NOT NULL,
  facet_key TEXT NOT NULL,
  facet_type TEXT NOT NULL,
  statement TEXT NOT NULL,
  workspace_scope TEXT NOT NULL,
  confidence REAL NOT NULL CHECK (confidence >= 0 AND confidence <= 1),
  stability_score REAL NOT NULL CHECK (stability_score >= 0 AND stability_score <= 1),
  status TEXT NOT NULL CHECK (status IN ('observing', 'active', 'superseded')),
  source_memory_ids_json TEXT NOT NULL DEFAULT '[]',
  source_agent_ids_json TEXT NOT NULL DEFAULT '[]',
  counter_evidence_memory_ids_json TEXT NOT NULL DEFAULT '[]',
  first_observed_at TEXT NOT NULL,
  last_observed_at TEXT NOT NULL,
  observation_cycles INTEGER NOT NULL DEFAULT 0,
  metadata_json TEXT NOT NULL DEFAULT '{}',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  FOREIGN KEY (memory_unit_id) REFERENCES memory_units(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_l4_persona_user_workspace
  ON l4_persona_facets(tenant_id, workspace_id, user_id, status);
CREATE INDEX IF NOT EXISTS idx_l4_persona_facet
  ON l4_persona_facets(facet_type, status);

CREATE TABLE IF NOT EXISTS l4_persona_history (
  id TEXT PRIMARY KEY,
  persona_id TEXT NOT NULL,
  replacement_persona_id TEXT NOT NULL,
  snapshot_json TEXT NOT NULL,
  reason TEXT NOT NULL,
  confidence REAL NOT NULL CHECK (confidence >= 0 AND confidence <= 1),
  created_at TEXT NOT NULL,
  FOREIGN KEY (persona_id) REFERENCES l4_persona_facets(id) ON DELETE CASCADE,
  FOREIGN KEY (replacement_persona_id)
    REFERENCES l4_persona_facets(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_l4_persona_history_persona
  ON l4_persona_history(persona_id, created_at);

CREATE TABLE IF NOT EXISTS graph_nodes (
  id TEXT PRIMARY KEY,
  tenant_id TEXT NOT NULL DEFAULT 'local',
  workspace_id TEXT NOT NULL DEFAULT 'default',
  user_id TEXT NOT NULL DEFAULT 'default',
  agent_id TEXT NOT NULL DEFAULT 'default',
  node_type TEXT NOT NULL,
  canonical_key TEXT,
  name TEXT,
  memory_unit_id TEXT,
  data_json TEXT NOT NULL DEFAULT '{}',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  FOREIGN KEY (memory_unit_id) REFERENCES memory_units(id) ON DELETE SET NULL
);

CREATE VIRTUAL TABLE IF NOT EXISTS graph_nodes_fts USING fts5(
  node_id UNINDEXED,
  name,
  canonical_key,
  data,
  tokenize = 'unicode61 remove_diacritics 2'
);

CREATE TABLE IF NOT EXISTS graph_node_evidence (
  node_id TEXT NOT NULL,
  memory_unit_id TEXT NOT NULL,
  confidence REAL NOT NULL DEFAULT 1
    CHECK (confidence >= 0 AND confidence <= 1),
  created_at TEXT NOT NULL,
  PRIMARY KEY (node_id, memory_unit_id),
  FOREIGN KEY (node_id) REFERENCES graph_nodes(id) ON DELETE CASCADE,
  FOREIGN KEY (memory_unit_id) REFERENCES memory_units(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_graph_node_evidence_memory
  ON graph_node_evidence(memory_unit_id, node_id);

CREATE TABLE IF NOT EXISTS graph_edges (
  id TEXT PRIMARY KEY,
  source_node_id TEXT NOT NULL,
  target_node_id TEXT NOT NULL,
  edge_type TEXT NOT NULL,
  weight REAL NOT NULL DEFAULT 0.5,
  confidence REAL NOT NULL DEFAULT 1,
  metadata_json TEXT NOT NULL DEFAULT '{}',
  created_at TEXT NOT NULL,
  FOREIGN KEY (source_node_id) REFERENCES graph_nodes(id) ON DELETE CASCADE,
  FOREIGN KEY (target_node_id) REFERENCES graph_nodes(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_graph_edges_source ON graph_edges(source_node_id);
CREATE INDEX IF NOT EXISTS idx_graph_edges_target ON graph_edges(target_node_id);
CREATE INDEX IF NOT EXISTS idx_graph_edges_type ON graph_edges(edge_type);

CREATE TABLE IF NOT EXISTS graph_extractions (
  id TEXT PRIMARY KEY,
  tenant_id TEXT NOT NULL,
  workspace_id TEXT NOT NULL,
  user_id TEXT NOT NULL,
  agent_id TEXT NOT NULL,
  source_memory_ids_json TEXT NOT NULL,
  model TEXT NOT NULL,
  confidence REAL NOT NULL CHECK (confidence >= 0 AND confidence <= 1),
  node_ids_json TEXT NOT NULL DEFAULT '[]',
  edge_ids_json TEXT NOT NULL DEFAULT '[]',
  metadata_json TEXT NOT NULL DEFAULT '{}',
  created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_graph_extractions_scope
  ON graph_extractions(
    tenant_id, workspace_id, user_id, agent_id, created_at
  );

CREATE TABLE IF NOT EXISTS lifecycle_events (
  id TEXT PRIMARY KEY,
  memory_unit_id TEXT NOT NULL,
  signal TEXT NOT NULL CHECK (
    signal IN ('candidate_hit', 'injected', 'cited_by_agent', 'user_confirmed', 'user_rejected')
  ),
  strength REAL NOT NULL DEFAULT 1,
  metadata_json TEXT NOT NULL DEFAULT '{}',
  created_at TEXT NOT NULL,
  FOREIGN KEY (memory_unit_id) REFERENCES memory_units(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_lifecycle_events_memory_unit ON lifecycle_events(memory_unit_id);
CREATE INDEX IF NOT EXISTS idx_lifecycle_events_signal ON lifecycle_events(signal);

CREATE TABLE IF NOT EXISTS review_events (
  id TEXT PRIMARY KEY,
  event_type TEXT NOT NULL,
  target_type TEXT NOT NULL,
  target_id TEXT NOT NULL,
  review_item_id TEXT,
  plan_id TEXT,
  status TEXT,
  metadata_json TEXT NOT NULL DEFAULT '{}',
  created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_review_events_target ON review_events(target_type, target_id);
CREATE INDEX IF NOT EXISTS idx_review_events_type ON review_events(event_type);
CREATE INDEX IF NOT EXISTS idx_review_events_created_at ON review_events(created_at);

CREATE TABLE IF NOT EXISTS review_audit_projections (
  target_type TEXT NOT NULL,
  target_id TEXT NOT NULL,
  event_count INTEGER NOT NULL DEFAULT 0,
  review_mark_count INTEGER NOT NULL DEFAULT 0,
  review_apply_count INTEGER NOT NULL DEFAULT 0,
  last_event_id TEXT,
  last_event_type TEXT,
  last_status TEXT,
  last_review_item_id TEXT,
  last_plan_id TEXT,
  first_event_at TEXT NOT NULL,
  last_event_at TEXT NOT NULL,
  metadata_json TEXT NOT NULL DEFAULT '{}',
  PRIMARY KEY (target_type, target_id)
);

CREATE INDEX IF NOT EXISTS idx_review_audit_projections_status
  ON review_audit_projections(last_status);
CREATE INDEX IF NOT EXISTS idx_review_audit_projections_last_event_at
  ON review_audit_projections(last_event_at);

CREATE TABLE IF NOT EXISTS checkpoints (
  scope TEXT PRIMARY KEY,
  cursor TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  metadata_json TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS runtime_cycles (
  id TEXT PRIMARY KEY,
  tenant_id TEXT NOT NULL,
  workspace_id TEXT NOT NULL,
  user_id TEXT NOT NULL,
  agent_id TEXT NOT NULL,
  session_id TEXT NOT NULL,
  turn_id TEXT NOT NULL,
  user_memory_id TEXT NOT NULL,
  user_content_hash TEXT NOT NULL,
  query TEXT NOT NULL,
  context_json TEXT NOT NULL,
  context_memory_ids_json TEXT NOT NULL DEFAULT '[]',
  status TEXT NOT NULL CHECK (status IN ('prepared', 'committed', 'aborted', 'failed')),
  assistant_memory_id TEXT,
  assistant_content_hash TEXT,
  cited_memory_ids_json TEXT NOT NULL DEFAULT '[]',
  metadata_json TEXT NOT NULL DEFAULT '{}',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  UNIQUE (tenant_id, workspace_id, user_id, agent_id, session_id, turn_id),
  FOREIGN KEY (user_memory_id) REFERENCES memory_units(id) ON DELETE RESTRICT,
  FOREIGN KEY (assistant_memory_id) REFERENCES memory_units(id) ON DELETE RESTRICT
);

CREATE INDEX IF NOT EXISTS idx_runtime_cycles_scope_session
  ON runtime_cycles(tenant_id, workspace_id, user_id, agent_id, session_id, created_at);
CREATE INDEX IF NOT EXISTS idx_runtime_cycles_status
  ON runtime_cycles(status, updated_at);

CREATE TABLE IF NOT EXISTS runtime_jobs (
  id TEXT PRIMARY KEY,
  tenant_id TEXT NOT NULL,
  workspace_id TEXT NOT NULL,
  user_id TEXT NOT NULL,
  agent_id TEXT NOT NULL,
  session_id TEXT,
  job_type TEXT NOT NULL,
  dedupe_key TEXT NOT NULL,
  payload_json TEXT NOT NULL DEFAULT '{}',
  status TEXT NOT NULL CHECK (status IN ('pending', 'running', 'completed', 'failed')),
  attempts INTEGER NOT NULL DEFAULT 0,
  max_attempts INTEGER NOT NULL DEFAULT 5,
  available_at TEXT NOT NULL,
  lease_owner TEXT,
  lease_until TEXT,
  last_error TEXT,
  result_json TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  UNIQUE (tenant_id, workspace_id, user_id, agent_id, job_type, dedupe_key)
);

CREATE INDEX IF NOT EXISTS idx_runtime_jobs_claim
  ON runtime_jobs(status, available_at, lease_until, created_at);
CREATE INDEX IF NOT EXISTS idx_runtime_jobs_scope
  ON runtime_jobs(tenant_id, workspace_id, user_id, agent_id, status);

CREATE TABLE IF NOT EXISTS lifecycle_age_stats (
  cycle_id TEXT NOT NULL,
  tenant_id TEXT NOT NULL,
  workspace_id TEXT NOT NULL,
  user_id TEXT NOT NULL,
  agent_id TEXT NOT NULL,
  layer TEXT NOT NULL,
  lifecycle_state TEXT NOT NULL,
  age_bucket INTEGER NOT NULL,
  memory_count INTEGER NOT NULL,
  token_estimate INTEGER NOT NULL,
  created_at TEXT NOT NULL,
  PRIMARY KEY (
    cycle_id, tenant_id, workspace_id, user_id, agent_id,
    layer, lifecycle_state, age_bucket
  )
);

CREATE INDEX IF NOT EXISTS idx_lifecycle_age_stats_scope
  ON lifecycle_age_stats(
    tenant_id, workspace_id, user_id, agent_id, created_at
  );

CREATE TABLE IF NOT EXISTS deletion_requests (
  id TEXT PRIMARY KEY,
  tenant_id TEXT NOT NULL,
  workspace_id TEXT NOT NULL,
  user_id TEXT NOT NULL,
  agent_id TEXT NOT NULL,
  target_type TEXT NOT NULL,
  target_id TEXT NOT NULL,
  mode TEXT NOT NULL CHECK (mode IN ('tombstone', 'physical')),
  reason TEXT NOT NULL,
  status TEXT NOT NULL CHECK (status IN ('requested', 'applied', 'failed')),
  metadata_json TEXT NOT NULL DEFAULT '{}',
  created_at TEXT NOT NULL,
  applied_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_deletion_requests_scope
  ON deletion_requests(
    tenant_id, workspace_id, user_id, agent_id, status, created_at
  );

CREATE TABLE IF NOT EXISTS ccr_entries (
  marker TEXT PRIMARY KEY,
  memory_unit_id TEXT NOT NULL,
  content_hash TEXT NOT NULL,
  content_type TEXT NOT NULL,
  strategy TEXT NOT NULL,
  original_text TEXT NOT NULL,
  compressed_text TEXT NOT NULL,
  original_token_estimate INTEGER NOT NULL,
  compressed_token_estimate INTEGER NOT NULL,
  metadata_json TEXT NOT NULL DEFAULT '{}',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  FOREIGN KEY (memory_unit_id) REFERENCES memory_units(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_ccr_entries_memory
  ON ccr_entries(memory_unit_id);
CREATE INDEX IF NOT EXISTS idx_ccr_entries_hash
  ON ccr_entries(content_hash);

CREATE TABLE IF NOT EXISTS tombstones (
  id TEXT PRIMARY KEY,
  target_type TEXT NOT NULL,
  target_id TEXT NOT NULL,
  reason TEXT NOT NULL,
  created_at TEXT NOT NULL,
  metadata_json TEXT NOT NULL DEFAULT '{}'
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_tombstones_target ON tombstones(target_type, target_id);
