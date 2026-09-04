from __future__ import annotations

import sqlite3
from decimal import Decimal
from pathlib import Path
from time import perf_counter

from finance_agent_core.contracts.queryplan import (
    AggregateFunction,
    Constraint,
    ConstraintExpression,
    ConstraintOperator,
    Intent,
    LogicalOperator,
    NullPlacement,
    ProductFamily,
    QueryPlan,
    constraint_key,
)
from finance_agent_core.contracts.routing import RouteDisposition
from finance_agent_core.domain import ExecutedAggregation, ExecutedSearch
from finance_agent_core.execution.aggregation import aggregate_records
from finance_agent_core.execution.authority import (
    ValidatedPlan,
    candidate_row_budget,
    open_validated_database,
    require_candidate_budget,
)
from finance_agent_core.execution.policy import (
    ranking_requires_usable_value,
    require_aggregate_contract,
    require_comparison_contract,
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


def _scaled_parameter(
    value: object,
    scale: Decimal | None,
    *,
    usable_numeric: bool = False,
) -> str | int | float | bool:
    if scale is None:
        if usable_numeric:
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise TypeError(f"numeric SQL parameter must be numeric: {value!r}")
            # Source-only numeric fields are projected through CAST(... AS REAL).
            # Binding a float as text makes SQLite compare storage classes when
            # the left-hand side is an expression without column affinity:
            # every REAL is smaller than every TEXT.  Preserve numeric binding
            # so SQL and the independent Decimal verifier evaluate the same
            # threshold.  The source expression is rounded to eight decimal
            # places by ``_source_number`` before this comparison.
            return value
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
) -> tuple[str, list[str | int | float | bool]]:
    spec = sql_fields[constraint.field]
    column = spec.column
    clauses: list[str] = []
    parameters: list[str | int | float | bool] = []
    operator = constraint.operator

    if operator in {
        ConstraintOperator.IS_PRESENT,
        ConstraintOperator.IS_MISSING,
    }:
        present_clauses: list[str] = []
        missing_clauses: list[str] = []
        if spec.quality_column is not None:
            present_clauses.append(f"{spec.quality_column} IN ('VALID', 'PARTIAL')")
            missing_clauses.append(
                f"COALESCE({spec.quality_column}, '') NOT IN ('VALID', 'PARTIAL')"
            )
        present_clauses.append(f"{column} IS NOT NULL")
        missing_clauses.append(f"{column} IS NULL")
        if spec.scale is not None or spec.usable_numeric:
            # Official 2026-08-26 policy: zero is not an available numeric
            # observation for filtering, ranking, or aggregation.
            present_clauses.append(f"{column} <> 0")
            missing_clauses.append(f"{column} = 0")
        else:
            present_clauses.append(f"TRIM(CAST({column} AS TEXT)) <> ''")
            missing_clauses.append(f"TRIM(CAST({column} AS TEXT)) = ''")
        if operator is ConstraintOperator.IS_PRESENT:
            return " AND ".join(present_clauses), parameters
        return f"({' OR '.join(missing_clauses)})", parameters

    if spec.quality_column is not None:
        clauses.append(f"{spec.quality_column} IN ('VALID', 'PARTIAL')")
    if spec.scale is not None or spec.usable_numeric:
        # Official 2026-08-26 evaluation rule: a numeric 0 or missing value is
        # not a usable filter/ranking basis.  Keep the source value intact in
        # storage, but exclude it at deterministic query time. ``remaining_days``
        # is server-derived from two dates rather than a source numeric field;
        # zero means "matures on the snapshot date" and is a real value.
        clauses.append(f"{column} IS NOT NULL")
        if constraint.field != "remaining_days":
            clauses.append(f"{column} <> 0")

    value = constraint.value

    def canonical_parameter(item: object) -> object:
        if constraint.field == "product_id" and spec.column == "ticker" and isinstance(item, str):
            return item.split(":", 1)[0]
        return item

    if operator in {ConstraintOperator.IN, ConstraintOperator.NOT_IN}:
        assert isinstance(value, list)
        placeholders = ", ".join("?" for _ in value)
        sql_operator = "IN" if operator is ConstraintOperator.IN else "NOT IN"
        clauses.append(f"{column} {sql_operator} ({placeholders})")
        parameters.extend(
            _scaled_parameter(
                canonical_parameter(item),
                spec.scale,
                usable_numeric=spec.usable_numeric,
            )
            for item in value
        )
    elif operator is ConstraintOperator.BETWEEN:
        assert isinstance(value, list)
        clauses.append(f"{column} BETWEEN ? AND ?")
        parameters.extend(
            _scaled_parameter(
                item,
                spec.scale,
                usable_numeric=spec.usable_numeric,
            )
            for item in value
        )
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
        parameters.append(
            _scaled_parameter(
                canonical_parameter(value),
                spec.scale,
                usable_numeric=spec.usable_numeric,
            )
        )
    return " AND ".join(clauses), parameters


