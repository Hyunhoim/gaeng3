from __future__ import annotations

import hashlib
import json
import re
from datetime import date
from enum import StrEnum
from typing import Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictFloat,
    StrictInt,
    StrictStr,
    model_validator,
)

from finance_agent_core.config import FieldDefinition, ValueType, load_field_registry

type ScalarValue = StrictBool | StrictInt | StrictFloat | StrictStr
type ConstraintValue = ScalarValue | list[ScalarValue]

SEARCH_PROJECTION_BY_FAMILY = {
    "overseas_etp": [
        "product_id",
        "product_name",
        "ticker",
        "total_expense_ratio_pct",
        "aum",
        "trading_currency",
        "dynamic_as_of",
    ],
    "domestic_etp": [
        "product_id",
        "product_name",
        "ticker",
        "one_month_return_pct",
        "aum",
        "trading_currency",
        "dynamic_as_of",
    ],
    "bond": [
        "product_id",
        "product_name",
        "ticker",
        "issuer",
        "bond_type",
        "maturity_date",
        "remaining_days",
        "coupon_rate_pct",
        "buy_yield_pct",
        "dynamic_as_of",
    ],
    "fund": [
        "product_id",
        "product_name",
        "short_name",
        "fund_geography_scope",
        "fund_management_attribute",
        "risk_level",
        "three_month_return_pct",
        "aum",
        "trading_currency",
        "dynamic_as_of",
    ],
}


class Intent(StrEnum):
    SEARCH = "search"
    COMPARE = "compare"
    AGGREGATE = "aggregate"
    EXPLAIN = "explain"


class ProductFamily(StrEnum):
    BOND = "bond"
    DOMESTIC_ETP = "domestic_etp"
    OVERSEAS_ETP = "overseas_etp"
    FUND = "fund"


def search_projection(
    family: ProductFamily | str,
    *fields: str,
) -> list[str]:
    """Return standard search evidence plus every selected plan field."""

    family_name = family.value if isinstance(family, ProductFamily) else family
    registry = load_field_registry()
    candidates = [*SEARCH_PROJECTION_BY_FAMILY[family_name], *fields]
    return list(
        dict.fromkeys(
            field
            for field in candidates
            if field in registry.fields
            and family_name in registry.fields[field].datasets
            and registry.require_field(field, [family_name]).selectable
        )
    )


class ConstraintOperator(StrEnum):
    EQ = "eq"
    NEQ = "neq"
    IN = "in"
    NOT_IN = "not_in"
    LT = "lt"
    LTE = "lte"
    GT = "gt"
    GTE = "gte"
    BETWEEN = "between"
    CONTAINS = "contains"
    IS_PRESENT = "is_present"
    IS_MISSING = "is_missing"


class ConstraintStrength(StrEnum):
    LOCKED = "locked"
    ASK_BEFORE_RELAXING = "ask_before_relaxing"
    PREFERENCE = "preference"


class LogicalOperator(StrEnum):
    """Boolean connective used by the server-owned constraint expression."""

    ALL = "all"
    ANY = "any"


class Unit(StrEnum):
    NONE = "none"
    CODE = "code"
    BOOLEAN = "boolean"
    PCT_POINT = "pct_point"
    SOURCE_CURRENCY_AMOUNT = "source_currency_amount"
    SOURCE_QUANTITY = "source_quantity"
    DAY = "day"
    YEAR = "year"
    DATE = "date"


class SortDirection(StrEnum):
    ASC = "asc"
    DESC = "desc"


class NullPlacement(StrEnum):
    FIRST = "first"
    LAST = "last"


class AggregateFunction(StrEnum):
    COUNT = "count"
    MIN = "min"
    MAX = "max"
    AVG = "avg"
    MEDIAN = "median"
    SUM = "sum"


class ContractModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Constraint(ContractModel):
    field: str
    operator: ConstraintOperator
    value: ConstraintValue
    unit: Unit
    strength: ConstraintStrength

    @model_validator(mode="after")
    def validate_value_shape(self) -> Constraint:
        if self.operator in {
            ConstraintOperator.IS_PRESENT,
            ConstraintOperator.IS_MISSING,
        }:
            if self.value is not True:
                raise ValueError(f"{self.operator} requires the literal true")
            return self
        is_list = isinstance(self.value, list)
        if self.operator in {ConstraintOperator.IN, ConstraintOperator.NOT_IN}:
            if not is_list or not self.value:
                raise ValueError(f"{self.operator} requires a non-empty list")
        elif self.operator is ConstraintOperator.BETWEEN:
            if not is_list or len(self.value) != 2:
                raise ValueError("between requires a two-item list")
        elif is_list:
            raise ValueError(f"{self.operator} requires a scalar value")

        if self.operator is ConstraintOperator.CONTAINS and not isinstance(self.value, str):
            raise ValueError("contains requires a string value")
        return self


class ConstraintExpression(ContractModel):
    """A bounded Boolean tree over immutable constraints in one QueryPlan.

    The flat constraint list remains the single typed catalogue consumed by
    field projection, evidence and policy code.  This expression only records
    how those immutable leaves are combined, so an ``OR`` can no longer be
    silently executed as ``AND``.  An absent expression preserves the legacy
    contract: every flat constraint is conjunctive. Constraints not referenced
    by this expression are also conjunctive; stable content keys prevent a
    later insertion/removal from retargeting a positional index.
    """

    operator: LogicalOperator
    constraint_keys: list[StrictStr] = Field(default_factory=list, max_length=20)
    children: list[ConstraintExpression] = Field(default_factory=list, max_length=10)

    @model_validator(mode="after")
    def validate_shape(self) -> ConstraintExpression:
        arity = len(self.constraint_keys) + len(self.children)
        if arity == 0:
            raise ValueError("constraint expression cannot be empty")
        if self.operator is LogicalOperator.ANY and arity < 2:
            raise ValueError("any constraint expression requires at least two branches")
        if len(self.constraint_keys) != len(set(self.constraint_keys)):
            raise ValueError("constraint expression keys must not repeat within one node")
        if any(re.fullmatch(r"[0-9a-f]{64}", key) is None for key in self.constraint_keys):
            raise ValueError("constraint expression keys must be SHA-256 values")
        return self


