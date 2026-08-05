from __future__ import annotations

import argparse
import json
from pathlib import Path

from finance_agent_core.agent import RoutedFinanceAgent
from finance_agent_core.agent.providers import LocalTestProvider, LocalTestSettings
from finance_agent_core.answering import (
    ExpectedGroundedAnswerProvider,
    LocalGroundedAnswerProvider,
)
from finance_agent_core.contracts.queryplan import ProductFamily
from finance_agent_core.evaluation.red_team_e2e import (
    InstrumentedAnswerProvider,
    InstrumentedQueryPlanProvider,
    InternalRedTeamRunner,
    ProviderTelemetry,
    load_internal_red_team_suite,
    verify_red_team_databases,
)
from finance_agent_core.storage import (
    ProductIdentitySnapshotCache,
    RecordSnapshotCache,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Run internal-red-team-v1 through Router, compiler, Oracle, verifier, "
            "grounded answer, and Backend DTO."
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


def _database_paths(database_dir: Path) -> dict[ProductFamily, Path]:
    return {
        ProductFamily.OVERSEAS_ETP: database_dir / "overseas_etp.sqlite3",
        ProductFamily.DOMESTIC_ETP: database_dir / "domestic_etp.sqlite3",
        ProductFamily.BOND: database_dir / "bond.sqlite3",
        ProductFamily.FUND: database_dir / "fund.sqlite3",
    }


def _build_services(
    *,
    provider_name: str,
    database_paths: dict[ProductFamily, Path],
    telemetry: ProviderTelemetry,
) -> tuple[dict[ProductFamily, RoutedFinanceAgent], str | None]:
    record_cache = RecordSnapshotCache(max_entries=4)
    identity_cache = ProductIdentitySnapshotCache(max_entries=4)
    if provider_name == "local_test":
        settings = LocalTestSettings.from_environment()
        standard_query_provider = InstrumentedQueryPlanProvider(
            LocalTestProvider(settings),
            telemetry,
        )
        fund_query_provider = InstrumentedQueryPlanProvider(
            LocalTestProvider(settings, internal_evaluation_family="fund"),
            telemetry,
        )
        answer_provider = InstrumentedAnswerProvider(
            LocalGroundedAnswerProvider(settings),
            telemetry,
        )
        standard_query_provider.provider.healthcheck()
        answer_provider.provider.healthcheck()
        model = settings.model
    else:
        standard_query_provider = None
        fund_query_provider = None
        answer_provider = InstrumentedAnswerProvider(
            ExpectedGroundedAnswerProvider(),
            telemetry,
        )
        model = None

    services: dict[ProductFamily, RoutedFinanceAgent] = {}
    for family in ProductFamily:
        query_provider = (
            fund_query_provider if family is ProductFamily.FUND else standard_query_provider
        )
        services[family] = RoutedFinanceAgent(
            database_paths,
            query_plan_provider=query_provider,
            answer_provider=answer_provider,
            allow_internal_disabled_dataset=True,
            record_cache=record_cache,
            identity_cache=identity_cache,
        )
    return services, model


def main(argv: list[str] | None = None) -> int:
    arguments = build_parser().parse_args(argv)
    provider_name = str(arguments.provider)
    loaded_suite = load_internal_red_team_suite()
    database_paths = _database_paths(arguments.database_dir)
    database_hashes = verify_red_team_databases(loaded_suite.suite, database_paths)
    telemetry = ProviderTelemetry()
    services, model = _build_services(
        provider_name=provider_name,
        database_paths=database_paths,
        telemetry=telemetry,
    )
    report = InternalRedTeamRunner(
        loaded_suite=loaded_suite,
        services=services,
        profile=provider_name,
        database_sha256_by_family=database_hashes,
        telemetry=telemetry,
        model=model,
    ).run()
    output = arguments.output or Path(
        f"artifacts/evaluation/internal-red-team-v1-{provider_name}.json"
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
                "safety_pass_rate": report.summary.safety_pass_rate,
                "fallback_rate": report.summary.fallback_rate,
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
