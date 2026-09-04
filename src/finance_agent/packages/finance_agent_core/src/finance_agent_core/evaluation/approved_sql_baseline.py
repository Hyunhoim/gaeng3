from __future__ import annotations

import hashlib
import json
import math
import resource
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from finance_agent_core.contracts.queryplan import ProductFamily
from finance_agent_core.evaluation.search_aggregate_benchmark import (
    BenchmarkIntent,
    SearchAggregateBenchmarkCase,
    execute_benchmark_case,
    load_search_aggregate_benchmark_suite,
)
from finance_agent_core.storage import (
    load_approved_dataset_manifest,
    require_approved_database,
    require_approved_database_paths,
)


class ApprovedSQLBaselineModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ApprovedSQLCaseResult(ApprovedSQLBaselineModel):
    case_id: str
    product_family: ProductFamily
    intent: BenchmarkIntent
    status: Literal["executed", "clarify", "unsupported", "error"]
    passed: bool
    latency_ms: float = Field(ge=0)
    peak_rss_kib: int = Field(ge=0)
    max_rss_delta_kib: int = Field(ge=0)
    candidate_count: int | None
    status_exact: bool
    intent_exact: bool
    candidate_count_exact: bool
    product_id_exact: bool | None
    aggregate_field_value_exact: bool | None
    observed_fingerprint_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    error_kind: str | None = None


class ApprovedSQLSummary(ApprovedSQLBaselineModel):
    total: int
    passed: int
    strict_accuracy: float = Field(ge=0, le=1)
    search_product_id_cases: int
    search_product_id_exact: int
    search_product_id_exact_rate: float = Field(ge=0, le=1)
    aggregate_cases: int
    aggregate_field_value_exact: int
    aggregate_field_value_exact_rate: float = Field(ge=0, le=1)
    candidate_count_exact: int
    candidate_count_exact_rate: float = Field(ge=0, le=1)
    measured_cases: int = Field(ge=0)
    percentile_method: Literal["nearest_rank"] = "nearest_rank"
    latency_ms_p50: float = Field(ge=0)
    latency_ms_p95: float = Field(ge=0)
    latency_ms_max: float = Field(ge=0)
    peak_rss_kib_p50: int = Field(ge=0)
    peak_rss_kib_p95: int = Field(ge=0)
    peak_rss_kib_max: int = Field(ge=0)
    max_rss_delta_kib_p50: int = Field(ge=0)
    max_rss_delta_kib_p95: int = Field(ge=0)
    max_rss_delta_kib_max: int = Field(ge=0)
    failures: list[str]


class ApprovedSQLExecutionBreakdown(ApprovedSQLBaselineModel):
    routed_active_cases: int = Field(ge=0)
    domestic_mock_cases: int = Field(ge=0)
    fund_internal_disabled_override_cases: int = Field(ge=0)


class ApprovedSQLBaselineReport(ApprovedSQLBaselineModel):
    schema_version: Literal["1.0"] = "1.0"
    evaluation_id: Literal["approved-sql-search-aggregate-v1"] = "approved-sql-search-aggregate-v1"
    status: Literal["public_regression_not_blind"] = "public_regression_not_blind"
    approved_release_id: str
    approved_manifest_canonical_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    database_sha256_by_family: dict[str, str]
    expectation_suite_id: str
    expectation_suite_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    expectation_provenance: Literal["same_source_workbooks_prior_normalized_db_build"] = (
        "same_source_workbooks_prior_normalized_db_build"
    )
    isolation: Literal["fresh_process_per_case_after_approval_validation"] = (
        "fresh_process_per_case_after_approval_validation"
    )
    execution_scope: Literal["mixed_public_oracle_regression_not_fastapi_e2e"] = (
        "mixed_public_oracle_regression_not_fastapi_e2e"
    )
    execution_breakdown: ApprovedSQLExecutionBreakdown
    latency_scope: Literal[
        "agent_core_execution_only_excludes_process_startup_approval_and_json_io"
    ] = "agent_core_execution_only_excludes_process_startup_approval_and_json_io"
    sample_size_warning: str
    summary: ApprovedSQLSummary
    results: list[ApprovedSQLCaseResult]


def _canonical_sha256(payload: object) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _rss_kib() -> int:
    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return round(peak / 1024) if sys.platform == "darwin" else int(peak)


