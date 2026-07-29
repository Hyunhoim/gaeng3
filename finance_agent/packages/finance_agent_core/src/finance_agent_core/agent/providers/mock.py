from __future__ import annotations

from typing import Literal

from finance_agent_core.contracts.queryplan import QueryPlan


def first_vertical_slice_plan(question_id: str) -> QueryPlan:
    return QueryPlan.model_validate(
        {
            "schema_version": "1.0",
            "question_id": question_id,
            "intent": "search",
            "product_families": ["overseas_etp"],
            "constraints": [
                {
                    "field": "product_type",
                    "operator": "eq",
                    "value": "ETF",
                    "unit": "code",
                    "strength": "locked",
                },
                {
                    "field": "investment_region",
                    "operator": "eq",
                    "value": "United States of America",
                    "unit": "code",
                    "strength": "locked",
                },
                {
                    "field": "asset_type",
                    "operator": "eq",
                    "value": "Bond",
                    "unit": "code",
                    "strength": "locked",
                },
                {
                    "field": "sellable",
                    "operator": "eq",
                    "value": True,
                    "unit": "boolean",
                    "strength": "locked",
                },
                {
                    "field": "trading_suspended",
                    "operator": "eq",
                    "value": False,
                    "unit": "boolean",
                    "strength": "locked",
                },
                {
                    "field": "total_expense_ratio_pct",
                    "operator": "lte",
                    "value": 0.2,
                    "unit": "pct_point",
                    "strength": "locked",
                },
            ],
            "ranking": [{"field": "aum", "direction": "desc", "nulls": "last"}],
            "projection": [
                "product_id",
                "product_name",
                "ticker",
                "total_expense_ratio_pct",
                "aum",
                "trading_currency",
                "dynamic_as_of",
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


def domestic_vertical_slice_plan(question_id: str) -> QueryPlan:
    return QueryPlan.model_validate(
        {
            "schema_version": "1.0",
            "question_id": question_id,
            "intent": "search",
            "product_families": ["domestic_etp"],
            "constraints": [
                {
                    "field": "product_type",
                    "operator": "eq",
                    "value": "ETF",
                    "unit": "code",
                    "strength": "locked",
                },
                {
                    "field": "investment_region",
                    "operator": "eq",
                    "value": "미국",
                    "unit": "code",
                    "strength": "locked",
                },
                {
                    "field": "asset_type",
                    "operator": "eq",
                    "value": "주식",
                    "unit": "code",
                    "strength": "locked",
                },
                {
                    "field": "sellable",
                    "operator": "eq",
                    "value": True,
                    "unit": "boolean",
                    "strength": "locked",
                },
                {
                    "field": "trading_suspended",
                    "operator": "eq",
                    "value": False,
                    "unit": "boolean",
                    "strength": "locked",
                },
                {
                    "field": "pension_eligible",
                    "operator": "eq",
                    "value": True,
                    "unit": "boolean",
                    "strength": "locked",
                },
            ],
            "ranking": [
                {
                    "field": "one_month_return_pct",
                    "direction": "desc",
                    "nulls": "last",
                }
            ],
            "projection": [
                "product_id",
                "product_name",
                "ticker",
                "one_month_return_pct",
                "aum",
                "trading_currency",
                "dynamic_as_of",
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


def bond_vertical_slice_plan(question_id: str) -> QueryPlan:
    return QueryPlan.model_validate(
        {
            "schema_version": "1.0",
            "question_id": question_id,
            "intent": "search",
            "product_families": ["bond"],
            "constraints": [
                {
                    "field": "currently_buyable",
                    "operator": "eq",
                    "value": True,
                    "unit": "boolean",
                    "strength": "locked",
                }
            ],
            "ranking": [
                {
                    "field": "buy_yield_pct",
                    "direction": "desc",
                    "nulls": "last",
                }
            ],
            "projection": [
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


class MockProvider:
    @property
    def provider_name(self) -> Literal["mock"]:
        return "mock"

    def generate_query_plan(self, question: str, question_id: str) -> QueryPlan:
        if not question.strip():
            raise ValueError("question cannot be blank")
        return first_vertical_slice_plan(question_id)


class DomesticMockProvider:
    @property
    def provider_name(self) -> Literal["mock"]:
        return "mock"

    def generate_query_plan(self, question: str, question_id: str) -> QueryPlan:
        if not question.strip():
            raise ValueError("question cannot be blank")
        return domestic_vertical_slice_plan(question_id)


class BondMockProvider:
    @property
    def provider_name(self) -> Literal["mock"]:
        return "mock"

    def generate_query_plan(self, question: str, question_id: str) -> QueryPlan:
        if not question.strip():
            raise ValueError("question cannot be blank")
        return bond_vertical_slice_plan(question_id)
