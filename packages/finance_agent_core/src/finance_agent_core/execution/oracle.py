from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

from finance_agent_core.contracts.queryplan import (
    Constraint,
    ConstraintOperator,
    Intent,
    NullPlacement,
    QueryPlan,
)
from finance_agent_core.domain import ExecutedSearch
from finance_agent_core.storage.bond import (
    DURATION_SCALE,
    QUANTITY_SCALE,
    RATE_SCALE,
)
from finance_agent_core.storage.bond import MONEY_SCALE as BOND_MONEY_SCALE
from finance_agent_core.storage.domestic_etp import FACTOR_SCALE, MONEY_SCALE, RETURN_SCALE
from finance_agent_core.storage.sqlite import (
    AUM_SCALE,
    FEE_SCALE,
    connect_read_only,
    load_manifest,
    row_to_record,
)


@dataclass(frozen=True)
class SqlField:
    column: str
    scale: Decimal | None = None
    quality_column: str | None = None


OVERSEAS_SQL_FIELDS = {
    "product_id": SqlField("product_id"),
    "product_family": SqlField("product_family"),
    "product_type": SqlField("product_type"),
    "product_name": SqlField("product_name"),
    "exchange_code": SqlField("exchange_code"),
    "ticker": SqlField("ticker"),
    "isin": SqlField("isin"),
    "sellable": SqlField("sellable"),
    "trading_suspended": SqlField("trading_suspended"),
    "asset_type": SqlField("asset_type"),
    "investment_region": SqlField("investment_region"),
    "total_expense_ratio_pct": SqlField(
        "total_expense_ratio_micro_pct",
        scale=FEE_SCALE,
        quality_column="total_expense_ratio_quality",
    ),
    "aum": SqlField(
        "aum_minor_units",
        scale=AUM_SCALE,
        quality_column="aum_quality",
    ),
    "trading_currency": SqlField("trading_currency"),
    "static_as_of": SqlField("static_as_of"),
    "dynamic_as_of": SqlField("dynamic_as_of"),
}

DOMESTIC_SQL_FIELDS = {
    **OVERSEAS_SQL_FIELDS,
    "short_name": SqlField("short_name"),
    "manager": SqlField("manager"),
    "base_index": SqlField("base_index", quality_column="base_index_quality"),
    "strategy": SqlField("strategy", quality_column="strategy_quality"),
    "leverage_factor": SqlField(
        "leverage_factor_micro",
        scale=FACTOR_SCALE,
        quality_column="leverage_factor_quality",
    ),
    "risk_level": SqlField("risk_level"),
    "pension_eligible": SqlField("pension_eligible"),
    "core_etf": SqlField("core_etf"),
    "trading_currency": SqlField(
        "trading_currency",
        quality_column="trading_currency_quality",
    ),
    "close_price": SqlField(
        "close_price_minor_units",
        scale=MONEY_SCALE,
        quality_column="close_price_quality",
    ),
    "one_day_return_pct": SqlField(
        "one_day_return_micro_pct",
        scale=RETURN_SCALE,
        quality_column="one_day_return_quality",
    ),
    "one_month_return_pct": SqlField(
        "one_month_return_micro_pct",
        scale=RETURN_SCALE,
        quality_column="one_month_return_quality",
    ),
    "three_month_return_pct": SqlField(
        "three_month_return_micro_pct",
        scale=RETURN_SCALE,
        quality_column="three_month_return_quality",
    ),
    "six_month_return_pct": SqlField(
        "six_month_return_micro_pct",
        scale=RETURN_SCALE,
        quality_column="six_month_return_quality",
    ),
    "one_year_return_pct": SqlField(
        "one_year_return_micro_pct",
        scale=RETURN_SCALE,
        quality_column="one_year_return_quality",
    ),
    "ytd_return_pct": SqlField(
        "ytd_return_micro_pct",
        scale=RETURN_SCALE,
        quality_column="ytd_return_quality",
    ),
    "daily_trading_value": SqlField(
        "daily_trading_value_minor_units",
        scale=MONEY_SCALE,
        quality_column="daily_trading_value_quality",
    ),
    "static_as_of": SqlField("static_as_of", quality_column="static_as_of_quality"),
    "dynamic_as_of": SqlField("dynamic_as_of", quality_column="dynamic_as_of_quality"),
}

