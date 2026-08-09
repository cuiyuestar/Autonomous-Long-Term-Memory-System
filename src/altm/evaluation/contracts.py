"""Canonical contracts for public benchmarks and anonymized agent traces."""

from __future__ import annotations

from typing import Any

from pydantic import Field

from altm.contracts import MemoryModel, MessageRole


class BenchmarkTurn(MemoryModel):
    id: str
    role: MessageRole
    content: str
    created_at: str
    evidence_ids: list[str] = Field(default_factory=lambda: list[str]())
    metadata: dict[str, Any] = Field(
        default_factory=lambda: dict[str, Any]()
    )


class BenchmarkSession(MemoryModel):
    id: str
    created_at: str
    turns: list[BenchmarkTurn] = Field(
        default_factory=lambda: list[BenchmarkTurn]()
    )
    metadata: dict[str, Any] = Field(
        default_factory=lambda: dict[str, Any]()
    )


class BenchmarkQuestion(MemoryModel):
    id: str
    corpus_id: str
    category: str
    question: str
    answer: str | None = None
    gold_evidence_ids: list[str] = Field(default_factory=lambda: list[str]())
    asked_at: str | None = None
    metadata: dict[str, Any] = Field(
        default_factory=lambda: dict[str, Any]()
    )


class BenchmarkCorpus(MemoryModel):
    id: str
    sessions: list[BenchmarkSession] = Field(
        default_factory=lambda: list[BenchmarkSession]()
    )
    questions: list[BenchmarkQuestion] = Field(
        default_factory=lambda: list[BenchmarkQuestion]()
    )
    metadata: dict[str, Any] = Field(
        default_factory=lambda: dict[str, Any]()
    )


class BenchmarkDataset(MemoryModel):
    name: str
    source_path: str
    source_sha256: str
    corpora: list[BenchmarkCorpus] = Field(
        default_factory=lambda: list[BenchmarkCorpus]()
    )
    metadata: dict[str, Any] = Field(
        default_factory=lambda: dict[str, Any]()
    )


class BenchmarkQuestionResult(MemoryModel):
    question_id: str
    corpus_id: str
    category: str
    gold_evidence_ids: list[str]
    retrieved_evidence_ids: list[str]
    retrieved_memory_ids: list[str]
    latency_ms: float
    reciprocal_rank: float
    metrics: dict[str, float]
    skipped: bool = False
    skip_reason: str | None = None


class BenchmarkAggregate(MemoryModel):
    question_count: int
    scored_question_count: int
    skipped_question_count: int
    metrics: dict[str, float]
    latency_ms: dict[str, float]


class BenchmarkReport(MemoryModel):
    benchmark: str
    dataset_sha256: str
    ran_at: str
    duration_ms: float
    config: dict[str, Any]
    aggregate: BenchmarkAggregate
    per_category: dict[str, BenchmarkAggregate]
    questions: list[BenchmarkQuestionResult]
    environment: dict[str, Any]