def constraint_key(constraint: Constraint) -> str:
    """Return the stable semantic identity of a typed constraint leaf."""

    payload = json.dumps(
        constraint.model_dump(mode="json"),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


class Ranking(ContractModel):
    field: str
    direction: SortDirection
    nulls: NullPlacement


class Aggregation(ContractModel):
    function: AggregateFunction
    field: str


class IntentPayload(ContractModel):
    comparison_fields: list[str] = Field(max_length=20)
    group_by: list[str] = Field(max_length=10)
    aggregations: list[Aggregation] = Field(max_length=10)
    explain_product_ids: list[str] = Field(max_length=20)


class Ambiguity(ContractModel):
    span: str = Field(min_length=1, max_length=200)
    reason: str = Field(min_length=1, max_length=500)
    options: list[str] = Field(min_length=1, max_length=10)


class UnsupportedCondition(ContractModel):
    span: str = Field(min_length=1, max_length=200)
    reason: str = Field(min_length=1, max_length=500)


def _values(value: ConstraintValue) -> list[ScalarValue]:
    return value if isinstance(value, list) else [value]


def _validate_value_type(constraint: Constraint, definition: FieldDefinition) -> None:
    if constraint.operator in {
        ConstraintOperator.IS_PRESENT,
        ConstraintOperator.IS_MISSING,
    }:
        # Availability is a property of the source observation rather than
        # the field's domain value.  The literal ``true`` keeps the DTO shape
        # explicit without pretending that NULL is a user-supplied value.
        return
    values = _values(constraint.value)
    if definition.value_type is ValueType.NUMBER:
        valid = all(
            isinstance(value, (int, float)) and not isinstance(value, bool) for value in values
        )
    elif definition.value_type is ValueType.BOOLEAN:
        valid = all(isinstance(value, bool) for value in values)
    elif definition.value_type is ValueType.DATE:
        valid = all(isinstance(value, str) for value in values)
        if valid:
            try:
                for value in values:
                    date.fromisoformat(value)
            except ValueError as error:
                raise ValueError(f"{constraint.field} expects ISO 8601 date values") from error
    else:
        valid = all(isinstance(value, str) for value in values)

    if not valid:
        raise ValueError(f"{constraint.field} expects {definition.value_type.value} values")
    if definition.value_type is ValueType.ENUM:
        unknown_values = set(values) - set(definition.enum_values)
        if unknown_values and not definition.unknown_enum_as_empty_set:
            raise ValueError(
                f"{constraint.field} has unknown enum values: {sorted(unknown_values)}"
            )


def _require_unique(values: list[str], label: str) -> None:
    if len(values) != len(set(values)):
        raise ValueError(f"{label} must not contain duplicates")


class QueryPlan(ContractModel):
    schema_version: Literal["1.0"]
    question_id: str = Field(min_length=1, max_length=128)
    intent: Intent
    product_families: list[ProductFamily] = Field(min_length=1, max_length=4)
    constraints: list[Constraint] = Field(max_length=20)
    constraint_expression: ConstraintExpression | None = Field(
        default=None,
        # Backwards-compatible plans must retain their byte-for-byte JSON
        # shape.  Pydantic 2.13 applies this to nested model serialization.
        exclude_if=lambda value: value is None,
    )
    ranking: list[Ranking] = Field(max_length=5)
    projection: list[str] = Field(min_length=1, max_length=30)
    limit: StrictInt = Field(ge=1, le=100)
    intent_payload: IntentPayload
    ambiguities: list[Ambiguity] = Field(max_length=10)
    unsupported_conditions: list[UnsupportedCondition] = Field(max_length=10)

    @model_validator(mode="after")
    def validate_against_registry(self) -> QueryPlan:
        registry = load_field_registry()
        families = [family.value for family in self.product_families]
        for family in families:
            registry.require_dataset(family)
        _require_unique(families, "product_families")
        _require_unique(self.projection, "projection")
        _require_unique([ranking.field for ranking in self.ranking], "ranking")
        _require_unique(self.intent_payload.comparison_fields, "comparison_fields")
        _require_unique(self.intent_payload.group_by, "group_by")
        _require_unique(self.intent_payload.explain_product_ids, "explain_product_ids")
        aggregation_keys = [
            f"{aggregation.function.value}:{aggregation.field}"
            for aggregation in self.intent_payload.aggregations
        ]
        _require_unique(aggregation_keys, "aggregations")

        for constraint in self.constraints:
            definition = registry.require_field(constraint.field, families)
            if not definition.queryable:
                raise ValueError(f"{constraint.field} is not queryable")
            if (
                constraint.operator
                not in {ConstraintOperator.IS_PRESENT, ConstraintOperator.IS_MISSING}
                and constraint.operator.value not in definition.allowed_operators
            ):
                raise ValueError(
                    f"{constraint.operator.value} is not allowed for {constraint.field}"
                )
            if constraint.unit.value != definition.unit:
                raise ValueError(
                    f"{constraint.field} requires unit {definition.unit}, "
                    f"not {constraint.unit.value}"
                )
            _validate_value_type(constraint, definition)

        if self.constraint_expression is not None:
            keys_by_value: dict[str, list[Constraint]] = {}
            for constraint in self.constraints:
                keys_by_value.setdefault(constraint_key(constraint), []).append(constraint)
            seen_keys: list[str] = []
            any_keys: set[str] = set()

            def visit(expression: ConstraintExpression, depth: int) -> None:
                if depth > 4:
                    raise ValueError("constraint expression exceeds maximum depth 4")
                seen_keys.extend(expression.constraint_keys)
                if expression.operator is LogicalOperator.ANY:
                    any_keys.update(expression.constraint_keys)
                    for child in expression.children:
                        any_keys.update(_expression_keys(child))
                for child in expression.children:
                    visit(child, depth + 1)

            def _expression_keys(expression: ConstraintExpression) -> set[str]:
                values = set(expression.constraint_keys)
                for child in expression.children:
                    values.update(_expression_keys(child))
                return values

            visit(self.constraint_expression, 1)
            if len(seen_keys) != len(set(seen_keys)):
                raise ValueError("constraint expression must reference each constraint once")
            missing_or_ambiguous = [
                key for key in seen_keys if len(keys_by_value.get(key, ())) != 1
            ]
            if missing_or_ambiguous:
                raise ValueError("constraint expression references an unknown or duplicate leaf")
            if any(
                keys_by_value[key][0].strength is not ConstraintStrength.LOCKED
                for key in any_keys
            ):
                raise ValueError("OR branches require locked constraints")

        for field_name in self.projection:
            definition = registry.require_field(field_name, families)
            if not definition.selectable:
                raise ValueError(f"{field_name} is not selectable")

        for ranking in self.ranking:
            definition = registry.require_field(ranking.field, families)
            if not definition.sortable:
                raise ValueError(f"{ranking.field} is not sortable")

        for field_name in self.intent_payload.comparison_fields + self.intent_payload.group_by:
            definition = registry.require_field(field_name, families)
            if not definition.selectable:
                raise ValueError(f"{field_name} is not selectable")
            if field_name in self.intent_payload.comparison_fields and not definition.comparable:
                raise ValueError(f"{field_name} is not comparable")

        for aggregation in self.intent_payload.aggregations:
            definition = registry.require_field(aggregation.field, families)
            if aggregation.function is AggregateFunction.COUNT:
                if not definition.selectable:
                    raise ValueError(f"{aggregation.field} cannot be counted")
            elif not definition.aggregatable:
                raise ValueError(f"{aggregation.field} is not aggregatable")

        self._validate_intent_payload()
        return self

    def _validate_intent_payload(self) -> None:
        payload = self.intent_payload
        if self.intent is Intent.SEARCH:
            if (
                payload.comparison_fields
                or payload.group_by
                or payload.aggregations
                or payload.explain_product_ids
            ):
                raise ValueError("search requires an empty intent_payload")
        elif self.intent is Intent.COMPARE:
            blocked = bool(self.ambiguities or self.unsupported_conditions)
            if not payload.comparison_fields and not blocked:
                raise ValueError("compare requires comparison_fields")
            if payload.group_by or payload.aggregations or payload.explain_product_ids:
                raise ValueError("compare contains fields for another intent")
            if self.ranking:
                raise ValueError("compare preserves requested product order and forbids ranking")
            missing_projection = set(payload.comparison_fields) - set(self.projection)
            if missing_projection:
                raise ValueError(
                    f"compare projection omits comparison fields: {sorted(missing_projection)}"
                )
        elif self.intent is Intent.AGGREGATE:
            if not payload.aggregations:
                raise ValueError("aggregate requires aggregations")
            if payload.comparison_fields or payload.explain_product_ids:
                raise ValueError("aggregate contains fields for another intent")
            if self.ranking:
                raise ValueError("aggregate groups use deterministic ordering and forbid ranking")
            required_projection = {
                *payload.group_by,
                *(aggregation.field for aggregation in payload.aggregations),
            }
            missing_projection = required_projection - set(self.projection)
            if missing_projection:
                raise ValueError(
                    f"aggregate projection omits execution fields: {sorted(missing_projection)}"
                )
        elif self.intent is Intent.EXPLAIN:
            if not payload.explain_product_ids:
                raise ValueError("explain requires explain_product_ids")
            if payload.comparison_fields or payload.group_by or payload.aggregations:
                raise ValueError("explain contains fields for another intent")
