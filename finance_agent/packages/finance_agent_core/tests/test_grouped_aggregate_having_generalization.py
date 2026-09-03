from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from finance_agent_core.agent.compound_query import (
    CompoundAggregation,
    TypedCompoundQueryEngine,
    _SourceRecord,
)
from finance_agent_core.agent.routed_service import RoutedFinanceAgent
from finance_agent_core.agent.router import IntentRouter
from finance_agent_core.contracts.queryplan import (
    AggregateFunction,
    ProductFamily,
    SortDirection,
)
from finance_agent_core.contracts.routing import InteractionIntent, RouteDisposition
from finance_agent_core.domain import DatabaseManifest, NormalizedOverseasEtpRecord
from finance_agent_core.storage import RecordSnapshotCache


def _engine() -> TypedCompoundQueryEngine:
    return TypedCompoundQueryEngine(
        {family: Path(f"/{family.value}.sqlite3") for family in ProductFamily},
        record_cache=RecordSnapshotCache(),
    )


@pytest.mark.parametrize(
    (
        "question",
        "family",
        "group_source",
        "metric_source",
        "minimum",
        "direction",
        "limit",
    ),
    [
        (
            "표면금리 유효 채권이 100개 이상인 세부분류만 남겨 "
            "평균 표면금리를 구하고, 평균이 높은 6개 분류를 보여주세요.",
            ProductFamily.BOND,
            "std_pd_scls_nm",
            "srfc_irt",
            100,
            SortDirection.DESC,
            6,
        ),
        (
            "총보수 유효 국내 ETP가 5개 이상인 운용사만 대상으로 "
            "평균 총보수를 내고, 평균이 낮은 순으로 최대 8곳을 주세요.",
            ProductFamily.DOMESTIC_ETP,
            "cu_fund_mgmt_co",
            "cu_charge_rt",
            5,
            SortDirection.ASC,
            8,
        ),
        (
            "해외 ETP 일간 수익률 표본이 20개 이상인 운용사별 평균을 "
            "구해 상위 8개 운용사를 순서대로 알려 주세요.",
            ProductFamily.OVERSEAS_ETP,
            "cu_fund_mgmt_co",
            "du_er_1d",
            20,
            SortDirection.DESC,
            8,
        ),
        (
            "해외주식 편입비 유효 펀드가 50개 이상인 투자지역만 남겨 "
            "평균 편입비가 큰 지역 8개를 구해 주세요.",
            ProductFamily.FUND,
            "fd_ivst_rgn_desc",
            "zrin_ovrs_stk_cmst_rt",
            50,
            SortDirection.DESC,
            8,
        ),
    ],
)
def test_group_having_plan_owns_valid_metric_and_aggregate_order(
    question: str,
    family: ProductFamily,
    group_source: str,
    metric_source: str,
    minimum: int,
    direction: SortDirection,
    limit: int,
) -> None:
    engine = _engine()
    plan = engine.compile(question, f"group-having-{family.value}")

    assert plan is not None
    assert plan.kind == "grouped_aggregate"
    family_plan = plan.family_plans[0]
    assert family_plan.product_family is family
    assert family_plan.group_by_source_field == group_source
    assert family_plan.group_min_count == minimum
    assert family_plan.group_min_count_source_field == metric_source
    assert family_plan.group_order_aggregation == CompoundAggregation(
        AggregateFunction.AVG,
        metric_source,
    )
    assert family_plan.direction is direction
    assert family_plan.group_limit == limit
    assert family_plan.limit == limit
    assert [(item.function, item.source_field) for item in family_plan.aggregations] == [
        (AggregateFunction.COUNT, metric_source),
        (AggregateFunction.AVG, metric_source),
    ]


def test_group_having_authorizes_only_the_fully_bound_aggregate_shape() -> None:
    question = (
        "해외주식 편입비 유효 펀드가 50개 이상인 투자지역만 남겨 "
        "평균 편입비가 큰 지역 8개를 구해 주세요."
    )
    engine = _engine()
    trace = IntentRouter().route_with_planning(question, "group-having-authority")
    plan = engine.compile(question, "group-having-authority")

    assert plan is not None
    assert trace.route_decision.disposition is RouteDisposition.EXECUTE
    assert trace.route_decision.draft.intent is InteractionIntent.SEARCH
    authorized, receipt = engine.authorize_decision(
        trace.route_decision,
        plan,
        unresolved_spans=trace.planning_decision.unresolved_spans,
    )

    assert authorized.disposition is RouteDisposition.EXECUTE
    assert authorized.draft.intent is InteractionIntent.AGGREGATE
    assert authorized.query_plan_intent is not None
    assert authorized.query_plan_intent.value == "aggregate"
    assert receipt is not None


