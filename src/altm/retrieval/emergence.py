"""Query-induced graph emergence for experience recall."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import cast

from altm.contracts import MemoryStatus, MemoryUnit, RecallCandidate, RecallQuery, ScoreBreakdown
from altm.lifecycle import adjust_retrieval_score
from altm.retrieval.fts import FTSRetrievalEngine
from altm.storage import SQLiteMemoryStore


@dataclass(frozen=True)
class QueryEmergencePolicy:
    seed_limit: int = 8
    max_hops: int = 2
    damping: float = 0.85
    min_graph_score: float = 0.01


class QueryEmergenceEngine:
    """Expand query entry points over memory graph edges with a small PPR-style walk."""

    def __init__(
        self,
        store: SQLiteMemoryStore,
        direct_retriever: FTSRetrievalEngine,
        policy: QueryEmergencePolicy | None = None,
    ) -> None:
        self.store = store
        self.direct_retriever = direct_retriever
        self.policy = policy or QueryEmergencePolicy()

    def emerge(
        self,
        query: RecallQuery,
        seed_limit: int | None = None,
        max_hops: int | None = None,
    ) -> Sequence[RecallCandidate]:
        seed_count = seed_limit or self.policy.seed_limit
        hop_count = max(0, max_hops if max_hops is not None else self.policy.max_hops)
        seed_query = query.model_copy(update={"top_k": max(query.top_k, seed_count)})
        direct_candidates = list(self.direct_retriever.recall(seed_query))[:seed_count]
        if not direct_candidates:
            return []

        seeds = _seed_scores(direct_candidates)
        adjacency = self._adjacency(query)
        ranks = dict(seeds)
        for _ in range(hop_count):
            next_ranks = {
                memory_id: (1.0 - self.policy.damping) * score
                for memory_id, score in seeds.items()
            }
            for memory_id, rank in ranks.items():
                neighbors = adjacency.get(memory_id, ())
                if not neighbors:
                    continue
                total_weight = sum(weight for _, weight in neighbors)
                if total_weight <= 0:
                    continue
                for neighbor_id, weight in neighbors:
                    next_ranks[neighbor_id] = next_ranks.get(neighbor_id, 0.0) + (
                        self.policy.damping * rank * weight / total_weight
                    )
            ranks = next_ranks

        direct_by_id = {candidate.memory.id: candidate for candidate in direct_candidates}
        candidates: list[RecallCandidate] = []
        for memory_id, graph_score in sorted(ranks.items(), key=lambda item: item[1], reverse=True):
            if graph_score < self.policy.min_graph_score:
                continue
            memory = self.store.get_memory_unit(memory_id)
            if memory is None or not _memory_allowed(memory, query):
                continue
            direct = direct_by_id.get(memory_id)
            matched_by = list(direct.matched_by) if direct is not None else []
            matched_by.append("query_emergence_seed" if direct is not None else "graph_ppr")
            retrieval_score = adjust_retrieval_score(memory, min(1.0, graph_score))
            if retrieval_score <= 0:
                continue
            candidates.append(
                RecallCandidate(
                    memory=memory,
                    score=ScoreBreakdown(
                        retrieval_score=retrieval_score,
                        resident_score=memory.score.resident_score,
                        structural=memory.score.structural,
                        recency=memory.score.recency,
                        access=memory.score.access,
                        semantic=graph_score,
                        evidence_quality=memory.score.evidence_quality,
                    ),
                    matched_by=matched_by,
                    explanation="Query-induced emergence expanded recall over memory graph edges.",
                )
            )
            if len(candidates) >= query.top_k:
                break
        return candidates

    def _adjacency(self, query: RecallQuery) -> dict[str, list[tuple[str, float]]]:
        adjacency: dict[str, list[tuple[str, float]]] = {}
        for edge in self.store.list_graph_edges():
            metadata = edge["metadata"]
            if (
                isinstance(metadata, dict)
                and cast(dict[object, object], metadata).get("review_status")
                == "rejected"
            ):
                continue
            source_id = str(edge["source_memory_id"])
            target_id = str(edge["target_memory_id"])
            source = self.store.get_memory_unit(source_id)
            target = self.store.get_memory_unit(target_id)
            if source is None or target is None:
                continue
            if not _memory_allowed(source, query) or not _memory_allowed(target, query):
                continue
            weight = max(
                0.0,
                _float_value(edge["weight"]) * _float_value(edge["confidence"]),
            )
            if weight <= 0:
                continue
            adjacency.setdefault(source_id, []).append((target_id, weight))
            adjacency.setdefault(target_id, []).append((source_id, weight))
        return adjacency


def _seed_scores(candidates: Sequence[RecallCandidate]) -> dict[str, float]:
    raw = {
        candidate.memory.id: max(0.0, candidate.score.retrieval_score or 0.0)
        for candidate in candidates
    }
    total = sum(raw.values())
    if total <= 0:
        fallback = 1.0 / float(len(raw))
        return dict.fromkeys(raw, fallback)
    return {memory_id: score / total for memory_id, score in raw.items()}


def _memory_allowed(memory: MemoryUnit, query: RecallQuery) -> bool:
    if memory.status in {MemoryStatus.DELETED, MemoryStatus.TOMBSTONED}:
        return False
    if query.preferred_layers and memory.layer not in query.preferred_layers:
        return False
    if query.statuses and memory.status not in query.statuses:
        return False
    if query.session_id is not None and memory.metadata.get("session_id") != query.session_id:
        return False
    return True


def _float_value(value: object) -> float:
    return float(value) if isinstance(value, (int, float)) else 0.0
