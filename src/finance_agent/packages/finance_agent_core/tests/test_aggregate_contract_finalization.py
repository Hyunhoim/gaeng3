from __future__ import annotations

from pathlib import Path

import pytest

from finance_agent_core.agent.compiler import (
    PlanCompilationBlockedError,
    ServerQueryPlanCompiler,
)
from finance_agent_core.agent.compound_query import TypedCompoundQueryEngine
from finance_agent_core.agent.routed_service import (
    RoutedFinanceAgent,
    _lock_approved_fund_aggregate_scope,
)
from finance_agent_core.agent.router import IntentRouter
from finance_agent_core.contracts.queryplan import AggregateFunction, ProductFamily
from finance_agent_core.execution import require_internal_evaluation_aggregation
from finance_agent_core.storage import RecordSnapshotCache, write_database


def _compile(question: str):
    decision = IntentRouter().route(question, f"aggregate-final-{abs(hash(question))}")
    return ServerQueryPlanCompiler({}).compile(decision)


def _compound_engine() -> TypedCompoundQueryEngine:
    return TypedCompoundQueryEngine(
        {family: Path(f"/{family.value}.sqlite3") for family in ProductFamily},
        record_cache=RecordSnapshotCache(),
    )


def test_executable_fund_aggregate_locks_the_approved_public_scope() -> None:
    parser_plan = _compile("공모펀드는 모두 몇 개인가요?")
    plan = _lock_approved_fund_aggregate_scope(parser_plan)

    assert [(item.field, item.operator.value, item.value) for item in plan.constraints] == [
        ("public_offering", "eq", True)
    ]
    require_internal_evaluation_aggregation(plan)


def test_fund_compound_aggregate_keeps_the_same_approved_scope() -> None:
    plan = _compound_engine().compile(
        "공모펀드를 투자지역과 위험등급별로 나눠 상품 수를 알려줘",
        "fund-two-axis-scope",
    )

    assert plan is not None
    family = plan.family_plans[0]
    assert family.group_by_source_fields == (
        "fd_ivst_rgn_desc",
        "zrin_fd_ivst_risk_gcd",
    )
    assert ("prvo_pbff_desc", "eq", "공모") in {
        (item.source_field, item.operator, item.value) for item in family.filters
    }
    query_plan = _compound_engine()._query_plan(plan, family)
    assert query_plan.intent_payload.group_by == ["investment_region", "risk_level"]
    assert any(
        item.field == "public_offering" and item.value is True
        for item in query_plan.constraints
    )


def test_shared_group_suffix_preserves_both_public_queryplan_axes() -> None:
    plan = _compile(
        "공모펀드를 투자지역과 위험등급별로 나눠 상품 수를 알려줘"
    )

    assert plan.intent_payload.group_by == ["investment_region", "risk_level"]


def test_two_axis_group_values_survive_compound_public_response(sample_database) -> None:
    database_path, _records, _manifest = sample_database
    result = RoutedFinanceAgent({"overseas_etp": database_path}).answer(
        "해외 ETP를 자산유형과 투자지역별로 나눠 상품 수를 알려줘",
        "two-axis-public-evidence",
    )

    assert result.status == "executed"
    assert result.query_plan is not None
    assert result.query_plan.intent_payload.group_by == [
        "asset_type",
        "investment_region",
    ]
    assert result.aggregates
    assert all(
        set(item.group_values) == {"asset_type", "investment_region"}
        for item in result.aggregates
    )


def test_bond_market_contrast_is_grouped_instead_of_silent_total_count() -> None:
    question = "국내채권을 장내와 장외로 나눠 각각 상품 수를 알려줘"
    plan = _compile(question)
    compound = _compound_engine().compile(question, "bond-market-groups")

    assert plan.intent_payload.group_by == ["bond_market"]
    assert compound is not None
    assert compound.family_plans[0].group_by_source_fields == ("pd_exg_mkt",)


@pytest.mark.parametrize(
    "question",
    [
        "국내 ETP를 운용사별로 AUM과 총보수 평균을 알려줘",
        "국내 ETP를 운용사별로 AUM, 총보수 및 1년 수익률 평균을 알려줘",
    ],
)
def test_one_trailing_function_keeps_every_coordinated_metric(question: str) -> None:
    plan = _compile(question)
    expected = [
        (AggregateFunction.AVG, "aum"),
        (AggregateFunction.AVG, "total_expense_ratio_pct"),
    ]
    if "1년" in question:
        expected.append((AggregateFunction.AVG, "one_year_return_pct"))

    assert [(item.function, item.field) for item in plan.intent_payload.aggregations] == expected
    compound = _compound_engine().compile(question, f"coordinated-{len(expected)}")
    assert compound is not None
    assert len(compound.family_plans[0].aggregations) == len(expected)


def test_repeated_average_word_keeps_colloquial_aum_and_cost_metrics() -> None:
    plan = _compile(
        "평균 운용자산 규모와 평균 비용이 얼마인지 해외 ETF 기준으로 알려줘"
    )

    assert [(item.function, item.field) for item in plan.intent_payload.aggregations] == [
        (AggregateFunction.AVG, "aum"),
        (AggregateFunction.AVG, "total_expense_ratio_pct"),
    ]


def test_two_sided_availability_returns_both_present_and_missing_counts(
    sample_database,
    tmp_path: Path,
) -> None:
    _database_path, records, manifest = sample_database
    source_values = dict(records[0].source_values)
    source_values["du_last_aum"] = None
    missing = records[0].model_copy(
        update={"aum": None, "source_values": source_values}
    )
    database_path = tmp_path / "availability-final.sqlite3"
    write_database(database_path, [missing, *records[1:]], manifest)
    with pytest.raises(PlanCompilationBlockedError, match="대상 필드"):
        _compile("해외 ETP에서 값이 있는 상품과 없는 상품 수를 각각 알려줘")

    plan = _compile(
        "해외 ETP에서 AUM 값이 있는 상품과 없는 상품 수를 각각 알려줘"
    )
    assert [(item.function, item.field) for item in plan.intent_payload.aggregations] == [
        (AggregateFunction.COUNT, "aum")
    ]
    assert not plan.constraints

    result = RoutedFinanceAgent({"overseas_etp": database_path}).answer(
        "해외 ETP에서 AUM 값이 있는 상품과 없는 상품 수를 각각 알려줘",
        "availability-two-sided-public",
    )
    assert result.status == "executed"
    assert len(result.aggregates) == 1
    evidence = result.aggregates[0]
    assert evidence.value == evidence.valid_count
    assert evidence.valid_count > 0
    assert evidence.missing_count > 0
    assert evidence.valid_count + evidence.missing_count == result.candidate_count

    blocked = RoutedFinanceAgent({"overseas_etp": database_path}).answer(
        "해외 ETP에서 값이 있는 상품과 없는 상품 수를 각각 알려줘",
        "availability-missing-owner",
    )
    assert blocked.status == "clarify"
    assert not blocked.aggregates
