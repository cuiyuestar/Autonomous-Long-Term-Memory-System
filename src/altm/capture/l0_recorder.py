"""L0 append-only capture.

L0 is the permanent raw archive. In this phase we store one captured message as
one L0 MemoryUnit and index it through SQLite FTS.
"""

from __future__ import annotations

from altm.contracts import (
    CaptureInput,
    LifecycleState,
    MemoryLayer,
    MemoryStatus,
    MemoryUnit,
)
from altm.ports import MemoryStore
from altm.utils import random_id, sha256_text, utc_now_iso


class L0Recorder:
    def __init__(self, store: MemoryStore) -> None:
        self.store = store

    def capture(self, capture_input: CaptureInput) -> MemoryUnit:
        created_at = capture_input.created_at or utc_now_iso()
        message_id = capture_input.message_id or random_id("msg")
        content_hash = sha256_text(capture_input.content)
        memory_id = "l0_%s" % message_id

        memory = MemoryUnit(
            id=memory_id,
            layer=MemoryLayer.L0,
            lifecycle_state=LifecycleState.PERMANENT,
            status=MemoryStatus.ACTIVE,
            content=capture_input.content,
            content_hash=content_hash,
            created_at=created_at,
            updated_at=created_at,
            metadata={
                **capture_input.metadata,
                "session_id": capture_input.session_id,
                "message_id": message_id,
                "role": capture_input.role.value,
            },
        )
        self.store.put_memory_unit(memory)
        return memory