def _plan_constraint_sql(
    plan: QueryPlan,
    sql_fields: dict[str, SqlField],
) -> tuple[list[str], list[str | int | float | bool]]:
    """Compile the server-owned Boolean tree without changing legacy AND plans."""

    expression = plan.constraint_expression
    if expression is None:
        clauses: list[str] = []
        parameters: list[str | int | float | bool] = []
        for constraint in plan.constraints:
            clause, values = _constraint_sql(constraint, sql_fields)
            clauses.append(f"({clause})")
            parameters.extend(values)
        return clauses, parameters

    constraints_by_key = {constraint_key(item): item for item in plan.constraints}

    def referenced_keys(node: ConstraintExpression) -> set[str]:
        keys = set(node.constraint_keys)
        for child in node.children:
            keys.update(referenced_keys(child))
        return keys

    def compile_expression(
        node: ConstraintExpression,
    ) -> tuple[str, list[str | int | float | bool]]:
        node_clauses: list[str] = []
        node_parameters: list[str | int | float | bool] = []
        for key in node.constraint_keys:
            clause, values = _constraint_sql(constraints_by_key[key], sql_fields)
            node_clauses.append(f"({clause})")
            node_parameters.extend(values)
        for child in node.children:
            clause, values = compile_expression(child)
            node_clauses.append(f"({clause})")
            node_parameters.extend(values)
        connective = " AND " if node.operator is LogicalOperator.ALL else " OR "
        return connective.join(node_clauses), node_parameters

    logical_keys = referenced_keys(expression)
    clauses = []
    parameters = []
    for constraint in plan.constraints:
        if constraint_key(constraint) in logical_keys:
            continue
        clause, values = _constraint_sql(constraint, sql_fields)
        clauses.append(f"({clause})")
        parameters.extend(values)
    expression_clause, expression_parameters = compile_expression(expression)
    clauses.append(f"({expression_clause})")
    parameters.extend(expression_parameters)
    return clauses, parameters


def _ranking_expression(field_name: str, sql_fields: dict[str, SqlField]) -> str:
    spec = sql_fields[field_name]
    if field_name == "risk_level":
        normalized = f"REPLACE(COALESCE({spec.column}, ''), ' ', '')"
        # Lower numeric value means lower *semantic risk*, independent of the
        # organizer's inverted grade number (1 = highest risk, 6 = lowest).
        # The source label remains untouched in projection and evidence.
        return (
            f"CASE {normalized} "
            "WHEN '매우낮은위험(6등급)' THEN 1 WHEN '매우낮은위험' THEN 1 "
            "WHEN '낮은위험(5등급)' THEN 2 WHEN '낮은위험' THEN 2 "
            "WHEN '보통위험(4등급)' THEN 3 WHEN '보통위험' THEN 3 "
            "WHEN '다소높은위험(3등급)' THEN 4 WHEN '다소높은위험' THEN 4 "
            "WHEN '높은위험(2등급)' THEN 5 WHEN '높은위험' THEN 5 "
            "WHEN '매우높은위험(1등급)' THEN 6 WHEN '매우높은위험' THEN 6 "
            "ELSE NULL END"
        )
    if spec.quality_column is None:
        return spec.column
    return (
        f"CASE WHEN {spec.quality_column} IN ('VALID', 'PARTIAL') THEN {spec.column} ELSE NULL END"
    )


