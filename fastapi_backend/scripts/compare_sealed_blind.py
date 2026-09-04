from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
from collections import Counter, defaultdict
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from finance_agent_core.config import load_field_registry

_SHA256 = re.compile(r"[0-9a-f]{64}")

_SUPPORTED_SUITES = {
    "finance-agent-independent-holdout-v2-300": ("IH", 300, "phase1"),
    "finance-agent-independent-holdout-phase2-v1-300": ("P2", 300, "phase2"),
    "finance-agent-development-phase3-v1-600": ("P3", 600, "phase2_style"),
    "finance-agent-independent-holdout-phase4-v1-1000": ("P4", 1000, "phase2_style"),
    "finance-agent-final-rehearsal-phase5-v1.1-105": ("P5", 105, "phase2_style"),
    "finance-agent-generalization-phase6-v1-150": ("P6", 150, "phase2_style"),
    "finance-agent-final-evaluation-phase7-v2-200": ("P7V2", 200, "phase2_style"),
    "finance-agent-paraphrase-evaluation-phase8-200": ("P8", 200, "phase2_style"),
}

_SUITE_ID_WIDTHS = {
    "finance-agent-independent-holdout-phase4-v1-1000": 4,
}

_BACKEND_DISPOSITION = {
    # NOT_FOUND and CANNOT_VERIFY are terminal results of an executed lookup,
    # not evidence that the request was blocked before execution.
    "success": "execute",
    "not_found": "execute",
    "cannot_verify": "execute",
    "clarification": "clarify",
    "unsupported": "unsupported",
    "error": "error",
}

_BACKEND_RESULT_STATUS = {
    "success": "SUCCESS",
    "clarification": "CLARIFY",
    "not_found": "NOT_FOUND",
    "cannot_verify": "CANNOT_VERIFY",
}


def _expected_ids(suite_id: str, prefix: str, case_count: int) -> set[str]:
    if suite_id == "finance-agent-final-rehearsal-phase5-v1.1-105":
        return {
            f"{prefix}-{set_id}-{index:02d}"
            for set_id in ("A", "B", "C")
            for index in range(1, 36)
        }
    width = _SUITE_ID_WIDTHS.get(suite_id, 3)
    return {f"{prefix}-{index:0{width}d}" for index in range(1, case_count + 1)}


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
    # The public-fund normalizer uses the human-readable official risk label
    # when present and falls back to the numeric code.  Gold may therefore name
    # either source column, but both represent one canonical QueryPlan field.
    result[("fund", "zrin_fd_ivst_risk_grd_nm")] = "risk_level"
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

    def normalize_identity(entry: Any) -> Any:
        if field_id != "product_identity" or not isinstance(entry, str) or ":" not in entry:
            return entry
        if family == "bond":
            # Runtime bond IDs include market/date/sequence after the public
            # pd_no. Gold is intentionally keyed by that official pd_no.
            return entry.split(":", maxsplit=1)[0]
        if family == "overseas_etp":
            # Runtime overseas IDs prefix the official ticker with market.
            return entry.rsplit(":", maxsplit=1)[1]
        return entry

    def normalize_date(entry: Any) -> Any:
        if (
            definition is not None
            and definition.unit == "date"
            and isinstance(entry, str)
            and re.fullmatch(r"\d{8}", entry.strip())
        ):
            raw = entry.strip()
            return f"{raw[:4]}-{raw[4:6]}-{raw[6:]}"
        return entry

    def normalize_value(entry: Any) -> Any:
        return normalize_date(normalize_identity(entry))

    if isinstance(value, list):
        normalized: object = tuple(
            sorted(
                (_normalize_scalar(normalize_value(entry), boolean=boolean) for entry in value),
                key=str,
            )
        )
    else:
        normalized = _normalize_scalar(normalize_value(value), boolean=boolean)
    return field_id, operator, normalized


def _safe_ratio(numerator: int, denominator: int) -> float:
    return 0.0 if denominator == 0 else numerator / denominator


def _external_evidence_required(expected: dict[str, Any]) -> bool:
    """Read both the Phase 4 and legacy Phase 2 Gold layouts."""
    if "external_evidence_required" in expected:
        return bool(expected["external_evidence_required"])
    status_basis = expected.get("expected_status_basis", {})
    return bool(
        status_basis.get("external_evidence_required")
        if isinstance(status_basis, dict)
        else False
    )


