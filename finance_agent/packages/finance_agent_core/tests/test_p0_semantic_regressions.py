import pytest

from finance_agent_core.agent.compiler import ServerQueryPlanCompiler
from finance_agent_core.agent.linker import (
    build_lexical_hints,
    canonicalize_query_plan_payload,
)
from finance_agent_core.agent.router import IntentRouter
from finance_agent_core.contracts import QueryPlan


@pytest.mark.parametrize(
    ("question", "family", "intent", "limit"),
    [
        (
            "원자재형 해외 ETF 중 순자산 규모 상위 5개를 보여줘.",
            "overseas_etp",
            "search",
            5,
        ),
        ("국내 채권형 ETF를 AUM 큰 순서로 다섯 개 찾아줘.", "domestic_etp", "search", None),
        (
            "글로벌 주식형 국내 ETF의 YTD 수익률 상위 3개를 순서대로 보여줘.",
            "domestic_etp",
            "search",
            3,
        ),
        ("해외 ETF의 AUM을 highest-to-lowest로 5개 보여줘.", "overseas_etp", "search", 5),
        ("매수 가능한 국내채권을 duration ascending으로 5개 정렬해줘.", "bond", "search", 5),
        ("공모펀드를 ３개월 return 높은 순으로 ５개 보여줘．", "fund", "search", 5),
        (
            "USD 거래 해외 ETF끼리만 총보수율(%) 낮은 순으로 5개 비교해줘.",
            "overseas_etp",
            "search",
            5,
        ),
    ],
)
def test_p0_ranked_requests_route_to_one_authorized_family(
    question: str,
    family: str,
    intent: str,
    limit: int | None,
) -> None:
    decision = IntentRouter().route(question, f"p0-{abs(hash(question))}")

    assert decision.disposition.value == "execute"
    assert decision.draft.intent.value == intent
    assert [item.value for item in decision.draft.product_families] == [family]
    assert decision.draft.requested_limit == limit


def test_markdown_count_label_is_a_search_limit_not_an_aggregate() -> None:
    question = """## 데이터 요청
- 상품군: 국내 ETF
- 지표: 1개월 수익률(%)
- 방향: 내림차순
- 개수: 3
위 조건 그대로 표를 작성해줘."""

    decision = IntentRouter().route(question, "p0-markdown-limit")
    hints = build_lexical_hints(question)

    assert decision.disposition.value == "execute"
    assert decision.draft.intent.value == "search"
    assert decision.draft.requested_limit == 3
    assert hints["limit"] == 3


@pytest.mark.parametrize(
    "question",
    [
        "전기보다 좋아진 국내 ETF를 보여줘.",
        "2024와 2025 중 더 나은 기간의 국내 ETF를 보여줘.",
        "해외 ETF를 AUM 기준으로 정렬해줘.",
        "미국 외 지역 해외 ETF를 AUM 큰 순으로 5개 보여줘.",
        "매수 불가능한 것은 제외하고 국내채권을 수익률 높은 순으로 5개 보여줘.",
        "회사채가 아닌 매수 가능 국내채권을 만기일 오름차순으로 5개 보여줘.",
        "판매 종료 상품을 제외한 공모펀드를 AUM 큰 순으로 5개 보여줘.",
        "USD 해외 ETF AUM과 KRW 국내 ETF 시가를 큰 순으로 섞어 정렬해줘.",
        "해외 ETF AUM과 국내채권 발행액을 환율 지정 없이 금액 순위로 만들어줘.",
        "ETF 1개월 수익률과 펀드 3개월 수익률을 같은 기준으로 정렬해줘.",
        "상품군: 공모펀드, 지표: AUM, 방향: 내림차순, 제한: 3개",
    ],
)
def test_p0_unresolved_semantics_are_pre_route_clarifications(question: str) -> None:
    decision = IntentRouter().route(question, f"p0-clarify-{abs(hash(question))}")

    assert decision.disposition.value == "clarify"
    assert decision.reason_code == "semantic_coverage_incomplete"
    assert decision.query_plan_intent is None


