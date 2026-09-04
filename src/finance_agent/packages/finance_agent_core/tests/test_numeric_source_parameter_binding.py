from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

import pytest

from finance_agent_core.contracts import QueryPlan
from finance_agent_core.execution.oracle import compile_search_sql
from finance_agent_core.execution.verifier import (
    _matches_constraint,
    _ranking_metric_value,
)


@dataclass(frozen=True)
class _VerifierRow:
    source_row: int
    product_id: str
    public_offering: bool
    fee: Decimal | None
    one_year_return: Decimal | None
    product_family: str = "fund"
    is_quarantined: bool = False
    source_snapshot_date: date = date(2026, 8, 24)
    static_as_of: date = date(2026, 8, 24)
    dynamic_as_of: date = date(2026, 8, 21)

    def canonical_value(self, field_name: str) -> object | None:
        return {
            "product_id": self.product_id,
            "public_offering": self.public_offering,
            "management_company_fee_pct": self.fee,
            "one_year_return_pct": self.one_year_return,
        }[field_name]

    def row_level_quality(self, _field_name: str) -> tuple[None, None]:
        return None, None


def _fee_search_plan(operator: str, threshold: float) -> QueryPlan:
    return QueryPlan.model_validate(
        {
            "schema_version": "1.0",
            "question_id": f"source-fee-{operator}",
            "intent": "search",
            "product_families": ["fund"],
            "constraints": [
                {
                    "field": "management_company_fee_pct",
                    "operator": operator,
                    "value": threshold,
                    "unit": "pct_point",
                    "strength": "locked",
                },
                {
                    "field": "public_offering",
                    "operator": "eq",
                    "value": True,
                    "unit": "boolean",
                    "strength": "locked",
                },
            ],
            "ranking": [
                {
                    "field": "one_year_return_pct",
                    "direction": "desc",
                    "nulls": "last",
                }
            ],
            "projection": [
                "product_id",
                "product_name",
                "management_company_fee_pct",
                "one_year_return_pct",
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
    )


_ROWS = (
    _VerifierRow(2, "missing-fee", True, None, Decimal("10")),
    _VerifierRow(3, "zero-fee", True, Decimal("0"), Decimal("10")),
    _VerifierRow(4, "below", True, Decimal("2.255"), Decimal("10")),
    _VerifierRow(5, "edge", True, Decimal("2.256"), Decimal("10")),
    _VerifierRow(6, "upper", True, Decimal("5.5"), Decimal("20")),
    _VerifierRow(7, "above", True, Decimal("5.6"), Decimal("30")),
    _VerifierRow(8, "zero-rank", True, Decimal("9"), Decimal("0")),
    _VerifierRow(9, "private", False, Decimal("9"), Decimal("40")),
)


def _sql_candidate_count(plan: QueryPlan) -> tuple[int, list[object]]:
    connection = sqlite3.connect(":memory:")
    try:
        connection.execute(
            "CREATE TABLE fund_products ("
            "product_id TEXT PRIMARY KEY, is_quarantined INTEGER NOT NULL, "
            "source_values_json TEXT NOT NULL, public_offering INTEGER, "
            "public_offering_quality TEXT, one_year_return_micro_pct INTEGER, "
            "one_year_return_quality TEXT)"
        )
        connection.executemany(
            "INSERT INTO fund_products VALUES (?, ?, ?, ?, ?, ?, ?)",
            [
                (
                    row.product_id,
                    0,
                    json.dumps(
                        {
                            "or_co_rwrd_r": (
                                None if row.fee is None else float(row.fee)
                            )
                        }
                    ),
                    int(row.public_offering),
                    "VALID",
                    (
                        None
                        if row.one_year_return is None
                        else int(row.one_year_return * 1_000_000)
                    ),
                    "PARTIAL",
                )
                for row in _ROWS
            ],
        )
        _, count_sql, parameters = compile_search_sql(plan)
        result = connection.execute(count_sql, parameters).fetchone()
        assert result is not None
        return int(result[0]), list(parameters)
    finally:
        connection.close()


@pytest.mark.parametrize(
    ("operator", "threshold", "expected_count"),
    (("gte", 2.256, 3), ("lte", 5.5, 3)),
)
def test_source_numeric_filter_binds_as_number_and_matches_python_verifier(
    operator: str,
    threshold: float,
    expected_count: int,
) -> None:
    plan = _fee_search_plan(operator, threshold)

    sql_count, parameters = _sql_candidate_count(plan)
    python_count = sum(
        1
        for row in _ROWS
        if all(_matches_constraint(row, constraint) for constraint in plan.constraints)
        and all(
            _ranking_metric_value(row, ranking.field) is not None
            for ranking in plan.ranking
        )
    )

    assert any(type(parameter) is float and parameter == threshold for parameter in parameters)
    assert sql_count == python_count == expected_count
