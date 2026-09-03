from __future__ import annotations

from pathlib import Path

from finance_agent_core.agent.compound_query import TypedCompoundQueryEngine
from finance_agent_core.contracts.queryplan import (
    AggregateFunction,
    Intent,
    ProductFamily,
    SortDirection,
)
from finance_agent_core.contracts.routing import (
    InteractionIntent,
    MinimalQueryDraft,
    RouteDecision,
    RouteDisposition,
)


def _engine() -> TypedCompoundQueryEngine:
    return TypedCompoundQueryEngine({}, record_cache=object())  # type: ignore[arg-type]


def test_all_family_count_expands_to_the_four_approved_datasets() -> None:
    plan = _engine().compile(
        "네 상품군에서 이름에 '글로벌'이 들어간 상품 수를 상품군별로 세어 줘.",
        "all-family-count",
    )

    assert plan is not None
    assert plan.kind == "family_count_aggregate"
    assert set(plan.product_families) == set(ProductFamily)
    assert all(
        family_plan.aggregations[0].function is AggregateFunction.COUNT
        for family_plan in plan.family_plans
    )
    assert all(
        any(
            item.source_field in {"pd_nm", "itm_nm"}
            and item.operator == "contains"
            and item.value == "글로벌"
            for item in family_plan.filters
        )
        for family_plan in plan.family_plans
    )


def test_family_summary_uses_the_same_canonical_metric_per_family() -> None:
    plan = _engine().compile(
        "원화 국내 ETP와 원화 펀드의 상품군별 평균 1개월 수익률을 비교해 줘.",
        "family-summary-average",
    )

    assert plan is not None
    assert plan.kind == "family_summary_aggregate"
    assert [item.product_family for item in plan.family_plans] == [
        ProductFamily.DOMESTIC_ETP,
        ProductFamily.FUND,
    ]
    assert [item.metric.source_fields for item in plan.family_plans] == [
        ("du_er_1m",),
        ("fd_mm1_ern_r",),
    ]
    assert all(
        item.aggregations[0].function is AggregateFunction.AVG
        for item in plan.family_plans
    )


def test_global_top_k_preserves_shared_filters_for_every_family() -> None:
    plan = _engine().compile(
        "국내 ETP와 펀드를 같이 찾아서 원화·판매 중·3개월 수익률 5% "
        "초과인 상품을 수익률 높은 순으로 8개 보여줘.",
        "shared-filter-global-top-k",
    )

    assert plan is not None
    assert plan.kind == "global_ranking"
    assert [item.metric.source_fields for item in plan.family_plans] == [
        ("du_er_3m",),
        ("fd_mm3_ern_r",),
    ]
    assert all(item.limit == 8 for item in plan.family_plans)
    assert all(
        any(item.operator == "gt" and item.value == 5 for item in family.filters)
        for family in plan.family_plans
    )


def test_product_name_literal_is_not_reinterpreted_as_a_region_filter() -> None:
    plan = _engine().compile(
        "네 상품군 전체에서 이름에 '미국'이 포함된 상품을 이름순으로 10개 찾아줘.",
        "product-name-span-ownership",
    )

    assert plan is not None
    assert plan.kind == "global_ranking"
    assert len(plan.family_plans) == 4
    assert all(item.metric.source_fields for item in plan.family_plans)
    for family_plan in plan.family_plans:
        assert any(
            item.operator == "contains" and item.value == "미국"
            for item in family_plan.filters
        )
        assert not any(
            item.source_field in {"ivs_regn_cd", "fd_ivst_area_cd"}
            for item in family_plan.filters
        )


def test_cohort_threshold_reuses_only_an_explicit_compatible_metric() -> None:
    plan = _engine().compile(
        "국공채 중 잔존일수 중앙값보다 만기가 가까운 채권을 6개 보여줘.",
        "bond-median-threshold",
    )

    assert plan is not None
    assert plan.kind == "aggregate_threshold"
    family_plan = plan.family_plans[0]
    assert family_plan.product_family is ProductFamily.BOND
    assert family_plan.metric.source_fields == ("remaining_days",)
    assert family_plan.aggregate_threshold is not None
    assert family_plan.aggregate_threshold.metric.source_fields == ("remaining_days",)
    assert family_plan.aggregate_threshold.function is AggregateFunction.MEDIAN
    assert family_plan.aggregate_threshold.operator == "lt"
    assert family_plan.limit == 6


def test_cross_currency_global_amount_ranking_fails_closed() -> None:
    assert (
        _engine().compile(
            "국내 ETF와 해외 ETF를 같이 찾아서 AUM 큰 순으로 5개 보여줘.",
            "mixed-currency-global-ranking",
        )
        is None
    )


def test_unknown_cohort_metric_fails_closed() -> None:
    assert (
        _engine().compile(
            "해외 ETF에서 인기점수 평균보다 인기 있는 상품을 5개 보여줘.",
            "unsupported-cohort-metric",
        )
        is None
    )


