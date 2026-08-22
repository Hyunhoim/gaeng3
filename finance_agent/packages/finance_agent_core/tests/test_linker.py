import pytest

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


def test_explicit_etp_type_overrides_broad_etf_etn_family_phrase() -> None:
    hints = build_lexical_hints("ETP 유형이 ETF인 해외 ETF·ETN 상품 중 티커 IVEG.O 상세 조회")

    product_types = [
        item for item in hints["required_constraints"] if item["field"] == "product_type"
    ]
    assert product_types == [{"field": "product_type", "operator": "eq", "value": "ETF"}]


@pytest.mark.parametrize(
    "question",
    [
        "국내 ETF·ETN 중 AUM이 큰 상품 3개를 보여줘",
        "해외 ETF나 ETN 중 총보수가 낮은 상품 3개를 보여줘",
    ],
)
def test_broad_etf_etn_family_phrase_does_not_add_a_type_constraint(
    question: str,
) -> None:
    hints = build_lexical_hints(question)

    assert not any(item["field"] == "product_type" for item in hints["required_constraints"])


@pytest.mark.parametrize(
    "question",
    [
        "해외 ETP 중 ETP 유형이 ETF 또는 ETN에 포함되는 상품 3개",
        "해외 ETF나 ETN 중 ETF인지 ETN인지 알려주는 상품 3개",
    ],
)
def test_explicit_multi_type_condition_is_preserved_as_a_set(question: str) -> None:
    hints = build_lexical_hints(question)

    product_types = [
        item for item in hints["required_constraints"] if item["field"] == "product_type"
    ]
    assert product_types == [
        {"field": "product_type", "operator": "eq", "value": "ETF"},
        {"field": "product_type", "operator": "eq", "value": "ETN"},
    ]


@pytest.mark.parametrize(
    "question",
    [
        "ETF 유형에 해당하며 해외 ETF·ETN 상품 조회",
        "ETF인지 ETN인지 확인하고 ETF라면 상세 조회",
    ],
)
def test_conditional_or_type_wording_keeps_the_explicit_etf_constraint(
    question: str,
) -> None:
    hints = build_lexical_hints(question, "overseas_etp")

    product_types = [
        item for item in hints["required_constraints"] if item["field"] == "product_type"
    ]
    assert product_types == [{"field": "product_type", "operator": "eq", "value": "ETF"}]


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


def test_domestic_pension_and_identity_wording_are_normalized() -> None:
    pension = build_lexical_hints("국내에서 연금으로 거래 가능한 ETF의 수를 계산해줘")
    identity = build_lexical_hints("국내 ETF 중 상품번호가 KR7091160002인 상품 상세 정보")

    assert pension["product_family"] == "domestic_etp"
    assert {
        (item["field"], item["operator"], item["value"]) for item in pension["required_constraints"]
    } >= {
        ("product_type", "eq", "ETF"),
        ("pension_eligible", "eq", True),
    }
    assert identity["required_eq_constraints"][-1] == {
        "field": "product_id",
        "operator": "eq",
        "value": "KR7091160002",
    }


def test_domestic_natural_pension_and_aum_ranking_wording_are_normalized() -> None:
    hints = build_lexical_hints("연금계좌로 거래가 가능한 국내 ETF 중 운용 자산이 제일 큰 거 3개")

    assert {
        (item["field"], item["operator"], item["value"]) for item in hints["required_constraints"]
    } >= {
        ("product_type", "eq", "ETF"),
        ("pension_eligible", "eq", True),
    }
    assert hints["required_rankings"] == [{"field": "aum", "direction": "desc", "nulls": "last"}]
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


def test_bond_plain_maturity_years_are_normalized_to_remaining_days() -> None:
    hints = build_lexical_hints("만기가 1년 이하인 채권 찾아줘")

    assert hints["product_family"] == "bond"
    assert {
        (item["field"], item["operator"], tuple(item["value"]))
        for item in hints["required_constraints"]
    } == {("remaining_days", "between", (0, 365))}


def test_bond_purchase_synonym_and_short_remaining_term_sort_are_normalized() -> None:
    hints = build_lexical_hints("현재 구매 가능한 국내 채권을 잔존 기일이 짧은 순서로 3건 검색해줘")

    assert ("currently_buyable", "eq", True) in {
        (item["field"], item["operator"], item["value"]) for item in hints["required_constraints"]
    }
    assert hints["required_rankings"] == [
        {"field": "remaining_days", "direction": "asc", "nulls": "last"}
    ]


def test_low_side_wording_overrides_generic_top_n_direction() -> None:
    hints = build_lexical_hints("미국에 투자하는 해외 ETF를 총보수율 낮은 쪽부터 상위 3건 보여줘")

    assert hints["required_rankings"] == [
        {"field": "total_expense_ratio_pct", "direction": "asc", "nulls": "last"}
    ]


def test_negated_public_fund_scope_is_not_silently_inverted() -> None:
    hints = build_lexical_hints("공모가 아닌 공모펀드를 보여줘")

    assert hints["product_family"] == "fund"
    assert not any(item["field"] == "public_offering" for item in hints["required_constraints"])
    assert any("공모가 아닌" in span for span in hints["unsupported_spans"])


def test_negated_trade_availability_requires_clarification() -> None:
    hints = build_lexical_hints("현재 거래 가능하지 않은 해외 ETF를 보여줘")

    assert not any(
        item["field"] in {"sellable", "trading_suspended"} for item in hints["required_constraints"]
    )
    assert any("거래 가능하지 않" in span for span in hints["ambiguity_spans"])


