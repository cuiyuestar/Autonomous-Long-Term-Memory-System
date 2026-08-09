import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from altm.contracts import (  # noqa: E402
    AccessSignal,
    CaptureInput,
    ContextBand,
    EvidenceRelation,
    L2AtomType,
    LifecycleState,
    MemoryLayer,
    MemoryStatus,
    ReviewActionRisk,
    ReviewActionType,
    ReviewApplyResult,
    ReviewAuditSummary,
    ReviewEvent,
    ReviewItemKind,
    ReviewStatus,
)


class ContractEnumTest(unittest.TestCase):
    def test_plan_critical_enums_keep_expected_values(self) -> None:
        self.assertEqual(MemoryLayer.L0.value, "L0")
        self.assertEqual(MemoryLayer.L4.value, "L4")
        self.assertEqual(LifecycleState.PERMANENT.value, "permanent")
        self.assertEqual(MemoryStatus.OBSERVING.value, "observing")
        self.assertEqual(EvidenceRelation.DERIVED_FROM.value, "derived_from")
        self.assertEqual(AccessSignal.USER_CONFIRMED.value, "user_confirmed")
        self.assertEqual(ContextBand.DRILLDOWN_MARKER.value, "drilldown_marker")
        self.assertEqual(L2AtomType.DECISION.value, "decision")
        self.assertEqual(ReviewStatus.PENDING.value, "pending")
        self.assertEqual(
            ReviewItemKind.SEMANTIC_DUPLICATE_CANDIDATE.value,
            "semantic_duplicate_candidate",
        )
        self.assertEqual(
            ReviewItemKind.CROSS_SESSION_L3_CANDIDATE.value,
            "cross_session_l3_candidate",
        )
        self.assertEqual(ReviewItemKind.L4_PERSONA_CANDIDATE.value, "l4_persona_candidate")
        self.assertEqual(ReviewActionType.PROMOTE_TO_LONG.value, "promote_to_long")
        self.assertEqual(ReviewActionRisk.HIGH.value, "high")
        self.assertEqual(ReviewApplyResult.__name__, "ReviewApplyResult")
        self.assertEqual(ReviewEvent.__name__, "ReviewEvent")
        self.assertEqual(ReviewAuditSummary.__name__, "ReviewAuditSummary")

    def test_capture_input_rejects_empty_content(self) -> None:
        with self.assertRaises(ValueError):
            CaptureInput(session_id="s1", content="   ")


if __name__ == "__main__":
    unittest.main()
