"""C0-C4 compression, observation, rescue, and tombstone lifecycle."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime, timedelta

from altm.context import ContentRouter
from altm.contracts import (
    LifecycleMeta,
    LifecycleState,
    MemoryLayer,
    MemoryStatus,
    MemoryUnit,
)
from altm.lifecycle.retention import RetentionManager
from altm.storage import SQLiteMemoryStore
from altm.utils import sha256_text, utc_now_iso

_AGE_THRESHOLDS = {1: 5, 2: 10, 3: 20, 4: 30}
_TARGET_RATIOS = {1: 0.70, 2: 0.50, 3: 0.30, 4: 0.15}


class CompressionLifecycleManager:
    def __init__(self, store: SQLiteMemoryStore) -> None:
        if store.scope is None:
            raise RuntimeError("Compression lifecycle requires an explicit memory scope")
        self.store = store
        self.router = ContentRouter(store)

    def run(self, limit: int = 1000) -> Sequence[MemoryUnit]:
        updated: list[MemoryUnit] = []
        for memory in self.store.list_memory_units(limit=limit):
            if memory.layer == MemoryLayer.L0:
                continue
            rescued = self._rescue_if_useful(memory)
            if rescued is not None:
                self.store.put_memory_unit(rescued)
                updated.append(rescued)
                continue
            if self._observation_expired(memory):
                RetentionManager(self.store).delete_memory(
                    memory_id=memory.id,
                    reason="c4_observation_expired",
                    physical=False,
                )
                continue
            compressed = self._compress_if_due(memory)
            if compressed is not None:
                self.store.put_memory_unit(compressed)
                updated.append(compressed)
        return updated

    def _compress_if_due(self, memory: MemoryUnit) -> MemoryUnit | None:
        if memory.lifecycle_state != LifecycleState.SHORT:
            return None
        if memory.metadata.get("pinned") is True:
            return None
        if memory.score.resident_score > 0.35:
            return None
        current_tier = memory.lifecycle.compression_tier
        next_tier = current_tier + 1
        if next_tier > 4 or memory.lifecycle.age < _AGE_THRESHOLDS[next_tier]:
            return None

        original = memory.content
        existing_marker = memory.metadata.get("ccr_marker")
        if isinstance(existing_marker, str):
            entry = self.router.retrieve(existing_marker)
            if entry is not None:
                original = str(entry["original_text"])
        original_tokens = max(1, (len(original) + 3) // 4)
        target_tokens = max(16, int(original_tokens * _TARGET_RATIOS[next_tier]))
        source = memory.model_copy(
            update={
                "content": original,
                "content_hash": sha256_text(original),
            }
        )
        result = self.router.compress(source, target_tokens)
        rendered = (
            result.rendered
            if result.rendered
            else "[content available through %s]" % result.marker
        )
        now = utc_now_iso()
        metadata = {
            **memory.metadata,
            "ccr_marker": result.marker,
            "compression_strategy": result.strategy,
            "compression_updated_at": now,
            "compressed_useful_access_count": memory.useful_access_count,
        }
        lifecycle = LifecycleMeta(
            age=memory.lifecycle.age,
            protection_tier=memory.lifecycle.protection_tier,
            compression_tier=next_tier,
            observation_until=(
                (datetime.now(UTC) + timedelta(days=30)).isoformat(
                    timespec="seconds"
                )
                if next_tier == 4
                else memory.lifecycle.observation_until
            ),
            promotion_candidate_since=memory.lifecycle.promotion_candidate_since,
            demotion_candidate_since=memory.lifecycle.demotion_candidate_since,
        )
        return memory.model_copy(
            update={
                "content": rendered,
                "content_hash": sha256_text(rendered),
                "status": (
                    MemoryStatus.OBSERVING
                    if next_tier == 4
                    else MemoryStatus.COMPRESSED
                ),
                "lifecycle": lifecycle,
                "metadata": metadata,
                "updated_at": now,
            }
        )

    def _rescue_if_useful(self, memory: MemoryUnit) -> MemoryUnit | None:
        marker = memory.metadata.get("ccr_marker")
        baseline = int(memory.metadata.get("compressed_useful_access_count", 0))
        if not isinstance(marker, str) or memory.useful_access_count <= baseline:
            return None
        entry = self.router.retrieve(marker)
        if entry is None:
            return None
        original = str(entry["original_text"])
        metadata = {
            **memory.metadata,
            "rescued_at": utc_now_iso(),
            "compressed_useful_access_count": memory.useful_access_count,
        }
        return memory.model_copy(
            update={
                "content": original,
                "content_hash": sha256_text(original),
                "status": MemoryStatus.ACTIVE,
                "lifecycle": memory.lifecycle.model_copy(
                    update={
                        "compression_tier": max(
                            0,
                            memory.lifecycle.compression_tier - 1,
                        ),
                        "observation_until": None,
                    }
                ),
                "metadata": metadata,
                "updated_at": utc_now_iso(),
            }
        )

    def _observation_expired(self, memory: MemoryUnit) -> bool:
        if (
            memory.lifecycle.compression_tier != 4
            or memory.status != MemoryStatus.OBSERVING
            or memory.lifecycle.observation_until is None
        ):
            return False
        return datetime.fromisoformat(memory.lifecycle.observation_until) <= datetime.now(
            UTC
        )
