"""Fresh final-red-team regressions over the approved four-database snapshot.

The cases were authored without reading prior Phase questions or Gold.  They
exercise the complete deterministic Router -> Compiler -> Oracle -> Verifier
path.  Ordinary unit-test runs skip this external-snapshot suite; the final
validation container mounts the approved databases at ``/data``.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from finance_agent_core.agent.routed_service import RoutedFinanceAgent
from finance_agent_core.contracts.queryplan import LogicalOperator, ProductFamily


@pytest.fixture(scope="module")
def agent() -> RoutedFinanceAgent:
    root = Path(os.environ.get("FINANCE_AGENT_METAMORPHIC_DB_ROOT", "/data"))
    paths = {family: root / f"{family.value}.sqlite3" for family in ProductFamily}
    if not all(path.is_file() for path in paths.values()):
        pytest.skip("approved four-database metamorphic snapshot is not mounted")
    return RoutedFinanceAgent(
        paths,
        allow_internal_disabled_dataset=True,
        capability_execution_overrides=set(ProductFamily),
    )


def _answer(agent: RoutedFinanceAgent, question: str, request_id: str):
    return agent.answer(question, request_id)


def _constraint_map(result):
    assert result.status == "executed"
    assert result.query_plan is not None
    return {
        (item.field, item.operator.value): item.value
        for item in result.query_plan.constraints
    }


def test_two_dates_keep_their_nearest_field_and_direction(agent):
    result = _answer(
        agent,
        "국내채권 가운데 발행일이 2025-01-01 이후이고 만기일은 "
        "2027-12-31 이하인 상품 다섯 개 알려줘.",
        "META-DATE",
    )
    assert _constraint_map(result) == {
        ("issue_date", "gte"): "2025-01-01",
        ("maturity_date", "lte"): "2027-12-31",
    }


def test_natural_or_preserves_both_threshold_branches(agent):
    result = _answer(
        agent,
        "국내채권 중 쿠폰이 최소 3% 또는 지금 살 때 세전 수익률이 "
        "최소 4%인 것을 열 개 찾아줘.",
        "META-OR",
    )
    constraints = _constraint_map(result)
    assert constraints[("coupon_rate_pct", "gte")] == 3
    assert constraints[("buy_yield_pct", "gte")] == 4
    assert result.query_plan.constraint_expression is not None
    assert result.query_plan.constraint_expression.operator is LogicalOperator.ANY
    assert result.query_plan.ranking == []


def test_issuer_exclusion_cannot_disappear(agent):
    result = _answer(
        agent,
        "국내채권 중 발행사가 대한민국이 아닌 AAA 등급 상품 다섯 건 알려줘.",
        "META-EXCLUDE",
    )
    constraints = _constraint_map(result)
    assert constraints[("issuer", "neq")] == "대한민국"
    assert constraints[("credit_rating", "eq")] == "AAA"


def test_exact_bond_detail_keeps_identity_and_venue(agent):
    result = _answer(
        agent,
        "국민주택1종채권 22-10 장외 상품 상세에서 매수수익률, 만기일을 보여줘.",
        "META-BOND-DETAIL",
    )
    constraints = _constraint_map(result)
    assert constraints[("product_id", "eq")] == "KR101501DCA3"
    assert constraints[("bond_market", "eq")] == "장외"
    assert [item.product_id for item in result.products] == ["KR101501DCA3"]


def test_fund_hedge_negation_cannot_reverse(agent):
    result = _answer(
        agent,
        "공모펀드 중 판매 중이고 환헤지는 안 하는 상품 다섯 개를 "
        "AUM 내림차순으로 알려줘.",
        "META-HEDGE",
    )
    constraints = _constraint_map(result)
    assert constraints[("sellable", "eq")] is True
    assert constraints[("currency_hedged", "eq")] is False


def test_manager_label_owns_the_literal_name(agent):
    result = _answer(
        agent,
        "해외 ETF 중 운용사 이름에 Vanguard가 들어간 상품 다섯 개를 "
        "비용이 싼 순서로 알려줘.",
        "META-MANAGER",
    )
    constraints = _constraint_map(result)
    assert constraints[("manager", "contains")] == "Vanguard"
    assert not any(field == "product_name" for field, _ in constraints)


def test_explicit_official_document_relation_never_becomes_broad_search(agent):
    result = _answer(
        agent,
        "6개월 이내 공식 문서에서 우주항공과 연관된 국내 상장 ETF를 찾아줘.",
        "META-EXTERNAL",
    )
    assert result.status != "executed"


def test_multi_measure_aggregate_cannot_drop_the_second_measure(agent):
    result = _answer(
        agent,
        "평균 운용자산 규모와 평균 비용이 얼마인지 해외 ETF 기준으로 계산해줘.",
        "META-AGG-MULTI",
    )
    assert result.status == "executed"
    assert result.query_plan is not None
    assert {
        (item.function.value, item.field)
        for item in result.query_plan.intent_payload.aggregations
    } == {("avg", "aum"), ("avg", "total_expense_ratio_pct")}


def test_normal_trading_condition_cannot_disappear(agent):
    result = _answer(
        agent,
        "국내 ETF 다섯 개를 찾는데 매매 가능, 거래 정상, 위험등급은 "
        "높은위험(2등급) 조건이야.",
        "META-BOOLEAN",
    )
    constraints = _constraint_map(result)
    assert constraints[("sellable", "eq")] is True
    assert constraints[("trading_suspended", "eq")] is False
    assert constraints[("risk_level", "eq")] == "높은위험(2등급)"
    assert result.query_plan.ranking == []


def test_three_named_tickers_remain_a_closed_set(agent):
    result = _answer(
        agent,
        "해외 ETF VOO와 IVV와 SPY 세 종목을 순자산 내림차순으로 정렬해줘.",
        "META-CLOSED-SET",
    )
    constraints = _constraint_map(result)
    assert set(constraints[("product_id", "in")]) == {
        "AMX:VOO",
        "AMX:IVV",
        "AMX:SPY",
    }
    assert result.query_plan.limit == 3
