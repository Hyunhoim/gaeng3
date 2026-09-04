from __future__ import annotations

import pytest

from finance_agent_core.agent.aggregate_parser import compile_aggregate_plan
from finance_agent_core.agent.fund_comparison_parser import (
    extract_fund_comparison_fields,
)
from finance_agent_core.agent.linker import build_lexical_hints
from finance_agent_core.agent.product_comparison import (
    ProductComparisonParseError,
    extract_comparison_fields,
)
from finance_agent_core.config import ValueType, load_field_registry
from finance_agent_core.contracts.queryplan import ProductFamily

_FAMILY_NOUN = {
    "bond": "원화 채권",
    "domestic_etp": "국내 ETF",
    "overseas_etp": "해외 ETF",
    "fund": "공모펀드",
}


def _comparison_fields(question: str, family: str) -> list[str]:
    if family == "fund":
        return list(extract_fund_comparison_fields(question))
    return extract_comparison_fields(question, ProductFamily(family))


def test_official_numeric_labels_cover_search_filter_and_ranking_matrix() -> None:
    registry = load_field_registry()
    for family in registry.datasets:
        for field, base_definition in registry.fields.items():
            if family not in base_definition.datasets:
                continue
            definition = base_definition.resolve(family)
            if definition.value_type is not ValueType.NUMBER:
                continue
            if definition.queryable:
                question = (
                    f"{definition.label} 1 이상인 {_FAMILY_NOUN[family]} 보여줘"
                )
                hints = build_lexical_hints(
                    question,
                    family,
                    force_product_family_hint=True,
                )
                assert field in {
                    item["field"] for item in hints["required_constraints"]
                }, (family, field, question, hints)
            if definition.sortable:
                question = (
                    f"{definition.label} 높은 순 {_FAMILY_NOUN[family]} 3개 보여줘"
                )
                hints = build_lexical_hints(
                    question,
                    family,
                    force_product_family_hint=True,
                )
                assert [item["field"] for item in hints["required_rankings"]] == [
                    field
                ], (family, field, question, hints)


def test_official_labels_cover_all_safe_sort_and_comparison_capabilities() -> None:
    registry = load_field_registry()
    for family in registry.datasets:
        for field, base_definition in registry.fields.items():
            if family not in base_definition.datasets:
                continue
            definition = base_definition.resolve(family)
            if definition.sortable:
                if definition.value_type is ValueType.DATE:
                    direction = "최신 순"
                elif definition.value_type in {
                    ValueType.STRING,
                    ValueType.ENUM,
                    ValueType.BOOLEAN,
                }:
                    direction = "오름차순"
                else:
                    continue
                question = (
                    f"{definition.label} {direction} {_FAMILY_NOUN[family]} 3개 보여줘"
                )
                hints = build_lexical_hints(
                    question,
                    family,
                    force_product_family_hint=True,
                )
                assert [item["field"] for item in hints["required_rankings"]] == [
                    field
                ], (family, field, question, hints)
            if definition.comparable:
                assert field in _comparison_fields(
                    f"{definition.label} 비교",
                    family,
                ), (family, field, definition.label)


def test_official_numeric_labels_cover_aggregate_matrix() -> None:
    registry = load_field_registry()
    for family in registry.datasets:
        for field, base_definition in registry.fields.items():
            if family not in base_definition.datasets:
                continue
            definition = base_definition.resolve(family)
            if not definition.aggregatable or definition.value_type is not ValueType.NUMBER:
                continue
            plan = compile_aggregate_plan(
                question=f"{_FAMILY_NOUN[family]} {definition.label} 평균 알려줘",
                question_id=f"aggregate-{family}-{field}",
                family=ProductFamily(family),
                base_payload={
                    "schema_version": "1.0",
                    "constraints": [],
                    "ambiguities": [],
                    "unsupported_conditions": [],
                },
                requested_limit=None,
            )
            assert [item.field for item in plan.intent_payload.aggregations] == [field]


@pytest.mark.parametrize(
    ("question", "family", "expected", "forbidden"),
    [
        (
            "120거래일 연환산 변동성 1 이상인 국내 ETF 보여줘",
            "domestic_etp",
            "one_hundred_twenty_day_volatility_pct",
            "twenty_day_volatility_pct",
        ),
        (
            "순자산총액 높은 순 국내 ETF 3개 보여줘",
            "domestic_etp",
            "net_asset_total",
            "aum",
        ),
        (
            "1개월 평균 거래대금 높은 순 국내 ETF 3개 보여줘",
            "domestic_etp",
            "one_month_average_trading_value",
            "daily_trading_value",
        ),
        (
            "총발행금액 높은 순 원화 채권 3개 보여줘",
            "bond",
            "total_issue_amount",
            "issue_amount",
        ),
    ],
)
def test_longest_official_field_label_owns_nested_surface(
    question: str,
    family: str,
    expected: str,
    forbidden: str,
) -> None:
    hints = build_lexical_hints(question, family, force_product_family_hint=True)
    fields = {
        *(item["field"] for item in hints["required_constraints"]),
        *(item["field"] for item in hints["required_rankings"]),
    }
    assert expected in fields
    assert forbidden not in fields


def test_registry_label_filters_require_exact_typed_values() -> None:
    enum_hints = build_lexical_hints(
        "지수 복제방법: Full인 해외 ETF 보여줘",
        "overseas_etp",
        force_product_family_hint=True,
    )
    text_hints = build_lexical_hints(
        "해외 ETP 운용 전략에 'swap' 포함된 해외 ETF 보여줘",
        "overseas_etp",
        force_product_family_hint=True,
    )
    boolean_hints = build_lexical_hints(
        "당사 판매 여부: 예인 공모펀드 보여줘",
        "fund",
        force_product_family_hint=True,
    )
    assert {
        (item["field"], item["operator"], item["value"])
        for item in enum_hints["required_constraints"]
    } >= {("index_replication_method", "eq", "Full")}
    assert {
        (item["field"], item["operator"], item["value"])
        for item in text_hints["required_constraints"]
    } >= {("strategy_description", "contains", "swap")}
    assert {
        (item["field"], item["operator"], item["value"])
        for item in boolean_hints["required_constraints"]
    } >= {("company_sellable", "eq", True)}


def test_product_name_sort_preamble_is_not_misread_as_an_exact_name_filter() -> None:
    hints = build_lexical_hints(
        "이름순으로 보고 싶습니다. 신용등급 AA-인 채권 다섯 종목을 조회해 주세요.",
        "bond",
        force_product_family_hint=True,
    )

    assert not any(
        item["field"] == "product_name" for item in hints["required_constraints"]
    )
    assert {
        (item["field"], item["operator"], item["value"])
        for item in hints["required_constraints"]
    } >= {("credit_rating", "eq", "AA-")}
    assert hints["required_rankings"] == [
        {"field": "product_name", "direction": "asc", "nulls": "last"}
    ]


@pytest.mark.parametrize(
    ("question", "family"),
    [
        ("fee 비교", ProductFamily.OVERSEAS_ETP),
        ("지역 비교", ProductFamily.DOMESTIC_ETP),
        ("발행사 비교", ProductFamily.OVERSEAS_ETP),
    ],
)
def test_low_confidence_registry_aliases_do_not_grant_comparison_authority(
    question: str,
    family: ProductFamily,
) -> None:
    with pytest.raises(ProductComparisonParseError):
        extract_comparison_fields(question, family)