def _record(
    product_id: str,
    manager: str,
    expense: str | None,
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
            "cu_fund_mgmt_co": manager,
            "cu_charge_rt": expense,
        },
    )


def test_group_minimum_counts_only_usable_metric_values_and_orders_average(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    question = (
        "총보수 유효 국내 ETP가 3개 이상인 운용사만 대상으로 "
        "평균 총보수를 내고 평균이 낮은 순으로 2곳을 주세요."
    )
    engine = _engine()
    records = (
        _record("P1", "A", "0.4"),
        _record("P2", "A", "0.2"),
        _record("P3", "A", "0.3"),
        _record("P4", "A", "0"),
        _record("P5", "B", "0.1"),
        _record("P6", "B", "0.2"),
        _record("P7", "B", None),
        _record("P8", "C", "0.7"),
        _record("P9", "C", "0.8"),
        _record("P10", "C", "0.9"),
    )
    monkeypatch.setattr(engine, "_load_records", lambda *_args, **_kwargs: records)
    plan = engine.compile(question, "group-valid-count")

    assert plan is not None
    result = engine.execute(plan).families[0]

    assert result.candidate_count == 6
    assert [item.group_value for item in result.grouped_aggregates] == ["A", "C"]
    assert [
        [value.value for value in item.aggregates]
        for item in result.grouped_aggregates
    ] == [
        [3, Decimal("0.3")],
        [3, Decimal("0.8")],
    ]


def test_bare_direction_does_not_choose_between_multiple_aggregate_outputs() -> None:
    question = (
        "국내 ETP를 운용사별로 AUM 평균과 총보수 평균을 함께 구하고 "
        "평균이 높은 5곳을 알려 주세요."
    )

    assert _engine().compile(question, "group-ambiguous-order") is None


@pytest.mark.parametrize(
    ("question", "function", "direction"),
    [
        (
            "국내 ETP를 운용사별로 묶어 총보수 표본이 3개 이상인 곳만 "
            "총보수 중앙값을 구하고 중앙값이 낮은 4곳을 알려 주세요.",
            AggregateFunction.MEDIAN,
            SortDirection.ASC,
        ),
        (
            "공모펀드를 투자지역별로 묶고 해외주식 구성비 표본이 10개 "
            "이상인 지역만 구성비 합계를 구해 합계가 큰 3개 지역을 보여 주세요.",
            AggregateFunction.SUM,
            SortDirection.DESC,
        ),
    ],
)
def test_group_having_supports_other_explicit_reductions(
    question: str,
    function: AggregateFunction,
    direction: SortDirection,
) -> None:
    plan = _engine().compile(question, f"group-{function.value}-order")

    assert plan is not None
    family = plan.family_plans[0]
    assert family.group_order_aggregation is not None
    assert family.group_order_aggregation.function is function
    assert family.direction is direction


def test_group_order_can_select_one_of_multiple_explicit_measures() -> None:
    question = (
        "국내 ETP를 운용사별로 묶어 AUM 합계와 총보수 평균을 구하고 "
        "총보수 평균이 낮은 5곳을 알려 주세요."
    )
    plan = _engine().compile(question, "group-multi-measure-order")

    assert plan is not None
    family = plan.family_plans[0]
    assert [(item.function, item.source_field) for item in family.aggregations] == [
        (AggregateFunction.SUM, "du_last_aum"),
        (AggregateFunction.AVG, "cu_charge_rt"),
    ]
    assert family.group_order_aggregation == CompoundAggregation(
        AggregateFunction.AVG,
        "cu_charge_rt",
    )
    assert family.direction is SortDirection.ASC


def test_group_having_uses_compound_runtime_without_losing_having_or_order(
    sample_database: tuple[
        Path,
        list[NormalizedOverseasEtpRecord],
        DatabaseManifest,
    ],
) -> None:
    database_path, _records, _manifest = sample_database
    agent = RoutedFinanceAgent({"overseas_etp": database_path})

    result = agent.answer(
        "해외 ETP 일간 수익률 표본이 1개 이상인 투자지역별 평균을 "
        "구해 상위 2개 지역을 알려 주세요.",
        "group-having-routed-runtime",
    )

    assert result.status == "executed"
    assert result.decision.draft.intent is InteractionIntent.AGGREGATE
    assert result.query_plan is not None
    assert result.query_plan.intent.value == "aggregate"
    assert result.aggregates
    assert len({tuple(item.group_values.items()) for item in result.aggregates}) <= 2
