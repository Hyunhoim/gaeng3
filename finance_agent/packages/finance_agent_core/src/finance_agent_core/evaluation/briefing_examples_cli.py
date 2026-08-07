from __future__ import annotations

import argparse
import json
from pathlib import Path

from finance_agent_core.agent import RoutedFinanceAgent
from finance_agent_core.contracts.queryplan import ProductFamily
from finance_agent_core.evaluation.briefing_examples import (
    BriefingExampleRunner,
    load_briefing_example_suite,
    report_semantic_sha256,
    verify_briefing_example_databases,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run the eight public briefing examples through the current safe path."
    )
    parser.add_argument(
        "--database-dir",
        type=Path,
        default=Path("artifacts/normalized"),
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("artifacts/evaluation/briefing-examples-v1-current.json"),
    )
    parser.add_argument("--generated-at-utc")
    parser.add_argument("--require-perfect", action="store_true")
    return parser


def _database_paths(database_dir: Path) -> dict[ProductFamily, Path]:
    return {
        ProductFamily.OVERSEAS_ETP: database_dir / "overseas_etp.sqlite3",
        ProductFamily.DOMESTIC_ETP: database_dir / "domestic_etp.sqlite3",
        ProductFamily.BOND: database_dir / "bond.sqlite3",
        ProductFamily.FUND: database_dir / "fund.sqlite3",
    }


def main(argv: list[str] | None = None) -> int:
    arguments = build_parser().parse_args(argv)
    loaded_suite = load_briefing_example_suite()
    database_paths = _database_paths(arguments.database_dir)
    database_hashes = verify_briefing_example_databases(
        loaded_suite.suite,
        database_paths,
    )
    service = RoutedFinanceAgent(database_paths)
    report = BriefingExampleRunner(
        loaded_suite=loaded_suite,
        service=service,
        database_sha256_by_family=database_hashes,
    ).run(generated_at_utc=arguments.generated_at_utc)
    arguments.output.parent.mkdir(parents=True, exist_ok=True)
    arguments.output.write_text(f"{report.model_dump_json(indent=2)}\n", encoding="utf-8")
    print(
        json.dumps(
            {
                "output": str(arguments.output),
                "passed": report.summary.passed,
                "total": report.summary.total,
                "answerable_executed": report.summary.answerable_executed,
                "unanswerable_safely_handled": report.summary.unanswerable_safely_handled,
                "unsafe_unanswerable_executions": (report.summary.unsafe_unanswerable_executions),
                "semantic_sha256": report_semantic_sha256(report),
                "perfect": report.summary.perfect,
            },
            ensure_ascii=False,
        )
    )
    if arguments.require_perfect and not report.summary.perfect:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
