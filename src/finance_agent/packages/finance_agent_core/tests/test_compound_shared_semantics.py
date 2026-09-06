from __future__ import annotations

from datetime import date

import pytest

from finance_agent_core.agent.compound_query import (
    CompoundFamilyPlan,
    CompoundFilter,
    CompoundMetric,
    TypedCompoundPlan,
    TypedCompoundQueryEngine,
    _SourceRecord,
    requires_typed_two_stage_plan,
)
from finance_agent_core.agent.linker import build_lexical_hints
from finance_agent_core.agent.semantic_gate import SemanticCoverageGate
from finance_agent_core.contracts.queryplan import ProductFamily, SortDirection
from finance_agent_core.contracts.routing import InteractionIntent


def _engine() -> TypedCompoundQueryEngine:
    return TypedCompoundQueryEngine({}, record_cache=object())  # type: ignore[arg-type]


@pytest.mark.parametrize("coordinator", ["와", " 및 ", "·"])
def test_coordinated_family_suffix_owns_every_compatible_filter(
    coordinator: str,
) -> None:
    question = (
        f"국내 ETF{coordinator}해외 ETF 중 총보수가 0.2% 이상 0.4% 이하인 "
        "상품을 상품군별로 AUM 큰 순서대로 2개씩 보여줘."
    )

    plan = _engine().compile(question, "shared-compatible-filter")

    assert plan is not None
    assert plan.kind == "parallel"
    assert [item.product_family for item in plan.family_plans] == [
        ProductFamily.DOMESTIC_ETP,
        ProductFamily.OVERSEAS_ETP,
    ]
    for family_plan in plan.family_plans:
        assert CompoundFilter(
            "cu_charge_rt",
            "between",
            (0.2, 0.4),
        ) in family_plan.filters
        assert family_plan.metric.source_fields == ("du_last_aum",)
        assert family_plan.direction.value == "desc"
        assert family_plan.limit == 2


@pytest.mark.parametrize(
    ("period", "domestic_source", "fund_source"),
    [
        ("최근 1년", "du_er_1y", "fd_yr1_ern_r"),
        ("최근 3개월", "du_er_3m", "fd_mm3_ern_r"),
        ("최근 6개월", "du_er_6m", "fd_mm6_ern_r"),
    ],
)
def test_shared_threshold_and_ranking_compile_for_each_compatible_family(
    period: str,
    domestic_source: str,
    fund_source: str,
) -> None:
    question = (
        f"국내 ETF와 공모펀드에서 {period} 수익률이 10% 이상인 상품을 "
        "상품군별 수익률 상위 2개씩 비교해줘."
    )

    plan = _engine().compile(question, "shared-return-threshold")

    assert plan is not None
    assert plan.kind == "parallel"
    domestic, fund = plan.family_plans
    assert CompoundFilter(domestic_source, "gte", 10) in domestic.filters
    assert CompoundFilter(fund_source, "gte", 10) in fund.filters
    assert domestic.metric.source_fields == (domestic_source,)
    assert fund.metric.source_fields == (fund_source,)
    assert domestic.limit == fund.limit == 2


def test_incompatible_shared_filter_rejects_the_entire_compound_plan() -> None:
    question = (
        "국내 ETF와 공모펀드에서 총보수 0.4% 이하인 상품을 "
        "상품군별 1년 수익률 상위 2개씩 보여줘."
    )

    assert _engine().compile(question, "incompatible-shared-filter") is None


def test_independent_clauses_never_inherit_the_last_family_filter() -> None:
    question = (
        "국내 ETF는 총보수 0.2% 이하 중 AUM 큰 순 2개, "
        "해외 ETF는 총보수 0.4% 이상 중 AUM 큰 순 2개를 따로 보여줘."
    )

    plan = _engine().compile(question, "independent-filter-ownership")

    assert plan is not None
    domestic, overseas = plan.family_plans
    assert CompoundFilter("cu_charge_rt", "lte", 0.2) in domestic.filters
    assert CompoundFilter("cu_charge_rt", "gte", 0.4) not in domestic.filters
    assert CompoundFilter("cu_charge_rt", "gte", 0.4) in overseas.filters
    assert CompoundFilter("cu_charge_rt", "lte", 0.2) not in overseas.filters


