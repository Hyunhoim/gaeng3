from __future__ import annotations

import random
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from finance_agent_core.evaluation.dense_schema_linker import SchemaCaseDiagnostic
from finance_agent_core.evaluation.schema_embedding_benchmark import (
    SchemaEmbeddingBenchmarkReport,
    schema_embedding_report_fingerprint,
)

SCHEMA_EMBEDDING_MODEL_ALIASES = (
    "bge-m3",
    "kure-v1",
    "qwen3-embedding-0.6b",
    "koe5",
    "multilingual-e5-large-instruct",
    "snowflake-arctic-l-v2",
    "nomic-v2-moe",
)


class SchemaEmbeddingAnalysisModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class BootstrapMetric(SchemaEmbeddingAnalysisModel):
    selected_value: float = Field(ge=0, le=1)
    comparator_value: float = Field(ge=0, le=1)
    observed_delta: float = Field(ge=-1, le=1)
    ci95_lower: float = Field(ge=-1, le=1)
    ci95_upper: float = Field(ge=-1, le=1)
    probability_selected_greater: float = Field(ge=0, le=1)
    probability_equal: float = Field(ge=0, le=1)


class PairedModelComparison(SchemaEmbeddingAnalysisModel):
    comparator: str
    case_count: int = Field(ge=1)
    gold_field_count: int = Field(ge=1)
    selected_only_exact_cases: int = Field(ge=0)
    comparator_only_exact_cases: int = Field(ge=0)
    both_exact_cases: int = Field(ge=0)
    neither_exact_cases: int = Field(ge=0)
    exact: BootstrapMetric
    recall_at_5: BootstrapMetric


class ScoreQuantiles(SchemaEmbeddingAnalysisModel):
    case_count: int = Field(ge=1)
    minimum: float
    p05: float
    p25: float
    p50: float
    p75: float
    p95: float
    maximum: float


class ConfidenceProfile(SchemaEmbeddingAnalysisModel):
    group: str
    top_1_score: ScoreQuantiles
    top_1_top_2_margin: ScoreQuantiles


class ExactFailureCase(SchemaEmbeddingAnalysisModel):
    case_id: str
    product_family: str
    split: str
    category: str
    question: str
    gold_fields: tuple[str, ...]
    predicted_at_gold_cardinality: tuple[str, ...]
    missing_at_5: tuple[str, ...]
    dense_top_1_score: float
    dense_margin_top_1_top_2: float


class SchemaEmbeddingStatisticalAnalysis(SchemaEmbeddingAnalysisModel):
    schema_version: Literal["1.0"] = "1.0"
    analysis_id: Literal["schema-embedding-cpu-public-v1-statistics"] = (
        "schema-embedding-cpu-public-v1-statistics"
    )
    status: Literal["public_development_not_blind"] = "public_development_not_blind"
    selected_model_alias: Literal["bge-m3"] = "bge-m3"
    fusion_strategy: Literal["lexical_first"] = "lexical_first"
    bootstrap_method: Literal["paired_case_resampling_percentile"] = (
        "paired_case_resampling_percentile"
    )
    bootstrap_iterations: int = Field(ge=1000)
    random_seed: int
    selected_report_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    comparisons: tuple[PairedModelComparison, ...]
    confidence_profiles: tuple[ConfidenceProfile, ...]
    exact_failure_cases: tuple[ExactFailureCase, ...]
    top_5_capacity_limited_case_ids: tuple[str, ...]
    selected_hits_at_5: int = Field(ge=0)
    maximum_possible_hits_at_5: int = Field(ge=1)
    capacity_adjusted_recall_at_5: float = Field(ge=0, le=1)
    interpretation_guard: Literal[
        "confidence_intervals_describe_this_public_suite_not_external_generalization"
    ] = "confidence_intervals_describe_this_public_suite_not_external_generalization"


@dataclass(frozen=True)
class _CaseOutcome:
    exact: bool
    hits_at_5: int
    gold_count: int


