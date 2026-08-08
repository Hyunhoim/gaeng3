from __future__ import annotations

import math
from collections import Counter
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import Literal

from pydantic import Field

from finance_agent_core.evaluation.coverage_plan import CoverageModel
from finance_agent_core.evaluation.coverage_question_runner import (
    CoverageQuestionCampaignReport,
    CoverageQuestionRunReport,
)
from finance_agent_core.evaluation.coverage_runner import CoverageRunReport
from finance_agent_core.evaluation.red_team_e2e import ProviderCallSnapshot
from finance_agent_core.evaluation.semantics import canonical_json_sha256

type ComparableCoverageReport = (
    CoverageRunReport | CoverageQuestionRunReport | CoverageQuestionCampaignReport
)


class CoverageObservation(CoverageModel):
    id: str
    question: str
    passed: bool
    plan_semantics_equal: bool
    evidence_semantics_equal: bool
    fallback_used: bool
    first_failure_stage: str | None
    latency_ms: float = Field(ge=0)


class CoverageProfileSnapshot(CoverageModel):
    label: str
    agent_profile: str
    agent_model: str | None
    total: int = Field(ge=1)
    passed: int = Field(ge=0)
    strict_accuracy: float = Field(ge=0, le=1)
    plan_semantic_passed: int = Field(ge=0)
    plan_semantic_rate: float = Field(ge=0, le=1)
    evidence_semantic_passed: int = Field(ge=0)
    evidence_semantic_rate: float = Field(ge=0, le=1)
    fallback_count: int = Field(ge=0)
    first_failure_stages: dict[str, int]
    latency_ms: dict[str, float]
    provider_calls: ProviderCallSnapshot


class CoverageProviderCallDelta(CoverageModel):
    query_plan_calls: int
    query_plan_errors: int
    query_plan_latency_ms: float
    answer_calls: int
    answer_errors: int
    answer_latency_ms: float


class CoveragePairwiseDelta(CoverageModel):
    baseline_label: str
    candidate_label: str
    total: int = Field(ge=1)
    strict_accuracy_delta: float = Field(ge=-1, le=1)
    plan_semantic_rate_delta: float = Field(ge=-1, le=1)
    evidence_semantic_rate_delta: float = Field(ge=-1, le=1)
    rescued: int = Field(ge=0)
    regressed: int = Field(ge=0)
    unchanged_pass: int = Field(ge=0)
    unchanged_fail: int = Field(ge=0)
    plan_rescued: int = Field(ge=0)
    plan_regressed: int = Field(ge=0)
    evidence_rescued: int = Field(ge=0)
    evidence_regressed: int = Field(ge=0)
    rescued_case_ids: list[str]
    regressed_case_ids: list[str]
    stage_transitions: dict[str, int]
    provider_call_delta: CoverageProviderCallDelta
    latency_delta_ms: dict[str, float]


class CoverageAblationReport(CoverageModel):
    schema_version: Literal["1.0"] = "1.0"
    ablation_id: str
    generated_at_utc: str
    status: Literal["internal_synthetic_not_blind"] = "internal_synthetic_not_blind"
    source_kind: Literal["canonical", "naturalized"]
    source_semantic_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    baseline_label: str
    profiles: list[CoverageProfileSnapshot] = Field(min_length=2)
    pairwise_deltas: list[CoveragePairwiseDelta] = Field(min_length=1)
    interpretation_limits: list[str] = Field(min_length=1, max_length=20)


def _percentile(values: Sequence[float], quantile: float) -> float:
    ordered = sorted(values)
    index = (len(ordered) - 1) * quantile
    lower = math.floor(index)
    upper = math.ceil(index)
    if lower == upper:
        return round(ordered[lower], 3)
    fraction = index - lower
    return round(ordered[lower] + (ordered[upper] - ordered[lower]) * fraction, 3)


def _report_kind(report: ComparableCoverageReport) -> Literal["canonical", "naturalized"]:
    return "canonical" if isinstance(report, CoverageRunReport) else "naturalized"


