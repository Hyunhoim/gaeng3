from __future__ import annotations

import hashlib
import json
import re
import time
from collections.abc import Mapping, Sequence
from importlib.resources import files
from typing import Literal, TypedDict

from finance_agent_core.agent.semantic_admission import (
    constrain_semantic_candidate_fields,
    is_generic_semantic_ambiguity,
    semantic_field_evidence,
)
from finance_agent_core.config import FieldRegistry, load_field_registry
from finance_agent_core.contracts.queryplan import ProductFamily
from finance_agent_core.retrieval.schema_dense import (
    DenseSchemaIndex,
    EmbeddingProvider,
    SchemaDenseActivationPolicy,
    build_schema_field_entries,
    p1_precision_field_thresholds,
)

_TOKEN = re.compile(r"[0-9a-z가-힣_]+", re.IGNORECASE)


class ResidualCase(TypedDict):
    id: str
    split: Literal["calibration", "test"]
    product_family: str
    residual_span: str
    expected: Literal["resolve", "clarify"]
    gold_field_id: str | None


class _Observation(TypedDict):
    id: str
    split: str
    expected: str
    gold_field_id: str | None
    lexical: list[str]
    dense: list[str]
    dense_scores: list[float]
    top_score: float
    margin: float


def _canonical_sha256(value: object) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    ).hexdigest()


def _normalize(value: str) -> str:
    return " ".join(item.casefold() for item in _TOKEN.findall(value))


def _sortable_fields(registry: FieldRegistry, family: ProductFamily) -> set[str]:
    return {
        field_id
        for field_id, definition in registry.fields.items()
        if family.value in definition.datasets
        and definition.resolve(family.value).sortable
    }


def _lexical_rank(
    span: str,
    family: ProductFamily,
    registry: FieldRegistry,
    *,
    top_k: int,
    allowed_field_ids: set[str] | None = None,
) -> list[str]:
    normalized = _normalize(span)
    scored: list[tuple[int, int, str]] = []
    for field_id in (
        _sortable_fields(registry, family)
        if allowed_field_ids is None
        else allowed_field_ids
    ):
        definition = registry.require_field(field_id, [family.value])
        surfaces = tuple(
            filter(
                None,
                (
                    _normalize(value)
                    for value in (field_id, definition.label, *definition.aliases)
                ),
            )
        )
        exact = any(normalized == value for value in surfaces)
        contained = max(
            (
                min(len(normalized), len(value))
                for value in surfaces
                if min(len(normalized), len(value)) >= 2
                and (normalized in value or value in normalized)
            ),
            default=0,
        )
        if exact:
            length = max(len(value) for value in surfaces if value == normalized)
            scored.append((2, length, field_id))
        elif contained:
            scored.append((1, contained, field_id))
    scored.sort(key=lambda item: (-item[0], -item[1], item[2]))
    return [field_id for _, _, field_id in scored[:top_k]]


def _p1_policy() -> SchemaDenseActivationPolicy:
    return SchemaDenseActivationPolicy(
        dense_min_score=0.382017,
        hclx_candidate_min_score=0.30,
        minimum_margin=0.007688,
        top_k=10,
        calibration_report_sha256="0" * 64,
        field_thresholds=p1_precision_field_thresholds(),
    )


def _selective_policy_observations(
    index: DenseSchemaIndex,
    cases: Sequence[ResidualCase],
    registry: FieldRegistry,
    *,
    top_k: int,
) -> list[dict[str, object]]:
    """Evaluate the full server admission policy, not an unbounded vector top-1."""

    policy = _p1_policy()
    observations: list[dict[str, object]] = []
    for case in cases:
        family = ProductFamily(case["product_family"])
        span = case["residual_span"]
        generic = is_generic_semantic_ambiguity(
            span,
            product_family=family,
            operation="rank",
        )
        eligible = constrain_semantic_candidate_fields(
            span,
            product_family=family,
            operation="rank",
            eligible_field_ids=_sortable_fields(registry, family),
        )
        lexical = _lexical_rank(
            span,
            family,
            registry,
            top_k=top_k,
            allowed_field_ids=eligible,
        )
        candidates = (
            []
            if generic or not eligible
            else index.search(
                span,
                family,
                top_k=top_k,
                allowed_field_ids=eligible,
            )
        )
        top = candidates[0] if candidates else None
        next_score = candidates[1].score if len(candidates) > 1 else -1.0
        margin = 0.0 if top is None else max(0.0, top.score - next_score)
        supported = []
        for candidate in candidates:
            threshold = policy.threshold_for(
                product_family=family,
                field_id=candidate.field_id,
                operation="rank",
            )
            evidence = semantic_field_evidence(
                span,
                field_id=candidate.field_id,
                product_family=family,
                operation="rank",
            )
            if (
                evidence.supported
                and candidate.score >= threshold.hclx_candidate_min_score
            ):
                supported.append(candidate)
        decision = "clarify"
        selected_field_id = None
        if not generic and len(supported) == 1:
            decision = "dense_resolve"
            selected_field_id = supported[0].field_id
        elif not generic and len(supported) > 1:
            decision = "hclx_review"
        observations.append(
            {
                "id": case["id"],
                "split": case["split"],
                "expected": case["expected"],
                "gold_field_id": case["gold_field_id"],
                "decision": decision,
                "selected_field_id": selected_field_id,
                "generic_gate": generic,
                "eligible_field_ids": sorted(eligible),
                "lexical": lexical,
                "dense": [item.field_id for item in candidates],
                "dense_scores": [item.score for item in candidates],
                "hclx_candidate_field_ids": [item.field_id for item in supported],
                "hclx_candidate_scores": [item.score for item in supported],
                "top_score": None if top is None else top.score,
                "margin": margin,
                "supported_evidence_count": len(supported),
            }
        )
    return observations