def _f1(precision: float, recall: float) -> float:
    return 0.0 if precision + recall == 0 else 2 * precision * recall / (precision + recall)


def _gold_product_identity(product: dict[str, Any], family: str) -> str:
    # Gold follows the organizer's public source identity for each table.  The
    # normalized runtime keeps a separate internal product_id for overseas ETPs
    # and duplicate bond lots, so compare those families by their public ticker.
    if family in {"bond", "overseas_etp"}:
        value = product.get("ticker")
        if value is None:
            fields = product.get("fields", ())
            if isinstance(fields, list):
                value = next(
                    (
                        item.get("normalized_value")
                        for item in fields
                        if isinstance(item, dict) and item.get("canonical_field") == "ticker"
                    ),
                    None,
                )
    else:
        value = product.get("product_id")
    return "" if value is None else str(value)


def _percentile(values: list[float], quantile: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, math.ceil(quantile * len(ordered)) - 1))
    return round(ordered[index], 3)


def _observed_product_key(product: dict[str, Any]) -> tuple[str, str]:
    fields = product.get("fields", ())
    family = ""
    if isinstance(fields, list) and fields and isinstance(fields[0], dict):
        family = str(fields[0].get("source_dataset", ""))
    return family, _gold_product_identity(product, family)


def _phase2_plan_metrics(
    expected: dict[str, Any],
    response: dict[str, Any],
    *,
    source_fields: dict[tuple[str, str], str],
) -> dict[str, bool | None]:
    subqueries = expected.get("expected_subqueries", ())
    if expected.get("expected_disposition") != "execute" or not isinstance(subqueries, list):
        return {
            "family_exact": None,
            "filters_exact": None,
            "ranking_exact": None,
            "limit_exact": None,
        }
    public_plan = response.get("query_plan")
    if (
        isinstance(public_plan, dict)
        and public_plan.get("plan_kind") == "composite_external_knowledge"
    ):
        # Composite external-knowledge plans use a deliberately different DTO.
        # Their product selection, relation evidence and DB intersection are
        # scored independently; a standard SQL QueryPlan comparison is N/A.
        return {
            "family_exact": None,
            "filters_exact": None,
            "ranking_exact": None,
            "limit_exact": None,
        }
    if len(subqueries) == 1:
        plan = public_plan
        observed_plans = [
            {
                "plan": plan,
                "candidate_count": response.get("candidate_count"),
                "returned_product_ids": [
                    product.get("product_id") for product in response.get("products", ())
                ],
            }
        ]
    else:
        observed_plans = [
            {
                "plan": item.get("query_plan"),
                "candidate_count": item.get("candidate_count"),
                "returned_product_ids": item.get("returned_product_ids", ()),
            }
            for item in response.get("family_searches", ())
            if isinstance(item, dict)
        ]
    if len(observed_plans) != len(subqueries) or any(
        not isinstance(item["plan"], dict) for item in observed_plans
    ):
        return {
            "family_exact": False,
            "filters_exact": False,
            "ranking_exact": False,
            "limit_exact": False,
        }

    family_exact = True
    filters_exact = True
    ranking_checks: list[bool] = []
    limit_checks: list[bool] = []
    identity_fields = {"product_identity", "product_name", "short_name", "ticker", "isin"}
    for expected_plan, observed in zip(subqueries, observed_plans, strict=True):
        plan = observed["plan"]
        assert isinstance(plan, dict)
        family = str(expected_plan["product_family"])
        family_exact = family_exact and plan.get("product_families") == [family]
        expected_filters = tuple(
            sorted(
                (
                    _normalize_filter(item, family=family, source_fields=source_fields)
                    for item in expected_plan.get("filters", ())
                ),
                key=str,
            )
        )
        observed_filters = tuple(
            sorted(
                (
                    _normalize_filter(item, family=family, source_fields=source_fields)
                    for item in plan.get("constraints", ())
                ),
                key=str,
            )
        )
        expected_identity = tuple(item for item in expected_filters if item[0] in identity_fields)
        observed_identity = tuple(item for item in observed_filters if item[0] in identity_fields)
        expected_empty_identity = tuple(
            item
            for item in expected_identity
            if item[1] == "in" and item[2] == ()
        )
        verified_empty_identity_scope = (
            bool(expected_empty_identity)
            and not observed_identity
            and observed.get("candidate_count") == 0
            and not observed.get("returned_product_ids", ())
        )
        if verified_empty_identity_scope:
            # QueryPlan forbids an empty IN list. The compound execution receipt
            # proves the identity scope was active and returned zero without SQL
            # broadening, so compare the remaining public constraints only.
            expected_filters = tuple(
                item for item in expected_filters if item not in expected_empty_identity
            )
            expected_identity = tuple(
                item for item in expected_identity if item not in expected_empty_identity
            )
        exact_single_identity = (
            len(expected_identity) == 1
            and len(observed_identity) == 1
            and expected_identity[0][1] == "eq"
            and observed_identity[0][1] == "eq"
            and observed.get("candidate_count") == 1
            and len(observed.get("returned_product_ids", ())) == 1
        )
        if exact_single_identity:
            expected_filters = tuple(
                item for item in expected_filters if item not in expected_identity
            )
            observed_filters = tuple(
                item for item in observed_filters if item not in observed_identity
            )
        if expected_identity or observed_identity:
            redundant_type = ("product_type", "eq", "etf")
            expected_filters = tuple(item for item in expected_filters if item != redundant_type)
            observed_filters = tuple(item for item in observed_filters if item != redundant_type)
        filters_exact = filters_exact and observed_filters == expected_filters

        if expected_plan.get("derived_metric") is None:
            ranking = plan.get("ranking", ())
            expected_raw_ranking = expected_plan.get("ranking_field")
            expected_ranking = (
                None
                if expected_raw_ranking is None
                else source_fields.get(
                    (family, str(expected_raw_ranking).casefold()), expected_raw_ranking
                )
            )
            observed_ranking = None
            observed_direction = None
            if isinstance(ranking, list) and len(ranking) == 1 and isinstance(ranking[0], dict):
                observed_ranking = ranking[0].get("field")
                observed_direction = ranking[0].get("direction")
            ranking_checks.append(
                observed_ranking == expected_ranking
                and observed_direction == expected_plan.get("direction")
            )
        # When an externally verified identity set is empty, QueryPlan cannot
        # encode IN []; no row can be affected by the limit. Treat the arbitrary
        # Gold default as unobservable instead of an Agent error.
        if not verified_empty_identity_scope:
            limit_checks.append(plan.get("limit") == expected_plan.get("limit"))
    return {
        "family_exact": family_exact,
        "filters_exact": filters_exact,
        "ranking_exact": all(ranking_checks) if ranking_checks else None,
        "limit_exact": all(limit_checks) if limit_checks else None,
    }


