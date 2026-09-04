from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from finance_agent_core.agent.compound_query import (
    CompoundFilter,
    TypedCompoundQueryEngine,
    _merge_clause_filters,
    _SourceRecord,
)
from finance_agent_core.contracts.queryplan import ProductFamily


def _engine() -> TypedCompoundQueryEngine:
    return TypedCompoundQueryEngine({}, record_cache=object())  # type: ignore[arg-type]


def _anchor(values: dict[str, str]) -> _SourceRecord:
    return _SourceRecord(
        product_id="REFERENCE001",
        product_name="가상 기준상품",
        ticker="REFERENCE001",
        source_id="SYNTHETIC-COMPOUND-GENERALIZATION",
        source_row=1,
        dynamic_as_of=date(2026, 8, 22),
        source_values=values,
    )


def test_numeric_exclusion_keeps_an_independent_bound_on_the_same_field() -> None:
    merged = _merge_clause_filters(
        (CompoundFilter("cu_charge_rt", "gte", 0.2),),
        (CompoundFilter("cu_charge_rt", "gt", 0.8),),
        text="연간보수율 0.2% 이상이고 연간보수율 0.8% 초과분은 배제",
        family=ProductFamily.OVERSEAS_ETP,
    )

    assert merged is not None
    assert set(merged) == {
        CompoundFilter("cu_charge_rt", "gte", 0.2),
        CompoundFilter("cu_charge_rt", "lte", 0.8),
    }


def test_anchor_comparison_keeps_every_conjunction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = _engine()
    monkeypatch.setattr(
        engine,
        "_find_record",
        lambda *_args, **_kwargs: _anchor(
            {"cu_charge_rt": "0.4", "du_last_aum": "250"}
        ),
    )
    question = (
        "해외 ETF 기준식별자 REFERENCE001보다 연간보수율은 더 낮고 "
        "AUM은 더욱 큰 다른 상품 1일 수익률 높은 순 4개"
    )

    plan = engine.compile(question, "anchor-conjunction-matrix")

    assert plan is not None
    relative = {
        (metric.source_fields, operator, value)
        for metric, operator, value in plan.family_plans[0].relative_filters
    }
    assert relative == {
        (("cu_charge_rt",), "lt", Decimal("0.4")),
        (("du_last_aum",), "gt", Decimal("250")),
    }


