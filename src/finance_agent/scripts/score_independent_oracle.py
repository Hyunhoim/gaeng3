from __future__ import annotations

import argparse
import hashlib
import json
from collections import Counter, defaultdict
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _load_jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def _physical_product_id(product: dict[str, Any]) -> str:
    for field in product.get("fields", []):
        if field.get("canonical_field") == "product_id":
            value = field.get("normalized_value")
            if value is not None:
                return str(value)
    for field in product.get("fields", []):
        source_key = field.get("source_key") or {}
        dataset = field.get("source_dataset")
        if dataset == "bond" and {
            "pd_no",
            "pd_exg_mkt",
            "info_base_dt",
            "info_seq",
        }.issubset(source_key):
            return ":".join(
                str(source_key[name])
                for name in ("pd_no", "pd_exg_mkt", "info_base_dt", "info_seq")
            )
        if dataset == "overseas_etp" and {
            "pd_exg_mkt_cd",
            "pd_itm_no",
        }.issubset(source_key):
            return f"{source_key['pd_exg_mkt_cd']}:{source_key['pd_itm_no']}"
    return str(product["product_id"])


def _product_key(product: dict[str, Any], *, cross_family: bool) -> str:
    physical_id = _physical_product_id(product)
    if not cross_family:
        return physical_id
    datasets = {
        str(field.get("source_dataset"))
        for field in product.get("fields", [])
        if field.get("source_dataset")
    }
    if len(datasets) != 1:
        raise ValueError(f"cross-family product has ambiguous dataset: {product!r}")
    return f"{datasets.pop()}|{physical_id}"


def _expected_products(expected: dict[str, Any]) -> tuple[list[str], bool]:
    kind = expected.get("kind")
    if kind == "ordered_products":
        return [str(value) for value in expected["ordered_product_ids"]], False
    if kind == "ordered_cross_family_products":
        return [str(value) for value in expected["ordered_product_keys"]], True
    return [], False


def _percent_metric_key(field: str) -> str:
    if field.endswith("_pct"):
        return f"avg_{field[:-4]}_micro_pct"
    if field == "duration_years":
        return "avg_duration_micro_years"
    return f"avg_{field}"


def _actual_aggregate_atoms(result: dict[str, Any]) -> list[dict[str, Any]]:
    atoms: dict[tuple[tuple[str, str], ...], dict[str, Any]] = {}
    order: list[tuple[tuple[str, str], ...]] = []
    for item in result.get("aggregates", []):
        groups = dict(item.get("group_values") or {})
        if not groups and len(result.get("family_searches", [])) > 1:
            groups["family"] = str(item.get("source_dataset"))
        key = tuple(sorted((str(name), str(value)) for name, value in groups.items()))
        if key not in atoms:
            atoms[key] = dict(groups)
            order.append(key)
        atom = atoms[key]
        function = item.get("function")
        field = str(item.get("field"))
        if function == "count" and field in {"product_id", "ticker", "isin"}:
            atom["product_count"] = item.get("value")
        elif function == "count":
            atom["available_count"] = item.get("valid_count", item.get("value"))
        elif function == "avg":
            value = item.get("value")
            if value is not None:
                multiplier = Decimal("1000000") if (
                    field.endswith("_pct") or field == "duration_years"
                ) else Decimal("1")
                atom[_percent_metric_key(field)] = Decimal(str(value)) * multiplier
            atom.setdefault("available_count", item.get("valid_count"))
        else:
            atom[f"{function}_{field}"] = item.get("value")
    return [atoms[key] for key in order]


def _equal_scalar(left: Any, right: Any) -> bool:
    try:
        left_decimal = Decimal(str(left))
        right_decimal = Decimal(str(right))
    except (InvalidOperation, ValueError):
        return left == right
    tolerance = max(Decimal("0.0001"), abs(right_decimal) * Decimal("0.000000001"))
    return abs(left_decimal - right_decimal) <= tolerance


def _atoms_exact(actual: list[dict[str, Any]], expected: list[dict[str, Any]]) -> bool:
    if len(actual) != len(expected):
        return False
    return all(
        set(left) == set(right)
        and all(_equal_scalar(left[key], right[key]) for key in right)
        for left, right in zip(actual, expected, strict=True)
    )


