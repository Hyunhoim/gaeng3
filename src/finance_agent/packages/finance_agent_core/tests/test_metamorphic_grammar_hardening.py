from __future__ import annotations

import pytest

from finance_agent_core.agent.compiler import ServerQueryPlanCompiler
from finance_agent_core.agent.linker import build_lexical_hints
from finance_agent_core.agent.router import IntentRouter
from finance_agent_core.agent.semantic_aliases import typed_alias_matches
from finance_agent_core.contracts.queryplan import AggregateFunction, ProductFamily
from finance_agent_core.contracts.routing import InteractionIntent, RouteDisposition


def _compile(question: str):
    decision = IntentRouter().route(question, f"metamorphic-{abs(hash(question))}")
    assert decision.disposition is RouteDisposition.EXECUTE
    return decision, ServerQueryPlanCompiler({}).compile(decision)


@pytest.mark.parametrize(
    ("question", "family", "field", "direction", "limit"),
    [
        ("순자산 하위 4개 국내 ETF 상품을 알려 주세요.", "domestic_etp", "aum", "asc", 4),
        (
            "총보수율 하위 4개 해외 ETF 상품을 알려 주세요.",
            "overseas_etp",
            "total_expense_ratio_pct",
            "asc",
            4,
        ),
        ("순자산 상위 5개 공모펀드 상품을 보여 주세요.", "fund", "aum", "desc", 5),
        ("표면금리 상위 4개 국내채권 상품을 알려 주세요.", "bond", "coupon_rate_pct", "desc", 4),
        ("잔존일수 하위 3개 원화채권 상품을 찾아 주세요.", "bond", "remaining_days", "asc", 3),
        (
            "일거래대금 상위 5개 국내 ETF 상품을 조회해 주세요.",
            "domestic_etp",
            "daily_trading_value",
            "desc",
            5,
        ),
    ],
)
def test_postposed_field_ordinal_is_a_typed_search(
    question: str,
    family: str,
    field: str,
    direction: str,
    limit: int,
) -> None:
    decision, plan = _compile(question)

    assert decision.draft.intent is InteractionIntent.SEARCH
    assert plan.product_families == [ProductFamily(family)]
    assert [(item.field, item.direction.value) for item in plan.ranking] == [
        (field, direction)
    ]
    assert plan.limit == limit


@pytest.mark.parametrize(
    ("question", "family", "product_type"),
    [
        ("국내 ETF 상품 수를 세어 주세요.", "domestic_etp", "ETF"),
        ("국내 ETN 상품 수를 세어 주세요.", "domestic_etp", "ETN"),
        ("제공 데이터의 국내 상장지수증권은 몇 개인가요?", "domestic_etp", "ETN"),
        ("해외 ETF 상품 수를 세어 주세요.", "overseas_etp", "ETF"),
        ("해외 상장지수펀드 개수를 집계해 주세요.", "overseas_etp", "ETF"),
        ("공모펀드 상품 수를 세어 주세요.", "fund", None),
        ("국내채권 종목 수를 세어 주세요.", "bond", None),
    ],
)
def test_short_count_actions_preserve_family_and_product_type(
    question: str,
    family: str,
    product_type: str | None,
) -> None:
    decision, plan = _compile(question)

    assert decision.draft.intent is InteractionIntent.AGGREGATE
    assert plan.product_families == [ProductFamily(family)]
    assert [(item.function, item.field) for item in plan.intent_payload.aggregations] == [
        (AggregateFunction.COUNT, "product_id")
    ]
    product_type_constraints = [
        item.value for item in plan.constraints if item.field == "product_type"
    ]
    assert product_type_constraints == ([] if product_type is None else [product_type])


