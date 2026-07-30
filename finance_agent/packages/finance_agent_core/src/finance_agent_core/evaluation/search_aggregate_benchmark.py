from __future__ import annotations

import hashlib
import json
import math
import resource
import subprocess
import sys
import time
from dataclasses import dataclass
from importlib.resources import files
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from finance_agent_core.agent import (
    FinanceAgent,
    RoutedAgentResult,
    RoutedFinanceAgent,
)
from finance_agent_core.agent.providers import DomesticMockProvider
from finance_agent_core.contracts.queryplan import Intent, ProductFamily

type BenchmarkIntent = Literal["search", "aggregate"]
type BenchmarkExecutor = Literal["routed", "domestic_mock"]


class SearchAggregateBenchmarkModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class BenchmarkDataContract(SearchAggregateBenchmarkModel):
    database_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_file_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class BenchmarkExpectation(SearchAggregateBenchmarkModel):
    candidate_count: int = Field(ge=0)
    top_product_ids: list[str] = Field(default_factory=list, max_length=10)
    aggregates: list[dict[str, object]] = Field(default_factory=list, max_length=20)


class SearchAggregateBenchmarkCase(SearchAggregateBenchmarkModel):
    id: str = Field(
        pattern=(
            r"^search-aggregate-perf-"
            r"(?:overseas-etp|domestic-etp|bond|fund)-(?:search|aggregate)$"
        )
    )
    product_family: ProductFamily
    intent: BenchmarkIntent
    executor: BenchmarkExecutor
    question: str = Field(min_length=5, max_length=2000)
    expected: BenchmarkExpectation

    @model_validator(mode="after")
    def validate_expectation(self) -> SearchAggregateBenchmarkCase:
        if self.intent == "search":
            if not self.expected.top_product_ids or self.expected.aggregates:
                raise ValueError("SEARCH requires product IDs and no aggregate fingerprint")
        elif self.expected.top_product_ids or not self.expected.aggregates:
            raise ValueError("AGGREGATE requires aggregate fingerprints and no product IDs")
        if self.executor == "domestic_mock" and (
            self.product_family is not ProductFamily.DOMESTIC_ETP or self.intent != "search"
        ):
            raise ValueError("domestic_mock is reserved for domestic ETP SEARCH")
        return self


class SearchAggregateBenchmarkSuite(SearchAggregateBenchmarkModel):
    schema_version: Literal["1.0"]
    suite_id: Literal["search-aggregate-performance-8"]
    suite_version: Literal["1.0"]
    status: Literal["public_performance_regression_not_blind"]
    data: dict[ProductFamily, BenchmarkDataContract]
    cases: list[SearchAggregateBenchmarkCase] = Field(min_length=8, max_length=8)

    @model_validator(mode="after")
    def validate_coverage(self) -> SearchAggregateBenchmarkSuite:
        expected_families = set(ProductFamily)
        if set(self.data) != expected_families:
            raise ValueError("data hashes must cover all four product families")
        pairs = {(case.product_family, case.intent) for case in self.cases}
        expected_pairs = {
            (family, intent) for family in expected_families for intent in ("search", "aggregate")
        }
        if pairs != expected_pairs:
            raise ValueError("suite requires one SEARCH and one AGGREGATE per family")
        if len({case.id for case in self.cases}) != len(self.cases):
            raise ValueError("benchmark case IDs must be unique")
        return self


@dataclass(frozen=True)
class LoadedSearchAggregateBenchmarkSuite:
    suite: SearchAggregateBenchmarkSuite
    suite_sha256: str


class SearchAggregateCaseResult(SearchAggregateBenchmarkModel):
    case_id: str
    product_family: ProductFamily
    intent: BenchmarkIntent
    status: Literal["executed", "clarify", "unsupported", "error"]
    passed: bool
    latency_ms: float = Field(ge=0)
    max_rss_delta_kib: int = Field(ge=0)
    candidate_count: int | None
    fingerprint: dict[str, object]
    checks: dict[str, bool]
    error: str | None = None


