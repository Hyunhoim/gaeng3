from __future__ import annotations

import datetime as dt
from collections.abc import Iterator, Mapping
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from finance_agent_core.audit.xlsx import XlsxStream
from finance_agent_core.config import QualityStatus, load_field_registry
from finance_agent_core.domain import NormalizedBondRecord

SOURCE_COLUMNS = (
    "pd_no",
    "pd_exg_mkt",
    "info_base_dt",
    "info_seq",
    "pd_nm",
    "pd_abrv_nm",
    "pd_ctry_cd",
    "pd_pbcm",
    "std_pd_mcls_nm",
    "std_pd_scls_nm",
    "bd_knd",
    "curr_cd",
    "isu_bal_amt",
    "isu_dt",
    "mat_dt",
    "srfc_irt",
    "pd_risk_gcd",
    "pd_risk_nm",
    "bd_ofr_tcd",
    "pd_pen_tr_yn",
    "bdbns_abl_chnl_nm",
    "bd_tisu_a",
    "pd_std_info_update",
    "buy_yield",
    "after_tax_yield",
    "applied_yield",
    "eval_price",
    "dirty",
    "cov",
    "buyable_quantity",
    "remaining_days",
    "dur",
    "crd_grd",
    "crd_grd_dt",
)
OFFICIAL_BUYABILITY_REASON = "official_2026-08-24_notice_ignores_buyable_quantity"
MICRO_PRECISION = Decimal("0.000001")
MONEY_PRECISION = Decimal("0.01")


class BondNormalizationError(ValueError):
    """Raised when a domestic bond row cannot be normalized safely."""


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
            raise BondNormalizationError(f"{field} is required")
        return None
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, dt.datetime):
        return value.isoformat(sep=" ")
    return str(value).strip()


def _decimal(value: Any, *, field: str) -> Decimal | None:
    rendered = _text(value, field=field)
    if rendered is None:
        return None
    try:
        return Decimal(rendered.replace(",", ""))
    except InvalidOperation as error:
        raise BondNormalizationError(f"{field} is not numeric: {rendered!r}") from error


def _micro_decimal(value: Any, *, field: str) -> Decimal | None:
    parsed = _decimal(value, field=field)
    return None if parsed is None else parsed.quantize(MICRO_PRECISION)


def _date(value: Any, *, field: str) -> dt.date | None:
    if _semantic_missing(value):
        return None
    if isinstance(value, dt.datetime):
        return value.date()
    if isinstance(value, dt.date):
        return value
    rendered = _text(value, field=field)
    if rendered is not None and rendered.endswith(".0") and rendered[:-2].isdigit():
        rendered = rendered[:-2]
    if rendered in {"0", "00000000"}:
        return None
    for date_format in ("%Y%m%d", "%Y-%m-%d", "%Y-%m-%d %H:%M:%S"):
        try:
            return dt.datetime.strptime(rendered or "", date_format).date()
        except ValueError:
            continue
    raise BondNormalizationError(f"{field} is not a supported date: {rendered!r}")


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
    present_quality: QualityStatus = QualityStatus.VALID,
    present_reason: str | None = None,
) -> tuple[QualityStatus, str | None]:
    if value is None:
        return QualityStatus.UNKNOWN, missing_reason
    return present_quality, present_reason


def _numeric_quality(value: Decimal | None, *, field: str) -> tuple[QualityStatus, str | None]:
    if value is None:
        return QualityStatus.UNKNOWN, f"{field}_missing"
    if value == 0:
        return QualityStatus.UNKNOWN, f"{field}_zero_excluded_by_official_policy"
    return QualityStatus.VALID, None