def _aggregate_preference_fields(plan: QueryPlan) -> tuple[str, ...]:
    """Fields whose availability should select a bond's representative row.

    One organizer bond ticker may have multiple physical venue rows.  An
    aggregate over a requested metric must not discard a usable value merely
    because the chronologically first venue row has that metric missing.  The
    candidate remains one logical ticker; this only chooses its best-supported
    physical row, with the source-defined physical ID and source order as
    deterministic tie-breaks.
    """

    return tuple(
        dict.fromkeys(
            [
                *plan.intent_payload.group_by,
                *(
                    item.field
                    for item in plan.intent_payload.aggregations
                    if item.field != "product_id"
                ),
            ]
        )
    )


def _sql_usable_value(spec: SqlField, field_name: str) -> str:
    clauses: list[str] = []
    if spec.quality_column is not None:
        clauses.append(f"{spec.quality_column} IN ('VALID', 'PARTIAL', 'STALE')")
    clauses.append(f"{spec.column} IS NOT NULL")
    if (spec.scale is not None or spec.usable_numeric) and field_name != "remaining_days":
        clauses.append(f"{spec.column} <> 0")
    return " AND ".join(clauses)


def compile_search_sql(
    plan: QueryPlan,
) -> tuple[str, str, list[str | int | float | bool]]:
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
    family = plan.product_families[0].value
    try:
        sql_fields = SQL_FIELDS_BY_FAMILY[family]
        table = TABLE_BY_FAMILY[family]
    except KeyError as error:
        raise ValueError(f"no deterministic SQL oracle for product family: {family}") from error
    where_clauses = ["is_quarantined = 0"]
    parameters: list[str | int | float | bool] = []
    # The organizer invalidated BUYABLE_QUANTITY and supplied no dedicated
    # delisting/listing-end field.  Maturity is a separate product attribute,
    # not an approved proxy for listing status, so the Runtime must not invent
    # an additional availability filter from it.
    constraint_clauses, constraint_parameters = _plan_constraint_sql(plan, sql_fields)
    where_clauses.extend(constraint_clauses)
    parameters.extend(constraint_parameters)
    for ranking in plan.ranking:
        if not ranking_requires_usable_value(plan, ranking.field):
            continue
        spec = sql_fields[ranking.field]
        if spec.quality_column is not None:
            where_clauses.append(f"{spec.quality_column} IN ('VALID', 'PARTIAL')")
        where_clauses.append(f"{spec.column} IS NOT NULL")
        if ranking.field != "remaining_days":
            where_clauses.append(f"{spec.column} <> 0")
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
    # Bond and overseas rows use an internal composite storage ID. Official
    # evaluation tie-breaks by the public product identifier, not by exchange,
    # market or snapshot suffixes embedded in that storage key.
    order_by.append("ticker ASC" if family in {"bond", "overseas_etp"} else "product_id ASC")

    if family == "bond":
        # A bond ticker can have more than one venue row.  Apply every locked
        # condition first, then select the smallest qualifying source row per
        # organizer product ID.  Selecting a row before filtering can lose a
        # valid product merely because another venue row sorts first.
        filtered_cte = f"filtered AS (SELECT * FROM {table} WHERE {where_sql})"
        filtered = f"WITH {filtered_cte} "
        if plan.intent is Intent.COMPARE:
            usable_score = " + ".join(
                f"CASE WHEN {_sql_usable_value(sql_fields[field], field)} "
                "THEN 1 ELSE 0 END"
                for field in plan.intent_payload.comparison_fields
            ) or "0"
            ranked = (
                "ranked AS (SELECT *, ROW_NUMBER() OVER (PARTITION BY ticker "
                f"ORDER BY ({usable_score}) DESC, product_id ASC, source_row ASC) "
                "AS _logical_rank "
                "FROM filtered) "
            )
            select_sql = (
                f"WITH {filtered_cte}, {ranked}SELECT * FROM ranked "
                f"WHERE _logical_rank = 1 ORDER BY {', '.join(order_by)} LIMIT ?"
            )
            count_sql = (
                f"WITH {filtered_cte}, {ranked}SELECT COUNT(*) AS candidate_count "
                "FROM ranked WHERE _logical_rank = 1"
            )
            return select_sql, count_sql, parameters
        logical_scope = "source_row IN (SELECT MIN(source_row) FROM filtered GROUP BY ticker)"
        select_sql = (
            f"{filtered}SELECT * FROM filtered WHERE {logical_scope} "
            f"ORDER BY {', '.join(order_by)} LIMIT ?"
        )
        count_sql = (
            f"{filtered}SELECT COUNT(*) AS candidate_count FROM filtered "
            f"WHERE {logical_scope}"
        )
    else:
        select_sql = (
            f"SELECT * FROM {table} WHERE {where_sql} "
            f"ORDER BY {', '.join(order_by)} LIMIT ?"
        )
        count_sql = f"SELECT COUNT(*) AS candidate_count FROM {table} WHERE {where_sql}"
    return select_sql, count_sql, parameters


