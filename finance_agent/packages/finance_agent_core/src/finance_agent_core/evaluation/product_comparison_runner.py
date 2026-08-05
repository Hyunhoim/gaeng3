from __future__ import annotations

import hashlib
import json
import math
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from importlib.resources import files
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from finance_agent_core.agent import RoutedAgentResult, RoutedFinanceAgent
from finance_agent_core.contracts.backend import routed_result_to_backend
from finance_agent_core.contracts.queryplan import Intent, ProductFamily
from finance_agent_core.storage import ProductIdentityCacheStats, RecordCacheStats

type ExpectedServiceStatus = Literal["executed", "clarify", "unsupported"]
type ExpectedComparisonStatus = Literal[
    "numeric_delta",
    "value_only",
    "currency_mismatch",
    "as_of_mismatch",
    "stale_input",
    "unavailable",
    "incomplete",
]

_EVALUATED_FAMILIES = {
    ProductFamily.OVERSEAS_ETP,
    ProductFamily.DOMESTIC_ETP,
    ProductFamily.BOND,
}


class ProductComparisonEvaluationModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ProductComparisonDataContract(ProductComparisonEvaluationModel):
    database_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_file_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class ProductComparisonExpectation(ProductComparisonEvaluationModel):
    status: ExpectedServiceStatus
    product_ids: list[str] = Field(max_length=2)
    comparison_fields: list[str] = Field(max_length=10)
    field_statuses: dict[str, ExpectedComparisonStatus]
    deltas: dict[str, str | None]
    answer_contains: list[str] = Field(min_length=1, max_length=10)

    @model_validator(mode="after")
    def validate_expected_state(self) -> ProductComparisonExpectation:
        expected_fields = self.comparison_fields
        if list(self.field_statuses) != expected_fields or list(self.deltas) != expected_fields:
            raise ValueError("comparison maps must preserve the expected field order")
        if self.status == "executed":
            if len(self.product_ids) != 2 or len(set(self.product_ids)) != 2:
                raise ValueError("executed comparison requires two unique ordered products")
            if not expected_fields:
                raise ValueError("executed comparison requires comparison fields")
            for field_name, status in self.field_statuses.items():
                delta = self.deltas[field_name]
                delta_status = status in {"numeric_delta", "stale_input"}
                if delta_status != (delta is not None):
                    raise ValueError("only numeric_delta or stale_input fields may declare a delta")
        elif self.product_ids or expected_fields or self.field_statuses or self.deltas:
            raise ValueError("control cases cannot declare executed comparison results")
        return self


class ProductComparisonCase(ProductComparisonEvaluationModel):
    id: str = Field(pattern=r"^product-compare-(?:overseas|domestic|bond)-\d{3}$")
    product_family: ProductFamily
    category: str = Field(min_length=1, max_length=100)
    question: str = Field(min_length=5, max_length=2000)
    expected: ProductComparisonExpectation

    @model_validator(mode="after")
    def validate_family(self) -> ProductComparisonCase:
        if self.product_family not in _EVALUATED_FAMILIES:
            raise ValueError("the extension suite covers the three non-fund product families")
        return self


class ProductComparisonSuite(ProductComparisonEvaluationModel):
    schema_version: Literal["1.0"]
    suite_id: Literal["product-compare-core-30"]
    suite_version: Literal["1.0"]
    status: Literal["public_regression_not_blind"]
    complements_suite_id: Literal["fund-compare-e2e-core-24"]
    data: dict[ProductFamily, ProductComparisonDataContract]
    cases: list[ProductComparisonCase] = Field(min_length=30, max_length=30)

    @model_validator(mode="after")
    def validate_coverage(self) -> ProductComparisonSuite:
        if set(self.data) != _EVALUATED_FAMILIES:
            raise ValueError("data hashes must cover all three evaluated families")
        ids = [case.id for case in self.cases]
        questions = ["".join(case.question.casefold().split()) for case in self.cases]
        if len(ids) != len(set(ids)):
            raise ValueError("comparison case IDs must be unique")
        if len(questions) != len(set(questions)):
            raise ValueError("comparison questions must be unique after normalization")
        for family in sorted(_EVALUATED_FAMILIES, key=lambda item: item.value):
            family_cases = [case for case in self.cases if case.product_family is family]
            executed = sum(case.expected.status == "executed" for case in family_cases)
            controls = len(family_cases) - executed
            if (len(family_cases), executed, controls) != (10, 6, 4):
                raise ValueError(f"{family.value} requires 10 cases: 6 executed and 4 controls")
        return self


@dataclass(frozen=True)
class LoadedProductComparisonSuite:
    suite: ProductComparisonSuite
    suite_sha256: str


class ProductComparisonCaseResult(ProductComparisonEvaluationModel):
    case_id: str
    product_family: ProductFamily
    category: str
    question: str
    expected_status: ExpectedServiceStatus
    actual_status: ExpectedServiceStatus | Literal["error"]
    passed: bool
    latency_ms: float = Field(ge=0)
    checks: dict[str, bool]
    product_ids: list[str]
    comparison_fields: list[str]
    field_statuses: dict[str, str]
    deltas: dict[str, str | None]
    error: str | None


