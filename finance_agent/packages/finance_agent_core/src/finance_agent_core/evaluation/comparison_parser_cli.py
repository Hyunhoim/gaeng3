from __future__ import annotations

import argparse
import json
from pathlib import Path

from finance_agent_core.agent.providers import (
    LocalFundComparisonDraftProvider,
    LocalTestSettings,
)
from finance_agent_core.evaluation.comparison_parser_runner import (
    ExpectedFundComparisonDraftProvider,
    FundComparisonParserEvaluationRunner,
    build_fund_comparison_parser_report,
    load_fund_comparison_parser_suite,
)
from finance_agent_core.evaluation.runner import sha256_file


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Evaluate natural-language public-fund comparison resolution."
    )
    parser.add_argument(
        "--database",
        type=Path,
        default=Path("artifacts/normalized/fund.sqlite3"),
    )
    parser.add_argument("--manifest", type=Path)
    parser.add_argument(
        "--provider",
        choices=("expected", "local_test"),
        default="expected",
    )
    parser.add_argument(
        "--split",
        choices=("development", "holdout", "all"),
        default="all",
    )
    parser.add_argument("--workers", type=int, default=1)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--require-perfect", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = build_parser().parse_args(argv)
    manifest = arguments.manifest or Path(f"{arguments.database}.manifest.json")
    loaded = load_fund_comparison_parser_suite()
    database_sha256 = sha256_file(arguments.database)
    manifest_sha256 = sha256_file(manifest)
    if database_sha256 != loaded.suite.database_sha256:
        raise RuntimeError(
            "database hash mismatch: "
            f"expected {loaded.suite.database_sha256}, got {database_sha256}"
        )
    if manifest_sha256 != loaded.suite.manifest_sha256:
        raise RuntimeError(
            "manifest hash mismatch: "
            f"expected {loaded.suite.manifest_sha256}, got {manifest_sha256}"
        )
    cases = [
        case
        for case in loaded.suite.cases
        if arguments.split == "all" or case.split.value == arguments.split
    ]
    if arguments.provider == "local_test":
        provider = LocalFundComparisonDraftProvider(LocalTestSettings.from_environment())
        provider.healthcheck()
    else:
        provider = ExpectedFundComparisonDraftProvider(cases)
    runner = FundComparisonParserEvaluationRunner(arguments.database, provider)
    results = runner.run(cases, arguments.workers)
    report = build_fund_comparison_parser_report(
        loaded=loaded,
        provider=provider,
        split=arguments.split,
        workers=arguments.workers,
        results=results,
    )
    output = arguments.output or Path(
        f"artifacts/evaluation/fund-compare-parser-{arguments.provider}-{arguments.split}.json"
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(f"{report.model_dump_json(indent=2)}\n", encoding="utf-8")
    print(
        json.dumps(
            {
                "provider": report.provider,
                "model": report.model,
                "split": report.split,
                "isolation": report.isolation,
                "summary": report.summary.model_dump(mode="json"),
                "output": str(output),
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    if arguments.require_perfect and report.summary.passed != report.summary.total:
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