BOND_SQL_FIELDS = {
    "product_id": SqlField("product_id"),
    "product_family": SqlField("product_family"),
    "product_name": SqlField("product_name"),
    "ticker": SqlField("ticker"),
    "short_name": SqlField("short_name", quality_column="short_name_quality"),
    "bond_market": SqlField("bond_market"),
    "issuer": SqlField("issuer", quality_column="issuer_quality"),
    "bond_major_class": SqlField("bond_major_class"),
    "bond_subclass": SqlField(
        "bond_subclass",
        quality_column="bond_subclass_quality",
    ),
    "bond_type": SqlField("bond_type", quality_column="bond_type_quality"),
    "trading_currency": SqlField(
        "trading_currency",
        quality_column="trading_currency_quality",
    ),
    "issue_amount": SqlField(
        "issue_amount_minor_units",
        scale=BOND_MONEY_SCALE,
    ),
    "issue_date": SqlField("issue_date", quality_column="issue_date_quality"),
    "maturity_date": SqlField(
        "maturity_date",
        quality_column="maturity_date_quality",
    ),
    "coupon_rate_pct": SqlField(
        "coupon_rate_micro_pct",
        scale=RATE_SCALE,
        quality_column="coupon_rate_quality",
    ),
    "credit_rating": SqlField(
        "credit_rating",
        quality_column="credit_rating_quality",
    ),
    "bond_risk_code": SqlField("bond_risk_code"),
    "buy_yield_pct": SqlField(
        "buy_yield_micro_pct",
        scale=RATE_SCALE,
        quality_column="buy_yield_quality",
    ),
    "after_tax_yield_pct": SqlField(
        "after_tax_yield_micro_pct",
        scale=RATE_SCALE,
        quality_column="after_tax_yield_quality",
    ),
    "buyable_quantity": SqlField(
        "buyable_quantity_units",
        scale=QUANTITY_SCALE,
        quality_column="buyable_quantity_quality",
    ),
    "currently_buyable": SqlField(
        "currently_buyable",
        quality_column="currently_buyable_quality",
    ),
    "remaining_days": SqlField(
        "remaining_days",
        scale=Decimal("1"),
        quality_column="remaining_days_quality",
    ),
    "duration_years": SqlField(
        "duration_micro_years",
        scale=DURATION_SCALE,
        quality_column="duration_quality",
    ),
    "static_as_of": SqlField("static_as_of", quality_column="static_as_of_quality"),
    "dynamic_as_of": SqlField("dynamic_as_of", quality_column="dynamic_as_of_quality"),
}

SQL_FIELDS_BY_FAMILY = {
    "overseas_etp": OVERSEAS_SQL_FIELDS,
    "domestic_etp": DOMESTIC_SQL_FIELDS,
    "bond": BOND_SQL_FIELDS,
}
TABLE_BY_FAMILY = {
    "overseas_etp": "overseas_etp_products",
    "domestic_etp": "domestic_etp_products",
    "bond": "bond_products",
}


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
    if plan.intent is not Intent.SEARCH:
        raise ValueError("the deterministic oracle currently supports search intent only")
    if len(plan.product_families) != 1:
        raise ValueError("the deterministic oracle requires exactly one product family")
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


class SQLiteOracle:
    def __init__(self, database_path: str | Path) -> None:
        self.database_path = Path(database_path)

    def execute(self, plan: QueryPlan) -> ExecutedSearch:
        select_sql, count_sql, parameters = compile_search_sql(plan)
        with connect_read_only(self.database_path) as connection:
            manifest = load_manifest(connection)
            family = plan.product_families[0].value
            if manifest.dataset != family:
                raise ValueError(
                    f"plan requests {family}, but database contains {manifest.dataset}"
                )
            count_row = connection.execute(count_sql, parameters).fetchone()
            if count_row is None:
                raise sqlite3.DatabaseError("candidate count query returned no row")
            rows = connection.execute(
                select_sql,
                [*parameters, plan.limit],
            ).fetchall()
        return ExecutedSearch(
            question_id=plan.question_id,
            candidate_count=int(count_row["candidate_count"]),
            records=[row_to_record(row) for row in rows],
            manifest=manifest,
            sql_template=select_sql,
            sql_parameters=[*parameters, plan.limit],
        )
