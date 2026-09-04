from __future__ import annotations

import pytest
from finance_agent_core.agent.compiler import ServerQueryPlanCompiler
from finance_agent_core.agent.router import IntentRouter
from finance_agent_core.contracts.queryplan import ProductFamily
from finance_agent_core.contracts.routing import InteractionIntent, RouteDisposition


def _compile(question: str):
    decision = IntentRouter().route(question, f"final-language-{abs(hash(question))}")
    assert decision.disposition is RouteDisposition.EXECUTE
    return decision, ServerQueryPlanCompiler({}).compile(decision)


@pytest.mark.parametrize(
    ("question", "family", "field", "direction", "limit"),
    [
        (
            "총보수가 0이나 결측이 아닌 국내 상장상품 중 가장 저렴한 5개를 알려줘.",
            ProductFamily.DOMESTIC_ETP,
            "total_expense_ratio_pct",
            "asc",
            5,
        ),
        (
            "해외 ETP의 일간 수익률 상위 5개를 보여줘. 0이나 빈 값은 빼고.",
            ProductFamily.OVERSEAS_ETP,
            "one_day_return_pct",
            "desc",
            5,
        ),
        (
            "잔존일수가 0이 아닌 채권을 만기까지 가장 짧게 남은 순서로 7건 찾아줘.",
            ProductFamily.BOND,
            "remaining_days",
            "asc",
            7,
        ),
        (
            "표면금리 3% 이상이고 회사채를 발행액 큰 순으로 8개 추려줘.",
            ProductFamily.BOND,
            "issue_amount",
            "desc",
            8,
        ),
        (
            "판매회사 보수가 입력된 펀드를 보수가 싼 순서로 5개 보여줘.",
            ProductFamily.FUND,
            "sale_company_fee_pct",
            "asc",
            5,
        ),
        (
            "해외주식 편입비가 확인되는 펀드 가운데 비중이 큰 5개를 알려줘.",
            ProductFamily.FUND,
            "overseas_stock_ratio_pct",
            "desc",
            5,
        ),
    ],
)
def test_explicit_metric_references_compile_without_losing_meaning(
    question: str,
    family: ProductFamily,
    field: str,
    direction: str,
    limit: int,
) -> None:
    _, plan = _compile(question)

    assert plan.product_families == [family]
    assert [(item.field, item.direction.value) for item in plan.ranking] == [
        (field, direction)
    ]
    assert plan.limit == limit


def test_parenthesized_exchange_code_is_a_filter_not_a_product_identity() -> None:
    decision, plan = _compile(
        "NASDAQ 코드(NAS)로 분류된 매매 가능 Equity 해외 ETP에서 "
        "일간 수익률 상위 8개를 보여줘."
    )

    assert decision.draft.product_mentions == []
    constraints = {(item.field, item.operator.value, item.value) for item in plan.constraints}
    assert ("exchange_code", "eq", "NAS") in constraints
    assert ("asset_type", "eq", "Equity") in constraints
    assert ("sellable", "eq", True) in constraints


def test_natural_domestic_bond_etf_constraints_are_all_locked() -> None:
    _, plan = _compile(
        "판매 가능한 국내 채권 ETF 중 연금 가능, 추적오차 0.50% 이하인 상품을 "
        "1년 성과순으로 8개 보여줘."
    )

    constraints = {(item.field, item.operator.value, item.value) for item in plan.constraints}
    assert {
        ("product_type", "eq", "ETF"),
        ("investment_region", "eq", "국내"),
        ("asset_type", "eq", "채권"),
        ("sellable", "eq", True),
        ("pension_eligible", "eq", True),
        ("tracking_error_pct", "lte", 0.5),
    } <= constraints


def test_global_equity_etf_keeps_exchange_region_and_asset_type_separate() -> None:
    _, plan = _compile(
        "AMX의 판매 가능 Global Equity ETF에서 총보수 0.50% 이하인 종목을 "
        "하루 성과순으로 10개 뽑아줘."
    )

    constraints = {(item.field, item.operator.value, item.value) for item in plan.constraints}
    assert ("exchange_code", "eq", "AMX") in constraints
    assert ("investment_region", "eq", "Global") in constraints
    assert ("asset_type", "eq", "Equity") in constraints
    assert ("total_expense_ratio_pct", "lte", 0.5) in constraints


