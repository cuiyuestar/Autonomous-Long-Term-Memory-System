"""L0 append-only capture.

L0 is the permanent raw archive. In this phase we store one captured message as
one L0 MemoryUnit and index it through SQLite FTS.
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta

from altm.contracts import (
    CaptureInput,
    LifecycleState,
    MemoryLayer,
    MemoryStatus,
    MemoryUnit,
)
from altm.ports import MemoryStore
from altm.utils import random_id, sha256_text, stable_id, utc_now_iso


class L0Recorder:
    def __init__(self, store: MemoryStore) -> None:
        self.store = store

    def capture(self, capture_input: CaptureInput) -> MemoryUnit:
        memory = self.build(capture_input)
        existing = self.store.get_memory_unit(memory.id)
        if existing is not None:
            if (
                existing.content_hash != memory.content_hash
                or existing.metadata.get("session_id") != capture_input.session_id
                or existing.metadata.get("role") != capture_input.role.value
                or existing.scope != capture_input.scope
            ):
                raise ValueError(
                    "L0 append-only idempotency conflict for message %s"
                    % memory.metadata["message_id"]
                )
            return existing
        self.store.put_memory_unit(memory)
        return memory

    def build(self, capture_input: CaptureInput) -> MemoryUnit:
        created_at = capture_input.created_at or utc_now_iso()
        message_id = capture_input.message_id or random_id("msg")
        content_hash = sha256_text(capture_input.content)
        memory_id = stable_id(
            "l0",
            *capture_input.scope.key_parts(),
            capture_input.session_id,
            message_id,
        )

        metadata = {
            **capture_input.metadata,
            "session_id": capture_input.session_id,
            "message_id": message_id,
            "role": capture_input.role.value,
        }
        retention_days = int(os.environ.get("ALTM_L0_RETENTION_DAYS", "0"))
        if retention_days < 0:
            raise ValueError("ALTM_L0_RETENTION_DAYS must not be negative")
        if retention_days > 0 and "retention_until" not in metadata:
            metadata["retention_until"] = (
                datetime.fromisoformat(created_at) + timedelta(days=retention_days)
            ).isoformat(timespec="seconds")
        return MemoryUnit(
            id=memory_id,
            scope=capture_input.scope,
            layer=MemoryLayer.L0,
            lifecycle_state=LifecycleState.PERMANENT,
            status=MemoryStatus.ACTIVE,
            content=capture_input.content,
            content_hash=content_hash,
            created_at=created_at,
            updated_at=created_at,
            metadata=metadata,
        )
