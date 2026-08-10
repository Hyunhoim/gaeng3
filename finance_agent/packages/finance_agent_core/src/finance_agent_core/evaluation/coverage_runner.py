from __future__ import annotations

import hashlib
import math
import time
from collections import Counter
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import Field

from finance_agent_core.agent.routed_service import RoutedAgentResult
from finance_agent_core.contracts.queryplan import Intent, ProductFamily, QueryPlan
from finance_agent_core.evaluation.coverage_plan import (
    CoverageCellKind,
    CoverageModel,
    CoverageOutcome,
    CoveragePlanCase,
    CoveragePlanSuite,
    _outcome_payload,
    coverage_plan_suite_semantic_sha256,
)
from finance_agent_core.evaluation.red_team_e2e import (
    ProviderCallSnapshot,
    ProviderTelemetry,
    RoutedAnswerService,
)
from finance_agent_core.evaluation.semantics import (
    canonical_json_sha256,
    query_plan_semantic_sha256,
)

type CoverageAgentProfile = Literal[
    "expected",
    "local_test_plan_only",
    "local_test_answer_only",
    "local_test",
    "local_test_grounded_plan_only",
    "local_test_grounded",
]


class CoverageCaseResult(CoverageModel):
    id: str
    question: str
    product_family: ProductFamily
    intent: Intent
    kind: CoverageCellKind
    field: str
    operator: str | None
    direction: str | None
    function: str | None
    group_by: str | None
    expected_outcome: CoverageOutcome
    actual_status: str
    actual_intent: Intent | None
    actual_product_families: list[ProductFamily]
    actual_plan: QueryPlan | None
    actual_outcome: CoverageOutcome | None
    answer_mode: str
    fallback_used: bool
    latency_ms: float = Field(ge=0)
    checks: dict[str, bool]
    violations: list[str]
    first_failure_stage: str | None
    passed: bool


class CoverageRunSummary(CoverageModel):
    total: int = Field(ge=1)
    passed: int = Field(ge=0)
    strict_accuracy: float = Field(ge=0, le=1)
    executed: int = Field(ge=0)
    execution_rate: float = Field(ge=0, le=1)
    plan_semantic_passed: int = Field(ge=0)
    plan_semantic_rate: float = Field(ge=0, le=1)
    evidence_semantic_passed: int = Field(ge=0)
    evidence_semantic_rate: float = Field(ge=0, le=1)
    fallback_count: int = Field(ge=0)
    by_family: dict[str, float]
    by_intent: dict[str, float]
    by_kind: dict[str, float]
    by_operator: dict[str, float]
    by_direction: dict[str, float]
    by_function: dict[str, float]
    first_failure_stages: dict[str, int]
    latency_ms: dict[str, float]
    perfect: bool


class CoverageRunReport(CoverageModel):
    schema_version: Literal["1.0"] = "1.0"
    report_id: str
    generated_at_utc: str
    status: Literal["internal_synthetic_not_blind"] = "internal_synthetic_not_blind"
    agent_profile: CoverageAgentProfile
    agent_model: str | None
    plan_suite_id: str
    plan_suite_semantic_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    database_sha256_by_family: dict[str, str]
    provider_calls: ProviderCallSnapshot
    summary: CoverageRunSummary
    cases: list[CoverageCaseResult]
    interpretation_limits: list[str] = Field(min_length=1, max_length=20)


def load_coverage_plan_suite(path: str | Path) -> CoveragePlanSuite:
    return CoveragePlanSuite.model_validate_json(Path(path).read_text(encoding="utf-8"))


def verify_coverage_databases(
    suite: CoveragePlanSuite,
    database_paths: Mapping[ProductFamily | str, str | Path],
) -> dict[str, str]:
    normalized = {ProductFamily(family): Path(path) for family, path in database_paths.items()}
    if set(normalized) != set(ProductFamily):
        raise ValueError("coverage run requires all four database paths")
    observed: dict[str, str] = {}
    for family in ProductFamily:
        digest = hashlib.sha256(normalized[family].read_bytes()).hexdigest()
        expected = suite.database_sha256_by_family[family.value]
        if digest != expected:
            raise ValueError(
                f"{family.value} database SHA-256 differs: expected {expected}, got {digest}"
            )
        observed[family.value] = digest
    return observed


