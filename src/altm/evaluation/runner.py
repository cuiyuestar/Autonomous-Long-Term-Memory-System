"""Native ALTM retrieval runner and reproducible benchmark metrics."""

from __future__ import annotations

import math
import os
import platform
import sys
import time
from collections.abc import Sequence
from pathlib import Path
from typing import cast

from altm.application import AltmApplication
from altm.capture import L0Recorder
from altm.contracts import CaptureInput, MemoryScope, MemoryUnit, RecallQuery
from altm.evaluation.contracts import (
    BenchmarkAggregate,
    BenchmarkCorpus,
    BenchmarkDataset,
    BenchmarkQuestionResult,
    BenchmarkReport,
)
from altm.folding import GraphLLMExtractor
from altm.llm import optional_embedding_client_from_env
from altm.retrieval import FTSRetrievalEngine
from altm.storage import SQLiteMemoryStore
from altm.utils import stable_id, utc_now_iso


class AltmBenchmarkRunner:
    def __init__(
        self,
        db_path: str | Path,
        top_ks: Sequence[int] = (5, 10),
        enrichment: str = "l0",
    ) -> None:
        normalized_ks = sorted({int(value) for value in top_ks})
        if not normalized_ks or normalized_ks[0] <= 0:
            raise ValueError("Benchmark top-k values must be positive")
        if enrichment not in {"l0", "embedding", "l2", "full"}:
            raise ValueError(
                "Benchmark enrichment must be l0, embedding, l2, or full"
            )
        self.db_path = Path(db_path)
        self.top_ks = tuple(normalized_ks)
        self.enrichment = enrichment
        self.application = AltmApplication(self.db_path)

    def run(self, dataset: BenchmarkDataset) -> BenchmarkReport:
        started = time.perf_counter()
        results: list[BenchmarkQuestionResult] = []
        for corpus in dataset.corpora:
            scope = self._scope(dataset, corpus)
            store = self.application.store(scope)
            self._ingest_corpus(store, corpus)
            self._enrich_corpus(scope, corpus)
            results.extend(self._evaluate_corpus(store, corpus))
        duration_ms = (time.perf_counter() - started) * 1000.0
        return BenchmarkReport(
            benchmark=dataset.name,
            dataset_sha256=dataset.source_sha256,
            ran_at=utc_now_iso(),
            duration_ms=duration_ms,
            config={
                "top_ks": list(self.top_ks),
                "enrichment": self.enrichment,
                "database": str(self.db_path),
                "embedding_model": os.environ.get("ALTM_EMBEDDING_MODEL"),
            },
            aggregate=_aggregate(results, self.top_ks),
            per_category={
                category: _aggregate(category_results, self.top_ks)
                for category, category_results in _group_by_category(results).items()
            },
            questions=results,
            environment={
                "python": sys.version.split()[0],
                "platform": platform.platform(),
                "altm_git_commit": os.environ.get("ALTM_GIT_COMMIT"),
                "dataset_source": dataset.source_path,
            },
        )

    def _scope(
        self,
        dataset: BenchmarkDataset,
        corpus: BenchmarkCorpus,
    ) -> MemoryScope:
        return MemoryScope(
            tenant_id="altm-benchmark",
            workspace_id=dataset.name,
            user_id=stable_id("benchmark_user", dataset.source_sha256, corpus.id),
            agent_id="altm-benchmark-runner",
        )

    def _ingest_corpus(
        self,
        store: SQLiteMemoryStore,
        corpus: BenchmarkCorpus,
    ) -> None:
        scope = store.scope
        if scope is None:
            raise RuntimeError("Benchmark ingestion requires a scoped store")
        recorder = L0Recorder(store)
        for session in corpus.sessions:
            for turn in session.turns:
                recorder.capture(
                    CaptureInput(
                        scope=scope,
                        session_id=session.id,
                        message_id="%s:%s" % (corpus.id, turn.id),
                        role=turn.role,
                        content=turn.content,
                        created_at=turn.created_at,
                        metadata={
                            **turn.metadata,
                            "benchmark_corpus_id": corpus.id,
                            "benchmark_session_id": session.id,
                            "benchmark_turn_id": turn.id,
                            "benchmark_evidence_ids": turn.evidence_ids,
                        },
                    )
                )

    def _enrich_corpus(
        self,
        scope: MemoryScope,
        corpus: BenchmarkCorpus,
    ) -> None:
        if self.enrichment == "l0":
            return
        if self.enrichment in {"l2", "full"}:
            for session in corpus.sessions:
                self.application.fold_l1(session.id, scope=scope)
                self.application.extract_l2(session.id, scope=scope)
                if self.enrichment == "full":
                    GraphLLMExtractor(self.application.store(scope)).extract_session(
                        session.id
                    )
        if self.enrichment == "l2":
            return
        while True:
            indexed = self.application.index_embeddings(limit=1000, scope=scope)
            indexed_count = indexed.get("indexed_count")
            if not isinstance(indexed_count, int):
                raise RuntimeError("Embedding index result is missing indexed_count")
            if indexed_count == 0:
                break
        if self.enrichment == "embedding":
            return
        self.application.build_semantic_l3(scope=scope)
        self.application.distill_semantic_l4(scope=scope)

    def _evaluate_corpus(
        self,
        store: SQLiteMemoryStore,
        corpus: BenchmarkCorpus,
    ) -> list[BenchmarkQuestionResult]:
        retriever = FTSRetrievalEngine(
            store,
            optional_embedding_client_from_env(),
        )
        results: list[BenchmarkQuestionResult] = []
        for question in corpus.questions:
            started = time.perf_counter()
            candidates = list(
                retriever.recall(
                    RecallQuery(
                        text=question.question,
                        top_k=max(self.top_ks),
                    )
                )
            )
            latency_ms = (time.perf_counter() - started) * 1000.0
            candidate_evidence = [
                _memory_evidence_ids(store, candidate.memory)
                for candidate in candidates
            ]
            retrieved_evidence_ids = _flatten_unique(candidate_evidence)
            if not question.gold_evidence_ids:
                results.append(
                    BenchmarkQuestionResult(
                        question_id=question.id,
                        corpus_id=question.corpus_id,
                        category=question.category,
                        gold_evidence_ids=[],
                        retrieved_evidence_ids=retrieved_evidence_ids,
                        retrieved_memory_ids=[
                            candidate.memory.id for candidate in candidates
                        ],
                        latency_ms=latency_ms,
                        reciprocal_rank=0.0,
                        metrics={},
                        skipped=True,
                        skip_reason="no_gold_evidence",
                    )
                )
                continue
            gold = set(question.gold_evidence_ids)
            reciprocal_rank = _reciprocal_rank(candidate_evidence, gold)
            metrics: dict[str, float] = {}
            for top_k in self.top_ks:
                top_evidence = set(
                    _flatten_unique(candidate_evidence[:top_k])
                )
                metrics["recall_any@%s" % top_k] = float(
                    bool(gold & top_evidence)
                )
                metrics["recall_all@%s" % top_k] = float(
                    gold <= top_evidence
                )
                metrics["ndcg@%s" % top_k] = _ndcg(
                    candidate_evidence[:top_k],
                    gold,
                    top_k,
                )
            results.append(
                BenchmarkQuestionResult(
                    question_id=question.id,
                    corpus_id=question.corpus_id,
                    category=question.category,
                    gold_evidence_ids=question.gold_evidence_ids,
                    retrieved_evidence_ids=retrieved_evidence_ids,
                    retrieved_memory_ids=[
                        candidate.memory.id for candidate in candidates
                    ],
                    latency_ms=latency_ms,
                    reciprocal_rank=reciprocal_rank,
                    metrics=metrics,
                )
            )
        return results


