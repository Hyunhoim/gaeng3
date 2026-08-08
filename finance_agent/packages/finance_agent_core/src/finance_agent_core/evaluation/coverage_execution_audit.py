from __future__ import annotations

from collections import Counter
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import Field

from finance_agent_core.contracts.queryplan import Intent, QueryPlan
from finance_agent_core.evaluation.coverage_analysis import plan_delta_codes
from finance_agent_core.evaluation.coverage_plan import (
    CoverageModel,
    CoveragePlanSuite,
    coverage_plan_suite_semantic_sha256,
)
from finance_agent_core.evaluation.coverage_question_runner import (
    CoverageQuestionCampaignReport,
    CoverageQuestionVariantResult,
)
from finance_agent_core.evaluation.semantics import query_plan_semantic_payload

_EFFECTIVE_CHECK_NAMES = (
    "executed",
    "family_exact",
    "intent_exact",
    "candidate_count_equal",
    "product_ids_equal",
    "evidence_semantics_equal",
    "no_fallback",
)


class CoverageExecutionAuditCase(CoverageModel):
    id: str
    source_case_id: str
    kind: str
    intent: Intent
    exact_plan_passed: bool
    execution_semantic_plan_passed: bool
    evidence_semantic_passed: bool
    exact_strict_passed: bool
    execution_semantic_strict_passed: bool
    classification: Literal["exact_pass", "execution_inert_upgrade", "still_failed"]
    ignored_plan_differences: list[str]
    exact_plan_delta_codes: list[str]
    residual_failed_checks: list[str]


class CoverageExecutionAuditBucket(CoverageModel):
    total: int = Field(ge=1)
    exact_plan_passed: int = Field(ge=0)
    execution_semantic_plan_passed: int = Field(ge=0)
    exact_strict_passed: int = Field(ge=0)
    execution_semantic_strict_passed: int = Field(ge=0)
    execution_inert_upgrades: int = Field(ge=0)
    exact_strict_rate: float = Field(ge=0, le=1)
    execution_semantic_strict_rate: float = Field(ge=0, le=1)


class CoverageExecutionAuditSummary(CoverageExecutionAuditBucket):
    source_variants: int = Field(ge=1)
    rejected_not_audited: int = Field(ge=0)
    still_failed: int = Field(ge=0)
    by_kind: dict[str, CoverageExecutionAuditBucket]
    residual_failed_checks: dict[str, int]


class CoverageExecutionAuditReport(CoverageModel):
    schema_version: Literal["1.0"] = "1.0"
    audit_id: str
    generated_at_utc: str
    status: Literal["internal_synthetic_not_blind"] = "internal_synthetic_not_blind"
    audit_policy: Literal["aggregate_execution_inert_v1"] = "aggregate_execution_inert_v1"
    source_report_id: str
    source_report_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_agent_profile: str
    source_agent_model: str | None
    plan_suite_id: str
    plan_suite_semantic_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    summary: CoverageExecutionAuditSummary
    cases: list[CoverageExecutionAuditCase]
    policy_notes: list[str] = Field(min_length=1, max_length=20)
    interpretation_limits: list[str] = Field(min_length=1, max_length=20)


def query_plan_execution_semantic_payload(plan: QueryPlan) -> dict[str, Any]:
    """Return a conservative execution-level payload for post-hoc diagnosis.

    The primary exact QueryPlan contract remains unchanged. Only fields proven inert
    by the current aggregate executor are normalized here:

    * aggregate ``projection`` is not consumed by the verifier projection builder;
      required group and aggregation fields are derived from ``intent_payload``;
    * a positive ``limit`` cannot change a non-grouped aggregate's single result.

    Grouped aggregate limits and every field for other intents stay exact.
    """

    payload = query_plan_semantic_payload(plan)
    if plan.intent is not Intent.AGGREGATE:
        return payload

    required_projection = {
        *plan.intent_payload.group_by,
        *(aggregation.field for aggregation in plan.intent_payload.aggregations),
    }
    payload["projection"] = sorted(required_projection)
    if not plan.intent_payload.group_by:
        payload.pop("limit", None)
    return payload