def _actual_outcome(
    result: RoutedAgentResult,
    *,
    latency_ms: float,
) -> CoverageOutcome | None:
    if (
        result.status != "executed"
        or result.query_plan is None
        or result.candidate_count is None
        or result.source_manifest is None
        or result.family_searches
    ):
        return None
    evidence_payload = _outcome_payload(
        candidate_count=result.candidate_count,
        products=result.products,
        comparisons=result.comparisons,
        aggregates=result.aggregates,
        source_manifest=result.source_manifest,
    )
    plan_sha = query_plan_semantic_sha256(result.query_plan)
    assert plan_sha is not None
    evidence_sha = canonical_json_sha256(evidence_payload)
    return CoverageOutcome(
        candidate_count=result.candidate_count,
        returned_product_ids=[item.product_id for item in result.products],
        product_evidence_count=len(result.products),
        comparison_evidence_count=len(result.comparisons),
        aggregate_evidence_count=len(result.aggregates),
        query_plan_semantic_sha256=plan_sha,
        evidence_semantic_sha256=evidence_sha,
        system_semantic_sha256=canonical_json_sha256(
            {
                "query_plan": plan_sha,
                "evidence": evidence_sha,
            }
        ),
        source_dataset=result.source_manifest.dataset,
        source_snapshot_date=result.source_manifest.source_snapshot_date.isoformat(),
        latency_ms=latency_ms,
    )


def _first_failure_stage(checks: Mapping[str, bool]) -> str | None:
    stages = (
        ("routing", ("executed", "family_exact", "intent_exact")),
        ("planning", ("query_plan_semantics_equal",)),
        ("retrieval", ("candidate_count_equal", "product_ids_equal")),
        ("evidence", ("evidence_semantics_equal", "system_semantics_equal")),
        ("answer", ("no_fallback",)),
    )
    for stage, names in stages:
        if any(not checks[name] for name in names):
            return stage
    return None


def run_coverage_question(
    case: CoveragePlanCase,
    service: RoutedAnswerService,
    *,
    question: str,
    request_id: str,
) -> CoverageCaseResult:
    started = time.perf_counter()
    try:
        result = service.answer(question, request_id)
        latency_ms = round((time.perf_counter() - started) * 1000, 3)
    except Exception as error:
        latency_ms = round((time.perf_counter() - started) * 1000, 3)
        checks = {
            "executed": False,
            "family_exact": False,
            "intent_exact": False,
            "query_plan_semantics_equal": False,
            "candidate_count_equal": False,
            "product_ids_equal": False,
            "evidence_semantics_equal": False,
            "system_semantics_equal": False,
            "no_fallback": True,
        }
        return CoverageCaseResult(
            id=request_id,
            question=question,
            product_family=case.cell.product_family,
            intent=case.cell.intent,
            kind=case.cell.kind,
            field=case.cell.field,
            operator=None if case.cell.operator is None else case.cell.operator.value,
            direction=None if case.cell.direction is None else case.cell.direction.value,
            function=None if case.cell.function is None else case.cell.function.value,
            group_by=case.cell.group_by,
            expected_outcome=case.outcome,
            actual_status=f"exception:{type(error).__name__}:{error}",
            actual_intent=None,
            actual_product_families=[],
            actual_plan=None,
            actual_outcome=None,
            answer_mode="exception",
            fallback_used=False,
            latency_ms=latency_ms,
            checks=checks,
            violations=[name for name, passed in checks.items() if not passed],
            first_failure_stage="routing",
            passed=False,
        )
    actual = _actual_outcome(result, latency_ms=latency_ms)
    actual_intent = None if result.query_plan is None else result.query_plan.intent
    fallback_used = bool(
        result.answer_composition is not None
        and result.answer_composition.mode == "deterministic_fallback"
    )
    answer_mode = (
        "deterministic" if result.answer_composition is None else result.answer_composition.mode
    )
    expected = case.outcome
    checks = {
        "executed": result.status == "executed" and actual is not None,
        "family_exact": result.decision.draft.product_families == [case.cell.product_family],
        "intent_exact": actual_intent is case.cell.intent,
        "query_plan_semantics_equal": (
            actual is not None
            and actual.query_plan_semantic_sha256 == expected.query_plan_semantic_sha256
        ),
        "candidate_count_equal": (
            actual is not None and actual.candidate_count == expected.candidate_count
        ),
        "product_ids_equal": (
            actual is not None and actual.returned_product_ids == expected.returned_product_ids
        ),
        "evidence_semantics_equal": (
            actual is not None
            and actual.evidence_semantic_sha256 == expected.evidence_semantic_sha256
        ),
        "system_semantics_equal": (
            actual is not None and actual.system_semantic_sha256 == expected.system_semantic_sha256
        ),
        "no_fallback": not fallback_used,
    }
    violations = [name for name, passed in checks.items() if not passed]
    return CoverageCaseResult(
        id=request_id,
        question=question,
        product_family=case.cell.product_family,
        intent=case.cell.intent,
        kind=case.cell.kind,
        field=case.cell.field,
        operator=None if case.cell.operator is None else case.cell.operator.value,
        direction=None if case.cell.direction is None else case.cell.direction.value,
        function=None if case.cell.function is None else case.cell.function.value,
        group_by=case.cell.group_by,
        expected_outcome=case.outcome,
        actual_status=result.status,
        actual_intent=actual_intent,
        actual_product_families=result.decision.draft.product_families,
        actual_plan=result.query_plan,
        actual_outcome=actual,
        answer_mode=answer_mode,
        fallback_used=fallback_used,
        latency_ms=latency_ms,
        checks=checks,
        violations=violations,
        first_failure_stage=_first_failure_stage(checks),
        passed=not violations,
    )


