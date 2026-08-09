"""Local-file loaders for LongMemEval and LoCoMo."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import cast

from altm.contracts import MessageRole
from altm.evaluation.contracts import (
    BenchmarkCorpus,
    BenchmarkDataset,
    BenchmarkQuestion,
    BenchmarkSession,
    BenchmarkTurn,
)

_FALLBACK_TIME = "1970-01-01T00:00:00+00:00"


def load_longmemeval(path: str | Path, limit: int | None = None) -> BenchmarkDataset:
    source_path, payload, digest = _load_json(path)
    rows = _object_list(payload, "LongMemEval dataset")
    if limit is not None:
        rows = rows[: max(0, limit)]
    corpora: list[BenchmarkCorpus] = []
    for row in rows:
        question_id = _required_text(row, "question_id")
        session_ids = _string_list(row.get("haystack_session_ids"), "haystack_session_ids")
        dates = _string_list(row.get("haystack_dates"), "haystack_dates")
        raw_sessions = _list_value(row.get("haystack_sessions"), "haystack_sessions")
        if not (len(session_ids) == len(dates) == len(raw_sessions)):
            raise ValueError(
                "LongMemEval session ids, dates, and sessions must have equal length"
            )
        sessions: list[BenchmarkSession] = []
        for session_id, created_at, raw_session in zip(
            session_ids,
            dates,
            raw_sessions,
            strict=True,
        ):
            turns: list[BenchmarkTurn] = []
            for turn_index, raw_turn in enumerate(
                _object_list(raw_session, "LongMemEval session"),
            ):
                turn_id = "%s:turn:%s" % (session_id, turn_index)
                turns.append(
                    BenchmarkTurn(
                        id=turn_id,
                        role=_message_role(raw_turn.get("role")),
                        content=_required_text(raw_turn, "content"),
                        created_at=created_at or _FALLBACK_TIME,
                        evidence_ids=[session_id, turn_id],
                        metadata={
                            "has_answer": bool(raw_turn.get("has_answer", False)),
                        },
                    )
                )
            sessions.append(
                BenchmarkSession(
                    id=session_id,
                    created_at=created_at or _FALLBACK_TIME,
                    turns=turns,
                )
            )
        answer_session_ids = _optional_string_list(row.get("answer_session_ids"))
        corpora.append(
            BenchmarkCorpus(
                id=question_id,
                sessions=sessions,
                questions=[
                    BenchmarkQuestion(
                        id=question_id,
                        corpus_id=question_id,
                        category=_required_text(row, "question_type"),
                        question=_required_text(row, "question"),
                        answer=_optional_text(row.get("answer")),
                        gold_evidence_ids=answer_session_ids,
                        asked_at=_optional_text(row.get("question_date")),
                        metadata={
                            "abstention": question_id.endswith("_abs"),
                        },
                    )
                ],
            )
        )
    return BenchmarkDataset(
        name="longmemeval",
        source_path=str(source_path),
        source_sha256=digest,
        corpora=corpora,
        metadata={
            "format": "longmemeval",
            "question_count": len(corpora),
        },
    )


def load_locomo(path: str | Path, limit: int | None = None) -> BenchmarkDataset:
    source_path, payload, digest = _load_json(path)
    rows = _top_level_rows(payload, ("data", "samples", "conversations"))
    corpora: list[BenchmarkCorpus] = []
    remaining = None if limit is None else max(0, limit)
    for row in rows:
        sample_id = _required_text(row, "sample_id")
        conversation = _required_object(row.get("conversation"), "conversation")
        speaker_a = _required_text(conversation, "speaker_a")
        session_keys = sorted(
            (
                key
                for key, value in conversation.items()
                if re.fullmatch(r"session_\d+", key) and isinstance(value, list)
            ),
            key=lambda value: int(value.rsplit("_", 1)[1]),
        )
        sessions: list[BenchmarkSession] = []
        for session_key in session_keys:
            created_at = (
                _optional_text(conversation.get("%s_date_time" % session_key))
                or _FALLBACK_TIME
            )
            turns: list[BenchmarkTurn] = []
            for turn_index, raw_turn in enumerate(
                _object_list(conversation[session_key], session_key),
            ):
                evidence_id = str(
                    raw_turn.get("dia_id")
                    or raw_turn.get("turn_id")
                    or raw_turn.get("id")
                    or "%s:%s" % (session_key, turn_index)
                )
                speaker = _required_text(raw_turn, "speaker")
                content = (
                    _optional_text(raw_turn.get("text"))
                    or _optional_text(raw_turn.get("content"))
                    or _optional_text(raw_turn.get("utterance"))
                )
                if content is None:
                    raise ValueError("LoCoMo turn requires text/content/utterance")
                turns.append(
                    BenchmarkTurn(
                        id=evidence_id,
                        role=(
                            MessageRole.USER
                            if speaker == speaker_a
                            else MessageRole.ASSISTANT
                        ),
                        content=content,
                        created_at=created_at,
                        evidence_ids=[evidence_id, session_key],
                        metadata={"speaker": speaker},
                    )
                )
            sessions.append(
                BenchmarkSession(
                    id=session_key,
                    created_at=created_at,
                    turns=turns,
                )
            )

        questions: list[BenchmarkQuestion] = []
        raw_questions = row.get("qa", row.get("qas", row.get("questions", [])))
        for question_index, raw_question in enumerate(
            _object_list(raw_questions, "LoCoMo questions"),
        ):
            if remaining is not None and remaining <= 0:
                break
            question_id = "%s:q:%s" % (sample_id, question_index)
            questions.append(
                BenchmarkQuestion(
                    id=question_id,
                    corpus_id=sample_id,
                    category=str(raw_question.get("category", "unknown")),
                    question=_required_text(raw_question, "question"),
                    answer=_optional_text(raw_question.get("answer")),
                    gold_evidence_ids=_optional_string_list(
                        raw_question.get("evidence"),
                    ),
                )
            )
            if remaining is not None:
                remaining -= 1
        if questions:
            corpora.append(
                BenchmarkCorpus(
                    id=sample_id,
                    sessions=sessions,
                    questions=questions,
                )
            )
        if remaining == 0:
            break
    return BenchmarkDataset(
        name="locomo",
        source_path=str(source_path),
        source_sha256=digest,
        corpora=corpora,
        metadata={
            "format": "locomo",
            "question_count": sum(len(corpus.questions) for corpus in corpora),
        },
    )


def _load_json(path: str | Path) -> tuple[Path, object, str]:
    source_path = Path(path)
    raw = source_path.read_bytes()
    return (
        source_path,
        cast(object, json.loads(raw.decode("utf-8"))),
        hashlib.sha256(raw).hexdigest(),
    )


def _top_level_rows(
    payload: object,
    aliases: tuple[str, ...],
) -> list[dict[str, object]]:
    if isinstance(payload, list):
        return _object_list(cast(list[object], payload), "dataset")
    root = _required_object(payload, "dataset")
    for alias in aliases:
        if alias in root:
            return _object_list(root[alias], "dataset.%s" % alias)
    raise ValueError("Dataset root must be an array or contain %s" % (aliases,))


def _object_list(value: object, label: str) -> list[dict[str, object]]:
    values = _list_value(value, label)
    if any(not isinstance(item, dict) for item in values):
        raise ValueError("%s must contain only objects" % label)
    return [
        {
            str(key): item
            for key, item in cast(dict[object, object], item).items()
        }
        for item in values
    ]


def _list_value(value: object, label: str) -> list[object]:
    if not isinstance(value, list):
        raise ValueError("%s must be an array" % label)
    return cast(list[object], value)


def _required_object(value: object, label: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise ValueError("%s must be an object" % label)
    return {
        str(key): item
        for key, item in cast(dict[object, object], value).items()
    }


def _required_text(value: dict[str, object], field: str) -> str:
    result = _optional_text(value.get(field))
    if result is None:
        raise ValueError("%s must be a non-empty string" % field)
    return result


def _optional_text(value: object) -> str | None:
    if isinstance(value, str) and value.strip():
        return value.strip()
    if isinstance(value, (int, float)):
        return str(value)
    return None


def _string_list(value: object, label: str) -> list[str]:
    if not isinstance(value, list):
        raise ValueError("%s must be an array of strings" % label)
    values = cast(list[object], value)
    if any(not isinstance(item, str) for item in values):
        raise ValueError("%s must be an array of strings" % label)
    return cast(list[str], values)


def _optional_string_list(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [
        str(item)
        for item in cast(list[object], value)
        if item is not None
    ]


def _message_role(value: object) -> MessageRole:
    try:
        return MessageRole(str(value))
    except ValueError as exc:
        raise ValueError("Unsupported benchmark message role: %s" % value) from exc
