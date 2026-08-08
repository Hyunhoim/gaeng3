from __future__ import annotations

import argparse
import json
from pathlib import Path

from finance_agent_core.evaluation.coverage_question_runner import (
    CoverageQuestionRunReport,
    load_coverage_question_batch,
    merge_coverage_question_run_reports,
)
from finance_agent_core.evaluation.coverage_runner import load_coverage_plan_suite


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Merge matching coverage question batches and Agent run reports into "
            "one hash-validated campaign result."
        )
    )
    parser.add_argument("--suite-input", type=Path, required=True)
    parser.add_argument("--batch-input", type=Path, action="append", required=True)
    parser.add_argument("--report-input", type=Path, action="append", required=True)
    parser.add_argument("--output", type=Path, required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = build_parser().parse_args(argv)
    if len(arguments.batch_input) != len(arguments.report_input):
        raise ValueError("batch-input and report-input counts must match")
    suite = load_coverage_plan_suite(arguments.suite_input)
    batches = [load_coverage_question_batch(path) for path in arguments.batch_input]
    reports = [
        CoverageQuestionRunReport.model_validate_json(path.read_text(encoding="utf-8"))
        for path in arguments.report_input
    ]
    campaign = merge_coverage_question_run_reports(
        suite=suite,
        batches=batches,
        reports=reports,
    )
    arguments.output.parent.mkdir(parents=True, exist_ok=True)
    arguments.output.write_text(
        f"{campaign.model_dump_json(indent=2)}\n",
        encoding="utf-8",
    )
    summary = campaign.summary
    print(
        json.dumps(
            {
                "output": str(arguments.output),
                "shards": len(campaign.shards),
                "requested": summary.requested,
                "generated": summary.generated,
                "generation_failures": summary.generation_failures,
                "accepted": summary.accepted,
                "rejected": summary.rejected,
                "generator_acceptance_rate": summary.generator_acceptance_rate,
                "executed": summary.executed,
                "passed": summary.passed,
                "agent_strict_accuracy": summary.agent_strict_accuracy,
                "end_to_end_yield": summary.end_to_end_yield,
                "plan_semantic_rate": summary.plan_semantic_rate,
                "evidence_semantic_rate": summary.evidence_semantic_rate,
                "fallback_count": summary.fallback_count,
                "provider_calls": campaign.provider_calls.model_dump(mode="json"),
                "perfect": summary.perfect,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
