from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
from collections import Counter, defaultdict
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from finance_agent_core.config import load_field_registry


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _load_json(path: Path, expected_sha256: str, label: str) -> dict[str, Any]:
    if not path.is_absolute() or not path.is_file() or path.is_symlink():
        raise RuntimeError(f"{label} must be an absolute regular file")
    if _sha256(path) != expected_sha256:
        raise RuntimeError(f"{label} SHA-256 differs")
    value = json.loads(path.read_bytes())
    if not isinstance(value, dict):
        raise TypeError(f"{label} must be a JSON object")
    return value


def _canonical_sha256(value: object) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _write_new(path: Path, payload: object) -> None:
    if not path.is_absolute() or not path.parent.is_dir() or path.is_symlink():
        raise RuntimeError("output must be a safe absolute path")
    encoded = (json.dumps(payload, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    descriptor = os.open(
        path,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0),
        0o400,
    )
    try:
        view = memoryview(encoded)
        while view:
            view = view[os.write(descriptor, view) :]
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _source_field_map() -> dict[tuple[str, str], str]:
    registry = load_field_registry()
    candidates: dict[tuple[str, str], list[str]] = defaultdict(list)
    for field_id, definition in registry.fields.items():
        for family in definition.datasets:
            resolved = definition.resolve(family)
            for source_column in resolved.source.columns:
                # Official XLSX schema uses upper-case raw column names while older
                # registries used lower-case names.  Gold stores the official spelling,
                # but source-column identity is case-insensitive at this boundary.
                key = (family, source_column.casefold())
                candidates[key].append(field_id)
    # One raw column may feed both a human-facing field and a derived/internal field.
    # Gold names the raw source column, so choose the field accepted by QueryPlan rather
    # than product_id/static-derived metadata. These choices are dataset contracts, not
    # per-question answer-key overrides.
    preferred = {
        ("overseas_etp", "pd_exg_mkt_cd"): "exchange_code",
        ("overseas_etp", "pd_itm_no"): "ticker",
        ("domestic_etp", "pd_exg_mkt_cd"): "exchange_code",
        ("domestic_etp", "pd_itm_no"): "isin",
        ("bond", "pd_no"): "ticker",
        ("bond", "pd_exg_mkt"): "bond_market",
        ("bond", "info_base_dt"): "static_as_of",
        ("bond", "mat_dt"): "maturity_date",
        ("fund", "zrin_fd_ivst_risk_grd_nm"): "risk_level",
    }
    result: dict[tuple[str, str], str] = {}
    for key, field_ids in candidates.items():
        choice = preferred.get(key)
        if choice is not None:
            if choice not in field_ids:
                raise RuntimeError(f"preferred source mapping is invalid: {key} -> {choice}")
            result[key] = choice
            continue
        queryable = [
            field_id
            for field_id in field_ids
            if registry.require_field(field_id, [key[0]]).queryable
        ]
        if len(queryable) == 1:
            result[key] = queryable[0]
        elif len(field_ids) == 1:
            result[key] = field_ids[0]
        else:
            raise RuntimeError(f"ambiguous source field mapping: {key} -> {field_ids}")
    return result


def _normalize_scalar(value: Any, *, boolean: bool = False) -> Any:
    if boolean:
        if isinstance(value, bool):
            return value
        if str(value).strip().casefold() in {"y", "yes", "true", "1"}:
            return True
        if str(value).strip().casefold() in {"n", "no", "false", "0"}:
            return False
    if isinstance(value, (int, float, Decimal)) and not isinstance(value, bool):
        try:
            return str(Decimal(str(value)).normalize())
        except InvalidOperation:
            pass
    if isinstance(value, str):
        return " ".join(value.split()).casefold()
    return value


def _normalize_filter(
    item: dict[str, Any],
    *,
    family: str,
    source_fields: dict[tuple[str, str], str],
) -> tuple[str, str, object]:
    registry = load_field_registry()
    raw_field = str(item["field"])
    field_id = source_fields.get((family, raw_field.casefold()), raw_field)
    operator = str(item["operator"])
    if field_id in {"product_id", "ticker", "isin"} and operator in {"eq", "in"}:
        field_id = "product_identity"
    try:
        definition = registry.require_field(field_id, [family])
    except ValueError:
        definition = None
    value = item.get("value")
    boolean = definition is not None and definition.value_type.value == "boolean"
    if isinstance(value, str):
        semantic_boolean = {
            "public_offering": {"공모": True, "사모": False},
            "sellable": {"판매중": True, "판매가능": True, "판매불가": False},
            "company_sellable": {"판매중": True, "판매가능": True, "판매불가": False},
        }.get(field_id, {})
        value = semantic_boolean.get(value.strip(), value)
    if isinstance(value, list):
        normalized: object = tuple(
            sorted(
                (_normalize_scalar(entry, boolean=boolean) for entry in value),
                key=str,
            )
        )
    else:
        normalized = _normalize_scalar(value, boolean=boolean)
    return field_id, operator, normalized


def _safe_ratio(numerator: int, denominator: int) -> float:
    return 0.0 if denominator == 0 else numerator / denominator


def _f1(precision: float, recall: float) -> float:
    return 0.0 if precision + recall == 0 else 2 * precision * recall / (precision + recall)


def _gold_product_identity(product: dict[str, Any], family: str) -> str:
    # Gold follows the organizer's public source identity for each table.  The
    # normalized runtime keeps a separate internal product_id for overseas ETPs
    # and duplicate bond lots, so compare those families by their public ticker.
    if family in {"bond", "overseas_etp"}:
        value = product.get("ticker")
    else:
        value = product.get("product_id")
    return "" if value is None else str(value)


def _percentile(values: list[float], quantile: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, math.ceil(quantile * len(ordered)) - 1))
    return round(ordered[index], 3)