def score(predictions: Path, oracle: Path) -> dict[str, Any]:
    rows = {row["id"]: row for row in _load_jsonl(predictions)}
    gold = _load_json(oracle)
    cases: list[dict[str, Any]] = []
    product_tp = product_fp = product_fn = 0
    status_counts: Counter[str] = Counter()
    by_bucket: dict[str, Counter[str]] = defaultdict(Counter)
    for expected_case in gold["oracles"]:
        case_id = expected_case["id"]
        row = rows[case_id]
        result = row.get("result")
        actual_status = "EXCEPTION" if result is None else str(result["status"]).upper()
        actual_disposition = {
            "EXECUTED": "EXECUTE",
            "CLARIFY": "CLARIFY",
            "UNSUPPORTED": "UNSUPPORTED",
        }.get(actual_status, actual_status)
        expected_disposition = expected_case["disposition"]
        status_exact = actual_disposition == expected_disposition
        kind = (expected_case.get("expected") or {}).get("kind")
        outcome_exact: bool | None = None
        expected_products, cross_family = _expected_products(expected_case.get("expected") or {})
        actual_products: list[str] = []
        actual_atoms: list[dict[str, Any]] = []
        if kind in {"ordered_products", "ordered_cross_family_products"}:
            if result is not None:
                actual_products = [
                    _product_key(product, cross_family=cross_family)
                    for product in result.get("products", [])
                ]
            expected_set = set(expected_products)
            actual_set = set(actual_products)
            product_tp += len(expected_set & actual_set)
            product_fp += len(actual_set - expected_set)
            product_fn += len(expected_set - actual_set)
            outcome_exact = status_exact and actual_products == expected_products
        elif kind == "aggregate_atoms":
            if result is not None:
                actual_atoms = _actual_aggregate_atoms(result)
            outcome_exact = status_exact and _atoms_exact(
                actual_atoms,
                expected_case["expected"]["atoms"],
            )
        elif expected_disposition in {"CLARIFY", "UNSUPPORTED"}:
            outcome_exact = status_exact
        status_counts["total"] += 1
        status_counts["exact"] += int(status_exact)
        by_bucket[expected_case["bucket"]]["total"] += 1
        by_bucket[expected_case["bucket"]]["status_exact"] += int(status_exact)
        by_bucket[expected_case["bucket"]]["outcome_applicable"] += int(
            outcome_exact is not None
        )
        by_bucket[expected_case["bucket"]]["outcome_exact"] += int(outcome_exact is True)
        cases.append(
            {
                "id": case_id,
                "bucket": expected_case["bucket"],
                "intent": expected_case["intent"],
                "kind": kind,
                "expected_disposition": expected_disposition,
                "actual_disposition": actual_disposition,
                "status_exact": status_exact,
                "outcome_exact": outcome_exact,
                "expected_products": expected_products,
                "actual_products": actual_products,
                "expected_atoms": (expected_case.get("expected") or {}).get("atoms", []),
                "actual_atoms": actual_atoms,
                "reason_code": None if result is None else result["decision"]["reason_code"],
            }
        )
    precision = product_tp / (product_tp + product_fp) if product_tp + product_fp else 0.0
    recall = product_tp / (product_tp + product_fn) if product_tp + product_fn else 0.0
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
    outcome_cases = [item for item in cases if item["outcome_exact"] is not None]
    return {
        "schema_version": "1.0",
        "prediction_file_sha256": _sha256(predictions),
        "oracle_file_sha256": _sha256(oracle),
        "case_count": len(cases),
        "status_accuracy": status_counts["exact"] / status_counts["total"],
        "outcome_exact_accuracy": sum(
            item["outcome_exact"] is True for item in outcome_cases
        )
        / len(outcome_cases),
        "product_micro_precision": precision,
        "product_micro_recall": recall,
        "product_micro_f1": f1,
        "by_bucket": {
            bucket: {
                "total": counts["total"],
                "status_accuracy": counts["status_exact"] / counts["total"],
                "outcome_exact_accuracy": (
                    counts["outcome_exact"] / counts["outcome_applicable"]
                    if counts["outcome_applicable"]
                    else None
                ),
            }
            for bucket, counts in sorted(by_bucket.items())
        },
        "failure_ids": [item["id"] for item in cases if item["outcome_exact"] is False],
        "cases": cases,
        "notes": [
            "Direct Core predictions cannot fairly score NOT_FOUND/CANNOT_VERIFY public API statuses.",
            "Product IDs are compared using field-level physical IDs when available.",
        ],
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--predictions", type=Path, required=True)
    parser.add_argument("--oracle", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    report = score(args.predictions.resolve(), args.oracle.resolve())
    encoded = json.dumps(report, ensure_ascii=False, indent=2, default=str) + "\n"
    if args.output is None:
        print(encoded, end="")
    else:
        args.output.write_text(encoded, encoding="utf-8")


if __name__ == "__main__":
    main()
