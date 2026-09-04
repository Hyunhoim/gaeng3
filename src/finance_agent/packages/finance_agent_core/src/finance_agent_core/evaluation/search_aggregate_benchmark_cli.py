from __future__ import annotations

import argparse
import json
from pathlib import Path

from finance_agent_core.contracts.queryplan import ProductFamily
from finance_agent_core.evaluation.runner import sha256_file
from finance_agent_core.evaluation.search_aggregate_benchmark import (
    LoadedSearchAggregateBenchmarkSuite,
    SearchAggregateBenchmarkRunner,
    build_search_aggregate_benchmark_report,
    execute_benchmark_case,
    load_search_aggregate_benchmark_suite,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Benchmark isolated deterministic SEARCH and AGGREGATE paths."
    )
    parser.add_argument(
        "--database-dir",
        type=Path,
        default=Path("artifacts/normalized"),
    )
    parser.add_argument("--output", type=Path)
    parser.add_argument("--require-perfect", action="store_true")
    parser.add_argument("--child-case", help=argparse.SUPPRESS)
    return parser


def _database_paths(database_dir: Path) -> dict[ProductFamily, Path]:
    return {family: database_dir / f"{family.value}.sqlite3" for family in ProductFamily}


def _validate_data_contracts(
    database_paths: dict[ProductFamily, Path],
    loaded: LoadedSearchAggregateBenchmarkSuite,
) -> None:
    suite = loaded.suite
    for family, contract in suite.data.items():
        database = database_paths[family]
        manifest = Path(f"{database}.manifest.json")
        actual_database = sha256_file(database)
        actual_manifest = sha256_file(manifest)
        if actual_database != contract.database_sha256:
            raise RuntimeError(
                f"{family.value} database hash mismatch: "
                f"expected {contract.database_sha256}, got {actual_database}"
            )
        if actual_manifest != contract.manifest_sha256:
            raise RuntimeError(
                f"{family.value} manifest hash mismatch: "
                f"expected {contract.manifest_sha256}, got {actual_manifest}"
            )
        source_sha256 = json.loads(manifest.read_text(encoding="utf-8")).get("source_file_sha256")
        if source_sha256 != contract.source_file_sha256:
            raise RuntimeError(
                f"{family.value} source file hash mismatch: "
                f"expected {contract.source_file_sha256}, got {source_sha256}"
            )


def main(argv: list[str] | None = None) -> int:
    arguments = build_parser().parse_args(argv)
    loaded = load_search_aggregate_benchmark_suite()
    database_paths = _database_paths(arguments.database_dir)
    _validate_data_contracts(database_paths, loaded)

    if arguments.child_case:
        try:
            case = next(case for case in loaded.suite.cases if case.id == arguments.child_case)
        except StopIteration as error:
            raise ValueError(f"unknown benchmark case: {arguments.child_case}") from error
        result = execute_benchmark_case(case, database_paths[case.product_family])
        print(result.model_dump_json())
        return 0

    runner = SearchAggregateBenchmarkRunner(arguments.database_dir)
    results = runner.run(loaded.suite.cases)
    report = build_search_aggregate_benchmark_report(loaded, results)
    output = arguments.output or Path("artifacts/evaluation/search-aggregate-performance-v1.json")
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(f"{report.model_dump_json(indent=2)}\n", encoding="utf-8")
    print(
        json.dumps(
            {
                "suite_id": report.suite_id,
                "suite_sha256": report.suite_sha256,
                "isolation": report.isolation,
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