def compare(
    predictions: dict[str, Any],
    gold: dict[str, Any],
    *,
    allow_post_gold_development: bool = False,
) -> dict[str, Any]:
    if allow_post_gold_development:
        if (
            predictions.get("answer_key_opened") is not True
            or predictions.get("evaluation_kind") != "post_gold_development_regression"
            or predictions.get("artifact_kind") != "post_gold_development_regression_predictions"
        ):
            raise RuntimeError("prediction artifact is not an explicit post-Gold regression")
    elif predictions.get("answer_key_opened") is not False:
        raise RuntimeError("prediction artifact was not frozen before Gold reveal")
    if predictions.get("question_count") != 300 or len(predictions.get("results", ())) != 300:
        raise RuntimeError("prediction count differs")
    if gold.get("suite_id") != "finance-agent-independent-holdout-v2-300":
        raise RuntimeError("Gold suite differs")
    if gold.get("questions_sha256") != predictions.get("questions_sha256"):
        raise RuntimeError("Gold and prediction question hashes differ")
    prediction_by_id = {item["id"]: item for item in predictions["results"]}
    gold_by_id = {item["id"]: item for item in gold.get("cases", ())}
    expected_ids = {f"IH-{index:03d}" for index in range(1, 301)}
    if set(prediction_by_id) != expected_ids or set(gold_by_id) != expected_ids:
        raise RuntimeError("Gold or prediction IDs differ")

    source_fields = _source_field_map()
    cases = []
    totals = Counter()
    category_totals: dict[str, Counter[str]] = defaultdict(Counter)
    product_tp = product_fp = product_fn = 0
    macro_precision: list[float] = []
    macro_recall: list[float] = []
    macro_f1: list[float] = []
    latencies: list[float] = []
    operation_counts = Counter()
    provider_outcomes = Counter()

    backend_disposition = {
        "success": "execute",
        "clarification": "clarify",
        "not_found": "not_found",
        "unsupported": "unsupported",
        "cannot_verify": "cannot_verify",
        "error": "error",
    }
    backend_result_status = {
        "success": "SUCCESS",
        "clarification": "CLARIFY",
        "not_found": "NOT_FOUND",
        "cannot_verify": "CANNOT_VERIFY",
    }

    for case_id in sorted(expected_ids):
        expected = gold_by_id[case_id]
        observed = prediction_by_id[case_id]
        response = observed["backend_response"]
        status = response["status"]
        predicted_disposition = backend_disposition.get(status, "error")
        expected_disposition = expected["expected_disposition"]
        disposition_exact = predicted_disposition == expected_disposition
        result_status_exact = None
        if expected["expected_result_status"] is not None:
            result_status_exact = (
                backend_result_status.get(status) == expected["expected_result_status"]
            )

        family = expected["expected_product_family"]
        expected_ids_ordered = list(expected["expected_top_product_ids"])
        observed_ids_ordered = [
            _gold_product_identity(item, family) for item in response["products"]
        ]
        expected_set = set(expected_ids_ordered)
        observed_set = set(observed_ids_ordered)
        tp = len(expected_set & observed_set)
        fp = len(observed_set - expected_set)
        fn = len(expected_set - observed_set)
        precision = _safe_ratio(tp, tp + fp)
        recall = _safe_ratio(tp, tp + fn)
        case_f1 = _f1(precision, recall)
        if expected_disposition == "execute":
            product_tp += tp
            product_fp += fp
            product_fn += fn
            macro_precision.append(precision)
            macro_recall.append(recall)
            macro_f1.append(case_f1)

        plan = response.get("query_plan")
        operation_exact = family_exact = filters_exact = ranking_exact = limit_exact = None
        if expected_disposition == "execute":
            if plan is not None and "intent" in plan:
                operation_exact = plan["intent"] == expected["expected_operation"]
                family_exact = plan["product_families"] == [expected["expected_product_family"]]
                expected_filters = tuple(
                    sorted(
                        (
                            _normalize_filter(
                                item,
                                family=family,
                                source_fields=source_fields,
                            )
                            for item in expected["expected_filters"]
                        ),
                        key=str,
                    )
                )
                observed_filters = tuple(
                    sorted(
                        (
                            _normalize_filter(
                                item,
                                family=family,
                                source_fields=source_fields,
                            )
                            for item in plan["constraints"]
                        ),
                        key=str,
                    )
                )
                filters_exact = observed_filters == expected_filters
                raw_ranking = expected["expected_ranking_field"]
                expected_ranking_field = (
                    None
                    if raw_ranking is None
                    else source_fields.get((family, raw_ranking.casefold()), raw_ranking)
                )
                observed_ranking = plan["ranking"]
                if expected_ranking_field is None:
                    ranking_exact = not observed_ranking
                else:
                    ranking_exact = len(observed_ranking) == 1 and (
                        observed_ranking[0]["field"] == expected_ranking_field
                        and observed_ranking[0]["direction"] == expected["expected_direction"]
                    )
                limit_exact = plan["limit"] == expected["expected_limit"]
            elif plan is not None and plan.get("plan_kind") == "composite_external_knowledge":
                operation_exact = plan.get("interaction_intent") == expected["expected_operation"]
                # The external plan intentionally has a different DTO. Product family,
                # filters, ranking and limit are verified by its own product-selection
                # and evidence contracts, so do not pretend they match a standard plan.
                family_exact = filters_exact = ranking_exact = limit_exact = False
            else:
                operation_exact = family_exact = filters_exact = ranking_exact = limit_exact = False

        product_order_exact = observed_ids_ordered == expected_ids_ordered
        candidate_count_exact = response["candidate_count"] == expected["expected_candidate_count"]
        outcome_exact = disposition_exact and (
            expected_disposition != "execute" or product_order_exact
        )
        strict_exact = outcome_exact and (
            expected_disposition != "execute"
            or all(
                (
                    operation_exact,
                    family_exact,
                    filters_exact,
                    ranking_exact,
                    limit_exact,
                    candidate_count_exact,
                )
            )
        )
        external_required = bool(expected["expected_status_basis"]["external_evidence_required"])
        documents_present = bool(response.get("documents") or response.get("citations"))
        case_metrics = {
            "id": case_id,
            "category": expected["category"],
            "external_evidence_required": external_required,
            "expected_disposition": expected_disposition,
            "predicted_disposition": predicted_disposition,
            "backend_status": status,
            "http_status": observed["http_status"],
            "disposition_exact": disposition_exact,
            "result_status_exact": result_status_exact,
            "operation_exact": operation_exact,
            "family_exact": family_exact,
            "filters_exact": filters_exact,
            "ranking_exact": ranking_exact,
            "limit_exact": limit_exact,
            "candidate_count_exact": candidate_count_exact,
            "product_order_exact": product_order_exact,
            "outcome_exact": outcome_exact,
            "strict_exact": strict_exact,
            "product_precision": round(precision, 6),
            "product_recall": round(recall, 6),
            "product_f1": round(case_f1, 6),
            "expected_product_count": len(expected_ids_ordered),
            "predicted_product_count": len(observed_ids_ordered),
            "documents_or_citations_present": documents_present,
            "duration_ms": observed["duration_ms"],
            "provider_operations": [item["operation"] for item in observed["provider_calls"]],
            "provider_outcomes": [item["outcome"] for item in observed["provider_calls"]],
        }
        cases.append(case_metrics)
        latencies.append(float(observed["duration_ms"]))
        aggregate_flags = {
            "disposition_exact": disposition_exact,
            "result_status_applicable": result_status_exact is not None,
            "result_status_exact": result_status_exact is True,
            "operation_exact": operation_exact is True,
            "family_exact": family_exact is True,
            "filters_exact": filters_exact is True,
            "ranking_exact": ranking_exact is True,
            "limit_exact": limit_exact is True,
            "outcome_exact": outcome_exact,
            "strict_exact": strict_exact,
            "http_200": observed["http_status"] == 200,
            "external_required": external_required,
            "external_outcome_exact": external_required and outcome_exact,
            "external_documents_present": external_required and documents_present,
        }
        if expected_disposition == "execute":
            aggregate_flags["candidate_count_exact"] = candidate_count_exact
            aggregate_flags["product_order_exact"] = product_order_exact
        totals.update(aggregate_flags)
        category_totals[expected["category"]].update(
            {
                "total": 1,
                "disposition_exact": disposition_exact,
                "outcome_exact": outcome_exact,
                "strict_exact": strict_exact,
            }
        )
        for record in observed["provider_calls"]:
            operation_counts[record["operation"]] += 1
            provider_outcomes[f"{record['operation']}:{record['outcome']}"] += 1

    execute_total = sum(1 for item in gold["cases"] if item["expected_disposition"] == "execute")
    result_applicable = totals["result_status_applicable"]
    micro_precision = _safe_ratio(product_tp, product_tp + product_fp)
    micro_recall = _safe_ratio(product_tp, product_tp + product_fn)
    report: dict[str, Any] = {
        "schema_version": "1.0",
        "artifact_kind": (
            "post_gold_development_gold_comparison"
            if allow_post_gold_development
            else "sealed_first_blind_gold_comparison"
        ),
        "evaluation_kind": (
            "post_gold_development_regression"
            if allow_post_gold_development
            else "first_independent_blind"
        ),
        "prediction_canonical_sha256": predictions["canonical_prediction_sha256"],
        "questions_sha256": predictions["questions_sha256"],
        "gold_revision": gold.get("gold_revision"),
        "implementation_commit": predictions["implementation_commit"],
        "image_id": predictions["image_id"],
        "counts": {
            "cases": 300,
            "execute": execute_total,
            "external_evidence_required": totals["external_required"],
            "http_200": totals["http_200"],
        },
        "metrics": {
            "disposition_accuracy": round(totals["disposition_exact"] / 300, 6),
            "result_status_accuracy": round(
                _safe_ratio(totals["result_status_exact"], result_applicable), 6
            ),
            "outcome_exact_accuracy": round(totals["outcome_exact"] / 300, 6),
            "strict_exact_accuracy": round(totals["strict_exact"] / 300, 6),
            "product_micro_precision": round(micro_precision, 6),
            "product_micro_recall": round(micro_recall, 6),
            "product_micro_f1": round(_f1(micro_precision, micro_recall), 6),
            "product_macro_precision": round(sum(macro_precision) / execute_total, 6),
            "product_macro_recall": round(sum(macro_recall) / execute_total, 6),
            "product_macro_f1": round(sum(macro_f1) / execute_total, 6),
            "product_order_exact_accuracy": round(totals["product_order_exact"] / execute_total, 6),
            "candidate_count_exact_accuracy": round(
                totals["candidate_count_exact"] / execute_total, 6
            ),
            "queryplan_operation_accuracy": round(totals["operation_exact"] / execute_total, 6),
            "queryplan_family_accuracy": round(totals["family_exact"] / execute_total, 6),
            "queryplan_filter_accuracy": round(totals["filters_exact"] / execute_total, 6),
            "queryplan_ranking_accuracy": round(totals["ranking_exact"] / execute_total, 6),
            "queryplan_limit_accuracy": round(totals["limit_exact"] / execute_total, 6),
            "external_outcome_exact_accuracy": round(
                _safe_ratio(totals["external_outcome_exact"], totals["external_required"]), 6
            ),
            "external_evidence_presence_rate": round(
                _safe_ratio(totals["external_documents_present"], totals["external_required"]),
                6,
            ),
            "latency_ms_p50": _percentile(latencies, 0.50),
            "latency_ms_p95": _percentile(latencies, 0.95),
            "latency_ms_p99": _percentile(latencies, 0.99),
            "latency_ms_max": round(max(latencies), 3),
        },
        "by_category": {
            category: {
                "total": values["total"],
                "disposition_accuracy": round(values["disposition_exact"] / values["total"], 6),
                "outcome_exact_accuracy": round(values["outcome_exact"] / values["total"], 6),
                "strict_exact_accuracy": round(values["strict_exact"] / values["total"], 6),
            }
            for category, values in sorted(category_totals.items())
        },
        "provider_operations": dict(sorted(operation_counts.items())),
        "provider_outcomes": dict(sorted(provider_outcomes.items())),
        "failure_ids": {
            "http_non_200": [item["id"] for item in cases if item["http_status"] != 200],
            "disposition_mismatch": [item["id"] for item in cases if not item["disposition_exact"]],
            "product_order_mismatch": [
                item["id"]
                for item in cases
                if item["expected_disposition"] == "execute" and not item["product_order_exact"]
            ],
            "queryplan_filter_mismatch": [
                item["id"] for item in cases if item["filters_exact"] is False
            ],
            "external_outcome_mismatch": [
                item["id"]
                for item in cases
                if item["external_evidence_required"] and not item["outcome_exact"]
            ],
        },
        "cases": cases,
    }
    report["report_sha256"] = _canonical_sha256(report)
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description="Compare one sealed prediction to private Gold.")
    parser.add_argument("--predictions", type=Path, required=True)
    parser.add_argument("--predictions-sha256", required=True)
    parser.add_argument("--gold", type=Path, required=True)
    parser.add_argument("--gold-sha256", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--allow-post-gold-development",
        action="store_true",
        help="compare an explicitly labelled post-Gold development regression",
    )
    arguments = parser.parse_args()
    predictions = _load_json(
        arguments.predictions,
        arguments.predictions_sha256,
        "predictions",
    )
    gold = _load_json(arguments.gold, arguments.gold_sha256, "Gold")
    report = compare(
        predictions,
        gold,
        allow_post_gold_development=arguments.allow_post_gold_development,
    )
    _write_new(arguments.output, report)
    print(
        json.dumps(
            {
                "metrics": report["metrics"],
                "by_category": report["by_category"],
                "failure_counts": {key: len(value) for key, value in report["failure_ids"].items()},
                "report_sha256": report["report_sha256"],
                "output_sha256": _sha256(arguments.output),
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