@pytest.mark.parametrize(
    "question",
    [
        "펀드 투자지역별로 1년 수익률이 적힌 수와 공란인 수를 계산해줘.",
        "해외 ETP 자산군 값이 비어 있는 경우와 채워진 경우의 수를 나눠 보여줘.",
    ],
)
def test_availability_count_language_routes_to_aggregate(question: str) -> None:
    decision = IntentRouter().route(question, f"availability-{abs(hash(question))}")

    assert decision.disposition is RouteDisposition.EXECUTE
    assert decision.draft.intent is InteractionIntent.AGGREGATE


def test_named_fund_ratio_comparison_is_not_mistaken_for_a_prohibited_request() -> None:
    decision = IntentRouter().route(
        "우리다같이적격TDF2035(ID KR5134490065)와 "
        "우리다같이적격TDF2030(ID KR5134490059)의 "
        "국내주식·해외주식 편입비와 환헤지 상태를 비교해줘.",
        "named-fund-comparison",
    )

    assert decision.disposition is RouteDisposition.EXECUTE
    assert decision.draft.intent is InteractionIntent.COMPARE


def _constraints(plan):
    return {
        (
            item.field,
            item.operator.value,
            tuple(item.value) if isinstance(item.value, list) else item.value,
        )
        for item in plan.constraints
    }


def test_duration_year_range_is_scaled_once_without_raw_day_duplicate() -> None:
    _, plan = _compile(
        "잔존기간이 1년 이상 3년 이하인 국공채 중 적용수익률 상위 6개를 찾아주세요."
    )

    remaining = [item for item in plan.constraints if item.field == "remaining_days"]
    assert [(item.operator.value, item.value) for item in remaining] == [
        ("between", [365, 1095])
    ]


def test_natural_bond_issuer_and_late_maturity_are_preserved() -> None:
    _, plan = _compile(
        "발행자가 대한민국으로 잡힌 국공채에서 만기가 늦게 오는 순으로 5개만 골라줘요."
    )

    assert ("issuer", "contains", "대한민국") in _constraints(plan)
    assert [(item.field, item.direction.value) for item in plan.ranking] == [
        ("maturity_date", "desc")
    ]


def test_late_maturity_with_result_count_does_not_require_sort_suffix() -> None:
    _, plan = _compile(
        "발행자가 대한민국으로 잡힌 원화 채권에서 쿠폰이 2.5% 이상인 것 중 "
        "만기가 늦게 오는 3개를 찾아줘."
    )

    assert ("issuer", "contains", "대한민국") in _constraints(plan)
    assert ("coupon_rate_pct", "gte", 2.5) in _constraints(plan)
    assert ("trading_currency", "eq", "KRW") in _constraints(plan)
    assert [(item.field, item.direction.value) for item in plan.ranking] == [
        ("maturity_date", "desc")
    ]


def test_coordinated_public_and_sellable_fund_scope_is_not_reduced() -> None:
    _, plan = _compile(
        "공모이고 판매도 가능한 펀드만 모아서 1년 수익률 상위 8개를 산출해 주세요."
    )

    assert ("public_offering", "eq", True) in _constraints(plan)
    assert ("sellable", "eq", True) in _constraints(plan)


def test_bond_tilde_ranges_bind_to_their_explicit_fields() -> None:
    _, plan = _compile(
        "장내 회사채 중 신용등급이 AA+, AA0, AA-이고 표면금리 3~6%, "
        "잔존 365~1,460일인 종목을 적용수익률 높은 순으로 10개 주세요."
    )

    assert ("coupon_rate_pct", "between", (3, 6)) in _constraints(plan)
    assert ("remaining_days", "between", (365, 1460)) in _constraints(plan)


def test_bond_year_span_and_standalone_rating_are_preserved() -> None:
    _, plan = _compile(
        "특수채·공사채·AAA 조건을 모두 만족하고 만기가 2027~2032년이며 "
        "듀레이션 5년 이하인 채권을 발행액순으로 8개 추려줘요."
    )

    assert ("credit_rating", "eq", "AAA") in _constraints(plan)
    assert (
        "maturity_date",
        "between",
        ("2027-01-01", "2032-12-31"),
    ) in _constraints(plan)


def test_plain_offline_fund_sales_channel_is_a_locked_filter() -> None:
    _, plan = _compile(
        "공모·판매 가능·오프라인 판매·해외 주식형·환헤지 펀드 중 "
        "AUM이 있고 판매보수 1.50% 이하이며 1년 수익률이 양수인 상위 8개를 주세요."
    )

    assert ("sales_channel", "contains", "오프라인") in _constraints(plan)