@pytest.mark.parametrize(
    "question",
    [
        (
            "공모펀드 기준식별자 REFERENCE001 기준 "
            "3개월수익률 이상·샤프지수 이하 다른 상품 AUM 작은 순 4개"
        ),
        (
            "해외 ETF 기준식별자 REFERENCE001보다 연간보수율은 낮고 "
            "샤프지수는 높은 다른 상품 AUM 큰 순 4개"
        ),
    ],
)
def test_unknown_anchor_conjunct_refuses_the_whole_plan(
    question: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = _engine()
    monkeypatch.setattr(
        engine,
        "_find_record",
        lambda *_args, **_kwargs: _anchor(
            {
                "fd_mm3_ern_r": "2.1",
                "cu_charge_rt": "0.5",
            }
        ),
    )

    assert engine.compile(question, "unknown-anchor-conjunct") is None


@pytest.mark.parametrize(
    "summary, representatives",
    [
        ("유효 상품 수, 중앙값, 최솟값과 최댓값", "상위 3개"),
        ("유효 상품 수, 단순평균, 최솟값과 최댓값", "상위 4개"),
        ("유효 상품 수, 단순평균, 최솟값과 최댓값", "하위 3개"),
        ("유효 상품 수, 단순평균, 합계, 최솟값과 최댓값", "상위 3개"),
    ],
)
def test_unsupported_cohort_contract_refuses_instead_of_substituting(
    summary: str,
    representatives: str,
) -> None:
    question = (
        "알파 운용 국내 ETF와 베타 운용 국내 ETF의 일거래대금 분포 차이를 "
        f"비교해줘. 각 집단의 {summary}을 계산하고 {representatives}도 보여줘."
    )

    assert _engine().compile(question, "unsupported-cohort-contract") is None


def test_supported_cohort_contract_remains_available() -> None:
    question = (
        "알파 운용 국내 ETF와 베타 운용 국내 ETF의 일거래대금 분포를 "
        "비교해줘. 각 집단의 유효 상품 수, 단순평균, 최솟값과 최댓값을 "
        "계산하고 상위 3개도 보여줘."
    )

    plan = _engine().compile(question, "supported-cohort-contract")

    assert plan is not None
    assert plan.kind == "cohort"


def test_preliminary_stage_without_a_final_ranking_refuses() -> None:
    question = (
        "판매 중 공모펀드에서 AUM 상위 18개를 예선으로 뽑고 "
        "그중 결선 후보를 알려줘"
    )

    assert _engine().compile(question, "incomplete-two-stage") is None


def test_parallel_unknown_explicit_ranking_refuses_all_families() -> None:
    question = (
        "국내 ETF는 AUM 큰 순 3개, 이어서 해외 ETF는 샤프지수 높은 순 2개를 "
        "합치지 말고 따로 보여줘"
    )

    assert _engine().compile(question, "unknown-parallel-ranking") is None


@pytest.mark.parametrize("bond_label", ["원화채권", "원화 채권"])
def test_parallel_accepts_independent_bond_and_fund_rankings(
    bond_label: str,
) -> None:
    question = (
        f"{bond_label}에서 듀레이션 짧은 2개와 "
        "공모펀드에서 AUM 큰 2개를 각각"
    )

    plan = _engine().compile(question, "parallel-won-bond-fund")

    assert plan is not None
    assert plan.kind == "parallel"
    assert [item.product_family for item in plan.family_plans] == [
        ProductFamily.BOND,
        ProductFamily.FUND,
    ]
    assert [item.metric.source_fields for item in plan.family_plans] == [
        ("dur",),
        ("fd_nast_suma",),
    ]
    assert [item.direction.value for item in plan.family_plans] == ["asc", "desc"]
    assert [item.limit for item in plan.family_plans] == [2, 2]


def test_parallel_shared_tail_ranking_is_bound_to_every_compatible_family() -> None:
    question = "국내 ETF와 해외 ETF는 따로 계산해서 총보수 낮은 상품 3개씩"

    plan = _engine().compile(question, "parallel-shared-fee")

    assert plan is not None
    assert plan.kind == "parallel"
    assert [item.product_family for item in plan.family_plans] == [
        ProductFamily.DOMESTIC_ETP,
        ProductFamily.OVERSEAS_ETP,
    ]
    assert [item.metric.source_fields for item in plan.family_plans] == [
        ("cu_charge_rt",),
        ("cu_charge_rt",),
    ]
    assert all(item.direction.value == "asc" for item in plan.family_plans)
    assert all(item.limit == 3 for item in plan.family_plans)


def test_parallel_shared_tail_accepts_object_particle_before_count() -> None:
    question = "국내 ETF와 해외 ETF는 따로 계산해서 총보수 낮은 상품을 3개씩"

    plan = _engine().compile(question, "parallel-shared-fee-object-particle")

    assert plan is not None
    assert plan.kind == "parallel"
    assert [item.product_family for item in plan.family_plans] == [
        ProductFamily.DOMESTIC_ETP,
        ProductFamily.OVERSEAS_ETP,
    ]
    assert [item.metric.source_fields for item in plan.family_plans] == [
        ("cu_charge_rt",),
        ("cu_charge_rt",),
    ]
    assert all(item.direction.value == "asc" for item in plan.family_plans)
    assert all(item.limit == 3 for item in plan.family_plans)


def test_parallel_family_local_metrics_share_one_trailing_count() -> None:
    question = (
        "국내 ETF는 한 달 수익률 낮은 3개, "
        "펀드는 반년 수익률 높은 3개를 따로 보여줘."
    )

    plan = _engine().compile(question, "parallel-local-metrics-shared-count")

    assert plan is not None
    assert plan.kind == "parallel"
    assert [item.product_family for item in plan.family_plans] == [
        ProductFamily.DOMESTIC_ETP,
        ProductFamily.FUND,
    ]
    assert [item.metric.source_fields for item in plan.family_plans] == [
        ("du_er_1m",),
        ("fd_mm6_ern_r",),
    ]
    assert [item.direction.value for item in plan.family_plans] == ["asc", "desc"]
    assert [item.limit for item in plan.family_plans] == [3, 3]


def test_parallel_without_literal_each_is_safe_when_every_clause_is_complete() -> None:
    question = (
        "국내채권은 신용등급 AA 이상 중 표면금리 상위 3개, "
        "국내 ETF는 위험등급 3 이하 중 AUM 상위 3개"
    )

    plan = _engine().compile(question, "parallel-complete-clauses")

    assert plan is not None
    assert plan.kind == "parallel"
    bond, domestic = plan.family_plans
    assert bond.metric.source_fields == ("srfc_irt",)
    assert bond.limit == 3
    assert CompoundFilter(
        "crd_grd",
        "in",
        ("AAA", "AA+", "AA0", "AA-"),
    ) in bond.filters
    assert domestic.metric.source_fields == ("du_last_aum",)
    assert domestic.limit == 3
    assert CompoundFilter(
        "pd_risk_nm",
        "in",
        (
            "매우높은위험(1등급)",
            "높은위험(2등급)",
            "다소높은위험(3등급)",
        ),
    ) in domestic.filters


def test_parallel_refuses_when_only_one_family_owns_the_ranking() -> None:
    question = "국내 ETF는 AUM 큰 3개, 해외 ETF도 따로 3개 보여줘"

    assert _engine().compile(question, "parallel-incomplete-owner") is None


def test_parallel_refuses_a_shared_metric_missing_from_one_family() -> None:
    question = "국내 ETF와 공모펀드는 따로 계산해서 총보수 낮은 상품 3개씩"

    assert _engine().compile(question, "parallel-incompatible-shared-metric") is None


@pytest.mark.parametrize(
    "question",
    [
        "국내 ETF와 해외 ETF를 하나로 모아 AUM 큰 순 3개",
        "국내 ETF와 해외 ETF를 통합해 순자산 상위 5개",
    ],
)
def test_cross_currency_aum_global_ranking_remains_fail_closed(question: str) -> None:
    assert _engine().compile(question, "global-aum-without-fx") is None


def test_parallel_product_name_order_applies_to_all_four_families() -> None:
    question = (
        "국내채권, 국내 ETF, 해외 ETF, 공모펀드를 "
        "각각 상품명 오름차순으로 한 개씩 보여 주세요."
    )

    plan = _engine().compile(question, "parallel-name-order")

    assert plan is not None
    assert plan.kind == "parallel"
    assert [item.product_family for item in plan.family_plans] == [
        ProductFamily.BOND,
        ProductFamily.DOMESTIC_ETP,
        ProductFamily.OVERSEAS_ETP,
        ProductFamily.FUND,
    ]
    assert [item.metric.source_fields for item in plan.family_plans] == [
        ("pd_nm",),
        ("pd_nm",),
        ("pd_nm",),
        ("itm_nm",),
    ]
    assert all(item.direction.value == "asc" for item in plan.family_plans)
    assert all(item.limit == 1 for item in plan.family_plans)


def test_shared_metric_can_precede_kind_split_and_result_count() -> None:
    question = (
        "국내 ETF와 공모펀드에서 3개월 수익률이 높은 상품을 "
        "종류별로 따로 5개씩 찾아 주세요."
    )

    plan = _engine().compile(question, "parallel-shared-metric-before-split")

    assert plan is not None
    assert plan.kind == "parallel"
    assert [item.metric.source_fields for item in plan.family_plans] == [
        ("du_er_3m",),
        ("fd_mm3_ern_r",),
    ]
    assert all(item.direction.value == "desc" for item in plan.family_plans)
    assert all(item.limit == 5 for item in plan.family_plans)


def test_preliminary_wording_without_final_word_compiles_two_stage() -> None:
    question = (
        "공모펀드에서 3개월 수익률 상위 20개를 예선으로 남기고 "
        "판매회사보수 낮은 순으로 세 개 제시해 주세요."
    )

    plan = _engine().compile(question, "two-stage-preliminary-only")

    assert plan is not None
    assert plan.kind == "two_stage"
    family = plan.family_plans[0]
    assert family.shortlist_metric is not None
    assert family.shortlist_metric.source_fields == ("fd_mm3_ern_r",)
    assert family.shortlist_direction.value == "desc"
    assert family.shortlist_limit == 20
    assert family.metric.source_fields == ("sale_co_rwrd_r",)
    assert family.direction.value == "asc"
    assert family.limit == 3
