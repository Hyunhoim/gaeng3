from __future__ import annotations

import argparse
import json
from pathlib import Path

from finance_agent_core.agent.providers import LocalTestSettings
from finance_agent_core.evaluation.metamorphic import (
    ExpectedMutationProvider,
    LocalQwenMutationProvider,
    generate_mutation_batch,
    mutation_batch_semantic_sha256,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Generate mechanically screened, non-blind metamorphic questions from the "
            "frozen official-shape mock suite."
        )
    )
    parser.add_argument(
        "--generator",
        choices=("expected", "local_test"),
        default="expected",
    )
    parser.add_argument("--output", type=Path)
    parser.add_argument("--require-all-accepted", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = build_parser().parse_args(argv)
    if arguments.generator == "local_test":
        provider = LocalQwenMutationProvider(LocalTestSettings.from_environment())
        provider.healthcheck()
    else:
        provider = ExpectedMutationProvider()
    batch = generate_mutation_batch(provider)
    output = arguments.output or Path(
        f"artifacts/evaluation/qwen-eval-lab-v1-{arguments.generator}-mutations.json"
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(f"{batch.model_dump_json(indent=2)}\n", encoding="utf-8")
    print(
        json.dumps(
            {
                "output": str(output),
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
    if arguments.require_all_accepted and batch.rejected_count:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
