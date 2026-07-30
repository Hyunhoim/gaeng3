from __future__ import annotations

import hashlib
import json
import math
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from importlib.resources import files
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from finance_agent_core.agent.providers import fund_comparison_plan
from finance_agent_core.answering import (
    GroundedAnswerDraft,
    GroundedAnswerProvider,
    build_grounded_answer_context,
    compose_grounded_answer,
)
from finance_agent_core.evaluation.models import EvaluationSplit
from finance_agent_core.execution import (
    ResultVerifier,
    SQLiteOracle,
    build_fund_comparison,
    build_product_evidence,
    require_internal_evaluation_comparison,
)
from finance_agent_core.storage import (
    connect_read_only,
    load_all_records,
    load_manifest,
)

type ComparisonStatus = Literal[
    "numeric_delta",
    "value_only",
    "currency_mismatch",
    "unavailable",
    "incomplete",
]
type CompositionMode = Literal["llm_grounded", "deterministic", "deterministic_fallback"]


class ComparisonEvaluationModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class FundComparisonExpectation(ComparisonEvaluationModel):
    found_product_ids: list[str] = Field(max_length=2)
    missing_product_ids: list[str] = Field(max_length=2)
    field_statuses: dict[str, ComparisonStatus]
    deltas: dict[str, str | None]
    answer_mode: Literal["llm_grounded", "deterministic"]


class FundComparisonCase(ComparisonEvaluationModel):
    id: str = Field(min_length=1, max_length=128)
    split: EvaluationSplit
    category: str = Field(min_length=1, max_length=100)
    question: str = Field(min_length=1, max_length=2000)
    product_ids: list[str] = Field(min_length=2, max_length=2)
    comparison_fields: list[str] = Field(min_length=1, max_length=10)
    expected: FundComparisonExpectation

    @model_validator(mode="after")
    def validate_case_contract(self) -> FundComparisonCase:
        if len(set(self.product_ids)) != 2:
            raise ValueError("comparison product IDs must be unique")
        if len(set(self.comparison_fields)) != len(self.comparison_fields):
            raise ValueError("comparison fields must be unique")
        expected_ids = self.expected.found_product_ids + self.expected.missing_product_ids
        if set(expected_ids) != set(self.product_ids) or len(expected_ids) != 2:
            raise ValueError("found and missing product IDs must partition requested IDs")
        found_in_request_order = [
            product_id
            for product_id in self.product_ids
            if product_id in self.expected.found_product_ids
        ]
        if found_in_request_order != self.expected.found_product_ids:
            raise ValueError("found product IDs must preserve request order")
        if set(self.expected.field_statuses) != set(self.comparison_fields):
            raise ValueError("field statuses must cover every comparison field")
        if set(self.expected.deltas) != set(self.comparison_fields):
            raise ValueError("deltas must cover every comparison field")
        for field_name, status in self.expected.field_statuses.items():
            delta = self.expected.deltas[field_name]
            if (status == "numeric_delta") != (delta is not None):
                raise ValueError("only numeric_delta fields may have an expected delta")
        expected_mode = "deterministic" if self.expected.missing_product_ids else "llm_grounded"
        if self.expected.answer_mode != expected_mode:
            raise ValueError("incomplete comparisons must use deterministic answer mode")
        return self


class FundComparisonSuite(ComparisonEvaluationModel):
    suite_id: str
    suite_version: str
    dataset: Literal["fund"]
    database_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    cases: list[FundComparisonCase] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_unique_cases(self) -> FundComparisonSuite:
        case_ids = [case.id for case in self.cases]
        if len(case_ids) != len(set(case_ids)):
            raise ValueError("comparison case IDs must be unique")
        return self


@dataclass(frozen=True)
class LoadedFundComparisonSuite:
    suite: FundComparisonSuite
    suite_sha256: str


class FundComparisonCaseResult(ComparisonEvaluationModel):
    case_id: str
    split: EvaluationSplit
    category: str
    question: str
    passed: bool
    mode: Literal["llm_grounded", "deterministic", "deterministic_fallback", "error"]
    generation_latency_ms: float = Field(ge=0)
    total_latency_ms: float = Field(ge=0)
    found_product_ids: list[str]
    missing_product_ids: list[str]
    field_statuses: dict[str, ComparisonStatus]
    deltas: dict[str, str | None]
    checks: dict[str, bool]
    violations: list[str]
    draft: GroundedAnswerDraft | None
    answer: str
    error: str | None


