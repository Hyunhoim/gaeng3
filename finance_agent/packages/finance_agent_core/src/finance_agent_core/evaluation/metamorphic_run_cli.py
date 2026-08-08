from __future__ import annotations

import argparse
import json
from pathlib import Path

from finance_agent_core.agent.providers import LocalTestSettings
from finance_agent_core.evaluation.metamorphic import (
    ExpectedMutationProvider,
    LocalQwenMutationProvider,
    MutationBatch,
    generate_mutation_batch,
)
from finance_agent_core.evaluation.metamorphic_runner import MetamorphicRunner
from finance_agent_core.evaluation.official_mock import (
    load_official_mock_suite,
    verify_official_mock_databases,
)
from finance_agent_core.evaluation.red_team_cli import _build_services, _database_paths
from finance_agent_core.evaluation.red_team_e2e import ProviderTelemetry


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Generate or replay a frozen metamorphic question batch through the full Agent."
        )
    )
    source = parser.add_mutually_exclusive_group()
    source.add_argument(
        "--generator",
        choices=("expected", "local_test"),
        default="expected",
    )
    source.add_argument(
        "--batch-input",
        type=Path,
        help="Replay a previously saved MutationBatch without calling a generator.",
    )
    parser.add_argument(
        "--batch-output",
        type=Path,
        help="Persist the exact generated MutationBatch for deterministic replay.",
    )
    parser.add_argument(
        "--agent-provider",
        choices=(
            "expected",
            "local_test_plan_only",
            "local_test_answer_only",
            "local_test",
        ),
        default="expected",
    )
    parser.add_argument(
        "--database-dir",
        type=Path,
        default=Path("artifacts/normalized"),
    )
    parser.add_argument("--output", type=Path)
    parser.add_argument("--require-perfect", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = build_parser().parse_args(argv)
    if arguments.batch_input is not None:
        batch = MutationBatch.model_validate_json(
            arguments.batch_input.read_text(encoding="utf-8")
        )
    else:
        if arguments.generator == "local_test":
            mutation_provider = LocalQwenMutationProvider(LocalTestSettings.from_environment())
            mutation_provider.healthcheck()
        else:
            mutation_provider = ExpectedMutationProvider()
        batch = generate_mutation_batch(mutation_provider)
    if arguments.batch_output is not None:
        arguments.batch_output.parent.mkdir(parents=True, exist_ok=True)
        arguments.batch_output.write_text(
            f"{batch.model_dump_json(indent=2)}\n",
            encoding="utf-8",
        )
    loaded_source = load_official_mock_suite()
    database_paths = _database_paths(arguments.database_dir)
    database_hashes = verify_official_mock_databases(loaded_source.suite, database_paths)
    telemetry = ProviderTelemetry()
    services, model = _build_services(
        provider_name=str(arguments.agent_provider),
        database_paths=database_paths,
        telemetry=telemetry,
    )
    report = MetamorphicRunner(
        batch=batch,
        services=services,
        agent_profile=str(arguments.agent_provider),  # type: ignore[arg-type]
        database_sha256_by_family=database_hashes,
        telemetry=telemetry,
        agent_model=model,
    ).run()
    output = arguments.output or Path(
        "artifacts/evaluation/"
        f"{batch.protocol_id}-{batch.generator}-generator-"
        f"{arguments.agent_provider}-agent.json"
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(f"{report.model_dump_json(indent=2)}\n", encoding="utf-8")
    print(
        json.dumps(
            {
                "output": str(output),
                "generator": report.generator,
                "agent_provider": report.agent_profile,
                "source": (f"{report.summary.source_passed}/{report.summary.source_total}"),
                "candidate_integrity": (
                    f"{report.summary.candidate_accepted}/{report.summary.candidate_total}"
                ),
                "candidate_strict": (
                    f"{report.summary.candidate_passed}/{report.summary.candidate_executed}"
                ),
                "semantic_consistency_rate": report.summary.semantic_consistency_rate,
                "safety_pass_rate": report.summary.safety_pass_rate,
                "failure_clusters": report.summary.failure_clusters,
                "perfect": report.summary.perfect,
            },
            ensure_ascii=False,
        )
    )
    return int(arguments.require_perfect and not report.summary.perfect)


if __name__ == "__main__":
    raise SystemExit(main())
