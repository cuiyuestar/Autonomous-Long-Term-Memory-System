"""Human-in-loop review queue for memory governance candidates."""

from __future__ import annotations

from typing import Sequence

from altm.config import high_risk_flags
from altm.contracts import (
    MemoryLayer,
    MemoryStatus,
    MemoryUnit,
    ReviewItemKind,
    ReviewQueueItem,
    ReviewStatus,
)
from altm.storage import SQLiteMemoryStore
from altm.utils import utc_now_iso


class ReviewQueue:
    def __init__(self, store: SQLiteMemoryStore) -> None:
        self.store = store

    def list_items(
        self,
        kind: ReviewItemKind | None = None,
        include_reviewed: bool = False,
        limit: int = 100,
    ) -> Sequence[ReviewQueueItem]:
        items: list[ReviewQueueItem] = []
        memory_units = list(self.store.list_memory_units(limit=max(limit, 1000)))
        items.extend(self._l2_pending_items(memory_units, include_reviewed))
        items.extend(self._promotion_items(memory_units, include_reviewed))
        items.extend(self._demotion_items(memory_units, include_reviewed))
        items.extend(self._l3_observing_items(memory_units, include_reviewed))
        items.extend(self._l4_persona_items(memory_units, include_reviewed))
        items.extend(self._semantic_duplicate_items(include_reviewed))
        items.extend(self._cross_session_l3_items(include_reviewed))

        if kind is not None:
            items = [item for item in items if item.kind == kind]
        return items[:limit]

    def mark_memory(
        self,
        memory_id: str,
        review_status: ReviewStatus,
        note: str | None = None,
        kind: ReviewItemKind | None = None,
    ) -> ReviewQueueItem | None:
        memory = self.store.get_memory_unit(memory_id)
        if memory is None:
            return None
        if (
            (kind is None or kind == ReviewItemKind.L2_PENDING)
            and memory.layer == MemoryLayer.L2
            and "atom_type" in memory.metadata
        ):
            updated = self.store.update_l2_review_status(memory_id, review_status.value, note=note)
        else:
            updates: dict[str, object] = {
                "governance_review_status": review_status.value,
                "governance_reviewed_at": utc_now_iso(),
            }
            if note is not None:
                updates["governance_review_note"] = note
            updated = self.store.update_memory_metadata(memory_id, updates)
        if updated is None:
            return None
        item = self._memory_to_review_item(updated, kind=kind, review_status=review_status)
        _append_review_event_if_enabled(
            self.store,
            event_type="review_mark",
            target_type=item.target_type,
            target_id=item.target_id,
            review_item_id=item.id,
            status=review_status.value,
            metadata={
                "kind": item.kind.value,
                "note": note,
            },
        )
        return item

    def mark_graph_edge(
        self,
        edge_id: str,
        review_status: ReviewStatus,
        note: str | None = None,
    ) -> ReviewQueueItem | None:
        updates: dict[str, object] = {
            "review_status": review_status.value,
            "reviewed_at": utc_now_iso(),
        }
        if note is not None:
            updates["review_note"] = note
        edge = self.store.update_graph_edge_metadata(edge_id, updates)
        if edge is None:
            return None
        item = self._edge_to_review_item(edge, review_status=review_status)
        _append_review_event_if_enabled(
            self.store,
            event_type="review_mark",
            target_type=item.target_type,
            target_id=item.target_id,
            review_item_id=item.id,
            status=review_status.value,
            metadata={
                "kind": item.kind.value,
                "note": note,
            },
        )
        return item

    def _l2_pending_items(
        self,
        memory_units: Sequence[MemoryUnit],
        include_reviewed: bool,
    ) -> Sequence[ReviewQueueItem]:
        items: list[ReviewQueueItem] = []
        for memory in memory_units:
            if memory.layer != MemoryLayer.L2 or "atom_type" not in memory.metadata:
                continue
            status = _review_status(memory.metadata.get("review_status"))
            if not include_reviewed and status != ReviewStatus.PENDING:
                continue
            items.append(
                self._memory_to_review_item(
                    memory,
                    kind=ReviewItemKind.L2_PENDING,
                    review_status=status,
                    title="Review L2 %s" % memory.metadata.get("atom_type", "atom"),
                )
            )
        return items

    def _promotion_items(
        self,
        memory_units: Sequence[MemoryUnit],
        include_reviewed: bool,
    ) -> Sequence[ReviewQueueItem]:
        items: list[ReviewQueueItem] = []
        for memory in memory_units:
            if memory.lifecycle.promotion_candidate_since is None:
                continue
            status = _review_status(memory.metadata.get("governance_review_status"))
            if not include_reviewed and status != ReviewStatus.PENDING:
                continue
            items.append(
                self._memory_to_review_item(
                    memory,
                    kind=ReviewItemKind.PROMOTION_CANDIDATE,
                    review_status=status,
                    title="Review promotion candidate",
                    metadata_extra={
                        "promotion_candidate_since": memory.lifecycle.promotion_candidate_since
                    },
                )
            )
        return items

    def _demotion_items(
        self,
        memory_units: Sequence[MemoryUnit],
        include_reviewed: bool,
    ) -> Sequence[ReviewQueueItem]:
        items: list[ReviewQueueItem] = []
        for memory in memory_units:
            if memory.lifecycle.demotion_candidate_since is None:
                continue
            status = _review_status(memory.metadata.get("governance_review_status"))
            if not include_reviewed and status != ReviewStatus.PENDING:
                continue
            items.append(
                self._memory_to_review_item(
                    memory,
                    kind=ReviewItemKind.DEMOTION_CANDIDATE,
                    review_status=status,
                    title="Review demotion candidate",
                    metadata_extra={
                        "demotion_candidate_since": memory.lifecycle.demotion_candidate_since
                    },
                )
            )
        return items

    def _l3_observing_items(
        self,
        memory_units: Sequence[MemoryUnit],
        include_reviewed: bool,
    ) -> Sequence[ReviewQueueItem]:
        items: list[ReviewQueueItem] = []
        for memory in memory_units:
            if memory.layer != MemoryLayer.L3 or memory.status != MemoryStatus.OBSERVING:
                continue
            status = _review_status(memory.metadata.get("governance_review_status"))
            if not include_reviewed and status != ReviewStatus.PENDING:
                continue
            items.append(
                self._memory_to_review_item(
                    memory,
                    kind=ReviewItemKind.L3_OBSERVING,
                    review_status=status,
                    title="Review L3 observing scene",
                    source_memory_ids=[
                        str(value) for value in memory.metadata.get("source_memory_ids", [])
                    ],
                )
            )
        return items

    def _l4_persona_items(
        self,
        memory_units: Sequence[MemoryUnit],
        include_reviewed: bool,
    ) -> Sequence[ReviewQueueItem]:
        items: list[ReviewQueueItem] = []
        for memory in memory_units:
            if (
                memory.layer != MemoryLayer.L4
                or memory.status != MemoryStatus.OBSERVING
                or memory.metadata.get("builder") != "l4_persona_candidate_builder"
            ):
                continue
            status = _review_status(memory.metadata.get("governance_review_status"))
            if not include_reviewed and status != ReviewStatus.PENDING:
                continue
            items.append(
                self._memory_to_review_item(
                    memory,
                    kind=ReviewItemKind.L4_PERSONA_CANDIDATE,
                    review_status=status,
                    title="Review L4 persona candidate",
                    source_memory_ids=[
                        str(value) for value in memory.metadata.get("source_memory_ids", [])
                    ],
                )
            )
        return items

    def _semantic_duplicate_items(self, include_reviewed: bool) -> Sequence[ReviewQueueItem]:
        items: list[ReviewQueueItem] = []
        for edge in self.store.list_graph_edges("semantic_duplicate_candidate"):
            metadata = edge.get("metadata", {})
            status = _review_status(metadata.get("review_status") if isinstance(metadata, dict) else None)
            if not include_reviewed and status != ReviewStatus.PENDING:
                continue
            items.append(self._edge_to_review_item(edge, review_status=status))
        return items

    def _cross_session_l3_items(self, include_reviewed: bool) -> Sequence[ReviewQueueItem]:
        items: list[ReviewQueueItem] = []
        for edge in self.store.list_graph_edges("cross_session_l3_candidate"):
            metadata = edge.get("metadata", {})
            status = _review_status(metadata.get("review_status") if isinstance(metadata, dict) else None)
            if not include_reviewed and status != ReviewStatus.PENDING:
                continue
            items.append(
                self._edge_to_review_item(
                    edge,
                    review_status=status,
                    kind=ReviewItemKind.CROSS_SESSION_L3_CANDIDATE,
                    title="Review cross-session L3 candidate",
                )
            )
        return items

    def _memory_to_review_item(
        self,
        memory: MemoryUnit,
        kind: ReviewItemKind | None = None,
        review_status: ReviewStatus | None = None,
        title: str | None = None,
        source_memory_ids: Sequence[str] = (),
        metadata_extra: dict[str, object] | None = None,
    ) -> ReviewQueueItem:
        resolved_kind = kind or _default_memory_kind(memory)
        metadata = dict(memory.metadata)
        if metadata_extra:
            metadata.update(metadata_extra)
        return ReviewQueueItem(
            id="review:%s:%s" % (resolved_kind.value, memory.id),
            kind=resolved_kind,
            target_type="memory_unit",
            target_id=memory.id,
            review_status=review_status or _review_status(metadata.get("review_status")),
            title=title or "Review memory",
            summary=memory.summary or memory.content[:200],
            source_memory_ids=list(source_memory_ids),
            metadata=metadata,
        )

    def _edge_to_review_item(
        self,
        edge: dict[str, object],
        review_status: ReviewStatus | None = None,
        kind: ReviewItemKind = ReviewItemKind.SEMANTIC_DUPLICATE_CANDIDATE,
        title: str = "Review semantic duplicate candidate",
    ) -> ReviewQueueItem:
        metadata = edge.get("metadata", {})
        metadata_dict = dict(metadata) if isinstance(metadata, dict) else {}
        source_memory_id = str(edge["source_memory_id"])
        target_memory_id = str(edge["target_memory_id"])
        similarity = metadata_dict.get("similarity", edge.get("weight"))
        return ReviewQueueItem(
            id="review:%s:%s" % (kind.value, edge["id"]),
            kind=kind,
            target_type="graph_edge",
            target_id=str(edge["id"]),
            review_status=review_status or _review_status(metadata_dict.get("review_status")),
            title=title,
            summary="%s <-> %s similarity=%s" % (source_memory_id, target_memory_id, similarity),
            source_memory_ids=[source_memory_id, target_memory_id],
            metadata=metadata_dict,
        )


