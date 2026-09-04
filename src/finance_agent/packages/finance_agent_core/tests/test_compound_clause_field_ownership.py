from __future__ import annotations

import pytest

from finance_agent_core.agent.compiler import ServerQueryPlanCompiler
from finance_agent_core.agent.compound_query import TypedCompoundQueryEngine
from finance_agent_core.agent.router import IntentRouter
from finance_agent_core.contracts.queryplan import ProductFamily, SortDirection


def _engine() -> TypedCompoundQueryEngine:
    return TypedCompoundQueryEngine({}, record_cache=object())  # type: ignore[arg-type]


def _filters(plan: object) -> set[tuple[str, str, object]]:
    return {
        (item.source_field, item.operator, item.value)
        for item in plan.filters  # type: ignore[attr-defined]
    }


@pytest.mark.parametrize(
    "provider_surface",
    ("당사 판매", "미래에셋증권에서 판매하는", "미래에셋에서 취급하는"),
)
def test_parallel_fund_clause_preserves_explicit_company_sale_scope(
    provider_surface: str,
) -> None:
    question = (
        "거래정지가 아닌 국내 ETF는 일거래대금 상위 5개, "
        f"{provider_surface} 공모펀드는 순자산 상위 5개를 따로 알려 주세요."
    )

    plan = _engine().compile(question, "parallel-company-sale")

    assert plan is not None
    assert plan.kind == "parallel"
    domestic, fund = plan.family_plans
    assert domestic.product_family is ProductFamily.DOMESTIC_ETP
    assert ("pd_tr_yn", "eq", "0") in _filters(domestic)
    assert fund.product_family is ProductFamily.FUND
    assert ("thco_sale_yn", "eq", "Y") in _filters(fund)
    assert fund.metric.source_fields == ("fd_nast_suma",)
    assert fund.direction is SortDirection.DESC
    assert fund.limit == 5


def test_parallel_exact_registry_fields_keep_clause_local_family_ownership() -> None:
    plan = _engine().compile(
        "국내채권은 평가가격 낮은 4개, 공모펀드는 최신 분배율 높은 "
        "4개를 서로 섞지 말고 알려 주세요.",
        "parallel-registry-fields",
    )

    assert plan is not None
    assert plan.kind == "parallel"
    bond, fund = plan.family_plans
    assert bond.product_family is ProductFamily.BOND
    assert bond.metric.source_fields == ("eval_price",)
    assert bond.direction is SortDirection.ASC
    assert bond.limit == 4
    assert fund.product_family is ProductFamily.FUND
    assert fund.metric.source_fields == ("fd_last_dstb_r",)
    assert fund.direction is SortDirection.DESC
    assert fund.limit == 4


def test_domestic_net_asset_total_is_not_reduced_to_aum() -> None:
    plan = _engine().compile(
        "국내 ETF는 순자산총액 높은 3개, 해외 ETF는 AUM 높은 3개를 각각 알려 주세요.",
        "parallel-nested-registry-field",
    )

    assert plan is not None
    domestic, overseas = plan.family_plans
    assert domestic.metric.source_fields == ("pd_net_tamt",)
    assert overseas.metric.source_fields == ("du_last_aum",)


@pytest.mark.parametrize(
    ("question", "expected"),
    [
        (
            "국내 ETF는 1개월 수익률, 공모펀드는 3개월 수익률 기준으로 "
            "각각 상위 3개",
            (("du_er_1m",), ("fd_mm3_ern_r",)),
        ),
        (
            "국내 ETF는 거래대금, 해외 ETF는 거래량 기준으로 각각 상위 2개",
            (("du_val_1d",), ("du_vol_1d",)),
        ),
        (
            "국내 ETF는 1개월 수익률, 공모펀드는 3개월 수익률 높은 순으로 "
            "각각 3개씩",
            (("du_er_1m",), ("fd_mm3_ern_r",)),
        ),
        (
            "국내 ETF는 거래대금, 해외 ETF는 거래량 높은 순으로 각각 3개씩",
            (("du_val_1d",), ("du_vol_1d",)),
        ),
    ],
)
def test_parallel_shared_order_preserves_each_clause_metric(
    question: str,
    expected: tuple[tuple[str, ...], tuple[str, ...]],
) -> None:
    plan = _engine().compile(question, "parallel-owned-metrics")

    assert plan is not None
    assert plan.kind == "parallel"
    assert tuple(item.metric.source_fields for item in plan.family_plans) == expected


