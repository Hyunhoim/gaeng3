from __future__ import annotations

import pytest

from finance_agent_core.agent.compiler import ServerQueryPlanCompiler
from finance_agent_core.agent.router import IntentRouter
from finance_agent_core.contracts.queryplan import AggregateFunction, ProductFamily
from finance_agent_core.contracts.routing import InteractionIntent, RouteDisposition


def _compile(question: str):
    decision = IntentRouter().route(question, f"fresh-aggregate-{abs(hash(question))}")
    assert decision.disposition is RouteDisposition.EXECUTE
    return decision, ServerQueryPlanCompiler({}).compile(decision)


@pytest.mark.parametrize(
    ("question", "family"),
    [
        (
            "해외 ETP의 ETF와 ETN 상품 수를 유형별로 알려 주세요.",
            ProductFamily.OVERSEAS_ETP,
        ),
        (
            "해외 ETP에서 ETN과 ETF를 상품 종류별로 나눠 각각 몇 개인지 집계해 주세요.",
            ProductFamily.OVERSEAS_ETP,
        ),
        (
            "국내 ETP ETF/ETN 개수를 유형별로 알려 주세요.",
            ProductFamily.DOMESTIC_ETP,
        ),
        (
            "국내 상장지수상품의 ETN 및 ETF 상품 수를 구분별로 세어 주세요.",
            ProductFamily.DOMESTIC_ETP,
        ),
    ],
)
def test_etf_etn_enumeration_is_a_product_type_group(
    question: str,
    family: ProductFamily,
) -> None:
    decision, plan = _compile(question)

    assert decision.draft.intent is InteractionIntent.AGGREGATE
    assert plan.product_families == [family]
    assert plan.intent_payload.group_by == ["product_type"]
    assert [(item.function, item.field) for item in plan.intent_payload.aggregations] == [
        (AggregateFunction.COUNT, "product_id")
    ]
    assert all(item.field not in {"manager", "product_type"} for item in plan.constraints)


@pytest.mark.parametrize(
    "question",
    [
        "펀드 국내·해외·혼합 구분별 상품 수를 알려 주세요.",
        "공모펀드를 해외와 국내 및 혼합으로 나눠 각각 몇 상품인지 알려 주세요.",
        "국내와 해외, 혼합 펀드는 각각 상품이 몇 개인지 집계해 주세요.",
    ],
)
def test_fund_geography_enumeration_is_one_group_axis(question: str) -> None:
    decision, plan = _compile(question)

    assert decision.draft.intent is InteractionIntent.AGGREGATE
    assert plan.product_families == [ProductFamily.FUND]
    assert plan.intent_payload.group_by == ["fund_geography_scope"]
    assert [(item.function, item.field) for item in plan.intent_payload.aggregations] == [
        (AggregateFunction.COUNT, "product_id")
    ]
    assert all(item.field != "fund_geography_scope" for item in plan.constraints)


@pytest.mark.parametrize(
    "question",
    [
        "해외 ETP 자산군별 최대 AUM을 알려 주세요.",
        "해외 상장지수상품을 투자 자산 유형마다 AUM 최댓값으로 집계해 주세요.",
    ],
)
def test_grouped_maximum_owns_ranking_alias_before_search(question: str) -> None:
    decision, plan = _compile(question)

    assert decision.draft.intent is InteractionIntent.AGGREGATE
    assert plan.product_families == [ProductFamily.OVERSEAS_ETP]
    assert plan.intent_payload.group_by == ["asset_type"]
    assert [(item.function, item.field) for item in plan.intent_payload.aggregations] == [
        (AggregateFunction.MAX, "aum")
    ]
    assert plan.ranking == []


def test_grouped_product_ranking_is_not_rewritten_as_a_value_aggregate() -> None:
    decision, plan = _compile("해외 ETP 자산군별 AUM 큰 순으로 상품 3개를 알려 주세요.")

    assert decision.draft.intent is InteractionIntent.SEARCH
    assert plan.intent_payload.group_by == []
    assert plan.intent_payload.aggregations == []
    assert [(item.field, item.direction.value) for item in plan.ranking] == [
        ("aum", "desc")
    ]
    assert plan.limit == 3


def test_real_overseas_manager_possessive_remains_a_manager_filter() -> None:
    decision, plan = _compile("블랙록의 해외 ETF 상품 수를 알려 주세요.")

    assert decision.draft.intent is InteractionIntent.AGGREGATE
    assert ("manager", "블랙록") in [
        (item.field, item.value) for item in plan.constraints
    ]
    assert plan.intent_payload.group_by == []

def test_single_etf_count_remains_a_filter_not_a_type_group() -> None:
    _, plan = _compile("해외 ETP 중 ETF만 몇 개인가요?")

    assert ("product_type", "ETF") in [
        (item.field, item.value) for item in plan.constraints
    ]
    assert plan.intent_payload.group_by == []