def compile_aggregate_sql(
    plan: QueryPlan,
) -> tuple[str, list[str | int | float | bool]]:
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
    parameters: list[str | int | float | bool] = []
    constraint_clauses, constraint_parameters = _plan_constraint_sql(plan, sql_fields)
    where_clauses.extend(constraint_clauses)
    parameters.extend(constraint_parameters)
    where_sql = " AND ".join(where_clauses)
    fields = verifier_projection_fields(plan)
    select_columns = verifier_select_columns(family, fields)
    # Every COUNT measure describes organizer rows, including COUNT(field)
    # availability summaries.  Do not collapse venue rows by ticker for those
    # counts; numeric reductions still select one deterministic logical bond.
    physical_bond_count = family == "bond" and all(
        item.function is AggregateFunction.COUNT
        for item in plan.intent_payload.aggregations
    )
    if family == "bond" and not physical_bond_count:
        preference_fields = _aggregate_preference_fields(plan)
        usable_score = " + ".join(
            f"CASE WHEN {_sql_usable_value(sql_fields[field], field)} THEN 1 ELSE 0 END"
            for field in preference_fields
        ) or "0"
        filtered = f"WITH filtered AS (SELECT * FROM {table} WHERE {where_sql}), "
        ranked = (
            "ranked AS (SELECT *, ROW_NUMBER() OVER (PARTITION BY ticker "
            f"ORDER BY ({usable_score}) DESC, product_id ASC, source_row ASC) "
            "AS _logical_rank "
            "FROM filtered) "
        )
        return (
            f"{filtered}{ranked}SELECT {', '.join(select_columns)} FROM ranked "
            "WHERE _logical_rank = 1 "
            "ORDER BY product_id ASC",
            parameters,
        )
    return (
        f"SELECT {', '.join(select_columns)} FROM {table} "
        f"WHERE {where_sql} ORDER BY product_id ASC",
        parameters,
    )


