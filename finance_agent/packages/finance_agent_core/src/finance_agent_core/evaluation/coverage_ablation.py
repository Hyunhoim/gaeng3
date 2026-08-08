from __future__ import annotations

import math
import random
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
    cluster_id: str
    question: str
    product_family: str
    intent: str
    kind: str
    field: str | None
    operator: str | None
    direction: str | None
    function: str | None
    axis: str | None
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
    strict_accuracy_ci95: list[float] = Field(min_length=2, max_length=2)
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


class CoverageBucketDelta(CoverageModel):
    value: str
    total: int = Field(ge=1)
    baseline_passed: int = Field(ge=0)
    candidate_passed: int = Field(ge=0)
    baseline_accuracy: float = Field(ge=0, le=1)
    candidate_accuracy: float = Field(ge=0, le=1)
    accuracy_delta: float = Field(ge=-1, le=1)
    rescued: int = Field(ge=0)
    regressed: int = Field(ge=0)
    net_rescued: int


class CoveragePairwiseDelta(CoverageModel):
    baseline_label: str
    candidate_label: str
    total: int = Field(ge=1)
    strict_accuracy_delta: float = Field(ge=-1, le=1)
    strict_accuracy_delta_ci95: list[float] = Field(min_length=2, max_length=2)
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
    mcnemar_exact_p_value: float = Field(ge=0, le=1)
    mcnemar_unit_rescued: int = Field(default=0, ge=0)
    mcnemar_unit_regressed: int = Field(default=0, ge=0)
    holm_adjusted_p_value: float = Field(ge=0, le=1)
    statistically_significant_after_holm: bool
    zero_strict_regression: bool
    breakdowns: dict[str, list[CoverageBucketDelta]]
    provider_call_delta: CoverageProviderCallDelta
    latency_delta_ms: dict[str, float]


class CoverageAblationReport(CoverageModel):
    schema_version: Literal["1.0"] = "1.0"
    ablation_id: str
    generated_at_utc: str
    status: Literal["internal_synthetic_not_blind"] = "internal_synthetic_not_blind"
    source_kind: Literal["canonical", "naturalized"]
    source_semantic_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    statistical_unit: Literal["case", "source_plan_cluster"] = "case"
    statistical_unit_count: int = Field(default=1, ge=1)
    baseline_label: str
    profiles: list[CoverageProfileSnapshot] = Field(min_length=2)
    pairwise_deltas: list[CoveragePairwiseDelta] = Field(min_length=1)
    interpretation_limits: list[str] = Field(min_length=1, max_length=20)


def _raw_percentile(values: Sequence[float], quantile: float) -> float:
    ordered = sorted(values)
    index = (len(ordered) - 1) * quantile
    lower = math.floor(index)
    upper = math.ceil(index)
    if lower == upper:
        return ordered[lower]
    fraction = index - lower
    return ordered[lower] + (ordered[upper] - ordered[lower]) * fraction


def _percentile(values: Sequence[float], quantile: float) -> float:
    return round(_raw_percentile(values, quantile), 3)


def _wilson_ci95(passed: int, total: int) -> list[float]:
    z = 1.959963984540054
    proportion = passed / total
    denominator = 1 + z**2 / total
    center = (proportion + z**2 / (2 * total)) / denominator
    half_width = (
        z * math.sqrt(proportion * (1 - proportion) / total + z**2 / (4 * total**2)) / denominator
    )
    return [
        round(max(0.0, center - half_width), 6),
        round(min(1.0, center + half_width), 6),
    ]


def _paired_bootstrap_ci95(
    differences: Sequence[int],
    *,
    seed: str,
    samples: int = 10_000,
) -> list[float]:
    if not differences:
        raise ValueError("paired bootstrap requires observations")
    if len(set(differences)) == 1:
        value = float(differences[0])
        return [value, value]
    generator = random.Random(seed)
    total = len(differences)
    estimates = [
        sum(differences[generator.randrange(total)] for _ in range(total)) / total
        for _ in range(samples)
    ]
    return [
        round(_raw_percentile(estimates, 0.025), 6),
        round(_raw_percentile(estimates, 0.975), 6),
    ]