class FundComparisonSummary(ComparisonEvaluationModel):
    total: int
    passed: int
    strict_accuracy: float
    complete_cases: int
    incomplete_cases: int
    llm_grounded_cases: int
    deterministic_cases: int
    fallback_cases: int
    fallback_rate: float
    field_status_accuracy: float
    numeric_delta_accuracy: float
    evidence_citation_rate: float
    source_date_coverage_rate: float
    generation_latency_ms_p50: float
    generation_latency_ms_p95: float
    generation_latency_ms_max: float
    failures: list[str]
    by_split: dict[str, dict[str, float | int]]
    by_category: dict[str, dict[str, float | int]]


class FundComparisonReport(ComparisonEvaluationModel):
    suite_id: str
    suite_version: str
    suite_sha256: str
    database_sha256: str
    manifest_sha256: str
    provider: str
    model: str | None
    split: Literal["development", "holdout", "all"]
    workers: int
    isolation: Literal["expected_comparison_plan_then_answer"] = (
        "expected_comparison_plan_then_answer"
    )
    summary: FundComparisonSummary
    results: list[FundComparisonCaseResult]


def load_fund_comparison_suite() -> LoadedFundComparisonSuite:
    resource = files("finance_agent_core.evaluation.suites").joinpath("fund_compare_core_20.json")
    raw = resource.read_bytes()
    payload: Any = json.loads(raw)
    return LoadedFundComparisonSuite(
        suite=FundComparisonSuite.model_validate(payload),
        suite_sha256=hashlib.sha256(raw).hexdigest(),
    )


def _decimal_string(value) -> str | None:
    if value is None:
        return None
    rendered = format(value, "f")
    if "." in rendered:
        rendered = rendered.rstrip("0").rstrip(".")
    return rendered or "0"


def _comparison_citations_present(answer: str, comparison) -> bool:
    for field in comparison.fields:
        for cell in field.cells:
            evidence = cell.evidence
            if evidence is None:
                continue
            fragments = (
                f"{evidence.source_id} 원본 행 {evidence.source_row}",
                "/".join(evidence.source_columns) or "constant",
                f"기준일 {evidence.as_of.isoformat()}",
            )
            if not all(fragment in answer for fragment in fragments):
                return False
    return True


class FundComparisonEvaluationRunner:
    def __init__(
        self,
        database_path: str | Path,
        provider: GroundedAnswerProvider,
    ) -> None:
        if provider.provider_name not in {"expected", "local_test"}:
            raise ValueError("fund comparison evaluation requires expected or local_test")
        self.database_path = Path(database_path)
        self.provider = provider
        self.oracle = SQLiteOracle(self.database_path)
        self.verifier = ResultVerifier()
        with connect_read_only(self.database_path) as connection:
            manifest = load_manifest(connection)
            self.universe = load_all_records(connection)
        if manifest.dataset != "fund":
            raise ValueError("fund comparison evaluation requires a fund database")

    def run_case(self, case: FundComparisonCase) -> FundComparisonCaseResult:
        started = time.perf_counter()
        try:
            plan = fund_comparison_plan(
                case.id,
                case.product_ids,
                case.comparison_fields,
            )
            require_internal_evaluation_comparison(plan)
            executed = self.oracle.execute(plan)
            verified = self.verifier.verify(plan, executed, self.universe)
            products = build_product_evidence(plan, verified)
            comparison = build_fund_comparison(plan, verified, products)
            context = build_grounded_answer_context(
                question=case.question,
                plan=plan,
                verified=comparison.verified,
                products=list(comparison.products),
            )
            composition = compose_grounded_answer(
                question=case.question,
                plan=plan,
                verified=comparison.verified,
                products=list(comparison.products),
                provider=self.provider,
            )
            statuses = {field.canonical_field: field.status for field in comparison.fields}
            deltas = {
                field.canonical_field: _decimal_string(field.delta) for field in comparison.fields
            }
            checks = {
                "found_product_order_exact": (
                    list(comparison.found_product_ids) == case.expected.found_product_ids
                ),
                "missing_product_ids_exact": (
                    list(comparison.missing_product_ids) == case.expected.missing_product_ids
                ),
                "field_statuses_exact": statuses == case.expected.field_statuses,
                "numeric_deltas_exact": deltas == case.expected.deltas,
                "answer_mode_exact": composition.mode == case.expected.answer_mode,
                "deterministic_core_preserved": (
                    context.deterministic_answer in composition.answer
                ),
                "evidence_citations_present": _comparison_citations_present(
                    composition.answer,
                    comparison,
                ),
                "source_date_covered": (
                    comparison.verified.manifest.source_snapshot_date.isoformat()
                    in composition.answer
                ),
                "safe_answer": bool(composition.answer.strip()),
                "verifier_passed": composition.verification.passed,
            }
            finished = time.perf_counter()
            return FundComparisonCaseResult(
                case_id=case.id,
                split=case.split,
                category=case.category,
                question=case.question,
                passed=all(checks.values()),
                mode=composition.mode,
                generation_latency_ms=composition.generation_latency_ms,
                total_latency_ms=round((finished - started) * 1000, 3),
                found_product_ids=list(comparison.found_product_ids),
                missing_product_ids=list(comparison.missing_product_ids),
                field_statuses=statuses,
                deltas=deltas,
                checks=checks,
                violations=composition.verification.violations,
                draft=composition.draft,
                answer=composition.answer,
                error=None,
            )
        except Exception as error:  # noqa: BLE001 - every case must be reportable
            finished = time.perf_counter()
            return FundComparisonCaseResult(
                case_id=case.id,
                split=case.split,
                category=case.category,
                question=case.question,
                passed=False,
                mode="error",
                generation_latency_ms=0,
                total_latency_ms=round((finished - started) * 1000, 3),
                found_product_ids=[],
                missing_product_ids=[],
                field_statuses={},
                deltas={},
                checks={"safe_answer": False},
                violations=[],
                draft=None,
                answer="",
                error=f"{type(error).__name__}: {error}",
            )

    def run(
        self,
        cases: list[FundComparisonCase],
        workers: int,
    ) -> list[FundComparisonCaseResult]:
        if workers < 1 or workers > 16:
            raise ValueError("workers must be in [1, 16]")
        if workers == 1:
            return [self.run_case(case) for case in cases]
        with ThreadPoolExecutor(max_workers=workers) as executor:
            return list(executor.map(self.run_case, cases))


