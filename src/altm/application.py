"""Application use cases shared by CLI and MCP adapters."""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import cast

from altm.capture import L0Recorder
from altm.config import HighRiskFlags, high_risk_flags
from altm.context import ContentRouter, SimpleContextFusion, SimpleContextGateway
from altm.contracts import (
    AbortedTurn,
    AccessSignal,
    ActiveWindowReport,
    CaptureInput,
    CommittedTurn,
    ContextBundle,
    ContextFusionBatchComparisonItem,
    ContextFusionBatchComparisonReport,
    ContextFusionComparisonReport,
    ContextFusionReport,
    MemoryLayer,
    MemoryScope,
    MemoryStatus,
    MemoryUnit,
    MessageRole,
    PreparedTurn,
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
    GraphLLMExtractor,
    L2Extractor,
    L4PersonaCandidateBuilder,
    LLMContextCapsuleSummarizer,
    RuleBasedL3SceneBuilder,
    SemanticL3SceneBuilder,
    SemanticL4PersonaDistiller,
)
from altm.governance import (
    SEMANTIC_DEDUP_MODES,
    AutonomousGovernanceEngine,
    SemanticDeduper,
    SemanticDedupPolicy,
)
from altm.lifecycle import (
    CompressionLifecycleManager,
    LifecycleGovernor,
    PersonaLifecycleManager,
    RetentionManager,
    SceneLifecycleManager,
)
from altm.llm import (
    OpenAICompatibleClient,
    OpenAICompatibleEmbeddingClient,
    embedding_config_candidate,
    embedding_config_from_sources,
    embedding_config_status,
    llm_config_from_env,
    optional_embedding_client_from_sources,
    save_embedding_config,
)
from altm.recall_policy import cross_session_query_layers
from altm.retrieval import (
    FTSRetrievalEngine,
    GlobalActiveWindowEngine,
    GlobalActiveWindowPolicy,
    QueryEmergenceEngine,
)
from altm.retrieval.remote_vector import EmbeddingIndexer
from altm.review import ReviewActionExecutor, ReviewActionPlanner, ReviewAuditReporter, ReviewQueue
from altm.storage import SQLiteMemoryStore
from altm.utils import sha256_text, stable_id, utc_now_iso


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

    def store(self, scope: MemoryScope | None = None) -> SQLiteMemoryStore:
        sqlite_store = SQLiteMemoryStore(
            self.db_path,
            schema_path=self.schema_path,
            scope=scope,
        )
        sqlite_store.initialize()
        return sqlite_store

    def initialize_store(self) -> None:
        self.store()

    def embedding_status(self) -> dict[str, object]:
        return embedding_config_status(self.db_path)

    def configure_embedding(
        self,
        base_url: str,
        model: str,
        api_key: str | None = None,
    ) -> dict[str, object]:
        candidate = embedding_config_candidate(
            self.db_path,
            base_url=base_url,
            model=model,
            api_key=api_key,
        )
        try:
            OpenAICompatibleEmbeddingClient(candidate).embed_text(
                "ALTM embedding connection check"
            )
        except Exception as exc:
            raise RuntimeError(
                "Embedding provider validation failed; check the URL, model, and API key"
            ) from exc
        save_embedding_config(self.db_path, candidate)
        return self.embedding_status()

    def _embedding_model(self) -> str | None:
        status = self.embedding_status()
        model = status.get("model")
        return model if isinstance(model, str) and model else None

    def remember(
        self,
        session_id: str,
        content: str,
        role: str | MessageRole = MessageRole.USER,
        message_id: str | None = None,
        scope: MemoryScope | None = None,
    ) -> MemoryUnit:
        resolved_scope = scope or MemoryScope()
        return L0Recorder(self.store(resolved_scope)).capture(
            CaptureInput(
                session_id=session_id,
                content=content,
                scope=resolved_scope,
                role=_message_role(role),
                message_id=message_id,
            )
        )

    def fold_l1(
        self,
        session_id: str,
        scope: MemoryScope | None = None,
    ) -> Sequence[MemoryUnit]:
        resolved_scope = scope or MemoryScope()
        return LLMContextCapsuleSummarizer(
            self.store(resolved_scope),
            OpenAICompatibleClient(llm_config_from_env("l1")),
        ).fold_session(session_id)

    def extract_l2(
        self,
        session_id: str,
        scope: MemoryScope | None = None,
    ) -> Sequence[MemoryUnit]:
        resolved_scope = scope or MemoryScope()
        return L2Extractor(
            self.store(resolved_scope),
            OpenAICompatibleClient(llm_config_from_env("l2")),
        ).extract_from_session(session_id)

    def prepare_turn(
        self,
        tenant_id: str,
        workspace_id: str,
        user_id: str,
        agent_id: str,
        session_id: str,
        turn_id: str,
        content: str,
        message_id: str | None = None,
        query: str | None = None,
        token_budget: int = 1200,
        recall_limit: int = 10,
        active_window_mode: str | None = None,
        active_limit: int = 5,
        strict_session: bool = False,
    ) -> PreparedTurn:
        scope = MemoryScope(
            tenant_id=tenant_id,
            workspace_id=workspace_id,
            user_id=user_id,
            agent_id=agent_id,
        )
        store = self.store(scope)
        capture_input = CaptureInput(
            session_id=session_id,
            content=content,
            scope=scope,
            role=MessageRole.USER,
            message_id=message_id or "%s:user" % turn_id,
        )
        user_memory = L0Recorder(store).capture(capture_input)
        resolved_query = query or content
        bundle = self.build_context(
            query=resolved_query,
            token_budget=token_budget,
            limit=recall_limit,
            session_id=session_id,
            active_window_mode=active_window_mode,
            active_limit=active_limit,
            strict_session=strict_session,
            scope=scope,
        )
        cycle_id = stable_id(
            "cycle",
            *scope.key_parts(),
            session_id,
            turn_id,
        )
        cycle = store.create_runtime_cycle(
            cycle_id=cycle_id,
            session_id=session_id,
            turn_id=turn_id,
            user_memory_id=user_memory.id,
            user_content_hash=user_memory.content_hash,
            query=resolved_query,
            context=bundle,
            metadata={"protocol": "prepare_commit_v1"},
        )
        job_id = store.enqueue_job(
            job_type="fold_l1",
            dedupe_key=user_memory.id,
            session_id=session_id,
            payload={"session_id": session_id, "trigger_memory_id": user_memory.id},
        )
        return PreparedTurn(
            cycle_id=str(cycle["id"]),
            scope=scope,
            session_id=session_id,
            turn_id=turn_id,
            user_memory_id=str(cycle["user_memory_id"]),
            query=str(cycle["query"]),
            context=ContextBundle.model_validate(cycle["context"]),
            enqueued_job_ids=[job_id],
            status=str(cycle["status"]),
            metadata=_object_dict(cycle["metadata"]),
        )

    def commit_turn(
        self,
        tenant_id: str,
        workspace_id: str,
        user_id: str,
        agent_id: str,
        cycle_id: str,
        assistant_content: str,
        cited_memory_ids: Sequence[str] = (),
        assistant_message_id: str | None = None,
    ) -> CommittedTurn:
        scope = MemoryScope(
            tenant_id=tenant_id,
            workspace_id=workspace_id,
            user_id=user_id,
            agent_id=agent_id,
        )
        store = self.store(scope)
        cycle = store.get_runtime_cycle(cycle_id)
        if cycle is None:
            raise ValueError("Unknown runtime cycle: %s" % cycle_id)
        session_id = str(cycle["session_id"])
        turn_id = str(cycle["turn_id"])
        assistant_memory = L0Recorder(store).build(
            CaptureInput(
                session_id=session_id,
                content=assistant_content,
                scope=scope,
                role=MessageRole.ASSISTANT,
                message_id=assistant_message_id or "%s:assistant" % turn_id,
            )
        )
        committed = store.commit_runtime_cycle(
            cycle_id=cycle_id,
            assistant_memory=assistant_memory,
            cited_memory_ids=cited_memory_ids,
            metadata={"protocol": "prepare_commit_v1"},
        )
        job_id = store.enqueue_job(
            job_type="fold_l1",
            dedupe_key=assistant_memory.id,
            session_id=session_id,
            payload={
                "session_id": session_id,
                "trigger_memory_id": assistant_memory.id,
            },
        )
        return CommittedTurn(
            cycle_id=cycle_id,
            scope=scope,
            session_id=session_id,
            turn_id=turn_id,
            assistant_memory_id=assistant_memory.id,
            cited_memory_ids=_string_values(committed["cited_memory_ids"]),
            enqueued_job_ids=[job_id],
            status=str(committed["status"]),
            metadata=_object_dict(committed["metadata"]),
        )

    def abort_turn(
        self,
        tenant_id: str,
        workspace_id: str,
        user_id: str,
        agent_id: str,
        cycle_id: str,
        reason: str,
    ) -> AbortedTurn:
        scope = MemoryScope(
            tenant_id=tenant_id,
            workspace_id=workspace_id,
            user_id=user_id,
            agent_id=agent_id,
        )
        aborted = self.store(scope).abort_runtime_cycle(
            cycle_id=cycle_id,
            reason=reason,
            metadata={"protocol": "prepare_commit_v1"},
        )
        metadata = _object_dict(aborted["metadata"])
        return AbortedTurn(
            cycle_id=cycle_id,
            scope=scope,
            session_id=str(aborted["session_id"]),
            turn_id=str(aborted["turn_id"]),
            reason=str(metadata["abort_reason"]),
            status=str(aborted["status"]),
            metadata=metadata,
        )

    def ui_graph_seeds(
        self,
        tenant_id: str,
        workspace_id: str,
        user_id: str,
        agent_id: str,
        query: str | None = None,
        limit: int = 24,
    ) -> list[dict[str, object]]:
        store = self.store(
            MemoryScope(
                tenant_id=tenant_id,
                workspace_id=workspace_id,
                user_id=user_id,
                agent_id=agent_id,
            )
        )
        if query is not None and query.strip():
            return list(store.search_graph_nodes(query=query, limit=limit))
        return list(store.list_recent_graph_nodes(limit=limit))

    def ui_graph_neighborhood(
        self,
        tenant_id: str,
        workspace_id: str,
        user_id: str,
        agent_id: str,
        seed_node_ids: Sequence[str],
        max_hops: int = 2,
        node_limit: int = 120,
    ) -> dict[str, object]:
        store = self.store(
            MemoryScope(
                tenant_id=tenant_id,
                workspace_id=workspace_id,
                user_id=user_id,
                agent_id=agent_id,
            )
        )
        return store.get_graph_subgraph(
            seed_node_ids=seed_node_ids,
            max_hops=max_hops,
            node_limit=node_limit,
        )

    def ui_memory_layers(
        self,
        tenant_id: str,
        workspace_id: str,
        user_id: str,
        agent_id: str,
        limit_per_layer: int = 80,
    ) -> dict[str, object]:
        layers = [
            MemoryLayer.L1,
            MemoryLayer.L2,
            MemoryLayer.L3,
            MemoryLayer.L4,
        ]
        store = self.store(
            MemoryScope(
                tenant_id=tenant_id,
                workspace_id=workspace_id,
                user_id=user_id,
                agent_id=agent_id,
            )
        )
        memories = store.list_recent_memory_units(
            layers=layers,
            limit_per_layer=limit_per_layer,
        )
        return {
            "counts": store.memory_layer_counts(layers),
            "layers": {
                layer.value: [
                    memory.model_dump(mode="json")
                    for memory in memories[layer.value]
                ]
                for layer in layers
            },
        }

    def process_next_job(
        self,
        worker_id: str,
        lease_seconds: int = 120,
    ) -> dict[str, object]:
        control_store = self.store()
        job = control_store.claim_job(worker_id=worker_id, lease_seconds=lease_seconds)
        if job is None:
            return {"status": "idle", "worker_id": worker_id}

        job_id = str(job["id"])
        scope = MemoryScope.model_validate(job["scope"])
        payload_value = job.get("payload", {})
        if not isinstance(payload_value, dict):
            control_store.fail_job(job_id, worker_id, "runtime job payload is not an object")
            return {"status": "failed", "job_id": job_id, "reason": "invalid_payload"}
        payload = _object_dict(cast(object, payload_value))
        session_id = str(payload.get("session_id") or job.get("session_id") or "")
        try:
            if job["job_type"] == "fold_l1":
                memories = list(self.fold_l1(session_id=session_id, scope=scope))
                result: dict[str, object] = {
                    "created_count": len(memories),
                    "memory_ids": [memory.id for memory in memories],
                }
                if memories:
                    self.store(scope).enqueue_job(
                        job_type="extract_l2",
                        dedupe_key=memories[-1].id,
                        session_id=session_id,
                        payload={
                            "session_id": session_id,
                            "trigger_memory_id": memories[-1].id,
                        },
                    )
            elif job["job_type"] == "extract_l2":
                memories = list(self.extract_l2(session_id=session_id, scope=scope))
                result = {
                    "created_count": len(memories),
                    "memory_ids": [memory.id for memory in memories],
                }
                if memories:
                    self.store(scope).enqueue_job(
                        job_type="index_embeddings",
                        dedupe_key=memories[-1].id,
                        session_id=session_id,
                        payload={
                            "session_id": session_id,
                            "trigger_memory_id": memories[-1].id,
                        },
                    )
                    self.store(scope).enqueue_job(
                        job_type="graph_extract",
                        dedupe_key=memories[-1].id,
                        session_id=session_id,
                        payload={
                            "session_id": session_id,
                            "trigger_memory_id": memories[-1].id,
                        },
                    )
            elif job["job_type"] == "graph_extract":
                result = GraphLLMExtractor(self.store(scope)).extract_session(
                    session_id=session_id
                )
            elif job["job_type"] == "index_embeddings":
                result = self.index_embeddings(limit=100, scope=scope)
                embedding_model = str(result["embedding_model"])
                trigger_memory_id = str(
                    payload.get("trigger_memory_id") or session_id
                )
                self.store(scope).enqueue_job(
                    job_type="semantic_l3",
                    dedupe_key="%s:%s" % (trigger_memory_id, embedding_model),
                    session_id=session_id,
                    payload={
                        "session_id": session_id,
                        "embedding_model": embedding_model,
                        "trigger_memory_id": trigger_memory_id,
                    },
                )
            elif job["job_type"] == "semantic_l3":
                embedding_model = str(payload.get("embedding_model") or "")
                if not embedding_model:
                    raise ValueError("semantic_l3 job requires embedding_model")
                memories = list(
                    SemanticL3SceneBuilder(
                        self.store(scope),
                        embedding_model=embedding_model,
                    ).build()
                )
                result = {
                    "created_count": len(memories),
                    "memory_ids": [memory.id for memory in memories],
                }
                activated = list(
                    SceneLifecycleManager(self.store(scope)).advance()
                )
                result.update(
                    {
                        "activated_count": len(activated),
                        "activated_memory_ids": [
                            memory.id for memory in activated
                        ],
                    }
                )
                trigger_memory_id = str(
                    payload.get("trigger_memory_id") or session_id
                )
                self.store(scope).enqueue_job(
                    job_type="semantic_l4",
                    dedupe_key="%s:%s" % (trigger_memory_id, embedding_model),
                    session_id=session_id,
                    payload={
                        "session_id": session_id,
                        "embedding_model": embedding_model,
                        "trigger_memory_id": trigger_memory_id,
                    },
                )
            elif job["job_type"] == "semantic_l4":
                embedding_model = str(payload.get("embedding_model") or "")
                if not embedding_model:
                    raise ValueError("semantic_l4 job requires embedding_model")
                memories = list(
                    SemanticL4PersonaDistiller(
                        self.store(scope),
                        embedding_model=embedding_model,
                    ).distill()
                )
                activated = list(
                    PersonaLifecycleManager(self.store(scope)).advance()
                )
                result = {
                    "created_count": len(memories),
                    "memory_ids": [memory.id for memory in memories],
                    "activated_count": len(activated),
                    "activated_memory_ids": [memory.id for memory in activated],
                }
                trigger_memory_id = str(
                    payload.get("trigger_memory_id") or session_id
                )
                self.store(scope).enqueue_job(
                    job_type="lifecycle",
                    dedupe_key=trigger_memory_id,
                    session_id=session_id,
                    payload={"session_id": session_id},
                )
                self.store(scope).enqueue_job(
                    job_type="retention",
                    dedupe_key=trigger_memory_id,
                    session_id=session_id,
                    payload={"session_id": session_id},
                )
            elif job["job_type"] == "lifecycle":
                memories = list(LifecycleGovernor(self.store(scope)).run_cycle())
                compressed = list(
                    CompressionLifecycleManager(self.store(scope)).run()
                )
                result = {
                    "updated_count": len(memories),
                    "memory_ids": [memory.id for memory in memories],
                    "compressed_count": len(compressed),
                    "compressed_memory_ids": [
                        memory.id for memory in compressed
                    ],
                }
            elif job["job_type"] == "retention":
                deleted = list(
                    RetentionManager(self.store(scope)).delete_expired_l0()
                )
                result = {
                    "deleted_count": len(deleted),
                    "deletions": deleted,
                }
            else:
                raise ValueError("Unsupported runtime job type: %s" % job["job_type"])
        except Exception as exc:
            control_store.fail_job(job_id, worker_id, str(exc))
            failed = control_store.get_job(job_id)
            return {
                "status": str(failed["status"]) if failed is not None else "failed",
                "job_id": job_id,
                "job_type": job["job_type"],
                "error": str(exc),
            }

        control_store.complete_job(job_id, worker_id, result)
        return {
            "status": "completed",
            "job_id": job_id,
            "job_type": job["job_type"],
            "result": result,
        }

    def pin_memory(
        self,
        scope: MemoryScope,
        memory_id: str,
        pinned: bool,
    ) -> MemoryUnit:
        store = self.store(scope)
        memory = store.get_memory_unit(memory_id)
        if memory is None:
            raise ValueError("Memory does not exist in the active scope: %s" % memory_id)
        metadata = dict(memory.metadata)
        metadata["pinned"] = pinned
        metadata["pin_updated_at"] = utc_now_iso()
        updated = memory.model_copy(
            update={"metadata": metadata, "updated_at": utc_now_iso()}
        )
        store.put_memory_unit(updated)
        if pinned:
            store.record_access_signal(memory_id, AccessSignal.USER_CONFIRMED)
        return updated

    def delete_memory(
        self,
        scope: MemoryScope,
        memory_id: str,
        reason: str,
        physical: bool = False,
    ) -> dict[str, object]:
        return RetentionManager(self.store(scope)).delete_memory(
            memory_id=memory_id,
            reason=reason,
            physical=physical,
        )

    def runtime_cycle(
        self,
        session_id: str,
        content: str,
        role: str | MessageRole = MessageRole.USER,
        message_id: str | None = None,
        query: str | None = None,
        token_budget: int = 1200,
        recall_limit: int = 10,
        active_window_mode: str | None = None,
        active_limit: int = 5,
        strict_session: bool = False,
        fold_l1: bool = True,
        extract_l2: bool = True,
        run_maintenance: bool = True,
        maintenance_model: str | None = None,
        index_embeddings: bool = False,
        embedding_limit: int = 100,
        scope: MemoryScope | None = None,
    ) -> dict[str, object]:
        resolved_scope = scope or MemoryScope()
        steps: dict[str, object] = {}
        captured = self.remember(
            session_id=session_id,
            content=content,
            role=role,
            message_id=message_id,
            scope=resolved_scope,
        )
        steps["capture_l0"] = {
            "status": "applied",
            "memory_id": captured.id,
            "memory": captured.model_dump(mode="json"),
        }

        if fold_l1:
            try:
                folded = list(self.fold_l1(session_id=session_id, scope=resolved_scope))
                steps["fold_l1"] = {
                    "status": "applied",
                    "memory_ids": [memory.id for memory in folded],
                    "count": len(folded),
                }
            except Exception as exc:
                steps["fold_l1"] = {"status": "degraded", "error": str(exc)}
        else:
            steps["fold_l1"] = {"status": "skipped", "reason": "not_requested"}

        if extract_l2:
            try:
                extracted = list(
                    self.extract_l2(session_id=session_id, scope=resolved_scope)
                )
                steps["extract_l2"] = {
                    "status": "applied",
                    "memory_ids": [memory.id for memory in extracted],
                    "count": len(extracted),
                }
            except Exception as exc:
                steps["extract_l2"] = {"status": "degraded", "error": str(exc)}
        else:
            steps["extract_l2"] = {"status": "skipped", "reason": "not_requested"}

        if run_maintenance:
            steps["maintenance_cycle"] = {
                "status": "applied",
                "result": self.maintenance_cycle(
                    model=maintenance_model,
                    index_embeddings=index_embeddings,
                    embedding_limit=embedding_limit,
                    dry_run=False,
                ),
            }
        else:
            steps["maintenance_cycle"] = {"status": "skipped", "reason": "not_requested"}

        bundle = self.build_context(
            query=query or content,
            token_budget=token_budget,
            limit=recall_limit,
            session_id=session_id,
            active_window_mode=active_window_mode,
            active_limit=active_limit,
            strict_session=strict_session,
            scope=resolved_scope,
        )
        steps["build_context"] = {
            "status": "applied",
            "included_count": len(bundle.items),
            "metadata": bundle.metadata,
        }
        injected_memory_ids = _record_context_bundle_signal(
            store=self.store(resolved_scope),
            bundle=bundle,
            signal=AccessSignal.INJECTED,
            source="runtime_cycle",
            query=query or content,
            session_id=session_id,
            strength=0.45,
        )
        steps["record_context_feedback"] = {
            "status": "applied",
            "signal": AccessSignal.INJECTED.value,
            "count": len(injected_memory_ids),
            "memory_ids": injected_memory_ids,
        }

        return {
            "status": "complete",
            "session_id": session_id,
            "query": query or content,
            "steps": steps,
            "context": bundle.model_dump(mode="json"),
            "summary": _runtime_cycle_summary(steps),
        }

    def runtime_session_cycle(
        self,
        session_id: str,
        messages: Sequence[dict[str, object]],
        query: str | None = None,
        token_budget: int = 1200,
        recall_limit: int = 10,
        active_window_mode: str | None = None,
        active_limit: int = 5,
        strict_session: bool = False,
        fold_l1: bool = True,
        extract_l2: bool = True,
        run_maintenance: bool = True,
        maintenance_model: str | None = None,
        index_embeddings: bool = False,
        embedding_limit: int = 100,
        scope: MemoryScope | None = None,
    ) -> dict[str, object]:
        resolved_scope = scope or MemoryScope()
        steps: dict[str, object] = {}
        capture_inputs: list[CaptureInput] = []
        for message in messages:
            content = str(message.get("content", "")).strip()
            if not content:
                continue
            role = str(message.get("role", MessageRole.USER.value))
            message_id = message.get("message_id")
            capture_inputs.append(
                CaptureInput(
                    session_id=session_id,
                    content=content,
                    scope=resolved_scope,
                    role=_message_role(role),
                    message_id=str(message_id) if message_id is not None else None,
                )
            )
        store = self.store(resolved_scope)
        recorder = L0Recorder(store)
        captured = [recorder.build(capture_input) for capture_input in capture_inputs]
        store.put_memory_units(captured)
        steps["capture_l0"] = {
            "status": "applied",
            "count": len(captured),
            "memory_ids": [memory.id for memory in captured],
        }

        if fold_l1:
            try:
                folded = list(self.fold_l1(session_id=session_id, scope=resolved_scope))
                steps["fold_l1"] = {
                    "status": "applied",
                    "memory_ids": [memory.id for memory in folded],
                    "count": len(folded),
                }
            except Exception as exc:
                steps["fold_l1"] = {"status": "degraded", "error": str(exc)}
        else:
            steps["fold_l1"] = {"status": "skipped", "reason": "not_requested"}

        if extract_l2:
            try:
                extracted = list(
                    self.extract_l2(session_id=session_id, scope=resolved_scope)
                )
                steps["extract_l2"] = {
                    "status": "applied",
                    "memory_ids": [memory.id for memory in extracted],
                    "count": len(extracted),
                }
            except Exception as exc:
                steps["extract_l2"] = {"status": "degraded", "error": str(exc)}
        else:
            steps["extract_l2"] = {"status": "skipped", "reason": "not_requested"}

        if run_maintenance:
            steps["maintenance_cycle"] = {
                "status": "applied",
                "result": self.maintenance_cycle(
                    model=maintenance_model,
                    index_embeddings=index_embeddings,
                    embedding_limit=embedding_limit,
                    dry_run=False,
                ),
            }
        else:
            steps["maintenance_cycle"] = {"status": "skipped", "reason": "not_requested"}

        fallback_query = query or _last_message_content(messages) or session_id
        bundle = self.build_context(
            query=fallback_query,
            token_budget=token_budget,
            limit=recall_limit,
            session_id=session_id,
            active_window_mode=active_window_mode,
            active_limit=active_limit,
            strict_session=strict_session,
            scope=resolved_scope,
        )
        steps["build_context"] = {
            "status": "applied",
            "included_count": len(bundle.items),
            "metadata": bundle.metadata,
        }
        injected_memory_ids = _record_context_bundle_signal(
            store=store,
            bundle=bundle,
            signal=AccessSignal.INJECTED,
            source="runtime_session_cycle",
            query=fallback_query,
            session_id=session_id,
            strength=0.45,
        )
        steps["record_context_feedback"] = {
            "status": "applied",
            "signal": AccessSignal.INJECTED.value,
            "count": len(injected_memory_ids),
            "memory_ids": injected_memory_ids,
        }

        return {
            "status": "complete",
            "session_id": session_id,
            "query": fallback_query,
            "message_count": len(messages),
            "captured_count": len(captured),
            "steps": steps,
            "context": bundle.model_dump(mode="json"),
            "summary": _runtime_cycle_summary(steps),
        }

    def mvp_chat(
        self,
        session_id: str,
        content: str,
        role: str | MessageRole = MessageRole.USER,
        message_id: str | None = None,
        assistant_content: str | None = None,
        assistant_message_id: str | None = None,
        cited_memory_ids: Sequence[str] = (),
        turn_id: str | None = None,
        scope: MemoryScope | None = None,
        query: str | None = None,
        token_budget: int = 1200,
        recall_limit: int = 10,
        active_window_mode: str | None = None,
        active_limit: int = 5,
        strict_session: bool = False,
        fold_l1: bool = True,
        extract_l2: bool = True,
        run_maintenance: bool = True,
        maintenance_model: str | None = None,
        index_embeddings: bool = False,
        embedding_limit: int = 100,
    ) -> dict[str, object]:
        del fold_l1, extract_l2, run_maintenance, maintenance_model
        del index_embeddings, embedding_limit
        resolved_scope = scope or MemoryScope()
        if _message_role(role) != MessageRole.USER:
            raise ValueError("mvp_chat compatibility wrapper only accepts a user turn")
        if assistant_content is None or not assistant_content.strip():
            raise ValueError(
                "mvp_chat no longer generates a synthetic assistant response; "
                "pass the real Host Agent response or use prepare_turn/commit_turn"
            )
        resolved_turn_id = turn_id or message_id or stable_id(
            "legacy_turn",
            *resolved_scope.key_parts(),
            session_id,
            sha256_text(content),
        )
        prepared = self.prepare_turn(
            tenant_id=resolved_scope.tenant_id,
            workspace_id=resolved_scope.workspace_id,
            user_id=resolved_scope.user_id,
            agent_id=resolved_scope.agent_id,
            session_id=session_id,
            turn_id=resolved_turn_id,
            content=content,
            message_id=message_id,
            query=query,
            token_budget=token_budget,
            recall_limit=recall_limit,
            active_window_mode=active_window_mode,
            active_limit=active_limit,
            strict_session=strict_session,
        )
        committed = self.commit_turn(
            tenant_id=resolved_scope.tenant_id,
            workspace_id=resolved_scope.workspace_id,
            user_id=resolved_scope.user_id,
            agent_id=resolved_scope.agent_id,
            cycle_id=prepared.cycle_id,
            assistant_content=assistant_content,
            cited_memory_ids=cited_memory_ids,
            assistant_message_id=assistant_message_id,
        )
        return {
            "status": "complete",
            "session_id": session_id,
            "turn_id": resolved_turn_id,
            "cycle_id": prepared.cycle_id,
            "deprecated": True,
            "deprecation_message": "Use prepare_turn followed by commit_turn.",
            "prepared_turn": prepared.model_dump(mode="json"),
            "assistant_response": {
                "content": assistant_content,
                "memory_id": committed.assistant_memory_id,
                "message_id": assistant_message_id,
                "cited_memory_ids": committed.cited_memory_ids,
            },
            "committed_turn": committed.model_dump(mode="json"),
            "context": prepared.context.model_dump(mode="json"),
        }

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

    def build_semantic_l3(
        self,
        scope: MemoryScope,
        model: str | None = None,
        threshold: float = 0.82,
        limit: int = 1000,
    ) -> dict[str, object]:
        embedding_model = model or self._embedding_model()
        if not embedding_model:
            raise RuntimeError("Semantic L3 requires a configured embedding model")
        memories = list(
            SemanticL3SceneBuilder(
                self.store(scope),
                embedding_model=embedding_model,
                similarity_threshold=threshold,
            ).build(limit=limit)
        )
        activated = list(SceneLifecycleManager(self.store(scope)).advance())
        return {
            "created_count": len(memories),
            "memory_ids": [memory.id for memory in memories],
            "activated_count": len(activated),
            "activated_memory_ids": [memory.id for memory in activated],
        }

    def distill_semantic_l4(
        self,
        scope: MemoryScope,
        model: str | None = None,
        threshold: float = 0.84,
        limit: int = 1000,
    ) -> dict[str, object]:
        embedding_model = model or self._embedding_model()
        if not embedding_model:
            raise RuntimeError("Semantic L4 requires a configured embedding model")
        memories = list(
            SemanticL4PersonaDistiller(
                self.store(scope),
                embedding_model=embedding_model,
                similarity_threshold=threshold,
            ).distill(limit=limit)
        )
        activated = list(PersonaLifecycleManager(self.store(scope)).advance())
        return {
            "created_count": len(memories),
            "memory_ids": [memory.id for memory in memories],
            "activated_count": len(activated),
            "activated_memory_ids": [memory.id for memory in activated],
        }

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
        embedding_model = model or self._embedding_model()
        if not embedding_model:
            raise RuntimeError(
                "Missing embedding model. Configure an embedding provider or pass model."
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
        scope: MemoryScope | None = None,
        cross_session_layers: Sequence[str | MemoryLayer] | None = None,
    ) -> Sequence[RecallCandidate]:
        store = self.store(scope)
        return FTSRetrievalEngine(
            store,
            optional_embedding_client_from_sources(self.db_path),
        ).recall(
            RecallQuery(
                text=query,
                top_k=limit,
                scope=scope,
                preferred_layers=_memory_layers(layers),
                session_id=session_id,
                cross_session_layers=_memory_layers(cross_session_layers),
                statuses=_memory_statuses(statuses),
            )
        )

    def drilldown(
        self,
        memory_id: str,
        scope: MemoryScope | None = None,
        query: str | None = None,
    ) -> MemoryUnit | dict[str, object] | None:
        store = self.store(scope)
        if memory_id.startswith("memory://") and "#" in memory_id:
            return ContentRouter(store).retrieve(memory_id, query=query)
        return store.get_memory_unit(memory_id)

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
        scope: MemoryScope | None = None,
    ) -> ContextBundle:
        flags = high_risk_flags()
        mode = _active_window_mode(active_window_mode, flags)
        store = self.store(scope)
        if mode == "off":
            candidates = self.recall(
                query=query,
                limit=limit,
                layers=layers,
                session_id=session_id,
                statuses=statuses,
                scope=scope,
                cross_session_layers=cross_session_query_layers(strict_session),
            )
            return SimpleContextGateway(store=store).assemble(
                candidates,
                token_budget=token_budget,
            )

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
        fusion_report = SimpleContextFusion(
            SimpleContextGateway(store=store)
        ).report(
            recall_candidates=recall_candidates,
            active_candidates=active_candidates,
            token_budget=token_budget,
            candidate_limit=candidate_limit,
        )
        bundle = fusion_report.bundle
        metadata = dict(bundle.metadata)
        metadata.update(
            {
                "active_window_mode": mode,
                "active_limit": active_count,
                "active_window_lifecycle_feedback": "deferred_to_prepare_turn",
                "active_window_feedback_memory_ids": [],
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
        direct_retriever = FTSRetrievalEngine(
            store,
            optional_embedding_client_from_sources(self.db_path),
        )
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

    def index_embeddings(
        self,
        limit: int = 100,
        scope: MemoryScope | None = None,
    ) -> dict[str, object]:
        client = OpenAICompatibleEmbeddingClient(
            embedding_config_from_sources(self.db_path)
        )
        indexed = EmbeddingIndexer(self.store(scope), client).index_missing(limit=limit)
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
        autonomous_rule_fallback: bool = False,
        apply_review_actions: bool = False,
        review_action_limit: int = 100,
        include_rejected_review_actions: bool = True,
        allow_second_confirm_review_actions: bool = False,
        dry_run: bool = False,
    ) -> dict[str, object]:
        embedding_model = model or self._embedding_model()
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

        if use_autonomous_governance:
            steps["semantic_dedup"] = {
                "status": "skipped",
                "reason": "autonomous_governance_replaces_legacy_semantic_dedup",
            }
            steps["cross_session_l3_candidates"] = {
                "status": "skipped",
                "reason": "autonomous_governance_replaces_legacy_l3_candidates",
            }
        elif embedding_model:
            steps["semantic_dedup"] = {
                "status": "applied",
                "result": self.semantic_dedup(
                    model=embedding_model,
                    threshold=semantic_threshold,
                    mode=semantic_mode,
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
        rule_fallback: bool = False,
        dry_run: bool = False,
        limit: int = 1000,
    ) -> dict[str, object]:
        embedding_model = model or self._embedding_model()
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

    def feedback(
        self,
        memory_id: str,
        signal: str | AccessSignal,
        scope: MemoryScope | None = None,
    ) -> MemoryUnit | None:
        store = self.store(scope)
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
        embedding_model = model or self._embedding_model()
        if not embedding_model:
            raise RuntimeError(
                "Missing embedding model. Configure an embedding provider or pass model."
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
            optional_embedding_client_from_sources(self.db_path),
        ).recall(
            RecallQuery(
                text=query,
                top_k=recall_limit,
                preferred_layers=parsed_layers,
                session_id=session_id,
                cross_session_layers=list(
                    cross_session_query_layers(strict_session)
                ),
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


def _record_context_bundle_signal(
    store: SQLiteMemoryStore,
    bundle: ContextBundle,
    signal: AccessSignal,
    source: str,
    query: str,
    session_id: str | None,
    strength: float,
    metadata: dict[str, object] | None = None,
) -> list[str]:
    memory_ids = _context_source_memory_ids(bundle)
    base_metadata: dict[str, object] = {
        "source": source,
        "query": query,
        "session_id": session_id,
        "context_item_count": len(bundle.items),
    }
    if metadata:
        base_metadata.update(metadata)
    for memory_id in memory_ids:
        store.record_access_signal(
            memory_id,
            signal,
            strength=strength,
            metadata=base_metadata,
        )
    return memory_ids


def _context_source_memory_ids(bundle: ContextBundle) -> list[str]:
    memory_ids: list[str] = []
    seen: set[str] = set()
    for item in bundle.items:
        for memory_id in item.source_memory_ids:
            if memory_id in seen:
                continue
            seen.add(memory_id)
            memory_ids.append(memory_id)
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


def _object_dict(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        return {}
    return {
        str(key): item
        for key, item in cast(dict[object, object], value).items()
    }


def _string_values(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item) for item in cast(list[object], value)]


def _maintenance_summary(steps: dict[str, object]) -> dict[str, object]:
    def step_result(name: str) -> dict[str, object]:
        step = _object_dict(steps.get(name))
        return _object_dict(step.get("result"))

    lifecycle = step_result("govern_lifecycle")
    semantic = step_result("semantic_dedup")
    l3 = step_result("cross_session_l3_candidates")
    l4 = step_result("build_l4_persona_candidates")
    autonomous = step_result("autonomous_governance")
    autonomous_summary = _object_dict(autonomous.get("summary"))
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


def _runtime_cycle_summary(steps: dict[str, object]) -> dict[str, object]:
    degraded_steps = [
        name
        for name, step in steps.items()
        if _object_dict(step).get("status") == "degraded"
    ]
    skipped_steps = [
        name
        for name, step in steps.items()
        if _object_dict(step).get("status") == "skipped"
    ]
    context_step = _object_dict(steps.get("build_context"))
    feedback_step = _object_dict(steps.get("record_context_feedback"))
    citation_step = _object_dict(steps.get("record_agent_citations"))
    maintenance_step = _object_dict(steps.get("maintenance_cycle"))
    result = _object_dict(maintenance_step.get("result"))
    maintenance_summary = _object_dict(result.get("summary"))
    return {
        "degraded_steps": degraded_steps,
        "skipped_steps": skipped_steps,
        "context_included_count": (
            context_step.get("included_count", 0)
        ),
        "context_feedback_count": (
            feedback_step.get("count", 0)
        ),
        "agent_citation_count": (
            citation_step.get("count", 0)
        ),
        "maintenance_summary": maintenance_summary,
    }


def _last_message_content(messages: Sequence[dict[str, object]]) -> str | None:
    for message in reversed(messages):
        content = str(message.get("content", "")).strip()
        if content:
            return content
    return None


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
        edge_metadata = edge.get("metadata") if edge is not None else None
        metadata = _object_dict(edge_metadata)
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
