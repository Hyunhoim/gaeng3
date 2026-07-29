from __future__ import annotations

from finance_agent_core.config import load_field_registry
from finance_agent_core.contracts.queryplan import (
    ConstraintOperator,
    ConstraintStrength,
    Intent,
    ProductFamily,
    QueryPlan,
)


class PlanExecutionBlockedError(ValueError):
    """Raised when a valid QueryPlan still requires clarification or unsupported work."""


def require_fund_public_scope(plan: QueryPlan) -> None:
    if plan.product_families != [ProductFamily.FUND]:
        return
    scope_constraints = [
        constraint for constraint in plan.constraints if constraint.field == "public_offering"
    ]
    if (
        len(scope_constraints) != 1
        or scope_constraints[0].operator is not ConstraintOperator.EQ
        or scope_constraints[0].value is not True
        or scope_constraints[0].strength is not ConstraintStrength.LOCKED
    ):
        raise PlanExecutionBlockedError(
            "fund search requires exactly one locked public_offering = true constraint"
        )


def require_executable_search(plan: QueryPlan) -> None:
    blockers: list[str] = []
    if plan.intent is not Intent.SEARCH:
        blockers.append(f"intent {plan.intent.value!r} is not executable yet")
    if plan.ambiguities:
        blockers.append(f"{len(plan.ambiguities)} ambiguity item(s) require clarification")
    if plan.unsupported_conditions:
        blockers.append(
            f"{len(plan.unsupported_conditions)} unsupported condition(s) require a safe refusal"
        )
    registry = load_field_registry()
    for family in plan.product_families:
        if not registry.require_dataset(family.value).execution_enabled:
            blockers.append(f"product family {family.value!r} is not enabled for execution")
    try:
        require_fund_public_scope(plan)
    except PlanExecutionBlockedError as error:
        blockers.append(str(error))
    if blockers:
        raise PlanExecutionBlockedError("; ".join(blockers))