@pytest.mark.parametrize(
    ("question", "field", "value"),
    [
        (
            "원자재 자산군 국내 ETF를 일거래대금 높은 순으로 3개 보여 주세요.",
            "asset_type",
            "원자재",
        ),
        (
            "국내 ETF 중 투자자산이 원자재인 상품 거래대금 상위 3개를 알려 주세요.",
            "asset_type",
            "원자재",
        ),
        (
            "자산유형이 Equity인 해외 ETF 중 총보수 하위 다섯 종목을 알려 주세요.",
            "asset_type",
            "Equity",
        ),
        (
            "해외 ETF에서 United States of America 지역만 AUM 내림차순 4개 조회해 주세요.",
            "investment_region",
            "United States of America",
        ),
        (
            "투자지역이 국내인 공모펀드를 1년 수익률 높은 순으로 5개 보여 주세요.",
            "investment_region",
            "국내",
        ),
        (
            "공모펀드 중 판매여부가 가능한 것만 순자산 내림차순 4개 조회해 주세요.",
            "sellable",
            True,
        ),
    ],
)
def test_explicit_filter_grammar_never_drops_or_retypes_a_condition(
    question: str,
    field: str,
    value: object,
) -> None:
    decision, plan = _compile(question)

    assert decision.draft.intent is InteractionIntent.SEARCH
    assert [(item.field, item.value) for item in plan.constraints if item.field == field] == [
        (field, value)
    ]
    if field == "investment_region" and value == "국내":
        assert all(item.field != "fund_geography_scope" for item in plan.constraints)


@pytest.mark.parametrize(
    ("question", "upper"),
    [
        ("국내채권에서 남은 기간 365일 이하만 매수수익률 내림차순 5개 조회해 주세요.", 365),
        ("남은 기간이 6개월 이내인 국내채권을 표면금리 상위 4개 보여 주세요.", 180),
        ("만기까지 잔여 기간 30일 이하인 국내채권을 잔존일수 하위 3개 알려 주세요.", 30),
    ],
)
def test_remaining_period_bound_is_locked_before_ranking(question: str, upper: int) -> None:
    _, plan = _compile(question)

    constraint = next(item for item in plan.constraints if item.field == "remaining_days")
    assert constraint.operator.value == "between"
    assert constraint.value == [0, upper]


@pytest.mark.parametrize(
    ("question", "field", "direction"),
    [
        (
            "석 달 수익률로 국내 ETF·공모펀드를 한 목록에 놓고 상위 5개를 알려 주세요.",
            "three_month_return_pct",
            "desc",
        ),
        (
            "국내 ETF 및 공모펀드 전체에서 분기 성과가 가장 높은 다섯 상품을 통합해 주세요.",
            "three_month_return_pct",
            "desc",
        ),
        (
            "국내 ETF 및 공모펀드 전체에서 반년 성과가 낮은 네 상품을 통합해 주세요.",
            "six_month_return_pct",
            "asc",
        ),
        (
            "6개월 수익률로 국내 ETF·공모펀드를 한 목록에 놓고 하위 4개를 알려 주세요.",
            "six_month_return_pct",
            "asc",
        ),
    ],
)
def test_global_period_anchor_has_one_family_independent_typed_meaning(
    question: str,
    field: str,
    direction: str,
) -> None:
    for family in (ProductFamily.DOMESTIC_ETP, ProductFamily.FUND):
        matches = [
            item
            for item in typed_alias_matches(question, family)
            if item.operation == "rank"
        ]
        assert {(item.field_id, item.direction.value) for item in matches} == {
            (field, direction)
        }


