from __future__ import annotations

import argparse
import json
from pathlib import Path

from finance_agent_core.evaluation.coverage_analysis import analyze_coverage_report
from finance_agent_core.evaluation.coverage_runner import (
    CoverageRunReport,
    load_coverage_plan_suite,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Explain coverage failures by stage and QueryPlan delta, then rank "
            "high-impact capability buckets for the next implementation cycle."
        )
    )
    parser.add_argument("--suite-input", type=Path, required=True)
    parser.add_argument("--report-input", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--top", type=int, default=15)
    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = build_parser().parse_args(argv)
    if arguments.top < 1:
        raise ValueError("coverage diagnosis top must be positive")
    suite = load_coverage_plan_suite(arguments.suite_input)
    report = CoverageRunReport.model_validate_json(
        arguments.report_input.read_text(encoding="utf-8")
    )
    diagnosis = analyze_coverage_report(suite, report)
    arguments.output.parent.mkdir(parents=True, exist_ok=True)
    arguments.output.write_text(
        f"{diagnosis.model_dump_json(indent=2)}\n",
        encoding="utf-8",
    )
    print(
        json.dumps(
            {
                "output": str(arguments.output),
                "total": diagnosis.summary.total,
                "passed": diagnosis.summary.passed,
                "failed": diagnosis.summary.failed,
                "first_failure_stages": diagnosis.summary.first_failure_stages,
                "top_priority_buckets": [
                    item.model_dump(mode="json")
                    for item in diagnosis.priority_buckets[: arguments.top]
                ],
                "top_plan_deltas": [
                    item.model_dump(mode="json")
                    for item in diagnosis.plan_delta_buckets[: arguments.top]
                ],
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