class ProductComparisonSummary(ProductComparisonEvaluationModel):
    total: int
    passed: int
    strict_accuracy: float
    executable_cases: int
    control_cases: int
    plan_exact_rate: float
    product_order_exact_rate: float
    field_exact_rate: float
    comparison_exact_rate: float
    backend_contract_rate: float
    control_suppression_rate: float
    answer_contract_rate: float
    latency_ms_p50: float
    latency_ms_p95: float
    latency_ms_max: float
    failures: list[str]
    by_product_family: dict[str, dict[str, float | int]]
    by_category: dict[str, dict[str, float | int]]


class ProductComparisonCacheSummary(ProductComparisonEvaluationModel):
    hits: int = Field(ge=0)
    misses: int = Field(ge=0)
    loads: int = Field(ge=0)
    invalidations: int = Field(ge=0)
    evictions: int = Field(ge=0)
    entries: int = Field(ge=0)
    records: int = Field(ge=0)


class ProductComparisonReport(ProductComparisonEvaluationModel):
    schema_version: Literal["1.1"] = "1.1"
    suite_id: str
    suite_version: str
    suite_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    status: Literal["public_regression_not_blind"]
    complements_suite_id: str
    provider: Literal["deterministic_server"]
    workers: int = Field(ge=1)
    data: dict[ProductFamily, ProductComparisonDataContract]
    identity_cache: ProductComparisonCacheSummary
    record_cache: ProductComparisonCacheSummary
    summary: ProductComparisonSummary
    results: list[ProductComparisonCaseResult]


def load_product_comparison_suite() -> LoadedProductComparisonSuite:
    resource = files("finance_agent_core.evaluation.suites").joinpath(
        "product_compare_core_30.json"
    )
    raw = resource.read_bytes()
    return LoadedProductComparisonSuite(
        suite=ProductComparisonSuite.model_validate(json.loads(raw)),
        suite_sha256=hashlib.sha256(raw).hexdigest(),
    )


def _rate(numerator: int, denominator: int) -> float:
    return round(numerator / denominator, 6) if denominator else 0.0


