from __future__ import annotations

import argparse
import json
from pathlib import Path

from finance_agent_core.agent.providers import LocalTestProvider, LocalTestSettings
from finance_agent_core.evaluation.models import EvaluationSplit
from finance_agent_core.evaluation.runner import (
    EvaluationRunner,
    ExpectedPlanProvider,
    build_report,
    sha256_file,
)
from finance_agent_core.evaluation.suite import load_core_evaluation_suite


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run a frozen ETP QueryPlan and oracle regression suite."
    )
    parser.add_argument(
        "--dataset",
        choices=("overseas_etp", "domestic_etp", "bond"),
        default="overseas_etp",
    )
    parser.add_argument(
        "--database",
        type=Path,
    )
    parser.add_argument(
        "--manifest",
        type=Path,
    )
    parser.add_argument("--provider", choices=("expected", "local_test"), default="expected")
    parser.add_argument(
        "--split",
        choices=("development", "holdout", "all"),
        default="all",
    )
    parser.add_argument("--workers", type=int, default=1)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--require-perfect", action="store_true")
    return parser


def _selected_cases(suite, split: str):
    if split == "all":
        return suite.cases
    selected = EvaluationSplit(split)
    return [case for case in suite.cases if case.split is selected]


def main(argv: list[str] | None = None) -> int:
    arguments = build_parser().parse_args(argv)
    database = arguments.database or Path(f"artifacts/normalized/{arguments.dataset}.sqlite3")
    manifest = arguments.manifest or Path(f"{database}.manifest.json")
    loaded = load_core_evaluation_suite(arguments.dataset)
    suite = loaded.suite
    database_sha256 = sha256_file(database)
    manifest_sha256 = sha256_file(manifest)
    if database_sha256 != suite.database_sha256:
        raise RuntimeError(
            f"database hash mismatch: expected {suite.database_sha256}, got {database_sha256}"
        )
    if manifest_sha256 != suite.manifest_sha256:
        raise RuntimeError(
            f"manifest hash mismatch: expected {suite.manifest_sha256}, got {manifest_sha256}"
        )
    cases = _selected_cases(suite, arguments.split)
    model: str | None = None
    if arguments.provider == "local_test":
        settings = LocalTestSettings.from_environment()
        provider = LocalTestProvider(settings)
        provider.healthcheck()
        model = settings.model
    else:
        provider = ExpectedPlanProvider(cases, suite.dataset)
    runner = EvaluationRunner(database, provider)
    results = runner.run(cases, arguments.workers)
    report = build_report(
        suite_id=suite.suite_id,
        suite_version=suite.suite_version,
        suite_sha256=loaded.suite_sha256,
        database_sha256=database_sha256,
        manifest_sha256=manifest_sha256,
        provider=arguments.provider,
        model=model,
        split=arguments.split,
        workers=arguments.workers,
        results=results,
    )
    output = arguments.output or Path(
        f"artifacts/evaluation/{arguments.dataset}-{arguments.provider}-{arguments.split}.json"
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(f"{report.model_dump_json(indent=2)}\n", encoding="utf-8")
    print(
        json.dumps(
            {
                "provider": report.provider,
                "model": report.model,
                "split": report.split,
                "summary": report.summary.model_dump(mode="json"),
                "output": str(output),
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    if arguments.require_perfect and report.summary.passed != report.summary.total:
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
