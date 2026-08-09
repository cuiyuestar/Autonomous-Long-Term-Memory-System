"""LLM-backed incremental L1 context capsule summarization."""

from __future__ import annotations

import json
from collections.abc import Sequence
from typing import Any, cast

from altm.contracts import (
    ContextCapsule,
    EvidenceRef,
    EvidenceRelation,
    FallbackLocator,
    LifecycleState,
    MemoryLayer,
    MemoryStatus,
    MemoryUnit,
)
from altm.llm import OpenAICompatibleClient
from altm.storage import SQLiteMemoryStore
from altm.utils import sha256_text, stable_id, utc_now_iso

SYSTEM_PROMPT = """You create an L1 context capsule from an ordered conversation segment.

Return only one JSON object with this schema:
{
  "title": "concise segment title",
  "task_goal": "the active user goal, or null",
  "local_context": "a faithful compact summary preserving motivations and corrections",
  "key_turns": ["important turn"],
  "decisions_mentioned": ["decision explicitly supported by the source"],
  "unresolved_questions": ["open question"],
  "emotional_or_pragmatic_tone": "optional tone, or null",
  "topic_tags": ["specific topic"],
  "confidence": 0.0
}

Rules:
- Preserve local context, task transitions, corrections, motivations, and unresolved work.
- Do not invent facts, decisions, preferences, or persona traits.
- Distinguish temporary task instructions from durable user preferences.
- Do not emit source ids; the runtime binds evidence deterministically.
- Use the source language.
"""


class LLMContextCapsuleSummarizer:
    def __init__(
        self,
        store: SQLiteMemoryStore,
        llm_client: OpenAICompatibleClient,
        batch_size: int = 80,
    ) -> None:
        self.store = store
        self.llm_client = llm_client
        self.batch_size = max(1, batch_size)

    def fold_session(self, session_id: str) -> Sequence[MemoryUnit]:
        if self.store.scope is None:
            raise RuntimeError("L1 summarization requires an explicit memory scope")
        checkpoint_scope = stable_id(
            "checkpoint",
            "l1",
            *self.store.scope.key_parts(),
            session_id,
        )
        l0_units, next_cursor = self.store.list_unprocessed_session_memories(
            layer=MemoryLayer.L0,
            session_id=session_id,
            checkpoint_scope=checkpoint_scope,
            limit=self.batch_size,
        )
        if not l0_units:
            return []

        response = self.llm_client.chat_json(
            [
                {"role": "system", "content": SYSTEM_PROMPT},
                {
                    "role": "user",
                    "content": json.dumps(
                        {
                            "session_id": session_id,
                            "messages": [
                                {
                                    "memory_id": unit.id,
                                    "message_id": unit.metadata.get("message_id"),
                                    "role": unit.metadata.get("role"),
                                    "created_at": unit.created_at,
                                    "content": unit.content,
                                }
                                for unit in l0_units
                            ],
                        },
                        ensure_ascii=False,
                    ),
                },
            ]
        )
        capsule = self._parse_capsule(response, session_id, l0_units)
        content = capsule.model_dump_json(indent=2)
        now = utc_now_iso()
        memory = MemoryUnit(
            id=capsule.id,
            scope=self.store.scope,
            layer=MemoryLayer.L1,
            lifecycle_state=LifecycleState.SHORT,
            status=MemoryStatus.ACTIVE,
            content=content,
            content_hash=sha256_text(content),
            summary=capsule.local_context,
            created_at=now,
            updated_at=now,
            evidence_refs=[
                EvidenceRef(
                    target_id=unit.id,
                    target_layer=MemoryLayer.L0,
                    relation=EvidenceRelation.SOURCE,
                    confidence=1.0,
                    fallback_locator=FallbackLocator(
                        session_id=session_id,
                        message_ids=[str(unit.metadata.get("message_id", unit.id))],
                        time_range=(unit.created_at, unit.created_at),
                        topic_tags=capsule.topic_tags,
                        text_hash=unit.content_hash,
                        excerpt=_truncate(unit.content, 240),
                    ),
                )
                for unit in l0_units
            ],
            metadata={
                "session_id": session_id,
                "source_message_ids": capsule.source_message_ids,
                "builder": "llm_context_capsule_summarizer",
                "llm_model": self.llm_client.config.model,
                "checkpoint_scope": checkpoint_scope,
                "checkpoint_cursor": str(next_cursor),
            },
        )
        self.store.put_memory_unit(memory)
        self.store.put_l1_context_capsule(capsule, memory.id)
        self.store.put_checkpoint(
            checkpoint_scope,
            str(next_cursor),
            metadata={
                "stage": "l1",
                "session_id": session_id,
                "memory_id": memory.id,
                "source_count": len(l0_units),
            },
        )
        return [memory]

    def _parse_capsule(
        self,
        response: dict[str, Any],
        session_id: str,
        l0_units: Sequence[MemoryUnit],
    ) -> ContextCapsule:
        title = str(response.get("title", "")).strip()
        local_context = str(response.get("local_context", "")).strip()
        if not title or not local_context:
            raise ValueError("L1 model response requires non-empty title and local_context")

        confidence = float(response.get("confidence", 0.5))
        if not 0.0 <= confidence <= 1.0:
            raise ValueError("L1 model confidence must be between 0 and 1")

        source_message_ids = [
            str(unit.metadata.get("message_id", unit.id)) for unit in l0_units
        ]
        capsule_id = stable_id(
            "l1",
            *self.store.scope.key_parts() if self.store.scope is not None else (),
            session_id,
            *[unit.content_hash for unit in l0_units],
        )
        return ContextCapsule(
            id=capsule_id,
            title=title,
            time_range=(l0_units[0].created_at, l0_units[-1].created_at),
            session_id=session_id,
            source_message_ids=source_message_ids,
            task_goal=_optional_text(response.get("task_goal")),
            local_context=local_context,
            key_turns=_string_list(response.get("key_turns")),
            decisions_mentioned=_string_list(response.get("decisions_mentioned")),
            unresolved_questions=_string_list(response.get("unresolved_questions")),
            emotional_or_pragmatic_tone=_optional_text(
                response.get("emotional_or_pragmatic_tone")
            ),
            topic_tags=_string_list(response.get("topic_tags")),
            confidence=confidence,
        )


def _string_list(value: object) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise ValueError("L1 model list fields must be arrays")
    return [
        text
        for item in cast(list[object], value)
        if (text := str(item).strip())
    ]


def _optional_text(value: object) -> str | None:
    if value is None:
        return None
    normalized = str(value).strip()
    return normalized or None


def _truncate(value: str, max_length: int) -> str:
    normalized = " ".join(value.strip().split())
    if len(normalized) <= max_length:
        return normalized
    return normalized[: max_length - 3] + "..."
