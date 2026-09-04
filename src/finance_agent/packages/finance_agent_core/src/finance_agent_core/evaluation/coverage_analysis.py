from __future__ import annotations

from collections import Counter
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import Field

from finance_agent_core.contracts.queryplan import QueryPlan
from finance_agent_core.evaluation.coverage_plan import (
    CoverageModel,
    CoveragePlanSuite,
    coverage_plan_suite_semantic_sha256,
)
from finance_agent_core.evaluation.coverage_runner import (
    CoverageCaseResult,
    CoverageRunReport,
)
from finance_agent_core.evaluation.semantics import query_plan_semantic_payload


class CoverageFailureBucket(CoverageModel):
    axis: str
    value: str
    total: int = Field(ge=1)
    failed: int = Field(ge=1)
    failure_rate: float = Field(gt=0, le=1)
    first_failure_stages: dict[str, int]
    example_case_ids: list[str] = Field(min_length=1, max_length=5)


class CoveragePlanDeltaBucket(CoverageModel):
    code: str
    count: int = Field(ge=1)
    example_case_ids: list[str] = Field(min_length=1, max_length=5)


class CoverageDiagnosisSummary(CoverageModel):
    total: int = Field(ge=1)
    passed: int = Field(ge=0)
    failed: int = Field(ge=0)
    first_failure_stages: dict[str, int]
    plan_delta_cases: int = Field(ge=0)
    distinct_plan_delta_codes: int = Field(ge=0)


class CoverageDiagnosisReport(CoverageModel):
    schema_version: Literal["1.0"] = "1.0"
    diagnosis_id: str
    generated_at_utc: str
    status: Literal["internal_synthetic_not_blind"] = "internal_synthetic_not_blind"
    plan_suite_id: str
    plan_suite_semantic_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_report_id: str
    source_agent_profile: str
    summary: CoverageDiagnosisSummary
    priority_buckets: list[CoverageFailureBucket]
    plan_delta_buckets: list[CoveragePlanDeltaBucket]
    failure_case_ids_by_stage: dict[str, list[str]]
    interpretation_limits: list[str] = Field(min_length=1, max_length=20)


def _serialized(value: object) -> str:
    from json import dumps

    return dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _list_delta_codes(
    *,
    label: str,
    expected: Sequence[str],
    actual: Sequence[str],
) -> list[str]:
    expected_counter = Counter(expected)
    actual_counter = Counter(actual)
    codes: list[str] = []
    for item, count in sorted((expected_counter - actual_counter).items()):
        codes.extend([f"{label}_missing:{item}"] * count)
    for item, count in sorted((actual_counter - expected_counter).items()):
        codes.extend([f"{label}_extra:{item}"] * count)
    return codes


def _constraint_delta_codes(
    expected: dict[str, Any],
    actual: dict[str, Any],
) -> list[str]:
    expected_by_key = {
        (str(item["field"]), str(item["operator"])): _serialized(item)
        for item in expected["constraints"]
    }
    actual_by_key = {
        (str(item["field"]), str(item["operator"])): _serialized(item)
        for item in actual["constraints"]
    }
    codes: list[str] = []
    for key, expected_payload in expected_by_key.items():
        field, operator = key
        actual_payload = actual_by_key.get(key)
        if actual_payload is None:
            same_field = sorted(
                candidate_operator
                for candidate_field, candidate_operator in actual_by_key
                if candidate_field == field
            )
            if same_field:
                codes.append(f"constraint_operator_changed:{field}")
            else:
                codes.append(f"constraint_missing:{field}:{operator}")
        elif actual_payload != expected_payload:
            codes.append(f"constraint_value_changed:{field}:{operator}")
    for field, operator in actual_by_key.keys() - expected_by_key.keys():
        if field not in {candidate_field for candidate_field, _ in expected_by_key}:
            codes.append(f"constraint_extra:{field}:{operator}")
    return codes


def _ranking_delta_codes(
    expected: dict[str, Any],
    actual: dict[str, Any],
) -> list[str]:
    expected_by_field = {str(item["field"]): _serialized(item) for item in expected["ranking"]}
    actual_by_field = {str(item["field"]): _serialized(item) for item in actual["ranking"]}
    codes: list[str] = []
    for field, expected_payload in expected_by_field.items():
        actual_payload = actual_by_field.get(field)
        if actual_payload is None:
            codes.append(f"ranking_missing:{field}")
        elif actual_payload != expected_payload:
            codes.append(f"ranking_changed:{field}")
    for field in actual_by_field.keys() - expected_by_field.keys():
        codes.append(f"ranking_extra:{field}")
    return codes