def _percentile(values: Sequence[float], quantile: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = (len(ordered) - 1) * quantile
    lower = math.floor(index)
    upper = math.ceil(index)
    if lower == upper:
        return round(ordered[lower], 3)
    fraction = index - lower
    return round(ordered[lower] + (ordered[upper] - ordered[lower]) * fraction, 3)


def _accuracy(
    results: Sequence[CoverageCaseResult],
    attribute: Literal[
        "product_family",
        "intent",
        "kind",
        "operator",
        "direction",
        "function",
    ],
) -> dict[str, float]:
    grouped: dict[str, list[bool]] = {}
    for result in results:
        value: Any = getattr(result, attribute)
        if value is None:
            continue
        key = value.value if hasattr(value, "value") else str(value)
        grouped.setdefault(key, []).append(result.passed)
    return {key: round(sum(values) / len(values), 6) for key, values in sorted(grouped.items())}


def _summary(results: Sequence[CoverageCaseResult]) -> CoverageRunSummary:
    total = len(results)
    passed = sum(result.passed for result in results)
    executed = sum(result.checks["executed"] for result in results)
    plan_passed = sum(result.checks["query_plan_semantics_equal"] for result in results)
    evidence_passed = sum(result.checks["evidence_semantics_equal"] for result in results)
    latencies = [result.latency_ms for result in results]
    return CoverageRunSummary(
        total=total,
        passed=passed,
        strict_accuracy=round(passed / total, 6),
        executed=executed,
        execution_rate=round(executed / total, 6),
        plan_semantic_passed=plan_passed,
        plan_semantic_rate=round(plan_passed / total, 6),
        evidence_semantic_passed=evidence_passed,
        evidence_semantic_rate=round(evidence_passed / total, 6),
        fallback_count=sum(result.fallback_used for result in results),
        by_family=_accuracy(results, "product_family"),
        by_intent=_accuracy(results, "intent"),
        by_kind=_accuracy(results, "kind"),
        by_operator=_accuracy(results, "operator"),
        by_direction=_accuracy(results, "direction"),
        by_function=_accuracy(results, "function"),
        first_failure_stages=dict(
            sorted(
                Counter(
                    result.first_failure_stage
                    for result in results
                    if result.first_failure_stage is not None
                ).items()
            )
        ),
        latency_ms={
            "min": round(min(latencies), 3),
            "p50": _percentile(latencies, 0.5),
            "p95": _percentile(latencies, 0.95),
            "max": round(max(latencies), 3),
        },
        perfect=passed == total,
    )


class CoverageRunner:
    def __init__(
        self,
        *,
        suite: CoveragePlanSuite,
        services: Mapping[ProductFamily, RoutedAnswerService],
        agent_profile: CoverageAgentProfile,
        agent_model: str | None,
        telemetry: ProviderTelemetry,
    ) -> None:
        if set(services) != set(ProductFamily):
            raise ValueError("coverage runner requires all four family services")
        self.suite = suite
        self.services = services
        self.agent_profile = agent_profile
        self.agent_model = agent_model
        self.telemetry = telemetry

    def run(self, *, generated_at_utc: str | None = None) -> CoverageRunReport:
        results = [
            run_coverage_question(
                case,
                self.services[case.cell.product_family],
                question=case.canonical_question,
                request_id=case.id,
            )
            for case in self.suite.cases
        ]
        timestamp = generated_at_utc or datetime.now(UTC).replace(microsecond=0).isoformat()
        return CoverageRunReport(
            report_id=f"{self.suite.suite_id}-{self.agent_profile}-canonical",
            generated_at_utc=timestamp,
            agent_profile=self.agent_profile,
            agent_model=self.agent_model,
            plan_suite_id=self.suite.suite_id,
            plan_suite_semantic_sha256=coverage_plan_suite_semantic_sha256(self.suite),
            database_sha256_by_family=self.suite.database_sha256_by_family,
            provider_calls=self.telemetry.snapshot(),
            summary=_summary(results),
            cases=results,
            interpretation_limits=[
                *self.suite.interpretation_limits,
                "canonical 질문 최초 실행은 규칙 기반 문장에 대한 개발 진단이다.",
                "strict 성공은 계획 의미와 전체 evidence 지문이 직접 실행 정답과 같음을 뜻한다.",
                "답변 문장의 자연스러움과 금융 유용성은 이 보고서에서 채점하지 않는다.",
            ],
        )
