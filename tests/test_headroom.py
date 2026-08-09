import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from altm.context import ContentRouter, SimpleContextGateway  # noqa: E402
from altm.contracts import (  # noqa: E402
    AccessSignal,
    LifecycleMeta,
    LifecycleState,
    MemoryLayer,
    MemoryScope,
    MemoryStatus,
    MemoryUnit,
    RecallCandidate,
    ScoreBreakdown,
)
from altm.lifecycle import CompressionLifecycleManager  # noqa: E402
from altm.storage import SQLiteMemoryStore  # noqa: E402
from altm.utils import sha256_text, utc_now_iso  # noqa: E402


class HeadroomTest(unittest.TestCase):
    def test_json_compression_persists_ccr_and_restores_original(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store = SQLiteMemoryStore(
                Path(tmpdir) / "memory.sqlite3",
                scope=_scope(),
            )
            store.initialize()
            content = json.dumps(
                {
                    "decision": "use SQLite",
                    "records": [
                        {"id": index, "payload": "value-" + ("x" * 100)}
                        for index in range(30)
                    ],
                }
            )
            memory = _memory("json-memory", content, "json")
            store.put_memory_unit(memory)
            bundle = SimpleContextGateway(store=store).assemble(
                [_candidate(memory)],
                token_budget=80,
            )

            self.assertEqual(len(bundle.items), 1)
            item = bundle.items[0]
            self.assertIn("#", item.retrieval_marker)
            self.assertEqual(item.metadata["compression_strategy"], "json_structure")
            entry = ContentRouter(store).retrieve(item.retrieval_marker)
            self.assertIsNotNone(entry)
            self.assertEqual(entry["original_text"], content)
            self.assertLess(
                entry["compressed_token_estimate"],
                entry["original_token_estimate"],
            )

    def test_natural_language_without_model_returns_marker_not_fake_summary(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store = SQLiteMemoryStore(
                Path(tmpdir) / "memory.sqlite3",
                scope=_scope(),
            )
            store.initialize()
            content = "Long natural language memory. " * 200
            memory = _memory("natural-memory", content, "natural")
            store.put_memory_unit(memory)

            with patch.dict("os.environ", {}, clear=True):
                bundle = SimpleContextGateway(store=store).assemble(
                    [_candidate(memory)],
                    token_budget=40,
                )

            item = bundle.items[0]
            self.assertEqual(
                item.metadata["compression_strategy"],
                "marker_only_model_unavailable",
            )
            self.assertIn("available through retrieval marker", item.content)
            entry = ContentRouter(store).retrieve(item.retrieval_marker)
            self.assertEqual(entry["original_text"], content)

    def test_compressed_memory_is_rescued_by_real_useful_feedback(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store = SQLiteMemoryStore(
                Path(tmpdir) / "memory.sqlite3",
                scope=_scope(),
            )
            store.initialize()
            content = json.dumps({"records": [{"value": "x" * 100} for _ in range(20)]})
            memory = _memory("compressible", content, "json").model_copy(
                update={
                    "score": ScoreBreakdown(resident_score=0.1),
                    "lifecycle": LifecycleMeta(age=5),
                }
            )
            store.put_memory_unit(memory)

            compressed = CompressionLifecycleManager(store).run()
            self.assertEqual(len(compressed), 1)
            self.assertEqual(compressed[0].lifecycle.compression_tier, 1)
            self.assertEqual(compressed[0].status, MemoryStatus.COMPRESSED)

            store.record_access_signal(memory.id, AccessSignal.USER_CONFIRMED)
            rescued = CompressionLifecycleManager(store).run()
            self.assertEqual(len(rescued), 1)
            self.assertEqual(rescued[0].content, content)
            self.assertEqual(rescued[0].status, MemoryStatus.ACTIVE)
            self.assertEqual(rescued[0].lifecycle.compression_tier, 0)

    def test_expired_c4_memory_is_tombstoned_not_physically_deleted(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store = SQLiteMemoryStore(
                Path(tmpdir) / "memory.sqlite3",
                scope=_scope(),
            )
            store.initialize()
            memory = _memory("expired-c4", "old compact memory", "natural").model_copy(
                update={
                    "status": MemoryStatus.OBSERVING,
                    "lifecycle": LifecycleMeta(
                        age=40,
                        compression_tier=4,
                        observation_until="2020-01-01T00:00:00+00:00",
                    ),
                }
            )
            store.put_memory_unit(memory)

            CompressionLifecycleManager(store).run()

            tombstoned = store.get_memory_unit(memory.id)
            self.assertIsNotNone(tombstoned)
            self.assertEqual(tombstoned.status, MemoryStatus.TOMBSTONED)


def _scope() -> MemoryScope:
    return MemoryScope(
        tenant_id="tenant",
        workspace_id="workspace",
        user_id="user",
        agent_id="agent",
    )


def _memory(memory_id: str, content: str, content_type: str) -> MemoryUnit:
    now = utc_now_iso()
    return MemoryUnit(
        id=memory_id,
        scope=_scope(),
        layer=MemoryLayer.L2,
        lifecycle_state=LifecycleState.SHORT,
        status=MemoryStatus.ACTIVE,
        content=content,
        content_hash=sha256_text(content),
        summary="headroom test",
        created_at=now,
        updated_at=now,
        metadata={"content_type": content_type},
    )


def _candidate(memory: MemoryUnit) -> RecallCandidate:
    return RecallCandidate(
        memory=memory,
        score=ScoreBreakdown(retrieval_score=0.9),
        matched_by=["test"],
    )


if __name__ == "__main__":
    unittest.main()