def plan_delta_codes(expected: QueryPlan, actual: QueryPlan | None) -> list[str]:
    if actual is None:
        return ["plan_missing"]

    expected_semantic = query_plan_semantic_payload(expected)
    actual_semantic = query_plan_semantic_payload(actual)
    codes: list[str] = []
    if expected_semantic["intent"] != actual_semantic["intent"]:
        codes.append(f"intent_changed:{expected_semantic['intent']}->{actual_semantic['intent']}")
    codes.extend(
        _list_delta_codes(
            label="family",
            expected=expected_semantic["product_families"],
            actual=actual_semantic["product_families"],
        )
    )
    codes.extend(_constraint_delta_codes(expected_semantic, actual_semantic))
    codes.extend(_ranking_delta_codes(expected_semantic, actual_semantic))
    codes.extend(
        _list_delta_codes(
            label="projection",
            expected=expected_semantic["projection"],
            actual=actual_semantic["projection"],
        )
    )
    if expected_semantic["limit"] != actual_semantic["limit"]:
        codes.append("limit_changed")

    expected_payload = expected_semantic["intent_payload"]
    actual_payload = actual_semantic["intent_payload"]
    codes.extend(
        _list_delta_codes(
            label="comparison_field",
            expected=expected_payload["comparison_fields"],
            actual=actual_payload["comparison_fields"],
        )
    )
    codes.extend(
        _list_delta_codes(
            label="group_by",
            expected=expected_payload["group_by"],
            actual=actual_payload["group_by"],
        )
    )
    codes.extend(
        _list_delta_codes(
            label="aggregation",
            expected=[_serialized(item) for item in expected_payload["aggregations"]],
            actual=[_serialized(item) for item in actual_payload["aggregations"]],
        )
    )
    codes.extend(
        _list_delta_codes(
            label="explain_product",
            expected=expected_payload["explain_product_ids"],
            actual=actual_payload["explain_product_ids"],
        )
    )
    if expected_semantic["ambiguities"] != actual_semantic["ambiguities"]:
        codes.append("ambiguities_changed")
    if expected_semantic["unsupported_conditions"] != actual_semantic["unsupported_conditions"]:
        codes.append("unsupported_conditions_changed")
    return sorted(set(codes))


def _axis_value(result: CoverageCaseResult, axis: str) -> str | None:
    value: Any = getattr(result, axis)
    if value is None:
        return None
    return value.value if hasattr(value, "value") else str(value)


def _failure_buckets(
    results: Sequence[CoverageCaseResult],
) -> list[CoverageFailureBucket]:
    axes = (
        "product_family",
        "intent",
        "kind",
        "field",
        "operator",
        "direction",
        "function",
        "group_by",
    )
    grouped: dict[tuple[str, str], list[CoverageCaseResult]] = {}
    for result in results:
        for axis in axes:
            value = _axis_value(result, axis)
            if value is not None:
                grouped.setdefault((axis, value), []).append(result)

    buckets: list[CoverageFailureBucket] = []
    for (axis, value), members in grouped.items():
        failed = [item for item in members if not item.passed]
        if not failed:
            continue
        stages = Counter(
            item.first_failure_stage for item in failed if item.first_failure_stage is not None
        )
        buckets.append(
            CoverageFailureBucket(
                axis=axis,
                value=value,
                total=len(members),
                failed=len(failed),
                failure_rate=round(len(failed) / len(members), 6),
                first_failure_stages=dict(sorted(stages.items())),
                example_case_ids=[item.id for item in failed[:5]],
            )
        )
    return sorted(
        buckets,
        key=lambda item: (
            -item.failed,
            -item.failure_rate,
            item.axis,
            item.value,
        ),
    )


def analyze_coverage_report(
    suite: CoveragePlanSuite,
    report: CoverageRunReport,
    *,
    generated_at_utc: str | None = None,
) -> CoverageDiagnosisReport:
    suite_hash = coverage_plan_suite_semantic_sha256(suite)
    if report.plan_suite_id != suite.suite_id:
        raise ValueError("coverage report plan suite ID differs")
    if report.plan_suite_semantic_sha256 != suite_hash:
        raise ValueError("coverage report plan suite SHA-256 differs")
    expected_by_id = {case.id: case.plan for case in suite.cases}
    result_ids = [result.id for result in report.cases]
    if result_ids != [case.id for case in suite.cases]:
        raise ValueError("coverage report case order differs from the plan suite")

    delta_examples: dict[str, list[str]] = {}
    plan_delta_cases = 0
    for result in report.cases:
        codes = plan_delta_codes(expected_by_id[result.id], result.actual_plan)
        if codes:
            plan_delta_cases += 1
        for code in codes:
            delta_examples.setdefault(code, []).append(result.id)
    delta_buckets = [
        CoveragePlanDeltaBucket(
            code=code,
            count=len(case_ids),
            example_case_ids=case_ids[:5],
        )
        for code, case_ids in delta_examples.items()
    ]
    delta_buckets.sort(key=lambda item: (-item.count, item.code))

    failures_by_stage: dict[str, list[str]] = {}
    for result in report.cases:
        if result.first_failure_stage is not None:
            failures_by_stage.setdefault(result.first_failure_stage, []).append(result.id)
    stages = {stage: len(ids) for stage, ids in sorted(failures_by_stage.items())}
    passed = sum(result.passed for result in report.cases)
    timestamp = generated_at_utc or datetime.now(UTC).replace(microsecond=0).isoformat()
    return CoverageDiagnosisReport(
        diagnosis_id=f"{report.report_id}-diagnosis-v1",
        generated_at_utc=timestamp,
        plan_suite_id=suite.suite_id,
        plan_suite_semantic_sha256=suite_hash,
        source_report_id=report.report_id,
        source_agent_profile=report.agent_profile,
        summary=CoverageDiagnosisSummary(
            total=len(report.cases),
            passed=passed,
            failed=len(report.cases) - passed,
            first_failure_stages=stages,
            plan_delta_cases=plan_delta_cases,
            distinct_plan_delta_codes=len(delta_buckets),
        ),
        priority_buckets=_failure_buckets(report.cases),
        plan_delta_buckets=delta_buckets,
        failure_case_ids_by_stage=failures_by_stage,
        interpretation_limits=[
            *report.interpretation_limits,
            "우선순위는 실패 건수와 실패율의 기계적 정렬이며 사업 중요도 가중치는 없다.",
            "계획 차이는 정확히 같은 QueryPlan 계약을 기준으로 하며 "
            "동치 표현을 자동 합치지 않는다.",
            "이 진단은 공개 synthetic 질문의 사후 분석이며 독립 blind 성능이 아니다.",
        ],
    )
