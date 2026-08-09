"""MCP server adapter.

The real MCP SDK requires Python >=3.10. The project target is Python >=3.11,
but the current local shell may still be Python 3.9. Keep imports lazy so core
storage and folding tests remain runnable until the runtime is upgraded.
"""

# FastMCP registers nested tool functions through decorators.
# pyright: reportUnusedFunction=false

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from pydantic import BaseModel

from altm.application import AltmApplication
from altm.contracts import MemoryScope


def _model_json(model: BaseModel) -> dict[str, Any]:
    return model.model_dump(mode="json")


def _optional_model_json(model: BaseModel | None) -> dict[str, Any] | None:
    return model.model_dump(mode="json") if model is not None else None


def _models_json(models: Sequence[BaseModel]) -> list[dict[str, Any]]:
    return [_model_json(model) for model in models]


def _json_value(value: BaseModel | dict[str, object] | None) -> dict[str, Any] | None:
    if value is None:
        return None
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    return dict(value)


def create_mcp_server(
    db_path: str,
    profile: str = "runtime",
    token_verifier: Any = None,
    auth_settings: Any = None,
    host: str = "127.0.0.1",
    port: int = 8000,
) -> Any:
    if profile not in {"runtime", "admin"}:
        raise ValueError("Unsupported MCP tool profile: %s" % profile)
    try:
        from mcp.server.fastmcp import FastMCP
    except ModuleNotFoundError as exc:
        raise RuntimeError(
            "MCP SDK is not installed. Use Python >=3.11 and install "
            "`altm[mcp]` before running the MCP server."
        ) from exc

    app = FastMCP(
        "altm",
        token_verifier=token_verifier,
        auth=auth_settings,
        host=host,
        port=port,
        stateless_http=True,
        json_response=True,
    )
    service = AltmApplication(db_path)

    @app.tool()
    def memory_prepare_turn(
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
    ) -> dict[str, Any]:
        return _model_json(
            service.prepare_turn(
                tenant_id=tenant_id,
                workspace_id=workspace_id,
                user_id=user_id,
                agent_id=agent_id,
                session_id=session_id,
                turn_id=turn_id,
                content=content,
                message_id=message_id,
                query=query,
                token_budget=token_budget,
                recall_limit=recall_limit,
                active_window_mode=active_window_mode,
                active_limit=active_limit,
                strict_session=strict_session,
            )
        )

    @app.tool()
    def memory_commit_turn(
        tenant_id: str,
        workspace_id: str,
        user_id: str,
        agent_id: str,
        cycle_id: str,
        assistant_content: str,
        cited_memory_ids: list[str] | None = None,
        assistant_message_id: str | None = None,
    ) -> dict[str, Any]:
        return _model_json(
            service.commit_turn(
                tenant_id=tenant_id,
                workspace_id=workspace_id,
                user_id=user_id,
                agent_id=agent_id,
                cycle_id=cycle_id,
                assistant_content=assistant_content,
                cited_memory_ids=cited_memory_ids or [],
                assistant_message_id=assistant_message_id,
            )
        )

    @app.tool()
    def memory_remember(
        session_id: str,
        content: str,
        role: str = "user",
        message_id: str | None = None,
    ) -> dict[str, Any]:
        return _model_json(
            service.remember(
                session_id=session_id,
                content=content,
                role=role,
                message_id=message_id,
            )
        )

    @app.tool()
    def memory_runtime_cycle(
        session_id: str,
        content: str,
        role: str = "user",
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
    ) -> dict[str, Any]:
        return service.runtime_cycle(
            session_id=session_id,
            content=content,
            role=role,
            message_id=message_id,
            query=query,
            token_budget=token_budget,
            recall_limit=recall_limit,
            active_window_mode=active_window_mode,
            active_limit=active_limit,
            strict_session=strict_session,
            fold_l1=fold_l1,
            extract_l2=extract_l2,
            run_maintenance=run_maintenance,
            maintenance_model=maintenance_model,
            index_embeddings=index_embeddings,
            embedding_limit=embedding_limit,
        )

    @app.tool()
    def memory_runtime_session_cycle(
        session_id: str,
        messages: list[dict[str, Any]],
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
    ) -> dict[str, Any]:
        return service.runtime_session_cycle(
            session_id=session_id,
            messages=messages,
            query=query,
            token_budget=token_budget,
            recall_limit=recall_limit,
            active_window_mode=active_window_mode,
            active_limit=active_limit,
            strict_session=strict_session,
            fold_l1=fold_l1,
            extract_l2=extract_l2,
            run_maintenance=run_maintenance,
            maintenance_model=maintenance_model,
            index_embeddings=index_embeddings,
            embedding_limit=embedding_limit,
        )

    @app.tool()
    def memory_mvp_chat(
        tenant_id: str,
        workspace_id: str,
        user_id: str,
        agent_id: str,
        session_id: str,
        content: str,
        assistant_content: str,
        role: str = "user",
        message_id: str | None = None,
        assistant_message_id: str | None = None,
        cited_memory_ids: list[str] | None = None,
        turn_id: str | None = None,
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
    ) -> dict[str, Any]:
        scope = MemoryScope(
            tenant_id=tenant_id,
            workspace_id=workspace_id,
            user_id=user_id,
            agent_id=agent_id,
        )
        return service.mvp_chat(
            session_id=session_id,
            content=content,
            role=role,
            message_id=message_id,
            assistant_content=assistant_content,
            assistant_message_id=assistant_message_id,
            cited_memory_ids=cited_memory_ids or [],
            turn_id=turn_id,
            scope=scope,
            query=query,
            token_budget=token_budget,
            recall_limit=recall_limit,
            active_window_mode=active_window_mode,
            active_limit=active_limit,
            strict_session=strict_session,
            fold_l1=fold_l1,
            extract_l2=extract_l2,
            run_maintenance=run_maintenance,
            maintenance_model=maintenance_model,
            index_embeddings=index_embeddings,
            embedding_limit=embedding_limit,
        )

    @app.tool()
    def memory_fold_l1(session_id: str) -> list[dict[str, Any]]:
        return _models_json(service.fold_l1(session_id))

    @app.tool()
    def memory_extract_l2(session_id: str) -> list[dict[str, Any]]:
        return _models_json(service.extract_l2(session_id))

    @app.tool()
    def memory_cluster_l3(
        session_id: str | None = None,
        min_group_size: int = 2,
        limit: int = 1000,
    ) -> list[dict[str, Any]]:
        return _models_json(
            service.cluster_l3(
                session_id=session_id,
                min_group_size=min_group_size,
                limit=limit,
            )
        )

    @app.tool()
    def memory_build_semantic_l3(
        tenant_id: str,
        workspace_id: str,
        user_id: str,
        agent_id: str,
        model: str | None = None,
        threshold: float = 0.82,
        limit: int = 1000,
    ) -> dict[str, Any]:
        return service.build_semantic_l3(
            scope=MemoryScope(
                tenant_id=tenant_id,
                workspace_id=workspace_id,
                user_id=user_id,
                agent_id=agent_id,
            ),
            model=model,
            threshold=threshold,
            limit=limit,
        )

    @app.tool()
    def memory_distill_semantic_l4(
        tenant_id: str,
        workspace_id: str,
        user_id: str,
        agent_id: str,
        model: str | None = None,
        threshold: float = 0.84,
        limit: int = 1000,
    ) -> dict[str, Any]:
        return service.distill_semantic_l4(
            scope=MemoryScope(
                tenant_id=tenant_id,
                workspace_id=workspace_id,
                user_id=user_id,
                agent_id=agent_id,
            ),
            model=model,
            threshold=threshold,
            limit=limit,
        )

    @app.tool()
    def memory_cross_session_l3_candidates(
        model: str | None = None,
        threshold: float = 0.82,
        limit: int = 1000,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        return service.cross_session_l3_candidates(
            model=model,
            threshold=threshold,
            limit=limit,
            dry_run=dry_run,
        )

    @app.tool()
    def memory_build_l4_persona_candidates(
        min_support: int = 2,
        limit: int = 1000,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        return service.build_l4_persona_candidates(
            min_support=min_support,
            limit=limit,
            dry_run=dry_run,
        )

    @app.tool()
    def memory_recall(
        query: str,
        limit: int = 10,
        layers: list[str] | None = None,
        session_id: str | None = None,
        statuses: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        return _models_json(
            service.recall(
                query=query,
                limit=limit,
                layers=layers,
                session_id=session_id,
                statuses=statuses,
            )
        )

    @app.tool()
    def memory_drilldown(
        tenant_id: str,
        workspace_id: str,
        user_id: str,
        agent_id: str,
        memory_id: str,
        query: str | None = None,
    ) -> dict[str, Any] | None:
        return _json_value(
            service.drilldown(
                memory_id,
                scope=MemoryScope(
                    tenant_id=tenant_id,
                    workspace_id=workspace_id,
                    user_id=user_id,
                    agent_id=agent_id,
                ),
                query=query,
            )
        )

    @app.tool()
    def memory_build_context(
        query: str,
        token_budget: int = 1200,
        limit: int = 10,
        layers: list[str] | None = None,
        session_id: str | None = None,
        statuses: list[str] | None = None,
        active_window_mode: str | None = None,
        active_limit: int = 5,
        candidate_limit: int | None = None,
        strict_session: bool = False,
    ) -> dict[str, Any]:
        return _model_json(
            service.build_context(
                query=query,
                token_budget=token_budget,
                limit=limit,
                layers=layers,
                session_id=session_id,
                statuses=statuses,
                active_window_mode=active_window_mode,
                active_limit=active_limit,
                candidate_limit=candidate_limit,
                strict_session=strict_session,
            )
        )
    @app.tool()
    def memory_emerge(
        query: str,
        limit: int = 10,
        seed_limit: int = 8,
        max_hops: int = 2,
        layers: list[str] | None = None,
        session_id: str | None = None,
        statuses: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        return _models_json(
            service.emerge(
                query=query,
                limit=limit,
                seed_limit=seed_limit,
                max_hops=max_hops,
                layers=layers,
                session_id=session_id,
                statuses=statuses,
            )
        )

    @app.tool()
    def memory_active_window(
        limit: int = 10,
        token_budget: int = 1200,
        session_id: str | None = None,
        layers: list[str] | None = None,
        statuses: list[str] | None = None,
        strict_session: bool = False,
    ) -> dict[str, Any]:
        return _model_json(
            service.active_window(
                limit=limit,
                token_budget=token_budget,
                session_id=session_id,
                layers=layers,
                statuses=statuses,
                strict_session=strict_session,
            )
        )

    @app.tool()
    def memory_active_window_report(
        limit: int = 10,
        decision_limit: int = 100,
        session_id: str | None = None,
        layers: list[str] | None = None,
        statuses: list[str] | None = None,
        strict_session: bool = False,
    ) -> dict[str, Any]:
        return _model_json(
            service.active_window_report(
                limit=limit,
                decision_limit=decision_limit,
                session_id=session_id,
                layers=layers,
                statuses=statuses,
                strict_session=strict_session,
            )
        )

    @app.tool()
    def memory_build_fused_context(
        query: str,
        token_budget: int = 1200,
        recall_limit: int = 10,
        active_limit: int = 5,
        candidate_limit: int | None = None,
        layers: list[str] | None = None,
        session_id: str | None = None,
        statuses: list[str] | None = None,
        strict_session: bool = False,
    ) -> dict[str, Any]:
        return _model_json(
            service.build_fused_context(
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
        )

    @app.tool()
    def memory_build_fused_context_report(
        query: str,
        token_budget: int = 1200,
        recall_limit: int = 10,
        active_limit: int = 5,
        candidate_limit: int | None = None,
        layers: list[str] | None = None,
        session_id: str | None = None,
        statuses: list[str] | None = None,
        strict_session: bool = False,
    ) -> dict[str, Any]:
        return _model_json(
            service.build_fused_context_report(
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
        )

    @app.tool()
    def memory_compare_fused_context(
        query: str,
        token_budget: int = 1200,
        recall_limit: int = 10,
        active_limit: int = 5,
        candidate_limit: int | None = None,
        layers: list[str] | None = None,
        session_id: str | None = None,
        statuses: list[str] | None = None,
        strict_session: bool = False,
    ) -> dict[str, Any]:
        return _model_json(
            service.compare_fused_context(
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
        )

    @app.tool()
    def memory_compare_fused_context_batch(
        queries: list[str],
        token_budget: int = 1200,
        recall_limit: int = 10,
        active_limit: int = 5,
        candidate_limit: int | None = None,
        layers: list[str] | None = None,
        session_id: str | None = None,
        statuses: list[str] | None = None,
        strict_session: bool = False,
    ) -> dict[str, Any]:
        return _model_json(
            service.compare_fused_context_batch(
                queries=queries,
                token_budget=token_budget,
                recall_limit=recall_limit,
                active_limit=active_limit,
                candidate_limit=candidate_limit,
                layers=layers,
                session_id=session_id,
                statuses=statuses,
                strict_session=strict_session,
            )
        )

    @app.tool()
    def memory_review_queue(
        kind: str | None = None,
        include_reviewed: bool = False,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        return _models_json(
            service.review_queue(
                kind=kind,
                include_reviewed=include_reviewed,
                limit=limit,
            )
        )

    @app.tool()
    def memory_review_mark(
        target_type: str,
        target_id: str,
        status: str,
        kind: str | None = None,
        note: str | None = None,
    ) -> dict[str, Any] | None:
        return _optional_model_json(
            service.review_mark(
                target_type=target_type,
                target_id=target_id,
                status=status,
                kind=kind,
                note=note,
            )
        )

    @app.tool()
    def memory_review_plan(
        include_rejected: bool = False,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        return _models_json(
            service.review_plan(
                include_rejected=include_rejected,
                limit=limit,
            )
        )

    @app.tool()
    def memory_review_apply(
        plan_id: str,
        confirm: bool = False,
        second_confirm: bool = False,
    ) -> dict[str, Any] | None:
        return _optional_model_json(
            service.review_apply(
                plan_id,
                confirm=confirm,
                second_confirm=second_confirm,
            )
        )

    @app.tool()
    def memory_review_events(
        target_type: str | None = None,
        target_id: str | None = None,
        event_type: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        return _models_json(
            service.review_events(
                target_type=target_type,
                target_id=target_id,
                event_type=event_type,
                limit=limit,
            )
        )

    @app.tool()
    def memory_review_audit(
        event_limit: int = 1000,
        recent_limit: int = 10,
    ) -> dict[str, Any]:
        return _model_json(
            service.review_audit(
                event_limit=event_limit,
                recent_limit=recent_limit,
            )
        )

    @app.tool()
    def memory_feedback(
        tenant_id: str,
        workspace_id: str,
        user_id: str,
        agent_id: str,
        memory_id: str,
        signal: str,
    ) -> dict[str, Any] | None:
        return _optional_model_json(
            service.feedback(
                memory_id,
                signal,
                scope=MemoryScope(
                    tenant_id=tenant_id,
                    workspace_id=workspace_id,
                    user_id=user_id,
                    agent_id=agent_id,
                ),
            )
        )

    @app.tool()
    def memory_pin(
        tenant_id: str,
        workspace_id: str,
        user_id: str,
        agent_id: str,
        memory_id: str,
    ) -> dict[str, Any]:
        return _model_json(
            service.pin_memory(
                scope=MemoryScope(
                    tenant_id=tenant_id,
                    workspace_id=workspace_id,
                    user_id=user_id,
                    agent_id=agent_id,
                ),
                memory_id=memory_id,
                pinned=True,
            )
        )

    @app.tool()
    def memory_unpin(
        tenant_id: str,
        workspace_id: str,
        user_id: str,
        agent_id: str,
        memory_id: str,
    ) -> dict[str, Any]:
        return _model_json(
            service.pin_memory(
                scope=MemoryScope(
                    tenant_id=tenant_id,
                    workspace_id=workspace_id,
                    user_id=user_id,
                    agent_id=agent_id,
                ),
                memory_id=memory_id,
                pinned=False,
            )
        )

    @app.tool()
    def memory_delete(
        tenant_id: str,
        workspace_id: str,
        user_id: str,
        agent_id: str,
        memory_id: str,
        reason: str,
        physical: bool = False,
    ) -> dict[str, Any]:
        return service.delete_memory(
            scope=MemoryScope(
                tenant_id=tenant_id,
                workspace_id=workspace_id,
                user_id=user_id,
                agent_id=agent_id,
            ),
            memory_id=memory_id,
            reason=reason,
            physical=physical,
        )

    @app.tool()
    def memory_index_embeddings(limit: int = 100) -> dict[str, Any]:
        return service.index_embeddings(limit=limit)

    @app.tool()
    def memory_govern_lifecycle(
        limit: int = 1000,
        layer: str | None = None,
    ) -> dict[str, Any]:
        return service.govern_lifecycle(limit=limit, layer=layer)

    @app.tool()
    def memory_autonomous_governance_cycle(
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
    ) -> dict[str, Any]:
        return service.autonomous_governance_cycle(
            model=model,
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

    @app.tool()
    def memory_autonomous_governance_rollback(
        target_type: str,
        target_id: str,
        reason: str = "manual_autonomous_rollback",
    ) -> dict[str, Any]:
        return service.autonomous_governance_rollback(
            target_type=target_type,
            target_id=target_id,
            reason=reason,
        )

    @app.tool()
    def memory_maintenance_cycle(
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
    ) -> dict[str, Any]:
        return service.maintenance_cycle(
            model=model,
            index_embeddings=index_embeddings,
            embedding_limit=embedding_limit,
            governance_limit=governance_limit,
            semantic_threshold=semantic_threshold,
            semantic_mode=semantic_mode,
            l3_threshold=l3_threshold,
            persona_min_support=persona_min_support,
            use_autonomous_governance=use_autonomous_governance,
            autonomous_model_mode=autonomous_model_mode,
            autonomous_rule_fallback=autonomous_rule_fallback,
            apply_review_actions=apply_review_actions,
            review_action_limit=review_action_limit,
            include_rejected_review_actions=include_rejected_review_actions,
            allow_second_confirm_review_actions=allow_second_confirm_review_actions,
            dry_run=dry_run,
        )

    @app.tool()
    def memory_semantic_dedup(
        model: str | None = None,
        limit: int = 1000,
        threshold: float = 0.92,
        mode: str = "auto",
        dry_run: bool = False,
        auto_merge_threshold: float = 0.97,
        auto_tombstone_threshold: float = 0.97,
    ) -> dict[str, Any]:
        return service.semantic_dedup(
            model=model,
            limit=limit,
            threshold=threshold,
            mode=mode,
            dry_run=dry_run,
            auto_merge_threshold=auto_merge_threshold,
            auto_tombstone_threshold=auto_tombstone_threshold,
        )

    @app.tool()
    def memory_restore_semantic_merge(
        edge_id: str,
        reason: str = "manual_rollback",
    ) -> dict[str, Any]:
        return service.restore_semantic_merge(edge_id=edge_id, reason=reason)

    if profile == "runtime":
        runtime_tools = {
            "memory_prepare_turn",
            "memory_commit_turn",
            "memory_mvp_chat",
            "memory_drilldown",
            "memory_feedback",
            "memory_pin",
            "memory_unpin",
            "memory_delete",
        }
        registered_tools = (
            "memory_remember",
            "memory_runtime_cycle",
            "memory_runtime_session_cycle",
            "memory_fold_l1",
            "memory_extract_l2",
            "memory_cluster_l3",
            "memory_build_semantic_l3",
            "memory_distill_semantic_l4",
            "memory_cross_session_l3_candidates",
            "memory_build_l4_persona_candidates",
            "memory_recall",
            "memory_build_context",
            "memory_emerge",
            "memory_active_window",
            "memory_active_window_report",
            "memory_build_fused_context",
            "memory_build_fused_context_report",
            "memory_compare_fused_context",
            "memory_compare_fused_context_batch",
            "memory_review_queue",
            "memory_review_mark",
            "memory_review_plan",
            "memory_review_apply",
            "memory_review_events",
            "memory_review_audit",
            "memory_index_embeddings",
            "memory_govern_lifecycle",
            "memory_autonomous_governance_cycle",
            "memory_autonomous_governance_rollback",
            "memory_maintenance_cycle",
            "memory_semantic_dedup",
            "memory_restore_semantic_merge",
        )
        for tool_name in registered_tools:
            if tool_name not in runtime_tools:
                app.remove_tool(tool_name)

    return app


def run_mcp_server(
    db_path: str,
    transport: str = "stdio",
    profile: str = "runtime",
    host: str = "127.0.0.1",
    port: int = 8000,
) -> None:
    if transport not in {"stdio", "sse", "streamable-http"}:
        raise ValueError("Unsupported MCP transport: %s" % transport)
    token_verifier = None
    auth_settings = None
    if transport != "stdio":
        from altm.adapters.mcp.auth import (
            auth_settings_from_env,
            token_verifier_from_env,
        )

        token_verifier = token_verifier_from_env(profile)
        auth_settings = auth_settings_from_env(profile)
    app = create_mcp_server(
        db_path,
        profile=profile,
        token_verifier=token_verifier,
        auth_settings=auth_settings,
        host=host,
        port=port,
    )
    app.run(transport=transport)
