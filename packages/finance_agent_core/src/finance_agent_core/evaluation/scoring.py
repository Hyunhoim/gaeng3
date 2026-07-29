from __future__ import annotations

import json
from typing import Any

from finance_agent_core.contracts.queryplan import ConstraintOperator, QueryPlan
from finance_agent_core.evaluation.models import (
    EvaluationCase,
    ExpectedBlocker,
    ExpectedDisposition,
)


def _normalized_value(operator: ConstraintOperator, value: object) -> str:
    if isinstance(value, list) and operator in {
        ConstraintOperator.IN,
        ConstraintOperator.NOT_IN,
    }:
        value = sorted(value, key=lambda item: (type(item).__name__, repr(item)))
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _normalized_constraints(plan: QueryPlan) -> list[tuple[str, str, str, str, str]]:
    return sorted(
        (
            item.field,
            item.operator.value,
            _normalized_value(item.operator, item.value),
            item.unit.value,
            item.strength.value,
        )
        for item in plan.constraints
    )


def _normalized_rankings(plan: QueryPlan) -> list[tuple[str, str, str]]:
    return [(item.field, item.direction.value, item.nulls.value) for item in plan.ranking]


def semantic_checks(
    case: EvaluationCase,
    actual: QueryPlan,
    product_family: str = "overseas_etp",
) -> dict[str, bool]:
    expected = case.expected_plan(product_family)
    expected_ambiguity = case.blocker is ExpectedBlocker.AMBIGUITY
    expected_unsupported = case.blocker is ExpectedBlocker.UNSUPPORTED
    actual_blocked = bool(
        actual.ambiguities or actual.unsupported_conditions or actual.intent.value != "search"
    )
    checks = {
        "intent": actual.intent == expected.intent,
        "product_families": actual.product_families == expected.product_families,
        "constraints": _normalized_constraints(actual) == _normalized_constraints(expected),
        "ranking": _normalized_rankings(actual) == _normalized_rankings(expected),
        "projection": actual.projection == expected.projection,
        "limit": actual.limit == expected.limit,
        "intent_payload": actual.intent_payload == expected.intent_payload,
        "ambiguity_signal": bool(actual.ambiguities) is expected_ambiguity,
        "unsupported_signal": bool(actual.unsupported_conditions) is expected_unsupported,
        "disposition": actual_blocked is (case.disposition is ExpectedDisposition.BLOCK),
    }
    checks["plan_exact"] = all(checks.values())
    return checks


def stable_plan_payload(plan: QueryPlan) -> dict[str, Any]:
    payload = plan.model_dump(mode="json")
    payload["constraints"] = sorted(
        payload["constraints"],
        key=lambda item: (
            item["field"],
            item["operator"],
            json.dumps(item["value"], ensure_ascii=False, sort_keys=True),
        ),
    )
    return payload