def _selective_policy_metrics(
    observations: Sequence[dict[str, object]],
) -> dict[str, float | int]:
    auto = [item for item in observations if item["decision"] == "dense_resolve"]
    correct_auto = [
        item
        for item in auto
        if item["expected"] == "resolve"
        and item["selected_field_id"] == item["gold_field_id"]
    ]
    wrong_auto = len(auto) - len(correct_auto)
    hclx = [item for item in observations if item["decision"] == "hclx_review"]
    hclx_with_gold = [
        item
        for item in hclx
        if item["expected"] == "resolve"
        and item["gold_field_id"] in item["hclx_candidate_field_ids"]
    ]
    controls = [item for item in observations if item["expected"] == "clarify"]
    resolvable = [item for item in observations if item["expected"] == "resolve"]
    reachable = len(correct_auto) + len(hclx_with_gold)
    return {
        "case_count": len(observations),
        "resolvable_case_count": len(resolvable),
        "control_case_count": len(controls),
        "dense_auto_admit_count": len(auto),
        "dense_auto_admit_correct": len(correct_auto),
        "dense_auto_admit_wrong": wrong_auto,
        "dense_auto_admit_precision": (
            round(len(correct_auto) / len(auto), 6) if auto else 1.0
        ),
        "hclx_review_count": len(hclx),
        "hclx_review_gold_in_top5": len(hclx_with_gold),
        "control_auto_execute_count": sum(
            item["decision"] == "dense_resolve" for item in controls
        ),
        "control_sent_to_hclx_count": sum(
            item["decision"] == "hclx_review" for item in controls
        ),
        "resolvable_reachable_count": reachable,
        "resolvable_reachable_rate": round(reachable / (len(resolvable) or 1), 6),
    }


def load_residual_holdout() -> tuple[dict[str, object], str]:
    resource = files("finance_agent_core.evaluation.suites").joinpath(
        "schema_dense_residual_holdout_v1.json"
    )
    raw = resource.read_bytes()
    payload = json.loads(raw)
    if (
        payload.get("suite_id") != "schema-dense-residual-developer-holdout-v1"
        or payload.get("status") != "developer_authored_not_independent_blind"
        or not isinstance(payload.get("cases"), list)
    ):
        raise ValueError("Schema Dense residual holdout contract differs")
    cases: list[ResidualCase] = payload["cases"]
    ids = [case["id"] for case in cases]
    if len(ids) != len(set(ids)) or not {case["split"] for case in cases} == {
        "calibration",
        "test",
    }:
        raise ValueError("Schema Dense residual holdout split or IDs differ")
    for case in cases:
        family = ProductFamily(case["product_family"])
        del family
        if (case["expected"] == "resolve") != (case["gold_field_id"] is not None):
            raise ValueError("Schema Dense residual holdout gold contract differs")
    return payload, hashlib.sha256(raw).hexdigest()


def _observations(
    index: DenseSchemaIndex,
    cases: Sequence[ResidualCase],
    registry: FieldRegistry,
    *,
    top_k: int,
) -> tuple[list[_Observation], list[float]]:
    observations: list[_Observation] = []
    latencies_ms: list[float] = []
    for case in cases:
        family = ProductFamily(case["product_family"])
        allowed = _sortable_fields(registry, family)
        lexical = _lexical_rank(case["residual_span"], family, registry, top_k=top_k)
        started = time.perf_counter()
        candidates = index.search(
            case["residual_span"],
            family,
            top_k=top_k,
            allowed_field_ids=allowed,
        )
        latencies_ms.append((time.perf_counter() - started) * 1000)
        scores = [candidate.score for candidate in candidates]
        top_score = scores[0] if scores else -1.0
        next_score = scores[1] if len(scores) > 1 else -1.0
        observations.append(
            {
                "id": case["id"],
                "split": case["split"],
                "expected": case["expected"],
                "gold_field_id": case["gold_field_id"],
                "lexical": lexical,
                "dense": [candidate.field_id for candidate in candidates],
                "dense_scores": scores,
                "top_score": top_score,
                "margin": max(0.0, top_score - next_score),
            }
        )
    return observations, latencies_ms


