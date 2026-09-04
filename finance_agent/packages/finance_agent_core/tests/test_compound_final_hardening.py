from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from finance_agent_core.agent.compound_query import (
    CompoundFamilyPlan,
    CompoundMetric,
    TypedCompoundPlan,
    TypedCompoundQueryEngine,
    _raw_value,
    _SourceRecord,
)
from finance_agent_core.agent.linker import build_lexical_hints
from finance_agent_core.contracts.backend import InteractionIntent
from finance_agent_core.contracts.queryplan import (
    AggregateFunction,
    Intent,
    ProductFamily,
    SortDirection,
)


def _engine() -> TypedCompoundQueryEngine:
    return TypedCompoundQueryEngine({}, record_cache=object())  # type: ignore[arg-type]


def _record(
    product_id: str,
    *,
    ticker: str | None = None,
    values: dict[str, str | int | bool | None],
) -> _SourceRecord:
    digits = "".join(character for character in product_id if character.isdigit())
    return _SourceRecord(
        product_id=product_id,
        product_name=f"가상상품-{product_id}",
        ticker=ticker,
        source_id="SYNTHETIC-COMPOUND-FINAL",
        source_row=int(digits[-6:] or "1"),
        dynamic_as_of=date(2026, 8, 22),
        source_values=values,
    )


def _filter_triples(plan: object) -> set[tuple[str, str, object]]:
    return {
        (item.source_field, item.operator, item.value)
        for item in plan.filters  # type: ignore[attr-defined]
    }


def test_remaining_days_uses_normalized_column_not_stale_source_json() -> None:
    record = _SourceRecord(
        product_id="KR6000000001:장외:20260822:1",
        product_name="가상 만기 경과 채권",
        ticker="KR6000000001",
        source_id="SYNTHETIC-COMPOUND-FINAL",
        source_row=1,
        dynamic_as_of=date(2026, 8, 22),
        source_values={"remaining_days": 0},
        remaining_days=-57,
    )

    assert _raw_value(record, "remaining_days") == -57


def test_preliminary_and_final_wording_compiles_two_stage_fee_sum() -> None:
    question = (
        "판매 중 공모펀드 중 3개월 수익률 0% 이상에서 AUM 상위 "
        "18개를 예선으로 뽑고 판매·운용·신탁·일반사무관리 보수 합계 "
        "낮은 순 4개를 결선에 올려줘"
    )

    plan = _engine().compile(question, "final-two-stage")

    assert plan is not None
    assert plan.kind == "two_stage"
    family = plan.family_plans[0]
    assert family.product_family is ProductFamily.FUND
    assert family.shortlist_metric is not None
    assert family.shortlist_metric.source_fields == ("fd_nast_suma",)
    assert family.shortlist_limit == 18
    assert family.shortlist_direction is SortDirection.DESC
    assert family.metric.kind == "add"
    assert family.metric.source_fields == (
        "sale_co_rwrd_r",
        "or_co_rwrd_r",
        "trusc_rwrd_r",
        "ofwk_trus_rwrd_r",
    )
    assert family.direction is SortDirection.ASC
    assert family.limit == 4
    assert ("sale_yn", "eq", "판매중") in _filter_triples(family)
    assert ("fd_mm3_ern_r", "gte", 0) in _filter_triples(family)


def test_then_separator_keeps_two_families_and_local_conditions() -> None:
    question = (
        "국내채권 듀레이션 2년 미만 민평금리 내림차순 2개. "
        "이어서 판매회사보수 1% 미만 공모펀드 1년 수익률 "
        "오름차순 3개, 따로 보여줘."
    )

    plan = _engine().compile(question, "final-then-parallel")

    assert plan is not None
    assert plan.kind == "parallel"
    bond, fund = plan.family_plans
    assert bond.product_family is ProductFamily.BOND
    assert bond.metric.source_fields == ("applied_yield",)
    assert bond.direction is SortDirection.DESC
    assert bond.limit == 2
    assert ("dur", "lt", 2) in _filter_triples(bond)
    assert fund.product_family is ProductFamily.FUND
    assert fund.metric.source_fields == ("fd_yr1_ern_r",)
    assert fund.direction is SortDirection.ASC
    assert fund.limit == 3
    assert ("sale_co_rwrd_r", "lt", 1) in _filter_triples(fund)