def _memory_evidence_ids(
    store: SQLiteMemoryStore,
    memory: MemoryUnit,
    depth: int = 0,
    seen: set[str] | None = None,
) -> list[str]:
    if depth > 6:
        return []
    visited = seen or set()
    if memory.id in visited:
        return []
    visited.add(memory.id)
    values: list[str] = []
    metadata_ids = memory.metadata.get("benchmark_evidence_ids")
    if isinstance(metadata_ids, list):
        values.extend(
            str(value)
            for value in cast(list[object], metadata_ids)
        )
    for field in ("benchmark_session_id", "benchmark_turn_id"):
        value = memory.metadata.get(field)
        if isinstance(value, str):
            values.append(value)
    for ref in memory.evidence_refs:
        target = store.get_memory_unit(ref.target_id)
        if target is not None:
            values.extend(
                _memory_evidence_ids(
                    store,
                    target,
                    depth=depth + 1,
                    seen=visited,
                )
            )
    return list(dict.fromkeys(values))


def _reciprocal_rank(
    candidate_evidence: Sequence[Sequence[str]],
    gold: set[str],
) -> float:
    for rank, evidence_ids in enumerate(candidate_evidence, start=1):
        if gold.intersection(evidence_ids):
            return 1.0 / float(rank)
    return 0.0


