from __future__ import annotations

import math
from collections import Counter
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import Field

from finance_agent_core.contracts.queryplan import ProductFamily
from finance_agent_core.evaluation.coverage_plan import (
    CoverageModel,
    CoveragePlanSuite,
    coverage_plan_suite_semantic_sha256,
)
from finance_agent_core.evaluation.coverage_questions import (
    CoverageQuestionBatch,
    CoverageQuestionCandidate,
    coverage_question_batch_semantic_sha256,
    merge_coverage_question_batches,
)
from finance_agent_core.evaluation.coverage_runner import (
    CoverageAgentProfile,
    CoverageCaseResult,
    run_coverage_question,
)
from finance_agent_core.evaluation.red_team_e2e import (
    ProviderCallSnapshot,
    ProviderTelemetry,
    RoutedAnswerService,
)


class CoverageQuestionVariantResult(CoverageModel):
    candidate: CoverageQuestionCandidate
    execution: CoverageCaseResult | None
    checks: dict[str, bool]
    violations: list[str]
    first_failure_stage: str | None
    passed: bool


class CoverageQuestionRunSummary(CoverageModel):
    requested: int = Field(ge=1)
    generated: int = Field(ge=0)
    generation_failures: int = Field(ge=0)
    accepted: int = Field(ge=0)
    rejected: int = Field(ge=0)
    generator_acceptance_rate: float = Field(ge=0, le=1)
    executed: int = Field(ge=0)
    passed: int = Field(ge=0)
    agent_strict_accuracy: float | None = Field(default=None, ge=0, le=1)
    end_to_end_yield: float = Field(ge=0, le=1)
    plan_semantic_rate: float | None = Field(default=None, ge=0, le=1)
    evidence_semantic_rate: float | None = Field(default=None, ge=0, le=1)
    fallback_count: int = Field(ge=0)
    generator_acceptance_by_axis: dict[str, float]
    agent_accuracy_by_axis: dict[str, float | None]
    agent_accuracy_by_family: dict[str, float | None]
    agent_accuracy_by_kind: dict[str, float | None]
    agent_accuracy_by_operator: dict[str, float | None]
    agent_accuracy_by_direction: dict[str, float | None]
    agent_accuracy_by_function: dict[str, float | None]
    first_failure_stages: dict[str, int]
    latency_ms: dict[str, float]
    perfect: bool


class CoverageQuestionRunReport(CoverageModel):
    schema_version: Literal["1.0"] = "1.0"
    report_id: str
    generated_at_utc: str
    status: Literal["internal_synthetic_not_blind"] = "internal_synthetic_not_blind"
    generator: Literal["expected", "local_test"]
    generator_model: str | None
    agent_profile: CoverageAgentProfile
    agent_model: str | None
    plan_suite_id: str
    plan_suite_semantic_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    question_batch_id: str
    question_batch_semantic_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    database_sha256_by_family: dict[str, str]
    provider_calls: ProviderCallSnapshot
    summary: CoverageQuestionRunSummary
    variants: list[CoverageQuestionVariantResult]
    interpretation_limits: list[str] = Field(min_length=1, max_length=20)


class CoverageQuestionShardReference(CoverageModel):
    question_batch_id: str
    question_batch_semantic_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    run_report_id: str


class CoverageQuestionCampaignReport(CoverageModel):
    schema_version: Literal["1.0"] = "1.0"
    campaign_id: str
    generated_at_utc: str
    status: Literal["internal_synthetic_not_blind"] = "internal_synthetic_not_blind"
    generator: Literal["expected", "local_test"]
    generator_model: str | None
    agent_profile: CoverageAgentProfile
    agent_model: str | None
    plan_suite_id: str
    plan_suite_semantic_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    database_sha256_by_family: dict[str, str]
    shards: list[CoverageQuestionShardReference] = Field(min_length=1)
    provider_calls: ProviderCallSnapshot
    summary: CoverageQuestionRunSummary
    variants: list[CoverageQuestionVariantResult]
    interpretation_limits: list[str] = Field(min_length=1, max_length=20)


def load_coverage_question_batch(path: str | Path) -> CoverageQuestionBatch:
    return CoverageQuestionBatch.model_validate_json(Path(path).read_text(encoding="utf-8"))


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


