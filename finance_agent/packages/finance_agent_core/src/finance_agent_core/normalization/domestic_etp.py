from __future__ import annotations

import datetime as dt
import re
from collections.abc import Iterator, Mapping
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from finance_agent_core.audit.xlsx import XlsxStream
from finance_agent_core.config import QualityStatus, load_field_registry
from finance_agent_core.domain import NormalizedDomesticEtpRecord

SOURCE_COLUMNS = (
    "pd_exg_mkt_cd",
    "pd_itm_no",
    "pd_itm_no_ma",
    "pd_grp_no",
    "pd_nm",
    "pd_abrv_nm",
    "pd_sale_yn",
    "pd_tr_yn",
    "pd_pen_tr_yn",
    "pd_risk_nm",
    "pd_curr_cd",
    "wu_inv_ast_type",
    "wu_inv_rgn",
    "wu_core_yn",
    "cu_fund_mgmt_co",
    "cu_base_index",
    "cu_strtegy",
    "cu_lev_fector",
    "cu_charge_rt",
    "du_last_aum",
    "du_last_nav",
    "du_clpr",
    "du_chas_errt",
    "du_diff_rt",
    "du_er_1d",
    "du_er_1m",
    "du_er_3m",
    "du_er_6m",
    "du_er_1y",
    "du_er_ytd",
    "du_val_1d",
    "du_vlty_1m",
    "du_vlty_1y",
    "du_vlty_3m",
    "du_vlty_6m",
    "du_vol_1d",
    "pd_dvid_pay_cnt",
    "pd_dvid_yield",
    "wu_upt_dt",
    "du_upt_dt",
)
VALID_PRODUCT_KEY = re.compile(r"(?:KR|KRG)[A-Z0-9]{9,12}")
MICRO_PRECISION = Decimal("0.000001")
MONEY_PRECISION = Decimal("0.01")
EIGHT_DECIMAL_PRECISION = Decimal("0.00000001")


class DomesticEtpNormalizationError(ValueError):
    """Raised when a non-quarantined domestic ETP row cannot be normalized."""


def _semantic_missing(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, str):
        stripped = value.strip()
        return not stripped or stripped.upper() == "NULL"
    return False


def _text(value: Any, *, field: str, required: bool = False) -> str | None:
    if _semantic_missing(value):
        if required:
            raise DomesticEtpNormalizationError(f"{field} is required")
        return None
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, dt.datetime):
        return value.isoformat(sep=" ")
    return str(value).strip()


def _decimal(value: Any, *, field: str) -> Decimal | None:
    text = _text(value, field=field)
    if text is None:
        return None
    try:
        return Decimal(text.replace(",", ""))
    except InvalidOperation as error:
        raise DomesticEtpNormalizationError(f"{field} is not numeric: {text!r}") from error


def _quantized_decimal(value: Any, *, field: str, precision: Decimal) -> Decimal | None:
    parsed = _decimal(value, field=field)
    return None if parsed is None else parsed.quantize(precision)


def _date_or_snapshot(value: Any, *, field: str, snapshot: dt.date) -> tuple[dt.date, bool]:
    if isinstance(value, dt.datetime):
        return value.date(), True
    if isinstance(value, dt.date):
        return value, True
    text = _text(value, field=field)
    if text is None:
        return snapshot, False
    if text.endswith(".0") and text[:-2].isdigit():
        text = text[:-2]
    for date_format in ("%Y%m%d", "%Y-%m-%d", "%Y-%m-%d %H:%M:%S"):
        try:
            return dt.datetime.strptime(text, date_format).date(), True
        except ValueError:
            continue
    raise DomesticEtpNormalizationError(f"{field} is not a supported date: {text!r}")


def _boolean_code(value: Any, *, field: str) -> bool | None:
    text = _text(value, field=field)
    if text is None:
        return None
    if text not in {"0", "1", "N", "Y"}:
        raise DomesticEtpNormalizationError(f"{field} has an unknown boolean code: {text!r}")
    return text in {"1", "Y"}


def _raw_scalar(value: Any) -> str | int | bool | None:
    if _semantic_missing(value):
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, dt.datetime):
        return value.isoformat(sep=" ")
    if isinstance(value, dt.date):
        return value.isoformat()
    return str(value).strip()


