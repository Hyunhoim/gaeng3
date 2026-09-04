from __future__ import annotations

from finance_agent_core.contracts.queryplan import (
    AggregateFunction,
    Aggregation,
    Constraint,
    ConstraintOperator,
    ConstraintStrength,
    Intent,
    IntentPayload,
    ProductFamily,
    QueryPlan,
    Unit,
)
from finance_agent_core.evaluation.coverage_execution_audit import (
    execution_semantic_plan_equal,
)


def _aggregate_plan(
    *,
    field: str = "total_expense_ratio_pct",
    group_by: list[str] | None = None,
    limit: int = 1,
    include_product_id: bool = False,
    constraints: list[Constraint] | None = None,
    extra_aggregations: list[Aggregation] | None = None,
) -> QueryPlan:
    groups = [] if group_by is None else group_by
    aggregations = [
        Aggregation(function=AggregateFunction.AVG, field=field),
        *(extra_aggregations or []),
    ]
    projection = list(
        dict.fromkeys(
            [
                *(["product_id"] if include_product_id else []),
                *groups,
                *(item.field for item in aggregations),
            ]
        )
    )
    return QueryPlan(
        schema_version="1.0",
        question_id="audit-aggregate",
        intent=Intent.AGGREGATE,
        product_families=[ProductFamily.OVERSEAS_ETP],
        constraints=constraints or [],
        ranking=[],
        projection=projection,
        limit=limit,
        intent_payload=IntentPayload(
            comparison_fields=[],
            group_by=groups,
            aggregations=aggregations,
            explain_product_ids=[],
        ),
        ambiguities=[],
        unsupported_conditions=[],
    )


def _search_plan(*, identity_field: str = "product_id", limit: int = 3) -> QueryPlan:
    return QueryPlan(
        schema_version="1.0",
        question_id="audit-search",
        intent=Intent.SEARCH,
        product_families=[ProductFamily.OVERSEAS_ETP],
        constraints=[
            Constraint(
                field=identity_field,
                operator=ConstraintOperator.EQ,
                value="AMX:TEST" if identity_field == "product_id" else "TEST",
                unit=Unit.CODE,
                strength=ConstraintStrength.LOCKED,
            )
        ],
        ranking=[],
        projection=["product_id"],
        limit=limit,
        intent_payload=IntentPayload(
            comparison_fields=[],
            group_by=[],
            aggregations=[],
            explain_product_ids=[],
        ),
        ambiguities=[],
        unsupported_conditions=[],
    )


def test_aggregate_projection_and_nongrouped_limit_are_execution_inert() -> None:
    expected = _aggregate_plan(limit=100)
    actual = _aggregate_plan(limit=1, include_product_id=True)

    assert execution_semantic_plan_equal(expected, actual)


def test_added_aggregation_is_not_execution_inert() -> None:
    expected = _aggregate_plan()
    actual = _aggregate_plan(
        extra_aggregations=[Aggregation(function=AggregateFunction.COUNT, field="product_id")]
    )

    assert not execution_semantic_plan_equal(expected, actual)


def test_changed_aggregate_target_group_or_constraint_is_not_execution_inert() -> None:
    expected = _aggregate_plan(group_by=["product_type"], limit=10)
    changed_target = _aggregate_plan(field="aum", group_by=["product_type"], limit=10)
    changed_group = _aggregate_plan(group_by=["trading_currency"], limit=10)
    changed_constraint = _aggregate_plan(
        group_by=["product_type"],
        limit=10,
        constraints=[
            Constraint(
                field="product_type",
                operator=ConstraintOperator.EQ,
                value="ETF",
                unit=Unit.CODE,
                strength=ConstraintStrength.LOCKED,
            )
        ],
    )

    assert not execution_semantic_plan_equal(expected, changed_target)
    assert not execution_semantic_plan_equal(expected, changed_group)
    assert not execution_semantic_plan_equal(expected, changed_constraint)


def test_grouped_aggregate_limit_is_not_execution_inert() -> None:
    expected = _aggregate_plan(group_by=["product_type"], limit=10)
    actual = _aggregate_plan(group_by=["product_type"], limit=1)

    assert not execution_semantic_plan_equal(expected, actual)


def test_product_id_and_ticker_constraints_are_not_equivalent() -> None:
    expected = _search_plan(identity_field="product_id")
    actual = _search_plan(identity_field="ticker")

    assert not execution_semantic_plan_equal(expected, actual)


def test_search_limit_and_projection_remain_exact() -> None:
    expected = _search_plan()
    changed_limit = _search_plan(limit=4)
    changed_projection = _search_plan().model_copy(
        update={"projection": ["product_id", "product_name"]}
    )

    assert not execution_semantic_plan_equal(expected, changed_limit)
    assert not execution_semantic_plan_equal(expected, changed_projection)
