"""Port interfaces for the memory system.

The implementation should depend on these ports rather than concrete storage,
retrieval, lifecycle, or adapter details.
"""

from __future__ import annotations

from typing import Iterable, Optional, Protocol, Sequence

from altm.contracts import (
    AccessSignal,
    ContextBundle,
    ContextCapsule,
    EvidenceRef,
    L2Atom,
    MemoryLayer,
    MemoryStatus,
    MemoryUnit,
    RecallCandidate,
    RecallQuery,
)


class MemoryStore(Protocol):
    def initialize(self) -> None:
        """Prepare physical storage and indexes."""
        ...

    def put_memory_unit(self, memory: MemoryUnit) -> None:
        """Persist a memory unit and its indexed representation."""
        ...

    def get_memory_unit(self, memory_id: str) -> Optional[MemoryUnit]:
        """Load a memory unit by id."""
        ...

    def search_fts(
        self,
        query: str,
        limit: int = 10,
        layers: Optional[Sequence[MemoryLayer]] = None,
        session_id: Optional[str] = None,
        statuses: Optional[Sequence[MemoryStatus]] = None,
    ) -> Sequence[MemoryUnit]:
        """Run keyword/FTS retrieval against locally indexed memory."""
        ...

    def put_l2_atom(self, atom: L2Atom, memory: MemoryUnit) -> None:
        """Persist an L2 atom in both MemoryUnit and type-specific L2 table."""
        ...

    def put_l1_context_capsule(self, capsule: ContextCapsule, memory_unit_id: str) -> None:
        """Persist a structured L1 capsule alongside the L1 MemoryUnit."""
        ...

    def record_access_signal(self, memory_id: str, signal: AccessSignal) -> None:
        """Record lifecycle feedback signal for a memory unit."""
        ...

    def add_evidence_refs(self, memory_id: str, refs: Iterable[EvidenceRef]) -> None:
        """Attach evidence references without weakening fallback locators."""
        ...

    def tombstone(self, target_type: str, target_id: str, reason: str) -> None:
        """Record tombstone before physical cleanup."""
        ...


class FoldingPipeline(Protocol):
    def fold_session(self, session_id: str) -> Sequence[MemoryUnit]:
        """Fold L0 messages into L1/L2/L3/L4 candidates."""
        ...


class RetrievalEngine(Protocol):
    def recall(self, query: RecallQuery) -> Sequence[RecallCandidate]:
        """Generate and rerank candidates for the current task."""
        ...


class LifecycleManager(Protocol):
    def record_access_signal(self, memory_id: str, signal: AccessSignal) -> None:
        """Record access usefulness signals separately from raw candidate hits."""
        ...

    def run_cycle(
        self,
        limit: int = 1000,
        layer: Optional[MemoryLayer] = None,
    ) -> Sequence[MemoryUnit]:
        """Run one governance cycle for promotion, demotion, and compression."""
        ...


class ContextGateway(Protocol):
    def assemble(self, candidates: Sequence[RecallCandidate], token_budget: int) -> ContextBundle:
        """Compress and band selected candidates before context injection."""
        ...


class HumanReviewGate(Protocol):
    def require_confirmation(self, decision_id: str, summary: str, options: Sequence[str]) -> str:
        """Ask the human to confirm important technical or memory decisions."""
        ...
