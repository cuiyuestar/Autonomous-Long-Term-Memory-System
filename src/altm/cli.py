"""Command line entry points for scaffold verification."""

from __future__ import annotations

import argparse
import json
import time
import uuid
from collections.abc import Sequence

from pydantic import BaseModel

from altm.adapters.mcp import run_mcp_server
from altm.application import AltmApplication
from altm.contracts import (
    AccessSignal,
    MemoryLayer,
    MemoryScope,
    MemoryStatus,
    MessageRole,
    ReviewItemKind,
    ReviewStatus,
)
from altm.governance import SEMANTIC_DEDUP_MODES


def _add_scope_arguments(
    parser: argparse.ArgumentParser,
    include_db: bool = True,
) -> None:
    if include_db:
        parser.add_argument("--db", required=True, help="Path to the SQLite database")
    parser.add_argument("--tenant-id", required=True)
    parser.add_argument("--workspace-id", required=True)
    parser.add_argument("--user-id", required=True)
    parser.add_argument("--agent-id", required=True)


def _scope_from_args(args: argparse.Namespace) -> MemoryScope:
    return MemoryScope(
        tenant_id=args.tenant_id,
        workspace_id=args.workspace_id,
        user_id=args.user_id,
        agent_id=args.agent_id,
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="altm")
    subparsers = parser.add_subparsers(dest="command", required=True)

    init_db = subparsers.add_parser("init-db", help="Initialize a SQLite memory database")
    init_db.add_argument("--db", required=True, help="Path to the SQLite database")
    init_db.add_argument("--schema", help="Optional schema path override")

    capture = subparsers.add_parser("capture", help="Capture one message into L0")
    capture.add_argument("--db", required=True, help="Path to the SQLite database")
    capture.add_argument("--session-id", required=True, help="Session identifier")
    capture.add_argument("--content", required=True, help="Raw message content")
    capture.add_argument("--role", choices=[role.value for role in MessageRole], default="user")
    capture.add_argument("--message-id", help="Optional source message id")

    prepare_turn = subparsers.add_parser(
        "prepare-turn",
        help="Persist a user turn and return scoped context for a Host Agent",
    )
    _add_scope_arguments(prepare_turn)
    prepare_turn.add_argument("--session-id", required=True)
    prepare_turn.add_argument("--turn-id", required=True)
    prepare_turn.add_argument("--content", required=True)
    prepare_turn.add_argument("--message-id")
    prepare_turn.add_argument("--query")
    prepare_turn.add_argument("--token-budget", type=int, default=1200)
    prepare_turn.add_argument("--recall-limit", type=int, default=10)
    prepare_turn.add_argument("--active-window-mode", choices=["off", "limited", "full"])
    prepare_turn.add_argument("--active-limit", type=int, default=5)
    prepare_turn.add_argument("--strict-session", action="store_true")

    commit_turn = subparsers.add_parser(
        "commit-turn",
        help="Persist a real Host Agent response and its verified memory citations",
    )
    _add_scope_arguments(commit_turn)
    commit_turn.add_argument("--cycle-id", required=True)
    commit_turn.add_argument("--assistant-content", required=True)
    commit_turn.add_argument("--assistant-message-id")
    commit_turn.add_argument("--cited-memory-id", action="append")

    pin_memory = subparsers.add_parser(
        "pin-memory",
        help="Pin or unpin a scoped memory as an explicit user signal",
    )
    _add_scope_arguments(pin_memory)
    pin_memory.add_argument("--memory-id", required=True)
    pin_memory.add_argument("--unpin", action="store_true")

    delete_memory = subparsers.add_parser(
        "delete-memory",
        help="Apply an audited tombstone or explicit physical deletion",
    )
    _add_scope_arguments(delete_memory)
    delete_memory.add_argument("--memory-id", required=True)
    delete_memory.add_argument("--reason", required=True)
    delete_memory.add_argument("--physical", action="store_true")

    worker = subparsers.add_parser(
        "worker",
        help="Process persistent ALTM background jobs with SQLite leases",
    )
    worker.add_argument("--db", required=True)
    worker.add_argument("--worker-id", default="worker-%s" % uuid.uuid4().hex)
    worker.add_argument("--lease-seconds", type=int, default=120)
    worker.add_argument("--max-jobs", type=int, default=0, help="0 means run continuously")
    worker.add_argument("--poll-seconds", type=float, default=1.0)

    runtime_cycle = subparsers.add_parser(
        "runtime-cycle",
        help="Run the MVP memory loop: capture, fold, optional L2 extraction, maintenance, and context",
    )
    runtime_cycle.add_argument("--db", required=True, help="Path to the SQLite database")
    runtime_cycle.add_argument("--session-id", required=True, help="Session identifier")
    runtime_cycle.add_argument("--content", required=True, help="Incoming message content")
    runtime_cycle.add_argument("--role", choices=[role.value for role in MessageRole], default="user")
    runtime_cycle.add_argument("--message-id", help="Optional source message id")
    runtime_cycle.add_argument("--query", help="Optional context query; defaults to content")
    runtime_cycle.add_argument("--token-budget", type=int, default=1200)
    runtime_cycle.add_argument("--recall-limit", type=int, default=10)
    runtime_cycle.add_argument("--active-window-mode", choices=["off", "limited", "full"])
    runtime_cycle.add_argument("--active-limit", type=int, default=5)
    runtime_cycle.add_argument("--strict-session", action="store_true")
    runtime_cycle.add_argument("--skip-fold-l1", action="store_true")
    runtime_cycle.add_argument("--skip-extract-l2", action="store_true")
    runtime_cycle.add_argument("--skip-maintenance", action="store_true")
    runtime_cycle.add_argument("--maintenance-model", help="Embedding model for maintenance; defaults to env")
    runtime_cycle.add_argument("--index-embeddings", action="store_true")
    runtime_cycle.add_argument("--embedding-limit", type=int, default=100)

    runtime_session = subparsers.add_parser(
        "runtime-session-cycle",
        help="Run the MVP memory loop for a full session transcript",
    )
    runtime_session.add_argument("--db", required=True, help="Path to the SQLite database")
    runtime_session.add_argument("--session-id", required=True, help="Session identifier")
    runtime_session.add_argument(
        "--message",
        action="append",
        required=True,
        help="Message as 'role:content' or raw user content; can be repeated",
    )
    runtime_session.add_argument("--query", help="Optional context query; defaults to last message")
    runtime_session.add_argument("--token-budget", type=int, default=1200)
    runtime_session.add_argument("--recall-limit", type=int, default=10)
    runtime_session.add_argument("--active-window-mode", choices=["off", "limited", "full"])
    runtime_session.add_argument("--active-limit", type=int, default=5)
    runtime_session.add_argument("--strict-session", action="store_true")
    runtime_session.add_argument("--skip-fold-l1", action="store_true")
    runtime_session.add_argument("--skip-extract-l2", action="store_true")
    runtime_session.add_argument("--skip-maintenance", action="store_true")
    runtime_session.add_argument("--maintenance-model", help="Embedding model for maintenance; defaults to env")
    runtime_session.add_argument("--index-embeddings", action="store_true")
    runtime_session.add_argument("--embedding-limit", type=int, default=100)

    mvp_chat = subparsers.add_parser(
        "mvp-chat",
        help="Run one interactive MVP chat turn with memory writeback and feedback",
    )
    mvp_chat.add_argument("--db", required=True, help="Path to the SQLite database")
    _add_scope_arguments(mvp_chat, include_db=False)
    mvp_chat.add_argument("--session-id", required=True, help="Session identifier")
    mvp_chat.add_argument(
        "--content",
        required=True,
        help="Incoming user message content",
    )
    mvp_chat.add_argument("--role", choices=[role.value for role in MessageRole], default="user")
    mvp_chat.add_argument("--message-id", help="Optional source user message id")
    mvp_chat.add_argument(
        "--assistant-content",
        required=True,
        help="Real response generated by the Host Agent",
    )
    mvp_chat.add_argument("--assistant-message-id", help="Optional assistant message id")
    mvp_chat.add_argument("--cited-memory-id", action="append")
    mvp_chat.add_argument("--turn-id")
    mvp_chat.add_argument("--query", help="Optional context query; defaults to content")
    mvp_chat.add_argument("--token-budget", type=int, default=1200)
    mvp_chat.add_argument("--recall-limit", type=int, default=10)
    mvp_chat.add_argument("--active-window-mode", choices=["off", "limited", "full"])
    mvp_chat.add_argument("--active-limit", type=int, default=5)
    mvp_chat.add_argument("--strict-session", action="store_true")
    mvp_chat.add_argument("--skip-fold-l1", action="store_true")
    mvp_chat.add_argument("--skip-extract-l2", action="store_true")
    mvp_chat.add_argument("--skip-maintenance", action="store_true")
    mvp_chat.add_argument("--maintenance-model", help="Embedding model for maintenance; defaults to env")
    mvp_chat.add_argument("--index-embeddings", action="store_true")
    mvp_chat.add_argument("--embedding-limit", type=int, default=100)

    fold_l1 = subparsers.add_parser("fold-l1", help="Generate a rule-based L1 capsule")
    fold_l1.add_argument("--db", required=True, help="Path to the SQLite database")
    fold_l1.add_argument("--session-id", required=True, help="Session identifier")

    extract_l2 = subparsers.add_parser("extract-l2", help="Extract L2 atoms with an LLM")
    extract_l2.add_argument("--db", required=True, help="Path to the SQLite database")
    extract_l2.add_argument("--session-id", required=True, help="Session identifier")

    search = subparsers.add_parser("search", help="Search memory through SQLite FTS")
    search.add_argument("--db", required=True, help="Path to the SQLite database")
    search.add_argument("--query", required=True, help="FTS query text")
    search.add_argument("--limit", type=int, default=10, help="Maximum result count")
    search.add_argument("--layer", action="append", choices=[layer.value for layer in MemoryLayer])
    search.add_argument("--status", action="append", choices=[status.value for status in MemoryStatus])
    search.add_argument("--session-id", help="Optional session filter")

    emerge = subparsers.add_parser(
        "emerge",
        help="Run query-induced graph emergence from direct recall entry points",
    )
    emerge.add_argument("--db", required=True, help="Path to the SQLite database")
    emerge.add_argument("--query", required=True, help="Recall query text")
    emerge.add_argument("--limit", type=int, default=10, help="Maximum emerged candidates")
    emerge.add_argument("--seed-limit", type=int, default=8, help="Maximum direct recall entry points")
    emerge.add_argument("--max-hops", type=int, default=2, help="Maximum graph expansion hops")
    emerge.add_argument("--layer", action="append", choices=[layer.value for layer in MemoryLayer])
    emerge.add_argument("--status", action="append", choices=[status.value for status in MemoryStatus])
    emerge.add_argument("--session-id", help="Optional session filter")

    build_context = subparsers.add_parser(
        "build-context",
        help="Recall memory and assemble a context bundle for agent injection",
    )
    build_context.add_argument("--db", required=True, help="Path to the SQLite database")
    build_context.add_argument("--query", required=True, help="Recall query text")
    build_context.add_argument("--limit", type=int, default=10, help="Maximum recall candidates")
    build_context.add_argument("--token-budget", type=int, default=1200, help="Context token budget")
    build_context.add_argument("--layer", action="append", choices=[layer.value for layer in MemoryLayer])
    build_context.add_argument("--status", action="append", choices=[status.value for status in MemoryStatus])
    build_context.add_argument("--session-id", help="Optional session filter")
    build_context.add_argument(
        "--active-window-mode",
        choices=["off", "limited", "full"],
        default=None,
        help="Active-window fusion mode; omitted means config-driven default",
    )
    build_context.add_argument("--active-limit", type=int, default=5, help="Maximum active-window candidates")
    build_context.add_argument("--candidate-limit", type=int, help="Maximum fused candidates before assembly")
    build_context.add_argument(
        "--strict-session",
        action="store_true",
        help="Only include active-window memories from the provided session id",
    )

    active_window = subparsers.add_parser(
        "active-window",
        help="Build a query-before global active memory window",
    )
    active_window.add_argument("--db", required=True, help="Path to the SQLite database")
    active_window.add_argument("--limit", type=int, default=10, help="Maximum active candidates")
    active_window.add_argument("--token-budget", type=int, default=1200, help="Context token budget")
    active_window.add_argument("--layer", action="append", choices=[layer.value for layer in MemoryLayer])
    active_window.add_argument("--status", action="append", choices=[status.value for status in MemoryStatus])
    active_window.add_argument("--session-id", help="Optional session affinity")
    active_window.add_argument(
        "--strict-session",
        action="store_true",
        help="Only include memories from the provided session id",
    )

    active_window_report = subparsers.add_parser(
        "active-window-report",
        help="Explain selected and filtered global active window candidates",
    )
    active_window_report.add_argument("--db", required=True, help="Path to the SQLite database")
    active_window_report.add_argument("--limit", type=int, default=10, help="Maximum active candidates")
    active_window_report.add_argument(
        "--decision-limit",
        type=int,
        default=100,
        help="Maximum inclusion/filter decisions to return",
    )
    active_window_report.add_argument("--layer", action="append", choices=[layer.value for layer in MemoryLayer])
    active_window_report.add_argument("--status", action="append", choices=[status.value for status in MemoryStatus])
    active_window_report.add_argument("--session-id", help="Optional session affinity")
    active_window_report.add_argument(
        "--strict-session",
        action="store_true",
        help="Only include memories from the provided session id",
    )

    build_fused_context = subparsers.add_parser(
        "build-fused-context",
        help="Explicitly fuse query recall with the global active window into a context bundle",
    )
    build_fused_context.add_argument("--db", required=True, help="Path to the SQLite database")
    build_fused_context.add_argument("--query", required=True, help="Recall query text")
    build_fused_context.add_argument("--recall-limit", type=int, default=10, help="Maximum query recall candidates")
    build_fused_context.add_argument("--active-limit", type=int, default=5, help="Maximum active-window candidates")
    build_fused_context.add_argument("--candidate-limit", type=int, help="Maximum fused candidates before context assembly")
    build_fused_context.add_argument("--token-budget", type=int, default=1200, help="Context token budget")
    build_fused_context.add_argument("--layer", action="append", choices=[layer.value for layer in MemoryLayer])
    build_fused_context.add_argument("--status", action="append", choices=[status.value for status in MemoryStatus])
    build_fused_context.add_argument("--session-id", help="Optional session affinity/filter")
    build_fused_context.add_argument(
        "--strict-session",
        action="store_true",
        help="Only include active-window memories from the provided session id",
    )

    build_fused_context_report = subparsers.add_parser(
        "build-fused-context-report",
        help="Explain explicit query recall and active-window context fusion",
    )
    build_fused_context_report.add_argument("--db", required=True, help="Path to the SQLite database")
    build_fused_context_report.add_argument("--query", required=True, help="Recall query text")
    build_fused_context_report.add_argument("--recall-limit", type=int, default=10, help="Maximum query recall candidates")
    build_fused_context_report.add_argument("--active-limit", type=int, default=5, help="Maximum active-window candidates")
    build_fused_context_report.add_argument(
        "--candidate-limit",
        type=int,
        help="Maximum fused candidates before context assembly",
    )
    build_fused_context_report.add_argument("--token-budget", type=int, default=1200, help="Context token budget")
    build_fused_context_report.add_argument("--layer", action="append", choices=[layer.value for layer in MemoryLayer])
    build_fused_context_report.add_argument("--status", action="append", choices=[status.value for status in MemoryStatus])
    build_fused_context_report.add_argument("--session-id", help="Optional session affinity/filter")
    build_fused_context_report.add_argument(
        "--strict-session",
        action="store_true",
        help="Only include active-window memories from the provided session id",
    )

    compare_fused_context = subparsers.add_parser(
        "compare-fused-context",
        help="Compare default query context with explicit active-window fusion",
    )
    compare_fused_context.add_argument("--db", required=True, help="Path to the SQLite database")
    compare_fused_context.add_argument("--query", required=True, help="Recall query text")
    compare_fused_context.add_argument("--recall-limit", type=int, default=10, help="Maximum query recall candidates")
    compare_fused_context.add_argument("--active-limit", type=int, default=5, help="Maximum active-window candidates")
    compare_fused_context.add_argument(
        "--candidate-limit",
        type=int,
        help="Maximum fused candidates before context assembly",
    )
    compare_fused_context.add_argument("--token-budget", type=int, default=1200, help="Context token budget")
    compare_fused_context.add_argument("--layer", action="append", choices=[layer.value for layer in MemoryLayer])
    compare_fused_context.add_argument("--status", action="append", choices=[status.value for status in MemoryStatus])
    compare_fused_context.add_argument("--session-id", help="Optional session affinity/filter")
    compare_fused_context.add_argument(
        "--strict-session",
        action="store_true",
        help="Only include active-window memories from the provided session id",
    )

    compare_fused_context_batch = subparsers.add_parser(
        "compare-fused-context-batch",
        help="Compare default and fused contexts across multiple queries",
    )
    compare_fused_context_batch.add_argument("--db", required=True, help="Path to the SQLite database")
    compare_fused_context_batch.add_argument(
        "--query",
        action="append",
        required=True,
        help="Recall query text; can be repeated",
    )
    compare_fused_context_batch.add_argument("--recall-limit", type=int, default=10, help="Maximum query recall candidates")
    compare_fused_context_batch.add_argument("--active-limit", type=int, default=5, help="Maximum active-window candidates")
    compare_fused_context_batch.add_argument(
        "--candidate-limit",
        type=int,
        help="Maximum fused candidates before context assembly",
    )
    compare_fused_context_batch.add_argument("--token-budget", type=int, default=1200, help="Context token budget")
    compare_fused_context_batch.add_argument("--layer", action="append", choices=[layer.value for layer in MemoryLayer])
    compare_fused_context_batch.add_argument("--status", action="append", choices=[status.value for status in MemoryStatus])
    compare_fused_context_batch.add_argument("--session-id", help="Optional session affinity/filter")
    compare_fused_context_batch.add_argument(
        "--strict-session",
        action="store_true",
        help="Only include active-window memories from the provided session id",
    )

    index_embeddings = subparsers.add_parser(
        "index-embeddings",
        help="Index missing or stale MemoryUnits into the configured embedding cache",
    )
    index_embeddings.add_argument("--db", required=True, help="Path to the SQLite database")
    index_embeddings.add_argument(
        "--limit",
        type=int,
        default=100,
        help="Maximum MemoryUnits to index in this run",
    )

    feedback = subparsers.add_parser("feedback", help="Record lifecycle feedback signal")
    feedback.add_argument("--db", required=True, help="Path to the SQLite database")
    feedback.add_argument("--memory-id", required=True, help="MemoryUnit identifier")
    feedback.add_argument("--signal", required=True, choices=[signal.value for signal in AccessSignal])

    govern_lifecycle = subparsers.add_parser(
        "govern-lifecycle",
        help="Run one lifecycle scoring and candidate marking cycle",
    )
    govern_lifecycle.add_argument("--db", required=True, help="Path to the SQLite database")
    govern_lifecycle.add_argument("--limit", type=int, default=1000, help="Maximum MemoryUnits")
    govern_lifecycle.add_argument("--layer", choices=[layer.value for layer in MemoryLayer])

    maintenance_cycle = subparsers.add_parser(
        "maintenance-cycle",
        help="Run an explicit autonomous memory maintenance cycle",
    )
    maintenance_cycle.add_argument("--db", required=True, help="Path to the SQLite database")
    maintenance_cycle.add_argument("--model", help="Embedding model name; defaults to env")
    maintenance_cycle.add_argument(
        "--index-embeddings",
        action="store_true",
        help="Also index missing embeddings before governance steps",
    )
    maintenance_cycle.add_argument("--embedding-limit", type=int, default=100)
    maintenance_cycle.add_argument("--governance-limit", type=int, default=1000)
    maintenance_cycle.add_argument("--semantic-threshold", type=float, default=0.92)
    maintenance_cycle.add_argument(
        "--semantic-mode",
        choices=sorted(SEMANTIC_DEDUP_MODES),
        default="auto",
    )
    maintenance_cycle.add_argument("--l3-threshold", type=float, default=0.82)
    maintenance_cycle.add_argument("--persona-min-support", type=int, default=2)
    maintenance_cycle.add_argument(
        "--skip-autonomous-governance",
        action="store_true",
        help="Disable the default autonomous governance engine in this cycle",
    )
    maintenance_cycle.add_argument(
        "--autonomous-model-mode",
        choices=["auto", "llm", "off"],
        default="auto",
        help="Model mode; missing or low-confidence semantic models defer high-risk actions",
    )
    maintenance_cycle.add_argument(
        "--apply-review-actions-compat",
        action="store_true",
        help="Compatibility only: also run legacy review action application",
    )
    maintenance_cycle.add_argument(
        "--skip-review-actions",
        action="store_true",
        help="Compatibility alias: keep legacy review action application disabled",
    )
    maintenance_cycle.add_argument(
        "--review-action-limit",
        type=int,
        default=100,
        help="Maximum reviewed action plans to apply or preview",
    )
    maintenance_cycle.add_argument(
        "--approved-only-review-actions",
        action="store_true",
        help="Only apply approved review actions; rejected review outcomes are skipped",
    )
    maintenance_cycle.add_argument(
        "--allow-second-confirm-review-actions",
        action="store_true",
        help="Allow maintenance-cycle to apply plans that require second confirmation",
    )
    maintenance_cycle.add_argument(
        "--dry-run",
        action="store_true",
        help="Preview destructive/candidate-writing steps where supported",
    )

    autonomous_governance = subparsers.add_parser(
        "autonomous-governance-cycle",
        help="Run autonomous governance decisions without human review gates",
    )
    autonomous_governance.add_argument("--db", required=True, help="Path to the SQLite database")
    autonomous_governance.add_argument("--model", help="Embedding model name; defaults to env")
    autonomous_governance.add_argument("--semantic-threshold", type=float, default=0.92)
    autonomous_governance.add_argument("--semantic-auto-merge-threshold", type=float, default=0.97)
    autonomous_governance.add_argument("--semantic-auto-tombstone-threshold", type=float, default=0.97)
    autonomous_governance.add_argument("--l3-threshold", type=float, default=0.82)
    autonomous_governance.add_argument("--persona-min-support", type=int, default=2)
    autonomous_governance.add_argument("--limit", type=int, default=1000)
    autonomous_governance.add_argument(
        "--model-mode",
        choices=["auto", "llm", "off"],
        default="auto",
        help="Model mode; missing or low-confidence semantic models defer high-risk actions",
    )
    autonomous_governance.add_argument(
        "--dry-run",
        action="store_true",
        help="Preview autonomous governance decisions without writing changes",
    )

    autonomous_rollback = subparsers.add_parser(
        "autonomous-governance-rollback",
        help="Rollback an autonomous governance action by target",
    )
    autonomous_rollback.add_argument("--db", required=True, help="Path to the SQLite database")
    autonomous_rollback.add_argument(
        "--target-type",
        required=True,
        choices=["memory_unit", "graph_edge"],
        help="Target type to rollback",
    )
    autonomous_rollback.add_argument("--target-id", required=True, help="Target id")
    autonomous_rollback.add_argument(
        "--reason",
        default="manual_autonomous_rollback",
        help="Rollback reason stored in autonomous audit events",
    )

    semantic_dedup = subparsers.add_parser(
        "semantic-dedup",
        help="Mark and optionally auto-resolve high-similarity L2 duplicates from cached embeddings",
    )
    semantic_dedup.add_argument("--db", required=True, help="Path to the SQLite database")
    semantic_dedup.add_argument("--model", help="Embedding model name; defaults to env")
    semantic_dedup.add_argument("--limit", type=int, default=1000, help="Maximum L2 MemoryUnits")
    semantic_dedup.add_argument("--threshold", type=float, default=0.92, help="Cosine threshold")
    semantic_dedup.add_argument(
        "--mode",
        choices=sorted(SEMANTIC_DEDUP_MODES),
        default="auto",
        help="Resolution mode; auto follows high-risk flags",
    )
    semantic_dedup.add_argument(
        "--dry-run",
        action="store_true",
        help="Preview candidates and guarded resolutions without writing graph edges or merges",
    )
    semantic_dedup.add_argument(
        "--auto-merge-threshold",
        type=float,
        default=0.97,
        help="Minimum cosine required for automatic protected merge",
    )
    semantic_dedup.add_argument(
        "--auto-tombstone-threshold",
        type=float,
        default=0.97,
        help="Minimum cosine required when automatic tombstone is enabled",
    )

    restore_semantic_merge = subparsers.add_parser(
        "restore-semantic-merge",
        help="Rollback an automatic L2 semantic merge by graph edge id",
    )
    restore_semantic_merge.add_argument("--db", required=True, help="Path to the SQLite database")
    restore_semantic_merge.add_argument("--edge-id", required=True, help="Semantic duplicate edge id")
    restore_semantic_merge.add_argument(
        "--reason",
        default="manual_rollback",
        help="Rollback reason stored in audit metadata",
    )

    cluster_l3 = subparsers.add_parser(
        "cluster-l3",
        help="Build rule-based L3 scenes from grouped L2 memories",
    )
    cluster_l3.add_argument("--db", required=True, help="Path to the SQLite database")
    cluster_l3.add_argument("--session-id", help="Optional session filter")
    cluster_l3.add_argument("--min-group-size", type=int, default=2, help="Minimum L2 per scene")
    cluster_l3.add_argument("--limit", type=int, default=1000, help="Maximum L2 MemoryUnits")

    cross_session_l3 = subparsers.add_parser(
        "cross-session-l3-candidates",
        help="Find cross-session L3 scene candidates from cached L2 embeddings",
    )
    cross_session_l3.add_argument("--db", required=True, help="Path to the SQLite database")
    cross_session_l3.add_argument("--model", help="Embedding model name; defaults to env")
    cross_session_l3.add_argument("--threshold", type=float, default=0.82, help="Cosine threshold")
    cross_session_l3.add_argument("--limit", type=int, default=1000, help="Maximum L2 MemoryUnits")
    cross_session_l3.add_argument(
        "--dry-run",
        action="store_true",
        help="Preview candidates without writing graph edges",
    )

    l4_persona = subparsers.add_parser(
        "build-l4-persona-candidates",
        help="Build observing L4/persona candidates from reviewed L2 memories",
    )
    l4_persona.add_argument("--db", required=True, help="Path to the SQLite database")
    l4_persona.add_argument("--min-support", type=int, default=2, help="Minimum L2 support count")
    l4_persona.add_argument("--limit", type=int, default=1000, help="Maximum L2 MemoryUnits")
    l4_persona.add_argument(
        "--dry-run",
        action="store_true",
        help="Preview L4 candidates without writing MemoryUnits",
    )

    review_queue = subparsers.add_parser(
        "review-queue",
        help="Legacy/debug: list compatibility review candidates outside the autonomous runtime path",
    )
    review_queue.add_argument("--db", required=True, help="Path to the SQLite database")
    review_queue.add_argument("--kind", choices=[kind.value for kind in ReviewItemKind])
    review_queue.add_argument("--limit", type=int, default=100, help="Maximum review items")
    review_queue.add_argument(
        "--include-reviewed",
        action="store_true",
        help="Include approved/rejected items",
    )

    review_mark = subparsers.add_parser(
        "review-mark",
        help="Legacy/debug: mark a compatibility review target outside the autonomous runtime path",
    )
    review_mark.add_argument("--db", required=True, help="Path to the SQLite database")
    review_mark.add_argument(
        "--target-type",
        required=True,
        choices=["memory_unit", "graph_edge"],
        help="Review target type",
    )
    review_mark.add_argument("--target-id", required=True, help="MemoryUnit id or graph edge id")
    review_mark.add_argument("--status", required=True, choices=[status.value for status in ReviewStatus])
    review_mark.add_argument(
        "--kind",
        choices=[kind.value for kind in ReviewItemKind],
        help="Optional review item kind for ambiguous memory_unit targets",
    )
    review_mark.add_argument("--note", help="Optional human review note")

    review_plan = subparsers.add_parser(
        "review-plan",
        help="Legacy/debug: preview compatibility action plans outside the autonomous runtime path",
    )
    review_plan.add_argument("--db", required=True, help="Path to the SQLite database")
    review_plan.add_argument("--limit", type=int, default=100, help="Maximum planned actions")
    review_plan.add_argument(
        "--include-rejected",
        action="store_true",
        help="Include actions for rejected review items",
    )

    review_apply = subparsers.add_parser(
        "review-apply",
        help="Legacy/debug: apply a compatibility action plan outside the autonomous runtime path",
    )
    review_apply.add_argument("--db", required=True, help="Path to the SQLite database")
    review_apply.add_argument("--plan-id", required=True, help="Review action plan id")
    review_apply.add_argument(
        "--confirm",
        action="store_true",
        help="Required to modify the database; omitted means dry-run",
    )
    review_apply.add_argument(
        "--second-confirm",
        action="store_true",
        help="Required for actions marked as requiring second confirmation",
    )

    review_events = subparsers.add_parser(
        "review-events",
        help="List append-only review audit events",
    )
    review_events.add_argument("--db", required=True, help="Path to the SQLite database")
    review_events.add_argument("--target-type", help="Optional review target type filter")
    review_events.add_argument("--target-id", help="Optional review target id filter")
    review_events.add_argument("--event-type", help="Optional event type filter")
    review_events.add_argument("--limit", type=int, default=100, help="Maximum review events")

    review_audit = subparsers.add_parser(
        "review-audit",
        help="Summarize review queue, action plans, and audit events",
    )
    review_audit.add_argument("--db", required=True, help="Path to the SQLite database")
    review_audit.add_argument("--event-limit", type=int, default=1000, help="Maximum events to scan")
    review_audit.add_argument("--recent-limit", type=int, default=10, help="Recent events to include")

    mcp_server = subparsers.add_parser("mcp-server", help="Run the MCP adapter server")
    mcp_server.add_argument("--db", required=True, help="Path to the SQLite database")
    mcp_server.add_argument(
        "--transport",
        choices=["stdio", "sse", "streamable-http"],
        default="stdio",
        help="MCP transport mode",
    )
    mcp_server.add_argument(
        "--profile",
        choices=["runtime", "admin"],
        default="runtime",
        help="Runtime exposes safe Agent tools; admin also exposes governance/debug tools",
    )
    mcp_server.add_argument("--host", default="127.0.0.1")
    mcp_server.add_argument("--port", type=int, default=8000)

    return parser


