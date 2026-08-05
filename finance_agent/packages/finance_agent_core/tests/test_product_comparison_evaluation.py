from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from finance_agent_core.agent import RoutedFinanceAgent
from finance_agent_core.contracts.queryplan import ProductFamily
from finance_agent_core.evaluation.product_comparison_runner import (
    ProductComparisonCase,
    ProductComparisonEvaluationRunner,
    ProductComparisonExpectation,
    build_product_comparison_report,
    evaluate_product_comparison_result,
    load_product_comparison_suite,
)


def test_product_comparison_suite_freezes_three_family_extension() -> None:
    loaded = load_product_comparison_suite()

    assert loaded.suite.suite_id == "product-compare-core-30"
    assert len(loaded.suite.cases) == 30
    assert loaded.suite.complements_suite_id == "fund-compare-e2e-core-24"
    assert len(loaded.suite_sha256) == 64
    for family in (
        ProductFamily.OVERSEAS_ETP,
        ProductFamily.DOMESTIC_ETP,
        ProductFamily.BOND,
    ):
        cases = [case for case in loaded.suite.cases if case.product_family is family]
        assert len(cases) == 10
        assert sum(case.expected.status == "executed" for case in cases) == 6


def test_product_comparison_expectation_rejects_delta_status_mismatch() -> None:
    with pytest.raises(ValidationError, match="may declare a delta"):
        ProductComparisonExpectation(
            status="executed",
            product_ids=["AMX:B1", "AMX:B2"],
            comparison_fields=["asset_type"],
            field_statuses={"asset_type": "value_only"},
            deltas={"asset_type": "1"},
            answer_contains=["투자 자산 유형"],
        )


def test_product_comparison_evaluator_checks_executed_result(
    sample_database: tuple[Path, list[object], object],
) -> None:
    path, _, _ = sample_database
    question = "해외 ETF AMX:B2와 AMX:B1의 총보수율과 AUM을 비교해줘"
    case = ProductComparisonCase(
        id="product-compare-overseas-901",
        product_family="overseas_etp",
        category="synthetic_executed",
        question=question,
        expected={
            "status": "executed",
            "product_ids": ["AMX:B2", "AMX:B1"],
            "comparison_fields": ["total_expense_ratio_pct", "aum"],
            "field_statuses": {
                "total_expense_ratio_pct": "numeric_delta",
                "aum": "numeric_delta",
            },
            "deltas": {
                "total_expense_ratio_pct": "-0.05",
                "aum": "-2000",
            },
            "answer_contains": ["요청한 해외 ETP 2개 중 2개", "차이(두 번째-첫 번째)"],
        },
    )
    result = RoutedFinanceAgent({"overseas_etp": path}).answer(question, case.id)

    evaluated = evaluate_product_comparison_result(case, result, latency_ms=1.0)

    assert evaluated.passed
    assert all(evaluated.checks.values())


def test_product_comparison_evaluator_checks_control_suppression(
    sample_database: tuple[Path, list[object], object],
) -> None:
    path, _, _ = sample_database
    question = "해외 ETF AMX:B1은 제외하고 AMX:B2의 AUM을 비교해줘"
    case = ProductComparisonCase(
        id="product-compare-overseas-902",
        product_family="overseas_etp",
        category="synthetic_control",
        question=question,
        expected={
            "status": "clarify",
            "product_ids": [],
            "comparison_fields": [],
            "field_statuses": {},
            "deltas": {},
            "answer_contains": ["비교 대상 역할을 바꾸는 표현"],
        },
    )
    result = RoutedFinanceAgent({"overseas_etp": path}).answer(question, case.id)

    evaluated = evaluate_product_comparison_result(case, result, latency_ms=1.0)

    assert evaluated.passed
    assert evaluated.checks["control_suppression"]
    assert evaluated.checks["backend_contract"]


def test_product_comparison_report_records_cache_observability(
    sample_database: tuple[Path, list[object], object],
) -> None:
    path, records, _ = sample_database
    loaded = load_product_comparison_suite()
    case = ProductComparisonCase(
        id="product-compare-overseas-903",
        product_family="overseas_etp",
        category="synthetic_cache",
        question="해외 ETF AMX:B1과 AMX:B2의 AUM을 비교해줘",
        expected={
            "status": "executed",
            "product_ids": ["AMX:B1", "AMX:B2"],
            "comparison_fields": ["aum"],
            "field_statuses": {"aum": "numeric_delta"},
            "deltas": {"aum": "2000"},
            "answer_contains": ["요청한 해외 ETP 2개 중 2개"],
        },
    )
    runner = ProductComparisonEvaluationRunner({"overseas_etp": path})
    result = runner.evaluate(case)
    synthetic_loaded = loaded.__class__(
        suite=loaded.suite.model_copy(update={"cases": [case]}),
        suite_sha256=loaded.suite_sha256,
    )

    report = build_product_comparison_report(
        synthetic_loaded,
        [result],
        workers=1,
        cache_stats=runner.cache_stats,
        identity_cache_stats=runner.identity_cache_stats,
    )

    assert report.schema_version == "1.1"
    assert report.identity_cache.hits == 0
    assert report.identity_cache.misses == 1
    assert report.identity_cache.loads == 1
    assert report.identity_cache.records == len(records)
    assert report.record_cache.loads == 0
    assert report.record_cache.records == 0
