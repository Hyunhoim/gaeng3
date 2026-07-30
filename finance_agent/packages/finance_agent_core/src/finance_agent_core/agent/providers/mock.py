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


def fund_vertical_slice_plan(question_id: str) -> QueryPlan:
    return QueryPlan.model_validate(
        {
            "schema_version": "1.0",
            "question_id": question_id,
            "intent": "search",
            "product_families": ["fund"],
            "constraints": [
                {
                    "field": "public_offering",
                    "operator": "eq",
                    "value": True,
                    "unit": "boolean",
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
                    "field": "company_sellable",
                    "operator": "eq",
                    "value": True,
                    "unit": "boolean",
                    "strength": "locked",
                },
                {
                    "field": "fund_geography_scope",
                    "operator": "eq",
                    "value": "해외",
                    "unit": "code",
                    "strength": "locked",
                },
                {
                    "field": "fund_management_attribute",
                    "operator": "eq",
                    "value": "주식형",
                    "unit": "code",
                    "strength": "locked",
                },
            ],
            "ranking": [
                {
                    "field": "three_month_return_pct",
                    "direction": "desc",
                    "nulls": "last",
                }
            ],
            "projection": [
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


def fund_comparison_plan(
    question_id: str,
    product_ids: list[str],
    comparison_fields: list[str],
) -> QueryPlan:
    if len(product_ids) != 2 or len(set(product_ids)) != 2:
        raise ValueError("fund comparison requires exactly two unique product IDs")
    projection = [
        "product_id",
        "product_name",
        "short_name",
        *comparison_fields,
        *(["trading_currency"] if "aum" in comparison_fields else []),
        "dynamic_as_of",
    ]
    projection = list(dict.fromkeys(projection))
    return QueryPlan.model_validate(
        {
            "schema_version": "1.0",
            "question_id": question_id,
            "intent": "compare",
            "product_families": ["fund"],
            "constraints": [
                {
                    "field": "public_offering",
                    "operator": "eq",
                    "value": True,
                    "unit": "boolean",
                    "strength": "locked",
                },
                {
                    "field": "product_id",
                    "operator": "in",
                    "value": product_ids,
                    "unit": "code",
                    "strength": "locked",
                },
            ],
            "ranking": [],
            "projection": projection,
            "limit": 2,
            "intent_payload": {
                "comparison_fields": comparison_fields,
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
