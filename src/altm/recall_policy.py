"""Shared layer-aware session policy for query-time recall."""

from __future__ import annotations

from collections.abc import Sequence

from altm.contracts import MemoryLayer, MemoryUnit

CROSS_SESSION_QUERY_LAYERS = (
    MemoryLayer.L2,
    MemoryLayer.L3,
    MemoryLayer.L4,
)


def cross_session_query_layers(strict_session: bool) -> tuple[MemoryLayer, ...]:
    """Return layers eligible for cross-session query recall."""
    return () if strict_session else CROSS_SESSION_QUERY_LAYERS


def memory_matches_recall_session(
    memory: MemoryUnit,
    session_id: str | None,
    cross_session_layers: Sequence[MemoryLayer],
) -> bool:
    """Apply current-session access plus explicit cross-session layer access."""
    if session_id is None or memory.metadata.get("session_id") == session_id:
        return True
    if memory.layer not in cross_session_layers:
        return False
    return (
        memory.metadata.get("review_status") != "rejected"
        and memory.metadata.get("governance_review_status") != "rejected"
    )
