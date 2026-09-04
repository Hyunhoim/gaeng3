from __future__ import annotations

import hashlib
import re
import sqlite3
import time
from collections import Counter
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum
from importlib.resources import files
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from finance_agent_core.config import AsOfBasis, FieldDefinition, ValueType, load_field_registry
from finance_agent_core.contracts.queryplan import (
    AggregateFunction,
    Aggregation,
    Constraint,
    ConstraintOperator,
    ConstraintStrength,
    Intent,
    IntentPayload,
    NullPlacement,
    ProductFamily,
    QueryPlan,
    Ranking,
    SortDirection,
    Unit,
    search_projection,
)
from finance_agent_core.evaluation.semantics import (
    canonical_json_sha256,
    query_plan_semantic_sha256,
)
from finance_agent_core.execution import (
    AggregateResultVerifier,
    ResultVerifier,
    SQLiteAggregateOracle,
    SQLiteOracle,
    authorize_internal_evaluation_plan,
    build_aggregate_evidence,
    build_comparison_evidence,
    build_product_comparison,
    build_product_evidence,
    require_internal_evaluation_aggregation,
    require_internal_evaluation_comparison,
    require_internal_evaluation_search,
)
from finance_agent_core.execution.sql_schema import SQL_FIELDS_BY_FAMILY, TABLE_BY_FAMILY, SqlField
from finance_agent_core.storage import RecordSnapshotCache
from finance_agent_core.storage.sqlite import connect_read_only

_SUITE_ID = "coverage-guided-plan-v1"
_CANONICAL_RENDERER_VERSION = "coverage-canonical-v2"
_IDENTITY_FIELDS = {"product_id", "ticker", "isin"}
_GROUP_BY_BLOCKED_FIELDS = {
    "product_id",
    "product_name",
    "short_name",
    "ticker",
    "isin",
    "product_family",
    "public_offering",
}
_ADDITIVE_UNITS = {"source_currency_amount", "source_quantity"}
_QUALITY_VALUES = ("VALID", "PARTIAL")
_FAMILY_LABELS = {
    ProductFamily.BOND: "국내채권",
    ProductFamily.DOMESTIC_ETP: "국내 ETF·ETN",
    ProductFamily.OVERSEAS_ETP: "해외 ETF·ETN",
    ProductFamily.FUND: "공모펀드",
}
_VALUE_LABELS: dict[object, str] = {
    "United States of America": "미국",
    "Bond": "채권",
    "Equity": "주식",
    "KRW": "KRW",
    "USD": "USD",
}


class CoverageModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class CoverageCellKind(StrEnum):
    SEARCH_CONSTRAINT = "search_constraint"
    SEARCH_RANKING = "search_ranking"
    COMPARE_FIELD = "compare_field"
    AGGREGATE_FIELD = "aggregate_field"
    GROUP_BY_FIELD = "group_by_field"


class CoverageCell(CoverageModel):
    key: str = Field(min_length=1, max_length=300)
    product_family: ProductFamily
    intent: Intent
    kind: CoverageCellKind
    field: str = Field(min_length=1, max_length=100)
    operator: ConstraintOperator | None = None
    direction: SortDirection | None = None
    function: AggregateFunction | None = None
    group_by: str | None = None

    @model_validator(mode="after")
    def validate_axis(self) -> CoverageCell:
        expected = {
            CoverageCellKind.SEARCH_CONSTRAINT: (self.operator is not None),
            CoverageCellKind.SEARCH_RANKING: (self.direction is not None),
            CoverageCellKind.COMPARE_FIELD: (
                self.operator is None and self.direction is None and self.function is None
            ),
            CoverageCellKind.AGGREGATE_FIELD: (self.function is not None),
            CoverageCellKind.GROUP_BY_FIELD: (
                self.group_by is not None and self.function is AggregateFunction.COUNT
            ),
        }
        if not expected[self.kind]:
            raise ValueError(f"coverage cell axes differ for {self.kind.value}")
        return self


class CoverageOutcome(CoverageModel):
    candidate_count: int = Field(ge=0)
    returned_product_ids: list[str] = Field(max_length=100)
    product_evidence_count: int = Field(ge=0)
    comparison_evidence_count: int = Field(ge=0)
    aggregate_evidence_count: int = Field(ge=0)
    query_plan_semantic_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    evidence_semantic_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    system_semantic_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_dataset: str
    source_snapshot_date: str
    latency_ms: float = Field(ge=0)


class CoveragePlanCase(CoverageModel):
    id: str = Field(pattern=r"^coverage-guided-plan-v1-[0-9]{4}$")
    cell: CoverageCell
    canonical_question: str = Field(min_length=1, max_length=3000)
    plan: QueryPlan
    outcome: CoverageOutcome

    @model_validator(mode="after")
    def validate_case(self) -> CoveragePlanCase:
        if self.plan.question_id != self.id:
            raise ValueError("coverage case and QueryPlan IDs differ")
        if self.plan.product_families != [self.cell.product_family]:
            raise ValueError("coverage case and QueryPlan families differ")
        if self.plan.intent is not self.cell.intent:
            raise ValueError("coverage case and QueryPlan intents differ")
        return self


class CoverageExclusion(CoverageModel):
    cell: CoverageCell
    reason_code: str = Field(min_length=1, max_length=100)
    reason: str = Field(min_length=1, max_length=2000)


class CoveragePlanSummary(CoverageModel):
    attempted_cells: int = Field(ge=1)
    executable_cases: int = Field(ge=0)
    excluded_cells: int = Field(ge=0)
    execution_rate: float = Field(ge=0, le=1)
    by_family: dict[str, int]
    by_kind: dict[str, int]
    by_operator: dict[str, int]
    by_direction: dict[str, int]
    by_function: dict[str, int]
    exclusion_reasons: dict[str, int]


