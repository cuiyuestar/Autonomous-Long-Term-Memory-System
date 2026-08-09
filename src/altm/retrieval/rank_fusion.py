"""Deterministic reciprocal-rank fusion primitives."""

from __future__ import annotations

from collections.abc import Sequence


def reciprocal_rank_scores(
    rankings: Sequence[Sequence[str]],
    rank_constant: int = 60,
    tie_keys: Sequence[Sequence[str]] | None = None,
) -> dict[str, float]:
    if rank_constant <= 0:
        raise ValueError("RRF rank_constant must be positive")
    if tie_keys is not None and len(tie_keys) != len(rankings):
        raise ValueError("RRF tie_keys must align with rankings")
    scores: dict[str, float] = {}
    for ranking_index, ranking in enumerate(rankings):
        ranking_ties = tie_keys[ranking_index] if tie_keys is not None else ()
        if ranking_ties and len(ranking_ties) != len(ranking):
            raise ValueError("Each RRF tie-key ranking must align with item ids")
        seen: set[str] = set()
        tied_ranks: dict[str, int] = {}
        for position, item_id in enumerate(ranking, start=1):
            if item_id in seen:
                continue
            seen.add(item_id)
            tie_key = ranking_ties[position - 1] if ranking_ties else item_id
            rank = tied_ranks.setdefault(tie_key, position)
            scores[item_id] = scores.get(item_id, 0.0) + (
                1.0 / float(rank_constant + rank)
            )
    return scores
