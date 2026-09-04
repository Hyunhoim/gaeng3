from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import sqlite3
from collections import Counter
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from finance_agent_core.audit.xlsx import XlsxStream

FAMILY_FILES = {
    "bond": ("prbd01n001_data.xlsx", "bond.sqlite3", "bond_products", "pd_no"),
    "domestic_etp": (
        "pref01n001_data.xlsx",
        "domestic_etp.sqlite3",
        "domestic_etp_products",
        "pd_itm_no",
    ),
    "overseas_etp": (
        "pref02n001_data.xlsx",
        "overseas_etp.sqlite3",
        "overseas_etp_products",
        "pd_itm_no",
    ),
    "fund": ("prfd01n001_data.xlsx", "fund.sqlite3", "fund_products", "itm_no"),
}
EXPECTED_IDS = {f"P4-{index:04d}" for index in range(1, 1001)}
ZERO = Decimal(0)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _decimal(value: Any) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, str) and not value.strip():
        return None
    try:
        return Decimal(str(value).strip())
    except (InvalidOperation, ValueError):
        return None


def _normalized(value: Any) -> Any:
    if isinstance(value, dt.datetime):
        return value.strftime("%Y%m%d")
    if isinstance(value, dt.date):
        return value.strftime("%Y%m%d")
    number = _decimal(value)
    if number is not None:
        return number.normalize()
    if isinstance(value, str):
        return " ".join(value.split()).casefold()
    return value


def _same(left: Any, right: Any) -> bool:
    left_number = _decimal(left)
    right_number = _decimal(right)
    if left_number is not None and right_number is not None:
        return abs(left_number - right_number) <= Decimal("0.00000001")
    return _normalized(left) == _normalized(right)


def _metric_fields(metric: dict[str, Any] | None) -> tuple[str, ...]:
    if not metric:
        return ()
    if metric.get("kind") == "field":
        return (str(metric["field"]).casefold(),)
    return tuple(str(field).casefold() for field in metric.get("fields", ()))


def _metric_value(source: dict[str, Any], metric: dict[str, Any] | None) -> Decimal | None:
    fields = _metric_fields(metric)
    if not fields:
        return None
    values = [_decimal(source.get(field)) for field in fields]
    if any(value is None or value == ZERO for value in values):
        return None
    numbers = [value for value in values if value is not None]
    kind = metric["kind"]
    if kind == "field":
        return numbers[0]
    if kind == "add":
        return sum(numbers, ZERO)
    if kind == "subtract":
        return numbers[0] - numbers[1]
    if kind == "ratio":
        return numbers[0] / numbers[1] * Decimal(str(metric.get("scale", 1)))
    if kind == "relative_change":
        return (numbers[0] - numbers[1]) / abs(numbers[1]) * Decimal(
            str(metric.get("scale", 1))
        )
    raise ValueError(f"unsupported metric kind: {kind}")


def _matches_filter(source: dict[str, Any], item: dict[str, Any]) -> bool:
    value = source.get(str(item["field"]).casefold())
    operator = item["operator"]
    expected = item.get("value")
    if operator == "eq":
        return _same(value, expected)
    if operator == "in":
        return any(_same(value, member) for member in expected)
    actual_number = _decimal(value)
    expected_number = _decimal(expected)
    if actual_number is None or actual_number == ZERO or expected_number is None:
        return False
    if operator == "gt":
        return actual_number > expected_number
    if operator == "gte":
        return actual_number >= expected_number
    if operator == "lte":
        return actual_number <= expected_number
    raise ValueError(f"unsupported filter operator: {operator}")


def _load_database_rows(database_dir: Path) -> dict[str, list[dict[str, Any]]]:
    result: dict[str, list[dict[str, Any]]] = {}
    for family, (_, database_name, table, identity_field) in FAMILY_FILES.items():
        path = database_dir / database_name
        connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            rows: list[dict[str, Any]] = []
            for product_id, source_row, source_values_json in connection.execute(
                f"SELECT product_id, source_row, source_values_json FROM {table} "
                "WHERE is_quarantined = 0 ORDER BY source_row"
            ):
                source = {
                    str(key).casefold(): value
                    for key, value in json.loads(source_values_json).items()
                }
                identity = source.get(identity_field)
                rows.append(
                    {
                        "internal_product_id": product_id,
                        "product_id": str(identity) if identity is not None else "",
                        "source_row": source_row,
                        "source": source,
                    }
                )
            result[family] = rows
        finally:
            connection.close()
    return result