def _dump(value: object) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2))


def _model_json(model: BaseModel | None) -> dict[str, object] | None:
    if model is None:
        return None
    return model.model_dump(mode="json")


def _models_json(models: Sequence[BaseModel]) -> list[dict[str, object] | None]:
    return [_model_json(model) for model in models]


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    if args.command == "init-db":
        AltmApplication(args.db, schema_path=args.schema).initialize_store()
        print("initialized sqlite memory store: %s" % args.db)
        return 0

    if args.command == "mcp-server":
        run_mcp_server(
            db_path=args.db,
            transport=args.transport,
            profile=args.profile,
            host=args.host,
            port=args.port,
        )
        return 0

    app = AltmApplication(args.db)

    if args.command == "prepare-turn":
        _dump(
            _model_json(
                app.prepare_turn(
                    tenant_id=args.tenant_id,
                    workspace_id=args.workspace_id,
                    user_id=args.user_id,
                    agent_id=args.agent_id,
                    session_id=args.session_id,
                    turn_id=args.turn_id,
                    content=args.content,
                    message_id=args.message_id,
                    query=args.query,
                    token_budget=args.token_budget,
                    recall_limit=args.recall_limit,
                    active_window_mode=args.active_window_mode,
                    active_limit=args.active_limit,
                    strict_session=args.strict_session,
                )
            )
        )
        return 0

    if args.command == "commit-turn":
        _dump(
            _model_json(
                app.commit_turn(
                    tenant_id=args.tenant_id,
                    workspace_id=args.workspace_id,
                    user_id=args.user_id,
                    agent_id=args.agent_id,
                    cycle_id=args.cycle_id,
                    assistant_content=args.assistant_content,
                    cited_memory_ids=args.cited_memory_id or [],
                    assistant_message_id=args.assistant_message_id,
                )
            )
        )
        return 0

    if args.command == "pin-memory":
        _dump(
            _model_json(
                app.pin_memory(
                    scope=_scope_from_args(args),
                    memory_id=args.memory_id,
                    pinned=not args.unpin,
                )
            )
        )
        return 0

    if args.command == "delete-memory":
        _dump(
            app.delete_memory(
                scope=_scope_from_args(args),
                memory_id=args.memory_id,
                reason=args.reason,
                physical=args.physical,
            )
        )
        return 0

    if args.command == "worker":
        processed = 0
        while args.max_jobs <= 0 or processed < args.max_jobs:
            result = app.process_next_job(
                worker_id=args.worker_id,
                lease_seconds=args.lease_seconds,
            )
            if result["status"] == "idle":
                if args.max_jobs > 0:
                    _dump(result)
                    return 0
                time.sleep(max(0.05, args.poll_seconds))
                continue
            _dump(result)
            processed += 1
        return 0

    if args.command == "capture":
        memory = app.remember(
            session_id=args.session_id,
            content=args.content,
            role=args.role,
            message_id=args.message_id,
        )
        _dump(_model_json(memory))
        return 0

    if args.command == "runtime-cycle":
        _dump(
            app.runtime_cycle(
                session_id=args.session_id,
                content=args.content,
                role=args.role,
                message_id=args.message_id,
                query=args.query,
                token_budget=args.token_budget,
                recall_limit=args.recall_limit,
                active_window_mode=args.active_window_mode,
                active_limit=args.active_limit,
                strict_session=args.strict_session,
                fold_l1=not args.skip_fold_l1,
                extract_l2=not args.skip_extract_l2,
                run_maintenance=not args.skip_maintenance,
                maintenance_model=args.maintenance_model,
                index_embeddings=args.index_embeddings,
                embedding_limit=args.embedding_limit,
            )
        )
        return 0

    if args.command == "runtime-session-cycle":
        _dump(
            app.runtime_session_cycle(
                session_id=args.session_id,
                messages=[_parse_runtime_message(value) for value in args.message],
                query=args.query,
                token_budget=args.token_budget,
                recall_limit=args.recall_limit,
                active_window_mode=args.active_window_mode,
                active_limit=args.active_limit,
                strict_session=args.strict_session,
                fold_l1=not args.skip_fold_l1,
                extract_l2=not args.skip_extract_l2,
                run_maintenance=not args.skip_maintenance,
                maintenance_model=args.maintenance_model,
                index_embeddings=args.index_embeddings,
                embedding_limit=args.embedding_limit,
            )
        )
        return 0

    if args.command == "mvp-chat":
        _dump(
            app.mvp_chat(
                session_id=args.session_id,
                content=args.content,
                role=args.role,
                message_id=args.message_id,
                assistant_content=args.assistant_content,
                assistant_message_id=args.assistant_message_id,
                cited_memory_ids=args.cited_memory_id or [],
                turn_id=args.turn_id,
                scope=_scope_from_args(args),
                query=args.query,
                token_budget=args.token_budget,
                recall_limit=args.recall_limit,
                active_window_mode=args.active_window_mode,
                active_limit=args.active_limit,
                strict_session=args.strict_session,
                fold_l1=not args.skip_fold_l1,
                extract_l2=not args.skip_extract_l2,
                run_maintenance=not args.skip_maintenance,
                maintenance_model=args.maintenance_model,
                index_embeddings=args.index_embeddings,
                embedding_limit=args.embedding_limit,
            )
        )
        return 0

    if args.command == "fold-l1":
        _dump(_models_json(app.fold_l1(args.session_id)))
        return 0

    if args.command == "extract-l2":
        _dump(_models_json(app.extract_l2(args.session_id)))
        return 0

    if args.command == "search":
        candidates = app.recall(
            query=args.query,
            limit=args.limit,
            layers=args.layer,
            statuses=args.status,
            session_id=args.session_id,
        )
        _dump(_models_json(candidates))
        return 0

    if args.command == "emerge":
        candidates = app.emerge(
            query=args.query,
            limit=args.limit,
            seed_limit=args.seed_limit,
            max_hops=args.max_hops,
            layers=args.layer,
            statuses=args.status,
            session_id=args.session_id,
        )
        _dump(_models_json(candidates))
        return 0

    if args.command == "build-context":
        bundle = app.build_context(
            query=args.query,
            token_budget=args.token_budget,
            limit=args.limit,
            layers=args.layer,
            statuses=args.status,
            session_id=args.session_id,
            active_window_mode=args.active_window_mode,
            active_limit=args.active_limit,
            candidate_limit=args.candidate_limit,
            strict_session=args.strict_session,
        )
        _dump(_model_json(bundle))
        return 0

    if args.command == "active-window":
        bundle = app.active_window(
            limit=args.limit,
            token_budget=args.token_budget,
            layers=args.layer,
            statuses=args.status,
            session_id=args.session_id,
            strict_session=args.strict_session,
        )
        _dump(_model_json(bundle))
        return 0

    if args.command == "active-window-report":
        report = app.active_window_report(
            limit=args.limit,
            decision_limit=args.decision_limit,
            layers=args.layer,
            statuses=args.status,
            session_id=args.session_id,
            strict_session=args.strict_session,
        )
        _dump(_model_json(report))
        return 0

    if args.command == "build-fused-context":
        bundle = app.build_fused_context(
            query=args.query,
            token_budget=args.token_budget,
            recall_limit=args.recall_limit,
            active_limit=args.active_limit,
            candidate_limit=args.candidate_limit,
            layers=args.layer,
            statuses=args.status,
            session_id=args.session_id,
            strict_session=args.strict_session,
        )
        _dump(_model_json(bundle))
        return 0

    if args.command == "build-fused-context-report":
        report = app.build_fused_context_report(
            query=args.query,
            token_budget=args.token_budget,
            recall_limit=args.recall_limit,
            active_limit=args.active_limit,
            candidate_limit=args.candidate_limit,
            layers=args.layer,
            statuses=args.status,
            session_id=args.session_id,
            strict_session=args.strict_session,
        )
        _dump(_model_json(report))
        return 0

    if args.command == "compare-fused-context":
        report = app.compare_fused_context(
            query=args.query,
            token_budget=args.token_budget,
            recall_limit=args.recall_limit,
            active_limit=args.active_limit,
            candidate_limit=args.candidate_limit,
            layers=args.layer,
            statuses=args.status,
            session_id=args.session_id,
            strict_session=args.strict_session,
        )
        _dump(_model_json(report))
        return 0

    if args.command == "compare-fused-context-batch":
        report = app.compare_fused_context_batch(
            queries=args.query,
            token_budget=args.token_budget,
            recall_limit=args.recall_limit,
            active_limit=args.active_limit,
            candidate_limit=args.candidate_limit,
            layers=args.layer,
            statuses=args.status,
            session_id=args.session_id,
            strict_session=args.strict_session,
        )
        _dump(_model_json(report))
        return 0

    if args.command == "index-embeddings":
        _dump(app.index_embeddings(limit=args.limit))
        return 0

    if args.command == "feedback":
        _dump(_model_json(app.feedback(args.memory_id, args.signal)))
        return 0

    if args.command == "govern-lifecycle":
        _dump(app.govern_lifecycle(limit=args.limit, layer=args.layer))
        return 0

    if args.command == "maintenance-cycle":
        _dump(
            app.maintenance_cycle(
                model=args.model,
                index_embeddings=args.index_embeddings,
                embedding_limit=args.embedding_limit,
                governance_limit=args.governance_limit,
                semantic_threshold=args.semantic_threshold,
                semantic_mode=args.semantic_mode,
                l3_threshold=args.l3_threshold,
                persona_min_support=args.persona_min_support,
                use_autonomous_governance=not args.skip_autonomous_governance,
                autonomous_model_mode=args.autonomous_model_mode,
                autonomous_rule_fallback=False,
                apply_review_actions=args.apply_review_actions_compat,
                review_action_limit=args.review_action_limit,
                include_rejected_review_actions=not args.approved_only_review_actions,
                allow_second_confirm_review_actions=args.allow_second_confirm_review_actions,
                dry_run=args.dry_run,
            )
        )
        return 0

    if args.command == "autonomous-governance-cycle":
        _dump(
            app.autonomous_governance_cycle(
                model=args.model,
                semantic_threshold=args.semantic_threshold,
                semantic_auto_merge_threshold=args.semantic_auto_merge_threshold,
                semantic_auto_tombstone_threshold=args.semantic_auto_tombstone_threshold,
                l3_threshold=args.l3_threshold,
                persona_min_support=args.persona_min_support,
                model_mode=args.model_mode,
                rule_fallback=False,
                dry_run=args.dry_run,
                limit=args.limit,
            )
        )
        return 0

    if args.command == "autonomous-governance-rollback":
        _dump(
            app.autonomous_governance_rollback(
                target_type=args.target_type,
                target_id=args.target_id,
                reason=args.reason,
            )
        )
        return 0

    if args.command == "semantic-dedup":
        _dump(
            app.semantic_dedup(
                model=args.model,
                limit=args.limit,
                threshold=args.threshold,
                mode=args.mode,
                dry_run=args.dry_run,
                auto_merge_threshold=args.auto_merge_threshold,
                auto_tombstone_threshold=args.auto_tombstone_threshold,
            )
        )
        return 0

    if args.command == "restore-semantic-merge":
        _dump(app.restore_semantic_merge(edge_id=args.edge_id, reason=args.reason))
        return 0

    if args.command == "cluster-l3":
        scenes = app.cluster_l3(
            session_id=args.session_id,
            min_group_size=args.min_group_size,
            limit=args.limit,
        )
        _dump(_models_json(scenes))
        return 0

    if args.command == "cross-session-l3-candidates":
        _dump(
            app.cross_session_l3_candidates(
                model=args.model,
                threshold=args.threshold,
                limit=args.limit,
                dry_run=args.dry_run,
            )
        )
        return 0

    if args.command == "build-l4-persona-candidates":
        _dump(
            app.build_l4_persona_candidates(
                min_support=args.min_support,
                limit=args.limit,
                dry_run=args.dry_run,
            )
        )
        return 0

    if args.command == "review-queue":
        items = app.review_queue(
            kind=args.kind,
            include_reviewed=args.include_reviewed,
            limit=args.limit,
        )
        _dump(_models_json(items))
        return 0

    if args.command == "review-mark":
        item = app.review_mark(
            target_type=args.target_type,
            target_id=args.target_id,
            status=args.status,
            kind=args.kind,
            note=args.note,
        )
        _dump(_model_json(item))
        return 0

    if args.command == "review-plan":
        plans = app.review_plan(
            include_rejected=args.include_rejected,
            limit=args.limit,
        )
        _dump(_models_json(plans))
        return 0

    if args.command == "review-apply":
        result = app.review_apply(
            args.plan_id,
            confirm=args.confirm,
            second_confirm=args.second_confirm,
        )
        _dump(_model_json(result))
        return 0

    if args.command == "review-events":
        events = app.review_events(
            target_type=args.target_type,
            target_id=args.target_id,
            event_type=args.event_type,
            limit=args.limit,
        )
        _dump(_models_json(events))
        return 0

    if args.command == "review-audit":
        summary = app.review_audit(
            event_limit=args.event_limit,
            recent_limit=args.recent_limit,
        )
        _dump(_model_json(summary))
        return 0

    raise ValueError("Unsupported command: %s" % args.command)


def _parse_runtime_message(value: str) -> dict[str, object]:
    if ":" not in value:
        return {"role": MessageRole.USER.value, "content": value}
    role, content = value.split(":", 1)
    role = role.strip()
    if role not in {item.value for item in MessageRole}:
        return {"role": MessageRole.USER.value, "content": value}
    return {"role": role, "content": content.strip()}


if __name__ == "__main__":
    raise SystemExit(main())