def _percentile(values: Sequence[float | int], percentile: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(float(value) for value in values)
    index = max(0, math.ceil(percentile * len(ordered)) - 1)
    return ordered[index]


def approved_database_paths(database_dir: Path) -> dict[ProductFamily, Path]:
    return {family: database_dir / f"{family.value}.sqlite3" for family in ProductFamily}


def validate_approved_baseline_contract(database_dir: Path) -> None:
    paths = approved_database_paths(database_dir)
    require_approved_database_paths(paths)
    approval = load_approved_dataset_manifest()
    suite = load_search_aggregate_benchmark_suite().suite
    for family in ProductFamily:
        suite_source = suite.data[family].source_file_sha256
        approved_source = approval.datasets[family.value].data_file_sha256
        if suite_source != approved_source:
            raise RuntimeError(
                f"{family.value} source workbook differs from the regression expectation"
            )


def execute_approved_sql_case(
    case: SearchAggregateBenchmarkCase,
    database_path: Path,
) -> ApprovedSQLCaseResult:
    require_approved_database(case.product_family.value, database_path)
    result = execute_benchmark_case(case, database_path)
    checks = result.checks
    fingerprint_sha256 = _canonical_sha256(result.fingerprint)
    error_kind = None
    if result.error:
        error_kind = result.error.split(":", maxsplit=1)[0][:100]
    return ApprovedSQLCaseResult(
        case_id=case.id,
        product_family=case.product_family,
        intent=case.intent,
        status=result.status,
        passed=result.passed,
        latency_ms=result.latency_ms,
        peak_rss_kib=_rss_kib(),
        max_rss_delta_kib=result.max_rss_delta_kib,
        candidate_count=result.candidate_count,
        status_exact=checks.get("status", False),
        intent_exact=checks.get("intent", False),
        candidate_count_exact=checks.get("candidate_count", False),
        product_id_exact=(
            checks.get("top_product_ids", False) if case.intent == "search" else None
        ),
        aggregate_field_value_exact=(
            checks.get("aggregates", False) if case.intent == "aggregate" else None
        ),
        observed_fingerprint_sha256=fingerprint_sha256,
        error_kind=error_kind,
    )


class ApprovedSQLBaselineRunner:
    def __init__(self, database_dir: Path, *, case_timeout_seconds: float = 60.0) -> None:
        if not 0 < case_timeout_seconds <= 300:
            raise ValueError("SQL child timeout must be between 0 and 300 seconds")
        self.database_dir = database_dir
        self.case_timeout_seconds = case_timeout_seconds

    @staticmethod
    def _failed_result(
        case: SearchAggregateBenchmarkCase,
        error_kind: str,
    ) -> ApprovedSQLCaseResult:
        return ApprovedSQLCaseResult(
            case_id=case.id,
            product_family=case.product_family,
            intent=case.intent,
            status="error",
            passed=False,
            latency_ms=0,
            peak_rss_kib=0,
            max_rss_delta_kib=0,
            candidate_count=None,
            status_exact=False,
            intent_exact=False,
            candidate_count_exact=False,
            product_id_exact=False if case.intent == "search" else None,
            aggregate_field_value_exact=False if case.intent == "aggregate" else None,
            observed_fingerprint_sha256=_canonical_sha256({}),
            error_kind=error_kind,
        )

    def run_case(self, case: SearchAggregateBenchmarkCase) -> ApprovedSQLCaseResult:
        command = [
            sys.executable,
            "-m",
            "finance_agent_core.evaluation.approved_sql_baseline_cli",
            "--database-dir",
            str(self.database_dir),
            "--child-case",
            case.id,
        ]
        try:
            completed = subprocess.run(
                command,
                check=False,
                capture_output=True,
                text=True,
                timeout=self.case_timeout_seconds,
            )
        except subprocess.TimeoutExpired:
            return self._failed_result(case, "child_process_timeout")
        if completed.returncode != 0:
            return self._failed_result(case, "child_process_failure")
        try:
            return ApprovedSQLCaseResult.model_validate_json(completed.stdout)
        except Exception:
            return self._failed_result(case, "invalid_child_result")

    def run(self, cases: Sequence[SearchAggregateBenchmarkCase]) -> list[ApprovedSQLCaseResult]:
        return [self.run_case(case) for case in cases]


def build_approved_sql_baseline_report(
    results: list[ApprovedSQLCaseResult],
) -> ApprovedSQLBaselineReport:
    loaded = load_search_aggregate_benchmark_suite()
    expected_by_id = {case.id: case for case in loaded.suite.cases}
    result_by_id = {result.case_id: result for result in results}
    if (
        len(results) != len(loaded.suite.cases)
        or len(result_by_id) != len(results)
        or set(result_by_id) != set(expected_by_id)
    ):
        raise ValueError("approved SQL report requires all eight benchmark cases")
    for case_id, result in result_by_id.items():
        expected = expected_by_id[case_id]
        if result.product_family is not expected.product_family or result.intent != expected.intent:
            raise ValueError(f"approved SQL result identity differs for benchmark case: {case_id}")
    approval = load_approved_dataset_manifest()
    domestic_mock_cases = sum(case.executor == "domestic_mock" for case in loaded.suite.cases)
    fund_internal_cases = sum(
        case.executor == "routed" and case.product_family is ProductFamily.FUND
        for case in loaded.suite.cases
    )
    routed_active_cases = len(loaded.suite.cases) - domestic_mock_cases - fund_internal_cases
    searches = [item for item in results if item.intent == "search"]
    aggregates = [item for item in results if item.intent == "aggregate"]
    measured_results = [item for item in results if item.status != "error"]
    latencies = [item.latency_ms for item in measured_results]
    peak_rss = [item.peak_rss_kib for item in measured_results]
    rss_delta = [item.max_rss_delta_kib for item in measured_results]
    passed = sum(item.passed for item in results)
    product_exact = sum(item.product_id_exact is True for item in searches)
    aggregate_exact = sum(item.aggregate_field_value_exact is True for item in aggregates)
    candidate_exact = sum(item.candidate_count_exact for item in results)
    return ApprovedSQLBaselineReport(
        approved_release_id=approval.release_id,
        approved_manifest_canonical_sha256=approval.canonical_sha256,
        database_sha256_by_family={
            family.value: approval.datasets[family.value].database_sha256
            for family in ProductFamily
        },
        expectation_suite_id=loaded.suite.suite_id,
        expectation_suite_sha256=loaded.suite_sha256,
        execution_breakdown=ApprovedSQLExecutionBreakdown(
            routed_active_cases=routed_active_cases,
            domestic_mock_cases=domestic_mock_cases,
            fund_internal_disabled_override_cases=fund_internal_cases,
        ),
        sample_size_warning=(
            "표본이 8개뿐이므로 p95는 최댓값에 가깝고 운영 SLO로 해석할 수 없습니다."
        ),
        summary=ApprovedSQLSummary(
            total=len(results),
            passed=passed,
            strict_accuracy=round(passed / len(results), 6),
            search_product_id_cases=len(searches),
            search_product_id_exact=product_exact,
            search_product_id_exact_rate=round(product_exact / len(searches), 6),
            aggregate_cases=len(aggregates),
            aggregate_field_value_exact=aggregate_exact,
            aggregate_field_value_exact_rate=round(aggregate_exact / len(aggregates), 6),
            candidate_count_exact=candidate_exact,
            candidate_count_exact_rate=round(candidate_exact / len(results), 6),
            measured_cases=len(measured_results),
            latency_ms_p50=round(_percentile(latencies, 0.5), 3),
            latency_ms_p95=round(_percentile(latencies, 0.95), 3),
            latency_ms_max=round(max(latencies, default=0), 3),
            peak_rss_kib_p50=round(_percentile(peak_rss, 0.5)),
            peak_rss_kib_p95=round(_percentile(peak_rss, 0.95)),
            peak_rss_kib_max=max(peak_rss, default=0),
            max_rss_delta_kib_p50=round(_percentile(rss_delta, 0.5)),
            max_rss_delta_kib_p95=round(_percentile(rss_delta, 0.95)),
            max_rss_delta_kib_max=max(rss_delta, default=0),
            failures=[item.case_id for item in results if not item.passed],
        ),
        results=results,
    )


__all__ = [
    "ApprovedSQLBaselineReport",
    "ApprovedSQLBaselineRunner",
    "ApprovedSQLCaseResult",
    "approved_database_paths",
    "build_approved_sql_baseline_report",
    "execute_approved_sql_case",
    "validate_approved_baseline_contract",
]