def _rate(numerator: int, denominator: int) -> float:
    return round(numerator / denominator, 6) if denominator else 0.0


def _check_rate(results: list[FundComparisonCaseResult], check: str) -> float:
    return _rate(sum(result.checks.get(check, False) for result in results), len(results))


def _percentile(values: list[float], percentile: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = max(0, math.ceil(percentile * len(ordered)) - 1)
    return round(ordered[index], 3)


def _group_metrics(
    results: list[FundComparisonCaseResult],
    attribute: Literal["split", "category"],
) -> dict[str, dict[str, float | int]]:
    grouped: dict[str, list[FundComparisonCaseResult]] = {}
    for result in results:
        value = getattr(result, attribute)
        key = value.value if isinstance(value, EvaluationSplit) else value
        grouped.setdefault(key, []).append(result)
    return {
        key: {
            "total": len(items),
            "passed": sum(item.passed for item in items),
            "accuracy": _rate(sum(item.passed for item in items), len(items)),
        }
        for key, items in sorted(grouped.items())
    }


def build_fund_comparison_report(
    *,
    suite: LoadedFundComparisonSuite,
    provider: GroundedAnswerProvider,
    split: Literal["development", "holdout", "all"],
    workers: int,
    results: list[FundComparisonCaseResult],
) -> FundComparisonReport:
    complete = [result for result in results if not result.missing_product_ids]
    latencies = [
        result.generation_latency_ms for result in results if result.mode == "llm_grounded"
    ]
    fallback_cases = sum(result.mode == "deterministic_fallback" for result in results)
    grounded_attempts = sum(
        result.mode in {"llm_grounded", "deterministic_fallback"} for result in results
    )
    summary = FundComparisonSummary(
        total=len(results),
        passed=sum(result.passed for result in results),
        strict_accuracy=_rate(sum(result.passed for result in results), len(results)),
        complete_cases=len(complete),
        incomplete_cases=len(results) - len(complete),
        llm_grounded_cases=sum(result.mode == "llm_grounded" for result in results),
        deterministic_cases=sum(result.mode == "deterministic" for result in results),
        fallback_cases=fallback_cases,
        fallback_rate=_rate(fallback_cases, grounded_attempts),
        field_status_accuracy=_check_rate(results, "field_statuses_exact"),
        numeric_delta_accuracy=_check_rate(results, "numeric_deltas_exact"),
        evidence_citation_rate=_check_rate(results, "evidence_citations_present"),
        source_date_coverage_rate=_check_rate(results, "source_date_covered"),
        generation_latency_ms_p50=_percentile(latencies, 0.50),
        generation_latency_ms_p95=_percentile(latencies, 0.95),
        generation_latency_ms_max=round(max(latencies, default=0.0), 3),
        failures=[result.case_id for result in results if not result.passed],
        by_split=_group_metrics(results, "split"),
        by_category=_group_metrics(results, "category"),
    )
    return FundComparisonReport(
        suite_id=suite.suite.suite_id,
        suite_version=suite.suite.suite_version,
        suite_sha256=suite.suite_sha256,
        database_sha256=suite.suite.database_sha256,
        manifest_sha256=suite.suite.manifest_sha256,
        provider=provider.provider_name,
        model=provider.model_name,
        split=split,
        workers=workers,
        summary=summary,
        results=results,
    )
