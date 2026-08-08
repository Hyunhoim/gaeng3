from __future__ import annotations

import argparse
import json
from pathlib import Path

from finance_agent_core.evaluation.coverage_execution_audit import (
    audit_coverage_execution_semantics,
)
from finance_agent_core.evaluation.coverage_question_runner import (
    CoverageQuestionCampaignReport,
)
from finance_agent_core.evaluation.coverage_runner import load_coverage_plan_suite
from finance_agent_core.evaluation.runner import sha256_file


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Audit a naturalized coverage campaign with a conservative execution-semantic "
            "metric while preserving the original exact strict result."
        )
    )
    parser.add_argument("--suite-input", type=Path, required=True)
    parser.add_argument("--report-input", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--generated-at-utc")
    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = build_parser().parse_args(argv)
    suite = load_coverage_plan_suite(arguments.suite_input)
    report = CoverageQuestionCampaignReport.model_validate_json(
        arguments.report_input.read_text(encoding="utf-8")
    )
    audit = audit_coverage_execution_semantics(
        suite,
        report,
        source_report_sha256=sha256_file(arguments.report_input),
        generated_at_utc=arguments.generated_at_utc,
    )
    arguments.output.parent.mkdir(parents=True, exist_ok=True)
    arguments.output.write_text(f"{audit.model_dump_json(indent=2)}\n", encoding="utf-8")
    summary = audit.summary
    print(
        json.dumps(
            {
                "output": str(arguments.output),
                "audit_policy": audit.audit_policy,
                "source_agent_profile": audit.source_agent_profile,
                "audited": summary.total,
                "exact_strict_passed": summary.exact_strict_passed,
                "execution_semantic_strict_passed": (summary.execution_semantic_strict_passed),
                "execution_inert_upgrades": summary.execution_inert_upgrades,
                "still_failed": summary.still_failed,
                "by_kind": {
                    key: value.model_dump(mode="json") for key, value in summary.by_kind.items()
                },
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