class CoveragePlanSuite(CoverageModel):
    schema_version: Literal["1.0"] = "1.0"
    suite_id: Literal["coverage-guided-plan-v1"] = _SUITE_ID
    generated_at_utc: str
    status: Literal["internal_synthetic_not_blind"] = "internal_synthetic_not_blind"
    registry_schema_version: str
    registry_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    database_sha256_by_family: dict[str, str]
    selection_contract: dict[str, str]
    summary: CoveragePlanSummary
    cases: list[CoveragePlanCase]
    exclusions: list[CoverageExclusion]
    interpretation_limits: list[str] = Field(min_length=1, max_length=20)

    @model_validator(mode="after")
    def validate_suite(self) -> CoveragePlanSuite:
        if set(self.database_sha256_by_family) != {family.value for family in ProductFamily}:
            raise ValueError("coverage suite must pin all four databases")
        ids = [case.id for case in self.cases]
        expected = [f"{_SUITE_ID}-{index:04d}" for index in range(1, len(ids) + 1)]
        if ids != expected:
            raise ValueError("coverage case IDs must be ordered and contiguous")
        keys = [case.cell.key for case in self.cases] + [item.cell.key for item in self.exclusions]
        if len(keys) != len(set(keys)):
            raise ValueError("coverage cell keys must be unique across cases and exclusions")
        if self.summary.attempted_cells != len(keys):
            raise ValueError("coverage attempted count differs")
        if self.summary.executable_cases != len(self.cases):
            raise ValueError("coverage executable count differs")
        if self.summary.excluded_cells != len(self.exclusions):
            raise ValueError("coverage excluded count differs")
        return self


class _ValueSample(CoverageModel):
    frequent: list[Any]
    ordered: list[Any]


class _DraftCase(CoverageModel):
    cell: CoverageCell
    plan: QueryPlan
    canonical_question: str


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _field_registry_sha256() -> str:
    resource = files("finance_agent_core.config").joinpath("field_registry.yaml")
    return hashlib.sha256(resource.read_bytes()).hexdigest()


def _normalized_paths(
    database_paths: Mapping[ProductFamily | str, str | Path],
) -> dict[ProductFamily, Path]:
    normalized = {ProductFamily(family): Path(path) for family, path in database_paths.items()}
    if set(normalized) != set(ProductFamily):
        raise ValueError("coverage generation requires all four database paths")
    return normalized


def _valid_where(spec: SqlField) -> tuple[list[str], list[object]]:
    clauses = ["is_quarantined = 0", f"{spec.column} IS NOT NULL"]
    parameters: list[object] = []
    if spec.quality_column is not None:
        clauses.append(f"{spec.quality_column} IN (?, ?)")
        parameters.extend(_QUALITY_VALUES)
    return clauses, parameters


def _from_storage(value: Any, definition: FieldDefinition, spec: SqlField) -> Any:
    if definition.value_type is ValueType.BOOLEAN:
        return bool(value)
    if definition.value_type is ValueType.NUMBER:
        decimal = Decimal(str(value))
        if spec.scale is not None:
            decimal /= spec.scale
        if decimal == decimal.to_integral_value():
            return int(decimal)
        return float(decimal.normalize())
    return str(value)


