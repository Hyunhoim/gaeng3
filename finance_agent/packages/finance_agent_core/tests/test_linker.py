from finance_agent_core.agent.linker import (
    build_lexical_hints,
    canonicalize_linked_query_plan,
    canonicalize_query_plan_payload,
)
from finance_agent_core.agent.providers import first_vertical_slice_plan
from finance_agent_core.contracts import QueryPlan
from finance_agent_core.evaluation import load_core_evaluation_suite
from finance_agent_core.evaluation.scoring import semantic_checks


def _plan_with(
    *,
    constraints: list[dict[str, object]],
    ranking: list[dict[str, str]],
) -> QueryPlan:
    payload = first_vertical_slice_plan("linked-plan").model_dump(mode="json")
    payload["constraints"] = constraints
    payload["ranking"] = ranking
    payload["ambiguities"] = []
    payload["unsupported_conditions"] = []
    return QueryPlan.model_validate(payload)


def test_lexical_hints_preserve_explicit_status_and_sort() -> None:
    hints = build_lexical_hints(
        "판매 가능하고 거래 중지가 아닌 미국 채권형 해외 ETF를 AUM 큰 순으로 보여줘"
    )

    assert hints["required_eq_constraints"] == [
        {"field": "product_type", "operator": "eq", "value": "ETF"},
        {"field": "asset_type", "operator": "eq", "value": "Bond"},
        {
            "field": "investment_region",
            "operator": "eq",
            "value": "United States of America",
        },
        {"field": "trading_suspended", "operator": "eq", "value": False},
        {"field": "sellable", "operator": "eq", "value": True},
    ]
    assert hints["required_rankings"] == [{"field": "aum", "direction": "desc", "nulls": "last"}]


def test_linker_canonicalizes_multiple_exchange_values_to_in() -> None:
    plan = _plan_with(
        constraints=[
            {
                "field": "exchange_code",
                "operator": "eq",
                "value": "AMX",
                "unit": "code",
                "strength": "locked",
            },
            {
                "field": "exchange_code",
                "operator": "eq",
                "value": "NAS",
                "unit": "code",
                "strength": "locked",
            },
            {
                "field": "product_type",
                "operator": "eq",
                "value": "ETF",
                "unit": "code",
                "strength": "locked",
            },
        ],
        ranking=[{"field": "aum", "direction": "desc", "nulls": "last"}],
    )

    linked = canonicalize_linked_query_plan(
        "AMEX나 NASDAQ의 해외 ETF를 AUM 상위 순으로 5개 보여줘",
        plan,
    )

    exchange = [item for item in linked.constraints if item.field == "exchange_code"]
    assert len(exchange) == 1
    assert exchange[0].operator.value == "in"
    assert exchange[0].value == ["AMX", "NAS"]


def test_linker_separates_aum_filter_from_product_name_sort() -> None:
    plan = _plan_with(
        constraints=[
            {
                "field": "aum",
                "operator": "eq",
                "value": 0,
                "unit": "source_currency_amount",
                "strength": "locked",
            },
            {
                "field": "product_type",
                "operator": "eq",
                "value": "ETF",
                "unit": "code",
                "strength": "locked",
            },
        ],
        ranking=[{"field": "aum", "direction": "desc", "nulls": "last"}],
    )

    linked = canonicalize_linked_query_plan(
        "AUM이 정확히 0인 해외 ETF를 상품명 순서로 5개 보여줘",
        plan,
    )

    assert [item.model_dump(mode="json") for item in linked.ranking] == [
        {"field": "product_name", "direction": "asc", "nulls": "last"}
    ]


def test_linker_removes_invented_quality_bound_and_blocks_unsupported() -> None:
    plan = _plan_with(
        constraints=[
            {
                "field": "product_type",
                "operator": "eq",
                "value": "ETF",
                "unit": "code",
                "strength": "locked",
            },
            {
                "field": "total_expense_ratio_pct",
                "operator": "gte",
                "value": 5.0,
                "unit": "pct_point",
                "strength": "locked",
            },
        ],
        ranking=[],
    )

    linked = canonicalize_linked_query_plan(
        "배당수익률이 5% 이상인 미국 해외 ETF를 찾아줘",
        plan,
    )

    assert {item.field for item in linked.constraints} == {
        "product_type",
        "investment_region",
    }
    assert not linked.ambiguities
    assert [item.span for item in linked.unsupported_conditions] == ["배당수익률"]


