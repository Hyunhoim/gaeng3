from __future__ import annotations

import hashlib
import json
import math
import time
from collections import Counter
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import Literal

from pydantic import Field

from finance_agent_core.agent import execute_answer_request
from finance_agent_core.contracts.backend import BackendAgentRequest
from finance_agent_core.contracts.queryplan import ProductFamily
from finance_agent_core.evaluation.metamorphic import (
    MetamorphicModel,
    MutationBatch,
    MutationCandidate,
    mutation_batch_semantic_sha256,
)
from finance_agent_core.evaluation.official_mock import (
    OfficialMockCase,
    load_official_mock_suite,
)
from finance_agent_core.evaluation.red_team_e2e import (
    ProviderCallSnapshot,
    ProviderTelemetry,
    RedTeamAttackClass,
    RedTeamCaseResult,
    RedTeamExpectation,
    RoutedAnswerService,
    _evaluate_case,
)


class MetamorphicExecutionCase(MetamorphicModel):
    id: str
    coverage_family: ProductFamily
    attack_class: RedTeamAttackClass
    question: str
    expectation: RedTeamExpectation


class MetamorphicVariantResult(MetamorphicModel):
    candidate: MutationCandidate
    source_semantic_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    variant_semantic_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    variant: RedTeamCaseResult | None
    checks: dict[str, bool]
    violations: list[str]
    passed: bool


class MetamorphicSummary(MetamorphicModel):
    source_total: int
    source_passed: int
    source_accuracy: float
    candidate_total: int
    candidate_accepted: int
    candidate_rejected: int
    candidate_executed: int
    candidate_passed: int
    candidate_strict_accuracy: float | None
    semantic_consistent: int
    semantic_consistency_rate: float | None
    safety_passed: int
    safety_pass_rate: float | None
    evidence_passed: int
    evidence_pass_rate: float | None
    axis_accuracy: dict[str, float | None]
    family_accuracy: dict[str, float | None]
    failure_clusters: dict[str, int]
    latency_ms: dict[str, float]
    perfect: bool


class MetamorphicReport(MetamorphicModel):
    schema_version: Literal["1.0"] = "1.0"
    report_id: str
    generated_at_utc: str
    generator: Literal["expected", "local_test"]
    generator_model: str | None
    agent_profile: Literal["expected", "local_test"]
    agent_model: str | None
    protocol_id: str
    protocol_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_suite_id: str
    source_suite_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    mutation_batch_semantic_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    database_sha256_by_family: dict[str, str]
    provider_calls: ProviderCallSnapshot
    summary: MetamorphicSummary
    source_results: list[RedTeamCaseResult]
    variants: list[MetamorphicVariantResult]
    interpretation_limits: list[str]


def _canonical_sha256(payload: object) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


def _system_semantics(result: RedTeamCaseResult) -> dict[str, object]:
    return {
        "backend_status": result.actual_backend_status.value,
        "interaction_intent": result.actual_interaction_intent.value,
        "product_families": [family.value for family in result.actual_product_families],
        "query_plan_intent": (
            None
            if result.actual_query_plan_intent is None
            else result.actual_query_plan_intent.value
        ),
        "candidate_count": result.actual_candidate_count,
        "product_ids": result.actual_product_ids,
        "comparison_fields": result.actual_comparison_fields,
        "aggregate_functions": result.actual_aggregate_functions,
        "answer_mode_class": (
            "control"
            if result.actual_backend_status.value in {"clarification", "unsupported", "not_found"}
            else "grounded"
        ),
    }


def _system_semantic_sha256(result: RedTeamCaseResult) -> str:
    return _canonical_sha256(_system_semantics(result))


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


def _group_accuracy(
    results: Sequence[MetamorphicVariantResult],
    *,
    attribute: Literal["axis", "coverage_family"],
) -> dict[str, float | None]:
    grouped: dict[str, list[bool]] = {}
    for result in results:
        value = getattr(result.candidate, attribute)
        grouped.setdefault(value.value, []).append(result.passed)
    return {
        key: None if not values else round(sum(values) / len(values), 6)
        for key, values in sorted(grouped.items())
    }


def _build_summary(
    source_results: Sequence[RedTeamCaseResult],
    variants: Sequence[MetamorphicVariantResult],
) -> MetamorphicSummary:
    accepted = [result for result in variants if result.candidate.validation.passed]
    executed = [result for result in accepted if result.variant is not None]
    passed = sum(result.passed for result in executed)
    consistent = sum(result.checks.get("system_semantics_equal", False) for result in executed)
    safety = sum(result.variant is not None and result.variant.safety_passed for result in executed)
    evidence = sum(
        result.variant is not None and result.variant.evidence_passed for result in executed
    )
    failure_clusters = Counter(
        "+".join(result.violations) if result.violations else "unclassified"
        for result in variants
        if not result.passed
    )
    latencies = [result.variant.latency_ms for result in executed if result.variant is not None]
    total_executed = len(executed)
    source_passed = sum(result.passed for result in source_results)
    return MetamorphicSummary(
        source_total=len(source_results),
        source_passed=source_passed,
        source_accuracy=round(source_passed / len(source_results), 6),
        candidate_total=len(variants),
        candidate_accepted=len(accepted),
        candidate_rejected=len(variants) - len(accepted),
        candidate_executed=total_executed,
        candidate_passed=passed,
        candidate_strict_accuracy=(
            None if not total_executed else round(passed / total_executed, 6)
        ),
        semantic_consistent=consistent,
        semantic_consistency_rate=(
            None if not total_executed else round(consistent / total_executed, 6)
        ),
        safety_passed=safety,
        safety_pass_rate=None if not total_executed else round(safety / total_executed, 6),
        evidence_passed=evidence,
        evidence_pass_rate=None if not total_executed else round(evidence / total_executed, 6),
        axis_accuracy=_group_accuracy(executed, attribute="axis"),
        family_accuracy=_group_accuracy(executed, attribute="coverage_family"),
        failure_clusters=dict(sorted(failure_clusters.items())),
        latency_ms={
            "min": 0.0 if not latencies else round(min(latencies), 3),
            "p50": _percentile(latencies, 0.5),
            "p95": _percentile(latencies, 0.95),
            "max": 0.0 if not latencies else round(max(latencies), 3),
        },
        perfect=(
            source_passed == len(source_results)
            and len(accepted) == len(variants)
            and passed == total_executed == len(variants)
        ),
    )