@pytest.mark.parametrize(
    "question",
    [
        "거래 중지되지 않은 국내 ETF·ETN 3개를 보여줘",
        "거래 정지된 상품을 제외하고 해외 ETF·ETN 3개를 보여줘",
    ],
)
def test_explicit_non_suspended_wording_is_not_inverted(question: str) -> None:
    hints = build_lexical_hints(question)

    suspended = [
        item for item in hints["required_constraints"] if item["field"] == "trading_suspended"
    ]
    assert suspended == [{"field": "trading_suspended", "operator": "eq", "value": False}]


def test_search_projection_includes_constraint_and_ranking_evidence_fields() -> None:
    payload = first_vertical_slice_plan("expanded-search-evidence").model_dump(mode="json")
    linked = canonicalize_query_plan_payload(
        "레버리지 배수가 1배 이상인 국내 ETF·ETN을 1년 수익률 낮은 순으로 3개 보여줘",
        payload,
    )

    assert "leverage_factor" in linked["projection"]
    assert "one_year_return_pct" in linked["projection"]


def test_vague_negated_ranking_is_not_silently_dropped() -> None:
    hints = build_lexical_hints("AUM이 크지 않은 해외 ETF를 보여줘")

    assert hints["required_rankings"] == []
    assert any("AUM이 크지 않" in span for span in hints["ambiguity_spans"])


@pytest.mark.parametrize(
    ("question", "blocked_field"),
    [
        ("미국 시장이 아닌 해외 ETF를 보여줘", "investment_region"),
        ("채권형이 아닌 해외 ETF를 보여줘", "asset_type"),
    ],
)
def test_negated_region_or_asset_type_is_not_silently_inverted(
    question: str,
    blocked_field: str,
) -> None:
    hints = build_lexical_hints(question)

    assert not any(item["field"] == blocked_field for item in hints["required_constraints"])
    assert hints["ambiguity_spans"]


def test_excluded_unlabeled_identity_is_not_silently_dropped() -> None:
    hints = build_lexical_hints("B2를 제외한 해외 ETF를 보여줘")

    assert not any(
        item["field"] in {"product_id", "ticker", "isin"} for item in hints["required_constraints"]
    )
    assert any("B2를 제외" in span for span in hints["ambiguity_spans"])


@pytest.mark.parametrize(
    ("question", "blocked_field"),
    [
        ("총보수 0.2% 이하가 아닌 해외 ETF를 보여줘", "total_expense_ratio_pct"),
        ("만기일 2027-01-01 이전이 아닌 국내채권을 보여줘", "maturity_date"),
    ],
)
def test_negated_numeric_or_date_operator_requires_clarification(
    question: str,
    blocked_field: str,
) -> None:
    hints = build_lexical_hints(question)

    assert not any(item["field"] == blocked_field for item in hints["required_constraints"])
    assert hints["ambiguity_spans"]


def test_explicit_negative_boolean_filters_keep_their_polarity() -> None:
    overseas = build_lexical_hints("판매가 가능하지 않은 해외 ETF를 보여줘")
    domestic = build_lexical_hints("연금 거래가 가능하지 않은 국내 ETF를 보여줘")
    bond = build_lexical_hints("매수가 가능하지 않은 국내채권을 보여줘")

    assert ("sellable", False) in {
        (item["field"], item["value"]) for item in overseas["required_constraints"]
    }
    assert ("pension_eligible", False) in {
        (item["field"], item["value"]) for item in domestic["required_constraints"]
    }
    assert ("currently_buyable", False) in {
        (item["field"], item["value"]) for item in bond["required_constraints"]
    }


def test_bond_ordered_credit_rating_expands_from_registry_scale() -> None:
    payload = first_vertical_slice_plan("wrong-family").model_dump(mode="json")
    linked = canonicalize_query_plan_payload(
        "신용등급 AA- 이상인 국내채권을 찾아줘",
        payload,
    )

    assert linked["product_families"] == ["bond"]
    assert linked["unsupported_conditions"] == []
    assert next(item for item in linked["constraints"] if item["field"] == "credit_rating") == {
        "field": "credit_rating",
        "operator": "in",
        "value": ["AAA", "AA+", "AA0", "AA-"],
        "unit": "code",
        "strength": "locked",
    }


def test_bond_invalid_credit_rating_is_blocked_instead_of_silently_dropped() -> None:
    payload = first_vertical_slice_plan("invalid-rating").model_dump(mode="json")
    linked = canonicalize_query_plan_payload(
        "신용등급 AAAA인 채권 찾아줘",
        payload,
    )

    assert linked["product_families"] == ["bond"]
    assert linked["unsupported_conditions"]
    assert linked["unsupported_conditions"][0]["span"] == "신용등급 AAAA"
    assert not any(item["field"] == "credit_rating" for item in linked["constraints"])


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


def test_fund_one_year_return_is_linked_without_unsupported_blocker() -> None:
    hints = build_lexical_hints("1년 수익률이 높은 공모펀드를 5개 찾아줘")

    assert hints["product_family"] == "fund"
    assert hints["required_eq_constraints"] == [
        {"field": "public_offering", "operator": "eq", "value": True}
    ]
    assert hints["required_rankings"] == [
        {"field": "one_year_return_pct", "direction": "desc", "nulls": "last"}
    ]
    assert "1년 수익률" not in hints["unsupported_spans"]


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