def test_domestic_hints_parse_family_amount_return_and_ranking() -> None:
    hints = build_lexical_hints(
        "AUM 1천억원에서 1조원 사이인 미국 주식형 국내 ETF를 1개월 수익률 높은 순으로 3개 보여줘"
    )

    assert hints["product_family"] == "domestic_etp"
    assert {
        (item["field"], item["operator"], str(item["value"]))
        for item in hints["required_constraints"]
    } >= {
        ("product_type", "eq", "ETF"),
        ("investment_region", "eq", "미국"),
        ("asset_type", "eq", "주식"),
        ("aum", "between", "[100000000000, 1000000000000]"),
    }
    assert hints["required_rankings"] == [
        {
            "field": "one_month_return_pct",
            "direction": "desc",
            "nulls": "last",
        }
    ]
    assert hints["limit"] == 3


def test_lexical_limit_matches_router_for_result_counter() -> None:
    hints = build_lexical_hints("연금 거래 가능한 국내 ETF를 AUM 큰 순으로 3건 보여줘")

    assert hints["limit"] == 3


def test_prelink_overrides_wrong_model_family_with_domestic_contract() -> None:
    payload = first_vertical_slice_plan("wrong-family").model_dump(mode="json")
    linked = canonicalize_query_plan_payload(
        "2배 레버리지 국내 ETF를 1개월 수익률 높은 순으로 5개 보여줘",
        payload,
    )

    assert linked["product_families"] == ["domestic_etp"]
    assert linked["projection"][3] == "one_month_return_pct"
    assert any(
        item["field"] == "leverage_factor" and item["value"] == 2 for item in linked["constraints"]
    )


def test_bond_hints_lock_availability_remaining_days_and_yield_ranking() -> None:
    hints = build_lexical_hints(
        "잔존일수 365일 이하인 매수 가능한 회사채를 매수수익률 높은 순으로 3개 보여줘"
    )

    assert hints["product_family"] == "bond"
    assert {
        (item["field"], item["operator"], item["value"]) for item in hints["required_constraints"]
    } == {
        ("bond_major_class", "eq", "회사채"),
        ("currently_buyable", "eq", True),
        ("remaining_days", "lte", 365),
    }
    assert hints["required_rankings"] == [
        {"field": "buy_yield_pct", "direction": "desc", "nulls": "last"}
    ]
    assert hints["limit"] == 3


def test_bond_ordered_credit_rating_is_blocked_without_guessing_scale() -> None:
    payload = first_vertical_slice_plan("wrong-family").model_dump(mode="json")
    linked = canonicalize_query_plan_payload(
        "신용등급 AA- 이상인 국내채권을 찾아줘",
        payload,
    )

    assert linked["product_families"] == ["bond"]
    assert linked["unsupported_conditions"]
    assert "신용등급 AA- 이상" in linked["unsupported_conditions"][0]["span"]


def test_fund_development_linker_matches_all_frozen_plans() -> None:
    suite = load_core_evaluation_suite("fund").suite
    development = [case for case in suite.cases if case.split.value == "development"]

    for case in development:
        payload = canonicalize_query_plan_payload(
            case.question,
            first_vertical_slice_plan(case.id).model_dump(mode="json"),
        )
        plan = QueryPlan.model_validate(payload)
        checks = semantic_checks(case, plan, "fund")
        assert checks["plan_exact"], (
            case.id,
            [name for name, passed in checks.items() if not passed],
        )


def test_fund_aum_without_currency_requires_clarification() -> None:
    hints = build_lexical_hints("AUM이 큰 공모펀드 5개를 보여줘")

    assert hints["product_family"] == "fund"
    assert hints["required_eq_constraints"] == [
        {"field": "public_offering", "operator": "eq", "value": True}
    ]
    assert hints["required_rankings"] == [{"field": "aum", "direction": "desc", "nulls": "last"}]
    assert hints["ambiguity_spans"] == ["AUM 비교 통화"]


def test_fund_model_family_handoff_blocks_unsupported_class_aggregation() -> None:
    suite = load_core_evaluation_suite("fund").suite
    case = next(case for case in suite.cases if case.id == "fund-050")
    payload = first_vertical_slice_plan(case.id).model_dump(mode="json")
    payload["product_families"] = ["fund"]

    linked = QueryPlan.model_validate(
        canonicalize_query_plan_payload(
            case.question,
            payload,
        )
    )
    checks = semantic_checks(case, linked, "fund")

    assert checks["plan_exact"], [name for name, passed in checks.items() if not passed]
    assert [item.field for item in linked.constraints] == ["public_offering"]
    assert linked.ranking == []
    assert {item.span for item in linked.unsupported_conditions} == {
        "대표 펀드",
        "클래스는 합쳐서",
    }