def test_explicit_filter_then_rank_sequence_is_not_a_fake_shortlist() -> None:
    question = (
        "표면금리가 4% 이상이고 만기까지 2년 미만 남은 원화채권을 "
        "먼저 추린 뒤, 클린 평가가격이 낮은 순으로 3개 정리해줘."
    )

    decision = SemanticCoverageGate().evaluate(
        question,
        interaction_intent="search",
    )
    hints = build_lexical_hints(
        question,
        ProductFamily.BOND.value,
        force_product_family_hint=True,
    )

    assert requires_typed_two_stage_plan(question) is False
    assert _engine().compile(question, "filter-then-rank") is None
    assert decision.has_unresolved_semantics is False
    assert hints["ambiguity_spans"] == []
    assert {
        (item["field"], item["operator"], str(item["value"]))
        for item in hints["required_constraints"]
    } >= {
        ("coupon_rate_pct", "gte", "4"),
        ("remaining_days", "lt", "730"),
        ("trading_currency", "eq", "KRW"),
    }
    assert hints["required_rankings"] == [
        {
            "field": "evaluation_price",
            "direction": "asc",
            "nulls": "last",
        }
    ]
    assert hints["limit"] == 3


def test_month_period_is_not_counted_as_a_shortlist_size() -> None:
    question = (
        "국내 ETF 중 3개월 수익률 -11.5 이상, "
        "20거래일 연환산 변동성 42.47 이하로 "
        "거른 뒤 순자산총액 상위 3개를 정리해 주세요."
    )

    assert requires_typed_two_stage_plan(question) is False
    assert _engine().compile(question, "period-not-shortlist") is None
    decision = SemanticCoverageGate().evaluate(question, interaction_intent="search")
    hints = build_lexical_hints(
        question,
        ProductFamily.DOMESTIC_ETP.value,
        force_product_family_hint=True,
    )
    assert decision.has_unresolved_semantics is False
    assert {
        (item["field"], item["operator"], str(item["value"]))
        for item in hints["required_constraints"]
    } >= {
        ("three_month_return_pct", "gte", "-11.5"),
        ("twenty_day_volatility_pct", "lte", "42.47"),
    }
    assert hints["required_rankings"] == [
        {
            "field": "net_asset_total",
            "direction": "desc",
            "nulls": "last",
        }
    ]


@pytest.mark.parametrize(
    "question",
    [
        "국내채권을 먼저 추린 뒤, 클린 평가가격이 낮은 순으로 3개 정리해줘.",
        "원화채권을 먼저 추린 뒤, 클린 평가가격을 정리해줘.",
    ],
)
def test_incomplete_filter_then_rank_sequence_remains_fail_closed(question: str) -> None:
    decision = SemanticCoverageGate().evaluate(question, interaction_intent="search")

    assert "먼저" in decision.ambiguity_spans


def test_real_two_stage_still_requires_two_bounded_result_sizes() -> None:
    question = (
        "국내채권 중 발행금액 상위 50개를 먼저 추린 뒤 "
        "표면금리 높은 순으로 5개 보여줘."
    )

    assert requires_typed_two_stage_plan(question) is True
    plan = _engine().compile(question, "true-two-stage")
    assert plan is not None
    assert plan.kind == "two_stage"
    assert plan.family_plans[0].shortlist_limit == 50
    assert plan.family_plans[0].limit == 5


@pytest.mark.parametrize(
    ("cue", "order_surface", "expected_direction"),
    [
        ("큰", "순", SortDirection.DESC),
        ("높은", "순으로", SortDirection.DESC),
        ("작은", "순서로", SortDirection.ASC),
        ("낮은", "순서대로", SortDirection.ASC),
    ],
)
def test_two_stage_shortlist_accepts_explicit_order_surface_variants(
    cue: str,
    order_surface: str,
    expected_direction: SortDirection,
) -> None:
    question = (
        f"국내 ETF를 최근 거래대금이 {cue} {order_surface} 30개 먼저 추린 다음, "
        "그 30개 안에서 1년 수익률이 높은 순으로 5개를 다시 골라주세요."
    )

    assert requires_typed_two_stage_plan(question) is True
    plan = _engine().compile(question, f"two-stage-{order_surface}")
    assert plan is not None
    assert plan.kind == "two_stage"
    family_plan = plan.family_plans[0]
    assert family_plan.shortlist_limit == 30
    assert family_plan.shortlist_direction is expected_direction
    assert family_plan.limit == 5
    assert family_plan.direction is SortDirection.DESC


def test_two_stage_shortlist_rejects_directionless_first_stage() -> None:
    question = (
        "국내 ETF를 최근 거래대금 기준 30개 먼저 추린 다음, "
        "그 30개 안에서 1년 수익률이 높은 순으로 5개를 다시 골라주세요."
    )

    assert requires_typed_two_stage_plan(question) is True
    assert _engine().compile(question, "directionless-first-stage") is None