def normalize_bond_row(
    *,
    source_row: int,
    values: Mapping[str, Any],
    present_source_fields: int,
    source_snapshot_date: dt.date | None = None,
) -> NormalizedBondRecord:
    registry = load_field_registry()
    snapshot = source_snapshot_date or registry.datasets["bond"].snapshot_date

    ticker = _text(values.get("pd_no"), field="pd_no", required=True) or ""
    bond_market = _text(values.get("pd_exg_mkt"), field="pd_exg_mkt", required=True)
    info_base_date = _date(values.get("info_base_dt"), field="info_base_dt")
    info_sequence = _text(values.get("info_seq"), field="info_seq", required=True) or ""
    if info_base_date is None:
        raise BondNormalizationError("info_base_dt is required")
    product_id = f"{ticker}:{bond_market}:{info_base_date.strftime('%Y%m%d')}:{info_sequence}"
    product_name = _text(values.get("pd_nm"), field="pd_nm", required=True)
    if bond_market not in {"장내", "장외"}:
        raise BondNormalizationError(f"PD_EXG_MKT has an unsupported value: {bond_market!r}")

    raw_issue_amount = _decimal(values.get("isu_bal_amt"), field="isu_bal_amt")
    issue_amount = None if raw_issue_amount is None else raw_issue_amount.quantize(MONEY_PRECISION)
    if issue_amount is None or issue_amount < 0:
        raise BondNormalizationError("isu_bal_amt must be a non-negative number")
    coupon_rate = _micro_decimal(values.get("srfc_irt"), field="srfc_irt")
    if coupon_rate is not None and coupon_rate < 0:
        raise BondNormalizationError("srfc_irt cannot be negative")
    buyable_quantity = _decimal(values.get("buyable_quantity"), field="buyable_quantity")
    if buyable_quantity is not None and buyable_quantity < 0:
        raise BondNormalizationError("buyable_quantity cannot be negative")
    duration = _micro_decimal(values.get("dur"), field="dur")
    if duration is not None and duration < 0:
        raise BondNormalizationError("dur cannot be negative")

    issue_date = _date(values.get("isu_dt"), field="isu_dt")
    maturity_date = _date(values.get("mat_dt"), field="mat_dt")
    dynamic_source_date = _date(
        values.get("pd_std_info_update"),
        field="pd_std_info_update",
    )
    dynamic_as_of = dynamic_source_date or snapshot
    remaining_days = None if maturity_date is None else (maturity_date - snapshot).days
    currently_buyable = True

    buy_yield = _micro_decimal(values.get("buy_yield"), field="buy_yield")
    after_tax_yield = _micro_decimal(
        values.get("after_tax_yield"),
        field="after_tax_yield",
    )
    applied_yield = _micro_decimal(values.get("applied_yield"), field="applied_yield")
    evaluation_price = _micro_decimal(values.get("eval_price"), field="eval_price")
    dirty_price = _micro_decimal(values.get("dirty"), field="dirty")
    convexity = _micro_decimal(values.get("cov"), field="cov")
    short_name = _text(values.get("pd_abrv_nm"), field="pd_abrv_nm")
    issuer = _text(values.get("pd_pbcm"), field="pd_pbcm")
    bond_subclass = _text(values.get("std_pd_scls_nm"), field="std_pd_scls_nm")
    bond_type = _text(values.get("bd_knd"), field="bd_knd")
    credit_rating = _text(values.get("crd_grd"), field="crd_grd")
    currency = _text(values.get("curr_cd"), field="curr_cd", required=True)

    quality_pairs = {
        "short_name": _quality(short_name, missing_reason="short_name_missing"),
        "issuer": _quality(issuer, missing_reason="issuer_missing"),
        "bond_subclass": _quality(bond_subclass, missing_reason="bond_subclass_missing"),
        "bond_type": _quality(bond_type, missing_reason="bond_type_missing"),
        "trading_currency": _quality(
            None if currency == "000" else currency,
            missing_reason="trading_currency_code_000_unconfirmed",
        ),
        "issue_date": _quality(issue_date, missing_reason="issue_date_missing"),
        "maturity_date": _quality(maturity_date, missing_reason="maturity_date_missing"),
        "coupon_rate_pct": _quality(coupon_rate, missing_reason="coupon_rate_missing"),
        "credit_rating": _quality(credit_rating, missing_reason="credit_rating_missing"),
        "bond_risk_code": (
            QualityStatus.PARTIAL,
            "risk_code_semantics_unconfirmed",
        ),
        "buy_yield_pct": _quality(
            buy_yield,
            missing_reason="buy_yield_missing",
            present_quality=QualityStatus.VALID,
        ),
        "after_tax_yield_pct": _quality(
            after_tax_yield,
            missing_reason="after_tax_yield_missing",
            present_quality=QualityStatus.VALID,
        ),
        "applied_yield_pct": _numeric_quality(
            applied_yield,
            field="applied_yield_pct",
        ),
        "evaluation_price": _numeric_quality(
            evaluation_price,
            field="evaluation_price",
        ),
        "dirty_price": _numeric_quality(
            dirty_price,
            field="dirty_price",
        ),
        "convexity": _numeric_quality(convexity, field="convexity"),
        "buyable_quantity": _quality(
            buyable_quantity,
            missing_reason="buyable_quantity_missing",
            present_quality=QualityStatus.UNKNOWN,
            present_reason=OFFICIAL_BUYABILITY_REASON,
        ),
        "currently_buyable": _quality(
            currently_buyable,
            missing_reason="buyable_quantity_or_maturity_missing",
            present_quality=QualityStatus.VALID,
            present_reason=OFFICIAL_BUYABILITY_REASON,
        ),
        "remaining_days": _quality(
            remaining_days,
            missing_reason="maturity_date_missing",
        ),
        "duration_years": _quality(
            duration,
            missing_reason="duration_missing",
            present_quality=QualityStatus.VALID,
        ),
        "static_as_of": (QualityStatus.VALID, None),
        "dynamic_as_of": _quality(
            dynamic_source_date,
            missing_reason="dynamic_as_of_missing_snapshot_fallback",
            present_quality=QualityStatus.VALID,
        ),
    }

    return NormalizedBondRecord(
        source_row=source_row,
        source_snapshot_date=snapshot,
        present_source_fields=present_source_fields,
        row_quality=QualityStatus.VALID,
        source_values={column: _raw_scalar(values.get(column)) for column in SOURCE_COLUMNS},
        product_id=product_id or "",
        product_name=product_name or "",
        ticker=ticker,
        short_name=short_name,
        bond_market=bond_market,
        issuer=issuer,
        bond_major_class=_text(
            values.get("std_pd_mcls_nm"),
            field="std_pd_mcls_nm",
            required=True,
        )
        or "",
        bond_subclass=bond_subclass,
        bond_type=bond_type,
        trading_currency=currency or "",
        issue_amount=issue_amount,
        issue_date=issue_date,
        maturity_date=maturity_date,
        coupon_rate_pct=coupon_rate,
        credit_rating=credit_rating,
        bond_risk_code=_text(
            values.get("pd_risk_gcd"),
            field="pd_risk_gcd",
            required=True,
        )
        or "",
        buy_yield_pct=buy_yield,
        after_tax_yield_pct=after_tax_yield,
        applied_yield_pct=applied_yield,
        evaluation_price=evaluation_price,
        dirty_price=dirty_price,
        convexity=convexity,
        buyable_quantity=buyable_quantity,
        currently_buyable=currently_buyable,
        remaining_days=remaining_days,
        duration_years=duration,
        static_as_of=snapshot,
        dynamic_as_of=dynamic_as_of,
        field_quality={name: quality for name, (quality, _) in quality_pairs.items()},
        field_quality_reasons={name: reason for name, (_, reason) in quality_pairs.items()},
    )


