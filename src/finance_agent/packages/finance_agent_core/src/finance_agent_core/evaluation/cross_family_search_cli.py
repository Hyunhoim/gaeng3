from __future__ import annotations

import argparse
import json
from pathlib import Path

from finance_agent_core.agent import RoutedFinanceAgent
from finance_agent_core.contracts.queryplan import ProductFamily
from finance_agent_core.evaluation.cross_family_search import (
    LoadedCrossFamilySearchSuite,
    load_cross_family_search_suite,
    run_cross_family_search_suite,
)
from finance_agent_core.evaluation.runner import sha256_file


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Evaluate deterministic cross-family parallel SEARCH v1."
    )
    parser.add_argument(
        "--database-dir",
        type=Path,
        default=Path("artifacts/normalized"),
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("artifacts/evaluation/cross-family-search-v1.json"),
    )
    parser.add_argument("--require-perfect", action="store_true")
    return parser


def _database_paths(database_dir: Path) -> dict[ProductFamily, Path]:
    return {
        family: database_dir / f"{family.value}.sqlite3"
        for family in (
            ProductFamily.DOMESTIC_ETP,
            ProductFamily.OVERSEAS_ETP,
        )
    }


def _validate_data_contracts(
    database_paths: dict[ProductFamily, Path],
    loaded: LoadedCrossFamilySearchSuite,
) -> None:
    for family, contract in loaded.suite.data.items():
        database = database_paths[family]
        manifest = Path(f"{database}.manifest.json")
        if sha256_file(database) != contract.database_sha256:
            raise RuntimeError(f"{family.value} database hash mismatch")
        if sha256_file(manifest) != contract.manifest_sha256:
            raise RuntimeError(f"{family.value} manifest hash mismatch")
        source_hash = json.loads(manifest.read_text(encoding="utf-8"))["source_file_sha256"]
        if source_hash != contract.source_file_sha256:
            raise RuntimeError(f"{family.value} source file hash mismatch")


def main(argv: list[str] | None = None) -> int:
    arguments = build_parser().parse_args(argv)
    loaded = load_cross_family_search_suite()
    database_paths = _database_paths(arguments.database_dir)
    _validate_data_contracts(database_paths, loaded)
    report = run_cross_family_search_suite(
        loaded,
        RoutedFinanceAgent(database_paths),
    )
    arguments.output.parent.mkdir(parents=True, exist_ok=True)
    arguments.output.write_text(
        f"{report.model_dump_json(indent=2)}\n",
        encoding="utf-8",
    )
    print(
        json.dumps(
            {
                "suite_id": report.suite_id,
                "suite_sha256": report.suite_sha256,
                "summary": report.summary.model_dump(mode="json"),
                "output": str(arguments.output),
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