def _ndcg(
    candidate_evidence: Sequence[Sequence[str]],
    gold: set[str],
    top_k: int,
) -> float:
    seen_gold: set[str] = set()
    relevance: list[float] = []
    for evidence_ids in candidate_evidence[:top_k]:
        new_gold = gold.intersection(evidence_ids).difference(seen_gold)
        relevance.append(1.0 if new_gold else 0.0)
        seen_gold.update(new_gold)
    dcg = sum(
        score / math.log2(rank + 1.0)
        for rank, score in enumerate(relevance, start=1)
    )
    ideal_count = min(len(gold), top_k)
    ideal = sum(
        1.0 / math.log2(rank + 1.0)
        for rank in range(1, ideal_count + 1)
    )
    return dcg / ideal if ideal > 0 else 0.0


def _flatten_unique(values: Sequence[Sequence[str]]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for items in values:
        for item in items:
            if item in seen:
                continue
            seen.add(item)
            result.append(item)
    return result


def _aggregate(
    results: Sequence[BenchmarkQuestionResult],
    top_ks: Sequence[int],
) -> BenchmarkAggregate:
    scored = [result for result in results if not result.skipped]
    metric_names = [
        metric
        for top_k in top_ks
        for metric in (
            "recall_any@%s" % top_k,
            "recall_all@%s" % top_k,
            "ndcg@%s" % top_k,
        )
    ]
    metrics = {
        metric: (
            sum(result.metrics.get(metric, 0.0) for result in scored)
            / len(scored)
            if scored
            else 0.0
        )
        for metric in metric_names
    }
    metrics["mrr"] = (
        sum(result.reciprocal_rank for result in scored) / len(scored)
        if scored
        else 0.0
    )
    latencies = sorted(result.latency_ms for result in results)
    return BenchmarkAggregate(
        question_count=len(results),
        scored_question_count=len(scored),
        skipped_question_count=len(results) - len(scored),
        metrics=metrics,
        latency_ms={
            "avg": sum(latencies) / len(latencies) if latencies else 0.0,
            "p50": _percentile(latencies, 0.50),
            "p95": _percentile(latencies, 0.95),
            "p99": _percentile(latencies, 0.99),
        },
    )


def _group_by_category(
    results: Sequence[BenchmarkQuestionResult],
) -> dict[str, list[BenchmarkQuestionResult]]:
    grouped: dict[str, list[BenchmarkQuestionResult]] = {}
    for result in results:
        grouped.setdefault(result.category, []).append(result)
    return grouped


def _percentile(values: Sequence[float], quantile: float) -> float:
    if not values:
        return 0.0
    index = max(0, min(len(values) - 1, math.ceil(len(values) * quantile) - 1))
    return values[index]