def _quality(
    value: object | None,
    *,
    missing_reason: str,
    zero_is_unknown: bool = False,
) -> tuple[QualityStatus, str | None]:
    if value is None:
        return QualityStatus.UNKNOWN, missing_reason
    if zero_is_unknown and value == 0:
        field = missing_reason.removesuffix("_missing")
        return QualityStatus.UNKNOWN, f"{field}_zero_semantics_unconfirmed"
    return QualityStatus.VALID, None


def normalize_domestic_etp_row(
    *,
    source_row: int,
    values: Mapping[str, Any],
    present_source_fields: int,
    source_snapshot_date: dt.date | None = None,
) -> NormalizedDomesticEtpRecord:
    registry = load_field_registry()
    snapshot = source_snapshot_date or registry.datasets["domestic_etp"].snapshot_date
    source_key = _text(values.get("pd_itm_no"), field="pd_itm_no") or ""
    exchange_code = _text(values.get("pd_exg_mkt_cd"), field="pd_exg_mkt_cd")
    invalid_product_key = VALID_PRODUCT_KEY.fullmatch(source_key) is None
    is_quarantined = invalid_product_key or exchange_code is None
    quarantine_reason = (
        "invalid_product_key"
        if invalid_product_key
        else "missing_exchange_code"
        if exchange_code is None
        else None
    )

    def required_text(field: str, fallback: str) -> str:
        value = _text(values.get(field), field=field)
        if value is not None:
            return value
        if is_quarantined:
            return fallback
        raise DomesticEtpNormalizationError(f"{field} is required")

    product_id = f"QUARANTINED:PREF01N001:{source_row}" if is_quarantined else source_key
    product_type = required_text("pd_grp_no", "ETF")
    if product_type not in {"ETF", "ETN"}:
        if not is_quarantined:
            raise DomesticEtpNormalizationError(
                f"pd_grp_no has an unsupported value: {product_type!r}"
            )
        product_type = "ETF"

    fee = _quantized_decimal(
        values.get("cu_charge_rt"), field="cu_charge_rt", precision=MICRO_PRECISION
    )
    if fee is not None and fee < 0:
        raise DomesticEtpNormalizationError("cu_charge_rt cannot be negative")
    fee_quality, fee_reason = _quality(
        fee,
        missing_reason="fee_missing",
        zero_is_unknown=True,
    )

    aum = _quantized_decimal(
        values.get("du_last_aum"), field="du_last_aum", precision=MONEY_PRECISION
    )
    if aum is not None and aum < 0:
        raise DomesticEtpNormalizationError("du_last_aum cannot be negative")
    aum_quality, aum_reason = _quality(
        aum,
        missing_reason="aum_missing",
        zero_is_unknown=True,
    )

    close_price = _quantized_decimal(
        values.get("du_clpr"), field="du_clpr", precision=MONEY_PRECISION
    )
    if close_price is not None and close_price < 0:
        raise DomesticEtpNormalizationError("du_clpr cannot be negative")
    daily_trading_value = _quantized_decimal(
        values.get("du_val_1d"), field="du_val_1d", precision=MONEY_PRECISION
    )
    if daily_trading_value is not None and daily_trading_value < 0:
        raise DomesticEtpNormalizationError("du_val_1d cannot be negative")
    daily_volume = _quantized_decimal(
        values.get("du_vol_1d"), field="du_vol_1d", precision=MONEY_PRECISION
    )
    if daily_volume is not None and daily_volume < 0:
        raise DomesticEtpNormalizationError("du_vol_1d cannot be negative")
    tracking_error = _quantized_decimal(
        values.get("du_chas_errt"), field="du_chas_errt", precision=MONEY_PRECISION
    )
    discrepancy = _quantized_decimal(
        values.get("du_diff_rt"), field="du_diff_rt", precision=MONEY_PRECISION
    )
    volatility = {
        name: _quantized_decimal(
            values.get(source), field=source, precision=EIGHT_DECIMAL_PRECISION
        )
        for name, source in {
            "twenty_day_volatility_pct": "du_vlty_1m",
            "sixty_day_volatility_pct": "du_vlty_3m",
            "one_hundred_twenty_day_volatility_pct": "du_vlty_6m",
            "two_hundred_fifty_two_day_volatility_pct": "du_vlty_1y",
        }.items()
    }
    distribution_yield = _quantized_decimal(
        values.get("pd_dvid_yield"),
        field="pd_dvid_yield",
        precision=EIGHT_DECIMAL_PRECISION,
    )
    distribution_payments_per_year = _quantized_decimal(
        values.get("pd_dvid_pay_cnt"),
        field="pd_dvid_pay_cnt",
        precision=Decimal("1"),
    )
    if distribution_payments_per_year is not None and distribution_payments_per_year < 0:
        raise DomesticEtpNormalizationError("pd_dvid_pay_cnt cannot be negative")

    leverage_factor = _quantized_decimal(
        values.get("cu_lev_fector"),
        field="cu_lev_fector",
        precision=MICRO_PRECISION,
    )
    returns = {
        name: _quantized_decimal(values.get(source), field=source, precision=MICRO_PRECISION)
        for name, source in {
            "one_day_return_pct": "du_er_1d",
            "one_month_return_pct": "du_er_1m",
            "three_month_return_pct": "du_er_3m",
            "six_month_return_pct": "du_er_6m",
            "one_year_return_pct": "du_er_1y",
            "ytd_return_pct": "du_er_ytd",
        }.items()
    }
    static_as_of, static_date_present = _date_or_snapshot(
        values.get("wu_upt_dt"),
        field="wu_upt_dt",
        snapshot=snapshot,
    )
    dynamic_as_of, dynamic_date_present = _date_or_snapshot(
        values.get("du_upt_dt"),
        field="du_upt_dt",
        snapshot=snapshot,
    )

    base_index = _text(values.get("cu_base_index"), field="cu_base_index")
    strategy = _text(values.get("cu_strtegy"), field="cu_strtegy")
    currency_code = _text(values.get("pd_curr_cd"), field="pd_curr_cd")
    quality_inputs: dict[str, tuple[object | None, str, bool]] = {
        "base_index": (base_index, "base_index_missing", False),
        "strategy": (strategy, "strategy_missing", False),
        "leverage_factor": (leverage_factor, "leverage_factor_missing", False),
        "trading_currency": (
            "KRW" if currency_code == "CURR_CD_KRW" else None,
            "trading_currency_unconfirmed",
            False,
        ),
        "close_price": (close_price, "close_price_missing", True),
        "daily_trading_value": (
            daily_trading_value,
            "daily_trading_value_missing",
            False,
        ),
        "daily_volume": (daily_volume, "daily_volume_missing", True),
        "tracking_error_pct": (tracking_error, "tracking_error_pct_missing", True),
        "discrepancy_pct": (discrepancy, "discrepancy_pct_missing", True),
        "distribution_yield_pct": (
            distribution_yield,
            "distribution_yield_pct_missing",
            True,
        ),
        "distribution_payments_per_year": (
            distribution_payments_per_year,
            "distribution_payments_per_year_missing",
            True,
        ),
        "static_as_of": (
            static_as_of if static_date_present else None,
            "static_as_of_missing_snapshot_fallback",
            False,
        ),
        "dynamic_as_of": (
            dynamic_as_of if dynamic_date_present else None,
            "dynamic_as_of_missing_snapshot_fallback",
            False,
        ),
        **{name: (value, f"{name}_missing", False) for name, value in returns.items()},
        **{name: (value, f"{name}_missing", True) for name, value in volatility.items()},
    }
    quality_pairs = {
        name: _quality(value, missing_reason=reason, zero_is_unknown=zero_unknown)
        for name, (value, reason, zero_unknown) in quality_inputs.items()
    }

    return NormalizedDomesticEtpRecord(
        source_row=source_row,
        source_snapshot_date=snapshot,
        present_source_fields=present_source_fields,
        is_quarantined=is_quarantined,
        quarantine_reason=quarantine_reason,
        row_quality=QualityStatus.INVALID if is_quarantined else QualityStatus.VALID,
        source_values={column: _raw_scalar(values.get(column)) for column in SOURCE_COLUMNS},
        product_id=product_id,
        product_type=product_type,
        product_name=required_text("pd_nm", f"격리 행 {source_row}"),
        short_name=required_text("pd_abrv_nm", f"격리 행 {source_row}"),
        exchange_code=required_text("pd_exg_mkt_cd", "UNKNOWN"),
        ticker=required_text("pd_itm_no_ma", f"ROW{source_row}"),
        isin=source_key or "UNKNOWN",
        sellable=_boolean_code(values.get("pd_sale_yn"), field="pd_sale_yn"),
        trading_suspended=_boolean_code(values.get("pd_tr_yn"), field="pd_tr_yn"),
        asset_type=required_text("wu_inv_ast_type", "UNKNOWN"),
        investment_region=required_text("wu_inv_rgn", "UNKNOWN"),
        manager=required_text("cu_fund_mgmt_co", "UNKNOWN"),
        base_index=base_index,
        strategy=strategy,
        leverage_factor=leverage_factor,
        risk_level=required_text("pd_risk_nm", "UNKNOWN"),
        pension_eligible=_boolean_code(values.get("pd_pen_tr_yn"), field="pd_pen_tr_yn"),
        core_etf=_boolean_code(values.get("wu_core_yn"), field="wu_core_yn"),
        total_expense_ratio_pct=fee,
        total_expense_ratio_quality=fee_quality,
        total_expense_ratio_quality_reason=fee_reason,
        aum=aum,
        aum_quality=aum_quality,
        aum_quality_reason=aum_reason,
        close_price=close_price,
        one_day_return_pct=returns["one_day_return_pct"],
        one_month_return_pct=returns["one_month_return_pct"],
        three_month_return_pct=returns["three_month_return_pct"],
        six_month_return_pct=returns["six_month_return_pct"],
        one_year_return_pct=returns["one_year_return_pct"],
        ytd_return_pct=returns["ytd_return_pct"],
        daily_trading_value=daily_trading_value,
        daily_volume=daily_volume,
        tracking_error_pct=tracking_error,
        discrepancy_pct=discrepancy,
        twenty_day_volatility_pct=volatility["twenty_day_volatility_pct"],
        sixty_day_volatility_pct=volatility["sixty_day_volatility_pct"],
        one_hundred_twenty_day_volatility_pct=volatility["one_hundred_twenty_day_volatility_pct"],
        two_hundred_fifty_two_day_volatility_pct=volatility[
            "two_hundred_fifty_two_day_volatility_pct"
        ],
        distribution_yield_pct=distribution_yield,
        distribution_payments_per_year=distribution_payments_per_year,
        static_as_of=static_as_of,
        dynamic_as_of=dynamic_as_of,
        field_quality={name: quality for name, (quality, _) in quality_pairs.items()},
        field_quality_reasons={name: reason for name, (_, reason) in quality_pairs.items()},
    )


