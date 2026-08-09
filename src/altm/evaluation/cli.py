"""Command line entry point for benchmark and anonymous trace evaluation."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from altm.evaluation.datasets import load_locomo, load_longmemeval
from altm.evaluation.runner import AltmBenchmarkRunner
from altm.evaluation.traces import anonymize_trace_file, load_anonymized_trace


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="altm-benchmark")
    subparsers = parser.add_subparsers(dest="command", required=True)

    run = subparsers.add_parser(
        "run",
        help="Run ALTM retrieval metrics against a local dataset file",
    )
    run.add_argument(
        "--format",
        required=True,
        choices=["longmemeval", "locomo", "anonymous-trace"],
    )
    run.add_argument("--dataset", required=True)
    run.add_argument("--db", required=True)
    run.add_argument("--output", required=True)
    run.add_argument("--top-k", type=int, action="append")
    run.add_argument("--limit", type=int)
    run.add_argument(
        "--enrichment",
        choices=["l0", "l2", "full"],
        default="l0",
    )

    anonymize = subparsers.add_parser(
        "anonymize-trace",
        help="HMAC identifiers and redact sensitive text before evaluation",
    )
    anonymize.add_argument("--input", required=True)
    anonymize.add_argument("--output", required=True)
    anonymize.add_argument(
        "--salt-env",
        default="ALTM_TRACE_ANONYMIZATION_SALT",
    )
    return parser


def main() -> None:
    args = build_parser().parse_args()
    if args.command == "anonymize-trace":
        salt = os.environ.get(args.salt_env)
        if not salt:
            raise RuntimeError(
                "Trace anonymization requires environment variable %s"
                % args.salt_env
            )
        result = anonymize_trace_file(
            source=args.input,
            destination=args.output,
            salt=salt,
        )
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        return

    if args.format == "longmemeval":
        dataset = load_longmemeval(args.dataset, limit=args.limit)
    elif args.format == "locomo":
        dataset = load_locomo(args.dataset, limit=args.limit)
    else:
        dataset = load_anonymized_trace(args.dataset, limit=args.limit)
    report = AltmBenchmarkRunner(
        db_path=args.db,
        top_ks=args.top_k or [5, 10],
        enrichment=args.enrichment,
    ).run(dataset)
    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        report.model_dump_json(indent=2),
        encoding="utf-8",
    )
    print(
        json.dumps(
            {
                "benchmark": report.benchmark,
                "output": str(output_path),
                "aggregate": report.aggregate.model_dump(mode="json"),
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
