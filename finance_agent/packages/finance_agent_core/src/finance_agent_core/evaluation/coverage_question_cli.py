from __future__ import annotations

import argparse
import json
from pathlib import Path

from finance_agent_core.agent.providers import LocalTestSettings
from finance_agent_core.contracts.queryplan import ProductFamily
from finance_agent_core.evaluation.coverage_plan import CoverageCellKind
from finance_agent_core.evaluation.coverage_questions import (
    coverage_question_batch_semantic_sha256,
    generate_coverage_question_batch,
)
from finance_agent_core.evaluation.coverage_runner import load_coverage_plan_suite
from finance_agent_core.evaluation.semantic_roundtrip import (
    LocalQwenSemanticQuestionProvider,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Ask local Qwen to naturalize registry-driven QueryPlans without exposing "
            "canonical source wording, then mechanically screen semantic preservation."
        )
    )
    parser.add_argument("--suite-input", type=Path, required=True)
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("artifacts/evaluation/coverage-guided-question-v1-local.json"),
    )
    parser.add_argument(
        "--family",
        action="append",
        choices=tuple(family.value for family in ProductFamily),
    )
    parser.add_argument(
        "--kind",
        action="append",
        choices=tuple(kind.value for kind in CoverageCellKind),
    )
    parser.add_argument("--offset", type=int, default=0)
    parser.add_argument("--limit", type=int)
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--require-all-generated", action="store_true")
    parser.add_argument("--require-all-accepted", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = build_parser().parse_args(argv)
    suite = load_coverage_plan_suite(arguments.suite_input)
    settings = LocalTestSettings.from_environment()
    provider = LocalQwenSemanticQuestionProvider(settings)
    provider.healthcheck()
    batch = generate_coverage_question_batch(
        provider,
        suite,
        families=(
            None
            if arguments.family is None
            else {ProductFamily(value) for value in arguments.family}
        ),
        kinds=None if arguments.kind is None else set(arguments.kind),
        offset=arguments.offset,
        limit=arguments.limit,
        workers=arguments.workers,
    )
    arguments.output.parent.mkdir(parents=True, exist_ok=True)
    arguments.output.write_text(f"{batch.model_dump_json(indent=2)}\n", encoding="utf-8")
    print(
        json.dumps(
            {
                "output": str(arguments.output),
                "generator": batch.generator,
                "model": batch.model,
                "selected_sources": batch.selected_source_count,
                "requested": batch.requested_count,
                "generated": batch.generated_count,
                "accepted": batch.accepted_count,
                "rejected": batch.rejected_count,
                "generation_failures": batch.generation_failure_count,
                "semantic_sha256": coverage_question_batch_semantic_sha256(batch),
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    if arguments.require_all_generated and batch.generation_failure_count:
        return 1
    if arguments.require_all_accepted and batch.rejected_count:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