def _percentile(values: list[float], quantile: float) -> float:
    if not values:
        raise ValueError("percentile requires at least one value")
    if not 0 <= quantile <= 1:
        raise ValueError("quantile must be in [0, 1]")
    ordered = sorted(values)
    position = (len(ordered) - 1) * quantile
    lower_index = int(position)
    upper_index = min(lower_index + 1, len(ordered) - 1)
    fraction = position - lower_index
    return ordered[lower_index] * (1 - fraction) + ordered[upper_index] * fraction


def _quantiles(values: list[float]) -> ScoreQuantiles:
    return ScoreQuantiles(
        case_count=len(values),
        minimum=round(min(values), 6),
        p05=round(_percentile(values, 0.05), 6),
        p25=round(_percentile(values, 0.25), 6),
        p50=round(_percentile(values, 0.50), 6),
        p75=round(_percentile(values, 0.75), 6),
        p95=round(_percentile(values, 0.95), 6),
        maximum=round(max(values), 6),
    )


def _outcome(case: SchemaCaseDiagnostic, source: Literal["lexical", "hybrid"]) -> _CaseOutcome:
    predicted = case.lexical_fields if source == "lexical" else case.hybrid_fields
    gold = set(case.gold_fields)
    exact = (
        case.lexical_exact
        if source == "lexical"
        else case.hybrid_exact
    )
    return _CaseOutcome(
        exact=exact,
        hits_at_5=len(gold & set(predicted[:5])),
        gold_count=len(gold),
    )


def _metric(
    selected: list[_CaseOutcome],
    comparator: list[_CaseOutcome],
    indices: list[int],
) -> tuple[float, float]:
    selected_exact = sum(selected[index].exact for index in indices) / len(indices)
    comparator_exact = sum(comparator[index].exact for index in indices) / len(indices)
    gold_count = sum(selected[index].gold_count for index in indices)
    selected_recall = sum(selected[index].hits_at_5 for index in indices) / gold_count
    comparator_recall = sum(comparator[index].hits_at_5 for index in indices) / gold_count
    return selected_exact - comparator_exact, selected_recall - comparator_recall


def _bootstrap_metric(
    selected_value: float,
    comparator_value: float,
    deltas: list[float],
) -> BootstrapMetric:
    tolerance = 1e-12
    return BootstrapMetric(
        selected_value=round(selected_value, 6),
        comparator_value=round(comparator_value, 6),
        observed_delta=round(selected_value - comparator_value, 6),
        ci95_lower=round(_percentile(deltas, 0.025), 6),
        ci95_upper=round(_percentile(deltas, 0.975), 6),
        probability_selected_greater=round(
            sum(delta > tolerance for delta in deltas) / len(deltas),
            6,
        ),
        probability_equal=round(
            sum(abs(delta) <= tolerance for delta in deltas) / len(deltas),
            6,
        ),
    )


