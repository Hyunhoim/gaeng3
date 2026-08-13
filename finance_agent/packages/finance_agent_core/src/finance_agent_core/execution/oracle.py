from __future__ import annotations

import sqlite3
from decimal import Decimal
from pathlib import Path
from time import perf_counter

from finance_agent_core.contracts.queryplan import (
    Constraint,
    ConstraintOperator,
    Intent,
    NullPlacement,
    ProductFamily,
    QueryPlan,
)
from finance_agent_core.contracts.routing import RouteDisposition
from finance_agent_core.domain import ExecutedAggregation, ExecutedSearch
from finance_agent_core.execution.aggregation import aggregate_records
from finance_agent_core.execution.authority import (
    ValidatedPlan,
    open_validated_database,
    require_candidate_budget,
)
from finance_agent_core.execution.policy import (
    require_aggregate_contract,
    require_comparison_contract,
    require_fund_aum_currency_scope,
    require_fund_public_scope,
)
from finance_agent_core.execution.sql_schema import (
    SQL_FIELDS_BY_FAMILY,
    TABLE_BY_FAMILY,
    SqlField,
)
from finance_agent_core.execution.verifier_projection import (
    project_verifier_rows,
    verifier_projection_fields,
    verifier_select_columns,
)
from finance_agent_core.observability import AuditOutcome, AuditStage, current_request_audit
from finance_agent_core.storage.sqlite import row_to_record

ORACLE_SUPPORTED_INTENTS = frozenset({Intent.SEARCH, Intent.COMPARE, Intent.AGGREGATE})
ORACLE_COMPARABLE_FAMILIES = frozenset(ProductFamily)
ORACLE_AGGREGATABLE_FAMILIES = frozenset(ProductFamily)


def _receipt_audit_fields(validated_plan: ValidatedPlan) -> dict[str, object]:
    receipt = validated_plan.receipt
    fields: dict[str, object] = {
        "route_disposition": RouteDisposition.EXECUTE,
        "interaction_intent": receipt.capability_interaction_intent,
        "product_families": (receipt.dataset,),
        "plan_sha256": receipt.plan_sha256,
    }
    if receipt.approved_manifest_sha256 is not None:
        fields.update(
            dataset_release_id=receipt.dataset_release_id,
            approved_dataset_manifest_sha256=receipt.approved_manifest_sha256,
            database_manifest_sha256=receipt.database_manifest_sha256,
            database_snapshot_sha256=receipt.database_sha256,
            source_snapshot_sha256=receipt.source_file_sha256,
        )
    return fields


def _scaled_parameter(value: object, scale: Decimal | None) -> str | int | bool:
    if scale is None:
        if isinstance(value, (str, bool, int)):
            return value
        if isinstance(value, float):
            return str(value)
        raise TypeError(f"unsupported SQL parameter: {value!r}")
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError(f"scaled SQL parameter must be numeric: {value!r}")
    scaled = Decimal(str(value)) * scale
    if scaled != scaled.to_integral_value():
        raise ValueError(f"query value exceeds supported precision: {value}")
    return int(scaled)