def _retrieval_metrics(
    observations: Sequence[_Observation],
    key: Literal["lexical", "dense"],
) -> dict[str, float | int]:
    resolved = [item for item in observations if item["expected"] == "resolve"]
    reciprocal_ranks: list[float] = []
    recalls = {3: 0, 5: 0, 10: 0}
    top1 = 0
    for item in resolved:
        ranked = item[key]
        gold = item["gold_field_id"]
        if ranked and ranked[0] == gold:
            top1 += 1
        rank = ranked.index(gold) + 1 if gold in ranked else None
        reciprocal_ranks.append(0.0 if rank is None else 1.0 / rank)
        for cutoff in recalls:
            recalls[cutoff] += int(gold in ranked[:cutoff])
    denominator = len(resolved) or 1
    return {
        "resolved_cases": len(resolved),
        "top1_accuracy": round(top1 / denominator, 6),
        "recall_at_3": round(recalls[3] / denominator, 6),
        "recall_at_5": round(recalls[5] / denominator, 6),
        "recall_at_10": round(recalls[10] / denominator, 6),
        "mrr": round(sum(reciprocal_ranks) / denominator, 6),
    }


def _admitted(item: _Observation, *, minimum_score: float, minimum_margin: float) -> bool:
    if item["top_score"] < minimum_score or item["margin"] < minimum_margin:
        return False
    return not item["lexical"] or item["lexical"][0] == item["dense"][0]


def _threshold_metrics(
    observations: Sequence[_Observation],
    *,
    minimum_score: float,
    minimum_margin: float,
) -> dict[str, float | int]:
    admitted = [
        item
        for item in observations
        if _admitted(item, minimum_score=minimum_score, minimum_margin=minimum_margin)
    ]
    correct = [
        item
        for item in admitted
        if item["expected"] == "resolve" and item["dense"][0] == item["gold_field_id"]
    ]
    controls = [item for item in admitted if item["expected"] == "clarify"]
    wrong_resolve = [
        item
        for item in admitted
        if item["expected"] == "resolve" and item["dense"][0] != item["gold_field_id"]
    ]
    resolvable = sum(item["expected"] == "resolve" for item in observations)
    return {
        "admitted": len(admitted),
        "correct": len(correct),
        "wrong_resolve": len(wrong_resolve),
        "control_auto_admit": len(controls),
        "precision": round(len(correct) / len(admitted), 6) if admitted else 1.0,
        "resolvable_coverage": round(len(correct) / (resolvable or 1), 6),
    }


def _calibrate(observations: Sequence[_Observation]) -> dict[str, object]:
    calibration = [item for item in observations if item["split"] == "calibration"]
    score_values = sorted({round(item["top_score"], 6) for item in calibration})
    margin_values = sorted({round(item["margin"], 6) for item in calibration})
    choices: list[tuple[float, float, dict[str, float | int]]] = []
    for score in score_values:
        for margin in margin_values:
            metrics = _threshold_metrics(
                calibration,
                minimum_score=score,
                minimum_margin=margin,
            )
            if (
                metrics["precision"] == 1.0
                and metrics["wrong_resolve"] == 0
                and metrics["control_auto_admit"] == 0
            ):
                choices.append((score, margin, metrics))
    if not choices:
        return {
            "status": "no_safe_threshold",
            "minimum_score": 1.000001,
            "minimum_margin": 2.000001,
            "calibration": _threshold_metrics(
                calibration,
                minimum_score=1.000001,
                minimum_margin=2.000001,
            ),
        }
    score, margin, metrics = max(
        choices,
        key=lambda item: (
            item[2]["resolvable_coverage"],
            item[2]["correct"],
            -item[0],
            -item[1],
        ),
    )
    return {
        "status": "safe_on_developer_calibration_only",
        "minimum_score": score,
        "minimum_margin": margin,
        "calibration": metrics,
    }