def _candidate_key(candidate: CoverageQuestionCandidate, attribute: str) -> str | None:
    if attribute == "axis":
        return candidate.axis.value
    value: Any = getattr(candidate.cell, attribute)
    if value is None:
        return None
    return value.value if hasattr(value, "value") else str(value)


def _generator_acceptance(
    variants: Sequence[CoverageQuestionVariantResult],
    attribute: str,
) -> dict[str, float]:
    grouped: dict[str, list[bool]] = {}
    for variant in variants:
        key = _candidate_key(variant.candidate, attribute)
        if key is not None:
            grouped.setdefault(key, []).append(variant.candidate.validation.passed)
    return {key: round(sum(values) / len(values), 6) for key, values in sorted(grouped.items())}


def _agent_accuracy(
    variants: Sequence[CoverageQuestionVariantResult],
    attribute: str,
) -> dict[str, float | None]:
    grouped: dict[str, list[bool]] = {}
    all_keys: set[str] = set()
    for variant in variants:
        key = _candidate_key(variant.candidate, attribute)
        if key is None:
            continue
        all_keys.add(key)
        if variant.candidate.validation.passed and variant.execution is not None:
            grouped.setdefault(key, []).append(variant.execution.passed)
    return {
        key: None if key not in grouped else round(sum(grouped[key]) / len(grouped[key]), 6)
        for key in sorted(all_keys)
    }


def _summary(
    batch: CoverageQuestionBatch,
    variants: Sequence[CoverageQuestionVariantResult],
) -> CoverageQuestionRunSummary:
    accepted = [variant for variant in variants if variant.candidate.validation.passed]
    executed = [variant for variant in accepted if variant.execution is not None]
    passed = sum(variant.execution is not None and variant.execution.passed for variant in executed)
    plan_passed = sum(
        variant.execution is not None and variant.execution.checks["query_plan_semantics_equal"]
        for variant in executed
    )
    evidence_passed = sum(
        variant.execution is not None and variant.execution.checks["evidence_semantics_equal"]
        for variant in executed
    )
    latencies = [
        variant.execution.latency_ms for variant in executed if variant.execution is not None
    ]
    first_failure_stages = Counter(
        variant.first_failure_stage
        for variant in variants
        if variant.first_failure_stage is not None
    )
    return CoverageQuestionRunSummary(
        requested=batch.requested_count,
        generated=batch.generated_count,
        generation_failures=batch.generation_failure_count,
        accepted=len(accepted),
        rejected=len(variants) - len(accepted),
        generator_acceptance_rate=(
            0.0 if not variants else round(len(accepted) / len(variants), 6)
        ),
        executed=len(executed),
        passed=passed,
        agent_strict_accuracy=(None if not executed else round(passed / len(executed), 6)),
        end_to_end_yield=(
            0.0 if not batch.requested_count else round(passed / batch.requested_count, 6)
        ),
        plan_semantic_rate=(None if not executed else round(plan_passed / len(executed), 6)),
        evidence_semantic_rate=(
            None if not executed else round(evidence_passed / len(executed), 6)
        ),
        fallback_count=sum(
            variant.execution is not None and variant.execution.fallback_used
            for variant in executed
        ),
        generator_acceptance_by_axis=_generator_acceptance(variants, "axis"),
        agent_accuracy_by_axis=_agent_accuracy(variants, "axis"),
        agent_accuracy_by_family=_agent_accuracy(variants, "product_family"),
        agent_accuracy_by_kind=_agent_accuracy(variants, "kind"),
        agent_accuracy_by_operator=_agent_accuracy(variants, "operator"),
        agent_accuracy_by_direction=_agent_accuracy(variants, "direction"),
        agent_accuracy_by_function=_agent_accuracy(variants, "function"),
        first_failure_stages=dict(sorted(first_failure_stages.items())),
        latency_ms={
            "min": 0.0 if not latencies else round(min(latencies), 3),
            "p50": _percentile(latencies, 0.5),
            "p95": _percentile(latencies, 0.95),
            "max": 0.0 if not latencies else round(max(latencies), 3),
        },
        perfect=(
            batch.generation_failure_count == 0
            and len(variants) == batch.requested_count
            and len(accepted) == len(variants)
            and len(executed) == len(accepted)
            and passed == len(executed)
        ),
    )