def _observations(report: ComparableCoverageReport) -> list[CoverageObservation]:
    if isinstance(report, CoverageRunReport):
        return [
            CoverageObservation(
                id=item.id,
                question=item.question,
                passed=item.passed,
                plan_semantics_equal=item.checks["query_plan_semantics_equal"],
                evidence_semantics_equal=item.checks["evidence_semantics_equal"],
                fallback_used=item.fallback_used,
                first_failure_stage=item.first_failure_stage,
                latency_ms=item.latency_ms,
            )
            for item in report.cases
        ]
    observations: list[CoverageObservation] = []
    for item in report.variants:
        execution = item.execution
        observations.append(
            CoverageObservation(
                id=item.candidate.id,
                question=item.candidate.question,
                passed=item.passed,
                plan_semantics_equal=(
                    False if execution is None else execution.checks["query_plan_semantics_equal"]
                ),
                evidence_semantics_equal=(
                    False if execution is None else execution.checks["evidence_semantics_equal"]
                ),
                fallback_used=False if execution is None else execution.fallback_used,
                first_failure_stage=item.first_failure_stage,
                latency_ms=0.0 if execution is None else execution.latency_ms,
            )
        )
    return observations


def _source_payload(
    kind: Literal["canonical", "naturalized"],
    observations: Sequence[CoverageObservation],
) -> dict[str, object]:
    return {
        "kind": kind,
        "questions": [
            {
                "id": item.id,
                "question": item.question,
            }
            for item in observations
        ],
    }


def _snapshot(
    label: str,
    report: ComparableCoverageReport,
    observations: Sequence[CoverageObservation],
) -> CoverageProfileSnapshot:
    total = len(observations)
    passed = sum(item.passed for item in observations)
    plan_passed = sum(item.plan_semantics_equal for item in observations)
    evidence_passed = sum(item.evidence_semantics_equal for item in observations)
    latencies = [item.latency_ms for item in observations]
    stages = Counter(
        item.first_failure_stage for item in observations if item.first_failure_stage is not None
    )
    return CoverageProfileSnapshot(
        label=label,
        agent_profile=report.agent_profile,
        agent_model=report.agent_model,
        total=total,
        passed=passed,
        strict_accuracy=round(passed / total, 6),
        plan_semantic_passed=plan_passed,
        plan_semantic_rate=round(plan_passed / total, 6),
        evidence_semantic_passed=evidence_passed,
        evidence_semantic_rate=round(evidence_passed / total, 6),
        fallback_count=sum(item.fallback_used for item in observations),
        first_failure_stages=dict(sorted(stages.items())),
        latency_ms={
            "min": round(min(latencies), 3),
            "p50": _percentile(latencies, 0.5),
            "p95": _percentile(latencies, 0.95),
            "max": round(max(latencies), 3),
        },
        provider_calls=report.provider_calls,
    )


def _call_delta(
    baseline: ProviderCallSnapshot,
    candidate: ProviderCallSnapshot,
) -> CoverageProviderCallDelta:
    return CoverageProviderCallDelta(
        query_plan_calls=candidate.query_plan_calls - baseline.query_plan_calls,
        query_plan_errors=candidate.query_plan_errors - baseline.query_plan_errors,
        query_plan_latency_ms=round(
            candidate.query_plan_latency_ms - baseline.query_plan_latency_ms,
            3,
        ),
        answer_calls=candidate.answer_calls - baseline.answer_calls,
        answer_errors=candidate.answer_errors - baseline.answer_errors,
        answer_latency_ms=round(
            candidate.answer_latency_ms - baseline.answer_latency_ms,
            3,
        ),
    )


def _state(item: CoverageObservation) -> str:
    return "pass" if item.passed else (item.first_failure_stage or "failed")