def _escape_like(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _constraint_sql(
    constraint: Constraint,
    sql_fields: dict[str, SqlField],
) -> tuple[str, list[str | int | bool]]:
    spec = sql_fields[constraint.field]
    column = spec.column
    clauses: list[str] = []
    parameters: list[str | int | bool] = []
    if spec.quality_column is not None:
        clauses.append(f"{spec.quality_column} IN ('VALID', 'PARTIAL')")

    operator = constraint.operator
    value = constraint.value
    if operator in {ConstraintOperator.IN, ConstraintOperator.NOT_IN}:
        assert isinstance(value, list)
        placeholders = ", ".join("?" for _ in value)
        sql_operator = "IN" if operator is ConstraintOperator.IN else "NOT IN"
        clauses.append(f"{column} {sql_operator} ({placeholders})")
        parameters.extend(_scaled_parameter(item, spec.scale) for item in value)
    elif operator is ConstraintOperator.BETWEEN:
        assert isinstance(value, list)
        clauses.append(f"{column} BETWEEN ? AND ?")
        parameters.extend(_scaled_parameter(item, spec.scale) for item in value)
    elif operator is ConstraintOperator.CONTAINS:
        assert isinstance(value, str)
        clauses.append(f"LOWER({column}) LIKE LOWER(?) ESCAPE '\\'")
        parameters.append(f"%{_escape_like(value)}%")
    else:
        assert not isinstance(value, list)
        sql_operators = {
            ConstraintOperator.EQ: "=",
            ConstraintOperator.NEQ: "<>",
            ConstraintOperator.LT: "<",
            ConstraintOperator.LTE: "<=",
            ConstraintOperator.GT: ">",
            ConstraintOperator.GTE: ">=",
        }
        clauses.append(f"{column} {sql_operators[operator]} ?")
        parameters.append(_scaled_parameter(value, spec.scale))
    return " AND ".join(clauses), parameters


def _ranking_expression(field_name: str, sql_fields: dict[str, SqlField]) -> str:
    spec = sql_fields[field_name]
    if spec.quality_column is None:
        return spec.column
    return (
        f"CASE WHEN {spec.quality_column} IN ('VALID', 'PARTIAL') THEN {spec.column} ELSE NULL END"
    )


def compile_search_sql(
    plan: QueryPlan,
) -> tuple[str, str, list[str | int | bool]]:
    if plan.intent not in {Intent.SEARCH, Intent.COMPARE}:
        raise ValueError("the deterministic oracle supports search and compare intents only")
    if len(plan.product_families) != 1:
        raise ValueError("the deterministic oracle requires exactly one product family")
    if plan.intent is Intent.COMPARE:
        if plan.product_families[0] not in ORACLE_COMPARABLE_FAMILIES:
            raise ValueError("product family is outside the comparison Oracle policy")
        require_comparison_contract(plan)
    else:
        require_fund_public_scope(plan)
        require_fund_aum_currency_scope(plan)
    family = plan.product_families[0].value
    try:
        sql_fields = SQL_FIELDS_BY_FAMILY[family]
        table = TABLE_BY_FAMILY[family]
    except KeyError as error:
        raise ValueError(f"no deterministic SQL oracle for product family: {family}") from error
    where_clauses = ["is_quarantined = 0"]
    parameters: list[str | int | bool] = []
    for constraint in plan.constraints:
        clause, values = _constraint_sql(constraint, sql_fields)
        where_clauses.append(f"({clause})")
        parameters.extend(values)
    where_sql = " AND ".join(where_clauses)

    order_by: list[str] = []
    for ranking in plan.ranking:
        expression = _ranking_expression(ranking.field, sql_fields)
        null_order = 0 if ranking.nulls is NullPlacement.FIRST else 1
        nonnull_order = 1 - null_order
        order_by.append(
            f"CASE WHEN {expression} IS NULL THEN {null_order} ELSE {nonnull_order} END ASC"
        )
        order_by.append(f"{expression} {ranking.direction.value.upper()}")
    order_by.append("product_id ASC")

    select_sql = f"SELECT * FROM {table} WHERE {where_sql} ORDER BY {', '.join(order_by)} LIMIT ?"
    count_sql = f"SELECT COUNT(*) AS candidate_count FROM {table} WHERE {where_sql}"
    return select_sql, count_sql, parameters


def compile_aggregate_sql(
    plan: QueryPlan,
) -> tuple[str, list[str | int | bool]]:
    if plan.intent is not Intent.AGGREGATE:
        raise ValueError("the aggregate Oracle requires aggregate intent")
    if len(plan.product_families) != 1:
        raise ValueError("the aggregate Oracle requires exactly one product family")
    if plan.product_families[0] not in ORACLE_AGGREGATABLE_FAMILIES:
        raise ValueError("product family is outside the aggregate Oracle policy")
    require_aggregate_contract(plan)
    family = plan.product_families[0].value
    try:
        sql_fields = SQL_FIELDS_BY_FAMILY[family]
        table = TABLE_BY_FAMILY[family]
    except KeyError as error:
        raise ValueError(
            f"no deterministic aggregate Oracle for product family: {family}"
        ) from error
    where_clauses = ["is_quarantined = 0"]
    parameters: list[str | int | bool] = []
    for constraint in plan.constraints:
        clause, values = _constraint_sql(constraint, sql_fields)
        where_clauses.append(f"({clause})")
        parameters.extend(values)
    where_sql = " AND ".join(where_clauses)
    fields = verifier_projection_fields(plan)
    select_columns = verifier_select_columns(family, fields)
    return (
        f"SELECT {', '.join(select_columns)} FROM {table} "
        f"WHERE {where_sql} ORDER BY product_id ASC",
        parameters,
    )


class SQLiteOracle:
    def __init__(self, database_path: str | Path) -> None:
        self.database_path = Path(database_path)

    def execute(self, validated_plan: ValidatedPlan) -> ExecutedSearch:
        started = perf_counter()
        audit = current_request_audit()
        try:
            with open_validated_database(
                validated_plan,
                self.database_path,
                oracle_kind="search",
            ) as (plan, connection, manifest):
                select_sql, count_sql, parameters = compile_search_sql(plan)
                count_row = connection.execute(count_sql, parameters).fetchone()
                if count_row is None:
                    raise sqlite3.DatabaseError("candidate count query returned no row")
                candidate_count = int(count_row["candidate_count"])
                require_candidate_budget(validated_plan, candidate_count)
                rows = connection.execute(
                    select_sql,
                    [*parameters, plan.limit],
                ).fetchall()
        except Exception:
            if audit is not None:
                audit.emit(
                    stage=AuditStage.SQL,
                    outcome=AuditOutcome.FAILED,
                    reason_code="statement_failed",
                    duration_ms=(perf_counter() - started) * 1000,
                    **_receipt_audit_fields(validated_plan),
                )
            raise
        if audit is not None:
            audit.emit(
                stage=AuditStage.SQL,
                outcome=AuditOutcome.SUCCEEDED,
                reason_code="parameterized_statement_completed",
                duration_ms=(perf_counter() - started) * 1000,
                candidate_count=candidate_count,
                result_count=len(rows),
                **_receipt_audit_fields(validated_plan),
            )
        return ExecutedSearch(
            question_id=plan.question_id,
            candidate_count=candidate_count,
            records=[row_to_record(row) for row in rows],
            manifest=manifest,
            sql_template=select_sql,
            sql_parameters=[*parameters, plan.limit],
        )


class SQLiteAggregateOracle:
    """Select aggregate candidates in SQLite and reduce them with exact Decimal math."""

    def __init__(self, database_path: str | Path) -> None:
        self.database_path = Path(database_path)

    def execute(self, validated_plan: ValidatedPlan) -> ExecutedAggregation:
        started = perf_counter()
        audit = current_request_audit()
        try:
            with open_validated_database(
                validated_plan,
                self.database_path,
                oracle_kind="aggregate",
            ) as (plan, connection, manifest):
                select_sql, parameters = compile_aggregate_sql(plan)
                bounded_sql = f"{select_sql} LIMIT ?"
                bounded_parameters = [
                    *parameters,
                    validated_plan.receipt.max_candidate_rows + 1,
                ]
                rows = connection.execute(bounded_sql, bounded_parameters).fetchall()
        except Exception:
            if audit is not None:
                audit.emit(
                    stage=AuditStage.SQL,
                    outcome=AuditOutcome.FAILED,
                    reason_code="statement_failed",
                    duration_ms=(perf_counter() - started) * 1000,
                    **_receipt_audit_fields(validated_plan),
                )
            raise
        if audit is not None:
            audit.emit(
                stage=AuditStage.SQL,
                outcome=AuditOutcome.SUCCEEDED,
                reason_code="parameterized_statement_completed",
                duration_ms=(perf_counter() - started) * 1000,
                candidate_count=len(rows),
                result_count=0,
                **_receipt_audit_fields(validated_plan),
            )
        require_candidate_budget(validated_plan, len(rows))
        family = plan.product_families[0].value
        records = project_verifier_rows(
            rows,
            family=family,
            fields=verifier_projection_fields(plan),
        )
        total_group_count, groups = aggregate_records(plan, records)
        return ExecutedAggregation(
            question_id=plan.question_id,
            candidate_count=len(records),
            total_group_count=total_group_count,
            groups=groups,
            manifest=manifest,
            sql_template=bounded_sql,
            sql_parameters=bounded_parameters,
        )
