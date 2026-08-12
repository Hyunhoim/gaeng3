from __future__ import annotations

import argparse
import json
from pathlib import Path

from finance_agent_core.evaluation.approved_sql_baseline import (
    ApprovedSQLBaselineRunner,
    approved_database_paths,
    build_approved_sql_baseline_report,
    execute_approved_sql_case,
    validate_approved_baseline_contract,
)
from finance_agent_core.evaluation.search_aggregate_benchmark import (
    load_search_aggregate_benchmark_suite,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Measure SQL search/aggregate against the current approved DB release."
    )
    parser.add_argument("--database-dir", type=Path, default=Path("artifacts/normalized"))
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("artifacts/evaluation/approved-sql-search-aggregate-v1.json"),
    )
    parser.add_argument("--require-perfect", action="store_true")
    parser.add_argument("--child-case", help=argparse.SUPPRESS)
    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = build_parser().parse_args(argv)
    loaded = load_search_aggregate_benchmark_suite()
    paths = approved_database_paths(arguments.database_dir)
    if arguments.child_case:
        try:
            case = next(case for case in loaded.suite.cases if case.id == arguments.child_case)
        except StopIteration as error:
            raise ValueError(f"unknown benchmark case: {arguments.child_case}") from error
        result = execute_approved_sql_case(case, paths[case.product_family])
        print(result.model_dump_json())
        return 0

    validate_approved_baseline_contract(arguments.database_dir)
    results = ApprovedSQLBaselineRunner(arguments.database_dir).run(loaded.suite.cases)
    report = build_approved_sql_baseline_report(results)
    arguments.output.parent.mkdir(parents=True, exist_ok=True)
    arguments.output.write_text(f"{report.model_dump_json(indent=2)}\n", encoding="utf-8")
    print(
        json.dumps(
            {
                "evaluation_id": report.evaluation_id,
                "approved_release_id": report.approved_release_id,
                "summary": report.summary.model_dump(mode="json"),
                "output": str(arguments.output),
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