def _cluster_bootstrap_ci95(
    values_by_cluster: Mapping[str, Sequence[int]],
    *,
    seed: str,
    samples: int = 10_000,
) -> list[float]:
    if not values_by_cluster:
        raise ValueError("cluster bootstrap requires observations")
    clusters = [list(values) for _, values in sorted(values_by_cluster.items())]
    if any(not values for values in clusters):
        raise ValueError("cluster bootstrap groups must not be empty")
    cluster_sums = [sum(values) for values in clusters]
    cluster_sizes = [len(values) for values in clusters]
    if len(clusters) == 1:
        value = cluster_sums[0] / cluster_sizes[0]
        return [round(value, 6), round(value, 6)]
    generator = random.Random(seed)
    count = len(clusters)
    estimates: list[float] = []
    for _ in range(samples):
        numerator = 0
        denominator = 0
        for _ in range(count):
            index = generator.randrange(count)
            numerator += cluster_sums[index]
            denominator += cluster_sizes[index]
        estimates.append(numerator / denominator)
    return [
        round(_raw_percentile(estimates, 0.025), 6),
        round(_raw_percentile(estimates, 0.975), 6),
    ]


def _mcnemar_exact_p_value(rescued: int, regressed: int) -> float:
    discordant = rescued + regressed
    if discordant == 0:
        return 1.0
    tail = min(rescued, regressed)
    probability = sum(math.comb(discordant, index) for index in range(tail + 1))
    return round(min(1.0, 2 * probability / (2**discordant)), 12)


def _report_kind(report: ComparableCoverageReport) -> Literal["canonical", "naturalized"]:
    return "canonical" if isinstance(report, CoverageRunReport) else "naturalized"


