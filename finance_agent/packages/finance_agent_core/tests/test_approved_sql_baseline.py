import json
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from finance_agent_core.evaluation.approved_sql_baseline import (
    ApprovedSQLBaselineRunner,
    ApprovedSQLCaseResult,
    build_approved_sql_baseline_report,
)
from finance_agent_core.evaluation.search_aggregate_benchmark import (
    load_search_aggregate_benchmark_suite,
)
from finance_agent_core.storage import load_approved_dataset_manifest


def _perfect_results() -> list[ApprovedSQLCaseResult]:
    loaded = load_search_aggregate_benchmark_suite()
    return [
        ApprovedSQLCaseResult(
            case_id=case.id,
            product_family=case.product_family,
            intent=case.intent,
            status="executed",
            passed=True,
            latency_ms=index + 1,
            peak_rss_kib=1000 + (index * 100),
            max_rss_delta_kib=index * 10,
            candidate_count=case.expected.candidate_count,
            status_exact=True,
            intent_exact=True,
            candidate_count_exact=True,
            product_id_exact=True if case.intent == "search" else None,
            aggregate_field_value_exact=True if case.intent == "aggregate" else None,
            observed_fingerprint_sha256=f"{index:064x}",
        )
        for index, case in enumerate(loaded.suite.cases)
    ]


def test_approved_sql_expectations_share_the_current_source_workbooks() -> None:
    suite = load_search_aggregate_benchmark_suite().suite
    approval = load_approved_dataset_manifest()

    assert all(
        suite.data[family].source_file_sha256 == approval.datasets[family.value].data_file_sha256
        for family in suite.data
    )
    assert any(
        suite.data[family].database_sha256 != approval.datasets[family.value].database_sha256
        for family in suite.data
    )


def test_approved_sql_report_keeps_exactness_but_removes_raw_product_ids() -> None:
    loaded = load_search_aggregate_benchmark_suite()
    report = build_approved_sql_baseline_report(_perfect_results())
    serialized = json.dumps(report.model_dump(mode="json"), ensure_ascii=False)
    raw_product_ids = [
        product_id for case in loaded.suite.cases for product_id in case.expected.top_product_ids
    ]

    assert report.summary.passed == 8
    assert report.summary.strict_accuracy == 1
    assert report.summary.search_product_id_exact == 4
    assert report.summary.aggregate_field_value_exact == 4
    assert report.summary.candidate_count_exact == 8
    assert report.summary.measured_cases == 8
    assert report.execution_scope == "mixed_public_oracle_regression_not_fastapi_e2e"
    assert (
        report.latency_scope
        == "agent_core_execution_only_excludes_process_startup_approval_and_json_io"
    )
    assert report.execution_breakdown.routed_active_cases == 5
    assert report.execution_breakdown.domestic_mock_cases == 1
    assert report.execution_breakdown.fund_internal_disabled_override_cases == 2
    assert all(product_id not in serialized for product_id in raw_product_ids)


def test_approved_sql_report_rejects_duplicate_or_mismatched_case_identity() -> None:
    results = _perfect_results()
    duplicate = [*results[:-1], results[0]]

    with pytest.raises(ValueError, match="all eight benchmark cases"):
        build_approved_sql_baseline_report(duplicate)

    wrong_family = [
        results[0].model_copy(update={"product_family": results[1].product_family}),
        *results[1:],
    ]
    with pytest.raises(ValueError, match="identity differs"):
        build_approved_sql_baseline_report(wrong_family)


def test_approved_sql_report_excludes_child_errors_from_runtime_percentiles() -> None:
    results = _perfect_results()
    results[-1] = results[-1].model_copy(
        update={
            "status": "error",
            "passed": False,
            "latency_ms": 0,
            "peak_rss_kib": 0,
            "max_rss_delta_kib": 0,
            "error_kind": "child_process_timeout",
        }
    )

    report = build_approved_sql_baseline_report(results)

    assert report.summary.measured_cases == 7
    assert report.summary.latency_ms_p50 == 4
    assert report.summary.peak_rss_kib_p50 == 1300


def test_approved_sql_runner_fails_closed_on_child_timeout() -> None:
    case = load_search_aggregate_benchmark_suite().suite.cases[0]
    runner = ApprovedSQLBaselineRunner(Path("/tmp/approved-sql-test"), case_timeout_seconds=1)

    with patch(
        "finance_agent_core.evaluation.approved_sql_baseline.subprocess.run",
        side_effect=subprocess.TimeoutExpired(cmd=["python"], timeout=1),
    ):
        result = runner.run_case(case)

    assert result.status == "error"
    assert not result.passed
    assert result.error_kind == "child_process_timeout"
