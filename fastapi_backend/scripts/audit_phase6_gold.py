from __future__ import annotations

import argparse
import json
import os
import re
from collections import Counter
from decimal import Decimal
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from fastapi_backend.scripts.audit_phase4_gold import (
    FAMILY_FILES,
    ZERO,
    _decimal,
    _load_database_rows,
    _load_xlsx_rows,
    _matches_filter,
    _metric_fields,
    _metric_value,
    _same,
    _sha256,
    _sort_rows,
)

SUITE_ID = "finance-agent-generalization-phase6-v1-150"
EXPECTED_IDS = [f"P6-{index:03d}" for index in range(1, 151)]
ALLOWED_FAMILIES = frozenset(FAMILY_FILES)
ALLOWED_OPERATIONS = frozenset({"search", "detail", "compare", "aggregate"})
ALLOWED_DISPOSITIONS = frozenset({"execute", "clarify", "unsupported"})
OFFICIAL_GOLD_DOMAINS = frozenset(
    {
        "assets.globalxetfs.com",
        "investments.miraeasset.com",
        "www.ecopro.co.kr",
        "www.fsc.go.kr",
        "www.marketvector.com",
        "www.samsungfund.com",
        "www.tigeretf.com",
        "www.vaneck.com",
    }
)


def _candidate_rows(
    rows: list[dict[str, Any]], spec: dict[str, Any]
) -> tuple[int, list[dict[str, Any]], dict[str, Decimal | None], list[dict[str, Any]]]:
    metric = spec.get("metric")
    shortlist = spec.get("shortlist")
    ranking_metrics = [
        value
        for value in (metric, shortlist and shortlist.get("metric"))
        if value
    ]
    excluded = {str(value) for value in spec.get("exclude_product_ids", ())}
    candidates: list[dict[str, Any]] = []
    for row in rows:
        if row["product_id"] in excluded:
            continue
        if not all(
            _matches_filter(row["source"], item)
            for item in spec.get("filters", ())
        ):
            continue
        if any(
            _metric_value(row["source"], item) is None
            for item in ranking_metrics
        ):
            continue
        candidates.append(row)

    unique: dict[str, dict[str, Any]] = {}
    for row in candidates:
        unique.setdefault(row["product_id"], row)
    candidates = list(unique.values())
    candidate_count = len(candidates)
    ranked_candidates = candidates
    if shortlist:
        ranked_candidates = _sort_rows(
            ranked_candidates,
            shortlist["metric"],
            shortlist["direction"],
        )[: int(shortlist["limit"])]
    if metric:
        ranked_candidates = _sort_rows(
            ranked_candidates,
            metric,
            spec["direction"],
        )
    else:
        explicit_order = next(
            (
                [str(value) for value in item.get("value", ())]
                for item in spec.get("filters", ())
                if item.get("operator") == "in"
                and str(item.get("field")).casefold()
                == FAMILY_FILES[str(spec["family"])][3]
            ),
            None,
        )
        if explicit_order is not None:
            positions = {product_id: index for index, product_id in enumerate(explicit_order)}
            ranked_candidates = sorted(
                ranked_candidates,
                key=lambda row: positions.get(row["product_id"], len(positions)),
            )
    limit = spec.get("limit")
    result = [] if limit is None else ranked_candidates[: int(limit)]
    ranking_values = {
        row["product_id"]: _metric_value(row["source"], metric)
        for row in result
    }
    return candidate_count, result, ranking_values, candidates


def _aggregate_values(
    rows: list[dict[str, Any]], metric: dict[str, Any] | None
) -> dict[str, Decimal] | None:
    if metric is None:
        return None
    values = [
        value
        for row in rows
        if (value := _metric_value(row["source"], metric)) is not None
    ]
    if not values:
        return None
    total = sum(values, ZERO)
    minimum = min(values)
    maximum = max(values)
    return {
        "count": Decimal(len(values)),
        "mean": total / Decimal(len(values)),
        "min": minimum,
        "max": maximum,
        "range": maximum - minimum,
    }