def execution_semantic_plan_equal(expected: QueryPlan, actual: QueryPlan | None) -> bool:
    if actual is None:
        return False
    return query_plan_execution_semantic_payload(expected) == query_plan_execution_semantic_payload(
        actual
    )


def _ignored_plan_differences(expected: QueryPlan, actual: QueryPlan | None) -> list[str]:
    if (
        actual is None
        or expected.intent is not Intent.AGGREGATE
        or actual.intent is not Intent.AGGREGATE
    ):
        return []
    expected_payload = query_plan_semantic_payload(expected)
    actual_payload = query_plan_semantic_payload(actual)
    ignored: list[str] = []
    if expected_payload["projection"] != actual_payload["projection"]:
        ignored.append("aggregate_projection_execution_inert")
    if (
        not expected.intent_payload.group_by
        and not actual.intent_payload.group_by
        and expected_payload["limit"] != actual_payload["limit"]
    ):
        ignored.append("aggregate_nongrouped_limit_execution_inert")
    return ignored


def _effective_checks(
    variant: CoverageQuestionVariantResult,
    *,
    plan_equal: bool,
) -> dict[str, bool]:
    execution = variant.execution
    if execution is None:
        return {name: False for name in _EFFECTIVE_CHECK_NAMES} | {
            "execution_semantic_plan_equal": False,
        }
    return {name: bool(execution.checks[name]) for name in _EFFECTIVE_CHECK_NAMES} | {
        "execution_semantic_plan_equal": plan_equal
    }


def _bucket(cases: Sequence[CoverageExecutionAuditCase]) -> CoverageExecutionAuditBucket:
    total = len(cases)
    if total < 1:
        raise ValueError("coverage execution audit bucket cannot be empty")
    exact_strict = sum(item.exact_strict_passed for item in cases)
    effective_strict = sum(item.execution_semantic_strict_passed for item in cases)
    return CoverageExecutionAuditBucket(
        total=total,
        exact_plan_passed=sum(item.exact_plan_passed for item in cases),
        execution_semantic_plan_passed=sum(item.execution_semantic_plan_passed for item in cases),
        exact_strict_passed=exact_strict,
        execution_semantic_strict_passed=effective_strict,
        execution_inert_upgrades=sum(
            item.classification == "execution_inert_upgrade" for item in cases
        ),
        exact_strict_rate=round(exact_strict / total, 6),
        execution_semantic_strict_rate=round(effective_strict / total, 6),
    )


