from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from finance_agent_core.storage.bond import (
    DURATION_SCALE,
    QUANTITY_SCALE,
    RATE_SCALE,
)
from finance_agent_core.storage.bond import MONEY_SCALE as BOND_MONEY_SCALE
from finance_agent_core.storage.domestic_etp import (
    FACTOR_SCALE,
    MONEY_SCALE,
    RETURN_SCALE,
)
from finance_agent_core.storage.public_fund import FUND_AUM_SCALE, FUND_RETURN_SCALE
from finance_agent_core.storage.sqlite import AUM_SCALE, FEE_SCALE


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

FUND_SQL_FIELDS = {
    "product_id": SqlField("product_id"),
    "product_family": SqlField("product_family"),
    "product_name": SqlField("product_name"),
    "short_name": SqlField("short_name"),
    "public_offering": SqlField(
        "public_offering",
        quality_column="public_offering_quality",
    ),
    "sellable": SqlField("sellable", quality_column="sellable_quality"),
    "company_sellable": SqlField(
        "company_sellable",
        quality_column="company_sellable_quality",
    ),
    "trading_currency": SqlField(
        "trading_currency",
        quality_column="trading_currency_quality",
    ),
    "investment_region": SqlField(
        "investment_region",
        quality_column="investment_region_quality",
    ),
    "fund_geography_scope": SqlField(
        "fund_geography_scope",
        quality_column="fund_geography_scope_quality",
    ),
    "fund_management_attribute": SqlField(
        "fund_management_attribute",
        quality_column="fund_management_attribute_quality",
    ),
    "investor_type": SqlField(
        "investor_type",
        quality_column="investor_type_quality",
    ),
    "currency_hedged": SqlField(
        "currency_hedged",
        quality_column="currency_hedged_quality",
    ),
    "risk_level": SqlField("risk_level", quality_column="risk_level_quality"),
    "aum": SqlField(
        "aum_ten_thousandth_units",
        scale=FUND_AUM_SCALE,
        quality_column="aum_quality",
    ),
    "base_index": SqlField("base_index", quality_column="base_index_quality"),
    "one_week_return_pct": SqlField(
        "one_week_return_micro_pct",
        scale=FUND_RETURN_SCALE,
        quality_column="one_week_return_quality",
    ),
    "one_month_return_pct": SqlField(
        "one_month_return_micro_pct",
        scale=FUND_RETURN_SCALE,
        quality_column="one_month_return_quality",
    ),
    "three_month_return_pct": SqlField(
        "three_month_return_micro_pct",
        scale=FUND_RETURN_SCALE,
        quality_column="three_month_return_quality",
    ),
    "six_month_return_pct": SqlField(
        "six_month_return_micro_pct",
        scale=FUND_RETURN_SCALE,
        quality_column="six_month_return_quality",
    ),
    "eighteen_month_return_pct": SqlField(
        "eighteen_month_return_micro_pct",
        scale=FUND_RETURN_SCALE,
        quality_column="eighteen_month_return_quality",
    ),
    "one_year_return_pct": SqlField(
        "one_year_return_micro_pct",
        scale=FUND_RETURN_SCALE,
        quality_column="one_year_return_quality",
    ),
    "two_year_return_pct": SqlField(
        "two_year_return_micro_pct",
        scale=FUND_RETURN_SCALE,
        quality_column="two_year_return_quality",
    ),
    "three_year_return_pct": SqlField(
        "three_year_return_micro_pct",
        scale=FUND_RETURN_SCALE,
        quality_column="three_year_return_quality",
    ),
    "five_year_return_pct": SqlField(
        "five_year_return_micro_pct",
        scale=FUND_RETURN_SCALE,
        quality_column="five_year_return_quality",
    ),
    "static_as_of": SqlField("static_as_of", quality_column="static_as_of_quality"),
    "dynamic_as_of": SqlField("dynamic_as_of", quality_column="dynamic_as_of_quality"),
}

SQL_FIELDS_BY_FAMILY = {
    "overseas_etp": OVERSEAS_SQL_FIELDS,
    "domestic_etp": DOMESTIC_SQL_FIELDS,
    "bond": BOND_SQL_FIELDS,
    "fund": FUND_SQL_FIELDS,
}

TABLE_BY_FAMILY = {
    "overseas_etp": "overseas_etp_products",
    "domestic_etp": "domestic_etp_products",
    "bond": "bond_products",
    "fund": "fund_products",
}