def paired_bootstrap_comparison(
    selected: list[_CaseOutcome],
    comparator: list[_CaseOutcome],
    *,
    comparator_name: str,
    iterations: int,
    seed: int,
) -> PairedModelComparison:
    if len(selected) != len(comparator) or not selected:
        raise ValueError("paired outcomes must have the same non-zero length")
    if iterations < 1000:
        raise ValueError("bootstrap requires at least 1,000 iterations")
    if any(
        left.gold_count != right.gold_count
        for left, right in zip(selected, comparator, strict=True)
    ):
        raise ValueError("paired outcomes must use the same gold cardinality")

    case_count = len(selected)
    all_indices = list(range(case_count))
    observed_exact_delta, observed_recall_delta = _metric(
        selected,
        comparator,
        all_indices,
    )
    selected_exact = sum(item.exact for item in selected) / case_count
    comparator_exact = sum(item.exact for item in comparator) / case_count
    gold_count = sum(item.gold_count for item in selected)
    selected_recall = sum(item.hits_at_5 for item in selected) / gold_count
    comparator_recall = sum(item.hits_at_5 for item in comparator) / gold_count

    rng = random.Random(seed)
    exact_deltas: list[float] = []
    recall_deltas: list[float] = []
    for _ in range(iterations):
        sample = [rng.randrange(case_count) for _ in range(case_count)]
        exact_delta, recall_delta = _metric(selected, comparator, sample)
        exact_deltas.append(exact_delta)
        recall_deltas.append(recall_delta)

    exact_metric = _bootstrap_metric(selected_exact, comparator_exact, exact_deltas)
    recall_metric = _bootstrap_metric(selected_recall, comparator_recall, recall_deltas)
    if abs(exact_metric.observed_delta - observed_exact_delta) > 1e-6:
        raise RuntimeError("exact bootstrap observed delta differs")
    if abs(recall_metric.observed_delta - observed_recall_delta) > 1e-6:
        raise RuntimeError("recall bootstrap observed delta differs")

    return PairedModelComparison(
        comparator=comparator_name,
        case_count=case_count,
        gold_field_count=gold_count,
        selected_only_exact_cases=sum(
            left.exact and not right.exact
            for left, right in zip(selected, comparator, strict=True)
        ),
        comparator_only_exact_cases=sum(
            right.exact and not left.exact
            for left, right in zip(selected, comparator, strict=True)
        ),
        both_exact_cases=sum(
            left.exact and right.exact
            for left, right in zip(selected, comparator, strict=True)
        ),
        neither_exact_cases=sum(
            not left.exact and not right.exact
            for left, right in zip(selected, comparator, strict=True)
        ),
        exact=exact_metric,
        recall_at_5=recall_metric,
    )


def _load_lexical_first_reports(
    artifact_dir: Path,
) -> dict[str, SchemaEmbeddingBenchmarkReport]:
    reports: dict[str, SchemaEmbeddingBenchmarkReport] = {}
    for alias in SCHEMA_EMBEDDING_MODEL_ALIASES:
        path = artifact_dir / f"{alias}-cpu-public-v1-lexical-first.json"
        report = SchemaEmbeddingBenchmarkReport.model_validate_json(
            path.read_text(encoding="utf-8")
        )
        if report.model.alias != alias or report.fusion.strategy != "lexical_first":
            raise ValueError(f"report identity differs: {path}")
        reports[alias] = report
    suite_signatures = {
        (
            report.suite_case_count,
            tuple(sorted(report.suite_sha256_by_family.items())),
            report.policy_migration_suite_sha256,
        )
        for report in reports.values()
    }
    if len(suite_signatures) != 1:
        raise ValueError("embedding reports do not share the same frozen suite")
    return reports


def _aligned_outcomes(
    selected: SchemaEmbeddingBenchmarkReport,
    comparator: SchemaEmbeddingBenchmarkReport,
) -> tuple[list[_CaseOutcome], list[_CaseOutcome]]:
    selected_cases = {item.case_id: item for item in selected.case_diagnostics}
    comparator_cases = {item.case_id: item for item in comparator.case_diagnostics}
    if set(selected_cases) != set(comparator_cases):
        raise ValueError("embedding reports have different case IDs")
    selected_outcomes: list[_CaseOutcome] = []
    comparator_outcomes: list[_CaseOutcome] = []
    for case_id in sorted(selected_cases):
        left = selected_cases[case_id]
        right = comparator_cases[case_id]
        if (
            left.question,
            left.product_family,
            left.gold_fields,
        ) != (
            right.question,
            right.product_family,
            right.gold_fields,
        ):
            raise ValueError(f"case contract differs: {case_id}")
        selected_outcomes.append(_outcome(left, "hybrid"))
        comparator_outcomes.append(_outcome(right, "hybrid"))
    return selected_outcomes, comparator_outcomes


