from __future__ import annotations

from finance_agent_core.agent.compiler import _remove_exact_detail_filter_constraints
from finance_agent_core.agent.compiler import ServerQueryPlanCompiler
from finance_agent_core.agent.linker import canonicalize_query_plan_payload
from finance_agent_core.agent.router import IntentRouter


def _constraints(question: str, family: str) -> list[dict[str, object]]:
    payload = canonicalize_query_plan_payload(
        question,
        {"question_id": "final-structural", "product_families": [family]},
        force_product_family_hint=True,
    )
    return payload["constraints"]


def test_korean_calendar_dates_remain_owned_by_each_bond_field() -> None:
    constraints = _constraints(
        "2025년 1월 1일 이후 발행되고 2027년 12월 31일까지 만기인 "
        "국내채권을 표면금리 높은 순으로 보여줘.",
        "bond",
    )

    assert {
        (item["field"], item["operator"], item["value"])
        for item in constraints
        if item["field"] in {"issue_date", "maturity_date"}
    } == {
        ("issue_date", "gte", "2025-01-01"),
        ("maturity_date", "lte", "2027-12-31"),
    }


def test_fund_sale_negation_does_not_cross_into_hedge_clause() -> None:
    constraints = _constraints(
        "현재 판매 가능하면서 환헤지하지 않은 공모펀드를 "
        "1년 수익률 높은 순으로 보여줘.",
        "fund",
    )

    values = {(item["field"], item["value"]) for item in constraints}
    assert ("sellable", True) in values
    assert ("sellable", False) not in values
    assert ("currency_hedged", False) in values


def test_core_etf_is_a_typed_filter_not_an_unresolved_adjective() -> None:
    constraints = _constraints(
        "코어 ETF 중 순자산이 큰 상품 5개를 보여줘.",
        "domestic_etp",
    )

    assert any(
        item["field"] == "core_etf"
        and item["operator"] == "eq"
        and item["value"] is True
        for item in constraints
    )


def test_exact_bond_detail_keeps_explicit_market_disambiguator() -> None:
    payload: dict[str, object] = {
        "constraints": [
            {"field": "product_id", "operator": "eq", "value": "123456"},
            {"field": "bond_market", "operator": "eq", "value": "장내"},
            {"field": "coupon_rate_pct", "operator": "gte", "value": 3},
        ]
    }

    _remove_exact_detail_filter_constraints(payload)

    assert payload["constraints"] == [
        {"field": "product_id", "operator": "eq", "value": "123456"},
        {"field": "bond_market", "operator": "eq", "value": "장내"},
    ]


def test_generic_rate_anaphora_reuses_one_explicit_coupon_metric() -> None:
    question = "표면이율이 3% 이상인 채권 중 금리가 높은 5개를 보여줘."
    decision = IntentRouter().route(question, "final-coupon-anaphora")
    plan = ServerQueryPlanCompiler({}).compile(decision)

    assert [(item.field, item.direction.value) for item in plan.ranking] == [
        ("coupon_rate_pct", "desc")
    ]
    assert any(
        item.field == "coupon_rate_pct"
        and item.operator.value == "gte"
        and item.value == 3
        for item in plan.constraints
    )


def test_prefixed_minimum_or_preserves_both_filter_branches() -> None:
    question = (
        "국내채권 중 쿠폰이 최소 3% 또는 지금 살 때 세전 수익률이 "
        "최소 4%인 것을 열 개 보여줘."
    )
    decision = IntentRouter().route(question, "final-prefixed-or")
    plan = ServerQueryPlanCompiler({}).compile(decision)

    assert {(item.field, item.operator.value, item.value) for item in plan.constraints} >= {
        ("coupon_rate_pct", "gte", 3),
        ("buy_yield_pct", "gte", 4),
    }
    assert plan.constraint_expression is not None
    assert plan.constraint_expression.operator.value == "any"
    assert len(plan.constraint_expression.constraint_keys) == 2
    assert plan.ranking == []


def test_issuer_exclusion_does_not_drop_adjacent_credit_filter() -> None:
    constraints = _constraints(
        "국내채권 중 발행사가 대한민국이 아닌 AAA 등급 상품 다섯 건",
        "bond",
    )

    assert {(item["field"], item["operator"], item["value"]) for item in constraints} >= {
        ("issuer", "neq", "대한민국"),
        ("credit_rating", "eq", "AAA"),
    }