def test_parallel_domestic_and_overseas_discrepancy_uses_each_official_field() -> None:
    plan = _engine().compile(
        "국내 ETF와 해외 ETF를 각각 괴리율 낮은 순 2개씩 보여줘",
        "final-parallel-discrepancy",
    )

    assert plan is not None
    assert plan.kind == "parallel"
    assert [item.product_family for item in plan.family_plans] == [
        ProductFamily.DOMESTIC_ETP,
        ProductFamily.OVERSEAS_ETP,
    ]
    assert all(item.metric.source_fields == ("du_diff_rt",) for item in plan.family_plans)
    assert all(item.direction is SortDirection.ASC for item in plan.family_plans)
    assert all(item.limit == 2 for item in plan.family_plans)


@pytest.mark.parametrize(
    ("question", "filter_field", "operator", "value", "rank_field"),
    [
        (
            "판매회사 보수 1% 이하 공모펀드를 1년 수익률 높은 순 3개 보여줘",
            "sale_co_rwrd_r",
            "lte",
            1,
            "fd_yr1_ern_r",
        ),
        (
            "원본 보존 비율 70% 이상 공모펀드를 AUM 높은 순 3개 보여줘",
            "fd_prsv_r",
            "gte",
            70,
            "fd_nast_suma",
        ),
    ],
)
def test_new_numeric_filter_owns_its_value_without_poisoning_rank(
    question: str,
    filter_field: str,
    operator: str,
    value: int,
    rank_field: str,
) -> None:
    hints = build_lexical_hints(question, "fund", force_product_family_hint=True)

    assert (filter_field, operator, value) in {
        (
            {
                "sale_company_fee_pct": "sale_co_rwrd_r",
                "preservation_ratio_pct": "fd_prsv_r",
            }.get(item["field"], item["field"]),
            item["operator"],
            item["value"],
        )
        for item in hints["required_constraints"]
    }
    assert hints["required_rankings"] == [
        {
            "field": {
                "fd_yr1_ern_r": "one_year_return_pct",
                "fd_nast_suma": "aum",
            }[rank_field],
            "direction": "desc",
            "nulls": "last",
        }
    ]
    assert hints["ambiguity_spans"] == []


@pytest.mark.parametrize(
    ("question", "field"),
    [
        ("국내채권을 적용수익률 높은 순 3개 보여줘", "applied_yield"),
        ("국내 ETF를 연초 대비 수익률 높은 순 3개 보여줘", "du_er_ytd"),
        ("국내 ETF를 분배수익률 높은 순 3개 보여줘", "pd_dvid_yield"),
        ("공모펀드를 최근 분배율 높은 순 3개 보여줘", "fd_last_dstb_r"),
    ],
)
def test_explicit_new_metric_aliases_compile_without_dense_guessing(
    question: str,
    field: str,
) -> None:
    family = "bond" if "채권" in question else "fund" if "펀드" in question else "domestic_etp"
    hints = build_lexical_hints(question, family, force_product_family_hint=True)
    canonical = {
        "applied_yield": "applied_yield_pct",
        "du_er_ytd": "ytd_return_pct",
        "pd_dvid_yield": "distribution_yield_pct",
        "fd_last_dstb_r": "latest_distribution_pct",
    }[field]

    assert hints["required_rankings"] == [
        {"field": canonical, "direction": "desc", "nulls": "last"}
    ]
    assert hints["ambiguity_spans"] == []


def test_bare_jung_transition_keeps_two_stage_shortlist() -> None:
    plan = _engine().compile(
        "국내 ETF AUM 상위 40개 중 일 거래대금 높은 순 5개 보여줘",
        "final-bare-jung-shortlist",
    )

    assert plan is not None
    assert plan.kind == "two_stage"
    family = plan.family_plans[0]
    assert family.shortlist_metric is not None
    assert family.shortlist_metric.source_fields == ("du_last_aum",)
    assert family.shortlist_limit == 40
    assert family.metric.source_fields == ("du_val_1d",)
    assert family.limit == 5


def test_gather_together_wording_means_one_global_top_k() -> None:
    question = (
        "국내 ETF와 공모펀드 후보를 한데 모아 "
        "3개월 수익률 오름차순 7개만 정리해줘"
    )

    plan = _engine().compile(question, "final-global-gather")

    assert plan is not None
    assert plan.kind == "global_ranking"
    assert [item.product_family for item in plan.family_plans] == [
        ProductFamily.DOMESTIC_ETP,
        ProductFamily.FUND,
    ]
    assert [item.metric.source_fields for item in plan.family_plans] == [
        ("du_er_3m",),
        ("fd_mm3_ern_r",),
    ]
    assert all(item.limit == 7 for item in plan.family_plans)
    assert all(item.direction is SortDirection.ASC for item in plan.family_plans)


