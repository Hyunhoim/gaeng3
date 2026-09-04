from __future__ import annotations

from pathlib import Path

import pytest
from finance_agent_core.agent import IntentRouter, RoutedFinanceAgent
from finance_agent_core.agent.compiler import (
    _augment_exact_detail_projection,
    _collapse_repeated_fund_identity_labels,
    _replace_bound_fund_names_with_ids,
)
from finance_agent_core.agent.product_comparison import (
    extract_comparison_fields as extract_product_comparison_fields,
)
from finance_agent_core.contracts.queryplan import ProductFamily
from finance_agent_core.contracts.routing import InteractionIntent, RouteDisposition
from finance_agent_core.evaluation.product_comparison_runner import (
    ProductComparisonCase,
    ProductComparisonEvaluationRunner,
    ProductComparisonExpectation,
    build_product_comparison_report,
    evaluate_product_comparison_result,
    load_product_comparison_suite,
)
from finance_agent_core.storage import ProductIdentityRecord
from pydantic import ValidationError


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


def test_bond_applied_yield_label_is_supported_by_comparison_parser() -> None:
    assert extract_product_comparison_fields(
        "국내채권 두 종목의 적용수익률을 나란히 비교해줘",
        ProductFamily.BOND,
    ) == ["applied_yield_pct"]


def test_fund_comparison_fields_support_three_product_projection() -> None:
    assert extract_product_comparison_fields(
        "세 펀드의 1년 수익률과 판매회사 보수를 차례대로 적어 주세요",
        ProductFamily.FUND,
    ) == ["one_year_return_pct", "sale_company_fee_pct"]


def test_coordinated_period_and_ratio_projection_expands_each_field() -> None:
    assert extract_product_comparison_fields(
        "1개월·3개월·1년 수익률을 나란히 보여 주세요",
        ProductFamily.FUND,
    ) == [
        "one_month_return_pct",
        "three_month_return_pct",
        "one_year_return_pct",
    ]
    assert extract_product_comparison_fields(
        "수익률을 1개월·3개월·1년 기준으로 나란히 적어 주세요",
        ProductFamily.FUND,
    ) == [
        "one_month_return_pct",
        "three_month_return_pct",
        "one_year_return_pct",
    ]
    assert extract_product_comparison_fields(
        "국내주식·해외주식 편입비를 순서대로 적어 주세요",
        ProductFamily.FUND,
    ) == ["domestic_stock_ratio_pct", "overseas_stock_ratio_pct"]


def test_exact_read_field_language_covers_schema_labels_and_safe_ellipsis() -> None:
    assert extract_product_comparison_fields(
        "발행자, 대분류·세부분류, 신용등급, 발행액, 평가가격을 보여 줘",
        ProductFamily.BOND,
    ) == [
        "issuer",
        "bond_major_class",
        "bond_subclass",
        "credit_rating",
        "issue_amount",
        "evaluation_price",
    ]
    assert extract_product_comparison_fields(
        "유형, 운용사, 기초지수와 매매 가능 여부를 보여 주세요",
        ProductFamily.DOMESTIC_ETP,
    ) == ["product_type", "manager", "base_index", "sellable"]
    assert extract_product_comparison_fields(
        "이 해외 상품의 유형, 자산군, 거래소를 나란히 적어 줘",
        ProductFamily.OVERSEAS_ETP,
    ) == ["product_type", "asset_type", "exchange_code"]


def test_exact_fund_detail_expands_elided_public_and_sellable_status() -> None:
    payload: dict[str, object] = {"projection": ["product_id", "product_name"]}

    _augment_exact_detail_projection(
        payload,
        ProductFamily.FUND,
        "위험등급, 환헤지, 공모 및 판매 가능 여부를 보고 싶어요",
    )

    assert {
        "risk_level",
        "currency_hedged",
        "public_offering",
        "sellable",
    }.issubset(payload["projection"])


def test_fund_name_and_repeated_labeled_id_collapse_to_one_identity() -> None:
    records = (
        ProductIdentityRecord(
            product_family="fund",
            product_id="KR5000000001",
            product_name="아틀라스장기성장자투자신탁 C-e",
            short_name="아틀라스장기성장 C-e",
            ticker=None,
            isin=None,
            public_offering=True,
            is_quarantined=False,
        ),
    )

    rewritten = _replace_bound_fund_names_with_ids(
        "‘아틀라스장기성장자투자신탁 C-e’(ID KR5000000001)의 AUM을 알려줘",
        ["KR5000000001"],
        records,
    )

    assert _collapse_repeated_fund_identity_labels(rewritten) == (
        "KR5000000001의 AUM을 알려줘"
    )


@pytest.mark.parametrize(
    "question",
    [
        "해외 ETF ALPH.K와 BETA.K 중 AUM과 일간 수익률이 어떻게 다른지 알려 주세요.",
        "해외 ETP FUND.N과 NEXT.O의 총보수와 AUM이 무엇이 다른지 비교해 주세요.",
    ],
)
def test_dotted_ticker_how_different_wording_routes_to_compare(question: str) -> None:
    decision = IntentRouter().route(question, "dotted-ticker-comparison")

    assert decision.disposition is RouteDisposition.EXECUTE
    assert decision.draft.intent is InteractionIntent.COMPARE
    assert decision.draft.product_families == [ProductFamily.OVERSEAS_ETP]


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
    # Exact-identity family corroboration loads once and the comparison
    # compiler reuses the same immutable snapshot.
    assert report.identity_cache.hits == 1
    assert report.identity_cache.misses == 1
    assert report.identity_cache.loads == 1
    assert report.identity_cache.records == len(records)
    assert report.record_cache.loads == 0
    assert report.record_cache.records == 0
