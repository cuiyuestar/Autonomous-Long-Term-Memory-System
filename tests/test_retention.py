import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from altm.capture import L0Recorder  # noqa: E402
from altm.contracts import (  # noqa: E402
    CaptureInput,
    EvidenceRef,
    EvidenceRelation,
    FallbackLocator,
    LifecycleState,
    MemoryLayer,
    MemoryScope,
    MemoryStatus,
    MemoryUnit,
)
from altm.lifecycle import RetentionManager  # noqa: E402
from altm.storage import SQLiteMemoryStore  # noqa: E402
from altm.utils import sha256_text, utc_now_iso  # noqa: E402


class RetentionTest(unittest.TestCase):
    def test_physical_l0_deletion_preserves_fallback_and_repairs_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store = SQLiteMemoryStore(
                Path(tmpdir) / "memory.sqlite3",
                scope=_scope(),
            )
            store.initialize()
            l0 = L0Recorder(store).capture(
                CaptureInput(
                    scope=_scope(),
                    session_id="session",
                    message_id="message",
                    content="sensitive source content",
                )
            )
            now = utc_now_iso()
            l1_content = "grounded summary"
            l1 = MemoryUnit(
                id="l1-summary",
                scope=_scope(),
                layer=MemoryLayer.L1,
                lifecycle_state=LifecycleState.SHORT,
                status=MemoryStatus.ACTIVE,
                content=l1_content,
                content_hash=sha256_text(l1_content),
                summary=l1_content,
                created_at=now,
                updated_at=now,
                evidence_refs=[
                    EvidenceRef(
                        target_id=l0.id,
                        target_layer=MemoryLayer.L0,
                        relation=EvidenceRelation.SOURCE,
                        confidence=1.0,
                        fallback_locator=FallbackLocator(
                            session_id="session",
                            message_ids=["message"],
                            text_hash=l0.content_hash,
                            excerpt="sensitive source content",
                        ),
                    )
                ],
            )
            store.put_memory_unit(l1)

            result = RetentionManager(store).delete_memory(
                memory_id=l0.id,
                reason="explicit_user_privacy_request",
                physical=True,
            )

            self.assertEqual(result["status"], "applied")
            self.assertIsNone(store.get_memory_unit(l0.id))
            repaired = store.get_memory_unit(l1.id)
            self.assertIsNotNone(repaired)
            self.assertEqual(repaired.evidence_refs[0].target_id, l0.id)
            self.assertIsNotNone(repaired.evidence_refs[0].fallback_locator)
            self.assertTrue(repaired.metadata["evidence_repairs"][0]["fallback_available"])
            with store.connect() as connection:
                request = connection.execute(
                    "SELECT status, mode FROM deletion_requests"
                ).fetchone()
            self.assertEqual((request["status"], request["mode"]), ("applied", "physical"))

    def test_configured_l0_ttl_uses_same_audited_deletion_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store = SQLiteMemoryStore(
                Path(tmpdir) / "memory.sqlite3",
                scope=_scope(),
            )
            store.initialize()
            with patch.dict(
                "os.environ",
                {"ALTM_L0_RETENTION_DAYS": "1"},
                clear=True,
            ):
                l0 = L0Recorder(store).capture(
                    CaptureInput(
                        scope=_scope(),
                        session_id="session",
                        message_id="expired",
                        content="expired source",
                        created_at="2020-01-01T00:00:00+00:00",
                    )
                )

            deleted = RetentionManager(store).delete_expired_l0()

            self.assertEqual(len(deleted), 1)
            self.assertEqual(deleted[0]["memory_id"], l0.id)
            self.assertIsNone(store.get_memory_unit(l0.id))


def _scope() -> MemoryScope:
    return MemoryScope(
        tenant_id="tenant",
        workspace_id="workspace",
        user_id="user",
        agent_id="agent",
    )


if __name__ == "__main__":
    unittest.main()
