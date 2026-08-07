from __future__ import annotations

import argparse
import json
from pathlib import Path

from finance_agent_core.evaluation.official_mock import (
    OfficialMockRunner,
    load_official_mock_suite,
    verify_official_mock_databases,
)
from finance_agent_core.evaluation.red_team_cli import _build_services, _database_paths
from finance_agent_core.evaluation.red_team_e2e import ProviderTelemetry


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Run the public 30-case official-shaped mock through Router, provider, "
            "Oracle, verifiers, Backend DTO, and the five-string official adapter."
        )
    )
    parser.add_argument(
        "--provider",
        choices=("expected", "local_test"),
        default="expected",
    )
    parser.add_argument(
        "--database-dir",
        type=Path,
        default=Path("artifacts/normalized"),
    )
    parser.add_argument("--output", type=Path)
    parser.add_argument("--require-perfect", action="store_true")
    parser.add_argument("--require-no-fallback", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = build_parser().parse_args(argv)
    provider_name = str(arguments.provider)
    loaded_suite = load_official_mock_suite()
    database_paths = _database_paths(arguments.database_dir)
    database_hashes = verify_official_mock_databases(loaded_suite.suite, database_paths)
    telemetry = ProviderTelemetry()
    services, model = _build_services(
        provider_name=provider_name,
        database_paths=database_paths,
        telemetry=telemetry,
    )
    report = OfficialMockRunner(
        loaded_suite=loaded_suite,
        services=services,
        profile=provider_name,  # type: ignore[arg-type]
        database_sha256_by_family=database_hashes,
        telemetry=telemetry,
        model=model,
    ).run()
    output = arguments.output or Path(
        f"artifacts/evaluation/official-mock-v1-30-{provider_name}.json"
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(f"{report.model_dump_json(indent=2)}\n", encoding="utf-8")
    print(
        json.dumps(
            {
                "output": str(output),
                "provider": provider_name,
                "passed": report.summary.passed,
                "total": report.summary.total,
                "strict_accuracy": report.summary.strict_accuracy,
                "unanswerable_safety_rate": report.summary.unanswerable_safety_rate,
                "official_contract_pass_rate": report.summary.official_contract_pass_rate,
                "fallback_rate": report.summary.fallback_rate,
                "latency_ms": report.summary.latency_ms,
                "perfect": report.summary.perfect,
            },
            ensure_ascii=False,
        )
    )
    if arguments.require_perfect and not report.summary.perfect:
        return 1
    if arguments.require_no_fallback and report.summary.fallback_count:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
