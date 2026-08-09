"""HMAC anonymization and benchmark loading for real Agent traces."""

from __future__ import annotations

import hashlib
import hmac
import json
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import cast

from altm.contracts import MessageRole
from altm.evaluation.contracts import (
    BenchmarkCorpus,
    BenchmarkDataset,
    BenchmarkQuestion,
    BenchmarkSession,
    BenchmarkTurn,
)

TraceRedactor = Callable[[str], str]
_FALLBACK_TIME = "1970-01-01T00:00:00+00:00"


@dataclass
class _TraceSessionBuilder:
    created_at: str
    turns: list[BenchmarkTurn] = field(
        default_factory=lambda: list[BenchmarkTurn]()
    )


@dataclass
class _TraceCorpusBuilder:
    sessions: dict[str, _TraceSessionBuilder] = field(
        default_factory=lambda: dict[str, _TraceSessionBuilder]()
    )
    questions: list[BenchmarkQuestion] = field(
        default_factory=lambda: list[BenchmarkQuestion]()
    )


def redact_trace_text(text: str) -> str:
    replacements = (
        (r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]{12,}\b", "[REDACTED_BEARER_TOKEN]"),
        (r"\b(?:sk|ak)-[A-Za-z0-9_-]{12,}\b", "[REDACTED_API_KEY]"),
        (
            r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b",
            "[REDACTED_EMAIL]",
        ),
        (r"(?<!\d)(?:\+?\d[\d ()-]{8,}\d)(?!\d)", "[REDACTED_PHONE]"),
        (
            r"\b(?:\d{1,3}\.){3}\d{1,3}\b",
            "[REDACTED_IP]",
        ),
        (r"(?<!\w)/(?:Users|home)/[^/\s]+", "/[REDACTED_HOME]"),
    )
    redacted = text
    for pattern, replacement in replacements:
        redacted = re.sub(pattern, replacement, redacted)
    return redacted


def anonymize_trace_file(
    source: str | Path,
    destination: str | Path,
    salt: str,
    redactor: TraceRedactor = redact_trace_text,
) -> dict[str, object]:
    if len(salt) < 16:
        raise ValueError("Trace anonymization salt must contain at least 16 characters")
    source_path = Path(source)
    destination_path = Path(destination)
    destination_path.parent.mkdir(parents=True, exist_ok=True)
    count = 0
    digest = hashlib.sha256()
    with (
        source_path.open("r", encoding="utf-8") as input_file,
        NamedTemporaryFile(
            "w",
            encoding="utf-8",
            dir=destination_path.parent,
            prefix="%s." % destination_path.name,
            suffix=".tmp",
            delete=False,
        ) as output_file,
    ):
        temporary_path = Path(output_file.name)
        for line_number, line in enumerate(input_file, start=1):
            if not line.strip():
                continue
            raw = _required_object(
                cast(object, json.loads(line)),
                "trace line %s" % line_number,
            )
            anonymized = _anonymize_record(
                raw,
                salt,
                redactor,
            )
            encoded = (
                json.dumps(anonymized, ensure_ascii=False, sort_keys=True)
                + "\n"
            )
            output_file.write(encoded)
            digest.update(encoded.encode("utf-8"))
            count += 1
    temporary_path.replace(destination_path)
    return {
        "record_count": count,
        "output_path": str(destination_path),
        "output_sha256": digest.hexdigest(),
        "redaction_version": "deterministic_pii_v1",
    }