class SearchAggregateBenchmarkSummary(SearchAggregateBenchmarkModel):
    total: int
    passed: int
    strict_accuracy: float
    latency_ms_p50: float
    latency_ms_p95: float
    latency_ms_max: float
    max_rss_delta_kib_p50: int
    max_rss_delta_kib_p95: int
    max_rss_delta_kib_max: int
    failures: list[str]
    by_intent: dict[str, dict[str, float | int]]
    by_product_family: dict[str, dict[str, float | int]]


class SearchAggregateBenchmarkReport(SearchAggregateBenchmarkModel):
    schema_version: Literal["1.0"] = "1.0"
    suite_id: str
    suite_version: str
    suite_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    status: Literal["public_performance_regression_not_blind"]
    provider: Literal["deterministic_server"]
    isolation: Literal["fresh_process_per_case"]
    data: dict[ProductFamily, BenchmarkDataContract]
    summary: SearchAggregateBenchmarkSummary
    results: list[SearchAggregateCaseResult]


def load_search_aggregate_benchmark_suite() -> LoadedSearchAggregateBenchmarkSuite:
    resource_path = files("finance_agent_core.evaluation.suites").joinpath(
        "search_aggregate_performance_8.json"
    )
    raw = resource_path.read_bytes()
    return LoadedSearchAggregateBenchmarkSuite(
        suite=SearchAggregateBenchmarkSuite.model_validate(json.loads(raw)),
        suite_sha256=hashlib.sha256(raw).hexdigest(),
    )


def _aggregate_fingerprint(result: RoutedAgentResult) -> list[dict[str, object]]:
    aggregates = result.aggregates
    return [
        {
            "function": aggregate.function.value,
            "field": aggregate.field,
            "group_values": aggregate.group_values,
            "value": aggregate.value,
            "row_count": aggregate.row_count,
            "valid_count": aggregate.valid_count,
            "missing_count": aggregate.missing_count,
        }
        for aggregate in aggregates
    ]


def execute_benchmark_case(
    case: SearchAggregateBenchmarkCase,
    database_path: Path,
) -> SearchAggregateCaseResult:
    rss_before = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    started = time.perf_counter()
    try:
        if case.executor == "domestic_mock":
            result = FinanceAgent(database_path, DomesticMockProvider()).answer(
                case.question,
                case.id,
            )
        else:
            result = RoutedFinanceAgent(
                {case.product_family: database_path},
                allow_internal_disabled_dataset=case.product_family is ProductFamily.FUND,
            ).answer(case.question, case.id)
        latency_ms = (time.perf_counter() - started) * 1000
        rss_after = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        product_ids = [product.product_id for product in result.products]
        aggregate_values = _aggregate_fingerprint(result) if case.intent == "aggregate" else []
        fingerprint: dict[str, object] = {
            "candidate_count": result.candidate_count,
            "top_product_ids": product_ids,
            "aggregates": aggregate_values,
        }
        status = result.status if isinstance(result, RoutedAgentResult) else "executed"
        checks = {
            "status": status == "executed",
            "intent": result.query_plan.intent is Intent(case.intent),
            "candidate_count": result.candidate_count == case.expected.candidate_count,
            "top_product_ids": product_ids == case.expected.top_product_ids,
            "aggregates": aggregate_values == case.expected.aggregates,
        }
        return SearchAggregateCaseResult(
            case_id=case.id,
            product_family=case.product_family,
            intent=case.intent,
            status=status,
            passed=all(checks.values()),
            latency_ms=round(latency_ms, 3),
            max_rss_delta_kib=max(0, rss_after - rss_before),
            candidate_count=result.candidate_count,
            fingerprint=fingerprint,
            checks=checks,
        )
    except Exception as error:
        latency_ms = (time.perf_counter() - started) * 1000
        rss_after = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        return SearchAggregateCaseResult(
            case_id=case.id,
            product_family=case.product_family,
            intent=case.intent,
            status="error",
            passed=False,
            latency_ms=round(latency_ms, 3),
            max_rss_delta_kib=max(0, rss_after - rss_before),
            candidate_count=None,
            fingerprint={},
            checks={},
            error=f"{type(error).__name__}: {error}",
        )