def test_two_stage_monthly_average_shortlist_is_fully_preserved() -> None:
    question = (
        "국내 ETF를 1개월 평균 거래대금이 큰 순서로 30개 먼저 추린 다음, "
        "그 30개 안에서 1년 수익률이 높은 5개를 다시 골라주세요. "
        "수익률 자료가 없는 상품은 30개 후보를 정한 다음에 빼주세요."
    )

    assert requires_typed_two_stage_plan(question) is True
    plan = _engine().compile(question, "monthly-average-two-stage")

    assert plan is not None
    family_plan = plan.family_plans[0]
    assert family_plan.shortlist_metric is not None
    assert family_plan.shortlist_metric.source_fields == ("du_val_1m",)
    assert family_plan.shortlist_direction is SortDirection.DESC
    assert family_plan.shortlist_limit == 30
    assert family_plan.metric.source_fields == ("du_er_1y",)
    assert family_plan.direction is SortDirection.DESC
    assert family_plan.limit == 5


def test_secondary_metric_availability_is_applied_after_shortlist(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = _engine()
    records = tuple(
        _SourceRecord(
            product_id=product_id,
            product_name=product_id,
            ticker=product_id,
            source_id="SYNTHETIC-STAGED-SEMANTICS",
            source_row=index,
            dynamic_as_of=date(2026, 8, 22),
            source_values={
                "du_last_aum": aum,
                "du_er_1y": final_return,
            },
        )
        for index, (product_id, aum, final_return) in enumerate(
            (
                ("A", "100", None),
                ("B", "90", "1"),
                ("C", "80", "100"),
            ),
            start=1,
        )
    )
    monkeypatch.setattr(engine, "_load_records", lambda *_args, **_kwargs: records)
    monkeypatch.setattr(engine, "_product_evidence", lambda *_args, **_kwargs: object())
    family_plan = CompoundFamilyPlan(
        product_family=ProductFamily.DOMESTIC_ETP,
        filters=(),
        metric=CompoundMetric("field", ("du_er_1y",)),
        direction=SortDirection.DESC,
        limit=1,
        shortlist_metric=CompoundMetric("field", ("du_last_aum",)),
        shortlist_limit=2,
    )
    compound = TypedCompoundPlan(
        question_id="staged-availability",
        question="synthetic staged semantics",
        interaction_intent=InteractionIntent.SEARCH,
        kind="two_stage",
        family_plans=(family_plan,),
    )

    execution = engine._execute_family(compound, family_plan)

    assert execution.candidate_count == 3
    assert execution.shortlist_ids == ("A", "B")
    assert [item.product_id for item in execution.product_records] == ["B"]
    assert execution.metric_values == (1,)


@pytest.mark.parametrize(
    ("question", "family", "expected_fields"),
    [
        (
            "국내 ETF 중 1년 수익률·60일 변동성이 유효한 상품을 "
            "AUM 순으로 3개 보여줘.",
            ProductFamily.DOMESTIC_ETP.value,
            {"one_year_return_pct", "sixty_day_volatility_pct"},
        ),
        (
            "해외 ETF 중 AUM과 일간 수익률이 있고 총보수 낮은 순으로 "
            "3개 보여줘.",
            ProductFamily.OVERSEAS_ETP.value,
            {"aum", "one_day_return_pct"},
        ),
    ],
)
def test_one_shared_availability_predicate_owns_every_coordinated_field(
    question: str,
    family: str,
    expected_fields: set[str],
) -> None:
    hints = build_lexical_hints(
        question,
        family,
        force_product_family_hint=True,
    )

    present_fields = {
        item["field"]
        for item in hints["required_constraints"]
        if item["operator"] == "is_present"
    }
    assert present_fields == expected_fields
    assert hints["ambiguity_spans"] == []


def test_shared_availability_never_crosses_an_independent_field_predicate() -> None:
    question = (
        "해외 ETF 중 AUM은 10억 이상이고 일간 수익률이 있는 상품을 "
        "총보수 낮은 순으로 3개 보여줘."
    )

    hints = build_lexical_hints(
        question,
        ProductFamily.OVERSEAS_ETP.value,
        force_product_family_hint=True,
    )

    constraints = {
        (item["field"], item["operator"], item["value"])
        for item in hints["required_constraints"]
    }
    assert ("aum", "gte", 1_000_000_000) in constraints
    assert ("aum", "is_present", True) not in constraints
    assert ("one_day_return_pct", "is_present", True) in constraints
