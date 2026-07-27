"""SQLite FTS retrieval implementation."""

from __future__ import annotations

from typing import Sequence

from altm.contracts import MemoryUnit, RecallCandidate, RecallQuery, ScoreBreakdown
from altm.lifecycle import adjust_retrieval_score
from altm.llm import OpenAICompatibleEmbeddingClient
from altm.retrieval.local_vector import LocalVectorRetriever
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
        ranked: dict[str, tuple[MemoryUnit, float, list[str]]] = {}
        degraded_channels: list[str] = []

        if self.remote_vector is not None:
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
            for memory, score in remote_matches:
                self._merge_scored(
                    ranked,
                    memory,
                    adjust_retrieval_score(memory, score + 0.2),
                    "remote_vector",
                )

        for memory, score in self.local_vector.search(
            query.text,
            limit=query.top_k,
            layers=query.preferred_layers,
            session_id=query.session_id,
            statuses=query.statuses,
        ):
            self._merge_scored(ranked, memory, adjust_retrieval_score(memory, score), "local_vector")

        trigram_units = self.store.search_fts_trigram(
            query.text,
            limit=query.top_k,
            layers=query.preferred_layers,
            session_id=query.session_id,
            statuses=query.statuses,
        )
        self._merge_ranked(ranked, trigram_units, "fts_trigram", base_score=0.8)

        unicode_units = self.store.search_fts(
            query.text,
            limit=query.top_k,
            layers=query.preferred_layers,
            session_id=query.session_id,
            statuses=query.statuses,
        )
        self._merge_ranked(ranked, unicode_units, "fts_unicode", base_score=0.6)

        if not ranked:
            like_units = self.store.search_like(
                query.text,
                limit=query.top_k,
                layers=query.preferred_layers,
                session_id=query.session_id,
                statuses=query.statuses,
            )
            self._merge_ranked(ranked, like_units, "like_fallback", base_score=0.4)

        ordered = sorted(ranked.values(), key=lambda item: item[1], reverse=True)[: query.top_k]
        candidates = []
        for unit, retrieval_score, matched_by in ordered:
            candidates.append(
                RecallCandidate(
                    memory=unit,
                    score=ScoreBreakdown(
                        retrieval_score=retrieval_score,
                        resident_score=unit.score.resident_score,
                        structural=unit.score.structural,
                        recency=unit.score.recency,
                        access=unit.score.access,
                        evidence_quality=unit.score.evidence_quality,
                    ),
                    matched_by=_matched_by_with_degraded(matched_by, degraded_channels),
                    explanation=_explanation(degraded_channels),
                )
            )
        return candidates

    def _merge_scored(
        self,
        ranked: dict[str, tuple[MemoryUnit, float, list[str]]],
        unit: MemoryUnit,
        score: float,
        channel: str,
    ) -> None:
        if score <= 0:
            return
        if unit.id in ranked:
            existing_unit, existing_score, matched_by = ranked[unit.id]
            ranked[unit.id] = (
                existing_unit,
                max(existing_score, score) + 0.05,
                [*matched_by, channel],
            )
        else:
            ranked[unit.id] = (unit, score, [channel])

    def _merge_ranked(
        self,
        ranked: dict[str, tuple[MemoryUnit, float, list[str]]],
        units: Sequence[MemoryUnit],
        channel: str,
        base_score: float,
    ) -> None:
        for rank, unit in enumerate(units):
            score = adjust_retrieval_score(unit, base_score / float(rank + 1))
            if score <= 0:
                continue
            if unit.id in ranked:
                existing_unit, existing_score, matched_by = ranked[unit.id]
                ranked[unit.id] = (
                    existing_unit,
                    max(existing_score, score) + 0.05,
                    [*matched_by, channel],
                )
            else:
                ranked[unit.id] = (unit, score, [channel])


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
    explanation = "Matched by configured vector and SQLite retrieval channels."
    if not degraded_channels:
        return explanation
    return "%s Degraded channels: %s; fallback retrieval channels were used." % (
        explanation,
        ", ".join(degraded_channels),
    )