@pytest.mark.parametrize(
    ("question", "field", "direction", "limit"),
    [
        ("하루 거래대금이 활발한 국내 ETF 4개를 찾아 주세요.", "daily_trading_value", "desc", 4),
        ("자금 거래가 큰 국내 ETF부터 네 상품을 보여 주세요.", "daily_trading_value", "desc", 4),
        ("하루 거래 주식 수가 많은 해외 ETF 3개를 알려 주세요.", "daily_volume", "desc", 3),
        ("거래량 기준으로 가장 활발한 해외 ETF 세 종목을 보여 주세요.", "daily_volume", "desc", 3),
        ("보유 관리비가 싼 해외 ETF 3개를 알려 주세요.", "total_expense_ratio_pct", "asc", 3),
        (
            "총 운용보수가 낮은 해외 ETF부터 세 종목 보여 주세요.",
            "total_expense_ratio_pct",
            "asc",
            3,
        ),
        (
            "지금 매수가격을 반영한 세전 보상이 큰 채권 세 개를 알려 주세요.",
            "buy_yield_pct",
            "desc",
            3,
        ),
        (
            "현재 가격으로 샀을 때 세전 수익률 상위 세 채권을 보여 주세요.",
            "buy_yield_pct",
            "desc",
            3,
        ),
        (
            "매수 후 세금 공제 기준 수익률이 높은 채권 세 종목을 보여 주세요.",
            "after_tax_yield_pct",
            "desc",
            3,
        ),
        (
            "액면에 정기적으로 붙는 이자가 낮은 채권 세 개를 알려 주세요.",
            "coupon_rate_pct",
            "asc",
            3,
        ),
        ("표면에 적힌 이율이 낮은 원화채권 3개를 찾아 주세요.", "coupon_rate_pct", "asc", 3),
        ("쿠폰금리 하위 세 국내채권을 보여 주세요.", "coupon_rate_pct", "asc", 3),
        ("최근 두 분기 수익률 하위 4개 국내 ETF를 알려 주세요.", "six_month_return_pct", "asc", 4),
        ("6개월 동안 덜 오른 국내 ETF부터 네 상품 보여 주세요.", "six_month_return_pct", "asc", 4),
    ],
)
def test_high_frequency_colloquial_metrics_compile_without_dense_guessing(
    question: str,
    field: str,
    direction: str,
    limit: int,
) -> None:
    _, plan = _compile(question)

    assert [(item.field, item.direction.value) for item in plan.ranking] == [
        (field, direction)
    ]
    assert plan.limit == limit


@pytest.mark.parametrize(
    ("question", "family", "filter_field", "filter_value", "rank_field"),
    [
        (
            "국내 ETF 중 총보수 0.3퍼센트를 넘지 않는 상품 하위 5개를 알려 주세요.",
            "domestic_etp",
            "total_expense_ratio_pct",
            0.3,
            "total_expense_ratio_pct",
        ),
        (
            "운용보수율 0.3% 이하만 남겨 국내 ETF 5개를 낮은 순으로 조회해 주세요.",
            "domestic_etp",
            "total_expense_ratio_pct",
            0.3,
            "total_expense_ratio_pct",
        ),
        (
            "원자재에 투자하는 국내 ETF만 남겨 하루 거래대금 순 3개를 조회해 주세요.",
            "domestic_etp",
            "asset_type",
            "원자재",
            "daily_trading_value",
        ),
        (
            "해외 주식 ETF만 골라 총보수 오름차순 5개를 조회해 주세요.",
            "overseas_etp",
            "asset_type",
            "Equity",
            "total_expense_ratio_pct",
        ),
    ],
)
def test_natural_filter_and_postposed_ranking_preserve_both_slots(
    question: str,
    family: str,
    filter_field: str,
    filter_value: object,
    rank_field: str,
) -> None:
    _, plan = _compile(question)

    assert plan.product_families == [ProductFamily(family)]
    assert any(
        item.field == filter_field and item.value == filter_value
        for item in plan.constraints
    )
    assert [item.field for item in plan.ranking] == [rank_field]


@pytest.mark.parametrize(
    ("question", "family", "function", "field", "group_by"),
    [
        ("제공 데이터의 해외 ETF는 모두 몇 개인가요?", "overseas_etp", "count", "product_id", []),
        (
            "국내 ETF 전체에서 유효한 1년 성과 평균을 알려 주세요.",
            "domestic_etp",
            "avg",
            "one_year_return_pct",
            [],
        ),
        ("해외 ETF의 유효한 AUM 중간값을 알려 주세요.", "overseas_etp", "median", "aum", []),
        (
            "공모형 펀드 분기 수익률의 산술평균은 얼마인가요?",
            "fund",
            "avg",
            "three_month_return_pct",
            [],
        ),
        (
            "공모펀드의 유효한 석 달 성과 평균을 알려 주세요.",
            "fund",
            "avg",
            "three_month_return_pct",
            [],
        ),
        (
            "국내 상장지수상품 수를 자산유형마다 묶어 알려 주세요.",
            "domestic_etp",
            "count",
            "product_id",
            ["asset_type"],
        ),
    ],
)
def test_aggregate_paraphrases_have_one_typed_measure(
    question: str,
    family: str,
    function: str,
    field: str,
    group_by: list[str],
) -> None:
    _, plan = _compile(question)

    assert plan.product_families == [ProductFamily(family)]
    assert [(item.function.value, item.field) for item in plan.intent_payload.aggregations] == [
        (function, field)
    ]
    assert plan.intent_payload.group_by == group_by
    assert all(item.field != "manager" or item.value for item in plan.constraints)


