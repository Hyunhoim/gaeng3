from __future__ import annotations

from finance_agent_core.config import ValueType, load_field_registry
from finance_agent_core.contracts.queryplan import (
    AggregateFunction,
    ConstraintOperator,
    ConstraintStrength,
    Intent,
    ProductFamily,
    QueryPlan,
)


class PlanExecutionBlockedError(ValueError):
    """Raised when a valid QueryPlan still requires clarification or unsupported work."""


_IDENTITY_GROUP_FIELDS = {
    "product_id",
    "product_name",
    "short_name",
    "ticker",
    "isin",
}
_ADDITIVE_UNITS = {"source_currency_amount", "source_quantity"}


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


def require_fund_aum_currency_scope(plan: QueryPlan) -> None:
    if plan.product_families != [ProductFamily.FUND]:
        return
    compares_aum = any(constraint.field == "aum" for constraint in plan.constraints) or any(
        ranking.field == "aum" for ranking in plan.ranking
    )
    compares_aum = compares_aum or any(
        aggregation.field == "aum" for aggregation in plan.intent_payload.aggregations
    )
    if not compares_aum:
        return
    currency_constraints = [
        constraint for constraint in plan.constraints if constraint.field == "trading_currency"
    ]
    if (
        len(currency_constraints) != 1
        or currency_constraints[0].operator is not ConstraintOperator.EQ
        or currency_constraints[0].value not in {"KRW", "USD"}
        or currency_constraints[0].strength is not ConstraintStrength.LOCKED
    ):
        raise PlanExecutionBlockedError(
            "fund AUM comparison requires exactly one locked "
            "trading_currency = KRW or USD constraint"
        )


def comparison_product_ids(plan: QueryPlan) -> list[str]:
    constraints = [
        constraint for constraint in plan.constraints if constraint.field == "product_id"
    ]
    if (
        len(constraints) != 1
        or constraints[0].operator is not ConstraintOperator.IN
        or constraints[0].strength is not ConstraintStrength.LOCKED
        or not isinstance(constraints[0].value, list)
        or len(constraints[0].value) != 2
        or not all(
            isinstance(product_id, str) and product_id.strip()
            for product_id in constraints[0].value
        )
        or len(set(constraints[0].value)) != 2
    ):
        raise PlanExecutionBlockedError(
            "comparison requires exactly two unique product IDs in one "
            "locked product_id IN constraint"
        )
    return list(constraints[0].value)


def fund_comparison_product_ids(plan: QueryPlan) -> list[str]:
    """Backward-compatible public-fund comparison identity helper."""

    if plan.product_families != [ProductFamily.FUND]:
        raise PlanExecutionBlockedError("fund comparison requires product family fund")
    return comparison_product_ids(plan)


def require_comparison_contract(
    plan: QueryPlan,
    *,
    require_enabled_dataset: bool = False,
) -> None:
    blockers: list[str] = []
    if plan.intent is not Intent.COMPARE:
        blockers.append(f"intent {plan.intent.value!r} is not a comparison")
    if len(plan.product_families) != 1:
        blockers.append("comparison requires exactly one product family")
    if plan.ambiguities:
        blockers.append(f"{len(plan.ambiguities)} ambiguity item(s) require clarification")
    if plan.unsupported_conditions:
        blockers.append(
            f"{len(plan.unsupported_conditions)} unsupported condition(s) require a safe refusal"
        )
    if plan.ranking:
        blockers.append("comparison preserves requested product order and forbids ranking")
    if plan.limit != 2:
        blockers.append("comparison limit must equal the two requested products")
    allowed_constraint_fields = {"product_id"}
    if plan.product_families == [ProductFamily.FUND]:
        allowed_constraint_fields.add("public_offering")
    unsupported_constraint_fields = {
        constraint.field for constraint in plan.constraints
    } - allowed_constraint_fields
    if unsupported_constraint_fields:
        blockers.append(
            f"comparison contains non-identity constraints: {sorted(unsupported_constraint_fields)}"
        )
    try:
        require_fund_public_scope(plan)
    except PlanExecutionBlockedError as error:
        blockers.append(str(error))
    try:
        comparison_product_ids(plan)
    except PlanExecutionBlockedError as error:
        blockers.append(str(error))
    required_projection = {
        "product_id",
        "product_name",
        *plan.intent_payload.comparison_fields,
    }
    missing_projection = required_projection - set(plan.projection)
    if missing_projection:
        blockers.append(f"comparison projection is missing {sorted(missing_projection)}")
    if len(plan.product_families) == 1:
        registry = load_field_registry()
        family = plan.product_families[0].value
        for field_name in plan.intent_payload.comparison_fields:
            definition = registry.require_field(field_name, [family])
            if not definition.comparable:
                blockers.append(f"field {field_name!r} is not comparable for {family!r}")
            if (
                "trading_currency" in definition.comparison_scope
                and "trading_currency" not in plan.projection
            ):
                blockers.append(f"{field_name} comparison must project trading_currency")
        if require_enabled_dataset and not registry.require_dataset(family).execution_enabled:
            blockers.append(f"product family {family!r} is not enabled for execution")
    if blockers:
        raise PlanExecutionBlockedError("; ".join(blockers))


