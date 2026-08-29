from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from finance_agent_core.storage.bond import (
    CONVEXITY_SCALE,
    DURATION_SCALE,
    PRICE_SCALE,
    RATE_SCALE,
)
from finance_agent_core.storage.bond import MONEY_SCALE as BOND_MONEY_SCALE
from finance_agent_core.storage.bond import QUANTITY_SCALE as BOND_QUANTITY_SCALE
from finance_agent_core.storage.domestic_etp import (
    EIGHT_DECIMAL_SCALE as DOMESTIC_EIGHT_DECIMAL_SCALE,
)
from finance_agent_core.storage.domestic_etp import (
    FACTOR_SCALE,
    HUNDREDTH_SCALE,
    MONEY_SCALE,
    RETURN_SCALE,
)
from finance_agent_core.storage.domestic_etp import (
    QUANTITY_SCALE as DOMESTIC_QUANTITY_SCALE,
)
from finance_agent_core.storage.public_fund import FUND_AUM_SCALE, FUND_RETURN_SCALE
from finance_agent_core.storage.sqlite import (
    AUM_SCALE,
    FEE_SCALE,
)
from finance_agent_core.storage.sqlite import (
    EIGHT_DECIMAL_SCALE as OVERSEAS_EIGHT_DECIMAL_SCALE,
)


@dataclass(frozen=True)
class SqlField:
    column: str
    scale: Decimal | None = None
    quality_column: str | None = None
    usable_numeric: bool = False


def _json_value(source_column: str) -> str:
    if not source_column.replace("_", "").isalnum():
        raise ValueError(f"unsafe source column: {source_column!r}")
    return f"json_extract(source_values_json, '$.{source_column}')"


def _source_text(source_column: str) -> SqlField:
    return SqlField(_json_value(source_column))


def _source_number(source_column: str) -> SqlField:
    return SqlField(
        f"CAST({_json_value(source_column)} AS REAL)",
        usable_numeric=True,
    )


def _source_boolean(source_column: str) -> SqlField:
    value = _json_value(source_column)
    return SqlField(
        f"CASE UPPER(CAST({value} AS TEXT)) "
        "WHEN 'Y' THEN 1 WHEN '1' THEN 1 "
        "WHEN 'N' THEN 0 WHEN '0' THEN 0 ELSE NULL END"
    )


def _source_date(source_column: str) -> SqlField:
    value = f"CAST({_json_value(source_column)} AS TEXT)"
    normalized = (
        f"CASE WHEN length({value}) = 8 THEN "
        f"substr({value}, 1, 4) || '-' || substr({value}, 5, 2) || '-' || "
        f"substr({value}, 7, 2) ELSE {value} END"
    )
    return SqlField(
        f"CASE WHEN {_json_value(source_column)} IS NULL "
        f"OR TRIM({value}) IN ('', '0', '00000000', '0000-00-00') "
        f"OR substr(TRIM({value}), 1, 4) = '0000' "
        f"THEN NULL ELSE {normalized} END"
    )


OVERSEAS_SQL_FIELDS = {
    "product_id": SqlField("product_id"),
    "product_family": SqlField("product_family"),
    "product_type": SqlField("product_type"),
    "product_name": SqlField("product_name"),
    "short_name": _source_text("pd_abrv_nm"),
    "exchange_code": SqlField("exchange_code"),
    "ticker": SqlField("ticker"),
    "isin": SqlField("isin"),
    "sellable": SqlField("sellable"),
    "trading_suspended": SqlField("trading_suspended"),
    "asset_type": SqlField("asset_type"),
    "investment_region": SqlField("investment_region"),
    "manager": _source_text("cu_fund_mgmt_co"),
    "leverage_factor": _source_number("cu_lev_fector"),
    "inverse_short": _source_boolean("cu_inverse_short_yn"),
    "index_tracking": _source_boolean("cu_index_tracking_yn"),
    "base_price": _source_number("du_bpr"),
    "listing_date": _source_date("pd_lstg_dt"),
    "fund_currency": _source_text("pd_curr_cd"),
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
    "one_day_return_pct": SqlField(
        "one_day_return_micro_pct",
        scale=FEE_SCALE,
        quality_column="one_day_return_quality",
    ),
    "close_price": SqlField(
        "close_price_eight_decimals",
        scale=OVERSEAS_EIGHT_DECIMAL_SCALE,
        quality_column="close_price_quality",
    ),
    "daily_trading_value": SqlField(
        "daily_trading_value_eight_decimals",
        scale=OVERSEAS_EIGHT_DECIMAL_SCALE,
        quality_column="daily_trading_value_quality",
    ),
    "daily_volume": SqlField(
        "daily_volume_eight_decimals",
        scale=OVERSEAS_EIGHT_DECIMAL_SCALE,
        quality_column="daily_volume_quality",
    ),
    "discrepancy_pct": SqlField(
        "discrepancy_micro_pct",
        scale=FEE_SCALE,
        quality_column="discrepancy_quality",
    ),
    "trading_currency": SqlField("trading_currency"),
    "static_as_of": SqlField("static_as_of"),
    "dynamic_as_of": SqlField("dynamic_as_of"),
}