@pytest.mark.parametrize(
    "question",
    [
        "식별자 AAA인 해외 ETF 상품을 정확히 조회해 주세요.",
        "AAA 해외 ETF의 제공 데이터 필드를 확인해 주세요.",
    ],
)
def test_short_overseas_ticker_is_detail_only_in_explicit_context(question: str) -> None:
    decision = IntentRouter().route(question, f"metamorphic-{abs(hash(question))}")

    assert decision.disposition is RouteDisposition.EXECUTE
    assert decision.draft.intent is InteractionIntent.DETAIL
    assert decision.draft.product_families == [ProductFamily.OVERSEAS_ETP]
    assert decision.draft.product_mentions == ["AAA"]


def _constraint(plan, field: str):
    matches = [item for item in plan.constraints if item.field == field]
    assert len(matches) == 1
    return matches[0]


@pytest.mark.parametrize(
    ("question", "field", "operator", "value"),
    [
        (
            "2020년부터 2022년까지 상장된 해외 ETF 중 AUM 높은 순 4개를 알려 주세요.",
            "listing_date",
            "between",
            ["2020-01-01", "2022-12-31"],
        ),
        (
            "설정일이 2021년 이후인 해외 ETF를 총보수 낮은 순 3개 보여 주세요.",
            "listing_date",
            "gte",
            "2021-01-01",
        ),
        (
            "2019년부터 2021년까지 발행된 채권을 표면금리 높은 순 5개 조회해 주세요.",
            "issue_date",
            "between",
            ["2019-01-01", "2021-12-31"],
        ),
        (
            "2020년에 발행된 국내채권 중 매수수익률 상위 3개를 알려 주세요.",
            "issue_date",
            "between",
            ["2020-01-01", "2020-12-31"],
        ),
        (
            "동적 지표 기준일이 2026-08-22 이후인 국내 ETF를 AUM 순 4개 보여 주세요.",
            "dynamic_as_of",
            "gte",
            "2026-08-22",
        ),
        (
            "AUM 업데이트일 2026-08-20부터 2026-08-24까지인 해외 ETF 3개를 알려 주세요.",
            "dynamic_as_of",
            "between",
            ["2026-08-20", "2026-08-24"],
        ),
    ],
)
def test_date_grammar_preserves_full_field_and_calendar_boundaries(
    question: str,
    field: str,
    operator: str,
    value: object,
) -> None:
    _, plan = _compile(question)

    constraint = _constraint(plan, field)
    assert constraint.operator.value == operator
    assert constraint.value == value


@pytest.mark.parametrize(
    ("question", "expected"),
    [
        ("인버스·숏 여부가 Y인 해외 ETF 중 AUM 상위 4개를 알려 주세요.", True),
        ("인버스 또는 숏인 해외 ETF를 거래량 높은 순 3개 보여 주세요.", True),
        ("인버스·숏 여부가 N인 해외 ETF 중 총보수 하위 5개를 조회해 주세요.", False),
    ],
)
def test_inverse_short_explicit_polarity_is_a_boolean_constraint(
    question: str,
    expected: bool,
) -> None:
    _, plan = _compile(question)

    constraint = _constraint(plan, "inverse_short")
    assert constraint.operator.value == "eq"
    assert constraint.value is expected


