from copy import deepcopy

import pytest
from pydantic import ValidationError

from finance_agent_core.contracts import QueryPlan


@pytest.fixture
def vertical_slice_plan() -> dict:
    return {
        "schema_version": "1.0",
        "question_id": "overseas-etp-001",
        "intent": "search",
        "product_families": ["overseas_etp"],
        "constraints": [
            {
                "field": "product_type",
                "operator": "eq",
                "value": "ETF",
                "unit": "code",
                "strength": "locked",
            },
            {
                "field": "investment_region",
                "operator": "eq",
                "value": "United States of America",
                "unit": "code",
                "strength": "locked",
            },
            {
                "field": "asset_type",
                "operator": "eq",
                "value": "Bond",
                "unit": "code",
                "strength": "locked",
            },
            {
                "field": "trading_suspended",
                "operator": "eq",
                "value": False,
                "unit": "boolean",
                "strength": "locked",
            },
            {
                "field": "sellable",
                "operator": "eq",
                "value": True,
                "unit": "boolean",
                "strength": "locked",
            },
            {
                "field": "total_expense_ratio_pct",
                "operator": "lte",
                "value": 0.20,
                "unit": "pct_point",
                "strength": "locked",
            },
        ],
        "ranking": [{"field": "aum", "direction": "desc", "nulls": "last"}],
        "projection": [
            "product_id",
            "product_name",
            "ticker",
            "total_expense_ratio_pct",
            "aum",
            "dynamic_as_of",
        ],
        "limit": 5,
        "intent_payload": {
            "comparison_fields": [],
            "group_by": [],
            "aggregations": [],
            "explain_product_ids": [],
        },
        "ambiguities": [],
        "unsupported_conditions": [],
    }


def test_vertical_slice_queryplan_round_trips(vertical_slice_plan: dict) -> None:
    plan = QueryPlan.model_validate(vertical_slice_plan)
    reparsed = QueryPlan.model_validate_json(plan.model_dump_json())

    assert reparsed == plan
    assert plan.limit == 5
    assert all(constraint.strength.value == "locked" for constraint in plan.constraints)


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (
            lambda plan: plan["constraints"][0].update(field="one_day_return_pct"),
            "is not queryable",
        ),
        (
            lambda plan: plan["constraints"][0].update(value="Mutual Fund"),
            "unknown enum values",
        ),
        (
            lambda plan: plan["constraints"][0].update(operator="lte"),
            "not allowed",
        ),
        (
            lambda plan: plan["constraints"][5].update(unit="none"),
            "requires unit pct_point",
        ),
        (
            lambda plan: plan.update(product_families=["fund"]),
            "no frozen field registry",
        ),
        (
            lambda plan: plan["projection"].append("one_day_return_pct"),
            "is not selectable",
        ),
    ],
)
def test_registry_violations_fail_closed(vertical_slice_plan: dict, mutation, message: str) -> None:
    payload = deepcopy(vertical_slice_plan)
    mutation(payload)

    with pytest.raises(ValidationError, match=message):
        QueryPlan.model_validate(payload)


def test_constraint_shape_is_checked_before_execution(
    vertical_slice_plan: dict,
) -> None:
    payload = deepcopy(vertical_slice_plan)
    payload["constraints"][5].update(operator="between", value=[0.1])

    with pytest.raises(ValidationError, match="two-item list"):
        QueryPlan.model_validate(payload)


def test_date_constraints_require_iso_8601(vertical_slice_plan: dict) -> None:
    payload = deepcopy(vertical_slice_plan)
    payload["constraints"].append(
        {
            "field": "dynamic_as_of",
            "operator": "gte",
            "value": "2026/06/01",
            "unit": "date",
            "strength": "locked",
        }
    )

    with pytest.raises(ValidationError, match="ISO 8601 date"):
        QueryPlan.model_validate(payload)


def test_search_rejects_fields_for_another_intent(vertical_slice_plan: dict) -> None:
    payload = deepcopy(vertical_slice_plan)
    payload["intent_payload"]["comparison_fields"] = ["aum"]

    with pytest.raises(ValidationError, match="empty intent_payload"):
        QueryPlan.model_validate(payload)


def test_compare_requires_comparison_fields(vertical_slice_plan: dict) -> None:
    payload = deepcopy(vertical_slice_plan)
    payload["intent"] = "compare"

    with pytest.raises(ValidationError, match="requires comparison_fields"):
        QueryPlan.model_validate(payload)


def test_extra_properties_are_forbidden(vertical_slice_plan: dict) -> None:
    payload = deepcopy(vertical_slice_plan)
    payload["silent_relaxation"] = True

    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        QueryPlan.model_validate(payload)