class MetamorphicRunner:
    def __init__(
        self,
        *,
        batch: MutationBatch,
        services: Mapping[ProductFamily, RoutedAnswerService],
        agent_profile: Literal["expected", "local_test"],
        database_sha256_by_family: dict[str, str],
        telemetry: ProviderTelemetry,
        agent_model: str | None,
    ) -> None:
        if set(services) != set(ProductFamily):
            raise ValueError("runner requires one service mapping per coverage family")
        source = load_official_mock_suite()
        if batch.source_suite_sha256 != source.sha256:
            raise ValueError("mutation batch source suite SHA-256 differs")
        self.batch = batch
        self.services = services
        self.agent_profile = agent_profile
        self.database_sha256_by_family = database_sha256_by_family
        self.telemetry = telemetry
        self.agent_model = agent_model
        self._source_by_id = {case.id: case for case in source.suite.cases}

    def _run_case(
        self,
        case: OfficialMockCase | MetamorphicExecutionCase,
    ) -> RedTeamCaseResult:
        request = BackendAgentRequest(request_id=case.id, question=case.question)
        started = time.perf_counter()
        adapter = execute_answer_request(self.services[case.coverage_family], request)
        latency_ms = round((time.perf_counter() - started) * 1000, 3)
        return _evaluate_case(  # type: ignore[arg-type]
            case,
            adapter,
            latency_ms,
            provider_plan=self.telemetry.query_plan(case.id),
        )

    def run(self, *, generated_at_utc: str | None = None) -> MetamorphicReport:
        source_ids = list(
            dict.fromkeys(candidate.source_case_id for candidate in self.batch.candidates)
        )
        source_results = [self._run_case(self._source_by_id[case_id]) for case_id in source_ids]
        source_results_by_id = {result.id: result for result in source_results}
        variants: list[MetamorphicVariantResult] = []
        for candidate in self.batch.candidates:
            source_case = self._source_by_id[candidate.source_case_id]
            source_result = source_results_by_id[candidate.source_case_id]
            source_semantic_sha256 = _system_semantic_sha256(source_result)
            if not candidate.validation.passed:
                variants.append(
                    MetamorphicVariantResult(
                        candidate=candidate,
                        source_semantic_sha256=source_semantic_sha256,
                        variant_semantic_sha256=None,
                        variant=None,
                        checks={
                            "source_expected_passed": source_result.passed,
                            "candidate_validation_passed": False,
                            "variant_expected_passed": False,
                            "system_semantics_equal": False,
                            "variant_safety_passed": False,
                            "variant_evidence_passed": False,
                        },
                        violations=["candidate_validation_failed"],
                        passed=False,
                    )
                )
                continue
            execution_case = MetamorphicExecutionCase(
                id=candidate.id,
                coverage_family=source_case.coverage_family,
                attack_class=source_case.attack_class,
                question=candidate.question,
                expectation=source_case.expectation,
            )
            variant = self._run_case(execution_case)
            variant_semantic_sha256 = _system_semantic_sha256(variant)
            checks = {
                "source_expected_passed": source_result.passed,
                "candidate_validation_passed": candidate.validation.passed,
                "variant_expected_passed": variant.passed,
                "system_semantics_equal": (variant_semantic_sha256 == source_semantic_sha256),
                "variant_safety_passed": variant.safety_passed,
                "variant_evidence_passed": variant.evidence_passed,
            }
            violations = [name for name, passed in checks.items() if not passed]
            variants.append(
                MetamorphicVariantResult(
                    candidate=candidate,
                    source_semantic_sha256=source_semantic_sha256,
                    variant_semantic_sha256=variant_semantic_sha256,
                    variant=variant,
                    checks=checks,
                    violations=violations,
                    passed=not violations,
                )
            )
        summary = _build_summary(source_results, variants)
        timestamp = generated_at_utc or datetime.now(UTC).replace(microsecond=0).isoformat()
        return MetamorphicReport(
            report_id=(
                f"qwen-eval-lab-v1-{self.batch.generator}-generator-{self.agent_profile}-agent"
            ),
            generated_at_utc=timestamp,
            generator=self.batch.generator,
            generator_model=self.batch.model,
            agent_profile=self.agent_profile,
            agent_model=self.agent_model,
            protocol_id=self.batch.protocol_id,
            protocol_sha256=self.batch.protocol_sha256,
            source_suite_id=self.batch.source_suite_id,
            source_suite_sha256=self.batch.source_suite_sha256,
            mutation_batch_semantic_sha256=mutation_batch_semantic_sha256(self.batch),
            database_sha256_by_family=self.database_sha256_by_family,
            provider_calls=self.telemetry.snapshot(),
            summary=summary,
            source_results=source_results,
            variants=variants,
            interpretation_limits=[
                *self.batch.interpretation_limits,
                "동일성 지문은 답변 문체를 제외한 실행 상태·상품·수치 구조만 비교함",
                "변형 실패는 Agent 결함과 생성 질문의 의미 변질을 구분해 검토해야 함",
            ],
        )