@pytest.mark.parametrize(
    ("question", "expected"),
    [
        (
            "추적오차율 1% 이하이고 괴리율 0.5% 이하인 국내 ETF를 AUM 상위 5개 보여 주세요.",
            {"tracking_error_pct": ("lte", 1), "discrepancy_pct": ("lte", 0.5)},
        ),
        (
            "추적 오차 2퍼센트 이하, NAV 괴리율 1퍼센트 미만인 국내 ETF 4개를 알려 주세요.",
            {"tracking_error_pct": ("lte", 2), "discrepancy_pct": ("lt", 1)},
        ),
        (
            "20거래일 연환산 변동성 15% 이하인 국내 ETF를 추적오차 낮은 순 3개 보여 주세요.",
            {"twenty_day_volatility_pct": ("lte", 15)},
        ),
        (
            "최근 한 달 변동성이 10퍼센트 미만인 국내 ETF를 괴리율 하위 4개 알려 주세요.",
            {"twenty_day_volatility_pct": ("lt", 10)},
        ),
        (
            "평가단가 9,000원 이상 11,000원 이하인 채권을 듀레이션 짧은 순 5개 보여 주세요.",
            {"evaluation_price": ("between", [9000, 11000])},
        ),
        (
            "클린 프라이스 10000원 미만인 채권을 민평금리 높은 순 3개 알려 주세요.",
            {"evaluation_price": ("lt", 10000)},
        ),
        (
            "민평수익률이 음수인 국내채권을 평가단가 낮은 순 4개 보여 주세요.",
            {"applied_yield_pct": ("lt", 0)},
        ),
        (
            "민평금리가 플러스인 채권을 이자부단가 높은 순 3개 알려 주세요.",
            {"applied_yield_pct": ("gt", 0)},
        ),
        (
            "월평균 거래대금 10억원 이상인 국내 ETF를 YTD 수익률 상위 5개 보여 주세요.",
            {"one_month_average_trading_value": ("gte", 1_000_000_000)},
        ),
        (
            "최근 1개월 평균 거래대금이 5억원을 넘는 국내 ETF 4개를 알려 주세요.",
            {"one_month_average_trading_value": ("gt", 500_000_000)},
        ),
    ],
)
def test_numeric_and_polarity_grammar_keeps_every_explicit_hard_filter(
    question: str,
    expected: dict[str, tuple[str, object]],
) -> None:
    _, plan = _compile(question)

    for field, (operator, value) in expected.items():
        constraint = _constraint(plan, field)
        assert constraint.operator.value == operator
        assert constraint.value == value


@pytest.mark.parametrize(
    ("question", "field", "direction", "limit"),
    [
        (
            "최근 한 달 평균 거래대금이 큰 국내 ETF 네 종목을 보여 주세요.",
            "one_month_average_trading_value",
            "desc",
            4,
        ),
        (
            "월평균 거래대금 낮은 순 국내 ETF 3개를 알려 주세요.",
            "one_month_average_trading_value",
            "asc",
            3,
        ),
        ("올해 들어 성과가 높은 국내 ETF 5개를 보여 주세요.", "ytd_return_pct", "desc", 5),
        ("연초대비 수익률 하위 국내 ETF 4개를 알려 주세요.", "ytd_return_pct", "asc", 4),
        (
            "경과이자가 포함된 가격이 낮은 채권 세 종목을 보여 주세요.",
            "dirty_price",
            "asc",
            3,
        ),
        ("이자부 가격 상위 국내채권 4개를 알려 주세요.", "dirty_price", "desc", 4),
        ("금리 변화에 더 민감한 채권 5개를 보여 주세요.", "duration_years", "desc", 5),
        ("수정 듀레이션 짧은 순 채권 3개를 알려 주세요.", "duration_years", "asc", 3),
    ],
)
def test_metric_paraphrases_resolve_to_registry_owned_rankings(
    question: str,
    field: str,
    direction: str,
    limit: int,
) -> None:
    _, plan = _compile(question)

    assert [(item.field, item.direction.value) for item in plan.ranking] == [
        (field, direction)
    ]
    assert plan.limit == limit


