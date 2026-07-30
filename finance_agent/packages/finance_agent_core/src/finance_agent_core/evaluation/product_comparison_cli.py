from __future__ import annotations

import argparse
import json
from pathlib import Path

from finance_agent_core.contracts.queryplan import ProductFamily
from finance_agent_core.evaluation.product_comparison_runner import (
    ProductComparisonEvaluationRunner,
    build_product_comparison_report,
    load_product_comparison_suite,
)
from finance_agent_core.evaluation.runner import sha256_file


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Evaluate public natural-language comparison regression for overseas ETP, "
            "domestic ETP, and domestic bonds."
        )
    )
    parser.add_argument(
        "--database-dir",
        type=Path,
        default=Path("artifacts/normalized"),
    )
    parser.add_argument("--workers", type=int, default=1)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--require-perfect", action="store_true")
    return parser


def _database_paths(database_dir: Path) -> dict[ProductFamily, Path]:
    return {
        family: database_dir / f"{family.value}.sqlite3"
        for family in (
            ProductFamily.OVERSEAS_ETP,
            ProductFamily.DOMESTIC_ETP,
            ProductFamily.BOND,
        )
    }


def main(argv: list[str] | None = None) -> int:
    arguments = build_parser().parse_args(argv)
    loaded = load_product_comparison_suite()
    database_paths = _database_paths(arguments.database_dir)
    for family, contract in loaded.suite.data.items():
        database = database_paths[family]
        manifest = Path(f"{database}.manifest.json")
        database_sha256 = sha256_file(database)
        manifest_sha256 = sha256_file(manifest)
        if database_sha256 != contract.database_sha256:
            raise RuntimeError(
                f"{family.value} database hash mismatch: "
                f"expected {contract.database_sha256}, got {database_sha256}"
            )
        if manifest_sha256 != contract.manifest_sha256:
            raise RuntimeError(
                f"{family.value} manifest hash mismatch: "
                f"expected {contract.manifest_sha256}, got {manifest_sha256}"
            )
        manifest_payload = json.loads(manifest.read_text(encoding="utf-8"))
        source_file_sha256 = manifest_payload.get("source_file_sha256")
        if source_file_sha256 != contract.source_file_sha256:
            raise RuntimeError(
                f"{family.value} source file hash mismatch: "
                f"expected {contract.source_file_sha256}, got {source_file_sha256}"
            )

    runner = ProductComparisonEvaluationRunner(database_paths)
    results = runner.run(loaded.suite.cases, arguments.workers)
    report = build_product_comparison_report(
        loaded,
        results,
        workers=arguments.workers,
        cache_stats=runner.cache_stats,
        identity_cache_stats=runner.identity_cache_stats,
    )
    output = arguments.output or Path("artifacts/evaluation/product-compare-deterministic-all.json")
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(f"{report.model_dump_json(indent=2)}\n", encoding="utf-8")
    print(
        json.dumps(
            {
                "provider": report.provider,
                "suite_id": report.suite_id,
                "suite_sha256": report.suite_sha256,
                "identity_cache": report.identity_cache.model_dump(mode="json"),
                "record_cache": report.record_cache.model_dump(mode="json"),
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
