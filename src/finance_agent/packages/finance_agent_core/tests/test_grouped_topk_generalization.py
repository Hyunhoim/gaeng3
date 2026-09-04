from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from finance_agent_core.agent.compound_query import (
    CompoundFilter,
    TypedCompoundQueryEngine,
    _SourceRecord,
)
from finance_agent_core.agent.routed_service import RoutedFinanceAgent
from finance_agent_core.agent.router import IntentRouter
from finance_agent_core.contracts.queryplan import ProductFamily, SortDirection
from finance_agent_core.contracts.routing import InteractionIntent, RouteDisposition
from finance_agent_core.domain import DatabaseManifest, NormalizedOverseasEtpRecord
from finance_agent_core.storage import RecordSnapshotCache

QUESTIONS = {
    "H007": (
        "채권 대분류(국공채·특수채·회사채)마다 표면금리가 높은 2개씩을 "
        "뽑아, 분류명과 분류 내 순위로 정렬해 주세요."
    ),
    "H008": (
        "3개월 수익률 유효 상품이 20개 이상인 국내 ETP 자산군만 남기고, "
        "각 자산군 상위 2개를 구해 주세요."
    ),
    "H009": (
        "일간 수익률 유효 종목이 10개 이상인 해외 거래소 코드별로 "
        "성과 1·2위를 찾아 거래소 코드순으로 보여줘요."
    ),
    "H010": (
        "펀드의 국내·해외·국내외혼합 구분마다 1년 수익률 상위 두 상품을 "
        "동일률이면 ID순으로 정해 주세요."
    ),
    "H011": (
        "국내·해외 ETP를 가족과 ETF/ETN 조합으로 나눠 "
        "총보수 최저 2개씩을 구해 주세요."
    ),
    "H012": (
        "AUM 유효 상품이 50개 이상인 펀드 운용 속성만 대상으로, "
        "속성별 AUM 1위를 하나씩 알려주세요."
    ),
}


def _engine() -> TypedCompoundQueryEngine:
    return TypedCompoundQueryEngine(
        {family: Path(f"/{family.value}.sqlite3") for family in ProductFamily},
        record_cache=RecordSnapshotCache(),
    )


@pytest.mark.parametrize(
    (
        "case_id",
        "families",
        "group_sources",
        "metric_sources",
        "direction",
        "limit",
        "minimum",
    ),
    [
        (
            "H007",
            (ProductFamily.BOND,),
            ("std_pd_mcls_nm",),
            ("srfc_irt",),
            SortDirection.DESC,
            2,
            None,
        ),
        (
            "H008",
            (ProductFamily.DOMESTIC_ETP,),
            ("wu_inv_ast_type",),
            ("du_er_3m",),
            SortDirection.DESC,
            2,
            20,
        ),
        (
            "H009",
            (ProductFamily.OVERSEAS_ETP,),
            ("pd_exg_mkt_cd",),
            ("du_er_1d",),
            SortDirection.DESC,
            2,
            10,
        ),
        (
            "H010",
            (ProductFamily.FUND,),
            ("ovrs_fd_desc",),
            ("fd_yr1_ern_r",),
            SortDirection.DESC,
            2,
            None,
        ),
        (
            "H011",
            (ProductFamily.DOMESTIC_ETP, ProductFamily.OVERSEAS_ETP),
            ("pd_grp_no", "pd_grp_no"),
            ("cu_charge_rt", "cu_charge_rt"),
            SortDirection.ASC,
            2,
            None,
        ),
        (
            "H012",
            (ProductFamily.FUND,),
            ("or_attr_desc",),
            ("fd_nast_suma",),
            SortDirection.DESC,
            1,
            50,
        ),
    ],
)
def test_grouped_topk_compiles_to_one_generic_partition_contract(
    case_id: str,
    families: tuple[ProductFamily, ...],
    group_sources: tuple[str, ...],
    metric_sources: tuple[str, ...],
    direction: SortDirection,
    limit: int,
    minimum: int | None,
) -> None:
    plan = _engine().compile(QUESTIONS[case_id], case_id)

    assert plan is not None
    assert plan.kind == "grouped_topk"
    assert plan.interaction_intent is InteractionIntent.SEARCH
    assert tuple(item.product_family for item in plan.family_plans) == families
    assert tuple(item.group_by_source_field for item in plan.family_plans) == group_sources
    assert tuple(item.metric.source_fields[0] for item in plan.family_plans) == metric_sources
    assert all(item.direction is direction for item in plan.family_plans)
    assert all(item.limit == limit == item.per_group_limit for item in plan.family_plans)
    assert all(item.group_min_count == minimum for item in plan.family_plans)