def iter_normalized_domestic_etp(
    data_path: str | Path,
) -> Iterator[NormalizedDomesticEtpRecord]:
    path = Path(data_path)
    with XlsxStream(path) as workbook:
        rows = workbook.iter_rows(0)
        try:
            header_row_number, header_cells = next(rows)
        except StopIteration as error:
            raise DomesticEtpNormalizationError(f"workbook has no header row: {path}") from error
        if header_row_number != 1:
            raise DomesticEtpNormalizationError(
                f"expected header at row 1, got {header_row_number}"
            )
        max_column = max(header_cells, default=-1)
        header = [
            _text(header_cells.get(index), field=f"header[{index}]", required=True)
            for index in range(max_column + 1)
        ]
        if len(header) != len(set(header)):
            raise DomesticEtpNormalizationError("workbook has duplicate header names")
        header_names = [str(name) for name in header]
        missing = sorted(set(SOURCE_COLUMNS) - set(header_names))
        if missing:
            raise DomesticEtpNormalizationError(f"workbook is missing source columns: {missing}")
        index = {name: position for position, name in enumerate(header_names)}

        for row_number, cells in rows:
            present_count = sum(
                not _semantic_missing(cells.get(column_index))
                for column_index in range(len(header_names))
            )
            values = {column: cells.get(index[column]) for column in SOURCE_COLUMNS}
            try:
                yield normalize_domestic_etp_row(
                    source_row=row_number,
                    values=values,
                    present_source_fields=present_count,
                )
            except (DomesticEtpNormalizationError, ValueError) as error:
                raise DomesticEtpNormalizationError(
                    f"failed to normalize {path.name} row {row_number}: {error}"
                ) from error
