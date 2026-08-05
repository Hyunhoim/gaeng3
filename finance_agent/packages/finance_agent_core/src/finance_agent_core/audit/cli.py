from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import tempfile
from collections.abc import Sequence
from importlib import resources
from pathlib import Path
from typing import Any

from finance_agent_core.audit.pipeline import audit_all
from finance_agent_core.audit.registry import DATASET_BY_NAME, DATASET_SPECS
from finance_agent_core.audit.verification import verify_expectations


def _date(value: str) -> dt.date:
    try:
        return dt.date.fromisoformat(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("date must use YYYY-MM-DD") from error


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Audit the four provided financial-product XLSX datasets."
    )
    parser.add_argument(
        "--data-dir",
        type=Path,
        default=os.getenv("PRODUCT_DATA_DIR"),
        required=os.getenv("PRODUCT_DATA_DIR") is None,
        help="Directory containing the eight datarows/schema XLSX files.",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("artifacts/data-audit"),
        help="Generated JSON destination; must not be inside --data-dir.",
    )
    parser.add_argument(
        "--snapshot-date",
        type=_date,
        default=dt.date(2026, 7, 11),
    )
    parser.add_argument(
        "--dataset",
        choices=sorted(DATASET_BY_NAME),
        action="append",
        help="Audit selected dataset(s); omit to audit all four.",
    )
    parser.add_argument(
        "--expectations",
        type=Path,
        help="Override the packaged regression expectations JSON.",
    )
    parser.add_argument(
        "--no-verify",
        action="store_true",
        help="Generate profiles without enforcing regression expectations.",
    )
    return parser


def _write_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
        delete=False,
    ) as temporary:
        json.dump(value, temporary, ensure_ascii=False, indent=2, sort_keys=True)
        temporary.write("\n")
        temporary_path = Path(temporary.name)
    temporary_path.replace(path)


def _load_expectations(path: Path | None) -> dict[str, Any]:
    if path is not None:
        return json.loads(path.read_text(encoding="utf-8"))
    packaged = resources.files("finance_agent_core.audit").joinpath("expectations.json")
    return json.loads(packaged.read_text(encoding="utf-8"))


def _filter_expectations(expectations: dict[str, Any], dataset_names: set[str]) -> dict[str, Any]:
    prefixes = tuple(f"datasets.{name}." for name in sorted(dataset_names))
    return {
        **expectations,
        "checks": [
            check for check in expectations.get("checks", []) if check["path"].startswith(prefixes)
        ],
    }


def _validate_paths(data_dir: Path, output_dir: Path) -> tuple[Path, Path]:
    resolved_data = data_dir.expanduser().resolve()
    resolved_output = output_dir.expanduser().resolve()
    if not resolved_data.is_dir():
        raise ValueError(f"Data directory does not exist: {resolved_data}")
    if resolved_output == resolved_data or resolved_data in resolved_output.parents:
        raise ValueError("Output directory must not be inside the raw-data directory")
    return resolved_data, resolved_output


def run(arguments: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(arguments)
    try:
        data_dir, output_dir = _validate_paths(args.data_dir, args.output_dir)
        selected_names = list(dict.fromkeys(args.dataset or []))
        selected = (
            [DATASET_BY_NAME[name] for name in selected_names]
            if selected_names
            else list(DATASET_SPECS)
        )
        report = audit_all(data_dir, selected, args.snapshot_date)
        if not args.no_verify:
            expectations = _load_expectations(args.expectations)
            if selected_names:
                expectations = _filter_expectations(expectations, dataset_names=set(selected_names))
            report["verification"] = verify_expectations(report, expectations)

        for name, dataset in report["datasets"].items():
            _write_json(output_dir / f"audit_{name}.json", dataset)
            print(
                f"{name}: rows={dataset['structure']['data_rows']} "
                f"columns={dataset['structure']['columns']}"
            )
        _write_json(output_dir / "finance_data_audit.json", report)

        verification = report.get("verification")
        if verification is not None:
            print(
                f"verification: {verification['passed_count']}/{verification['total_count']} passed"
            )
            if not verification["passed"]:
                for result in verification["results"]:
                    if not result["passed"]:
                        print(
                            f"FAILED {result['path']}: "
                            f"expected={result['expected']!r} actual={result['actual']!r}"
                        )
                return 2
        print(f"output: {output_dir}")
        return 0
    except (OSError, ValueError) as error:
        print(f"audit error: {error}")
        return 2


def main() -> int:
    return run()