@pytest.mark.parametrize(
    "question",
    [
        "국내 ETF는 1개월 수익률, 공모펀드는 각각 상위 3개",
        "국내 ETF는 1개월과 3개월 수익률, 공모펀드는 3개월 수익률 기준으로 "
        "각각 상위 3개",
    ],
)
def test_parallel_clause_metric_gap_or_overlap_is_atomic(question: str) -> None:
    assert _engine().compile(question, "parallel-metric-gap") is None


def test_global_ranking_excludes_only_schema_ineligible_family() -> None:
    plan = _engine().compile(
        "국내 ETF, 해외 ETF, 공모펀드를 1년 수익률 기준으로 한 목록에서 상위 10개",
        "global-schema-exclusion",
    )

    assert plan is not None
    assert plan.kind == "global_ranking"
    assert [item.product_family for item in plan.family_plans] == [
        ProductFamily.DOMESTIC_ETP,
        ProductFamily.FUND,
    ]
    assert [item.metric.source_fields for item in plan.family_plans] == [
        ("du_er_1y",),
        ("fd_yr1_ern_r",),
    ]
    assert [item.product_family for item in plan.excluded_families] == [
        ProductFamily.OVERSEAS_ETP
    ]
    assert plan.excluded_families[0].canonical_field == "one_year_return_pct"


def test_multi_family_product_count_compiles_as_typed_aggregate() -> None:
    plan = _engine().compile(
        "국내 ETF와 공모펀드의 상품 수를 상품군별로 알려줘",
        "family-count",
    )

    assert plan is not None
    assert plan.kind == "family_count_aggregate"
    assert plan.interaction_intent.value == "aggregate"
    assert [item.product_family for item in plan.family_plans] == [
        ProductFamily.DOMESTIC_ETP,
        ProductFamily.FUND,
    ]
    assert all(
        len(item.aggregations) == 1
        and item.aggregations[0].function.value == "count"
        for item in plan.family_plans
    )
    assert any(
        constraint.source_field == "prvo_pbff_desc"
        and constraint.operator == "eq"
        and constraint.value == "공모"
        for constraint in plan.family_plans[1].filters
    )


def test_two_stage_requires_first_direction_and_keeps_both_metrics() -> None:
    complete = _engine().compile(
        "국내 ETF 중 순자산이 큰 열 개를 먼저 뽑고 그중 거래대금이 많은 세 개",
        "two-stage-complete",
    )
    incomplete = _engine().compile(
        "우선 거래대금 기준으로 국내 ETF 20개 후보를 만들고, "
        "이 안에서 보수율이 낮은 5개",
        "two-stage-incomplete",
    )

    assert complete is not None
    family = complete.family_plans[0]
    assert complete.kind == "two_stage"
    assert family.shortlist_metric is not None
    assert family.shortlist_metric.source_fields == ("du_last_aum",)
    assert family.shortlist_direction is SortDirection.DESC
    assert family.shortlist_limit == 10
    assert family.metric.source_fields == ("du_val_1d",)
    assert family.direction is SortDirection.DESC
    assert family.limit == 3
    assert incomplete is None


def test_explicit_average_owns_metric_alias_and_routes_to_aggregate() -> None:
    question = (
        "전체 국내채권에 다른 조건을 적용하지 말고 각 범주를 빠짐없이 "
        "나눠서 채권 대분류별 매수수익률 평균을 제공 데이터 기준으로 "
        "정확히 계산해줘."
    )

    decision = IntentRouter().route(question, "grouped-buy-yield-average")
    plan = ServerQueryPlanCompiler({}).compile(decision)

    assert decision.draft.intent.value == "aggregate"
    assert decision.disposition.value == "execute"
    assert plan.intent_payload.group_by == ["bond_major_class"]
    assert [(item.function.value, item.field) for item in plan.intent_payload.aggregations] == [
        ("avg", "buy_yield_pct")
    ]