def _documents(catalog: dict[str, Any]) -> list[dict[str, Any]]:
    raw = catalog.get("documents", ())
    return list(raw.values()) if isinstance(raw, dict) else list(raw)


def _products(catalog: dict[str, Any]) -> list[dict[str, Any]]:
    raw = catalog.get("products", ())
    return list(raw.values()) if isinstance(raw, dict) else list(raw)


def audit(
    gold: dict[str, Any],
    questions: dict[str, Any],
    source_dir: Path,
    database_dir: Path,
    snapshot_roots: tuple[Path, ...],
) -> dict[str, Any]:
    issues: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []

    def issue(code: str, detail: str, case_id: str | None = None) -> None:
        payload = {"code": code, "detail": detail}
        if case_id is not None:
            payload["case_id"] = case_id
        issues.append(payload)

    def warn(code: str, detail: str) -> None:
        warnings.append({"code": code, "detail": detail})

    cases = list(gold.get("cases", ()))
    ids = [str(case.get("id")) for case in cases]
    question_cases = list(questions.get("cases", ()))
    question_ids = [str(case.get("id")) for case in question_cases]
    if gold.get("suite_id") != SUITE_ID:
        issue("suite_id_mismatch", "Phase 6 suite ID differs")
    if gold.get("phase") != 6:
        issue("phase_mismatch", "Gold phase differs from 6")
    if gold.get("case_count") != 150 or len(cases) != 150:
        issue("case_count_mismatch", "Gold case count differs from 150")
    if ids != EXPECTED_IDS or len(ids) != len(set(ids)):
        issue("case_id_contract_mismatch", "P6 IDs are missing, duplicated, or unordered")
    if question_ids != EXPECTED_IDS or len(question_ids) != len(set(question_ids)):
        issue("question_id_contract_mismatch", "question-only IDs differ")
    if any(set(case) != {"id", "question"} for case in question_cases):
        issue("question_only_leak", "question-only case contains fields other than id/question")

    source_file_checks: list[dict[str, Any]] = []
    for entry in gold.get("source_files", ()):
        path = source_dir / str(entry["filename"])
        present = path.is_file()
        exact = (
            present
            and path.stat().st_size == int(entry["size_bytes"])
            and _sha256(path) == entry["sha256"]
        )
        source_file_checks.append(
            {"family": entry["family"], "role": entry["role"], "exact": exact}
        )
        if not exact:
            issue(
                "source_workbook_mismatch",
                f"{entry['family']} {entry['role']} differs",
            )

    used_filter_fields = {
        str(item.get("field")).casefold()
        for case in cases
        for spec in case.get("calculation_spec", ())
        for item in spec.get("filters", ())
    }
    used_metric_fields = {
        str(value).casefold()
        for case in cases
        for spec in case.get("calculation_spec", ())
        for metric in (spec.get("metric"), (spec.get("shortlist") or {}).get("metric"))
        if metric
        for value in _metric_fields(metric)
    }
    if "buyable_quantity" in used_filter_fields | used_metric_fields:
        issue("invalid_buyable_quantity_use", "Gold uses the invalid bond field")
    if any(case.get("expected_result_status") == "CANNOT_VERIFY" for case in cases):
        issue("cannot_verify_used_as_gold", "CANNOT_VERIFY is a runtime failure, not Gold")
    overseas_one_year = {
        str(value).casefold()
        for case in cases
        for spec in case.get("calculation_spec", ())
        if spec.get("family") == "overseas_etp"
        for metric in (spec.get("metric"), (spec.get("shortlist") or {}).get("metric"))
        if metric
        for value in _metric_fields(metric)
        if "1y" in str(value).casefold() or "yr1" in str(value).casefold()
    }
    if overseas_one_year:
        issue("fabricated_overseas_one_year", "Overseas ETP 1Y field appears")

    disposition_status = {
        "execute": {"SUCCESS", "NOT_FOUND"},
        "clarify": {"CLARIFY"},
        "unsupported": {None},
    }
    for case in cases:
        case_id = str(case.get("id"))
        disposition = case.get("expected_disposition")
        status = case.get("expected_result_status")
        families = list(case.get("expected_product_families", ()))
        if disposition not in ALLOWED_DISPOSITIONS:
            issue("invalid_disposition", str(disposition), case_id)
        elif status not in disposition_status[disposition]:
            issue("invalid_disposition_status_pair", f"{disposition}/{status}", case_id)
        if any(family not in ALLOWED_FAMILIES for family in families):
            issue("invalid_product_family", repr(families), case_id)
        if case.get("expected_operation") not in ALLOWED_OPERATIONS | {None}:
            issue("invalid_operation", str(case.get("expected_operation")), case_id)
        specs = list(case.get("calculation_spec", ()))
        subqueries = list(case.get("expected_subqueries", ()))
        if len(specs) != len(subqueries):
            issue("subquery_spec_count_mismatch", "spec/subquery count differs", case_id)
        if disposition == "execute" and status == "SUCCESS" and not subqueries:
            issue("success_without_subquery", "SUCCESS has no DB subquery", case_id)
        if disposition != "execute" and (specs or subqueries):
            issue("non_execute_has_query", "non-execute case contains a query", case_id)
        expected_total = case.get("expected_candidate_count_total")
        if expected_total is not None and (not isinstance(expected_total, int) or expected_total < 0):
            issue("invalid_candidate_total", repr(expected_total), case_id)
        flattened: list[tuple[str, str, int, int]] = []
        for group_index, subquery in enumerate(subqueries, start=1):
            family = str(subquery.get("product_family"))
            ids_in_group = [str(value) for value in subquery.get("top_product_ids", ())]
            if len(ids_in_group) != len(set(ids_in_group)):
                issue("duplicate_top_product", family, case_id)
            flattened.extend(
                (family, product_id, group_index, rank)
                for rank, product_id in enumerate(ids_in_group, start=1)
            )
            candidate_hash = subquery.get("candidate_id_set_sha256")
            if candidate_hash is not None and not re.fullmatch(r"[0-9a-f]{64}", str(candidate_hash)):
                issue("invalid_candidate_hash", family, case_id)
        declared = [
            (
                str(item.get("family")),
                str(item.get("product_id")),
                int(item.get("group_index", -1)),
                int(item.get("rank", -1)),
            )
            for item in case.get("expected_top_products", ())
        ]
        if flattened != declared:
            issue("flattened_top_products_mismatch", "case/subquery ranking differs", case_id)

    database_rows = _load_database_rows(database_dir)
    required_fields: dict[str, set[str]] = {
        family: {identity_field}
        for family, (_, _, _, identity_field) in FAMILY_FILES.items()
    }
    for case in cases:
        for spec in case.get("calculation_spec", ()):
            family = str(spec["family"])
            required_fields[family].update(
                str(item["field"]).casefold() for item in spec.get("filters", ())
            )
            required_fields[family].update(_metric_fields(spec.get("metric")))
            required_fields[family].update(
                _metric_fields((spec.get("shortlist") or {}).get("metric"))
            )
        for subquery in case.get("expected_subqueries", ()):
            family = str(subquery["product_family"])
            for record in subquery.get("records", ()):
                required_fields[family].update(
                    str(field).casefold() for field in record.get("source_values", {})
                )
    valid_source_rows = {
        family: {int(row["source_row"]) for row in rows}
        for family, rows in database_rows.items()
    }
    xlsx_rows = _load_xlsx_rows(source_dir, required_fields, valid_source_rows)
    xlsx_by_row = {
        family: {int(row["source_row"]): row for row in rows}
        for family, rows in xlsx_rows.items()
    }

    source_rows_checked = source_values_checked = source_value_mismatches = 0
    candidate_checks = candidate_mismatches = 0
    topn_checks = topn_mismatches = 0
    aggregate_checks = aggregate_mismatches = 0
    total_checks = total_mismatches = 0
    for case in cases:
        if case.get("expected_disposition") != "execute":
            continue
        case_id = str(case["id"])
        expected_total = 0
        for spec, subquery in zip(
            case.get("calculation_spec", ()),
            case.get("expected_subqueries", ()),
            strict=True,
        ):
            family = str(spec["family"])
            if family != subquery.get("product_family"):
                issue("subquery_family_mismatch", family, case_id)
                continue
            for record in subquery.get("records", ()):
                source_rows_checked += 1
                row = xlsx_by_row[family].get(int(record["source_row"]))
                if row is None:
                    issue("missing_source_row", family, case_id)
                    continue
                if row["product_id"] != str(record["product_id"]):
                    issue("product_identity_mismatch", family, case_id)
                for field, expected_value in record.get("source_values", {}).items():
                    source_values_checked += 1
                    actual_value = row["source"].get(str(field).casefold())
                    if not _same(actual_value, expected_value):
                        source_value_mismatches += 1
                        if source_value_mismatches <= 50:
                            issue(
                                "source_value_mismatch",
                                f"{family}:{field}:row={record['source_row']}",
                                case_id,
                            )
            candidate_count, recalculated, ranking_values, candidates = _candidate_rows(
                xlsx_rows[family], spec
            )
            expected_total += candidate_count
            candidate_checks += 1
            if candidate_count != int(subquery.get("candidate_count", -1)):
                candidate_mismatches += 1
                issue("candidate_count_mismatch", family, case_id)
            if spec.get("limit") is not None:
                topn_checks += 1
                actual_ids = [row["product_id"] for row in recalculated]
                expected_ids = [str(value) for value in subquery.get("top_product_ids", ())]
                if actual_ids != expected_ids:
                    topn_mismatches += 1
                    issue("topn_mismatch", family, case_id)
            for record in subquery.get("records", ()):
                expected_ranking = record.get("ranking_value")
                if expected_ranking is None:
                    continue
                actual_ranking = ranking_values.get(str(record["product_id"]))
                expected_number = _decimal(expected_ranking)
                if actual_ranking is None or expected_number is None or abs(
                    actual_ranking - expected_number
                ) > Decimal("0.00000001"):
                    issue("ranking_value_mismatch", family, case_id)
            expected_aggregates = subquery.get("aggregates")
            if expected_aggregates is not None:
                aggregate_checks += 1
                actual_aggregates = _aggregate_values(candidates, spec.get("metric"))
                mismatch = actual_aggregates is None
                if actual_aggregates is not None:
                    for key, expected_value in expected_aggregates.items():
                        actual_value = actual_aggregates.get(str(key))
                        expected_number = _decimal(expected_value)
                        if actual_value is None or expected_number is None or abs(
                            actual_value - expected_number
                        ) > Decimal("0.000001"):
                            mismatch = True
                if mismatch:
                    aggregate_mismatches += 1
                    issue("aggregate_mismatch", family, case_id)
        total_checks += 1
        if expected_total != int(case.get("expected_candidate_count_total", -1)):
            total_mismatches += 1
            issue("candidate_total_mismatch", "case total differs", case_id)

    all_products = {
        (family, row["product_id"])
        for family, rows in database_rows.items()
        for row in rows
    }
    relation_rows = [
        row
        for case in cases
        for row in case.get("expected_verified_relation_products", ())
    ]
    for row in relation_rows:
        key = (str(row.get("family")), str(row.get("product_id")))
        if key not in all_products:
            issue("external_relation_product_missing_from_db", repr(key))

    catalog = gold.get("external_evidence_catalog", {})
    documents = _documents(catalog)
    document_ids = [str(item.get("id")) for item in documents]
    if len(document_ids) != len(set(document_ids)):
        issue("duplicate_external_document_id", "catalog document IDs duplicate")
    required_evidence = {
        str(value) for case in cases for value in case.get("required_evidence_ids", ())
    }
    missing_evidence = sorted(required_evidence - set(document_ids))
    if missing_evidence:
        issue("missing_external_evidence_id", repr(missing_evidence))
    cutoff = str(catalog.get("cutoff"))
    for document in documents:
        url = document.get("url")
        domain = (urlparse(str(url)).hostname or "").casefold() if url else ""
        if domain and domain not in OFFICIAL_GOLD_DOMAINS:
            issue("non_official_external_domain", domain)
        publication_date = document.get("publication_date")
        if publication_date and str(publication_date) > cutoff:
            issue("external_document_after_cutoff", str(document.get("id")))
        if not re.fullmatch(r"[0-9a-f]{64}", str(document.get("sha256", ""))):
            issue("invalid_external_document_hash", str(document.get("id")))
    catalog_products = _products(catalog)
    for item in catalog_products:
        key = (str(item.get("family")), str(item.get("product_id")))
        if key not in all_products:
            issue("external_catalog_product_missing_from_db", repr(key))

    snapshot_hashes: set[str] = set()
    for root in snapshot_roots:
        if not root.exists():
            continue
        for path in root.rglob("*"):
            if path.is_file():
                try:
                    snapshot_hashes.add(_sha256(path))
                except OSError:
                    continue
    snapshot_exact = sum(
        str(document.get("sha256")) in snapshot_hashes for document in documents
    )
    snapshot_missing_ids = [
        str(document.get("id"))
        for document in documents
        if str(document.get("sha256")) not in snapshot_hashes
    ]
    if snapshot_exact != len(documents):
        warn(
            "external_snapshot_not_fully_local",
            (
                f"{len(documents) - snapshot_exact}/{len(documents)} Gold evidence snapshots "
                f"are not locally reproducible: {', '.join(snapshot_missing_ids)}"
            ),
        )

    issue_counts = Counter(item["code"] for item in issues)
    warning_counts = Counter(item["code"] for item in warnings)
    return {
        "schema_version": "1.0",
        "artifact_kind": "phase6_gold_independent_integrity_audit",
        "status": "failed" if issues else ("passed_with_limitations" if warnings else "passed"),
        "summary": {
            "case_count": len(cases),
            "source_workbooks_exact": sum(item["exact"] for item in source_file_checks),
            "source_workbooks_total": len(source_file_checks),
            "source_rows_checked": source_rows_checked,
            "source_values_checked": source_values_checked,
            "source_value_mismatches": source_value_mismatches,
            "candidate_counts_checked": candidate_checks,
            "candidate_count_mismatches": candidate_mismatches,
            "topn_checked": topn_checks,
            "topn_mismatches": topn_mismatches,
            "aggregates_checked": aggregate_checks,
            "aggregate_mismatches": aggregate_mismatches,
            "case_candidate_totals_checked": total_checks,
            "case_candidate_total_mismatches": total_mismatches,
            "external_documents": len(documents),
            "external_snapshots_hash_exact": snapshot_exact,
            "external_snapshots_missing_ids": snapshot_missing_ids,
            "external_relation_rows_checked": len(relation_rows),
            "issue_count": len(issues),
            "warning_count": len(warnings),
        },
        "issue_counts": dict(sorted(issue_counts.items())),
        "warning_counts": dict(sorted(warning_counts.items())),
        "issues": issues[:300],
        "warnings": warnings,
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Audit sealed Phase 6 private Gold.")
    parser.add_argument("--gold", type=Path, required=True)
    parser.add_argument("--gold-sha256", required=True)
    parser.add_argument("--questions", type=Path, required=True)
    parser.add_argument("--questions-sha256", required=True)
    parser.add_argument("--source-dir", type=Path, required=True)
    parser.add_argument("--database-dir", type=Path, required=True)
    parser.add_argument("--snapshot-root", type=Path, action="append", default=[])
    parser.add_argument("--output", type=Path, required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    if _sha256(arguments.gold) != arguments.gold_sha256:
        raise SystemExit("Gold SHA-256 differs")
    if _sha256(arguments.questions) != arguments.questions_sha256:
        raise SystemExit("question-only SHA-256 differs")
    gold = json.loads(arguments.gold.read_bytes())
    questions = json.loads(arguments.questions.read_bytes())
    if gold.get("questions_sha256") != arguments.questions_sha256:
        raise SystemExit("Gold/question-only linkage differs")
    result = audit(
        gold,
        questions,
        arguments.source_dir,
        arguments.database_dir,
        tuple(arguments.snapshot_root),
    )
    result["gold_sha256"] = arguments.gold_sha256
    result["questions_sha256"] = arguments.questions_sha256
    encoded = (json.dumps(result, ensure_ascii=False, indent=2) + "\n").encode()
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