class CoverageQuestionRunner:
    def __init__(
        self,
        *,
        suite: CoveragePlanSuite,
        batch: CoverageQuestionBatch,
        services: Mapping[ProductFamily, RoutedAnswerService],
        agent_profile: CoverageAgentProfile,
        agent_model: str | None,
        telemetry: ProviderTelemetry,
    ) -> None:
        if set(services) != set(ProductFamily):
            raise ValueError("coverage question runner requires all four family services")
        if batch.plan_suite_id != suite.suite_id:
            raise ValueError("coverage question batch plan suite ID differs")
        if batch.plan_suite_semantic_sha256 != coverage_plan_suite_semantic_sha256(suite):
            raise ValueError("coverage question batch plan suite SHA-256 differs")
        self.suite = suite
        self.batch = batch
        self.services = services
        self.agent_profile = agent_profile
        self.agent_model = agent_model
        self.telemetry = telemetry
        self._cases = {case.id: case for case in suite.cases}
        for candidate in batch.candidates:
            try:
                source = self._cases[candidate.source_case_id]
            except KeyError as error:
                raise ValueError(
                    f"coverage candidate source case is missing: {candidate.source_case_id}"
                ) from error
            if candidate.cell != source.cell:
                raise ValueError("coverage candidate cell differs from source case")

    def run(self, *, generated_at_utc: str | None = None) -> CoverageQuestionRunReport:
        variants: list[CoverageQuestionVariantResult] = []
        for candidate in self.batch.candidates:
            if not candidate.validation.passed:
                variants.append(
                    CoverageQuestionVariantResult(
                        candidate=candidate,
                        execution=None,
                        checks={
                            "candidate_validation_passed": False,
                            "agent_strict_passed": False,
                        },
                        violations=["candidate_validation_failed"],
                        first_failure_stage="mutation_validation",
                        passed=False,
                    )
                )
                continue
            source = self._cases[candidate.source_case_id]
            execution = run_coverage_question(
                source,
                self.services[source.cell.product_family],
                question=candidate.question,
                request_id=candidate.id,
            )
            checks = {
                "candidate_validation_passed": True,
                "agent_strict_passed": execution.passed,
            }
            violations = [name for name, passed in checks.items() if not passed]
            variants.append(
                CoverageQuestionVariantResult(
                    candidate=candidate,
                    execution=execution,
                    checks=checks,
                    violations=violations,
                    first_failure_stage=execution.first_failure_stage,
                    passed=not violations,
                )
            )
        timestamp = generated_at_utc or datetime.now(UTC).replace(microsecond=0).isoformat()
        return CoverageQuestionRunReport(
            report_id=(
                f"{self.batch.protocol_id}-{self.batch.generator}-generator-"
                f"{self.agent_profile}-agent"
            ),
            generated_at_utc=timestamp,
            generator=self.batch.generator,
            generator_model=self.batch.model,
            agent_profile=self.agent_profile,
            agent_model=self.agent_model,
            plan_suite_id=self.suite.suite_id,
            plan_suite_semantic_sha256=coverage_plan_suite_semantic_sha256(self.suite),
            question_batch_id=self.batch.batch_id,
            question_batch_semantic_sha256=coverage_question_batch_semantic_sha256(self.batch),
            database_sha256_by_family=self.suite.database_sha256_by_family,
            provider_calls=self.telemetry.snapshot(),
            summary=_summary(self.batch, variants),
            variants=variants,
            interpretation_limits=[
                *self.batch.interpretation_limits,
                "Agent 정확도 분모는 기계 의미 보존 검사를 통과한 질문이다.",
                "end-to-end yield는 생성 요청 전체 중 strict 통과 비율이다.",
                "공개 데이터·자동 생성·사후 개선 실험이며 독립 blind가 아니다.",
            ],
        )