def _review_status(value: object) -> ReviewStatus:
    if isinstance(value, str):
        try:
            return ReviewStatus(value)
        except ValueError:
            return ReviewStatus.PENDING
    return ReviewStatus.PENDING


def _append_review_event_if_enabled(
    store: SQLiteMemoryStore,
    event_type: str,
    target_type: str,
    target_id: str,
    review_item_id: str,
    status: str,
    metadata: dict[str, object],
) -> None:
    if not high_risk_flags().enable_review_event_sourcing:
        return
    store.append_review_event(
        event_type=event_type,
        target_type=target_type,
        target_id=target_id,
        review_item_id=review_item_id,
        status=status,
        metadata=metadata,
    )


def _default_memory_kind(memory: MemoryUnit) -> ReviewItemKind:
    if memory.layer == MemoryLayer.L2:
        return ReviewItemKind.L2_PENDING
    if memory.layer == MemoryLayer.L3:
        return ReviewItemKind.L3_OBSERVING
    if memory.layer == MemoryLayer.L4:
        return ReviewItemKind.L4_PERSONA_CANDIDATE
    if memory.lifecycle.promotion_candidate_since is not None:
        return ReviewItemKind.PROMOTION_CANDIDATE
    if memory.lifecycle.demotion_candidate_since is not None:
        return ReviewItemKind.DEMOTION_CANDIDATE
    return ReviewItemKind.L2_PENDING