def iter_normalized_bonds(data_path: str | Path) -> Iterator[NormalizedBondRecord]:
    path = Path(data_path)
    with XlsxStream(path) as workbook:
        rows = workbook.iter_rows(0)
        try:
            header_row_number, header_cells = next(rows)
        except StopIteration as error:
            raise BondNormalizationError(f"workbook has no header row: {path}") from error
        if header_row_number != 1:
            raise BondNormalizationError(f"expected header at row 1, got {header_row_number}")
        max_column = max(header_cells, default=-1)
        header = [
            _text(header_cells.get(index), field=f"header[{index}]", required=True)
            for index in range(max_column + 1)
        ]
        if len(header) != len(set(header)):
            raise BondNormalizationError("workbook has duplicate header names")
        header_names = [str(name) for name in header]
        missing = sorted(set(SOURCE_COLUMNS) - set(header_names))
        if missing:
            raise BondNormalizationError(f"workbook is missing source columns: {missing}")
        index = {name: position for position, name in enumerate(header_names)}

        for row_number, cells in rows:
            present_count = sum(
                not _semantic_missing(cells.get(column_index))
                for column_index in range(len(header_names))
            )
            values = {column: cells.get(index[column]) for column in SOURCE_COLUMNS}
            try:
                yield normalize_bond_row(
                    source_row=row_number,
                    values=values,
                    present_source_fields=present_count,
                )
            except (BondNormalizationError, ValueError) as error:
                raise BondNormalizationError(
                    f"failed to normalize {path.name} row {row_number}: {error}"
                ) from error