def _compare_phase2(
    predictions: dict[str, Any],
    gold: dict[str, Any],
    *,
    allow_post_gold_development: bool = False,
) -> dict[str, Any]:
    suite_id = str(predictions["suite_id"])
    suite_contract = _SUPPORTED_SUITES.get(suite_id)
    if suite_contract is None or suite_contract[2] not in {"phase2", "phase2_style"}:
        raise RuntimeError("prediction suite is not a registered Phase 2-style contract")
    id_prefix, case_count, _ = suite_contract
    expected_ids = _expected_ids(suite_id, id_prefix, case_count)
    prediction_by_id = {item["id"]: item for item in predictions["results"]}
    gold_by_id = {item["id"]: item for item in gold.get("cases", ())}
    if set(prediction_by_id) != expected_ids or set(gold_by_id) != expected_ids:
        raise RuntimeError("Phase 2 Gold or prediction IDs differ")

    source_fields = _source_field_map()
    totals = Counter()
    by_category: dict[str, Counter[str]] = defaultdict(Counter)
    by_scope: dict[str, Counter[str]] = defaultdict(Counter)
    product_tp = product_fp = product_fn = 0
    macro_precision: list[float] = []
    macro_recall: list[float] = []
    macro_f1: list[float] = []
    latencies: list[float] = []
    operation_counts = Counter()
    provider_outcomes = Counter()
    cases: list[dict[str, Any]] = []

    for case_id in sorted(expected_ids):
        expected = gold_by_id[case_id]
        observed = prediction_by_id[case_id]
        response = observed["backend_response"]
        status = response["status"]
        predicted_disposition = _BACKEND_DISPOSITION.get(status, "error")
        expected_disposition = expected["expected_disposition"]
        disposition_exact = predicted_disposition == expected_disposition
        expected_status = expected.get("expected_result_status")
        result_status_exact = (
            None
            if expected_status is None
            else _BACKEND_RESULT_STATUS.get(status) == expected_status
        )

        expected_products = [
            (str(item["family"]), str(item["product_id"]))
            for item in expected.get("expected_top_products", ())
        ]
        observed_products = [_observed_product_key(item) for item in response.get("products", ())]
        expected_set = set(expected_products)
        observed_set = set(observed_products)
        tp = len(expected_set & observed_set)
        fp = len(observed_set - expected_set)
        fn = len(expected_set - observed_set)
        if not expected_set and not observed_set:
            # Returning no product is the exact retrieval result for verified
            # empty SUCCESS/NOT_FOUND/CANNOT_VERIFY cases, not a zero-score miss.
            precision = recall = case_f1 = 1.0
        else:
            precision = _safe_ratio(tp, tp + fp)
            recall = _safe_ratio(tp, tp + fn)
            case_f1 = _f1(precision, recall)
        is_execute = expected_disposition == "execute"
        if is_execute:
            product_tp += tp
            product_fp += fp
            product_fn += fn
            macro_precision.append(precision)
            macro_recall.append(recall)
            macro_f1.append(case_f1)

        order_exact = observed_products == expected_products
        candidate_count_exact = response.get("candidate_count") == expected.get(
            "expected_candidate_count_total"
        )
        plan_metrics = _phase2_plan_metrics(
            expected,
            response,
            source_fields=source_fields,
        )
        cross_family = expected.get("query_scope") == "cross_family"
        cross_family_execute_success = (
            None if not (cross_family and is_execute) else disposition_exact and order_exact
        )
        external_required = _external_evidence_required(expected)
        external_documents_present = bool(response.get("documents"))
        outcome_exact = (
            disposition_exact
            and result_status_exact is not False
            and (not is_execute or order_exact)
        )
        applicable_plan_metrics = [value for value in plan_metrics.values() if value is not None]
        strict_exact = outcome_exact and (
            not is_execute
            or (candidate_count_exact and all(value is True for value in applicable_plan_metrics))
        )
        false_execution = not is_execute and predicted_disposition == "execute"
        false_block = is_execute and predicted_disposition != "execute"

        totals.update(
            {
                "disposition_exact": disposition_exact,
                "result_status_applicable": result_status_exact is not None,
                "result_status_exact": result_status_exact is True,
                "execute": is_execute,
                "product_order_exact": is_execute and order_exact,
                "candidate_count_exact": is_execute and candidate_count_exact,
                "outcome_exact": outcome_exact,
                "strict_exact": strict_exact,
                "false_execution": false_execution,
                "false_block": false_block,
                "external_required": external_required,
                "external_outcome_exact": external_required and outcome_exact,
                "external_documents_present": (external_required and external_documents_present),
                "cross_family_execute": cross_family and is_execute,
                "cross_family_execute_success": cross_family_execute_success is True,
                "http_200": observed["http_status"] == 200,
            }
        )
        for key, value in plan_metrics.items():
            if value is not None:
                metric = key.removesuffix("_exact")
                totals[f"plan_{metric}_applicable"] += 1
                totals[f"plan_{key}"] += value is True
        for group in (
            by_category[str(expected["category"])],
            by_scope[str(expected["query_scope"])],
        ):
            group["total"] += 1
            group["disposition_exact"] += disposition_exact
            group["outcome_exact"] += outcome_exact
            group["strict_exact"] += strict_exact
        duration_ms = float(observed["duration_ms"])
        latencies.append(duration_ms)
        for call in observed.get("provider_calls", ()):
            operation_counts[str(call["operation"])] += 1
            provider_outcomes[f"{call['operation']}:{call['outcome']}"] += 1
        cases.append(
            {
                "id": case_id,
                "category": expected["category"],
                "query_scope": expected["query_scope"],
                "expected_disposition": expected_disposition,
                "predicted_disposition": predicted_disposition,
                "disposition_exact": disposition_exact,
                "result_status_exact": result_status_exact,
                "expected_product_families": expected["expected_product_families"],
                "observed_product_families": response.get("product_families", []),
                "expected_products": expected_products,
                "observed_products": observed_products,
                "product_precision": precision,
                "product_recall": recall,
                "product_f1": case_f1,
                "product_order_exact": order_exact,
                "candidate_count_exact": candidate_count_exact,
                **plan_metrics,
                "external_evidence_required": external_required,
                "external_documents_present": external_documents_present,
                "false_execution": false_execution,
                "false_block": false_block,
                "outcome_exact": outcome_exact,
                "strict_exact": strict_exact,
                "http_status": observed["http_status"],
                "duration_ms": duration_ms,
            }
        )

    execute_total = totals["execute"]
    micro_precision = _safe_ratio(product_tp, product_tp + product_fp)
    micro_recall = _safe_ratio(product_tp, product_tp + product_fn)

    def grouped(values: dict[str, Counter[str]]) -> dict[str, dict[str, float | int]]:
        return {
            key: {
                "total": item["total"],
                "disposition_accuracy": round(
                    _safe_ratio(item["disposition_exact"], item["total"]), 6
                ),
                "outcome_exact_accuracy": round(
                    _safe_ratio(item["outcome_exact"], item["total"]), 6
                ),
                "strict_exact_accuracy": round(_safe_ratio(item["strict_exact"], item["total"]), 6),
            }
            for key, item in sorted(values.items())
        }

    report: dict[str, Any] = {
        "schema_version": "2.0",
        "artifact_kind": (
            "post_gold_development_regression_comparison"
            if allow_post_gold_development
            else "sealed_first_blind_gold_comparison"
        ),
        "evaluation_kind": (
            "post_gold_development_regression"
            if allow_post_gold_development
            else "first_independent_blind"
        ),
        "suite_id": predictions["suite_id"],
        "suite_phase": predictions.get("suite_phase"),
        "prediction_canonical_sha256": predictions["canonical_prediction_sha256"],
        "questions_sha256": predictions["questions_sha256"],
        "implementation_commit": predictions["implementation_commit"],
        "image_id": predictions["image_id"],
        "regression_composition": predictions.get("regression_composition"),
        "counts": {
            "cases": case_count,
            "execute": execute_total,
            "cross_family": sum(item["query_scope"] == "cross_family" for item in cases),
            "cross_family_execute": totals["cross_family_execute"],
            "external_evidence_required": totals["external_required"],
            "http_200": totals["http_200"],
        },
        "metrics": {
            "disposition_accuracy": round(totals["disposition_exact"] / case_count, 6),
            "result_status_accuracy": round(
                _safe_ratio(totals["result_status_exact"], totals["result_status_applicable"]), 6
            ),
            "outcome_exact_accuracy": round(totals["outcome_exact"] / case_count, 6),
            "strict_exact_accuracy": round(totals["strict_exact"] / case_count, 6),
            "product_micro_precision": round(micro_precision, 6),
            "product_micro_recall": round(micro_recall, 6),
            "product_micro_f1": round(_f1(micro_precision, micro_recall), 6),
            "product_macro_precision": round(_safe_ratio(sum(macro_precision), execute_total), 6),
            "product_macro_recall": round(_safe_ratio(sum(macro_recall), execute_total), 6),
            "product_macro_f1": round(_safe_ratio(sum(macro_f1), execute_total), 6),
            "product_order_exact_accuracy": round(
                _safe_ratio(totals["product_order_exact"], execute_total), 6
            ),
            "candidate_count_exact_accuracy": round(
                _safe_ratio(totals["candidate_count_exact"], execute_total), 6
            ),
            "single_plan_family_accuracy": round(
                _safe_ratio(totals["plan_family_exact"], totals["plan_family_applicable"]), 6
            ),
            "single_plan_filter_accuracy": round(
                _safe_ratio(totals["plan_filters_exact"], totals["plan_filters_applicable"]), 6
            ),
            "single_plan_ranking_accuracy": round(
                _safe_ratio(totals["plan_ranking_exact"], totals["plan_ranking_applicable"]), 6
            ),
            "single_plan_limit_accuracy": round(
                _safe_ratio(totals["plan_limit_exact"], totals["plan_limit_applicable"]), 6
            ),
            "cross_family_execute_exact_accuracy": round(
                _safe_ratio(totals["cross_family_execute_success"], totals["cross_family_execute"]),
                6,
            ),
            "external_outcome_exact_accuracy": round(
                _safe_ratio(totals["external_outcome_exact"], totals["external_required"]), 6
            ),
            "external_evidence_presence_rate": round(
                _safe_ratio(totals["external_documents_present"], totals["external_required"]), 6
            ),
            "false_execution_rate": round(totals["false_execution"] / case_count, 6),
            "false_block_rate": round(totals["false_block"] / case_count, 6),
            "latency_ms_p50": _percentile(latencies, 0.50),
            "latency_ms_p95": _percentile(latencies, 0.95),
            "latency_ms_p99": _percentile(latencies, 0.99),
            "latency_ms_max": round(max(latencies), 3),
        },
        "by_category": grouped(by_category),
        "by_scope": grouped(by_scope),
        "provider_operations": dict(sorted(operation_counts.items())),
        "provider_outcomes": dict(sorted(provider_outcomes.items())),
        "failure_ids": {
            "http_non_200": [item["id"] for item in cases if item["http_status"] != 200],
            "disposition_mismatch": [item["id"] for item in cases if not item["disposition_exact"]],
            "false_execution": [item["id"] for item in cases if item["false_execution"]],
            "false_block": [item["id"] for item in cases if item["false_block"]],
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


def compare(
    predictions: dict[str, Any],
    gold: dict[str, Any],
    *,
    allow_post_gold_development: bool = False,
) -> dict[str, Any]:
    artifact_kind = predictions.get("artifact_kind")
    if allow_post_gold_development:
        if (
            predictions.get("answer_key_opened") is not True
            or predictions.get("evaluation_kind") != "post_gold_development_regression"
            or artifact_kind
            not in {
                "post_gold_development_regression_predictions",
                "post_gold_composed_targeted_regression_predictions",
            }
        ):
            raise RuntimeError("prediction artifact is not an explicit post-Gold regression")
    elif predictions.get("answer_key_opened") is not False:
        raise RuntimeError("prediction artifact was not frozen before Gold reveal")
    suite_id = predictions.get("suite_id", "finance-agent-independent-holdout-v2-300")
    suite_contract = _SUPPORTED_SUITES.get(suite_id)
    if suite_contract is None or gold.get("suite_id") != suite_id:
        raise RuntimeError("Gold suite differs")
    id_prefix, case_count, comparator = suite_contract
    if (
        predictions.get("question_count") != case_count
        or len(predictions.get("results", ())) != case_count
    ):
        raise RuntimeError("prediction count differs")
    composition = predictions.get("regression_composition")
    if artifact_kind == "post_gold_composed_targeted_regression_predictions":
        if not isinstance(composition, dict):
            raise RuntimeError("composed regression requires composition provenance")
        unchanged = composition.get("unchanged_full_run_cases")
        targeted = composition.get("actual_targeted_rerun_cases")
        source_hashes = predictions.get("targeted_regression_source_sha256s")
        if (
            not isinstance(unchanged, int)
            or not isinstance(targeted, int)
            or unchanged < 0
            or targeted <= 0
            or unchanged + targeted != case_count
            or not isinstance(source_hashes, list)
            or not source_hashes
            or any(_SHA256.fullmatch(str(value)) is None for value in source_hashes)
        ):
            raise RuntimeError("composed regression provenance is invalid")
    if gold.get("questions_sha256") != predictions.get("questions_sha256"):
        raise RuntimeError("Gold and prediction question hashes differ")
    prediction_by_id = {item["id"]: item for item in predictions["results"]}
    gold_by_id = {item["id"]: item for item in gold.get("cases", ())}
    expected_ids = _expected_ids(suite_id, id_prefix, case_count)
    if set(prediction_by_id) != expected_ids or set(gold_by_id) != expected_ids:
        raise RuntimeError("Gold or prediction IDs differ")

    if comparator in {"phase2", "phase2_style"}:
        return _compare_phase2(
            predictions,
            gold,
            allow_post_gold_development=allow_post_gold_development,
        )

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

    for case_id in sorted(expected_ids):
        expected = gold_by_id[case_id]
        observed = prediction_by_id[case_id]
        response = observed["backend_response"]
        status = response["status"]
        predicted_disposition = _BACKEND_DISPOSITION.get(status, "error")
        expected_disposition = expected["expected_disposition"]
        disposition_exact = predicted_disposition == expected_disposition
        result_status_exact = None
        if expected["expected_result_status"] is not None:
            result_status_exact = (
                _BACKEND_RESULT_STATUS.get(status) == expected["expected_result_status"]
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
        if not expected_set and not observed_set:
            precision = recall = case_f1 = 1.0
        else:
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
                # and evidence contracts. Mark those standard-QueryPlan dimensions as
                # not applicable instead of counting a different schema as an error.
                family_exact = filters_exact = ranking_exact = limit_exact = None
            else:
                operation_exact = family_exact = filters_exact = ranking_exact = limit_exact = False

        product_order_exact = observed_ids_ordered == expected_ids_ordered
        candidate_count_exact = response["candidate_count"] == expected["expected_candidate_count"]
        outcome_exact = (
            disposition_exact
            and result_status_exact is not False
            and (expected_disposition != "execute" or product_order_exact)
        )
        applicable_strict_metrics = [
            value
            for value in (
                operation_exact,
                family_exact,
                filters_exact,
                ranking_exact,
                limit_exact,
                candidate_count_exact,
            )
            if value is not None
        ]
        strict_exact = outcome_exact and (
            expected_disposition != "execute" or all(applicable_strict_metrics)
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
            "operation_applicable": operation_exact is not None,
            "family_exact": family_exact is True,
            "family_applicable": family_exact is not None,
            "filters_exact": filters_exact is True,
            "filters_applicable": filters_exact is not None,
            "ranking_exact": ranking_exact is True,
            "ranking_applicable": ranking_exact is not None,
            "limit_exact": limit_exact is True,
            "limit_applicable": limit_exact is not None,
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
        "suite_id": suite_id,
        "suite_phase": predictions.get("suite_phase"),
        "gold_revision": gold.get("gold_revision"),
        "implementation_commit": predictions["implementation_commit"],
        "image_id": predictions["image_id"],
        "regression_composition": composition,
        "counts": {
            "cases": case_count,
            "execute": execute_total,
            "external_evidence_required": totals["external_required"],
            "http_200": totals["http_200"],
        },
        "metrics": {
            "disposition_accuracy": round(totals["disposition_exact"] / case_count, 6),
            "result_status_accuracy": round(
                _safe_ratio(totals["result_status_exact"], result_applicable), 6
            ),
            "outcome_exact_accuracy": round(totals["outcome_exact"] / case_count, 6),
            "strict_exact_accuracy": round(totals["strict_exact"] / case_count, 6),
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
            "queryplan_operation_accuracy": round(
                _safe_ratio(totals["operation_exact"], totals["operation_applicable"]), 6
            ),
            "queryplan_family_accuracy": round(
                _safe_ratio(totals["family_exact"], totals["family_applicable"]), 6
            ),
            "queryplan_filter_accuracy": round(
                _safe_ratio(totals["filters_exact"], totals["filters_applicable"]), 6
            ),
            "queryplan_ranking_accuracy": round(
                _safe_ratio(totals["ranking_exact"], totals["ranking_applicable"]), 6
            ),
            "queryplan_limit_accuracy": round(
                _safe_ratio(totals["limit_exact"], totals["limit_applicable"]), 6
            ),
            "queryplan_applicable_counts": {
                "operation": totals["operation_applicable"],
                "family": totals["family_applicable"],
                "filters": totals["filters_applicable"],
                "ranking": totals["ranking_applicable"],
                "limit": totals["limit_applicable"],
            },
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