def load_anonymized_trace(
    path: str | Path,
    limit: int | None = None,
) -> BenchmarkDataset:
    source_path = Path(path)
    raw_bytes = source_path.read_bytes()
    corpora: dict[str, _TraceCorpusBuilder] = {}
    question_count = 0
    for line_number, line in enumerate(
        raw_bytes.decode("utf-8").splitlines(),
        start=1,
    ):
        if not line.strip():
            continue
        record = _required_object(
            cast(object, json.loads(line)),
            "anonymous trace line %s" % line_number,
        )
        if record.get("anonymized") is not True:
            raise ValueError(
                "Trace evaluation accepts only records with anonymized=true"
            )
        corpus_id = _required_text(record, "trace_id")
        session_id = _required_text(record, "session_id")
        corpus = corpora.setdefault(corpus_id, _TraceCorpusBuilder())
        session = corpus.sessions.setdefault(
            session_id,
            _TraceSessionBuilder(
                created_at=(
                    _optional_text(record.get("timestamp"))
                    or _FALLBACK_TIME
                )
            ),
        )

        kind = str(record.get("kind", "message"))
        if kind == "message":
            turn_id = _required_text(record, "turn_id")
            session.turns.append(
                BenchmarkTurn(
                    id=turn_id,
                    role=_message_role(record.get("role")),
                    content=_required_text(record, "content"),
                    created_at=(
                        _optional_text(record.get("timestamp"))
                        or _FALLBACK_TIME
                    ),
                    evidence_ids=[turn_id, session_id],
                )
            )
            continue
        if kind != "query":
            raise ValueError("Unsupported anonymous trace kind: %s" % kind)
        if limit is not None and question_count >= max(0, limit):
            continue
        corpus.questions.append(
            BenchmarkQuestion(
                id=_required_text(record, "query_id"),
                corpus_id=corpus_id,
                category=str(record.get("category", "real_trace")),
                question=_required_text(record, "query"),
                answer=_optional_text(record.get("answer")),
                gold_evidence_ids=_string_list(
                    record.get("relevant_turn_ids"),
                ),
                asked_at=_optional_text(record.get("timestamp")),
            )
        )
        question_count += 1

    benchmark_corpora: list[BenchmarkCorpus] = []
    for corpus_id, corpus in corpora.items():
        if not corpus.questions:
            continue
        sessions = [
            BenchmarkSession(
                id=session_id,
                created_at=session.created_at,
                turns=session.turns,
            )
            for session_id, session in corpus.sessions.items()
        ]
        benchmark_corpora.append(
            BenchmarkCorpus(
                id=corpus_id,
                sessions=sessions,
                questions=corpus.questions,
            )
        )
    return BenchmarkDataset(
        name="anonymous_trace",
        source_path=str(source_path),
        source_sha256=hashlib.sha256(raw_bytes).hexdigest(),
        corpora=benchmark_corpora,
        metadata={
            "format": "altm_anonymous_trace_v1",
            "question_count": question_count,
        },
    )


def _anonymize_record(
    record: dict[str, object],
    salt: str,
    redactor: TraceRedactor,
) -> dict[str, object]:
    trace_id = _anonymized_id("trace", _required_text(record, "trace_id"), salt)
    session_id = _anonymized_id(
        "session",
        _required_text(record, "session_id"),
        salt,
    )
    result: dict[str, object] = {
        "anonymized": True,
        "schema": "altm_anonymous_trace_v1",
        "kind": str(record.get("kind", "message")),
        "trace_id": trace_id,
        "session_id": session_id,
    }
    for field_name in ("timestamp", "role", "category"):
        if field_name in record:
            result[field_name] = record[field_name]
    for field_name in ("content", "query", "answer"):
        value = record.get(field_name)
        if isinstance(value, str):
            result[field_name] = redactor(value)
    if "turn_id" in record:
        result["turn_id"] = _anonymized_id(
            "turn",
            _required_text(record, "turn_id"),
            salt,
        )
    if "query_id" in record:
        result["query_id"] = _anonymized_id(
            "query",
            _required_text(record, "query_id"),
            salt,
        )
    relevant_ids = record.get("relevant_turn_ids")
    if relevant_ids is not None:
        result["relevant_turn_ids"] = [
            _anonymized_id("turn", value, salt)
            for value in _string_list(relevant_ids)
        ]
    return result


def _anonymized_id(namespace: str, value: str, salt: str) -> str:
    digest = hmac.new(
        salt.encode("utf-8"),
        ("%s:%s" % (namespace, value)).encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    return "%s_%s" % (namespace, digest[:24])


def _required_text(record: dict[str, object], field: str) -> str:
    value = _optional_text(record.get(field))
    if value is None:
        raise ValueError("Trace field %s must be a non-empty string" % field)
    return value


def _required_object(value: object, label: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise ValueError("%s must be an object" % label)
    return {
        str(key): item
        for key, item in cast(dict[object, object], value).items()
    }


def _optional_text(value: object) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


def _string_list(value: object) -> list[str]:
    if not isinstance(value, list):
        raise ValueError("Trace relevance labels must be an array of strings")
    values = cast(list[object], value)
    if any(not isinstance(item, str) for item in values):
        raise ValueError("Trace relevance labels must be an array of strings")
    return cast(list[str], values)


def _message_role(value: object) -> MessageRole:
    try:
        return MessageRole(str(value))
    except ValueError as exc:
        raise ValueError("Unsupported trace role: %s" % value) from exc