def build_schema_embedding_statistical_analysis(
    artifact_dir: Path,
    *,
    iterations: int = 10_000,
    seed: int = 20_260_812,
) -> SchemaEmbeddingStatisticalAnalysis:
    reports = _load_lexical_first_reports(artifact_dir)
    selected = reports["bge-m3"]
    ordered_cases = sorted(selected.case_diagnostics, key=lambda item: item.case_id)
    selected_outcomes = [_outcome(item, "hybrid") for item in ordered_cases]
    lexical_outcomes = [_outcome(item, "lexical") for item in ordered_cases]
    comparisons = [
        paired_bootstrap_comparison(
            selected_outcomes,
            lexical_outcomes,
            comparator_name="lexical_baseline",
            iterations=iterations,
            seed=seed,
        )
    ]
    for offset, alias in enumerate(SCHEMA_EMBEDDING_MODEL_ALIASES[1:], start=1):
        left, right = _aligned_outcomes(selected, reports[alias])
        comparisons.append(
            paired_bootstrap_comparison(
                left,
                right,
                comparator_name=alias,
                iterations=iterations,
                seed=seed + offset,
            )
        )

    profile_groups: dict[str, list[SchemaCaseDiagnostic]] = {
        "all": ordered_cases,
        "hybrid_exact": [item for item in ordered_cases if item.hybrid_exact],
        "hybrid_not_exact": [item for item in ordered_cases if not item.hybrid_exact],
    }
    for family in sorted({item.product_family.value for item in ordered_cases}):
        profile_groups[family] = [
            item for item in ordered_cases if item.product_family.value == family
        ]
    confidence_profiles = tuple(
        ConfidenceProfile(
            group=group,
            top_1_score=_quantiles(
                [
                    item.dense_top_1_score
                    for item in cases
                    if item.dense_top_1_score is not None
                ]
            ),
            top_1_top_2_margin=_quantiles(
                [
                    item.dense_margin_top_1_top_2
                    for item in cases
                    if item.dense_margin_top_1_top_2 is not None
                ]
            ),
        )
        for group, cases in profile_groups.items()
    )
    exact_failures = tuple(
        ExactFailureCase(
            case_id=item.case_id,
            product_family=item.product_family.value,
            split=item.split.value,
            category=item.category,
            question=item.question,
            gold_fields=item.gold_fields,
            predicted_at_gold_cardinality=item.hybrid_fields[: len(item.gold_fields)],
            missing_at_5=item.hybrid_missing_at_5,
            dense_top_1_score=item.dense_top_1_score or 0.0,
            dense_margin_top_1_top_2=item.dense_margin_top_1_top_2 or 0.0,
        )
        for item in ordered_cases
        if not item.hybrid_exact
    )
    capacity_limited = tuple(
        item.case_id
        for item in ordered_cases
        if len(item.gold_fields) > 5 and item.hybrid_missing_at_5
    )
    selected_hits_at_5 = sum(item.hits_at_5 for item in selected_outcomes)
    maximum_possible_hits_at_5 = sum(
        min(5, item.gold_count) for item in selected_outcomes
    )
    return SchemaEmbeddingStatisticalAnalysis(
        bootstrap_iterations=iterations,
        random_seed=seed,
        selected_report_fingerprint=schema_embedding_report_fingerprint(selected),
        comparisons=tuple(comparisons),
        confidence_profiles=confidence_profiles,
        exact_failure_cases=exact_failures,
        top_5_capacity_limited_case_ids=capacity_limited,
        selected_hits_at_5=selected_hits_at_5,
        maximum_possible_hits_at_5=maximum_possible_hits_at_5,
        capacity_adjusted_recall_at_5=round(
            selected_hits_at_5 / maximum_possible_hits_at_5,
            6,
        ),
    )


__all__ = [
    "BootstrapMetric",
    "PairedModelComparison",
    "SchemaEmbeddingStatisticalAnalysis",
    "build_schema_embedding_statistical_analysis",
    "paired_bootstrap_comparison",
]