@pytest.mark.parametrize(
    ("question", "expected_constraints", "ranking"),
    [
        (
            "현재 매수할 수 있는 국내채권은 총 몇 개인지 집계해줘.",
            {("currently_buyable", "eq", True)},
            None,
        ),
        (
            "거래 중지 상품은 빼고 판매 가능한 해외 ETF를 총보수 낮은 순으로 5개 보여줘.",
            {("trading_suspended", "eq", False), ("sellable", "eq", True)},
            ("total_expense_ratio_pct", "asc"),
        ),
        (
            "거래 정지된 것은 제외하고 국내 ETF를 AUM 큰 순으로 5개 보여줘.",
            {("trading_suspended", "eq", False)},
            ("aum", "desc"),
        ),
        (
            "연금 거래 불가 상품을 제외한 국내 ETF를 총보수 낮은 순으로 5개 보여줘.",
            {("pension_eligible", "eq", True)},
            ("total_expense_ratio_pct", "asc"),
        ),
        (
            "잔존일수 365일 초과는 빼고 매수 가능한 국내채권을 잔존일수 낮은 순으로 5개 보여줘.",
            {("remaining_days", "lte", 365), ("currently_buyable", "eq", True)},
            ("remaining_days", "asc"),
        ),
        (
            "환헤지하지 않는 상품은 빼고 공모펀드를 3개월 수익률 높은 순으로 5개 보여줘.",
            {("currency_hedged", "eq", True)},
            ("three_month_return_pct", "desc"),
        ),
        (
            "KRW 국내 ETF끼리 1개월 수익률(%) 높은 순으로 5개 보여줘.",
            {("trading_currency", "eq", "KRW")},
            ("one_month_return_pct", "desc"),
        ),
        (
            "KRW 매수 가능 국내채권끼리 매수수익률(%) 높은 순으로 5개 보여줘.",
            {("trading_currency", "eq", "KRW"), ("currently_buyable", "eq", True)},
            ("buy_yield_pct", "desc"),
        ),
        (
            "USD 거래 해외 ETF끼리만 총보수율(%) 낮은 순으로 5개 비교해줘.",
            {("trading_currency", "eq", "USD")},
            ("total_expense_ratio_pct", "asc"),
        ),
    ],
)
def test_p0_safe_conditions_lower_without_polarity_inversion(
    question: str,
    expected_constraints: set[tuple[str, str, object]],
    ranking: tuple[str, str] | None,
) -> None:
    hints = build_lexical_hints(question)
    actual_constraints = {
        (item["field"], item["operator"], item["value"]) for item in hints["required_constraints"]
    }

    assert expected_constraints <= actual_constraints
    assert hints["ambiguity_spans"] == []
    if ranking is not None:
        assert hints["required_rankings"] == [
            {"field": ranking[0], "direction": ranking[1], "nulls": "last"}
        ]

    plan = QueryPlan.model_validate(
        canonicalize_query_plan_payload(
            question,
            {"question_id": f"p0-plan-{abs(hash(question))}"},
        )
    )
    assert plan.ambiguities == []


@pytest.mark.parametrize(
    ("question", "blocked_field"),
    [
        ("미국 외 지역 해외 ETF를 AUM 큰 순으로 5개 보여줘.", "investment_region"),
        ("회사채가 아닌 매수 가능 국내채권을 만기일 오름차순으로 5개 보여줘.", "bond_major_class"),
    ],
)
def test_inverse_enum_exclusions_remain_fail_closed(
    question: str,
    blocked_field: str,
) -> None:
    hints = build_lexical_hints(question)

    assert not any(item["field"] == blocked_field for item in hints["required_constraints"])
    assert hints["ambiguity_spans"]


@pytest.mark.parametrize(
    ("question", "family", "field", "direction", "limit"),
    [
        (
            "전체 해외 ETF·ETN에서 다른 조건은 걸지 말고 상품명 가나다순으로 3개를 "
            "보여줘. 결측값은 뒤로 보내줘.",
            "overseas_etp",
            "product_name",
            "asc",
            3,
        ),
        (
            "전체 해외 ETF·ETN에서 다른 조건은 걸지 말고 총보수율이 낮은 순으로 5개를 "
            "보여줘. 결측값은 뒤로 보내줘.",
            "overseas_etp",
            "total_expense_ratio_pct",
            "asc",
            5,
        ),
        (
            "전체 해외 ETF·ETN에서 다른 조건은 걸지 말고 운용규모가 큰 순으로 6개를 "
            "보여줘. 결측값은 뒤로 보내줘.",
            "overseas_etp",
            "aum",
            "desc",
            6,
        ),
        (
            "전체 해외 ETF·ETN에서 다른 조건은 걸지 말고 동적 기준일이 최신인 순으로 "
            "2개를 보여줘. 결측값은 뒤로 보내줘.",
            "overseas_etp",
            "dynamic_as_of",
            "desc",
            2,
        ),
        (
            "전체 해외 ETF·ETN에서 다른 조건은 걸지 말고 정적 기준일이 오래된 순으로 "
            "7개를 보여줘. 결측값은 뒤로 보내줘.",
            "overseas_etp",
            "static_as_of",
            "asc",
            7,
        ),
        (
            "전체 국내 ETF·ETN에서 다른 조건은 걸지 말고 일 거래대금이 큰 순으로 7개를 "
            "보여줘. 결측값은 뒤로 보내줘.",
            "domestic_etp",
            "daily_trading_value",
            "desc",
            7,
        ),
        (
            "전체 국내채권에서 다른 조건은 걸지 말고 만기일이 빠른 순으로 5개를 "
            "보여줘. 결측값은 뒤로 보내줘.",
            "bond",
            "maturity_date",
            "asc",
            5,
        ),
        (
            "전체 국내채권에서 다른 조건은 걸지 말고 발행금액이 큰 순으로 6개를 "
            "보여줘. 결측값은 뒤로 보내줘.",
            "bond",
            "issue_amount",
            "desc",
            6,
        ),
        (
            "전체 국내채권에서 다른 조건은 걸지 말고 표면금리가 높은 순으로 2개를 "
            "보여줘. 결측값은 뒤로 보내줘.",
            "bond",
            "coupon_rate_pct",
            "desc",
            2,
        ),
    ],
)
def test_no_extra_condition_searches_compile_without_false_exclusion(
    question: str,
    family: str,
    field: str,
    direction: str,
    limit: int,
) -> None:
    decision = IntentRouter().route(question, f"p0-open-search-{abs(hash(question))}")
    plan = ServerQueryPlanCompiler({}).compile(decision)

    assert decision.disposition.value == "execute"
    assert [item.value for item in decision.draft.product_families] == [family]
    assert plan.ambiguities == []
    assert plan.unsupported_conditions == []
    assert plan.limit == limit
    assert [(item.field, item.direction.value) for item in plan.ranking] == [(field, direction)]