def _percentile(values: list[float], percentile: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = max(0, math.ceil(percentile * len(ordered)) - 1)
    return round(ordered[index], 3)


def _group_metrics(
    results: list[ProductComparisonCaseResult],
    attribute: Literal["product_family", "category"],
) -> dict[str, dict[str, float | int]]:
    grouped: dict[str, list[ProductComparisonCaseResult]] = {}
    for result in results:
        value = getattr(result, attribute)
        key = value.value if isinstance(value, ProductFamily) else value
        grouped.setdefault(key, []).append(result)
    return {
        key: {
            "total": len(items),
            "passed": sum(item.passed for item in items),
            "accuracy": _rate(sum(item.passed for item in items), len(items)),
        }
        for key, items in sorted(grouped.items())
    }


def evaluate_product_comparison_result(
    case: ProductComparisonCase,
    result: RoutedAgentResult,
    *,
    latency_ms: float,
) -> ProductComparisonCaseResult:
    expected = case.expected
    product_ids = [product.product_id for product in result.products]
    comparison_fields = [item.canonical_field for item in result.comparisons]
    field_statuses = {item.canonical_field: item.status for item in result.comparisons}
    deltas = {item.canonical_field: item.delta for item in result.comparisons}
    backend_contract = False
    try:
        backend = routed_result_to_backend(result)
        backend_contract = (
            backend.request_id == case.id
            and backend.comparisons == result.comparisons
            and (
                result.status != "executed"
                or len(
                    [
                        citation
                        for citation in backend.citations
                        if citation.kind == "comparison_field"
                    ]
                )
                == len(result.comparisons)
            )
        )
    except ValueError:
        backend_contract = False

    executed = expected.status == "executed"
    checks = {
        "status": result.status == expected.status,
        "plan_exact": (
            result.query_plan is not None
            and result.query_plan.intent is Intent.COMPARE
            and result.query_plan.product_families == [case.product_family]
            and result.query_plan.intent_payload.comparison_fields == expected.comparison_fields
        )
        if executed
        else result.query_plan is None,
        "product_order": product_ids == expected.product_ids,
        "field_order": comparison_fields == expected.comparison_fields,
        "comparison_values": (
            field_statuses == expected.field_statuses and deltas == expected.deltas
        ),
        "candidate_count": result.candidate_count == 2
        if executed
        else result.candidate_count is None,
        "backend_contract": backend_contract,
        "control_suppression": (
            not result.products
            and not result.comparisons
            and result.candidate_count is None
            and result.source_manifest is None
        )
        if not executed
        else True,
        "answer_contract": all(fragment in result.answer for fragment in expected.answer_contains),
    }
    return ProductComparisonCaseResult(
        case_id=case.id,
        product_family=case.product_family,
        category=case.category,
        question=case.question,
        expected_status=expected.status,
        actual_status=result.status,
        passed=all(checks.values()),
        latency_ms=round(latency_ms, 3),
        checks=checks,
        product_ids=product_ids,
        comparison_fields=comparison_fields,
        field_statuses=field_statuses,
        deltas=deltas,
        error=None,
    )


class ProductComparisonEvaluationRunner:
    def __init__(
        self,
        database_paths: dict[ProductFamily | str, str | Path],
    ) -> None:
        self.agent = RoutedFinanceAgent(database_paths)

    @property
    def cache_stats(self) -> RecordCacheStats:
        return self.agent.record_cache.stats()

    @property
    def identity_cache_stats(self) -> ProductIdentityCacheStats:
        return self.agent.identity_cache.stats()

    def evaluate(self, case: ProductComparisonCase) -> ProductComparisonCaseResult:
        started = time.perf_counter()
        try:
            result = self.agent.answer(case.question, case.id)
            latency_ms = (time.perf_counter() - started) * 1000
            return evaluate_product_comparison_result(
                case,
                result,
                latency_ms=latency_ms,
            )
        except Exception as error:
            latency_ms = (time.perf_counter() - started) * 1000
            return ProductComparisonCaseResult(
                case_id=case.id,
                product_family=case.product_family,
                category=case.category,
                question=case.question,
                expected_status=case.expected.status,
                actual_status="error",
                passed=False,
                latency_ms=round(latency_ms, 3),
                checks={},
                product_ids=[],
                comparison_fields=[],
                field_statuses={},
                deltas={},
                error=f"{type(error).__name__}: {error}",
            )

    def run(
        self,
        cases: list[ProductComparisonCase],
        workers: int = 1,
    ) -> list[ProductComparisonCaseResult]:
        if workers < 1:
            raise ValueError("workers must be at least one")
        if workers == 1:
            return [self.evaluate(case) for case in cases]
        with ThreadPoolExecutor(max_workers=workers) as executor:
            return list(executor.map(self.evaluate, cases))


def build_product_comparison_report(
    loaded: LoadedProductComparisonSuite,
    results: list[ProductComparisonCaseResult],
    *,
    workers: int,
    cache_stats: RecordCacheStats,
    identity_cache_stats: ProductIdentityCacheStats,
) -> ProductComparisonReport:
    if len(results) != len(loaded.suite.cases):
        raise ValueError("report requires one result for every suite case")
    executable = [result for result in results if result.expected_status == "executed"]
    controls = [result for result in results if result.expected_status != "executed"]

    def passed_check(items: list[ProductComparisonCaseResult], name: str) -> int:
        return sum(item.checks.get(name, False) for item in items)

    passed = sum(result.passed for result in results)
    latencies = [result.latency_ms for result in results]
    return ProductComparisonReport(
        suite_id=loaded.suite.suite_id,
        suite_version=loaded.suite.suite_version,
        suite_sha256=loaded.suite_sha256,
        status=loaded.suite.status,
        complements_suite_id=loaded.suite.complements_suite_id,
        provider="deterministic_server",
        workers=workers,
        data=loaded.suite.data,
        identity_cache=ProductComparisonCacheSummary(
            hits=identity_cache_stats.hits,
            misses=identity_cache_stats.misses,
            loads=identity_cache_stats.loads,
            invalidations=identity_cache_stats.invalidations,
            evictions=identity_cache_stats.evictions,
            entries=identity_cache_stats.entries,
            records=identity_cache_stats.records,
        ),
        record_cache=ProductComparisonCacheSummary(
            hits=cache_stats.hits,
            misses=cache_stats.misses,
            loads=cache_stats.loads,
            invalidations=cache_stats.invalidations,
            evictions=cache_stats.evictions,
            entries=cache_stats.entries,
            records=cache_stats.records,
        ),
        summary=ProductComparisonSummary(
            total=len(results),
            passed=passed,
            strict_accuracy=_rate(passed, len(results)),
            executable_cases=len(executable),
            control_cases=len(controls),
            plan_exact_rate=_rate(
                passed_check(executable, "plan_exact"),
                len(executable),
            ),
            product_order_exact_rate=_rate(
                passed_check(executable, "product_order"),
                len(executable),
            ),
            field_exact_rate=_rate(
                passed_check(executable, "field_order"),
                len(executable),
            ),
            comparison_exact_rate=_rate(
                passed_check(executable, "comparison_values"),
                len(executable),
            ),
            backend_contract_rate=_rate(
                passed_check(results, "backend_contract"),
                len(results),
            ),
            control_suppression_rate=_rate(
                passed_check(controls, "control_suppression"),
                len(controls),
            ),
            answer_contract_rate=_rate(
                passed_check(results, "answer_contract"),
                len(results),
            ),
            latency_ms_p50=_percentile(latencies, 0.5),
            latency_ms_p95=_percentile(latencies, 0.95),
            latency_ms_max=round(max(latencies, default=0.0), 3),
            failures=[result.case_id for result in results if not result.passed],
            by_product_family=_group_metrics(results, "product_family"),
            by_category=_group_metrics(results, "category"),
        ),
        results=results,
    )
