from __future__ import annotations

import pytest

from finance_agent_core.agent.compiler import ServerQueryPlanCompiler
from finance_agent_core.agent.linker import build_lexical_hints
from finance_agent_core.agent.router import IntentRouter
from finance_agent_core.contracts.routing import RouteDisposition


def _compiled_constraints(question: str) -> set[tuple[str, str, object, str]]:
    decision = IntentRouter().route(question, f"fresh-regression-{abs(hash(question))}")
    assert decision.disposition is RouteDisposition.EXECUTE
    plan = ServerQueryPlanCompiler({}).compile(decision)
    return {
        (item.field, item.operator.value, item.value, item.strength.value)
        for item in plan.constraints
    }


@pytest.mark.parametrize(
    "ending",
    ["분류되고", "분류되며", "분류되었고", "표시되고"],
)
def test_official_overseas_region_survives_connective_endings(ending: str) -> None:
    question = (
        "해외 ETF 가운데 투자 지역이 United States of America로 "
        f"{ending} 총보수 0.25% 이하인 종목을 보수 낮은 순으로 세 개 보여 주세요."
    )

    hints = build_lexical_hints(
        question,
        "overseas_etp",
        force_product_family_hint=True,
    )
    assert {
        (item["field"], item["operator"], item["value"])
        for item in hints["required_constraints"]
    } >= {
        ("investment_region", "eq", "United States of America"),
        ("product_type", "eq", "ETF"),
    }
    assert (
        "investment_region",
        "eq",
        "United States of America",
        "locked",
    ) in _compiled_constraints(question)


@pytest.mark.parametrize(
    "surface",
    [
        "투자 자산군이 원자재인",
        "투자자산군은 원자재인",
        "투자 자산 군이 원자재인",
    ],
)
def test_domestic_commodity_asset_type_is_a_locked_filter(surface: str) -> None:
    question = f"국내 ETN 중 {surface} 상품을 일 거래대금 많은 순으로 세 개 보여 주세요."

    hints = build_lexical_hints(
        question,
        "domestic_etp",
        force_product_family_hint=True,
    )
    assert ("asset_type", "eq", "원자재") in {
        (item["field"], item["operator"], item["value"])
        for item in hints["required_constraints"]
    }
    assert ("asset_type", "eq", "원자재", "locked") in _compiled_constraints(question)


def test_exchange_numeric_filter_and_distribution_ranking_are_all_preserved() -> None:
    question = (
        "나스닥에 상장된 해외 ETF 중 총보수가 0.5% 이하인 상품을 "
        "보수가 낮은 순서로 5개 보여줘."
    )

    hints = build_lexical_hints(question)
    constraints = {
        (item["field"], item["operator"], item["value"])
        for item in hints["required_constraints"]
    }
    assert constraints >= {
        ("product_type", "eq", "ETF"),
        ("exchange_code", "eq", "NAS"),
        ("total_expense_ratio_pct", "lte", 0.5),
    }
    assert hints["required_rankings"] == [
        {"field": "total_expense_ratio_pct", "direction": "asc", "nulls": "last"}
    ]
    assert IntentRouter().route(question, "fresh-exchange").disposition is RouteDisposition.EXECUTE


def test_sellable_distribution_yield_filter_and_repeated_metric_ranking_execute() -> None:
    question = (
        "현재 판매 중인 국내 ETF 가운데 분배수익률이 0보다 큰 상품을 "
        "분배수익률 순으로 5개 보여줘."
    )

    hints = build_lexical_hints(question)
    assert hints["unsupported_spans"] == []
    assert hints["ambiguity_spans"] == []
    assert {(
        item["field"],
        item["operator"],
        item["value"],
    ) for item in hints["required_constraints"]} >= {
        ("sellable", "eq", True),
        ("distribution_yield_pct", "gt", 0),
    }
    assert hints["required_rankings"] == [
        {"field": "distribution_yield_pct", "direction": "desc", "nulls": "last"}
    ]
    assert (
        IntentRouter().route(question, "fresh-distribution").disposition
        is RouteDisposition.EXECUTE
    )


def test_single_fee_antecedent_resolves_referential_fund_ranking() -> None:
    question = (
        "판매 중인 낮은 위험 펀드 중 판매회사 보수가 있는 상품을 "
        "그 보수가 낮은 순으로 5개 찾아줘."
    )

    hints = build_lexical_hints(question)
    assert hints["ambiguity_spans"] == []
    assert {item["field"] for item in hints["required_constraints"]} >= {
        "sellable",
        "risk_level",
    }
    assert hints["required_rankings"] == [
        {"field": "sale_company_fee_pct", "direction": "asc", "nulls": "last"}
    ]
    assert (
        IntentRouter().route(question, "fresh-fee-reference").disposition
        is RouteDisposition.EXECUTE
    )


def test_snapshot_today_return_is_not_misclassified_as_a_forecast() -> None:
    question = (
        "마감 가격이 10달러에서 50달러 사이인 미국 주식 ETF를 "
        "오늘 상승률 높은 순으로 7개 줘."
    )

    hints = build_lexical_hints(question, "overseas_etp", force_product_family_hint=True)
    assert ("close_price", "between", [10, 50]) in [
        (item["field"], item["operator"], item["value"])
        for item in hints["required_constraints"]
    ]
    assert hints["required_rankings"] == [
        {"field": "one_day_return_pct", "direction": "desc", "nulls": "last"}
    ]
    decision = IntentRouter().route(question, "fresh-today-return")
    assert decision.disposition is RouteDisposition.EXECUTE


def test_numeric_range_and_colloquial_fee_ranking_coexist() -> None:
    question = (
        "하루 등락이 0.5% 이상 3% 이하인 해외 ETF를 "
        "수수료 부담이 적은 순으로 5개 추려줘."
    )

    hints = build_lexical_hints(question)
    assert ("one_day_return_pct", "between", [0.5, 3]) in [
        (item["field"], item["operator"], item["value"])
        for item in hints["required_constraints"]
    ]
    assert hints["required_rankings"] == [
        {"field": "total_expense_ratio_pct", "direction": "asc", "nulls": "last"}
    ]
    assert (
        IntentRouter().route(question, "fresh-daily-range").disposition
        is RouteDisposition.EXECUTE
    )


@pytest.mark.parametrize("exchange", ["나스닥", "뉴욕거래소", "뉴욕 증권거래소", "아멕스"])
def test_korean_us_exchange_name_implies_overseas_etp_schema(exchange: str) -> None:
    question = f"{exchange}에 상장된 채권 ETF 중 AUM이 큰 순으로 5개 찾아줘."

    decision = IntentRouter().route(question, f"fresh-korean-exchange-{exchange}")
    assert decision.disposition is RouteDisposition.EXECUTE
    assert [family.value for family in decision.draft.product_families] == ["overseas_etp"]
