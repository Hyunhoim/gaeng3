from __future__ import annotations

import argparse
import json
from pathlib import Path

from finance_agent_core.evaluation.coverage_runner import (
    CoverageAgentProfile,
    CoverageRunner,
    load_coverage_plan_suite,
    verify_coverage_databases,
)
from finance_agent_core.evaluation.red_team_cli import _build_services, _database_paths
from finance_agent_core.evaluation.red_team_e2e import ProviderTelemetry

_PROFILES: tuple[CoverageAgentProfile, ...] = (
    "expected",
    "local_test_plan_only",
    "local_test_answer_only",
    "local_test",
    "local_test_grounded_plan_only",
    "local_test_grounded",
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Run canonical questions from a coverage-guided plan suite and compare "
            "QueryPlan plus field-level evidence with direct Oracle expectations."
        )
    )
    parser.add_argument("--suite-input", type=Path, required=True)
    parser.add_argument("--database-dir", type=Path, default=Path("artifacts/normalized"))
    parser.add_argument("--agent-provider", choices=_PROFILES, default="expected")
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("artifacts/evaluation/coverage-guided-plan-v1-canonical-first.json"),
    )
    parser.add_argument("--require-perfect", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = build_parser().parse_args(argv)
    suite = load_coverage_plan_suite(arguments.suite_input)
    database_paths = _database_paths(arguments.database_dir)
    verify_coverage_databases(suite, database_paths)
    telemetry = ProviderTelemetry()
    services, model = _build_services(
        provider_name=arguments.agent_provider,
        database_paths=database_paths,
        telemetry=telemetry,
    )
    report = CoverageRunner(
        suite=suite,
        services=services,
        agent_profile=arguments.agent_provider,
        agent_model=model,
        telemetry=telemetry,
    ).run()
    arguments.output.parent.mkdir(parents=True, exist_ok=True)
    arguments.output.write_text(f"{report.model_dump_json(indent=2)}\n", encoding="utf-8")
    summary = report.summary
    print(
        json.dumps(
            {
                "output": str(arguments.output),
                "agent_provider": report.agent_profile,
                "total": summary.total,
                "passed": summary.passed,
                "strict_accuracy": summary.strict_accuracy,
                "execution_rate": summary.execution_rate,
                "plan_semantic_rate": summary.plan_semantic_rate,
                "evidence_semantic_rate": summary.evidence_semantic_rate,
                "fallback_count": summary.fallback_count,
                "by_family": summary.by_family,
                "by_kind": summary.by_kind,
                "first_failure_stages": summary.first_failure_stages,
                "perfect": summary.perfect,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return int(arguments.require_perfect and not summary.perfect)


if __name__ == "__main__":
    raise SystemExit(main())