def evaluate_residual_holdout(provider: EmbeddingProvider) -> dict[str, object]:
    """Evaluate v1/v2/v3 retrieval without granting any corpus execution authority."""

    suite, suite_sha256 = load_residual_holdout()
    cases: list[ResidualCase] = suite["cases"]  # type: ignore[assignment]
    registry = load_field_registry()
    indexes: dict[str, DenseSchemaIndex] = {}
    for version in (
        "schema-field-text-v1",
        "schema-field-text-v2",
        "schema-field-prototype-v3",
    ):
        entries = build_schema_field_entries(corpus_template_version=version)
        indexes[version] = DenseSchemaIndex.build(
            entries,
            provider,
            corpus_template_version=version,
        )
    observations_by_version: dict[str, list[_Observation]] = {}
    latency_by_version: dict[str, list[float]] = {}
    for version, index in indexes.items():
        observations_by_version[version], latency_by_version[version] = _observations(
            index,
            cases,
            registry,
            top_k=10,
        )
    v3 = observations_by_version["schema-field-prototype-v3"]
    selective = _selective_policy_observations(
        indexes["schema-field-prototype-v3"],
        cases,
        registry,
        top_k=10,
    )
    selective_test = [item for item in selective if item["split"] == "test"]
    selective_all_metrics = _selective_policy_metrics(selective)
    selective_test_metrics = _selective_policy_metrics(selective_test)
    threshold = _calibrate(v3)
    test = [item for item in v3 if item["split"] == "test"]
    test_threshold = _threshold_metrics(
        test,
        minimum_score=float(threshold["minimum_score"]),
        minimum_margin=float(threshold["minimum_margin"]),
    )
    test_dense = _retrieval_metrics(test, "dense")
    test_diagnostics = [
        {
            "id": item["id"],
            "expected": item["expected"],
            "gold_field_id": item["gold_field_id"],
            "lexical": item["lexical"],
            "dense": item["dense"][:5],
            "dense_scores": item["dense_scores"][:5],
            "top_score": item["top_score"],
            "margin": item["margin"],
            "auto_admitted": _admitted(
                item,
                minimum_score=float(threshold["minimum_score"]),
                minimum_margin=float(threshold["minimum_margin"]),
            ),
        }
        for item in test
    ]
    gates = {
        "test_recall_at_5_at_least_0_98": test_dense["recall_at_5"] >= 0.98,
        "selective_test_auto_admit_precision_1_0": (
            selective_test_metrics["dense_auto_admit_precision"] == 1.0
        ),
        "selective_test_control_auto_execute_0": (
            selective_test_metrics["control_auto_execute_count"] == 0
        ),
        "selective_test_control_sent_to_hclx_0": (
            selective_test_metrics["control_sent_to_hclx_count"] == 0
        ),
        "selective_test_wrong_resolve_0": (
            selective_test_metrics["dense_auto_admit_wrong"] == 0
        ),
        "independent_blind_completed": False,
    }
    return {
        "schema_version": "1.0",
        "evaluation_id": "schema-dense-residual-developer-holdout-v1",
        "status": "developer_authored_not_independent_blind",
        "scope": "single_residual_ranking_field_linking_only",
        "suite_sha256": suite_sha256,
        "case_count": len(cases),
        "provider": provider.metadata.model_dump(mode="json"),
        "corpora": {
            version: {
                "manifest": index.manifest.model_dump(mode="json"),
                "all": _retrieval_metrics(observations_by_version[version], "dense"),
                "test": _retrieval_metrics(
                    [item for item in observations_by_version[version] if item["split"] == "test"],
                    "dense",
                ),
                "latency_ms": {
                    "p50": round(sorted(latency_by_version[version])[len(cases) // 2], 6),
                    "maximum": round(max(latency_by_version[version]), 6),
                },
            }
            for version, index in indexes.items()
        },
        "lexical": {
            "all": _retrieval_metrics(v3, "lexical"),
            "test": _retrieval_metrics(test, "lexical"),
        },
        "calibrated_threshold": threshold,
        "test_threshold_metrics": test_threshold,
        "test_diagnostics": test_diagnostics,
        "selective_policy": {
            "policy_sha256": _p1_policy().policy_sha256,
            "all_metrics": selective_all_metrics,
            "test_metrics": selective_test_metrics,
            "test_diagnostics": selective_test,
        },
        "activation_gates": gates,
        "activation_decision": (
            "internal_selective_gate_passed_independent_blind_pending"
            if all(value for key, value in gates.items() if key != "independent_blind_completed")
            else "shadow_only_quality_gate_failed"
        ),
        "report_sha256": "filled_after_canonicalization",
    }


def finalize_report(report: Mapping[str, object]) -> dict[str, object]:
    payload = dict(report)
    payload["report_sha256"] = _canonical_sha256(
        {key: value for key, value in payload.items() if key != "report_sha256"}
    )
    return payload


__all__ = ["evaluate_residual_holdout", "finalize_report", "load_residual_holdout"]
