import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from altm.evaluation import (  # noqa: E402
    AltmBenchmarkRunner,
    anonymize_trace_file,
    load_anonymized_trace,
    load_locomo,
    load_longmemeval,
)
from altm.evaluation.cli import build_parser  # noqa: E402


class EvaluationTest(unittest.TestCase):
    def test_embedding_enrichment_indexes_l0_without_other_folding(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            dataset_path = root / "longmemeval.json"
            dataset_path.write_text(
                json.dumps(
                    [
                        {
                            "question_id": "q-embedding",
                            "question_type": "single-session-user",
                            "question": "What is the release deadline?",
                            "answer": "September 1, 2026",
                            "question_date": "2026-08-09",
                            "haystack_session_ids": ["s1"],
                            "haystack_dates": ["2026-08-01T00:00:00+00:00"],
                            "haystack_sessions": [
                                [
                                    {
                                        "role": "user",
                                        "content": (
                                            "The release deadline is September 1, 2026."
                                        ),
                                    }
                                ]
                            ],
                            "answer_session_ids": ["s1"],
                        }
                    ]
                ),
                encoding="utf-8",
            )
            runner = AltmBenchmarkRunner(
                root / "benchmark.sqlite3",
                top_ks=[1],
                enrichment="embedding",
            )

            with (
                patch.object(
                    runner.application,
                    "index_embeddings",
                    side_effect=[
                        {"indexed_count": 1},
                        {"indexed_count": 0},
                    ],
                ) as index_embeddings,
                patch.object(runner.application, "fold_l1") as fold_l1,
                patch.object(runner.application, "extract_l2") as extract_l2,
            ):
                report = runner.run(load_longmemeval(dataset_path))

            self.assertEqual(index_embeddings.call_count, 2)
            fold_l1.assert_not_called()
            extract_l2.assert_not_called()
            self.assertEqual(report.config["enrichment"], "embedding")

    def test_cli_accepts_embedding_enrichment(self) -> None:
        args = build_parser().parse_args(
            [
                "run",
                "--format",
                "longmemeval",
                "--dataset",
                "dataset.json",
                "--db",
                "benchmark.sqlite3",
                "--output",
                "report.json",
                "--enrichment",
                "embedding",
            ]
        )

        self.assertEqual(args.enrichment, "embedding")

    def test_ndcg_counts_each_gold_session_once(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            dataset_path = root / "longmemeval.json"
            dataset_path.write_text(
                json.dumps(
                    [
                        {
                            "question_id": "q-duplicate-session",
                            "question_type": "single-session-user",
                            "question": "What is the release deadline?",
                            "answer": "September 1, 2026",
                            "question_date": "2026-08-09",
                            "haystack_session_ids": ["s1"],
                            "haystack_dates": ["2026-08-01T00:00:00+00:00"],
                            "haystack_sessions": [
                                [
                                    {
                                        "role": "user",
                                        "content": (
                                            "The release deadline is September 1, 2026."
                                        ),
                                    },
                                    {
                                        "role": "assistant",
                                        "content": (
                                            "Confirmed: the release deadline remains "
                                            "September 1, 2026."
                                        ),
                                    },
                                ]
                            ],
                            "answer_session_ids": ["s1"],
                        }
                    ]
                ),
                encoding="utf-8",
            )

            report = AltmBenchmarkRunner(
                root / "benchmark.sqlite3",
                top_ks=[5],
            ).run(load_longmemeval(dataset_path))

            self.assertEqual(report.aggregate.metrics["ndcg@5"], 1.0)

    def test_longmemeval_loader_skips_empty_turns_and_preserves_source_index(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "longmemeval.json"
            path.write_text(
                json.dumps(
                    [
                        {
                            "question_id": "q-empty-turn",
                            "question_type": "single-session-user",
                            "question": "What is the release deadline?",
                            "answer": "September 1, 2026",
                            "question_date": "2026-08-09",
                            "haystack_session_ids": ["s1"],
                            "haystack_dates": ["2026-08-01T00:00:00+00:00"],
                            "haystack_sessions": [
                                [
                                    {"role": "user", "content": ""},
                                    {
                                        "role": "user",
                                        "content": (
                                            "The release deadline is September 1, 2026."
                                        ),
                                        "has_answer": True,
                                    },
                                ]
                            ],
                            "answer_session_ids": ["s1"],
                        }
                    ]
                ),
                encoding="utf-8",
            )

            dataset = load_longmemeval(path)

            turns = dataset.corpora[0].sessions[0].turns
            self.assertEqual(len(turns), 1)
            self.assertEqual(turns[0].id, "s1:turn:1")
            self.assertEqual(turns[0].evidence_ids, ["s1", "s1:turn:1"])

    def test_longmemeval_loader_and_native_retrieval_metrics(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            dataset_path = root / "longmemeval.json"
            dataset_path.write_text(
                json.dumps(
                    [
                        {
                            "question_id": "q1",
                            "question_type": "single-session-user",
                            "question": "What is the release deadline?",
                            "answer": "September 1, 2026",
                            "question_date": "2026-08-09",
                            "haystack_session_ids": ["s1", "s2"],
                            "haystack_dates": [
                                "2026-08-01T00:00:00+00:00",
                                "2026-08-02T00:00:00+00:00",
                            ],
                            "haystack_sessions": [
                                [
                                    {
                                        "role": "user",
                                        "content": "Unrelated lunch discussion.",
                                    }
                                ],
                                [
                                    {
                                        "role": "user",
                                        "content": (
                                            "The release deadline is September 1, 2026."
                                        ),
                                        "has_answer": True,
                                    }
                                ],
                            ],
                            "answer_session_ids": ["s2"],
                        }
                    ]
                ),
                encoding="utf-8",
            )

            dataset = load_longmemeval(dataset_path)
            report = AltmBenchmarkRunner(
                root / "benchmark.sqlite3",
                top_ks=[1, 5],
            ).run(dataset)

            self.assertEqual(dataset.metadata["question_count"], 1)
            self.assertEqual(report.aggregate.scored_question_count, 1)
            self.assertEqual(report.aggregate.metrics["recall_any@1"], 1.0)
            self.assertEqual(report.aggregate.metrics["recall_all@1"], 1.0)
            self.assertEqual(report.questions[0].retrieved_evidence_ids[0], "s2")

    def test_locomo_loader_preserves_turn_evidence_ids(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "locomo.json"
            path.write_text(
                json.dumps(
                    [
                        {
                            "sample_id": "conversation-1",
                            "conversation": {
                                "speaker_a": "Alice",
                                "speaker_b": "Bob",
                                "session_1_date_time": "2026-08-01 10:00",
                                "session_1": [
                                    {
                                        "speaker": "Alice",
                                        "dia_id": "D1:1",
                                        "text": "I moved to Seattle.",
                                    },
                                    {
                                        "speaker": "Bob",
                                        "dia_id": "D1:2",
                                        "text": "How is Seattle?",
                                    },
                                ],
                            },
                            "qa": [
                                {
                                    "question": "Where did Alice move?",
                                    "answer": "Seattle",
                                    "category": 1,
                                    "evidence": ["D1:1"],
                                }
                            ],
                        }
                    ]
                ),
                encoding="utf-8",
            )

            dataset = load_locomo(path)

            corpus = dataset.corpora[0]
            self.assertEqual(corpus.sessions[0].turns[0].id, "D1:1")
            self.assertEqual(
                corpus.questions[0].gold_evidence_ids,
                ["D1:1"],
            )
            self.assertEqual(corpus.questions[0].category, "1")

    def test_trace_anonymization_redacts_and_evaluates(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            source = root / "raw.jsonl"
            destination = root / "anonymous.jsonl"
            records = [
                {
                    "kind": "message",
                    "trace_id": "customer-project",
                    "session_id": "real-session",
                    "turn_id": "turn-secret-1",
                    "timestamp": "2026-08-01T00:00:00+00:00",
                    "role": "user",
                    "content": (
                        "Email alice@example.com. We chose SQLite for storage."
                    ),
                },
                {
                    "kind": "query",
                    "trace_id": "customer-project",
                    "session_id": "real-session",
                    "query_id": "query-secret-1",
                    "timestamp": "2026-08-02T00:00:00+00:00",
                    "query": "Which database did we choose for storage?",
                    "answer": "SQLite",
                    "relevant_turn_ids": ["turn-secret-1"],
                    "category": "decision_recall",
                },
            ]
            source.write_text(
                "".join(
                    json.dumps(record) + "\n"
                    for record in records
                ),
                encoding="utf-8",
            )

            result = anonymize_trace_file(
                source,
                destination,
                salt="0123456789abcdef-test-salt",
            )
            anonymous_text = destination.read_text(encoding="utf-8")
            dataset = load_anonymized_trace(destination)
            report = AltmBenchmarkRunner(
                root / "trace.sqlite3",
                top_ks=[1],
            ).run(dataset)

            self.assertEqual(result["record_count"], 2)
            self.assertNotIn("alice@example.com", anonymous_text)
            self.assertNotIn("customer-project", anonymous_text)
            self.assertNotIn("turn-secret-1", anonymous_text)
            self.assertIn("[REDACTED_EMAIL]", anonymous_text)
            self.assertEqual(report.aggregate.metrics["recall_any@1"], 1.0)
            self.assertEqual(report.aggregate.metrics["mrr"], 1.0)

    def test_trace_loader_rejects_raw_records(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "raw.jsonl"
            path.write_text(
                json.dumps(
                    {
                        "kind": "message",
                        "trace_id": "raw",
                        "session_id": "raw",
                    }
                ),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(ValueError, "anonymized=true"):
                load_anonymized_trace(path)


if __name__ == "__main__":
    unittest.main()
