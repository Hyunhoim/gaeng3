from __future__ import annotations

import argparse
import json
from pathlib import Path

from finance_agent_core.agent.providers import LocalTestProvider, LocalTestSettings
from finance_agent_core.contracts.queryplan import ProductFamily
from finance_agent_core.evaluation.grounded_plan_audit import GroundedPlanAuditRunner
from finance_agent_core.evaluation.metamorphic import MutationBatch
from finance_agent_core.evaluation.official_mock import (
    load_official_mock_suite,
    verify_official_mock_databases,
)
from finance_agent_core.evaluation.red_team_cli import _database_paths


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Audit local Qwen grounded-plan proposals before downstream execution."
    )
    parser.add_argument("--batch-input", type=Path, required=True)
    parser.add_argument(
        "--database-dir",
        type=Path,
        default=Path("artifacts/normalized"),
    )
    parser.add_argument("--output", type=Path, required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = build_parser().parse_args(argv)
    batch = MutationBatch.model_validate_json(arguments.batch_input.read_text(encoding="utf-8"))
    settings = LocalTestSettings.from_environment()
    standard_provider = LocalTestProvider(settings)
    fund_provider = LocalTestProvider(settings, internal_evaluation_family="fund")
    standard_provider.healthcheck()
    database_paths = _database_paths(arguments.database_dir)
    database_hashes = verify_official_mock_databases(
        load_official_mock_suite().suite,
        database_paths,
    )
    providers = {
        ProductFamily.BOND: standard_provider,
        ProductFamily.DOMESTIC_ETP: standard_provider,
        ProductFamily.OVERSEAS_ETP: standard_provider,
        ProductFamily.FUND: fund_provider,
    }
    report = GroundedPlanAuditRunner(
        batch=batch,
        database_paths=database_paths,
        providers=providers,
        database_sha256_by_family=database_hashes,
    ).run()
    arguments.output.parent.mkdir(parents=True, exist_ok=True)
    arguments.output.write_text(
        f"{report.model_dump_json(indent=2)}\n",
        encoding="utf-8",
    )
    print(
        json.dumps(
            {
                "output": str(arguments.output),
                "eligible": report.summary.eligible,
                "provider_valid": report.summary.provider_valid,
                "provider_errors": report.summary.provider_errors,
                "gate_accepted": report.summary.gate_accepted,
                "gate_rejected": report.summary.gate_rejected,
                "model_rescues": report.summary.model_rescues,
                "model_supplements": report.summary.model_supplements,
                "fail_closed": report.summary.fail_closed,
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
