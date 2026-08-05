from __future__ import annotations

import argparse
import json
from pathlib import Path

from finance_agent_core.agent import RoutedFinanceAgent
from finance_agent_core.agent.providers import LocalTestSettings
from finance_agent_core.answering import (
    ExpectedGroundedAnswerProvider,
    LocalGroundedAnswerProvider,
)
from finance_agent_core.evaluation.cross_family_answer import (
    CountingGroundedAnswerProvider,
    run_cross_family_answer_suite,
)
from finance_agent_core.evaluation.cross_family_search import (
    load_cross_family_search_suite,
)
from finance_agent_core.evaluation.cross_family_search_cli import (
    _database_paths,
    _validate_data_contracts,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Evaluate cross-family evidence-only grounded answers."
    )
    parser.add_argument(
        "--database-dir",
        type=Path,
        default=Path("artifacts/normalized"),
    )
    parser.add_argument(
        "--provider",
        choices=("expected", "local_test"),
        default="expected",
    )
    parser.add_argument("--output", type=Path)
    parser.add_argument("--require-perfect", action="store_true")
    parser.add_argument("--require-zero-fallback", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = build_parser().parse_args(argv)
    loaded = load_cross_family_search_suite()
    database_paths = _database_paths(arguments.database_dir)
    _validate_data_contracts(database_paths, loaded)
    if arguments.provider == "local_test":
        delegate = LocalGroundedAnswerProvider(LocalTestSettings.from_environment())
        delegate.healthcheck()
    else:
        delegate = ExpectedGroundedAnswerProvider()
    provider = CountingGroundedAnswerProvider(delegate)
    report = run_cross_family_answer_suite(
        loaded,
        RoutedFinanceAgent(database_paths, answer_provider=provider),
        provider,
    )
    output = arguments.output or Path(
        f"artifacts/evaluation/cross-family-answer-{arguments.provider}-v1.json"
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(f"{report.model_dump_json(indent=2)}\n", encoding="utf-8")
    print(
        json.dumps(
            {
                "provider": report.provider,
                "model": report.model,
                "suite_id": report.suite_id,
                "summary": report.summary.model_dump(mode="json"),
                "output": str(output),
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    if arguments.require_perfect and report.summary.passed != report.summary.total:
        return 2
    if arguments.require_zero_fallback and report.summary.deterministic_fallback:
        return 3
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
