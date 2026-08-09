import sys
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from altm.context import ContextBudgeter, SimpleContextGateway  # noqa: E402
from altm.contracts import (  # noqa: E402
    ContextBand,
    LifecycleState,
    MemoryLayer,
    MemoryStatus,
    MemoryUnit,
    RecallCandidate,
    ScoreBreakdown,
)
from altm.utils import sha256_text, utc_now_iso  # noqa: E402


class ContextGatewayTest(unittest.TestCase):
    def test_assemble_context_bundle_with_bands_and_markers(self) -> None:
        candidates = [
            _candidate("m1", MemoryLayer.L2, "first memory", 0.9),
            _candidate("m2", MemoryLayer.L1, "second memory", 0.5),
            _candidate("m3", MemoryLayer.L0, "third memory", 0.2),
        ]

        bundle = SimpleContextGateway().assemble(candidates, token_budget=40)

        self.assertEqual(len(bundle.items), 3)
        self.assertEqual(bundle.metadata["budget_strategy"], "char_estimate_4_chars_per_token")
        self.assertEqual(bundle.metadata["budget_degraded_reason"], "not_configured")
        self.assertEqual(bundle.items[0].band, ContextBand.IMMEDIATE)
        self.assertEqual(bundle.items[1].band, ContextBand.WORKING)
        self.assertEqual(bundle.items[2].band, ContextBand.BACKGROUND)
        self.assertEqual(bundle.items[0].retrieval_marker, "memory://m1")
        self.assertEqual(bundle.items[0].source_memory_ids, ["m1"])
        self.assertEqual(bundle.metadata["included_count"], 3)

    def test_assemble_respects_budget_by_truncating(self) -> None:
        candidates = [_candidate("m1", MemoryLayer.L2, "x" * 200, 0.9)]

        bundle = SimpleContextGateway().assemble(candidates, token_budget=5)

        self.assertEqual(len(bundle.items), 1)
        self.assertLessEqual(len(bundle.items[0].content), 20)
        self.assertTrue(bundle.items[0].metadata["truncated"])

    def test_assemble_can_use_injected_token_counter(self) -> None:
        candidates = [_candidate("m1", MemoryLayer.L2, "alpha beta gamma delta epsilon", 0.9)]
        budgeter = ContextBudgeter(
            token_counter=lambda text: len(text.split()),
            strategy="test_word_tokenizer",
        )

        bundle = SimpleContextGateway(budgeter=budgeter).assemble(candidates, token_budget=4)

        self.assertEqual(bundle.metadata["budget_strategy"], "test_word_tokenizer")
        self.assertLessEqual(bundle.items[0].metadata["token_count_estimate"], 4)
        self.assertTrue(bundle.items[0].metadata["truncated"])

    def test_optional_tokenizer_flag_can_disable_tokenizer_budgeting(self) -> None:
        with patch.dict(
            "os.environ",
            {
                "ALTM_ENABLE_OPTIONAL_CONTEXT_TOKENIZER": "false",
                "ALTM_CONTEXT_TOKENIZER": "tiktoken",
            },
        ):
            budgeter = ContextBudgeter.from_env()

        self.assertEqual(budgeter.strategy, "char_estimate_4_chars_per_token")
        self.assertTrue(budgeter.degraded)
        self.assertEqual(budgeter.degraded_reason, "disabled_by_flag")


def _candidate(
    memory_id: str,
    layer: MemoryLayer,
    content: str,
    retrieval_score: float,
) -> RecallCandidate:
    now = utc_now_iso()
    memory = MemoryUnit(
        id=memory_id,
        layer=layer,
        lifecycle_state=LifecycleState.SHORT,
        status=MemoryStatus.ACTIVE,
        content=content,
        content_hash=sha256_text(content),
        summary=content,
        created_at=now,
        updated_at=now,
    )
    return RecallCandidate(
        memory=memory,
        score=ScoreBreakdown(retrieval_score=retrieval_score),
        matched_by=["test"],
    )


if __name__ == "__main__":
    unittest.main()
