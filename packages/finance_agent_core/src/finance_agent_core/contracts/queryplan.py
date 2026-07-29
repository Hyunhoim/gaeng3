from __future__ import annotations

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
        "buyable_quantity",
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


class ConstraintStrength(StrEnum):
    LOCKED = "locked"
    ASK_BEFORE_RELAXING = "ask_before_relaxing"
    PREFERENCE = "preference"


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
        if unknown_values:
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

        for constraint in self.constraints:
            definition = registry.require_field(constraint.field, families)
            if not definition.queryable:
                raise ValueError(f"{constraint.field} is not queryable")
            if constraint.operator.value not in definition.allowed_operators:
                raise ValueError(
                    f"{constraint.operator.value} is not allowed for {constraint.field}"
                )
            if constraint.unit.value != definition.unit:
                raise ValueError(
                    f"{constraint.field} requires unit {definition.unit}, "
                    f"not {constraint.unit.value}"
                )
            _validate_value_type(constraint, definition)

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
            if not payload.comparison_fields:
                raise ValueError("compare requires comparison_fields")
            if payload.group_by or payload.aggregations or payload.explain_product_ids:
                raise ValueError("compare contains fields for another intent")
        elif self.intent is Intent.AGGREGATE:
            if not payload.aggregations:
                raise ValueError("aggregate requires aggregations")
            if payload.comparison_fields or payload.explain_product_ids:
                raise ValueError("aggregate contains fields for another intent")
        elif self.intent is Intent.EXPLAIN:
            if not payload.explain_product_ids:
                raise ValueError("explain requires explain_product_ids")
            if payload.comparison_fields or payload.group_by or payload.aggregations:
                raise ValueError("explain contains fields for another intent")