@pytest.mark.parametrize(
    ("question", "family", "function", "field", "group_by"),
    [
        (
            "전체 해외 ETF·ETN에 다른 조건을 적용하지 말고 총보수율 평균을 제공 데이터 "
            "기준으로 정확히 계산해줘.",
            "overseas_etp",
            "avg",
            "total_expense_ratio_pct",
            [],
        ),
        (
            "전체 해외 ETF·ETN에 다른 조건을 적용하지 말고 운용규모 최솟값을 제공 데이터 "
            "기준으로 정확히 계산해줘.",
            "overseas_etp",
            "min",
            "aum",
            [],
        ),
        (
            "전체 해외 ETF·ETN에 다른 조건을 적용하지 말고 각 범주를 빠짐없이 나눠서 "
            "ETF·ETN 유형별 총보수율 평균을 제공 데이터 기준으로 정확히 계산해줘.",
            "overseas_etp",
            "avg",
            "total_expense_ratio_pct",
            ["product_type"],
        ),
        (
            "전체 국내 ETF·ETN에 다른 조건을 적용하지 말고 1개월 수익률 평균을 제공 데이터 "
            "기준으로 정확히 계산해줘.",
            "domestic_etp",
            "avg",
            "one_month_return_pct",
            [],
        ),
        (
            "전체 국내채권에 다른 조건을 적용하지 말고 잔존일수 최솟값을 제공 데이터 "
            "기준으로 정확히 계산해줘.",
            "bond",
            "min",
            "remaining_days",
            [],
        ),
        (
            "전체 국내채권에 다른 조건을 적용하지 말고 각 범주를 빠짐없이 나눠서 채권 "
            "대분류별 매수수익률 평균을 제공 데이터 기준으로 정확히 계산해줘.",
            "bond",
            "avg",
            "buy_yield_pct",
            ["bond_major_class"],
        ),
    ],
)
def test_no_extra_condition_aggregates_keep_exact_semantics(
    question: str,
    family: str,
    function: str,
    field: str,
    group_by: list[str],
) -> None:
    decision = IntentRouter().route(question, f"p0-open-aggregate-{abs(hash(question))}")
    plan = ServerQueryPlanCompiler({}).compile(decision)

    assert decision.disposition.value == "execute"
    assert decision.draft.intent.value == "aggregate"
    assert [item.value for item in decision.draft.product_families] == [family]
    assert plan.ambiguities == []
    assert plan.unsupported_conditions == []
    assert [(item.function.value, item.field) for item in plan.intent_payload.aggregations] == [
        (function, field)
    ]
    assert plan.intent_payload.group_by == group_by


@pytest.mark.parametrize(
    "question",
    [
        "다른 조건은 걸지 말고 미국 외 지역 해외 ETF를 AUM 큰 순으로 보여줘.",
        "다른 조건은 걸지 말고 회사채가 아닌 국내채권을 만기일 빠른 순으로 보여줘.",
    ],
)
def test_no_extra_condition_disclaimer_preserves_real_ambiguities(question: str) -> None:
    decision = IntentRouter().route(question, f"p0-disclaimer-lock-{abs(hash(question))}")

    assert decision.disposition.value == "clarify"
    assert decision.reason_code == "semantic_coverage_incomplete"


def test_no_extra_condition_disclaimer_preserves_a_safe_explicit_exclusion() -> None:
    question = "다른 조건은 걸지 말고 ETF 말고 ETN만 해외 ETP를 보여줘."
    hints = build_lexical_hints(question)

    assert hints["ambiguity_spans"] == []
    assert {
        (item["field"], item["operator"], item["value"]) for item in hints["required_constraints"]
    } >= {("product_type", "neq", "ETF")}