def test_one_table_direct_ranking_requires_complete_global_rank_spec() -> None:
    """A global comparison without direction/count must not become two searches."""

    plan = _engine().compile(
        "국내 ETF와 공모펀드를 한 표에서 1년 수익률로 직접 순위를 매겨줘.",
        "final-incomplete-global-ranking",
    )

    assert plan is None


def test_one_table_direct_ranking_compiles_one_global_top_k() -> None:
    plan = _engine().compile(
        "국내 ETF와 공모펀드를 한 표에서 1년 수익률 높은 순으로 "
        "상위 5개 직접 순위를 매겨줘.",
        "final-complete-global-ranking",
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
    assert all(item.limit == 5 for item in plan.family_plans)
    assert all(item.direction is SortDirection.DESC for item in plan.family_plans)


def test_remove_leverage_and_suspension_compiles_complements() -> None:
    question = (
        "레버리지형과 거래 정지 종목은 걷어내고 "
        "국내 ETF AUM 큰 순 5개"
    )

    plan = _engine().compile(question, "final-exclusion-complements")

    assert plan is not None
    assert plan.kind == "bounded"
    filters = _filter_triples(plan.family_plans[0])
    assert ("cu_lev_fector", "lte", 1) in filters
    assert ("pd_tr_yn", "eq", "0") in filters
    assert not any(field == "pd_tr_yn" and value != "0" for field, _operator, value in filters)


def test_excluded_numeric_range_is_inverted_before_execution() -> None:
    question = (
        "해외 ETF 중 연간보수율 0.8% 초과분은 배제하고 "
        "1일 수익률 -1.5% 이상, AUM 내림차순 5개"
    )

    plan = _engine().compile(question, "final-excluded-range")

    assert plan is not None
    family = plan.family_plans[0]
    assert family.product_family is ProductFamily.OVERSEAS_ETP
    filters = _filter_triples(family)
    assert ("cu_charge_rt", "lte", 0.8) in filters
    assert ("du_er_1d", "gte", -1.5) in filters
    assert family.metric.source_fields == ("du_last_aum",)


def test_risk_level_group_request_executes_count_sum_and_average(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    question = (
        "판매 중 공모펀드를 위험등급마다 나눠 "
        "그룹별 개수/AUM 합계/1년수익률 평균을 알려줘"
    )
    engine = _engine()
    records = (
        _record(
            "FUND000001",
            values={
                "prvo_pbff_desc": "공모",
                "sale_yn": "판매중",
                "zrin_fd_ivst_risk_gcd": "1",
                "itm_no": "FUND000001",
                "fd_nast_suma": "100",
                "fd_yr1_ern_r": "8",
            },
        ),
        _record(
            "FUND000002",
            values={
                "prvo_pbff_desc": "공모",
                "sale_yn": "판매중",
                "zrin_fd_ivst_risk_gcd": "1",
                "itm_no": "FUND000002",
                "fd_nast_suma": "50",
                "fd_yr1_ern_r": "4",
            },
        ),
        _record(
            "FUND000003",
            values={
                "prvo_pbff_desc": "공모",
                "sale_yn": "판매중",
                "zrin_fd_ivst_risk_gcd": "2",
                "itm_no": "FUND000003",
                "fd_nast_suma": "25",
                "fd_yr1_ern_r": "0",
            },
        ),
    )
    monkeypatch.setattr(engine, "_load_records", lambda *_args, **_kwargs: records)

    plan = engine.compile(question, "final-grouped-aggregate")
    assert plan is not None
    assert plan.kind == "grouped_aggregate"
    family = plan.family_plans[0]
    assert [item.function for item in family.aggregations] == [
        AggregateFunction.COUNT,
        AggregateFunction.SUM,
        AggregateFunction.AVG,
    ]
    execution = engine.execute(plan)

    assert execution.families[0].query_plan.intent is Intent.AGGREGATE
    assert execution.families[0].query_plan.intent_payload.group_by == ["risk_level"]
    groups = {item.group_value: item for item in execution.families[0].grouped_aggregates}
    high = groups["매우높은위험(1등급)"]
    assert [item.value for item in high.aggregates] == [
        2,
        Decimal(150),
        Decimal(6),
    ]
    # A published numeric zero is unavailable for metric aggregation under the
    # organizer's fixed rule; the group remains visible and reports no mean.
    higher = groups["높은위험(2등급)"]
    assert [item.value for item in higher.aggregates] == [1, Decimal(25), None]


def test_reference_bond_pronoun_keeps_both_relative_metrics(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    question = (
        "장내채권 기준식별자 KR1234567890의 민평금리를 웃돌고 "
        "그 기준채권보다 듀레이션 짧은 다른 종목 "
        "발행잔액 큰 순 5개"
    )
    anchor = _record(
        "KR1234567890",
        ticker="KR1234567890",
        values={"applied_yield": "4.2", "dur": "2.5"},
    )
    engine = _engine()
    monkeypatch.setattr(engine, "_find_record", lambda *_args, **_kwargs: anchor)

    plan = engine.compile(question, "final-bond-anchor-pronoun")

    assert plan is not None
    family = plan.family_plans[0]
    assert family.exclude_product_ids == ("KR1234567890",)
    assert {
        (metric.source_fields, operator, value)
        for metric, operator, value in family.relative_filters
    } == {
        (("applied_yield",), "gt", Decimal("4.2")),
        (("dur",), "lt", Decimal("2.5")),
    }
    assert family.metric.source_fields == ("isu_bal_amt",)
    assert family.limit == 5


def test_reference_fund_threshold_list_uses_anchor_values(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    question = (
        "공모펀드 기준식별자 FUND000001 기준 "
        "3개월수익률 이상·판매회사보수 이하 다른 상품 "
        "AUM 작은 순 4개"
    )
    anchor = _record(
        "FUND000001",
        values={"fd_mm3_ern_r": "3.1", "sale_co_rwrd_r": "0.7"},
    )
    engine = _engine()
    monkeypatch.setattr(engine, "_find_record", lambda *_args, **_kwargs: anchor)

    plan = engine.compile(question, "final-fund-anchor-list")

    assert plan is not None
    family = plan.family_plans[0]
    assert family.exclude_product_ids == ("FUND000001",)
    assert {
        (metric.source_fields, operator, value)
        for metric, operator, value in family.relative_filters
    } == {
        (("fd_mm3_ern_r",), "gte", Decimal("3.1")),
        (("sale_co_rwrd_r",), "lte", Decimal("0.7")),
    }
    assert family.metric.source_fields == ("fd_nast_suma",)
    assert family.direction is SortDirection.ASC
    assert family.limit == 4


def test_duplicate_public_identity_uses_first_eligible_ranking_row(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unavailable first source row must not erase a later valid row."""

    engine = _engine()
    records = (
        _record(
            "KR0000000001:장외:001",
            ticker="KR0000000001",
            values={"buy_yield": "0"},
        ),
        _record(
            "KR0000000001:장내:002",
            ticker="KR0000000001",
            values={"buy_yield": "5.4"},
        ),
        _record(
            "KR0000000002:장내:003",
            ticker="KR0000000002",
            values={"buy_yield": "5.1"},
        ),
    )
    monkeypatch.setattr(engine, "_load_records", lambda *_args, **_kwargs: records)
    family_plan = CompoundFamilyPlan(
        product_family=ProductFamily.BOND,
        filters=(),
        metric=CompoundMetric("field", ("buy_yield",)),
        direction=SortDirection.DESC,
        limit=2,
    )
    plan = TypedCompoundPlan(
        question_id="eligible-dedup",
        question="국내채권 매수수익률 높은 순 2개",
        interaction_intent=InteractionIntent.SEARCH,
        kind="bounded",
        family_plans=(family_plan,),
    )

    execution = engine.execute(plan).families[0]

    assert execution.candidate_count == 2
    assert [record.ticker for record in execution.product_records] == [
        "KR0000000001",
        "KR0000000002",
    ]
    assert execution.metric_values == (Decimal("5.4"), Decimal("5.1"))


def test_parallel_family_local_metric_wins_over_coordinated_prefix() -> None:
    """A first-family metric must never leak into the next family clause."""

    plan = _engine().compile(
        "순자산 상위 공모펀드 2개와 하루 거래대금 상위 국내 ETF 2개를 "
        "따로 조회해 주세요.",
        "parallel-local-metric",
    )

    assert plan is not None
    assert plan.kind == "parallel"
    assert [family.product_family for family in plan.family_plans] == [
        ProductFamily.FUND,
        ProductFamily.DOMESTIC_ETP,
    ]
    assert [family.metric.source_fields for family in plan.family_plans] == [
        ("fd_nast_suma",),
        ("du_val_1d",),
    ]
    assert [family.limit for family in plan.family_plans] == [2, 2]
