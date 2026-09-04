from __future__ import annotations

import argparse
import json
from pathlib import Path

from finance_agent_core.contracts.queryplan import ProductFamily
from finance_agent_core.evaluation.coverage_plan import (
    CoveragePlanSuite,
    coverage_plan_suite_semantic_sha256,
    generate_coverage_plan_suite,
    rerender_coverage_plan_suite,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Generate and directly execute a registry-driven QueryPlan coverage suite "
            "for all four product families."
        )
    )
    parser.add_argument(
        "--database-dir",
        type=Path,
        default=Path("artifacts/normalized"),
    )
    parser.add_argument(
        "--suite-input",
        type=Path,
        help="Re-render canonical wording without re-running frozen direct outcomes.",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("artifacts/evaluation/coverage-guided-plan-v1.json"),
    )
    parser.add_argument("--require-no-exclusions", action="store_true")
    return parser


def _database_paths(database_dir: Path) -> dict[ProductFamily, Path]:
    return {
        ProductFamily.BOND: database_dir / "bond.sqlite3",
        ProductFamily.DOMESTIC_ETP: database_dir / "domestic_etp.sqlite3",
        ProductFamily.OVERSEAS_ETP: database_dir / "overseas_etp.sqlite3",
        ProductFamily.FUND: database_dir / "fund.sqlite3",
    }


def main(argv: list[str] | None = None) -> int:
    arguments = build_parser().parse_args(argv)
    if arguments.suite_input is None:
        suite = generate_coverage_plan_suite(_database_paths(arguments.database_dir))
    else:
        original = CoveragePlanSuite.model_validate_json(
            arguments.suite_input.read_text(encoding="utf-8")
        )
        suite = rerender_coverage_plan_suite(original)
    arguments.output.parent.mkdir(parents=True, exist_ok=True)
    arguments.output.write_text(f"{suite.model_dump_json(indent=2)}\n", encoding="utf-8")
    summary = suite.summary
    print(
        json.dumps(
            {
                "output": str(arguments.output),
                "suite_id": suite.suite_id,
                "semantic_sha256": coverage_plan_suite_semantic_sha256(suite),
                "attempted_cells": summary.attempted_cells,
                "executable_cases": summary.executable_cases,
                "excluded_cells": summary.excluded_cells,
                "execution_rate": summary.execution_rate,
                "by_family": summary.by_family,
                "by_kind": summary.by_kind,
                "exclusion_reasons": summary.exclusion_reasons,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return int(arguments.require_no_exclusions and summary.excluded_cells > 0)


if __name__ == "__main__":
    raise SystemExit(main())