def audit_coverage_execution_semantics(
    suite: CoveragePlanSuite,
    report: CoverageQuestionCampaignReport,
    *,
    source_report_sha256: str,
    generated_at_utc: str | None = None,
) -> CoverageExecutionAuditReport:
    suite_hash = coverage_plan_suite_semantic_sha256(suite)
    if report.plan_suite_id != suite.suite_id:
        raise ValueError("coverage execution audit plan suite ID differs")
    if report.plan_suite_semantic_sha256 != suite_hash:
        raise ValueError("coverage execution audit plan suite SHA-256 differs")

    cases_by_id = {case.id: case for case in suite.cases}
    audited: list[CoverageExecutionAuditCase] = []
    for variant in report.variants:
        if not variant.candidate.validation.passed:
            continue
        try:
            source = cases_by_id[variant.candidate.source_case_id]
        except KeyError as error:
            raise ValueError(
                f"coverage execution audit source case missing: {variant.candidate.source_case_id}"
            ) from error
        if variant.candidate.cell != source.cell:
            raise ValueError("coverage execution audit candidate cell differs from source")

        execution = variant.execution
        actual_plan = None if execution is None else execution.actual_plan
        exact_plan = actual_plan is not None and query_plan_semantic_payload(
            source.plan
        ) == query_plan_semantic_payload(actual_plan)
        if execution is not None and exact_plan != bool(
            execution.checks["query_plan_semantics_equal"]
        ):
            raise ValueError(
                f"coverage execution audit exact plan check differs: {variant.candidate.id}"
            )
        effective_plan = execution_semantic_plan_equal(source.plan, actual_plan)
        effective_checks = _effective_checks(variant, plan_equal=effective_plan)
        effective_strict = all(effective_checks.values())
        exact_strict = bool(execution is not None and execution.passed)
        if exact_strict:
            classification = "exact_pass"
        elif effective_strict:
            classification = "execution_inert_upgrade"
        else:
            classification = "still_failed"
        audited.append(
            CoverageExecutionAuditCase(
                id=variant.candidate.id,
                source_case_id=source.id,
                kind=source.cell.kind.value,
                intent=source.plan.intent,
                exact_plan_passed=exact_plan,
                execution_semantic_plan_passed=effective_plan,
                evidence_semantic_passed=bool(
                    execution is not None and execution.checks["evidence_semantics_equal"]
                ),
                exact_strict_passed=exact_strict,
                execution_semantic_strict_passed=effective_strict,
                classification=classification,
                ignored_plan_differences=_ignored_plan_differences(
                    source.plan,
                    actual_plan,
                ),
                exact_plan_delta_codes=plan_delta_codes(source.plan, actual_plan),
                residual_failed_checks=[
                    name for name, passed in effective_checks.items() if not passed
                ],
            )
        )
    if not audited:
        raise ValueError("coverage execution audit has no accepted variants")

    grouped: dict[str, list[CoverageExecutionAuditCase]] = {}
    for item in audited:
        grouped.setdefault(item.kind, []).append(item)
    overall = _bucket(audited)
    residual = Counter(check for item in audited for check in item.residual_failed_checks)
    timestamp = generated_at_utc or datetime.now(UTC).replace(microsecond=0).isoformat()
    return CoverageExecutionAuditReport(
        audit_id=f"{report.campaign_id}-aggregate-execution-inert-v1-audit",
        generated_at_utc=timestamp,
        source_report_id=report.campaign_id,
        source_report_sha256=source_report_sha256,
        source_agent_profile=report.agent_profile,
        source_agent_model=report.agent_model,
        plan_suite_id=suite.suite_id,
        plan_suite_semantic_sha256=suite_hash,
        summary=CoverageExecutionAuditSummary(
            **overall.model_dump(),
            source_variants=len(report.variants),
            rejected_not_audited=len(report.variants) - len(audited),
            still_failed=sum(item.classification == "still_failed" for item in audited),
            by_kind={key: _bucket(values) for key, values in sorted(grouped.items())},
            residual_failed_checks=dict(sorted(residual.items())),
        ),
        cases=audited,
        policy_notes=[
            "기존 QueryPlan exact strict 결과는 수정하거나 덮어쓰지 않는다.",
            (
                "AGGREGATE projection은 실행기가 intent_payload에서 필요한 필드를 "
                "다시 구성하므로 정규화한다."
            ),
            "group_by가 없는 AGGREGATE는 항상 단일 결과이므로 양의 limit 차이만 정규화한다.",
            "group_by가 있는 AGGREGATE의 limit과 검색·비교 계획의 모든 필드는 그대로 비교한다.",
            "집계 함수·대상·그룹·조건·상품군 차이는 절대로 정규화하지 않는다.",
        ],
        interpretation_limits=[
            "공개 데이터와 자동 생성 질문을 사용한 사후 개발 진단이며 독립 blind 평가가 아니다.",
            "execution-semantic strict는 현재 실행기 구현에 한정된 보조 지표다.",
            "공식 점수나 HyperCLOVA X 성능으로 해석할 수 없다.",
        ],
    )