def require_fund_comparison_contract(
    plan: QueryPlan,
    *,
    require_enabled_dataset: bool = False,
) -> None:
    """Backward-compatible public-fund comparison contract."""

    if plan.product_families != [ProductFamily.FUND]:
        raise PlanExecutionBlockedError("fund comparison requires product family fund")
    require_comparison_contract(
        plan,
        require_enabled_dataset=require_enabled_dataset,
    )


def require_aggregate_contract(
    plan: QueryPlan,
    *,
    require_enabled_dataset: bool = False,
) -> None:
    blockers: list[str] = []
    if plan.intent is not Intent.AGGREGATE:
        blockers.append(f"intent {plan.intent.value!r} is not an aggregation")
    if len(plan.product_families) != 1:
        blockers.append("aggregation requires exactly one product family")
    if plan.ambiguities:
        blockers.append(f"{len(plan.ambiguities)} ambiguity item(s) require clarification")
    if plan.unsupported_conditions:
        blockers.append(
            f"{len(plan.unsupported_conditions)} unsupported condition(s) require a safe refusal"
        )
    if plan.ranking:
        blockers.append("aggregation uses deterministic group ordering and forbids ranking")
    if len(plan.intent_payload.group_by) > 2:
        blockers.append("aggregation supports at most two group fields")

    registry = load_field_registry()
    if len(plan.product_families) == 1:
        family = plan.product_families[0].value
        if require_enabled_dataset and not registry.require_dataset(family).execution_enabled:
            blockers.append(f"product family {family!r} is not enabled for execution")
        for field_name in plan.intent_payload.group_by:
            definition = registry.require_field(field_name, [family])
            if field_name in _IDENTITY_GROUP_FIELDS:
                blockers.append(f"identity field {field_name!r} cannot drive aggregation groups")
            if definition.value_type in {ValueType.NUMBER, ValueType.DATE}:
                blockers.append(
                    f"group field {field_name!r} requires an explicit bucketing contract"
                )

        amount_functions = []
        for aggregation in plan.intent_payload.aggregations:
            definition = registry.require_field(aggregation.field, [family])
            if (
                aggregation.function is AggregateFunction.SUM
                and definition.unit not in _ADDITIVE_UNITS
            ):
                blockers.append(
                    f"sum is not meaningful for {aggregation.field!r} with unit {definition.unit!r}"
                )
            if (
                aggregation.function is not AggregateFunction.COUNT
                and definition.unit == "source_currency_amount"
            ):
                amount_functions.append(aggregation)

        if amount_functions and "trading_currency" not in plan.intent_payload.group_by:
            currency_constraints = [
                constraint
                for constraint in plan.constraints
                if constraint.field == "trading_currency"
            ]
            currency_locked = (
                len(currency_constraints) == 1
                and currency_constraints[0].operator is ConstraintOperator.EQ
                and currency_constraints[0].strength is ConstraintStrength.LOCKED
                and isinstance(currency_constraints[0].value, str)
                and bool(currency_constraints[0].value)
            )
            if not currency_locked:
                blockers.append(
                    "amount aggregation requires one locked trading_currency equality "
                    "or trading_currency group_by"
                )
        try:
            require_fund_public_scope(plan)
        except PlanExecutionBlockedError as error:
            blockers.append(str(error))
    if blockers:
        raise PlanExecutionBlockedError("; ".join(blockers))


def _require_search_contract(
    plan: QueryPlan,
    *,
    require_enabled_dataset: bool,
) -> None:
    blockers: list[str] = []
    if plan.intent is not Intent.SEARCH:
        blockers.append(f"intent {plan.intent.value!r} is not executable yet")
    if plan.ambiguities:
        blockers.append(f"{len(plan.ambiguities)} ambiguity item(s) require clarification")
    if plan.unsupported_conditions:
        blockers.append(
            f"{len(plan.unsupported_conditions)} unsupported condition(s) require a safe refusal"
        )
    if require_enabled_dataset:
        registry = load_field_registry()
        for family in plan.product_families:
            if not registry.require_dataset(family.value).execution_enabled:
                blockers.append(f"product family {family.value!r} is not enabled for execution")
    try:
        require_fund_public_scope(plan)
    except PlanExecutionBlockedError as error:
        blockers.append(str(error))
    try:
        require_fund_aum_currency_scope(plan)
    except PlanExecutionBlockedError as error:
        blockers.append(str(error))
    if blockers:
        raise PlanExecutionBlockedError("; ".join(blockers))


def require_executable_search(plan: QueryPlan) -> None:
    _require_search_contract(plan, require_enabled_dataset=True)


def require_internal_evaluation_search(plan: QueryPlan) -> None:
    """Validate a frozen Oracle regression plan without opening Agent execution."""

    _require_search_contract(plan, require_enabled_dataset=False)


def require_executable_comparison(plan: QueryPlan) -> None:
    require_comparison_contract(plan, require_enabled_dataset=True)


def require_internal_evaluation_comparison(plan: QueryPlan) -> None:
    """Validate comparison without opening a disabled official dataset."""

    require_comparison_contract(plan, require_enabled_dataset=False)


def require_executable_aggregation(plan: QueryPlan) -> None:
    require_aggregate_contract(plan, require_enabled_dataset=True)


def require_internal_evaluation_aggregation(plan: QueryPlan) -> None:
    """Validate aggregate execution without opening a disabled official dataset."""

    require_aggregate_contract(plan, require_enabled_dataset=False)