def test_all_family_phrase_can_close_only_the_typed_router_family_gap() -> None:
    question = "네 상품군에서 이름에 '글로벌'이 들어간 상품 수를 상품군별로 세어 줘."
    engine = TypedCompoundQueryEngine(
        {family: Path(f"/approved/{family.value}.sqlite3") for family in ProductFamily},
        record_cache=object(),  # type: ignore[arg-type]
    )
    plan = engine.compile(question, "all-family-authority")
    assert plan is not None
    routed = RouteDecision(
        draft=MinimalQueryDraft(
            request_id="all-family-authority",
            question=question,
            intent=InteractionIntent.CLARIFY,
            product_families=[],
            product_mentions=[],
        ),
        disposition=RouteDisposition.CLARIFY,
        reason_code="aggregate_product_identity_unbound",
        reason="quoted name literal was not yet bound",
        query_plan_intent=None,
        capability_matrix_version="1.0",
    )

    authorized, receipt = engine.authorize_decision(routed, plan)

    assert authorized.disposition is RouteDisposition.EXECUTE
    assert authorized.draft.intent is InteractionIntent.AGGREGATE
    assert set(authorized.draft.product_families) == set(ProductFamily)
    assert authorized.query_plan_intent is Intent.AGGREGATE
    assert receipt is not None


def test_aggregate_threshold_route_is_normalized_to_a_search_only_after_compile() -> None:
    question = "국공채 중 잔존일수 중앙값보다 만기가 가까운 채권을 6개 보여줘."
    engine = TypedCompoundQueryEngine(
        {ProductFamily.BOND: Path("/approved/bond.sqlite3")},
        record_cache=object(),  # type: ignore[arg-type]
    )
    plan = engine.compile(question, "threshold-route-normalization")
    assert plan is not None
    routed = RouteDecision(
        draft=MinimalQueryDraft(
            request_id="threshold-route-normalization",
            question=question,
            intent=InteractionIntent.AGGREGATE,
            product_families=[ProductFamily.BOND],
            product_mentions=[],
            requested_limit=6,
        ),
        disposition=RouteDisposition.EXECUTE,
        reason_code="capability_executable",
        reason="aggregate words were observed",
        query_plan_intent=Intent.AGGREGATE,
        capability_matrix_version="1.0",
    )

    authorized, receipt = engine.authorize_decision(routed, plan)

    assert authorized.disposition is RouteDisposition.EXECUTE
    assert authorized.draft.intent is InteractionIntent.SEARCH
    assert authorized.query_plan_intent is Intent.SEARCH
    assert authorized.reason_code == "typed_aggregate_threshold_authorized"
    assert receipt is not None


def test_explicit_issuer_cohort_and_low_price_order_are_preserved() -> None:
    issuer = _engine().compile(
        "한국주택금융공사 채권 중 그 발행사 평균보다 평가가격이 높은 것을 "
        "평가가격 낮은 순으로 5개 보여줘.",
        "issuer-cohort",
    )
    low_price = _engine().compile(
        "해외 원자재형 ETP 중 같은 자산유형 평균보다 종가가 낮은 상품을 "
        "종가 낮은 순으로 6개 보여줘.",
        "low-price-cohort",
    )

    assert issuer is not None
    assert issuer.kind == "aggregate_threshold"
    assert [
        (item.source_field, item.operator, item.value)
        for item in issuer.family_plans[0].filters
    ] == [("pd_pbcm", "eq", "한국주택금융공사")]
    assert low_price is not None
    assert low_price.family_plans[0].metric.source_fields == ("du_clpr",)
    assert low_price.family_plans[0].direction is SortDirection.ASC


def test_shared_product_name_literal_is_applied_to_every_parallel_family() -> None:
    plan = _engine().compile(
        "상품명에 '인도'가 들어간 국내 ETP와 공모펀드를 각각 상품명 "
        "오름차순 5개씩, ETP 다음 펀드 순으로 보여줘.",
        "parallel-shared-name",
    )

    assert plan is not None
    assert plan.kind == "parallel"
    assert all(
        any(
            item.operator == "contains" and item.value == "인도"
            for item in family.filters
        )
        for family in plan.family_plans
    )


def test_group_dimension_does_not_become_an_affirmative_row_filter() -> None:
    plan = _engine().compile(
        "국내 ETP, 해외 ETP, 공모펀드의 판매가능 여부별 상품 수를 "
        "상품군 순서와 판매불가·판매가능 순으로 모두 집계해줘.",
        "sellable-groups",
    )

    assert plan is not None
    assert plan.kind == "family_count_aggregate"
    assert all(item.group_by_source_field is not None for item in plan.family_plans)
    assert all(
        not any(
            condition.source_field == family.group_by_source_field
            for condition in family.filters
        )
        for family in plan.family_plans
    )


def test_row_field_gap_requires_and_preserves_both_numeric_fields() -> None:
    plan = _engine().compile(
        "6개월 수익률이 1개월 수익률보다 높은 국내 ETP를 두 수익률 "
        "차이가 큰 순으로 7개 보여줘.",
        "row-field-gap",
    )

    assert plan is not None
    assert plan.kind == "field_relative_ranking"
    assert plan.family_plans[0].field_comparisons == (
        ("du_er_6m", "gt", "du_er_1m"),
    )
    assert plan.family_plans[0].metric.source_fields == ("du_er_6m", "du_er_1m")