DOMESTIC_SQL_FIELDS = {
    **OVERSEAS_SQL_FIELDS,
    "short_name": SqlField("short_name"),
    "manager": SqlField("manager"),
    "net_asset_value": _source_number("du_last_nav"),
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
    "daily_volume": SqlField(
        "daily_volume_hundredth_units",
        scale=DOMESTIC_QUANTITY_SCALE,
        quality_column="daily_volume_quality",
    ),
    "tracking_error_pct": SqlField(
        "tracking_error_hundredth_pct",
        scale=HUNDREDTH_SCALE,
        quality_column="tracking_error_quality",
    ),
    "discrepancy_pct": SqlField(
        "discrepancy_hundredth_pct",
        scale=HUNDREDTH_SCALE,
        quality_column="discrepancy_quality",
    ),
    "twenty_day_volatility_pct": SqlField(
        "twenty_day_volatility_eight_decimal_pct",
        scale=DOMESTIC_EIGHT_DECIMAL_SCALE,
        quality_column="twenty_day_volatility_quality",
    ),
    "sixty_day_volatility_pct": SqlField(
        "sixty_day_volatility_eight_decimal_pct",
        scale=DOMESTIC_EIGHT_DECIMAL_SCALE,
        quality_column="sixty_day_volatility_quality",
    ),
    "one_hundred_twenty_day_volatility_pct": SqlField(
        "one_hundred_twenty_day_volatility_eight_decimal_pct",
        scale=DOMESTIC_EIGHT_DECIMAL_SCALE,
        quality_column="one_hundred_twenty_day_volatility_quality",
    ),
    "two_hundred_fifty_two_day_volatility_pct": SqlField(
        "two_hundred_fifty_two_day_volatility_eight_decimal_pct",
        scale=DOMESTIC_EIGHT_DECIMAL_SCALE,
        quality_column="two_hundred_fifty_two_day_volatility_quality",
    ),
    "distribution_yield_pct": SqlField(
        "distribution_yield_eight_decimal_pct",
        scale=DOMESTIC_EIGHT_DECIMAL_SCALE,
        quality_column="distribution_yield_quality",
    ),
    "distribution_payments_per_year": SqlField(
        "distribution_payments_per_year_units",
        quality_column="distribution_payments_per_year_quality",
    ),
    "static_as_of": SqlField("static_as_of", quality_column="static_as_of_quality"),
    "dynamic_as_of": SqlField("dynamic_as_of", quality_column="dynamic_as_of_quality"),
}

for _overseas_only_field in (
    "base_price",
    "fund_currency",
    "index_tracking",
    "inverse_short",
    "listing_date",
):
    DOMESTIC_SQL_FIELDS.pop(_overseas_only_field)

BOND_SQL_FIELDS = {
    # The database keeps a physical LOT-level composite key, while the
    # organizer's PD_NO (ticker) is the public, logical bond identity.
    "product_id": SqlField("ticker"),
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
    "public_offering": _source_boolean("bd_ofr_tcd"),
    "pension_eligible": _source_boolean("pd_pen_tr_yn"),
    "sales_channel": _source_text("bdbns_abl_chnl_nm"),
    "risk_level": _source_text("pd_risk_nm"),
    "total_issue_amount": _source_number("bd_tisu_a"),
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
    "applied_yield_pct": SqlField(
        "applied_yield_micro_pct",
        scale=RATE_SCALE,
        quality_column="applied_yield_quality",
    ),
    "evaluation_price": SqlField(
        "evaluation_price_micro_units",
        scale=PRICE_SCALE,
        quality_column="evaluation_price_quality",
    ),
    "dirty_price": SqlField(
        "dirty_price_micro_units",
        scale=PRICE_SCALE,
        quality_column="dirty_price_quality",
    ),
    "convexity": SqlField(
        "convexity_micro",
        scale=CONVEXITY_SCALE,
        quality_column="convexity_quality",
    ),
    "buyable_quantity": SqlField(
        "buyable_quantity_units",
        scale=BOND_QUANTITY_SCALE,
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
    "sales_channel": SqlField("sales_channel", quality_column="sales_channel_quality"),
    "fee_type": _source_text("han_clas_fee_type"),
    "management_company_fee_pct": _source_number("or_co_rwrd_r"),
    "interest_dividend_type": SqlField(
        "interest_dividend_type",
        quality_column="interest_dividend_type_quality",
    ),
    "sale_company_fee_pct": SqlField(
        "sale_company_fee_micro_pct",
        scale=FUND_RETURN_SCALE,
        quality_column="sale_company_fee_quality",
    ),
    "trustee_fee_pct": SqlField(
        "trustee_fee_micro_pct",
        scale=FUND_RETURN_SCALE,
        quality_column="trustee_fee_quality",
    ),
    "domestic_stock_ratio_pct": SqlField(
        "domestic_stock_ratio_micro_pct",
        scale=FUND_RETURN_SCALE,
        quality_column="domestic_stock_ratio_quality",
    ),
    "domestic_bond_ratio_pct": SqlField(
        "domestic_bond_ratio_micro_pct",
        scale=FUND_RETURN_SCALE,
        quality_column="domestic_bond_ratio_quality",
    ),
    "overseas_stock_ratio_pct": SqlField(
        "overseas_stock_ratio_micro_pct",
        scale=FUND_RETURN_SCALE,
        quality_column="overseas_stock_ratio_quality",
    ),
    "overseas_bond_ratio_pct": SqlField(
        "overseas_bond_ratio_micro_pct",
        scale=FUND_RETURN_SCALE,
        quality_column="overseas_bond_ratio_quality",
    ),
    "latest_distribution_pct": SqlField(
        "latest_distribution_micro_pct",
        scale=FUND_RETURN_SCALE,
        quality_column="latest_distribution_quality",
    ),
    "preservation_ratio_pct": SqlField(
        "preservation_ratio_micro_pct",
        scale=FUND_RETURN_SCALE,
        quality_column="preservation_ratio_quality",
    ),
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


def logical_product_scope(family: str) -> str:
    """Restrict SQL execution to one stable row per organizer product ID."""

    if family == "bond":
        return (
            "source_row IN (SELECT MIN(source_row) FROM bond_products "
            "WHERE is_quarantined = 0 GROUP BY ticker)"
        )
    return "1 = 1"