def _observations(report: ComparableCoverageReport) -> list[CoverageObservation]:
    if isinstance(report, CoverageRunReport):
        return [
            CoverageObservation(
                id=item.id,
                cluster_id=item.id,
                question=item.question,
                product_family=item.product_family.value,
                intent=item.intent.value,
                kind=item.kind.value,
                field=item.field,
                operator=item.operator,
                direction=item.direction,
                function=item.function,
                axis=None,
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
        if not item.candidate.validation.passed:
            continue
        execution = item.execution
        observations.append(
            CoverageObservation(
                id=item.candidate.id,
                cluster_id=item.candidate.source_case_id,
                question=item.candidate.question,
                product_family=item.candidate.cell.product_family.value,
                intent=item.candidate.cell.intent.value,
                kind=item.candidate.cell.kind.value,
                field=item.candidate.cell.field,
                operator=(
                    None
                    if item.candidate.cell.operator is None
                    else item.candidate.cell.operator.value
                ),
                direction=(
                    None
                    if item.candidate.cell.direction is None
                    else item.candidate.cell.direction.value
                ),
                function=(
                    None
                    if item.candidate.cell.function is None
                    else item.candidate.cell.function.value
                ),
                axis=item.candidate.axis.value,
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
    *,
    kind: Literal["canonical", "naturalized"],
) -> CoverageProfileSnapshot:
    total = len(observations)
    passed = sum(item.passed for item in observations)
    plan_passed = sum(item.plan_semantics_equal for item in observations)
    evidence_passed = sum(item.evidence_semantics_equal for item in observations)
    latencies = [item.latency_ms for item in observations]
    stages = Counter(
        item.first_failure_stage for item in observations if item.first_failure_stage is not None
    )
    accuracy_ci95 = _wilson_ci95(passed, total)
    if kind == "naturalized":
        passed_by_cluster: dict[str, list[int]] = {}
        for item in observations:
            passed_by_cluster.setdefault(item.cluster_id, []).append(int(item.passed))
        accuracy_ci95 = _cluster_bootstrap_ci95(
            passed_by_cluster,
            seed=f"coverage-profile-v2:{label}",
        )
    return CoverageProfileSnapshot(
        label=label,
        agent_profile=report.agent_profile,
        agent_model=report.agent_model,
        total=total,
        passed=passed,
        strict_accuracy=round(passed / total, 6),
        strict_accuracy_ci95=accuracy_ci95,
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


_BREAKDOWN_DIMENSIONS = (
    "product_family",
    "intent",
    "kind",
    "field",
    "operator",
    "direction",
    "function",
    "axis",
)


def _breakdowns(
    pairs: Sequence[tuple[CoverageObservation, CoverageObservation]],
) -> dict[str, list[CoverageBucketDelta]]:
    result: dict[str, list[CoverageBucketDelta]] = {}
    for dimension in _BREAKDOWN_DIMENSIONS:
        grouped: dict[str, list[tuple[CoverageObservation, CoverageObservation]]] = {}
        for left, right in pairs:
            left_value = getattr(left, dimension)
            right_value = getattr(right, dimension)
            if left_value != right_value:
                raise ValueError(f"coverage ablation {dimension} metadata differs")
            if left_value is not None:
                grouped.setdefault(left_value, []).append((left, right))
        result[dimension] = []
        for value, bucket_pairs in sorted(grouped.items()):
            total = len(bucket_pairs)
            baseline_passed = sum(left.passed for left, _ in bucket_pairs)
            candidate_passed = sum(right.passed for _, right in bucket_pairs)
            rescued = sum(not left.passed and right.passed for left, right in bucket_pairs)
            regressed = sum(left.passed and not right.passed for left, right in bucket_pairs)
            baseline_accuracy = baseline_passed / total
            candidate_accuracy = candidate_passed / total
            result[dimension].append(
                CoverageBucketDelta(
                    value=value,
                    total=total,
                    baseline_passed=baseline_passed,
                    candidate_passed=candidate_passed,
                    baseline_accuracy=round(baseline_accuracy, 6),
                    candidate_accuracy=round(candidate_accuracy, 6),
                    accuracy_delta=round(candidate_accuracy - baseline_accuracy, 6),
                    rescued=rescued,
                    regressed=regressed,
                    net_rescued=rescued - regressed,
                )
            )
    return result


def _mcnemar_cluster_counts(
    pairs: Sequence[tuple[CoverageObservation, CoverageObservation]],
) -> tuple[int, int]:
    grouped: dict[str, list[tuple[CoverageObservation, CoverageObservation]]] = {}
    for left, right in pairs:
        if left.cluster_id != right.cluster_id:
            raise ValueError("coverage ablation observation clusters differ")
        grouped.setdefault(left.cluster_id, []).append((left, right))
    rescued = 0
    regressed = 0
    for cluster_pairs in grouped.values():
        baseline_passed = all(left.passed for left, _ in cluster_pairs)
        candidate_passed = all(right.passed for _, right in cluster_pairs)
        rescued += not baseline_passed and candidate_passed
        regressed += baseline_passed and not candidate_passed
    return rescued, regressed


def _pairwise(
    baseline_label: str,
    candidate_label: str,
    baseline: Sequence[CoverageObservation],
    candidate: Sequence[CoverageObservation],
    baseline_snapshot: CoverageProfileSnapshot,
    candidate_snapshot: CoverageProfileSnapshot,
    *,
    kind: Literal["canonical", "naturalized"],
) -> CoveragePairwiseDelta:
    baseline_by_id = {item.id: item for item in baseline}
    candidate_by_id = {item.id: item for item in candidate}
    if list(baseline_by_id) != list(candidate_by_id):
        raise ValueError("coverage ablation observation IDs or order differ")
    pairs = [(item, candidate_by_id[item.id]) for item in baseline]
    rescued = [left.id for left, right in pairs if not left.passed and right.passed]
    regressed = [left.id for left, right in pairs if left.passed and not right.passed]
    transitions = Counter(f"{_state(left)}->{_state(right)}" for left, right in pairs)
    paired_differences = [int(right.passed) - int(left.passed) for left, right in pairs]
    clustered_differences: dict[str, list[int]] = {}
    for (left, _), difference in zip(pairs, paired_differences, strict=True):
        clustered_differences.setdefault(left.cluster_id, []).append(difference)
    difference_ci95 = _paired_bootstrap_ci95(
        paired_differences,
        seed=f"coverage-ablation-v1:{baseline_label}:{candidate_label}",
    )
    if kind == "naturalized":
        difference_ci95 = _cluster_bootstrap_ci95(
            clustered_differences,
            seed=f"coverage-ablation-v2:{baseline_label}:{candidate_label}",
        )
    mcnemar_rescued, mcnemar_regressed = _mcnemar_cluster_counts(pairs)
    raw_p_value = _mcnemar_exact_p_value(mcnemar_rescued, mcnemar_regressed)
    return CoveragePairwiseDelta(
        baseline_label=baseline_label,
        candidate_label=candidate_label,
        total=len(pairs),
        strict_accuracy_delta=round(
            candidate_snapshot.strict_accuracy - baseline_snapshot.strict_accuracy,
            6,
        ),
        strict_accuracy_delta_ci95=difference_ci95,
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
        mcnemar_exact_p_value=raw_p_value,
        mcnemar_unit_rescued=mcnemar_rescued,
        mcnemar_unit_regressed=mcnemar_regressed,
        holm_adjusted_p_value=raw_p_value,
        statistically_significant_after_holm=False,
        zero_strict_regression=not regressed,
        breakdowns=_breakdowns(pairs),
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


def _apply_holm_correction(
    deltas: Sequence[CoveragePairwiseDelta],
) -> list[CoveragePairwiseDelta]:
    ordered = sorted(
        enumerate(deltas),
        key=lambda item: item[1].mcnemar_exact_p_value,
    )
    adjusted_by_index: dict[int, float] = {}
    running_max = 0.0
    total = len(ordered)
    for rank, (index, delta) in enumerate(ordered):
        adjusted = min(1.0, delta.mcnemar_exact_p_value * (total - rank))
        running_max = max(running_max, adjusted)
        adjusted_by_index[index] = round(running_max, 12)
    return [
        delta.model_copy(
            update={
                "holm_adjusted_p_value": adjusted_by_index[index],
                "statistically_significant_after_holm": (
                    adjusted_by_index[index] < 0.05 and delta.strict_accuracy_delta > 0
                ),
            }
        )
        for index, delta in enumerate(deltas)
    ]


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
    if not first_observations:
        raise ValueError("coverage ablation has no mechanically accepted questions")
    source_hash = canonical_json_sha256(_source_payload(kind, first_observations))
    for label in labels[1:]:
        observed_hash = canonical_json_sha256(_source_payload(kind, observations_by_label[label]))
        if observed_hash != source_hash:
            raise ValueError(f"coverage ablation source questions differ for {label}")
    snapshots = [
        _snapshot(
            label,
            reports[label],
            observations_by_label[label],
            kind=kind,
        )
        for label in labels
    ]
    snapshot_by_label = {item.label: item for item in snapshots}
    baseline_label = labels[0]
    pairwise = _apply_holm_correction(
        [
            _pairwise(
                baseline_label,
                label,
                observations_by_label[baseline_label],
                observations_by_label[label],
                snapshot_by_label[baseline_label],
                snapshot_by_label[label],
                kind=kind,
            )
            for label in labels[1:]
        ]
    )
    timestamp = generated_at_utc or datetime.now(UTC).replace(microsecond=0).isoformat()
    return CoverageAblationReport(
        ablation_id=f"coverage-{kind}-ablation-v2",
        generated_at_utc=timestamp,
        source_kind=kind,
        source_semantic_sha256=source_hash,
        statistical_unit=("case" if kind == "canonical" else "source_plan_cluster"),
        statistical_unit_count=len({item.cluster_id for item in first_observations}),
        baseline_label=baseline_label,
        profiles=snapshots,
        pairwise_deltas=pairwise,
        interpretation_limits=[
            "첫 번째 입력을 baseline으로 사용해 이후 profile을 문항별 비교한다.",
            "rescued와 regressed는 같은 질문·같은 정답 계획·같은 데이터 지문에서만 계산한다.",
            (
                "naturalized 역할 비교 분모는 기계 의미 보존을 통과한 질문이며 "
                "생성 거절·오류는 별도 생성 지표로 본다."
            ),
            (
                "canonical 정확도 구간은 Wilson 95%이며, naturalized 정확도와 paired "
                "차이 구간은 같은 정답 계획의 세 문체를 묶은 seed 고정 10,000회 "
                "cluster bootstrap이다."
            ),
            (
                "paired 개선 검정은 canonical 문항 또는 naturalized source plan을 "
                "단위로 한 exact McNemar이며 여러 후보는 Holm 방식으로 보정한다."
            ),
            "provider_call_delta는 후보에서 baseline을 뺀 부호 있는 변화량이다.",
            "자동 생성·공개 개발 평가이며 독립 blind나 HyperCLOVA X 성능이 아니다.",
        ],
    )