def _percentile(values: list[float | int], percentile: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = max(0, math.ceil(percentile * len(ordered)) - 1)
    return float(ordered[index])


def _group_metrics(
    results: list[SearchAggregateCaseResult],
    attribute: Literal["intent", "product_family"],
) -> dict[str, dict[str, float | int]]:
    grouped: dict[str, list[SearchAggregateCaseResult]] = {}
    for result in results:
        value = getattr(result, attribute)
        key = value.value if isinstance(value, ProductFamily) else value
        grouped.setdefault(key, []).append(result)
    return {
        key: {
            "total": len(items),
            "passed": sum(item.passed for item in items),
            "accuracy": round(sum(item.passed for item in items) / len(items), 6),
            "latency_ms": round(sum(item.latency_ms for item in items) / len(items), 3),
            "max_rss_delta_kib": max(item.max_rss_delta_kib for item in items),
        }
        for key, items in sorted(grouped.items())
    }


def build_search_aggregate_benchmark_report(
    loaded: LoadedSearchAggregateBenchmarkSuite,
    results: list[SearchAggregateCaseResult],
) -> SearchAggregateBenchmarkReport:
    if len(results) != len(loaded.suite.cases):
        raise ValueError("report requires one result for every benchmark case")
    passed = sum(result.passed for result in results)
    latencies = [result.latency_ms for result in results]
    memory = [result.max_rss_delta_kib for result in results]
    return SearchAggregateBenchmarkReport(
        suite_id=loaded.suite.suite_id,
        suite_version=loaded.suite.suite_version,
        suite_sha256=loaded.suite_sha256,
        status=loaded.suite.status,
        provider="deterministic_server",
        isolation="fresh_process_per_case",
        data=loaded.suite.data,
        summary=SearchAggregateBenchmarkSummary(
            total=len(results),
            passed=passed,
            strict_accuracy=round(passed / len(results), 6),
            latency_ms_p50=round(_percentile(latencies, 0.5), 3),
            latency_ms_p95=round(_percentile(latencies, 0.95), 3),
            latency_ms_max=round(max(latencies, default=0), 3),
            max_rss_delta_kib_p50=round(_percentile(memory, 0.5)),
            max_rss_delta_kib_p95=round(_percentile(memory, 0.95)),
            max_rss_delta_kib_max=max(memory, default=0),
            failures=[result.case_id for result in results if not result.passed],
            by_intent=_group_metrics(results, "intent"),
            by_product_family=_group_metrics(results, "product_family"),
        ),
        results=results,
    )


class SearchAggregateBenchmarkRunner:
    def __init__(self, database_dir: Path) -> None:
        self.database_dir = database_dir

    def run_case(
        self,
        case: SearchAggregateBenchmarkCase,
    ) -> SearchAggregateCaseResult:
        command = [
            sys.executable,
            "-m",
            "finance_agent_core.evaluation.search_aggregate_benchmark_cli",
            "--database-dir",
            str(self.database_dir),
            "--child-case",
            case.id,
        ]
        completed = subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
        )
        if completed.returncode != 0:
            return SearchAggregateCaseResult(
                case_id=case.id,
                product_family=case.product_family,
                intent=case.intent,
                status="error",
                passed=False,
                latency_ms=0,
                max_rss_delta_kib=0,
                candidate_count=None,
                fingerprint={},
                checks={},
                error=(
                    f"child exit {completed.returncode}: "
                    f"{completed.stderr.strip() or completed.stdout.strip()}"
                ),
            )
        try:
            return SearchAggregateCaseResult.model_validate_json(completed.stdout)
        except Exception as error:
            return SearchAggregateCaseResult(
                case_id=case.id,
                product_family=case.product_family,
                intent=case.intent,
                status="error",
                passed=False,
                latency_ms=0,
                max_rss_delta_kib=0,
                candidate_count=None,
                fingerprint={},
                checks={},
                error=f"invalid child result: {type(error).__name__}: {error}",
            )

    def run(
        self,
        cases: list[SearchAggregateBenchmarkCase],
    ) -> list[SearchAggregateCaseResult]:
        return [self.run_case(case) for case in cases]