def _sample_values(
    connection: sqlite3.Connection,
    family: ProductFamily,
    field_name: str,
) -> _ValueSample:
    registry = load_field_registry()
    definition = registry.require_field(field_name, [family.value])
    spec = SQL_FIELDS_BY_FAMILY[family.value][field_name]
    table = TABLE_BY_FAMILY[family.value]
    clauses, parameters = _valid_where(spec)
    where = " AND ".join(clauses)
    frequent_rows = connection.execute(
        f"SELECT {spec.column} AS value, COUNT(*) AS frequency "
        f"FROM {table} WHERE {where} "
        f"GROUP BY {spec.column} ORDER BY frequency DESC, {spec.column} ASC LIMIT 8",
        parameters,
    ).fetchall()
    count_row = connection.execute(
        f"SELECT COUNT(DISTINCT {spec.column}) AS value_count FROM {table} WHERE {where}",
        parameters,
    ).fetchone()
    value_count = 0 if count_row is None else int(count_row["value_count"])
    ordered: list[Any] = []
    if value_count:
        offsets = sorted(
            {0, value_count // 4, value_count // 2, (3 * value_count) // 4, value_count - 1}
        )
        for offset in offsets:
            row = connection.execute(
                f"SELECT DISTINCT {spec.column} AS value FROM {table} WHERE {where} "
                f"ORDER BY {spec.column} ASC LIMIT 1 OFFSET ?",
                [*parameters, offset],
            ).fetchone()
            if row is not None:
                ordered.append(_from_storage(row["value"], definition, spec))
    frequent = [
        _from_storage(row["value"], definition, spec)
        for row in frequent_rows
        if row["value"] not in {None, ""}
    ]
    return _ValueSample(
        frequent=list(dict.fromkeys(frequent)),
        ordered=list(dict.fromkeys(ordered)),
    )


def _contains_literal(value: str) -> str:
    tokens = re.findall(r"[가-힣A-Za-z0-9]{2,}", value)
    if not tokens:
        return value[: min(8, len(value))]
    token = max(tokens, key=lambda item: (len(item), item))
    return token[: min(12, len(token))]


def _constraint_value(
    operator: ConstraintOperator,
    definition: FieldDefinition,
    sample: _ValueSample,
) -> Any:
    values = sample.ordered or sample.frequent
    if not values:
        raise ValueError("field has no valid sample value")
    if operator in {ConstraintOperator.IN, ConstraintOperator.NOT_IN}:
        choices = sample.frequent or values
        if len(choices) < 2:
            raise ValueError(f"{operator.value} requires two distinct values")
        return choices[:2]
    if operator is ConstraintOperator.BETWEEN:
        if len(values) < 2:
            raise ValueError("between requires two distinct values")
        low_index = min(1, len(values) - 2)
        high_index = max(low_index + 1, len(values) - 2)
        return [values[low_index], values[high_index]]
    if operator is ConstraintOperator.CONTAINS:
        value = str((sample.frequent or values)[0])
        literal = _contains_literal(value)
        if not literal:
            raise ValueError("contains sample is blank")
        return literal
    if operator in {
        ConstraintOperator.LT,
        ConstraintOperator.LTE,
        ConstraintOperator.GT,
        ConstraintOperator.GTE,
    }:
        return values[len(values) // 2]
    if definition.value_type is ValueType.BOOLEAN:
        return bool((sample.frequent or values)[0])
    return (sample.frequent or values)[0]


def _constraint(
    family: ProductFamily,
    field_name: str,
    operator: ConstraintOperator,
    value: Any,
) -> Constraint:
    definition = load_field_registry().require_field(field_name, [family.value])
    return Constraint(
        field=field_name,
        operator=operator,
        value=value,
        unit=Unit(definition.unit),
        strength=ConstraintStrength.LOCKED,
    )


def _dominant_currency(
    connection: sqlite3.Connection,
    family: ProductFamily,
) -> str:
    sample = _sample_values(connection, family, "trading_currency")
    preferred = [value for value in sample.frequent if value in {"KRW", "USD"}]
    values = preferred or sample.frequent
    if not values:
        raise ValueError("family has no valid trading currency")
    return str(values[0])


def _base_constraints(
    connection: sqlite3.Connection,
    family: ProductFamily,
    *,
    amount_scope: bool,
) -> list[Constraint]:
    constraints: list[Constraint] = []
    if family is ProductFamily.FUND:
        constraints.append(
            _constraint(
                family,
                "public_offering",
                ConstraintOperator.EQ,
                True,
            )
        )
    if amount_scope:
        constraints.append(
            _constraint(
                family,
                "trading_currency",
                ConstraintOperator.EQ,
                _dominant_currency(connection, family),
            )
        )
    return constraints


def _merge_constraints(constraints: Sequence[Constraint]) -> list[Constraint]:
    merged: dict[str, Constraint] = {}
    for constraint in constraints:
        existing = merged.get(constraint.field)
        if existing is not None and existing != constraint:
            raise ValueError(f"conflicting generated constraints for {constraint.field}")
        merged[constraint.field] = constraint
    return list(merged.values())


def _projection(family: ProductFamily, *fields: str) -> list[str]:
    return search_projection(family, *fields)


def _empty_payload() -> IntentPayload:
    return IntentPayload(
        comparison_fields=[],
        group_by=[],
        aggregations=[],
        explain_product_ids=[],
    )


def _cell_key(
    family: ProductFamily,
    kind: CoverageCellKind,
    field: str,
    *,
    operator: ConstraintOperator | None = None,
    direction: SortDirection | None = None,
    function: AggregateFunction | None = None,
    group_by: str | None = None,
) -> str:
    axes = [family.value, kind.value, field]
    for value in (operator, direction, function):
        if value is not None:
            axes.append(value.value)
    if group_by is not None:
        axes.append(group_by)
    return ":".join(axes)


def _primary_operator(definition: FieldDefinition) -> ConstraintOperator:
    operators = {ConstraintOperator(value) for value in definition.allowed_operators}
    if definition.value_type in {ValueType.NUMBER, ValueType.DATE}:
        for candidate in (
            ConstraintOperator.BETWEEN,
            ConstraintOperator.GTE,
            ConstraintOperator.EQ,
        ):
            if candidate in operators:
                return candidate
    if definition.value_type is ValueType.STRING and ConstraintOperator.CONTAINS in operators:
        return ConstraintOperator.CONTAINS
    return ConstraintOperator.EQ


def _display_value(field_name: str, value: Any, unit: Unit) -> str:
    if isinstance(value, bool):
        labels = {
            "sellable": ("판매 가능", "판매 불가"),
            "trading_suspended": ("거래 중지", "거래 중지 아님"),
            "pension_eligible": ("연금 거래 가능", "연금 거래 불가"),
            "core_etf": ("핵심 ETF", "핵심 ETF 아님"),
            "currently_buyable": ("현재 매수 가능", "현재 매수 불가"),
            "public_offering": ("공모", "사모"),
            "company_sellable": ("당사 판매 가능", "당사 판매 불가"),
            "currency_hedged": ("환헤지", "환노출"),
        }
        yes, no = labels.get(field_name, ("예", "아니오"))
        return yes if value else no
    if value in _VALUE_LABELS:
        return _VALUE_LABELS[value]
    if unit is Unit.PCT_POINT:
        return f"{value}%"
    if unit is Unit.DAY:
        return f"{value}일"
    if unit is Unit.YEAR:
        return f"{value}년"
    return str(value)


def _constraint_phrase(family: ProductFamily, constraint: Constraint) -> str:
    definition = load_field_registry().require_field(constraint.field, [family.value])
    values = constraint.value if isinstance(constraint.value, list) else [constraint.value]
    rendered = [_display_value(constraint.field, value, constraint.unit) for value in values]
    label = definition.label
    if constraint.field == "public_offering" and constraint.value is True:
        return "공모"
    if constraint.operator is ConstraintOperator.EQ:
        if isinstance(constraint.value, bool):
            return rendered[0]
        identity_labels = {
            "product_id": "상품 ID",
            "ticker": "티커",
            "isin": "ISIN",
        }
        return f"{identity_labels.get(constraint.field, label)} {rendered[0]}"
    if constraint.operator is ConstraintOperator.NEQ:
        return f"{label} {rendered[0]} 제외"
    if constraint.operator is ConstraintOperator.IN:
        return f"{label} {' 또는 '.join(rendered)}"
    if constraint.operator is ConstraintOperator.NOT_IN:
        return f"{label} {' 및 '.join(rendered)} 제외"
    if constraint.operator is ConstraintOperator.LT:
        return f"{label} {rendered[0]} 미만"
    if constraint.operator is ConstraintOperator.LTE:
        return f"{label} {rendered[0]} 이하"
    if constraint.operator is ConstraintOperator.GT:
        return f"{label} {rendered[0]} 초과"
    if constraint.operator is ConstraintOperator.GTE:
        return f"{label} {rendered[0]} 이상"
    if constraint.operator is ConstraintOperator.BETWEEN:
        return f"{label} {rendered[0]} 이상 {rendered[1]} 이하"
    return f"{label}에 {rendered[0]} 포함"


def render_canonical_question(plan: QueryPlan) -> str:
    family = plan.product_families[0]
    family_label = _FAMILY_LABELS[family]
    visible_constraints = [
        constraint
        for constraint in plan.constraints
        if not (constraint.field == "public_offering" and constraint.value is True)
    ]
    condition = ""
    if visible_constraints:
        condition = (
            " 중 "
            + ", ".join(
                _constraint_phrase(family, constraint) for constraint in visible_constraints
            )
            + " 조건을 만족하는 상품"
        )
    if plan.intent is Intent.COMPARE:
        ids = next(
            constraint.value for constraint in plan.constraints if constraint.field == "product_id"
        )
        assert isinstance(ids, list)
        labels = [
            load_field_registry().require_field(field, [family.value]).label
            for field in plan.intent_payload.comparison_fields
        ]
        return f"{family_label} 상품 ID {ids[0]}와 {ids[1]}의 {', '.join(labels)} 항목을 비교해줘"
    if plan.intent is Intent.AGGREGATE:
        group = ""
        if plan.intent_payload.group_by:
            labels = [
                load_field_registry().require_field(field, [family.value]).label
                for field in plan.intent_payload.group_by
            ]
            group = f"{', '.join(labels)}별 "
        metrics: list[str] = []
        function_labels = {
            AggregateFunction.COUNT: "개수",
            AggregateFunction.MIN: "최솟값",
            AggregateFunction.MAX: "최댓값",
            AggregateFunction.AVG: "평균",
            AggregateFunction.SUM: "합계",
        }
        for aggregation in plan.intent_payload.aggregations:
            if (
                aggregation.function is AggregateFunction.COUNT
                and aggregation.field == "product_id"
            ):
                metrics.append("상품 수")
            else:
                label = (
                    load_field_registry()
                    .require_field(
                        aggregation.field,
                        [family.value],
                    )
                    .label
                )
                metrics.append(f"{label} {function_labels[aggregation.function]}")
        return f"{family_label}{condition}에 대해 {group}{', '.join(metrics)} 계산해줘"
    ranking = ""
    if plan.ranking:
        phrases = []
        for item in plan.ranking:
            label = load_field_registry().require_field(item.field, [family.value]).label
            direction = "낮은 순" if item.direction is SortDirection.ASC else "높은 순"
            phrases.append(f"{label} {direction}")
        ranking = " " + ", ".join(phrases) + "으로"
    return f"{family_label}{condition}{ranking} {plan.limit}개를 조회해줘"


def _search_draft(
    connection: sqlite3.Connection,
    family: ProductFamily,
    field_name: str,
    operator: ConstraintOperator,
) -> _DraftCase:
    registry = load_field_registry()
    definition = registry.require_field(field_name, [family.value])
    sample = _sample_values(connection, family, field_name)
    value = _constraint_value(operator, definition, sample)
    target = _constraint(family, field_name, operator, value)
    amount_scope = definition.unit == "source_currency_amount" and field_name != "trading_currency"
    constraints = _merge_constraints(
        [*_base_constraints(connection, family, amount_scope=amount_scope), target]
    )
    limit = len(value) if field_name in _IDENTITY_FIELDS and isinstance(value, list) else 3
    if field_name in _IDENTITY_FIELDS and not isinstance(value, list):
        limit = 1
    cell = CoverageCell(
        key=_cell_key(
            family,
            CoverageCellKind.SEARCH_CONSTRAINT,
            field_name,
            operator=operator,
        ),
        product_family=family,
        intent=Intent.SEARCH,
        kind=CoverageCellKind.SEARCH_CONSTRAINT,
        field=field_name,
        operator=operator,
    )
    plan = QueryPlan(
        schema_version="1.0",
        question_id="draft",
        intent=Intent.SEARCH,
        product_families=[family],
        constraints=constraints,
        ranking=[],
        projection=_projection(family, field_name),
        limit=limit,
        intent_payload=_empty_payload(),
        ambiguities=[],
        unsupported_conditions=[],
    )
    return _DraftCase(cell=cell, plan=plan, canonical_question=render_canonical_question(plan))


def _ranking_draft(
    connection: sqlite3.Connection,
    family: ProductFamily,
    field_name: str,
    direction: SortDirection,
) -> _DraftCase:
    definition = load_field_registry().require_field(field_name, [family.value])
    constraints = _base_constraints(
        connection,
        family,
        amount_scope=definition.unit == "source_currency_amount",
    )
    cell = CoverageCell(
        key=_cell_key(
            family,
            CoverageCellKind.SEARCH_RANKING,
            field_name,
            direction=direction,
        ),
        product_family=family,
        intent=Intent.SEARCH,
        kind=CoverageCellKind.SEARCH_RANKING,
        field=field_name,
        direction=direction,
    )
    plan = QueryPlan(
        schema_version="1.0",
        question_id="draft",
        intent=Intent.SEARCH,
        product_families=[family],
        constraints=constraints,
        ranking=[Ranking(field=field_name, direction=direction, nulls=NullPlacement.LAST)],
        projection=_projection(family, field_name),
        limit=3,
        intent_payload=_empty_payload(),
        ambiguities=[],
        unsupported_conditions=[],
    )
    return _DraftCase(cell=cell, plan=plan, canonical_question=render_canonical_question(plan))


def _comparison_ids(
    connection: sqlite3.Connection,
    family: ProductFamily,
    field_name: str,
    definition: FieldDefinition,
) -> list[str]:
    table = TABLE_BY_FAMILY[family.value]
    sql_fields = SQL_FIELDS_BY_FAMILY[family.value]
    field_spec = sql_fields[field_name]
    clauses, parameters = _valid_where(field_spec)
    if family is ProductFamily.FUND:
        public = sql_fields["public_offering"]
        clauses.append(f"{public.column} = 1")
        if public.quality_column is not None:
            clauses.append(f"{public.quality_column} IN (?, ?)")
            parameters.extend(_QUALITY_VALUES)
    grouping_columns: list[str] = []
    if "trading_currency" in definition.comparison_scope:
        grouping_columns.append(sql_fields["trading_currency"].column)
    if "as_of" in definition.comparison_scope:
        as_of_field = (
            "dynamic_as_of" if definition.as_of_basis is AsOfBasis.DYNAMIC else "static_as_of"
        )
        grouping_columns.append(sql_fields[as_of_field].column)
    selected = ["product_id", *grouping_columns]
    rows = connection.execute(
        f"SELECT {', '.join(selected)} FROM {table} "
        f"WHERE {' AND '.join(clauses)} ORDER BY product_id ASC",
        parameters,
    ).fetchall()
    grouped: dict[tuple[Any, ...], list[str]] = {}
    for row in rows:
        key = tuple(row[column] for column in grouping_columns)
        bucket = grouped.setdefault(key, [])
        product_id = str(row["product_id"])
        if product_id not in bucket:
            bucket.append(product_id)
        if len(bucket) == 2:
            return bucket
    raise ValueError("no comparison pair satisfies the field scope")


def _comparison_draft(
    connection: sqlite3.Connection,
    family: ProductFamily,
    field_name: str,
) -> _DraftCase:
    registry = load_field_registry()
    definition = registry.require_field(field_name, [family.value])
    product_ids = _comparison_ids(connection, family, field_name, definition)
    constraints = [
        *_base_constraints(connection, family, amount_scope=False),
        _constraint(family, "product_id", ConstraintOperator.IN, product_ids),
    ]
    projection_fields = ["product_id", "product_name", field_name]
    if "trading_currency" in definition.comparison_scope:
        projection_fields.append("trading_currency")
    cell = CoverageCell(
        key=_cell_key(family, CoverageCellKind.COMPARE_FIELD, field_name),
        product_family=family,
        intent=Intent.COMPARE,
        kind=CoverageCellKind.COMPARE_FIELD,
        field=field_name,
    )
    plan = QueryPlan(
        schema_version="1.0",
        question_id="draft",
        intent=Intent.COMPARE,
        product_families=[family],
        constraints=_merge_constraints(constraints),
        ranking=[],
        projection=_projection(family, *projection_fields),
        limit=2,
        intent_payload=IntentPayload(
            comparison_fields=[field_name],
            group_by=[],
            aggregations=[],
            explain_product_ids=[],
        ),
        ambiguities=[],
        unsupported_conditions=[],
    )
    return _DraftCase(cell=cell, plan=plan, canonical_question=render_canonical_question(plan))


def _aggregate_draft(
    connection: sqlite3.Connection,
    family: ProductFamily,
    field_name: str,
    function: AggregateFunction,
    *,
    group_by: str | None = None,
) -> _DraftCase:
    registry = load_field_registry()
    definition = registry.require_field(field_name, [family.value])
    amount_scope = (
        function is not AggregateFunction.COUNT
        and definition.unit == "source_currency_amount"
        and group_by != "trading_currency"
    )
    constraints = _base_constraints(connection, family, amount_scope=amount_scope)
    kind = (
        CoverageCellKind.GROUP_BY_FIELD
        if group_by is not None
        else CoverageCellKind.AGGREGATE_FIELD
    )
    cell_field = group_by or field_name
    cell = CoverageCell(
        key=_cell_key(
            family,
            kind,
            cell_field,
            function=function,
            group_by=group_by,
        ),
        product_family=family,
        intent=Intent.AGGREGATE,
        kind=kind,
        field=cell_field,
        function=function,
        group_by=group_by,
    )
    projection_fields = [field_name, *([] if group_by is None else [group_by])]
    plan = QueryPlan(
        schema_version="1.0",
        question_id="draft",
        intent=Intent.AGGREGATE,
        product_families=[family],
        constraints=constraints,
        ranking=[],
        projection=list(dict.fromkeys(projection_fields)),
        limit=100,
        intent_payload=IntentPayload(
            comparison_fields=[],
            group_by=[] if group_by is None else [group_by],
            aggregations=[Aggregation(function=function, field=field_name)],
            explain_product_ids=[],
        ),
        ambiguities=[],
        unsupported_conditions=[],
    )
    return _DraftCase(cell=cell, plan=plan, canonical_question=render_canonical_question(plan))


def _draft_cases_for_family(
    connection: sqlite3.Connection,
    family: ProductFamily,
) -> tuple[list[_DraftCase], list[CoverageExclusion]]:
    registry = load_field_registry()
    definitions = {
        name: base.resolve(family.value)
        for name, base in registry.fields.items()
        if family.value in base.datasets
    }
    drafts: dict[str, _DraftCase] = {}
    exclusions: dict[str, CoverageExclusion] = {}

    def add(draft: _DraftCase) -> None:
        drafts.setdefault(draft.cell.key, draft)

    def attempt(cell: CoverageCell, factory: Any) -> bool:
        if cell.key in drafts:
            return True
        if cell.key in exclusions:
            return False
        try:
            add(factory())
        except Exception as error:
            exclusions[cell.key] = CoverageExclusion(
                cell=cell,
                reason_code="case_construction_error",
                reason=f"{type(error).__name__}: {error}",
            )
            return False
        return True

    def search_cell(field_name: str, operator: ConstraintOperator) -> CoverageCell:
        return CoverageCell(
            key=_cell_key(
                family,
                CoverageCellKind.SEARCH_CONSTRAINT,
                field_name,
                operator=operator,
            ),
            product_family=family,
            intent=Intent.SEARCH,
            kind=CoverageCellKind.SEARCH_CONSTRAINT,
            field=field_name,
            operator=operator,
        )

    def ranking_cell(field_name: str, direction: SortDirection) -> CoverageCell:
        return CoverageCell(
            key=_cell_key(
                family,
                CoverageCellKind.SEARCH_RANKING,
                field_name,
                direction=direction,
            ),
            product_family=family,
            intent=Intent.SEARCH,
            kind=CoverageCellKind.SEARCH_RANKING,
            field=field_name,
            direction=direction,
        )

    def comparison_cell(field_name: str) -> CoverageCell:
        return CoverageCell(
            key=_cell_key(family, CoverageCellKind.COMPARE_FIELD, field_name),
            product_family=family,
            intent=Intent.COMPARE,
            kind=CoverageCellKind.COMPARE_FIELD,
            field=field_name,
        )

    def aggregate_cell(
        field_name: str,
        function: AggregateFunction,
        *,
        group_by: str | None = None,
    ) -> CoverageCell:
        kind = (
            CoverageCellKind.GROUP_BY_FIELD
            if group_by is not None
            else CoverageCellKind.AGGREGATE_FIELD
        )
        cell_field = group_by or field_name
        return CoverageCell(
            key=_cell_key(
                family,
                kind,
                cell_field,
                function=function,
                group_by=group_by,
            ),
            product_family=family,
            intent=Intent.AGGREGATE,
            kind=kind,
            field=cell_field,
            function=function,
            group_by=group_by,
        )

    queryable = [
        (name, definition)
        for name, definition in definitions.items()
        if definition.queryable and name != "product_family"
    ]
    for field_name, definition in queryable:
        if field_name == "public_offering":
            continue
        operator = _primary_operator(definition)
        built = attempt(
            search_cell(field_name, operator),
            lambda field_name=field_name, operator=operator: _search_draft(
                connection,
                family,
                field_name,
                operator,
            ),
        )
        if not built and operator is not ConstraintOperator.EQ:
            attempt(
                search_cell(field_name, ConstraintOperator.EQ),
                lambda field_name=field_name: _search_draft(
                    connection,
                    family,
                    field_name,
                    ConstraintOperator.EQ,
                ),
            )

    covered_operators = {
        draft.cell.operator
        for draft in drafts.values()
        if draft.cell.kind is CoverageCellKind.SEARCH_CONSTRAINT
    }
    declared_operators = {
        ConstraintOperator(operator)
        for _, definition in queryable
        for operator in definition.allowed_operators
    }
    for operator in sorted(declared_operators - covered_operators, key=lambda item: item.value):
        compatible = [
            (name, definition)
            for name, definition in queryable
            if operator.value in definition.allowed_operators
            and name not in {"product_family", "public_offering"}
        ]
        compatible.sort(
            key=lambda item: (
                item[1].coverage_pct,
                item[0] not in _IDENTITY_FIELDS,
                item[0],
            ),
            reverse=True,
        )
        for field_name, _ in compatible:
            if attempt(
                search_cell(field_name, operator),
                lambda field_name=field_name, operator=operator: _search_draft(
                    connection,
                    family,
                    field_name,
                    operator,
                ),
            ):
                break

    sortable = sorted(name for name, definition in definitions.items() if definition.sortable)
    for index, field_name in enumerate(sortable):
        direction = SortDirection.ASC if index % 2 == 0 else SortDirection.DESC
        attempt(
            ranking_cell(field_name, direction),
            lambda field_name=field_name, direction=direction: _ranking_draft(
                connection,
                family,
                field_name,
                direction,
            ),
        )
    if sortable:
        observed = {draft.cell.direction for draft in drafts.values() if draft.cell.direction}
        missing_directions = sorted(
            {SortDirection.ASC, SortDirection.DESC} - observed,
            key=lambda item: item.value,
        )
        for direction in missing_directions:
            field_name = sortable[0]
            attempt(
                ranking_cell(field_name, direction),
                lambda field_name=field_name, direction=direction: _ranking_draft(
                    connection,
                    family,
                    field_name,
                    direction,
                ),
            )

    for field_name, definition in sorted(definitions.items()):
        if definition.comparable:
            attempt(
                comparison_cell(field_name),
                lambda field_name=field_name: _comparison_draft(
                    connection,
                    family,
                    field_name,
                ),
            )

    aggregatable = sorted(
        (name, definition) for name, definition in definitions.items() if definition.aggregatable
    )
    function_cycle = (
        AggregateFunction.AVG,
        AggregateFunction.MIN,
        AggregateFunction.MAX,
    )
    for index, (field_name, definition) in enumerate(aggregatable):
        function = (
            AggregateFunction.SUM
            if definition.unit in _ADDITIVE_UNITS and index % 4 == 3
            else function_cycle[index % len(function_cycle)]
        )
        attempt(
            aggregate_cell(field_name, function),
            lambda field_name=field_name, function=function: _aggregate_draft(
                connection,
                family,
                field_name,
                function,
            ),
        )
    attempt(
        aggregate_cell("product_id", AggregateFunction.COUNT),
        lambda: _aggregate_draft(
            connection,
            family,
            "product_id",
            AggregateFunction.COUNT,
        ),
    )

    covered_functions = {
        draft.cell.function
        for draft in drafts.values()
        if draft.cell.kind is CoverageCellKind.AGGREGATE_FIELD
    }
    target_functions = {
        AggregateFunction.COUNT,
        AggregateFunction.MIN,
        AggregateFunction.MAX,
        AggregateFunction.AVG,
    }
    if any(definition.unit in _ADDITIVE_UNITS for _, definition in aggregatable):
        target_functions.add(AggregateFunction.SUM)
    for function in sorted(target_functions - covered_functions, key=lambda item: item.value):
        if function is AggregateFunction.COUNT:
            attempt(
                aggregate_cell("product_id", function),
                lambda function=function: _aggregate_draft(
                    connection,
                    family,
                    "product_id",
                    function,
                ),
            )
            continue
        compatible = [
            (name, definition)
            for name, definition in aggregatable
            if function is not AggregateFunction.SUM or definition.unit in _ADDITIVE_UNITS
        ]
        if compatible:
            field_name = compatible[0][0]
            attempt(
                aggregate_cell(field_name, function),
                lambda field_name=field_name, function=function: _aggregate_draft(
                    connection,
                    family,
                    field_name,
                    function,
                ),
            )

    group_fields = sorted(
        name
        for name, definition in definitions.items()
        if name not in _GROUP_BY_BLOCKED_FIELDS
        and definition.selectable
        and definition.value_type in {ValueType.ENUM, ValueType.BOOLEAN}
    )
    for group_field in group_fields:
        attempt(
            aggregate_cell(
                "product_id",
                AggregateFunction.COUNT,
                group_by=group_field,
            ),
            lambda group_field=group_field: _aggregate_draft(
                connection,
                family,
                "product_id",
                AggregateFunction.COUNT,
                group_by=group_field,
            ),
        )
    return list(drafts.values()), list(exclusions.values())


def _outcome_payload(
    *,
    candidate_count: int,
    products: Sequence[BaseModel],
    comparisons: Sequence[BaseModel],
    aggregates: Sequence[BaseModel],
    source_manifest: BaseModel,
) -> dict[str, Any]:
    return {
        "candidate_count": candidate_count,
        "products": [item.model_dump(mode="json") for item in products],
        "comparisons": [item.model_dump(mode="json") for item in comparisons],
        "aggregates": [item.model_dump(mode="json") for item in aggregates],
        "source_manifest": source_manifest.model_dump(mode="json"),
    }


def execute_coverage_plan(
    plan: QueryPlan,
    database_path: Path,
    *,
    record_cache: RecordSnapshotCache,
) -> CoverageOutcome:
    started = time.perf_counter()
    products: list[BaseModel] = []
    comparisons: list[BaseModel] = []
    aggregates: list[BaseModel] = []
    if plan.intent is Intent.AGGREGATE:
        require_internal_evaluation_aggregation(plan)
        validated_plan = authorize_internal_evaluation_plan(plan, database_path)
        plan = validated_plan.canonical_plan
        executed = SQLiteAggregateOracle(database_path).execute(validated_plan)
        verified = AggregateResultVerifier().verify(
            plan,
            executed,
            record_cache.get(database_path).records,
        )
        aggregates = list(build_aggregate_evidence(plan, verified))
        candidate_count = verified.candidate_count
        manifest = verified.manifest
    else:
        if plan.intent is Intent.COMPARE:
            require_internal_evaluation_comparison(plan)
        else:
            require_internal_evaluation_search(plan)
        validated_plan = authorize_internal_evaluation_plan(plan, database_path)
        plan = validated_plan.canonical_plan
        executed_search = SQLiteOracle(database_path).execute(validated_plan)
        universe = (
            None if plan.intent is Intent.COMPARE else record_cache.get(database_path).records
        )
        verified_search = ResultVerifier().verify(plan, executed_search, universe)
        products = list(build_product_evidence(plan, verified_search))
        if plan.intent is Intent.COMPARE:
            comparison = build_product_comparison(plan, verified_search, products)
            products = list(comparison.products)
            comparisons = list(build_comparison_evidence(comparison))
            verified_search = comparison.verified
        candidate_count = verified_search.candidate_count
        manifest = verified_search.manifest
    evidence_payload = _outcome_payload(
        candidate_count=candidate_count,
        products=products,
        comparisons=comparisons,
        aggregates=aggregates,
        source_manifest=manifest,
    )
    evidence_sha = canonical_json_sha256(evidence_payload)
    plan_sha = query_plan_semantic_sha256(plan)
    assert plan_sha is not None
    return CoverageOutcome(
        candidate_count=candidate_count,
        returned_product_ids=[str(item.product_id) for item in products],
        product_evidence_count=len(products),
        comparison_evidence_count=len(comparisons),
        aggregate_evidence_count=len(aggregates),
        query_plan_semantic_sha256=plan_sha,
        evidence_semantic_sha256=evidence_sha,
        system_semantic_sha256=canonical_json_sha256(
            {
                "query_plan": plan_sha,
                "evidence": evidence_sha,
            }
        ),
        source_dataset=manifest.dataset,
        source_snapshot_date=manifest.source_snapshot_date.isoformat(),
        latency_ms=round((time.perf_counter() - started) * 1000, 3),
    )


def _summary(
    cases: Sequence[CoveragePlanCase],
    exclusions: Sequence[CoverageExclusion],
) -> CoveragePlanSummary:
    attempted = len(cases) + len(exclusions)
    return CoveragePlanSummary(
        attempted_cells=attempted,
        executable_cases=len(cases),
        excluded_cells=len(exclusions),
        execution_rate=round(len(cases) / attempted, 6),
        by_family=dict(sorted(Counter(case.cell.product_family.value for case in cases).items())),
        by_kind=dict(sorted(Counter(case.cell.kind.value for case in cases).items())),
        by_operator=dict(
            sorted(
                Counter(
                    case.cell.operator.value for case in cases if case.cell.operator is not None
                ).items()
            )
        ),
        by_direction=dict(
            sorted(
                Counter(
                    case.cell.direction.value for case in cases if case.cell.direction is not None
                ).items()
            )
        ),
        by_function=dict(
            sorted(
                Counter(
                    case.cell.function.value for case in cases if case.cell.function is not None
                ).items()
            )
        ),
        exclusion_reasons=dict(sorted(Counter(item.reason_code for item in exclusions).items())),
    )


def generate_coverage_plan_suite(
    database_paths: Mapping[ProductFamily | str, str | Path],
    *,
    generated_at_utc: str | None = None,
) -> CoveragePlanSuite:
    paths = _normalized_paths(database_paths)
    record_cache = RecordSnapshotCache(max_entries=4)
    cases: list[CoveragePlanCase] = []
    exclusions: list[CoverageExclusion] = []
    case_index = 0
    for family in ProductFamily:
        with connect_read_only(paths[family]) as connection:
            try:
                drafts, draft_exclusions = _draft_cases_for_family(connection, family)
            except Exception as error:
                raise RuntimeError(
                    f"coverage draft generation failed for {family.value}"
                ) from error
        exclusions.extend(draft_exclusions)
        for draft in drafts:
            case_index += 1
            case_id = f"{_SUITE_ID}-{case_index:04d}"
            plan = draft.plan.model_copy(update={"question_id": case_id})
            question = render_canonical_question(plan)
            try:
                outcome = execute_coverage_plan(
                    plan,
                    paths[family],
                    record_cache=record_cache,
                )
            except Exception as error:
                exclusions.append(
                    CoverageExclusion(
                        cell=draft.cell,
                        reason_code="direct_execution_error",
                        reason=f"{type(error).__name__}: {error}",
                    )
                )
                case_index -= 1
                continue
            cases.append(
                CoveragePlanCase(
                    id=case_id,
                    cell=draft.cell,
                    canonical_question=question,
                    plan=plan,
                    outcome=outcome,
                )
            )
    generated_at = generated_at_utc or datetime.now(UTC).replace(microsecond=0).isoformat()
    registry = load_field_registry()
    return CoveragePlanSuite(
        generated_at_utc=generated_at,
        registry_schema_version=registry.schema_version,
        registry_sha256=_field_registry_sha256(),
        database_sha256_by_family={
            family.value: _sha256(paths[family]) for family in ProductFamily
        },
        selection_contract={
            "search": "one primary case per queryable field plus missing operator coverage",
            "ranking": "one deterministic direction per sortable field plus both directions",
            "compare": "one same-scope product pair per comparable field",
            "aggregate": "one valid function per aggregatable field plus all function classes",
            "group_by": "COUNT(product_id) for every non-identity enum or boolean group field",
        },
        summary=_summary(cases, exclusions),
        cases=cases,
        exclusions=exclusions,
        interpretation_limits=[
            "field registry와 제공 데이터에서 자동 생성한 내부 synthetic 개발 세트다.",
            "질문 문장은 규칙 기반 canonical 표현이며 실제 사용자 분포를 대표하지 않는다.",
            "직접 QueryPlan 실행 성공은 자연어 이해 성능이나 HyperCLOVA X 성능이 아니다.",
            "실패를 본 뒤 수정한 결과는 최초 관측과 분리해 보존해야 한다.",
            "공모펀드는 내부 평가 계약에서만 실행하며 공식 제출 승인과 구분한다.",
            f"canonical question renderer version: {_CANONICAL_RENDERER_VERSION}",
        ],
    )


def coverage_plan_suite_semantic_sha256(suite: CoveragePlanSuite) -> str:
    payload = suite.model_dump(mode="json")
    payload.pop("generated_at_utc", None)
    for case in payload["cases"]:
        case["outcome"].pop("latency_ms", None)
    return canonical_json_sha256(payload)


def rerender_coverage_plan_suite(
    suite: CoveragePlanSuite,
    *,
    generated_at_utc: str | None = None,
) -> CoveragePlanSuite:
    """Refresh only canonical wording while preserving frozen plans and direct outcomes."""

    cases = [
        case.model_copy(update={"canonical_question": render_canonical_question(case.plan)})
        for case in suite.cases
    ]
    limits = [
        item
        for item in suite.interpretation_limits
        if not item.startswith("canonical question renderer version:")
    ]
    timestamp = generated_at_utc or datetime.now(UTC).replace(microsecond=0).isoformat()
    return suite.model_copy(
        update={
            "generated_at_utc": timestamp,
            "cases": cases,
            "interpretation_limits": [
                *limits,
                f"canonical question renderer version: {_CANONICAL_RENDERER_VERSION}",
            ],
        }
    )