def test_explicit_group_enum_list_is_a_domain_filter_not_separate_cohorts() -> None:
    bond = _engine().compile(QUESTIONS["H007"], "group-enum-bond")
    fund = _engine().compile(QUESTIONS["H010"], "group-enum-fund")
    etp = _engine().compile(QUESTIONS["H011"], "group-enum-etp")

    assert bond is not None and fund is not None and etp is not None
    assert bond.family_plans[0].filters == (
        CompoundFilter(
            "std_pd_mcls_nm",
            "in",
            ("국공채", "특수채", "회사채"),
        ),
    )
    assert fund.family_plans[0].filters == (
        CompoundFilter("ovrs_fd_desc", "in", ("국내", "해외", "국내외혼합")),
    )
    assert all(
        item.filters
        == (CompoundFilter("pd_grp_no", "in", ("ETF", "ETN")),)
        for item in etp.family_plans
    )


def _record(
    product_id: str,
    group: str,
    value: str | None,
) -> _SourceRecord:
    return _SourceRecord(
        product_id=product_id,
        product_name=product_id,
        ticker=product_id,
        source_id="PREF01N001",
        source_row=int(product_id.removeprefix("P")),
        dynamic_as_of=date(2026, 8, 22),
        source_values={
            "pd_itm_no": product_id,
            "wu_inv_ast_type": group,
            "du_er_3m": value,
        },
    )


def test_grouped_topk_having_counts_valid_values_and_ranks_inside_each_group(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    question = (
        "3개월 수익률 유효 상품이 2개 이상인 국내 ETP 자산군만 남기고, "
        "각 자산군 상위 2개를 구해 주세요."
    )
    engine = _engine()
    records = (
        _record("P1", "주식", "5"),
        _record("P2", "주식", "5"),
        _record("P3", "주식", "0"),
        _record("P4", "채권", "9"),
        _record("P5", "원자재", "2"),
        _record("P6", "원자재", "7"),
        _record("P7", "원자재", "4"),
    )
    monkeypatch.setattr(engine, "_load_records", lambda *_args, **_kwargs: records)
    plan = engine.compile(question, "grouped-topk-runtime")

    assert plan is not None
    execution = engine.execute(plan).families[0]

    assert execution.candidate_count == 5
    assert execution.grouped_product_valid_counts == (("원자재", 3), ("주식", 2))
    assert execution.grouped_product_values == (
        "원자재",
        "원자재",
        "주식",
        "주식",
    )
    # Equal metric values use ascending product ID; each group is independently ranked.
    assert [item.product_id for item in execution.products] == ["P6", "P7", "P1", "P2"]


def test_grouped_topk_does_not_choose_between_ambiguous_metrics() -> None:
    question = (
        "국내 ETP 자산군마다 AUM과 총보수를 기준으로 "
        "각 자산군 상위 2개를 알려 주세요."
    )

    assert _engine().compile(question, "grouped-topk-ambiguous") is None


def test_group_aggregate_topk_remains_an_aggregate_row_order() -> None:
    question = (
        "총보수 유효 국내 ETP가 5개 이상인 운용사만 대상으로 평균 총보수를 "
        "내고, 평균이 낮은 순으로 최대 8곳을 주세요."
    )
    plan = _engine().compile(question, "aggregate-not-product-topk")

    assert plan is not None
    assert plan.kind == "grouped_aggregate"


def test_grouped_topk_closes_only_the_exact_router_representation_gap() -> None:
    engine = _engine()
    trace = IntentRouter().route_with_planning(QUESTIONS["H009"], "grouped-gap")
    plan = engine.compile(QUESTIONS["H009"], "grouped-gap")

    assert plan is not None
    assert trace.route_decision.disposition is RouteDisposition.CLARIFY
    authorized, receipt = engine.authorize_decision(
        trace.route_decision,
        plan,
        unresolved_spans=trace.planning_decision.unresolved_spans,
    )

    assert authorized.disposition is RouteDisposition.EXECUTE
    assert authorized.draft.intent is InteractionIntent.SEARCH
    assert receipt is not None


def test_grouped_topk_uses_routed_runtime_and_preserves_per_group_order(
    sample_database: tuple[
        Path,
        list[NormalizedOverseasEtpRecord],
        DatabaseManifest,
    ],
) -> None:
    agent = RoutedFinanceAgent({"overseas_etp": sample_database[0]})

    result = agent.answer(
        "해외 ETP 자산군마다 AUM이 높은 2개씩 알려 주세요.",
        "grouped-topk-routed",
    )

    assert result.status == "executed"
    assert result.candidate_count == 11
    assert [item.product_id for item in result.products] == ["Z0", "H1"]
    assert "[Bond] 유효 순위 후보 11개" in result.answer
