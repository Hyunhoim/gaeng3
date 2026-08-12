from __future__ import annotations

import argparse
import json
from pathlib import Path

from finance_agent_core.evaluation.bm25_contract_benchmark import (
    run_bm25_contract_benchmark,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Measure the synthetic caller-fed SQLite FTS5/BM25 contract."
    )
    parser.add_argument("--repetitions", type=int, default=20)
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("artifacts/evaluation/bm25-document-contract-v1.json"),
    )
    parser.add_argument("--require-perfect", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = build_parser().parse_args(argv)
    report = run_bm25_contract_benchmark(repetitions=arguments.repetitions)
    arguments.output.parent.mkdir(parents=True, exist_ok=True)
    arguments.output.write_text(f"{report.model_dump_json(indent=2)}\n", encoding="utf-8")
    print(
        json.dumps(
            {
                "evaluation_id": report.evaluation_id,
                "status": report.status,
                "summary": report.summary.model_dump(mode="json"),
                "actual_corpus_quality_status": report.actual_corpus_quality_status,
                "output": str(arguments.output),
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    if arguments.require_perfect and report.summary.passed != report.summary.cases:
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