@pytest.mark.parametrize(
    ("question", "expected"),
    [
        (
            "판매 가능하고 거래정지가 아닌 해외 ETF 중 총보수 0.5% 이하 상품 5개를 알려 주세요.",
            {
                "sellable": True,
                "trading_suspended": False,
                "total_expense_ratio_pct": 0.5,
            },
        ),
        (
            "판매 불가이면서 거래 중지된 해외 ETF를 AUM 높은 순 3개 보여 주세요.",
            {"sellable": False, "trading_suspended": True},
        ),
        (
            "미래에셋증권에서 판매하는 배당형 공모펀드 중 보전율 50% 이상 상품을 알려 주세요.",
            {
                "company_sellable": True,
                "interest_dividend_type": "배당",
                "preservation_ratio_pct": 50,
            },
        ),
        (
            "당사 판매 상품 중 이자형이고 수수료후취인 공모펀드를 순자산 상위 4개 보여 주세요.",
            {"company_sellable": True, "interest_dividend_type": "이자", "fee_type": "수수료후취"},
        ),
    ],
)
def test_boolean_and_enum_paraphrases_remain_explicit_constraints(
    question: str,
    expected: dict[str, object],
) -> None:
    _, plan = _compile(question)

    for field, value in expected.items():
        assert _constraint(plan, field).value == value


@pytest.mark.parametrize(
    ("question", "family", "operator", "value"),
    [
        (
            "상품명에 반도체가 포함된 국내 ETF를 거래대금 높은 순 4개 보여 주세요.",
            "domestic_etp",
            "contains",
            "반도체",
        ),
        (
            "이름에 성장이라는 문구가 들어간 공모펀드를 순자산 상위 3개 알려 주세요.",
            "fund",
            "contains",
            "성장",
        ),
        (
            "상품명이 '샘플 성장 펀드'인 공모펀드를 정확히 조회해 주세요.",
            "fund",
            "eq",
            "샘플 성장 펀드",
        ),
        (
            "정식 상품명은 테스트 ETF인 국내 ETF의 정보를 찾아 주세요.",
            "domestic_etp",
            "eq",
            "테스트 ETF",
        ),
    ],
)
def test_product_name_filter_supports_contains_and_explicit_exact_match(
    question: str,
    family: str,
    operator: str,
    value: str,
) -> None:
    if operator == "eq":
        # Exact product names are resolved to an authoritative product_id by
        # the Compiler when a DB is present.  At the grammar boundary, verify
        # the name is nevertheless represented as an exact locked filter.
        hints = build_lexical_hints(
            question,
            family,
            force_product_family_hint=True,
        )
        matching = [
            item
            for item in hints["required_constraints"]
            if item["field"] == "product_name"
        ]
        assert matching == [{"field": "product_name", "operator": "eq", "value": value}]
        return

    _, plan = _compile(question)

    assert plan.product_families == [ProductFamily(family)]
    constraint = _constraint(plan, "product_name")
    assert constraint.operator.value == operator
    assert constraint.value == value


@pytest.mark.parametrize(
    ("question", "filter_field", "operator", "value", "rank_field", "direction"),
    [
        (
            "3개월 수익률 5% 이상인 공모펀드를 순자산 낮은 순으로 4개 보여 주세요.",
            "three_month_return_pct",
            "gte",
            5,
            "aum",
            "asc",
        ),
        (
            "3개월 수익률 2% 이상인 국내 ETF를 20거래일 연환산 변동성 낮은 순 5개 알려 주세요.",
            "three_month_return_pct",
            "gte",
            2,
            "twenty_day_volatility_pct",
            "asc",
        ),
        (
            "1년 수익률 5% 이상인 공모펀드를 판매회사보수 높은 순 3개 조회해 주세요.",
            "one_year_return_pct",
            "gte",
            5,
            "sale_company_fee_pct",
            "desc",
        ),
    ],
)
def test_later_explicit_rank_owns_direction_after_an_earlier_numeric_filter(
    question: str,
    filter_field: str,
    operator: str,
    value: object,
    rank_field: str,
    direction: str,
) -> None:
    _, plan = _compile(question)

    constraint = _constraint(plan, filter_field)
    assert constraint.operator.value == operator
    assert constraint.value == value
    assert [(item.field, item.direction.value) for item in plan.ranking] == [
        (rank_field, direction)
    ]