def _sum_provider_calls(
    reports: Sequence[CoverageQuestionRunReport],
) -> ProviderCallSnapshot:
    return ProviderCallSnapshot(
        query_plan_calls=sum(item.provider_calls.query_plan_calls for item in reports),
        query_plan_errors=sum(item.provider_calls.query_plan_errors for item in reports),
        query_plan_latency_ms=round(
            sum(item.provider_calls.query_plan_latency_ms for item in reports),
            3,
        ),
        answer_calls=sum(item.provider_calls.answer_calls for item in reports),
        answer_errors=sum(item.provider_calls.answer_errors for item in reports),
        answer_latency_ms=round(
            sum(item.provider_calls.answer_latency_ms for item in reports),
            3,
        ),
    )


def merge_coverage_question_run_reports(
    *,
    suite: CoveragePlanSuite,
    batches: Sequence[CoverageQuestionBatch],
    reports: Sequence[CoverageQuestionRunReport],
    generated_at_utc: str | None = None,
) -> CoverageQuestionCampaignReport:
    if not batches or len(batches) != len(reports):
        raise ValueError("coverage campaign requires one non-empty report per batch")
    merged_batch = merge_coverage_question_batches(batches)
    first_report = reports[0]
    suite_hash = coverage_plan_suite_semantic_sha256(suite)
    invariant_fields = (
        "schema_version",
        "status",
        "generator",
        "generator_model",
        "agent_profile",
        "agent_model",
        "plan_suite_id",
        "plan_suite_semantic_sha256",
        "database_sha256_by_family",
    )
    variant_by_id: dict[str, CoverageQuestionVariantResult] = {}
    shards: list[CoverageQuestionShardReference] = []
    for batch, report in zip(batches, reports, strict=True):
        for field_name in invariant_fields:
            if getattr(report, field_name) != getattr(first_report, field_name):
                raise ValueError(f"coverage run report {field_name} differs")
        if report.plan_suite_id != suite.suite_id:
            raise ValueError("coverage run report plan suite ID differs")
        if report.plan_suite_semantic_sha256 != suite_hash:
            raise ValueError("coverage run report plan suite SHA-256 differs")
        expected_batch_hash = coverage_question_batch_semantic_sha256(batch)
        if report.question_batch_semantic_sha256 != expected_batch_hash:
            raise ValueError("coverage run report question batch SHA-256 differs")
        if report.generator != batch.generator or report.generator_model != batch.model:
            raise ValueError("coverage run report generator differs from its batch")
        expected_candidate_ids = [item.id for item in batch.candidates]
        observed_candidate_ids = [item.candidate.id for item in report.variants]
        if observed_candidate_ids != expected_candidate_ids:
            raise ValueError("coverage run report variants differ from its batch")
        for variant in report.variants:
            if variant.candidate != next(
                item for item in batch.candidates if item.id == variant.candidate.id
            ):
                raise ValueError("coverage run report candidate payload differs")
            if variant.candidate.id in variant_by_id:
                raise ValueError("coverage campaign contains duplicate candidate IDs")
            variant_by_id[variant.candidate.id] = variant
        shards.append(
            CoverageQuestionShardReference(
                question_batch_id=batch.batch_id,
                question_batch_semantic_sha256=expected_batch_hash,
                run_report_id=report.report_id,
            )
        )
    ordered_variants = [variant_by_id[item.id] for item in merged_batch.candidates]
    timestamp = generated_at_utc or datetime.now(UTC).replace(microsecond=0).isoformat()
    return CoverageQuestionCampaignReport(
        campaign_id=(
            f"{merged_batch.protocol_id}-{merged_batch.generator}-generator-"
            f"{first_report.agent_profile}-agent-campaign"
        ),
        generated_at_utc=timestamp,
        generator=merged_batch.generator,
        generator_model=merged_batch.model,
        agent_profile=first_report.agent_profile,
        agent_model=first_report.agent_model,
        plan_suite_id=suite.suite_id,
        plan_suite_semantic_sha256=suite_hash,
        database_sha256_by_family=suite.database_sha256_by_family,
        shards=shards,
        provider_calls=_sum_provider_calls(reports),
        summary=_summary(merged_batch, ordered_variants),
        variants=ordered_variants,
        interpretation_limits=[
            *merged_batch.interpretation_limits,
            "각 shard의 질문 batch hash와 실행 report를 일대일로 검증한 뒤 합쳤다.",
            "캠페인 집계도 자동 생성·공개 데이터·사후 분석이며 독립 blind가 아니다.",
        ],
    )
