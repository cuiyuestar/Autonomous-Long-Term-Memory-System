"""Application use cases shared by CLI and MCP adapters."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Sequence

from altm.capture import L0Recorder
from altm.config import HighRiskFlags, high_risk_flags
from altm.context import SimpleContextFusion, SimpleContextGateway
from altm.contracts import (
    AccessSignal,
    ActiveWindowReport,
    CaptureInput,
    ContextBundle,
    ContextFusionBatchComparisonItem,
    ContextFusionBatchComparisonReport,
    ContextFusionComparisonReport,
    ContextFusionReport,
    MemoryLayer,
    MemoryStatus,
    MemoryUnit,
    MessageRole,
    RecallCandidate,
    RecallQuery,
    ReviewActionPlan,
    ReviewApplyResult,
    ReviewAuditSummary,
    ReviewEvent,
    ReviewItemKind,
    ReviewQueueItem,
    ReviewStatus,
)
from altm.folding import (
    CrossSessionL3CandidateFinder,
    L4PersonaCandidateBuilder,
    L2Extractor,
    RuleBasedL1Summarizer,
    RuleBasedL3SceneBuilder,
)
from altm.governance import (
    SEMANTIC_DEDUP_MODES,
    AutonomousGovernanceEngine,
    SemanticDedupPolicy,
    SemanticDeduper,
)
from altm.lifecycle import LifecycleGovernor
from altm.llm import (
    OpenAICompatibleClient,
    OpenAICompatibleEmbeddingClient,
    embedding_config_from_env,
    llm_config_from_env,
    optional_embedding_client_from_env,
)
from altm.retrieval import (
    FTSRetrievalEngine,
    GlobalActiveWindowEngine,
    GlobalActiveWindowPolicy,
    QueryEmergenceEngine,
)
from altm.retrieval.remote_vector import EmbeddingIndexer
from altm.review import ReviewActionExecutor, ReviewActionPlanner, ReviewAuditReporter, ReviewQueue
from altm.storage import SQLiteMemoryStore


class AltmApplication:
    """Thin orchestration layer for public entry points.

    This class intentionally keeps domain behavior in the existing capture,
    folding, retrieval, lifecycle, governance, and review modules. It only
    centralizes adapter-level orchestration that was duplicated across CLI and
    MCP.
    """

    def __init__(
        self,
        db_path: str | Path,
        schema_path: str | Path | None = None,
    ) -> None:
        self.db_path = db_path
        self.schema_path = schema_path

    def store(self) -> SQLiteMemoryStore:
        sqlite_store = SQLiteMemoryStore(self.db_path, schema_path=self.schema_path)
        sqlite_store.initialize()
        return sqlite_store

    def initialize_store(self) -> None:
        self.store()

    def remember(
        self,
        session_id: str,
        content: str,
        role: str | MessageRole = MessageRole.USER,
        message_id: str | None = None,
    ) -> MemoryUnit:
        return L0Recorder(self.store()).capture(
            CaptureInput(
                session_id=session_id,
                content=content,
                role=_message_role(role),
                message_id=message_id,
            )
        )

    def fold_l1(self, session_id: str) -> Sequence[MemoryUnit]:
        return RuleBasedL1Summarizer(self.store()).fold_session(session_id)

    def extract_l2(self, session_id: str) -> Sequence[MemoryUnit]:
        return L2Extractor(
            self.store(),
            OpenAICompatibleClient(llm_config_from_env()),
        ).extract_from_session(session_id)

    def cluster_l3(
        self,
        session_id: str | None = None,
        min_group_size: int = 2,
        limit: int = 1000,
    ) -> Sequence[MemoryUnit]:
        return RuleBasedL3SceneBuilder(self.store()).build(
            session_id=session_id,
            min_group_size=min_group_size,
            limit=limit,
        )

    def cross_session_l3_candidates(
        self,
        model: str | None = None,
        threshold: float = 0.82,
        limit: int = 1000,
        dry_run: bool = False,
    ) -> dict[str, object]:
        flags = high_risk_flags()
        if not flags.enable_cross_session_l3_candidates:
            return {
                "enabled": False,
                "candidate_count": 0,
                "edge_ids": [],
                "candidates": [],
            }
        embedding_model = model or os.environ.get("ALTM_EMBEDDING_MODEL")
        if not embedding_model:
            raise RuntimeError(
                "Missing embedding model. Set ALTM_EMBEDDING_MODEL or pass model."
            )
        candidates = CrossSessionL3CandidateFinder(self.store()).find(
            embedding_model=embedding_model,
            threshold=threshold,
            limit=limit,
            dry_run=dry_run,
        )
        return {
            "enabled": True,
            "dry_run": dry_run,
            "embedding_model": embedding_model,
            "threshold": threshold,
            "candidate_count": len(candidates),
            "edge_ids": [candidate.edge_id for candidate in candidates],
            "candidates": [
                {
                    "source_memory_id": candidate.source_memory_id,
                    "target_memory_id": candidate.target_memory_id,
                    "source_session_id": candidate.source_session_id,
                    "target_session_id": candidate.target_session_id,
                    "atom_type": candidate.atom_type,
                    "similarity": candidate.similarity,
                    "edge_id": candidate.edge_id,
                }
                for candidate in candidates
            ],
        }

    def build_l4_persona_candidates(
        self,
        min_support: int = 2,
        limit: int = 1000,
        dry_run: bool = False,
    ) -> dict[str, object]:
        flags = high_risk_flags()
        if not flags.enable_l4_persona_candidates:
            return {
                "enabled": False,
                "candidate_count": 0,
                "memory_ids": [],
                "candidates": [],
            }
        candidates = L4PersonaCandidateBuilder(self.store()).build(
            min_support=min_support,
            limit=limit,
            dry_run=dry_run,
        )
        return {
            "enabled": True,
            "dry_run": dry_run,
            "min_support": min_support,
            "candidate_count": len(candidates),
            "memory_ids": [candidate.id for candidate in candidates],
            "candidates": [
                {
                    "memory_id": candidate.id,
                    "atom_type": candidate.metadata.get("atom_type"),
                    "persona_key": candidate.metadata.get("persona_key"),
                    "support_count": candidate.metadata.get("support_count"),
                    "source_memory_ids": candidate.metadata.get("source_memory_ids", []),
                    "summary": candidate.summary,
                }
                for candidate in candidates
            ],
        }

    def recall(
        self,
        query: str,
        limit: int = 10,
        layers: Sequence[str | MemoryLayer] | None = None,
        session_id: str | None = None,
        statuses: Sequence[str | MemoryStatus] | None = None,
    ) -> Sequence[RecallCandidate]:
        store = self.store()
        return FTSRetrievalEngine(store, optional_embedding_client_from_env()).recall(
            RecallQuery(
                text=query,
                top_k=limit,
                preferred_layers=_memory_layers(layers),
                session_id=session_id,
                statuses=_memory_statuses(statuses),
            )
        )

    def drilldown(self, memory_id: str) -> MemoryUnit | None:
        return self.store().get_memory_unit(memory_id)

    def build_context(
        self,
        query: str,
        token_budget: int = 1200,
        limit: int = 10,
        layers: Sequence[str | MemoryLayer] | None = None,
        session_id: str | None = None,
        statuses: Sequence[str | MemoryStatus] | None = None,
        active_window_mode: str | None = None,
        active_limit: int = 5,
        candidate_limit: int | None = None,
        strict_session: bool = False,
    ) -> ContextBundle:
        flags = high_risk_flags()
        mode = _active_window_mode(active_window_mode, flags)
        if mode == "off":
            candidates = self.recall(
                query=query,
                limit=limit,
                layers=layers,
                session_id=session_id,
                statuses=statuses,
            )
            return SimpleContextGateway().assemble(candidates, token_budget=token_budget)

        store = self.store()
        active_count = min(active_limit, 2) if mode == "limited" else active_limit
        recall_candidates, active_candidates = self._fusion_candidates(
            query=query,
            recall_limit=limit,
            active_limit=active_count,
            layers=layers,
            session_id=session_id,
            statuses=statuses,
            strict_session=strict_session,
            store=store,
            flags=flags,
        )
        fusion_report = SimpleContextFusion().report(
            recall_candidates=recall_candidates,
            active_candidates=active_candidates,
            token_budget=token_budget,
            candidate_limit=candidate_limit,
        )
        bundle = fusion_report.bundle
        feedback_memory_ids: list[str] = []
        if flags.enable_active_window_lifecycle_feedback:
            feedback_memory_ids = _record_active_window_injections(
                store=store,
                report=fusion_report,
                query=query,
                mode=mode,
                session_id=session_id,
            )
        metadata = dict(bundle.metadata)
        metadata.update(
            {
                "active_window_mode": mode,
                "active_limit": active_count,
                "active_window_lifecycle_feedback": (
                    "injected" if flags.enable_active_window_lifecycle_feedback else "disabled"
                ),
                "active_window_feedback_memory_ids": feedback_memory_ids,
            }
        )
        return bundle.model_copy(update={"metadata": metadata})

    def emerge(
        self,
        query: str,
        limit: int = 10,
        seed_limit: int = 8,
        max_hops: int = 2,
        layers: Sequence[str | MemoryLayer] | None = None,
        session_id: str | None = None,
        statuses: Sequence[str | MemoryStatus] | None = None,
    ) -> Sequence[RecallCandidate]:
        store = self.store()
        direct_retriever = FTSRetrievalEngine(store, optional_embedding_client_from_env())
        return QueryEmergenceEngine(store, direct_retriever).emerge(
            RecallQuery(
                text=query,
                top_k=limit,
                preferred_layers=_memory_layers(layers),
                session_id=session_id,
                statuses=_memory_statuses(statuses),
            ),
            seed_limit=seed_limit,
            max_hops=max_hops,
        )

    def active_window(
        self,
        limit: int = 10,
        token_budget: int = 1200,
        session_id: str | None = None,
        layers: Sequence[str | MemoryLayer] | None = None,
        statuses: Sequence[str | MemoryStatus] | None = None,
        strict_session: bool = False,
    ) -> ContextBundle:
        flags = high_risk_flags()
        candidates = _active_window_engine(self.store(), flags).select(
            limit=limit,
            session_id=session_id,
            layers=_memory_layers(layers),
            statuses=_memory_statuses(statuses),
            strict_session=strict_session,
        )
        return SimpleContextGateway().assemble(candidates, token_budget=token_budget)

    def active_window_report(
        self,
        limit: int = 10,
        decision_limit: int = 100,
        session_id: str | None = None,
        layers: Sequence[str | MemoryLayer] | None = None,
        statuses: Sequence[str | MemoryStatus] | None = None,
        strict_session: bool = False,
    ) -> ActiveWindowReport:
        flags = high_risk_flags()
        return _active_window_engine(self.store(), flags).report(
            limit=limit,
            decision_limit=decision_limit,
            session_id=session_id,
            layers=_memory_layers(layers),
            statuses=_memory_statuses(statuses),
            strict_session=strict_session,
        )

    def build_fused_context(
        self,
        query: str,
        token_budget: int = 1200,
        recall_limit: int = 10,
        active_limit: int = 5,
        candidate_limit: int | None = None,
        layers: Sequence[str | MemoryLayer] | None = None,
        session_id: str | None = None,
        statuses: Sequence[str | MemoryStatus] | None = None,
        strict_session: bool = False,
    ) -> ContextBundle:
        return self._fused_report(
            query=query,
            token_budget=token_budget,
            recall_limit=recall_limit,
            active_limit=active_limit,
            candidate_limit=candidate_limit,
            layers=layers,
            session_id=session_id,
            statuses=statuses,
            strict_session=strict_session,
        ).bundle

    def build_fused_context_report(
        self,
        query: str,
        token_budget: int = 1200,
        recall_limit: int = 10,
        active_limit: int = 5,
        candidate_limit: int | None = None,
        layers: Sequence[str | MemoryLayer] | None = None,
        session_id: str | None = None,
        statuses: Sequence[str | MemoryStatus] | None = None,
        strict_session: bool = False,
    ) -> ContextFusionReport:
        return self._fused_report(
            query=query,
            token_budget=token_budget,
            recall_limit=recall_limit,
            active_limit=active_limit,
            candidate_limit=candidate_limit,
            layers=layers,
            session_id=session_id,
            statuses=statuses,
            strict_session=strict_session,
        )

    def compare_fused_context(
        self,
        query: str,
        token_budget: int = 1200,
        recall_limit: int = 10,
        active_limit: int = 5,
        candidate_limit: int | None = None,
        layers: Sequence[str | MemoryLayer] | None = None,
        session_id: str | None = None,
        statuses: Sequence[str | MemoryStatus] | None = None,
        strict_session: bool = False,
    ) -> ContextFusionComparisonReport:
        recall_candidates, active_candidates = self._fusion_candidates(
            query=query,
            recall_limit=recall_limit,
            active_limit=active_limit,
            layers=layers,
            session_id=session_id,
            statuses=statuses,
            strict_session=strict_session,
        )
        return SimpleContextFusion().compare(
            recall_candidates=recall_candidates,
            active_candidates=active_candidates,
            token_budget=token_budget,
            candidate_limit=candidate_limit,
        )

    def compare_fused_context_batch(
        self,
        queries: Sequence[str],
        token_budget: int = 1200,
        recall_limit: int = 10,
        active_limit: int = 5,
        candidate_limit: int | None = None,
        layers: Sequence[str | MemoryLayer] | None = None,
        session_id: str | None = None,
        statuses: Sequence[str | MemoryStatus] | None = None,
        strict_session: bool = False,
    ) -> ContextFusionBatchComparisonReport:
        items: list[ContextFusionBatchComparisonItem] = []
        baseline_only_counts: dict[str, int] = {}
        fused_only_counts: dict[str, int] = {}
        for query in queries:
            report = self.compare_fused_context(
                query=query,
                token_budget=token_budget,
                recall_limit=recall_limit,
                active_limit=active_limit,
                candidate_limit=candidate_limit,
                layers=layers,
                session_id=session_id,
                statuses=statuses,
                strict_session=strict_session,
            )
            items.append(ContextFusionBatchComparisonItem(query=query, report=report))
            _count_memory_ids(report.baseline_only_memory_ids, baseline_only_counts)
            _count_memory_ids(report.fused_only_memory_ids, fused_only_counts)

        return ContextFusionBatchComparisonReport(
            items=items,
            query_count=len(items),
            total_baseline_included=sum(len(item.report.baseline_memory_ids) for item in items),
            total_fused_included=sum(len(item.report.fused_memory_ids) for item in items),
            total_shared=sum(len(item.report.shared_memory_ids) for item in items),
            total_baseline_only=sum(len(item.report.baseline_only_memory_ids) for item in items),
            total_fused_only=sum(len(item.report.fused_only_memory_ids) for item in items),
            baseline_only_memory_counts=baseline_only_counts,
            fused_only_memory_counts=fused_only_counts,
            metadata={
                "comparison_strategy": "batch_baseline_build_context_vs_explicit_fusion",
                "token_budget": token_budget,
                "recall_limit": recall_limit,
                "active_limit": active_limit,
                "candidate_limit": candidate_limit,
                "recommendation": _batch_recommendation(
                    query_count=len(items),
                    total_baseline_only=sum(
                        len(item.report.baseline_only_memory_ids) for item in items
                    ),
                    total_fused_only=sum(len(item.report.fused_only_memory_ids) for item in items),
                ),
            },
        )

    def index_embeddings(self, limit: int = 100) -> dict[str, object]:
        client = OpenAICompatibleEmbeddingClient(embedding_config_from_env())
        indexed = EmbeddingIndexer(self.store(), client).index_missing(limit=limit)
        return {
            "embedding_model": client.config.model,
            "indexed_count": len(indexed),
            "memory_ids": [memory.id for memory in indexed],
        }

    def maintenance_cycle(
        self,
        model: str | None = None,
        index_embeddings: bool = False,
        embedding_limit: int = 100,
        governance_limit: int = 1000,
        semantic_threshold: float = 0.92,
        semantic_mode: str = "auto",
        l3_threshold: float = 0.82,
        persona_min_support: int = 2,
        use_autonomous_governance: bool = True,
        autonomous_model_mode: str = "auto",
        autonomous_rule_fallback: bool = True,
        apply_review_actions: bool = False,
        review_action_limit: int = 100,
        include_rejected_review_actions: bool = True,
        allow_second_confirm_review_actions: bool = False,
        dry_run: bool = False,
    ) -> dict[str, object]:
        embedding_model = model or os.environ.get("ALTM_EMBEDDING_MODEL")
        steps: dict[str, object] = {}

        if index_embeddings:
            try:
                steps["index_embeddings"] = {
                    "status": "applied",
                    "result": self.index_embeddings(limit=embedding_limit),
                }
            except RuntimeError as exc:
                steps["index_embeddings"] = {
                    "status": "degraded",
                    "error": str(exc),
                }
        else:
            steps["index_embeddings"] = {"status": "skipped", "reason": "not_requested"}

        lifecycle = self.govern_lifecycle(limit=governance_limit)
        steps["govern_lifecycle"] = {"status": "applied", "result": lifecycle}

        if embedding_model:
            steps["semantic_dedup"] = {
                "status": "applied",
                "result": self.semantic_dedup(
                    model=embedding_model,
                    threshold=semantic_threshold,
                    mode="mark-only" if use_autonomous_governance else semantic_mode,
                    dry_run=dry_run,
                ),
            }
            steps["cross_session_l3_candidates"] = {
                "status": "applied",
                "result": self.cross_session_l3_candidates(
                    model=embedding_model,
                    threshold=l3_threshold,
                    dry_run=dry_run,
                ),
            }
        else:
            steps["semantic_dedup"] = {
                "status": "skipped",
                "reason": "missing_embedding_model",
            }
            steps["cross_session_l3_candidates"] = {
                "status": "skipped",
                "reason": "missing_embedding_model",
            }

        if use_autonomous_governance:
            steps["build_l4_persona_candidates"] = {
                "status": "skipped",
                "reason": "autonomous_governance_replaces_review_candidate_builder",
            }
        else:
            steps["build_l4_persona_candidates"] = {
                "status": "applied",
                "result": self.build_l4_persona_candidates(
                    min_support=persona_min_support,
                    dry_run=dry_run,
                ),
            }
        if use_autonomous_governance:
            steps["autonomous_governance"] = {
                "status": "preview" if dry_run else "applied",
                "result": self.autonomous_governance_cycle(
                    model=embedding_model,
                    semantic_threshold=semantic_threshold,
                    l3_threshold=l3_threshold,
                    persona_min_support=persona_min_support,
                    model_mode=autonomous_model_mode,
                    rule_fallback=autonomous_rule_fallback,
                    dry_run=dry_run,
                    limit=governance_limit,
                ),
            }
        else:
            steps["autonomous_governance"] = {
                "status": "skipped",
                "reason": "not_requested",
            }
        if apply_review_actions:
            steps["apply_review_actions"] = {
                "status": "preview" if dry_run else "applied",
                "result": self.apply_review_action_plans(
                    include_rejected=include_rejected_review_actions,
                    limit=review_action_limit,
                    allow_second_confirmation=allow_second_confirm_review_actions,
                    dry_run=dry_run,
                ),
            }
        else:
            steps["apply_review_actions"] = {
                "status": "skipped",
                "reason": "compatibility_path_not_default",
            }
        audit = self.review_audit()
        steps["review_audit"] = {
            "status": "applied",
            "result": audit.model_dump(mode="json"),
        }
        return {
            "status": "complete",
            "dry_run": dry_run,
            "embedding_model": embedding_model,
            "steps": steps,
            "summary": _maintenance_summary(steps),
        }

    def autonomous_governance_cycle(
        self,
        model: str | None = None,
        semantic_threshold: float = 0.92,
        semantic_auto_merge_threshold: float = 0.97,
        semantic_auto_tombstone_threshold: float = 0.97,
        l3_threshold: float = 0.82,
        persona_min_support: int = 2,
        include_p0: bool = True,
        include_p1: bool = True,
        model_mode: str = "auto",
        rule_fallback: bool = True,
        dry_run: bool = False,
        limit: int = 1000,
    ) -> dict[str, object]:
        embedding_model = model or os.environ.get("ALTM_EMBEDDING_MODEL")
        return AutonomousGovernanceEngine(self.store()).run(
            embedding_model=embedding_model,
            semantic_threshold=semantic_threshold,
            semantic_auto_merge_threshold=semantic_auto_merge_threshold,
            semantic_auto_tombstone_threshold=semantic_auto_tombstone_threshold,
            l3_threshold=l3_threshold,
            persona_min_support=persona_min_support,
            include_p0=include_p0,
            include_p1=include_p1,
            model_mode=model_mode,
            rule_fallback=rule_fallback,
            dry_run=dry_run,
            limit=limit,
        )

    def autonomous_governance_rollback(
        self,
        target_type: str,
        target_id: str,
        reason: str = "manual_autonomous_rollback",
    ) -> dict[str, object]:
        return AutonomousGovernanceEngine(self.store()).rollback(
            target_type=target_type,
            target_id=target_id,
            reason=reason,
        )

    def feedback(self, memory_id: str, signal: str | AccessSignal) -> MemoryUnit | None:
        store = self.store()
        store.record_access_signal(memory_id, _access_signal(signal))
        return store.get_memory_unit(memory_id)

    def govern_lifecycle(
        self,
        limit: int = 1000,
        layer: str | MemoryLayer | None = None,
    ) -> dict[str, object]:
        updated = LifecycleGovernor(self.store()).run_cycle(
            limit=limit,
            layer=_optional_memory_layer(layer),
        )
        return {
            "updated_count": len(updated),
            "memory_ids": [memory.id for memory in updated],
            "promotion_candidates": [
                memory.id
                for memory in updated
                if memory.lifecycle.promotion_candidate_since is not None
            ],
            "demotion_candidates": [
                memory.id
                for memory in updated
                if memory.lifecycle.demotion_candidate_since is not None
            ],
        }

    def semantic_dedup(
        self,
        model: str | None = None,
        limit: int = 1000,
        threshold: float = 0.92,
        mode: str = "auto",
        dry_run: bool = False,
        auto_merge_threshold: float = 0.97,
        auto_tombstone_threshold: float = 0.97,
    ) -> dict[str, object]:
        embedding_model = model or os.environ.get("ALTM_EMBEDDING_MODEL")
        if not embedding_model:
            raise RuntimeError(
                "Missing embedding model. Set ALTM_EMBEDDING_MODEL or pass model."
            )

        semantic_mode = _semantic_dedup_mode(mode)
        store = self.store()
        deduper = SemanticDeduper(
            store,
            SemanticDedupPolicy(
                similarity_threshold=threshold,
                auto_merge_threshold=auto_merge_threshold,
                auto_tombstone_threshold=auto_tombstone_threshold,
            ),
        )
        candidates = deduper.mark_candidates(
            embedding_model=embedding_model,
            limit=limit,
            dry_run=dry_run,
        )
        flags = high_risk_flags()
        auto_merge_enabled = (
            flags.enable_auto_l2_semantic_merge
            if semantic_mode == "auto"
            else semantic_mode in {"auto-merge", "auto-tombstone"}
        )
        auto_tombstone_enabled = (
            flags.enable_auto_l2_tombstone
            if semantic_mode == "auto"
            else semantic_mode == "auto-tombstone"
        )
        if semantic_mode == "mark-only":
            auto_merge_enabled = False
            auto_tombstone_enabled = False
        resolutions = deduper.auto_resolve_candidates(
            candidates,
            auto_merge=auto_merge_enabled,
            auto_tombstone=auto_tombstone_enabled,
            dry_run=dry_run,
        )
        applied_resolutions = [resolution for resolution in resolutions if resolution.merged]
        preview_resolutions = [
            resolution
            for resolution in resolutions
            if bool(resolution.details.get("would_merge"))
        ]
        return {
            "candidate_count": len(candidates),
            "edge_ids": [candidate.edge_id for candidate in candidates],
            "mode": semantic_mode,
            "dry_run": dry_run,
            "threshold": threshold,
            "auto_merge_threshold": auto_merge_threshold,
            "auto_tombstone_threshold": auto_tombstone_threshold,
            "auto_merge_enabled": auto_merge_enabled,
            "auto_tombstone_enabled": auto_tombstone_enabled,
            "auto_resolution_count": len(applied_resolutions),
            "auto_resolution_attempt_count": len(resolutions),
            "would_resolution_count": len(preview_resolutions),
            "auto_resolutions": [
                {
                    "edge_id": resolution.edge_id,
                    "canonical_memory_id": resolution.canonical_memory_id,
                    "duplicate_memory_id": resolution.duplicate_memory_id,
                    "merged": resolution.merged,
                    "tombstoned": resolution.tombstoned,
                    "reason": resolution.reason,
                    "dry_run": resolution.dry_run,
                    "details": resolution.details,
                }
                for resolution in resolutions
            ],
            "candidates": [
                {
                    "source_memory_id": candidate.source_memory_id,
                    "target_memory_id": candidate.target_memory_id,
                    "atom_type": candidate.atom_type,
                    "similarity": candidate.similarity,
                    "edge_id": candidate.edge_id,
                }
                for candidate in candidates
            ],
        }

    def restore_semantic_merge(
        self,
        edge_id: str,
        reason: str = "manual_rollback",
    ) -> dict[str, object]:
        return self.store().restore_semantic_l2_merge(edge_id=edge_id, reason=reason)

    def review_queue(
        self,
        kind: str | ReviewItemKind | None = None,
        include_reviewed: bool = False,
        limit: int = 100,
    ) -> Sequence[ReviewQueueItem]:
        return ReviewQueue(self.store()).list_items(
            kind=_optional_review_item_kind(kind),
            include_reviewed=include_reviewed,
            limit=limit,
        )

    def review_mark(
        self,
        target_type: str,
        target_id: str,
        status: str | ReviewStatus,
        kind: str | ReviewItemKind | None = None,
        note: str | None = None,
    ) -> ReviewQueueItem | None:
        queue = ReviewQueue(self.store())
        review_status = _review_status(status)
        if target_type == "memory_unit":
            return queue.mark_memory(
                target_id,
                review_status,
                note=note,
                kind=_optional_review_item_kind(kind),
            )
        if target_type == "graph_edge":
            return queue.mark_graph_edge(target_id, review_status, note=note)
        raise ValueError("Unsupported review target_type: %s" % target_type)

    def review_plan(
        self,
        include_rejected: bool = False,
        limit: int = 100,
    ) -> Sequence[ReviewActionPlan]:
        return ReviewActionPlanner(self.store()).plan(
            include_rejected=include_rejected,
            limit=limit,
        )

    def review_apply(
        self,
        plan_id: str,
        confirm: bool = False,
        second_confirm: bool = False,
    ) -> ReviewApplyResult | None:
        return ReviewActionExecutor(self.store()).apply(
            plan_id,
            confirm=confirm,
            second_confirm=second_confirm,
        )

    def apply_review_action_plans(
        self,
        include_rejected: bool = True,
        limit: int = 100,
        allow_second_confirmation: bool = False,
        dry_run: bool = False,
    ) -> dict[str, object]:
        if limit <= 0:
            return _empty_review_action_application(
                include_rejected=include_rejected,
                limit=limit,
                allow_second_confirmation=allow_second_confirmation,
                dry_run=dry_run,
            )

        store = self.store()
        plans = ReviewActionPlanner(store).plan(
            include_rejected=include_rejected,
            limit=limit,
        )
        applied_plan_ids = {
            event.plan_id
            for event in store.list_review_events(
                event_type="review_apply",
                limit=max(limit * 10, 5000),
            )
            if event.plan_id is not None
        }
        executor = ReviewActionExecutor(store)
        results: list[dict[str, object]] = []
        skipped: list[dict[str, object]] = []

        for plan in plans:
            if _review_plan_already_applied(store, plan, applied_plan_ids):
                skipped.append(_skipped_review_plan(plan, "already_applied"))
                continue
            if plan.requires_second_confirmation and not allow_second_confirmation:
                skipped.append(_skipped_review_plan(plan, "requires_second_confirmation"))
                continue

            result = executor.apply(
                plan.id,
                confirm=not dry_run,
                second_confirm=plan.requires_second_confirmation
                and allow_second_confirmation
                and not dry_run,
            )
            if result is None:
                skipped.append(_skipped_review_plan(plan, "plan_not_found"))
                continue
            results.append(result.model_dump(mode="json"))

        return {
            "dry_run": dry_run,
            "include_rejected": include_rejected,
            "limit": limit,
            "allow_second_confirmation": allow_second_confirmation,
            "considered_count": len(plans),
            "result_count": len(results),
            "applied_count": sum(1 for result in results if result.get("applied")),
            "preview_count": sum(
                1 for result in results if dry_run and not result.get("applied")
            ),
            "failed_count": sum(
                1 for result in results if not dry_run and not result.get("applied")
            ),
            "skipped_count": len(skipped),
            "skipped_already_applied_count": _count_skipped_review_plans(
                skipped,
                "already_applied",
            ),
            "skipped_second_confirmation_count": _count_skipped_review_plans(
                skipped,
                "requires_second_confirmation",
            ),
            "results": results,
            "skipped": skipped,
        }

    def review_events(
        self,
        target_type: str | None = None,
        target_id: str | None = None,
        event_type: str | None = None,
        limit: int = 100,
    ) -> Sequence[ReviewEvent]:
        return self.store().list_review_events(
            target_type=target_type,
            target_id=target_id,
            event_type=event_type,
            limit=limit,
        )

    def review_audit(
        self,
        event_limit: int = 1000,
        recent_limit: int = 10,
    ) -> ReviewAuditSummary:
        return ReviewAuditReporter(self.store()).summarize(
            event_limit=event_limit,
            recent_limit=recent_limit,
        )

    def _fused_report(
        self,
        query: str,
        token_budget: int,
        recall_limit: int,
        active_limit: int,
        candidate_limit: int | None,
        layers: Sequence[str | MemoryLayer] | None,
        session_id: str | None,
        statuses: Sequence[str | MemoryStatus] | None,
        strict_session: bool,
    ) -> ContextFusionReport:
        recall_candidates, active_candidates = self._fusion_candidates(
            query=query,
            recall_limit=recall_limit,
            active_limit=active_limit,
            layers=layers,
            session_id=session_id,
            statuses=statuses,
            strict_session=strict_session,
        )
        return SimpleContextFusion().report(
            recall_candidates=recall_candidates,
            active_candidates=active_candidates,
            token_budget=token_budget,
            candidate_limit=candidate_limit,
        )

    def _fusion_candidates(
        self,
        query: str,
        recall_limit: int,
        active_limit: int,
        layers: Sequence[str | MemoryLayer] | None,
        session_id: str | None,
        statuses: Sequence[str | MemoryStatus] | None,
        strict_session: bool,
        store: SQLiteMemoryStore | None = None,
        flags: HighRiskFlags | None = None,
    ) -> tuple[Sequence[RecallCandidate], Sequence[RecallCandidate]]:
        store = store or self.store()
        flags = flags or high_risk_flags()
        parsed_layers = _memory_layers(layers)
        parsed_statuses = _memory_statuses(statuses)
        recall_candidates = FTSRetrievalEngine(
            store,
            optional_embedding_client_from_env(),
        ).recall(
            RecallQuery(
                text=query,
                top_k=recall_limit,
                preferred_layers=parsed_layers,
                session_id=session_id,
                statuses=parsed_statuses,
            )
        )
        active_candidates = _active_window_engine(store, flags).select(
            limit=active_limit,
            session_id=session_id,
            layers=parsed_layers,
            statuses=parsed_statuses,
            strict_session=strict_session,
        )
        return recall_candidates, active_candidates


def _memory_layers(values: Sequence[str | MemoryLayer] | None) -> list[MemoryLayer]:
    return [value if isinstance(value, MemoryLayer) else MemoryLayer(value) for value in values or []]


def _optional_memory_layer(value: str | MemoryLayer | None) -> MemoryLayer | None:
    if value is None:
        return None
    return value if isinstance(value, MemoryLayer) else MemoryLayer(value)


def _memory_statuses(values: Sequence[str | MemoryStatus] | None) -> list[MemoryStatus]:
    return [
        value if isinstance(value, MemoryStatus) else MemoryStatus(value)
        for value in values or []
    ]


def _message_role(value: str | MessageRole) -> MessageRole:
    return value if isinstance(value, MessageRole) else MessageRole(value)


def _access_signal(value: str | AccessSignal) -> AccessSignal:
    return value if isinstance(value, AccessSignal) else AccessSignal(value)


def _review_status(value: str | ReviewStatus) -> ReviewStatus:
    return value if isinstance(value, ReviewStatus) else ReviewStatus(value)


def _optional_review_item_kind(
    value: str | ReviewItemKind | None,
) -> ReviewItemKind | None:
    if value is None:
        return None
    return value if isinstance(value, ReviewItemKind) else ReviewItemKind(value)


def _semantic_dedup_mode(value: str) -> str:
    if value not in SEMANTIC_DEDUP_MODES:
        raise ValueError("Unsupported semantic_dedup mode: %s" % value)
    return value


def _count_memory_ids(memory_ids: Sequence[str], counts: dict[str, int]) -> None:
    for memory_id in memory_ids:
        counts[memory_id] = counts.get(memory_id, 0) + 1


def _active_window_mode(value: str | None, flags: HighRiskFlags) -> str:
    if value is None:
        return "full" if flags.enable_default_active_window_in_build_context else "off"
    if value not in {"off", "limited", "full"}:
        raise ValueError("Unsupported active_window_mode: %s" % value)
    return value


def _active_window_engine(
    store: SQLiteMemoryStore,
    flags: HighRiskFlags,
) -> GlobalActiveWindowEngine:
    return GlobalActiveWindowEngine(
        store,
        GlobalActiveWindowPolicy(
            allow_l4_persona=flags.enable_l4_persona_active_window,
        ),
    )


def _record_active_window_injections(
    store: SQLiteMemoryStore,
    report: ContextFusionReport,
    query: str,
    mode: str,
    session_id: str | None,
) -> list[str]:
    memory_ids: list[str] = []
    for decision in report.decisions:
        if not decision.selected or "active_window" not in decision.sources:
            continue
        store.record_access_signal(
            decision.memory_id,
            AccessSignal.INJECTED,
            strength=0.35,
            metadata={
                "source": "build_context_active_window",
                "query": query,
                "active_window_mode": mode,
                "session_id": session_id,
                "sources": decision.sources,
            },
        )
        memory_ids.append(decision.memory_id)
    return memory_ids


def _batch_recommendation(
    query_count: int,
    total_baseline_only: int,
    total_fused_only: int,
) -> str:
    if query_count <= 0:
        return "insufficient_queries"
    if total_baseline_only > 0:
        return "keep_explicit_only"
    if total_fused_only >= query_count:
        return "consider_opt_in_fusion"
    return "collect_more_evidence"


def _maintenance_summary(steps: dict[str, object]) -> dict[str, object]:
    def step_result(name: str) -> dict[str, object]:
        step = steps.get(name, {})
        if not isinstance(step, dict):
            return {}
        result = step.get("result", {})
        return result if isinstance(result, dict) else {}

    lifecycle = step_result("govern_lifecycle")
    semantic = step_result("semantic_dedup")
    l3 = step_result("cross_session_l3_candidates")
    l4 = step_result("build_l4_persona_candidates")
    autonomous = step_result("autonomous_governance")
    autonomous_summary = autonomous.get("summary", {})
    if not isinstance(autonomous_summary, dict):
        autonomous_summary = {}
    review_actions = step_result("apply_review_actions")
    audit = step_result("review_audit")
    return {
        "updated_lifecycle_count": lifecycle.get("updated_count", 0),
        "semantic_candidate_count": semantic.get("candidate_count", 0),
        "semantic_auto_resolution_count": semantic.get("auto_resolution_count", 0),
        "cross_session_l3_candidate_count": l3.get("candidate_count", 0),
        "l4_persona_candidate_count": l4.get("candidate_count", 0),
        "autonomous_decision_count": autonomous_summary.get("decision_count", 0),
        "autonomous_applied_count": autonomous_summary.get("applied_count", 0),
        "autonomous_semantic_applied_count": autonomous_summary.get(
            "semantic_applied_count",
            0,
        ),
        "autonomous_l3_applied_count": autonomous_summary.get(
            "cross_session_l3_applied_count",
            0,
        ),
        "autonomous_l4_applied_count": autonomous_summary.get(
            "l4_persona_applied_count",
            0,
        ),
        "review_action_applied_count": review_actions.get("applied_count", 0),
        "review_action_skipped_count": review_actions.get("skipped_count", 0),
        "review_event_count": audit.get("total_events", 0),
    }


def _empty_review_action_application(
    include_rejected: bool,
    limit: int,
    allow_second_confirmation: bool,
    dry_run: bool,
) -> dict[str, object]:
    return {
        "dry_run": dry_run,
        "include_rejected": include_rejected,
        "limit": limit,
        "allow_second_confirmation": allow_second_confirmation,
        "considered_count": 0,
        "result_count": 0,
        "applied_count": 0,
        "preview_count": 0,
        "failed_count": 0,
        "skipped_count": 0,
        "skipped_already_applied_count": 0,
        "skipped_second_confirmation_count": 0,
        "results": [],
        "skipped": [],
    }


def _review_plan_already_applied(
    store: SQLiteMemoryStore,
    plan: ReviewActionPlan,
    applied_plan_ids: set[str],
) -> bool:
    if plan.id in applied_plan_ids:
        return True
    metadata: dict[str, object] = {}
    if plan.target_type == "memory_unit":
        memory = store.get_memory_unit(plan.target_id)
        metadata = dict(memory.metadata) if memory is not None else {}
    elif plan.target_type == "graph_edge":
        edge = store.get_graph_edge(plan.target_id)
        edge_metadata = edge.get("metadata", {}) if edge is not None else {}
        metadata = dict(edge_metadata) if isinstance(edge_metadata, dict) else {}
    return metadata.get("review_action_id") == plan.id


def _skipped_review_plan(plan: ReviewActionPlan, reason: str) -> dict[str, object]:
    return {
        "plan_id": plan.id,
        "review_item_id": plan.review_item_id,
        "action_type": plan.action_type.value,
        "target_type": plan.target_type,
        "target_id": plan.target_id,
        "risk": plan.risk.value,
        "requires_second_confirmation": plan.requires_second_confirmation,
        "reason": reason,
    }


def _count_skipped_review_plans(
    skipped: Sequence[dict[str, object]],
    reason: str,
) -> int:
    return sum(1 for item in skipped if item.get("reason") == reason)