class SQLiteOracle:
    def __init__(self, database_path: str | Path) -> None:
        self.database_path = Path(database_path)

    def candidate_product_ids(self, validated_plan: ValidatedPlan) -> tuple[str, ...]:
        """Return every product ID admitted by an authorized SEARCH filter.

        External evidence must be intersected *after* ordinary DB hard filters
        but *before* the requested TOP-N is applied.  Reusing the exact Oracle
        SQL here prevents a second Python interpretation of field scales,
        quality/zero-value policy, bond deduplication, or locked constraints.
        The caller receives IDs only; ranking and limit remain the downstream
        external executor's responsibility after evidence intersection.
        """

        started = perf_counter()
        audit = current_request_audit()
        try:
            with open_validated_database(
                validated_plan,
                self.database_path,
                oracle_kind="search",
                connection_audit_reason="external_prefilter_connection",
            ) as (plan, connection, _manifest):
                if plan.intent is not Intent.SEARCH:
                    raise ValueError("external DB prefilter requires SEARCH intent")
                select_sql, count_sql, parameters = compile_search_sql(plan)
                count_row = connection.execute(count_sql, parameters).fetchone()
                if count_row is None:
                    raise sqlite3.DatabaseError("candidate count query returned no row")
                candidate_count = int(count_row["candidate_count"])
                require_candidate_budget(validated_plan, candidate_count)
                rows = connection.execute(
                    select_sql,
                    [*parameters, candidate_count],
                ).fetchall()
        except Exception:
            if audit is not None:
                audit.emit(
                    stage=AuditStage.SQL,
                    outcome=AuditOutcome.FAILED,
                    reason_code="external_prefilter_failed",
                    duration_ms=(perf_counter() - started) * 1000,
                    **_receipt_audit_fields(validated_plan),
                )
            raise
        product_ids = tuple(str(row["product_id"]) for row in rows)
        if len(product_ids) != candidate_count or len(set(product_ids)) != len(product_ids):
            raise sqlite3.DatabaseError(
                "external prefilter candidate IDs differ from the Oracle count"
            )
        if audit is not None:
            audit.emit(
                stage=AuditStage.SQL,
                outcome=AuditOutcome.SUCCEEDED,
                reason_code="external_prefilter_completed",
                duration_ms=(perf_counter() - started) * 1000,
                candidate_count=candidate_count,
                result_count=len(product_ids),
                **_receipt_audit_fields(validated_plan),
            )
        return product_ids

    def execute(self, validated_plan: ValidatedPlan) -> ExecutedSearch:
        started = perf_counter()
        statements_started: float | None = None
        statements_duration_ms: float | None = None
        audit = current_request_audit()
        try:
            with open_validated_database(
                validated_plan,
                self.database_path,
                oracle_kind="search",
                connection_audit_reason="oracle_connection",
            ) as (plan, connection, manifest):
                select_sql, count_sql, parameters = compile_search_sql(plan)
                statements_started = perf_counter()
                count_row = connection.execute(count_sql, parameters).fetchone()
                if count_row is None:
                    raise sqlite3.DatabaseError("candidate count query returned no row")
                candidate_count = int(count_row["candidate_count"])
                require_candidate_budget(validated_plan, candidate_count)
                rows = connection.execute(
                    select_sql,
                    [*parameters, plan.limit],
                ).fetchall()
                statements_duration_ms = (perf_counter() - statements_started) * 1000
        except Exception:
            if audit is not None:
                if statements_started is not None and statements_duration_ms is None:
                    audit.emit(
                        stage=AuditStage.SQL,
                        outcome=AuditOutcome.FAILED,
                        reason_code="oracle_statements_failed",
                        duration_ms=(perf_counter() - statements_started) * 1000,
                        **_receipt_audit_fields(validated_plan),
                    )
                audit.emit(
                    stage=AuditStage.SQL,
                    outcome=AuditOutcome.FAILED,
                    reason_code="statement_failed",
                    duration_ms=(perf_counter() - started) * 1000,
                    **_receipt_audit_fields(validated_plan),
                )
            raise
        if audit is not None:
            assert statements_duration_ms is not None
            audit.emit(
                stage=AuditStage.SQL,
                outcome=AuditOutcome.SUCCEEDED,
                reason_code="oracle_statements_completed",
                duration_ms=statements_duration_ms,
                candidate_count=candidate_count,
                result_count=len(rows),
                **_receipt_audit_fields(validated_plan),
            )
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
        statements_started: float | None = None
        statements_duration_ms: float | None = None
        audit = current_request_audit()
        try:
            with open_validated_database(
                validated_plan,
                self.database_path,
                oracle_kind="aggregate",
                connection_audit_reason="oracle_connection",
            ) as (plan, connection, manifest):
                select_sql, parameters = compile_aggregate_sql(plan)
                bounded_sql = f"{select_sql} LIMIT ?"
                bounded_parameters = [
                    *parameters,
                    candidate_row_budget(validated_plan) + 1,
                ]
                statements_started = perf_counter()
                rows = connection.execute(bounded_sql, bounded_parameters).fetchall()
                statements_duration_ms = (perf_counter() - statements_started) * 1000
        except Exception:
            if audit is not None:
                if statements_started is not None and statements_duration_ms is None:
                    audit.emit(
                        stage=AuditStage.SQL,
                        outcome=AuditOutcome.FAILED,
                        reason_code="oracle_statements_failed",
                        duration_ms=(perf_counter() - statements_started) * 1000,
                        **_receipt_audit_fields(validated_plan),
                    )
                audit.emit(
                    stage=AuditStage.SQL,
                    outcome=AuditOutcome.FAILED,
                    reason_code="statement_failed",
                    duration_ms=(perf_counter() - started) * 1000,
                    **_receipt_audit_fields(validated_plan),
                )
            raise
        if audit is not None:
            assert statements_duration_ms is not None
            audit.emit(
                stage=AuditStage.SQL,
                outcome=AuditOutcome.SUCCEEDED,
                reason_code="oracle_statements_completed",
                duration_ms=statements_duration_ms,
                candidate_count=len(rows),
                result_count=0,
                **_receipt_audit_fields(validated_plan),
            )
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
