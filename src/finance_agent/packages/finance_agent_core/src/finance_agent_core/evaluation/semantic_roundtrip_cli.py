from __future__ import annotations

import argparse
import json
from pathlib import Path

from finance_agent_core.agent.providers import LocalTestSettings
from finance_agent_core.evaluation.metamorphic import (
    MutationBatch,
    mutation_batch_semantic_sha256,
)
from finance_agent_core.evaluation.official_mock import (
    load_official_mock_suite,
    verify_official_mock_databases,
)
from finance_agent_core.evaluation.red_team_cli import _build_services, _database_paths
from finance_agent_core.evaluation.red_team_e2e import ProviderTelemetry
from finance_agent_core.evaluation.semantic_roundtrip import (
    LocalQwenSemanticQuestionProvider,
    generate_semantic_roundtrip_batch,
    rescreen_semantic_roundtrip_batch,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Generate mechanically screened questions from QueryPlan semantics while hiding "
            "the public source wording from local Qwen."
        )
    )
    parser.add_argument(
        "--database-dir",
        type=Path,
        default=Path("artifacts/normalized"),
    )
    parser.add_argument(
        "--batch-input",
        type=Path,
        help="Reapply the current mechanical screen without regenerating Qwen text.",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path(
            "artifacts/evaluation/semantic-roundtrip-v1-local-mutations-first-observation.json"
        ),
    )
    parser.add_argument("--require-all-accepted", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = build_parser().parse_args(argv)
    source = load_official_mock_suite()
    database_paths = _database_paths(arguments.database_dir)
    verify_official_mock_databases(source.suite, database_paths)
    services, _ = _build_services(
        provider_name="expected",
        database_paths=database_paths,
        telemetry=ProviderTelemetry(),
    )
    if arguments.batch_input is not None:
        original = MutationBatch.model_validate_json(
            arguments.batch_input.read_text(encoding="utf-8")
        )
        batch = rescreen_semantic_roundtrip_batch(original, services=services)
    else:
        settings = LocalTestSettings.from_environment()
        provider = LocalQwenSemanticQuestionProvider(settings)
        provider.healthcheck()
        batch = generate_semantic_roundtrip_batch(provider, services=services)
    arguments.output.parent.mkdir(parents=True, exist_ok=True)
    arguments.output.write_text(f"{batch.model_dump_json(indent=2)}\n", encoding="utf-8")
    print(
        json.dumps(
            {
                "output": str(arguments.output),
                "generator": batch.generator,
                "model": batch.model,
                "requested": batch.requested_count,
                "generated": batch.generated_count,
                "accepted": batch.accepted_count,
                "rejected": batch.rejected_count,
                "semantic_sha256": mutation_batch_semantic_sha256(batch),
                "all_accepted": batch.rejected_count == 0,
            },
            ensure_ascii=False,
        )
    )
    return int(arguments.require_all_accepted and batch.rejected_count > 0)


if __name__ == "__main__":
    raise SystemExit(main())