def _load_xlsx_rows(
    source_dir: Path,
    required_fields: dict[str, set[str]],
    valid_source_rows: dict[str, set[int]],
) -> dict[str, list[dict[str, Any]]]:
    result: dict[str, list[dict[str, Any]]] = {}
    for family, (workbook_name, _, _, _) in FAMILY_FILES.items():
        selected: list[dict[str, Any]] = []
        with XlsxStream(source_dir / workbook_name) as workbook:
            iterator = workbook.iter_rows()
            header_row_number, header_cells = next(iterator)
            if header_row_number != 1:
                raise RuntimeError(f"{family} workbook header row differs")
            headers = {
                index: str(value).strip().casefold()
                for index, value in header_cells.items()
                if value is not None
            }
            selected_indices = {
                index: header
                for index, header in headers.items()
                if header in required_fields[family]
            }
            identity_field = FAMILY_FILES[family][3]
            for row_number, cells in iterator:
                if row_number not in valid_source_rows[family]:
                    continue
                source = {
                    selected_indices[index]: value
                    for index, value in cells.items()
                    if index in selected_indices
                }
                identity = source.get(identity_field)
                selected.append(
                    {
                        "product_id": str(identity) if identity is not None else "",
                        "source_row": row_number,
                        "source": source,
                    }
                )
        result[family] = selected
    return result


def _sort_rows(
    rows: list[dict[str, Any]],
    metric: dict[str, Any],
    direction: str,
) -> list[dict[str, Any]]:
    reverse = direction == "desc"
    # Stable two-step ordering preserves product ID ascending for equal metrics.
    ordered = sorted(rows, key=lambda row: row["product_id"])
    return sorted(
        ordered,
        key=lambda row: _metric_value(row["source"], metric),
        reverse=reverse,
    )


def _recalculate(
    rows: list[dict[str, Any]],
    spec: dict[str, Any],
) -> tuple[int, list[dict[str, Any]], dict[str, Decimal | None]]:
    metric = spec.get("metric")
    shortlist = spec.get("shortlist")
    ranking_metrics = [value for value in (metric, shortlist and shortlist.get("metric")) if value]
    excluded = {str(value) for value in spec.get("exclude_product_ids", ())}
    candidates: list[dict[str, Any]] = []
    for row in rows:
        if row["product_id"] in excluded:
            continue
        if not all(_matches_filter(row["source"], item) for item in spec.get("filters", ())):
            continue
        if any(_metric_value(row["source"], item) is None for item in ranking_metrics):
            continue
        candidates.append(row)

    # The contract counts unique public product IDs. Family filters make bond
    # venue duplicates deterministic before this boundary.
    unique: dict[str, dict[str, Any]] = {}
    for row in candidates:
        unique.setdefault(row["product_id"], row)
    candidates = list(unique.values())
    candidate_count = len(candidates)
    if shortlist:
        candidates = _sort_rows(candidates, shortlist["metric"], shortlist["direction"])[
            : int(shortlist["limit"])
        ]
    if metric:
        candidates = _sort_rows(candidates, metric, spec["direction"])
    limit = int(spec["limit"])
    result = candidates[:limit]
    ranking_values = {
        row["product_id"]: _metric_value(row["source"], metric) for row in result
    }
    return candidate_count, result, ranking_values


