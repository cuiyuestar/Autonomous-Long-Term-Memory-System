"""Rule-based L1 context capsule generator.

This is a deterministic mock for pipeline verification. It preserves source
message ids and fallback locators, but it must not be treated as a factual L2
extractor.
"""

from __future__ import annotations

import re
from typing import List, Sequence

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
from altm.storage import SQLiteMemoryStore
from altm.utils import sha256_text, stable_id, utc_now_iso


DECISION_HINTS = (
    "决定",
    "确认",
    "选择",
    "采用",
    "优先",
    "必须",
    "需要",
    "decide",
    "decided",
    "confirm",
    "confirmed",
    "choose",
    "chosen",
    "must",
    "should",
)

QUESTION_HINTS = (
    "?",
    "？",
    "待确认",
    "不确定",
    "需要确认",
    "后续确认",
    "todo",
    "tbd",
    "unclear",
)

KNOWN_TOPIC_TAGS = (
    "L0",
    "L1",
    "L2",
    "L3",
    "L4",
    "SQLite",
    "FTS",
    "MCP",
    "Pydantic",
    "Headroom",
    "CogniFold",
    "OpenClaw",
    "Hermes",
    "lifecycle",
    "retrieval",
)


class RuleBasedL1Summarizer:
    def __init__(self, store: SQLiteMemoryStore) -> None:
        self.store = store

    def fold_session(self, session_id: str) -> Sequence[MemoryUnit]:
        l0_units = list(self.store.list_l0_by_session(session_id))
        if not l0_units:
            return []

        capsule = self._build_capsule(session_id, l0_units)
        content = capsule.model_dump_json(indent=2)
        now = utc_now_iso()
        memory = MemoryUnit(
            id=capsule.id,
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
                        excerpt=self._truncate(unit.content, 160),
                    ),
                )
                for unit in l0_units
            ],
            metadata={
                "session_id": session_id,
                "source_message_ids": capsule.source_message_ids,
                "mock_strategy": "rule_based_l1_capsule",
            },
        )
        self.store.put_memory_unit(memory)
        self.store.put_l1_context_capsule(capsule, memory.id)
        return [memory]

    def _build_capsule(self, session_id: str, l0_units: Sequence[MemoryUnit]) -> ContextCapsule:
        source_message_ids = [str(unit.metadata.get("message_id", unit.id)) for unit in l0_units]
        title = self._title(l0_units)
        local_context = self._local_context(l0_units)
        time_range = (l0_units[0].created_at, l0_units[-1].created_at)
        identity = stable_id("l1", session_id, *[unit.content_hash for unit in l0_units])

        return ContextCapsule(
            id=identity,
            title=title,
            time_range=time_range,
            session_id=session_id,
            source_message_ids=source_message_ids,
            task_goal=self._task_goal(l0_units),
            local_context=local_context,
            key_turns=self._key_turns(l0_units),
            decisions_mentioned=self._matching_lines(l0_units, DECISION_HINTS, limit=8),
            unresolved_questions=self._matching_lines(l0_units, QUESTION_HINTS, limit=8),
            topic_tags=self._topic_tags(l0_units),
            confidence=0.45,
        )

    def _title(self, l0_units: Sequence[MemoryUnit]) -> str:
        first_user = next(
            (
                unit.content.strip()
                for unit in l0_units
                if unit.metadata.get("role") in {"user", "system"}
            ),
            l0_units[0].content.strip(),
        )
        return self._truncate(first_user.replace("\n", " "), 80)

    def _task_goal(self, l0_units: Sequence[MemoryUnit]) -> str:
        user_turns = [
            unit.content.strip()
            for unit in l0_units
            if unit.metadata.get("role") == "user" and unit.content.strip()
        ]
        return self._truncate(user_turns[0], 240) if user_turns else ""

    def _local_context(self, l0_units: Sequence[MemoryUnit]) -> str:
        role_counts = {}
        for unit in l0_units:
            role = str(unit.metadata.get("role", "other"))
            role_counts[role] = role_counts.get(role, 0) + 1
        role_summary = ", ".join("%s=%s" % (role, count) for role, count in sorted(role_counts.items()))
        return "Session contains %s L0 messages (%s)." % (len(l0_units), role_summary)

    def _key_turns(self, l0_units: Sequence[MemoryUnit]) -> List[str]:
        turns = []
        for unit in l0_units[:8]:
            role = unit.metadata.get("role", "other")
            turns.append("[%s] %s" % (role, self._truncate(unit.content, 180)))
        return turns

    def _matching_lines(
        self,
        l0_units: Sequence[MemoryUnit],
        hints: Sequence[str],
        limit: int,
    ) -> List[str]:
        matches: List[str] = []
        lowered_hints = tuple(hint.lower() for hint in hints)
        for unit in l0_units:
            for line in self._split_lines(unit.content):
                lowered = line.lower()
                if any(hint in lowered for hint in lowered_hints):
                    matches.append(self._truncate(line, 220))
                if len(matches) >= limit:
                    return matches
        return matches

    def _topic_tags(self, l0_units: Sequence[MemoryUnit]) -> List[str]:
        text = "\n".join(unit.content for unit in l0_units)
        tags = [tag for tag in KNOWN_TOPIC_TAGS if tag.lower() in text.lower()]

        for match in re.findall(r"`([^`]{2,40})`", text):
            if len(tags) >= 12:
                break
            if match not in tags:
                tags.append(match)
        return tags[:12]

    def _split_lines(self, text: str) -> List[str]:
        lines = []
        for raw_line in text.splitlines():
            line = raw_line.strip()
            if line:
                lines.append(line)
        return lines or [text.strip()]

    def _truncate(self, value: str, max_length: int) -> str:
        normalized = " ".join(value.strip().split())
        if len(normalized) <= max_length:
            return normalized
        return normalized[: max_length - 3] + "..."
