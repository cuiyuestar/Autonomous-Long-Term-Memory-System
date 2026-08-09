"""SQLite FTS retrieval implementation."""

from __future__ import annotations

from collections.abc import Sequence

from altm.contracts import MemoryUnit, RecallCandidate, RecallQuery, ScoreBreakdown
from altm.lifecycle import adjust_retrieval_score
from altm.llm import OpenAICompatibleEmbeddingClient
from altm.retrieval.graph import HeterogeneousGraphRetriever
from altm.retrieval.local_vector import LocalVectorRetriever
from altm.retrieval.rank_fusion import reciprocal_rank_scores
from altm.retrieval.remote_vector import RemoteVectorRetriever
from altm.storage import SQLiteMemoryStore


class FTSRetrievalEngine:
    def __init__(
        self,
        store: SQLiteMemoryStore,
        embedding_client: OpenAICompatibleEmbeddingClient | None = None,
    ) -> None:
        self.store = store
        self.local_vector = LocalVectorRetriever(store)
        self.remote_vector = (
            RemoteVectorRetriever(store, embedding_client) if embedding_client is not None else None
        )

    def recall(self, query: RecallQuery) -> Sequence[RecallCandidate]:
        channels: list[tuple[str, list[MemoryUnit]]] = []
        memories: dict[str, MemoryUnit] = {}
        graph_candidates: dict[str, RecallCandidate] = {}
        degraded_channels: list[str] = []

        if self.remote_vector is not None:
            remote_matches: Sequence[tuple[MemoryUnit, float]]
            try:
                remote_matches = self.remote_vector.search(
                    query.text,
                    limit=query.top_k,
                    layers=query.preferred_layers,
                    session_id=query.session_id,
                    statuses=query.statuses,
                )
            except RuntimeError:
                degraded_channels.append("remote_vector")
                remote_matches = []
            channels.append(
                ("remote_vector", [memory for memory, _ in remote_matches])
            )

        channels.append(
            (
                "local_vector",
                [
                    memory
                    for memory, _ in self.local_vector.search(
                        query.text,
                        limit=query.top_k,
                        layers=query.preferred_layers,
                        session_id=query.session_id,
                        statuses=query.statuses,
                    )
                ],
            )
        )

        trigram_units = self.store.search_fts_trigram(
            query.text,
            limit=query.top_k,
            layers=query.preferred_layers,
            session_id=query.session_id,
            statuses=query.statuses,
        )
        channels.append(("fts_trigram", list(trigram_units)))

        unicode_units = self.store.search_fts(
            query.text,
            limit=query.top_k,
            layers=query.preferred_layers,
            session_id=query.session_id,
            statuses=query.statuses,
        )
        channels.append(("fts_unicode", list(unicode_units)))

        graph_results = list(HeterogeneousGraphRetriever(self.store).recall(query))
        if graph_results:
            graph_candidates = {
                candidate.memory.id: candidate
                for candidate in graph_results
            }
            channels.append(
                (
                    "graph_ppr",
                    [candidate.memory for candidate in graph_results],
                )
            )

        if not any(units for _, units in channels):
            like_units = self.store.search_like(
                query.text,
                limit=query.top_k,
                layers=query.preferred_layers,
                session_id=query.session_id,
                statuses=query.statuses,
            )
            channels.append(("like_fallback", list(like_units)))

        channel_ids: list[list[str]] = []
        channel_tie_keys: list[list[str]] = []
        matched_by: dict[str, list[str]] = {}
        for channel, units in channels:
            ranking: list[str] = []
            tie_ranking: list[str] = []
            for unit in units:
                memories[unit.id] = unit
                ranking.append(unit.id)
                tie_ranking.append(unit.content_hash)
                matched_by.setdefault(unit.id, []).append(channel)
            if ranking:
                channel_ids.append(ranking)
                channel_tie_keys.append(tie_ranking)
        fused_scores = reciprocal_rank_scores(
            channel_ids,
            tie_keys=channel_tie_keys,
        )
        if not fused_scores:
            return []
        max_fused_score = max(fused_scores.values())

        candidates: list[RecallCandidate] = []
        for memory_id, fused_score in sorted(
            fused_scores.items(),
            key=lambda item: (-item[1], item[0]),
        ):
            unit = memories[memory_id]
            retrieval_score = adjust_retrieval_score(
                unit,
                fused_score / max_fused_score,
            )
            if retrieval_score <= 0:
                continue
            graph_candidate = graph_candidates.get(memory_id)
            candidate_matched_by = list(matched_by.get(memory_id, ()))
            if graph_candidate is not None:
                candidate_matched_by = list(
                    dict.fromkeys(
                        [*candidate_matched_by, *graph_candidate.matched_by]
                    )
                )
            candidates.append(
                RecallCandidate(
                    memory=unit,
                    score=ScoreBreakdown(
                        retrieval_score=retrieval_score,
                        resident_score=unit.score.resident_score,
                        structural=unit.score.structural,
                        recency=unit.score.recency,
                        access=unit.score.access,
                        semantic=(
                            graph_candidate.score.semantic
                            if graph_candidate is not None
                            else None
                        ),
                        evidence_quality=unit.score.evidence_quality,
                    ),
                    matched_by=_matched_by_with_degraded(
                        candidate_matched_by,
                        degraded_channels,
                    ),
                    explanation=_explanation(degraded_channels),
                    metadata={
                        "rank_fusion": {
                            "strategy": "rrf",
                            "rank_constant": 60,
                            "raw_score": fused_score,
                            "channels": matched_by.get(memory_id, []),
                        },
                        **(
                            graph_candidate.metadata
                            if graph_candidate is not None
                            else {}
                        ),
                    },
                )
            )
            if len(candidates) >= query.top_k:
                break
        return candidates


def _matched_by_with_degraded(
    matched_by: Sequence[str],
    degraded_channels: Sequence[str],
) -> list[str]:
    values = list(matched_by)
    for channel in degraded_channels:
        marker = "%s_degraded" % channel
        if marker not in values:
            values.append(marker)
    return values


def _explanation(degraded_channels: Sequence[str]) -> str:
    explanation = (
        "Ranked with reciprocal-rank fusion across configured vector, SQLite, "
        "and typed graph retrieval channels."
    )
    if not degraded_channels:
        return explanation
    return "%s Degraded channels: %s; fallback retrieval channels were used." % (
        explanation,
        ", ".join(degraded_channels),
    )