def audit(
    gold: dict[str, Any],
    source_dir: Path,
    database_dir: Path,
    external_snapshot_dir: Path | None = None,
) -> dict[str, Any]:
    issues: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []

    def issue(code: str, *, case_id: str | None = None, detail: str) -> None:
        payload = {"code": code, "detail": detail}
        if case_id is not None:
            payload["case_id"] = case_id
        issues.append(payload)

    def warn(code: str, *, detail: str) -> None:
        warnings.append({"code": code, "detail": detail})

    cases = gold.get("cases", ())
    ids = [str(case.get("id")) for case in cases]
    if gold.get("suite_id") != "finance-agent-independent-holdout-phase4-v1-1000":
        issue("suite_id_mismatch", detail="Phase 4 suite ID differs")
    if gold.get("case_count") != 1000 or len(cases) != 1000:
        issue("case_count_mismatch", detail="Gold case count differs from 1000")
    if set(ids) != EXPECTED_IDS or len(ids) != len(set(ids)):
        issue("case_id_contract_mismatch", detail="P4 IDs are missing or duplicated")

    source_file_checks = []
    for entry in gold.get("source_files", ()):
        path = source_dir / str(entry["filename"])
        present = path.is_file()
        actual_sha = _sha256(path) if present else None
        actual_size = path.stat().st_size if present else None
        exact = (
            present
            and actual_sha == entry.get("sha256")
            and actual_size == entry.get("size_bytes")
        )
        source_file_checks.append({"family": entry["family"], "role": entry["role"], "exact": exact})
        if not exact:
            issue("source_workbook_mismatch", detail=f"{entry['family']} {entry['role']} differs")

    serialized_cases = json.dumps(cases, ensure_ascii=False).casefold()
    if "buyable_quantity" in serialized_cases:
        issue("invalid_buyable_quantity_use", detail="Gold case contract uses invalid bond field")
    overseas_1y_fields = {
        str(value).casefold()
        for case in cases
        for spec in case.get("calculation_spec", ())
        if spec.get("family") == "overseas_etp"
        for metric in [spec.get("metric"), (spec.get("shortlist") or {}).get("metric")]
        if metric
        for value in _metric_fields(metric)
        if "1y" in str(value).casefold() or "yr1" in str(value).casefold()
    }
    if overseas_1y_fields:
        issue("fabricated_overseas_one_year", detail="Overseas 1Y field appears in Gold")

    database_rows = _load_database_rows(database_dir)
    required_fields: dict[str, set[str]] = {
        family: {identity_field}
        for family, (_, _, _, identity_field) in FAMILY_FILES.items()
    }
    for case in cases:
        for spec, subquery in zip(
            case.get("calculation_spec", ()), case.get("expected_subqueries", ())
        ):
            family = str(subquery["product_family"])
            required_fields[family].update(
                str(item["field"]).casefold() for item in spec.get("filters", ())
            )
            required_fields[family].update(_metric_fields(spec.get("metric")))
            required_fields[family].update(
                _metric_fields((spec.get("shortlist") or {}).get("metric"))
            )
            for record in subquery.get("records", ()):
                required_fields[family].update(
                    str(field).casefold() for field in record.get("source_values", {})
                )
    valid_source_rows = {
        family: {int(row["source_row"]) for row in rows}
        for family, rows in database_rows.items()
    }
    xlsx_rows = _load_xlsx_rows(source_dir, required_fields, valid_source_rows)

    source_row_checks = source_value_checks = identity_checks = 0
    source_value_mismatches = 0
    candidate_checks = topn_checks = ranking_value_checks = 0
    candidate_mismatches = topn_mismatches = ranking_value_mismatches = 0
    total_count_checks = total_count_mismatches = 0
    calculation_by_case = {
        case["id"]: case.get("calculation_spec", ()) for case in cases
    }
    for case in cases:
        expected_total = 0
        specs = calculation_by_case[case["id"]]
        subqueries = case.get("expected_subqueries", ())
        if len(specs) != len(subqueries):
            issue("subquery_spec_count_mismatch", case_id=case["id"], detail="spec count differs")
            continue
        for spec, subquery in zip(specs, subqueries, strict=True):
            family = str(subquery["product_family"])
            if family != spec.get("family"):
                issue("subquery_family_mismatch", case_id=case["id"], detail="family differs")
                continue
            by_source_row = {row["source_row"]: row for row in xlsx_rows[family]}
            for record in subquery.get("records", ()):
                source_row_checks += 1
                row_number = int(record["source_row"])
                db_row = by_source_row.get(row_number)
                xlsx_row = by_source_row.get(row_number)
                if db_row is None or xlsx_row is None:
                    issue("missing_source_row", case_id=case["id"], detail=f"{family} row missing")
                    continue
                identity_checks += 1
                if xlsx_row["product_id"] != str(record["product_id"]):
                    issue("product_identity_mismatch", case_id=case["id"], detail=family)
                for field, expected_value in record.get("source_values", {}).items():
                    source_value_checks += 1
                    actual_value = xlsx_row["source"].get(str(field).casefold())
                    if not _same(actual_value, expected_value):
                        source_value_mismatches += 1
                        if source_value_mismatches <= 50:
                            issue(
                                "source_value_mismatch",
                                case_id=case["id"],
                                detail=f"{family}:{field}:row={row_number}",
                            )

            if case.get("expected_disposition") != "execute":
                continue
            candidate_count, recalculated, ranking_values = _recalculate(
                xlsx_rows[family], spec
            )
            candidate_checks += 1
            expected_total += candidate_count
            if candidate_count != int(subquery["candidate_count"]):
                candidate_mismatches += 1
                issue("candidate_count_mismatch", case_id=case["id"], detail=family)
            recalculated_ids = [row["product_id"] for row in recalculated]
            expected_ids = [str(value) for value in subquery.get("top_product_ids", ())]
            topn_checks += 1
            if recalculated_ids != expected_ids:
                topn_mismatches += 1
                issue("topn_mismatch", case_id=case["id"], detail=family)
            for record in subquery.get("records", ()):
                expected_ranking = record.get("ranking_value")
                if expected_ranking is None:
                    continue
                ranking_value_checks += 1
                actual_ranking = ranking_values.get(str(record["product_id"]))
                expected_decimal = _decimal(expected_ranking)
                if actual_ranking is None or expected_decimal is None or abs(
                    actual_ranking - expected_decimal
                ) > Decimal("0.00000001"):
                    ranking_value_mismatches += 1
                    issue("ranking_value_mismatch", case_id=case["id"], detail=family)
        if case.get("expected_disposition") == "execute":
            total_count_checks += 1
            if expected_total != int(case.get("expected_candidate_count_total", -1)):
                total_count_mismatches += 1
                issue("candidate_total_mismatch", case_id=case["id"], detail="case total differs")

    catalog = gold.get("external_evidence_catalog", {})
    raw_documents = catalog.get("documents", ())
    documents = list(raw_documents.values()) if isinstance(raw_documents, dict) else raw_documents
    document_ids = [str(item.get("id")) for item in documents]
    if len(document_ids) != len(set(document_ids)):
        issue("duplicate_external_document_id", detail="catalog document IDs duplicate")
    required_evidence = {
        str(value) for case in cases for value in case.get("required_evidence_ids", ())
    }
    missing_evidence_ids = sorted(required_evidence - set(document_ids))
    if missing_evidence_ids:
        issue("missing_external_evidence_id", detail="required evidence ID absent")
    allowed_domains = {
        "assets.globalxetfs.com",
        "www.tigeretf.com",
        "investments.miraeasset.com",
        "www.samsungfund.com",
    }
    for document in documents:
        domain = (urlparse(str(document.get("url"))).hostname or "").casefold()
        if domain not in allowed_domains:
            issue("unapproved_external_domain", detail=domain)
        publication_date = document.get("publication_date")
        if publication_date and str(publication_date) > str(catalog.get("cutoff")):
            issue("external_document_after_cutoff", detail=str(document.get("id")))
    snapshot_present = 0
    snapshot_hash_exact = 0
    for document in documents:
        path = Path(str(document.get("local_snapshot_path", "")))
        if not path.is_file() and external_snapshot_dir is not None:
            path = external_snapshot_dir / f"phase4-gold-{document.get('id')}.pdf"
        if path.is_file():
            snapshot_present += 1
            if _sha256(path) == document.get("sha256") and path.stat().st_size == document.get("size"):
                snapshot_hash_exact += 1
    if snapshot_present != len(documents):
        warn(
            "external_snapshot_not_portable",
            detail=(
                f"{len(documents) - snapshot_present}/{len(documents)} snapshots are unavailable "
                "at the Gold paths; byte-level external evidence cannot be independently repeated"
            ),
        )

    all_products = {
        (family, row["product_id"])
        for family, rows in database_rows.items()
        for row in rows
    }
    raw_catalog_products = catalog.get("products", ())
    catalog_products = (
        list(raw_catalog_products.values())
        if isinstance(raw_catalog_products, dict)
        else raw_catalog_products
    )
    catalog_product_missing = [
        (str(item.get("family")), str(item.get("product_id")))
        for item in catalog_products
        if (str(item.get("family")), str(item.get("product_id"))) not in all_products
    ]
    if catalog_product_missing:
        issue("external_catalog_product_missing_from_db", detail="catalog/DB intersection differs")
    relation_rows = [
        row
        for case in cases
        for row in (case.get("external_task") or {}).get("verified_relation_rows", ())
    ]
    relation_product_missing = [
        row
        for row in relation_rows
        if (str(row.get("family")), str(row.get("product_id"))) not in all_products
    ]
    if relation_product_missing:
        issue("external_relation_product_missing_from_db", detail="relation/DB intersection differs")

    issue_counts = Counter(item["code"] for item in issues)
    warning_counts = Counter(item["code"] for item in warnings)
    return {
        "schema_version": "1.0",
        "artifact_kind": "phase4_gold_independent_recalculation_audit",
        "status": "failed" if issues else ("passed_with_limitations" if warnings else "passed"),
        "summary": {
            "case_count": len(cases),
            "source_workbooks_exact": sum(item["exact"] for item in source_file_checks),
            "source_workbooks_total": len(source_file_checks),
            "source_rows_checked": source_row_checks,
            "product_identities_checked": identity_checks,
            "source_values_checked": source_value_checks,
            "source_value_mismatches": source_value_mismatches,
            "candidate_counts_checked": candidate_checks,
            "candidate_count_mismatches": candidate_mismatches,
            "topn_checked": topn_checks,
            "topn_mismatches": topn_mismatches,
            "ranking_values_checked": ranking_value_checks,
            "ranking_value_mismatches": ranking_value_mismatches,
            "case_candidate_totals_checked": total_count_checks,
            "case_candidate_total_mismatches": total_count_mismatches,
            "external_documents": len(documents),
            "external_snapshots_present": snapshot_present,
            "external_snapshot_hashes_exact": snapshot_hash_exact,
            "external_catalog_products": len(catalog_products),
            "external_relation_rows_checked": len(relation_rows),
            "issue_count": len(issues),
            "warning_count": len(warnings),
        },
        "issue_counts": dict(sorted(issue_counts.items())),
        "warning_counts": dict(sorted(warning_counts.items())),
        "issues": issues[:200],
        "warnings": warnings,
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Independently recalculate Phase 4 private Gold.")
    parser.add_argument("--gold", type=Path, required=True)
    parser.add_argument("--gold-sha256", required=True)
    parser.add_argument("--questions", type=Path, required=True)
    parser.add_argument("--questions-sha256", required=True)
    parser.add_argument("--source-dir", type=Path, required=True)
    parser.add_argument("--database-dir", type=Path, required=True)
    parser.add_argument("--external-snapshot-dir", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    if _sha256(arguments.gold) != arguments.gold_sha256:
        raise SystemExit("Gold SHA-256 differs")
    if _sha256(arguments.questions) != arguments.questions_sha256:
        raise SystemExit("question-only SHA-256 differs")
    gold = json.loads(arguments.gold.read_bytes())
    if gold.get("questions_sha256") != arguments.questions_sha256:
        raise SystemExit("Gold/question-only linkage differs")
    result = audit(
        gold,
        arguments.source_dir,
        arguments.database_dir,
        arguments.external_snapshot_dir,
    )
    result["gold_sha256"] = arguments.gold_sha256
    result["questions_sha256"] = arguments.questions_sha256
    encoded = (json.dumps(result, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    descriptor = os.open(
        arguments.output,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0),
        0o400,
    )
    try:
        os.write(descriptor, encoded)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    print(json.dumps(result["summary"], ensure_ascii=False, sort_keys=True))
    return 0 if not result["issues"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