def _pairwise(
    baseline_label: str,
    candidate_label: str,
    baseline: Sequence[CoverageObservation],
    candidate: Sequence[CoverageObservation],
    baseline_snapshot: CoverageProfileSnapshot,
    candidate_snapshot: CoverageProfileSnapshot,
) -> CoveragePairwiseDelta:
    baseline_by_id = {item.id: item for item in baseline}
    candidate_by_id = {item.id: item for item in candidate}
    if list(baseline_by_id) != list(candidate_by_id):
        raise ValueError("coverage ablation observation IDs or order differ")
    pairs = [(item, candidate_by_id[item.id]) for item in baseline]
    rescued = [left.id for left, right in pairs if not left.passed and right.passed]
    regressed = [left.id for left, right in pairs if left.passed and not right.passed]
    transitions = Counter(f"{_state(left)}->{_state(right)}" for left, right in pairs)
    return CoveragePairwiseDelta(
        baseline_label=baseline_label,
        candidate_label=candidate_label,
        total=len(pairs),
        strict_accuracy_delta=round(
            candidate_snapshot.strict_accuracy - baseline_snapshot.strict_accuracy,
            6,
        ),
        plan_semantic_rate_delta=round(
            candidate_snapshot.plan_semantic_rate - baseline_snapshot.plan_semantic_rate,
            6,
        ),
        evidence_semantic_rate_delta=round(
            candidate_snapshot.evidence_semantic_rate - baseline_snapshot.evidence_semantic_rate,
            6,
        ),
        rescued=len(rescued),
        regressed=len(regressed),
        unchanged_pass=sum(left.passed and right.passed for left, right in pairs),
        unchanged_fail=sum(not left.passed and not right.passed for left, right in pairs),
        plan_rescued=sum(
            not left.plan_semantics_equal and right.plan_semantics_equal for left, right in pairs
        ),
        plan_regressed=sum(
            left.plan_semantics_equal and not right.plan_semantics_equal for left, right in pairs
        ),
        evidence_rescued=sum(
            not left.evidence_semantics_equal and right.evidence_semantics_equal
            for left, right in pairs
        ),
        evidence_regressed=sum(
            left.evidence_semantics_equal and not right.evidence_semantics_equal
            for left, right in pairs
        ),
        rescued_case_ids=rescued,
        regressed_case_ids=regressed,
        stage_transitions=dict(sorted(transitions.items())),
        provider_call_delta=_call_delta(
            baseline_snapshot.provider_calls,
            candidate_snapshot.provider_calls,
        ),
        latency_delta_ms={
            key: round(
                candidate_snapshot.latency_ms[key] - baseline_snapshot.latency_ms[key],
                3,
            )
            for key in ("min", "p50", "p95", "max")
        },
    )


def compare_coverage_profiles(
    reports: Mapping[str, ComparableCoverageReport],
    *,
    generated_at_utc: str | None = None,
) -> CoverageAblationReport:
    if len(reports) < 2:
        raise ValueError("coverage ablation requires at least two profiles")
    labels = list(reports)
    if len(labels) != len(set(labels)):
        raise ValueError("coverage ablation labels must be unique")
    kinds = {_report_kind(report) for report in reports.values()}
    if len(kinds) != 1:
        raise ValueError("coverage ablation cannot mix canonical and naturalized reports")
    kind = kinds.pop()
    plan_suite_hashes = {report.plan_suite_semantic_sha256 for report in reports.values()}
    if len(plan_suite_hashes) != 1:
        raise ValueError("coverage ablation plan suite SHA-256 differs")

    observations_by_label = {label: _observations(report) for label, report in reports.items()}
    first_observations = observations_by_label[labels[0]]
    source_hash = canonical_json_sha256(_source_payload(kind, first_observations))
    for label in labels[1:]:
        observed_hash = canonical_json_sha256(_source_payload(kind, observations_by_label[label]))
        if observed_hash != source_hash:
            raise ValueError(f"coverage ablation source questions differ for {label}")
    snapshots = [_snapshot(label, reports[label], observations_by_label[label]) for label in labels]
    snapshot_by_label = {item.label: item for item in snapshots}
    baseline_label = labels[0]
    pairwise = [
        _pairwise(
            baseline_label,
            label,
            observations_by_label[baseline_label],
            observations_by_label[label],
            snapshot_by_label[baseline_label],
            snapshot_by_label[label],
        )
        for label in labels[1:]
    ]
    timestamp = generated_at_utc or datetime.now(UTC).replace(microsecond=0).isoformat()
    return CoverageAblationReport(
        ablation_id=f"coverage-{kind}-ablation-v1",
        generated_at_utc=timestamp,
        source_kind=kind,
        source_semantic_sha256=source_hash,
        baseline_label=baseline_label,
        profiles=snapshots,
        pairwise_deltas=pairwise,
        interpretation_limits=[
            "첫 번째 입력을 baseline으로 사용해 이후 profile을 문항별 비교한다.",
            "rescued와 regressed는 같은 질문·같은 정답 계획·같은 데이터 지문에서만 계산한다.",
            "provider_call_delta는 후보에서 baseline을 뺀 부호 있는 변화량이다.",
            "자동 생성·공개 개발 평가이며 독립 blind나 HyperCLOVA X 성능이 아니다.",
        ],
    )