@pytest.mark.parametrize(
    ("question", "family", "field", "direction", "limit"),
    [
        (
            "달러로 거래되는 해외 ETF를 이름 역순으로 네 종목 조회해 주세요.",
            "overseas_etp",
            "product_name",
            "desc",
            4,
        ),
        (
            "미래에셋에서 판매 가능한 공모펀드를 펀드명 오름차순으로 다섯 개 알려 주세요.",
            "fund",
            "product_name",
            "asc",
            5,
        ),
        (
            "국내 범위의 주식형 공모펀드를 이름이 앞선 순서로 네 개 조회해 주세요.",
            "fund",
            "product_name",
            "asc",
            4,
        ),
    ],
)
def test_explicit_name_order_is_not_overridden_by_generic_adjectives(
    question: str,
    family: str,
    field: str,
    direction: str,
    limit: int,
) -> None:
    _, plan = _compile(question)

    assert plan.product_families == [ProductFamily(family)]
    assert [(item.field, item.direction.value) for item in plan.ranking] == [
        (field, direction)
    ]
    assert plan.limit == limit


@pytest.mark.parametrize(
    ("question", "field", "direction"),
    [
        (
            "보유 비용이 덜 드는 국내 ETF 세 종목을 비용이 싼 것부터 알려 주세요.",
            "total_expense_ratio_pct",
            "asc",
        ),
        (
            "연초 이후 성적이 우수한 국내 ETF 상위 다섯 개가 궁금합니다.",
            "ytd_return_pct",
            "desc",
        ),
        (
            "최근 한 달 흔들림이 작은 국내 ETF를 네 개만 추려 주세요.",
            "twenty_day_volatility_pct",
            "asc",
        ),
        (
            "처음 발행한 총액이 큰 국내채권을 다섯 개만 내림차순으로 알려 주세요.",
            "total_issue_amount",
            "desc",
        ),
        (
            "5년 누적 성과가 높은 공모펀드를 상위 다섯 개 골라 주세요.",
            "five_year_return_pct",
            "desc",
        ),
    ],
)
def test_generalized_metric_paraphrases_compile_to_one_safe_field(
    question: str,
    field: str,
    direction: str,
) -> None:
    _, plan = _compile(question)

    assert [(item.field, item.direction.value) for item in plan.ranking] == [
        (field, direction)
    ]


def test_exact_credit_rating_with_adverb_remains_a_locked_filter() -> None:
    _, plan = _compile(
        "신용등급이 정확히 AAA인 국내채권을 이름순으로 네 개 보여 주세요."
    )

    constraint = _constraint(plan, "credit_rating")
    assert constraint.operator.value == "eq"
    assert constraint.value == "AAA"
    assert [(item.field, item.direction.value) for item in plan.ranking] == [
        ("product_name", "asc")
    ]


@pytest.mark.parametrize(
    ("question", "region", "asset_type"),
    [
        (
            "중국 지역에 투자하는 해외 ETF를 총보수 낮은 순으로 세 개 보여 주세요.",
            "China",
            None,
        ),
        (
            "미국 주식 자산군의 해외 ETF를 AUM 큰 순서로 네 개 정리해 주세요.",
            "United States of America",
            "Equity",
        ),
    ],
)
def test_korean_overseas_scope_is_canonicalized_without_fake_manager(
    question: str,
    region: str,
    asset_type: str | None,
) -> None:
    _, plan = _compile(question)

    assert _constraint(plan, "investment_region").value == region
    if asset_type is not None:
        assert _constraint(plan, "asset_type").value == asset_type
    assert all(item.field != "manager" for item in plan.constraints)


@pytest.mark.parametrize(
    ("question", "exchange_code"),
    [
            (
                "나스닥 상장 해외 ETP 중 일간 수익률이 있는 상품을 "
                "하루 성과 높은 순 5개 알려 주세요.",
                "NAS",
            ),
        ("뉴욕증권거래소 상장 해외 ETF를 AUM 순으로 3개 보여 주세요.", "NYS"),
        ("아멕스에 상장된 해외 ETP를 거래량 상위 4개 알려 주세요.", "AMX"),
    ],
)
def test_korean_exchange_names_are_locked_as_overseas_filters(
    question: str,
    exchange_code: str,
) -> None:
    _, plan = _compile(question)

    assert _constraint(plan, "exchange_code").value == exchange_code
