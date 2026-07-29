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
    "PD_NO",
    "PD_EXG_MKT",
    "PD_NM",
    "PD_ABRV_NM",
    "PD_CTRY_CD",
    "PD_PBCM",
    "STD_PD_MCLS_NM",
    "STD_PD_SCLS_NM",
    "BD_KND",
    "CURR_CD",
    "ISU_BAL_AMT",
    "ISU_DT",
    "MAT_DT",
    "SRFC_IRT",
    "PD_RISK_GCD",
    "PD_STD_INFO_UPDATE",
    "BUY_YIELD",
    "AFTER_TAX_YIELD",
    "BUYABLE_QUANTITY",
    "REMAINING_DAYS",
    "DUR",
    "CRD_GRD",
    "CRD_GRD_DT",
)
DYNAMIC_STALE_REASON = "dynamic_value_as_of_2026-02-24_before_snapshot"
MICRO_PRECISION = Decimal("0.000001")


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


def normalize_bond_row(
    *,
    source_row: int,
    values: Mapping[str, Any],
    present_source_fields: int,
    source_snapshot_date: dt.date | None = None,
) -> NormalizedBondRecord:
    registry = load_field_registry()
    snapshot = source_snapshot_date or registry.datasets["bond"].snapshot_date

    product_id = _text(values.get("PD_NO"), field="PD_NO", required=True)
    product_name = _text(values.get("PD_NM"), field="PD_NM", required=True)
    bond_market = _text(values.get("PD_EXG_MKT"), field="PD_EXG_MKT", required=True)
    if bond_market not in {"장내", "장외"}:
        raise BondNormalizationError(f"PD_EXG_MKT has an unsupported value: {bond_market!r}")

    issue_amount = _decimal(values.get("ISU_BAL_AMT"), field="ISU_BAL_AMT")
    if issue_amount is None or issue_amount < 0:
        raise BondNormalizationError("ISU_BAL_AMT must be a non-negative number")
    coupon_rate = _micro_decimal(values.get("SRFC_IRT"), field="SRFC_IRT")
    if coupon_rate is not None and coupon_rate < 0:
        raise BondNormalizationError("SRFC_IRT cannot be negative")
    buyable_quantity = _decimal(values.get("BUYABLE_QUANTITY"), field="BUYABLE_QUANTITY")
    if buyable_quantity is not None and buyable_quantity < 0:
        raise BondNormalizationError("BUYABLE_QUANTITY cannot be negative")
    duration = _micro_decimal(values.get("DUR"), field="DUR")
    if duration is not None and duration < 0:
        raise BondNormalizationError("DUR cannot be negative")

    issue_date = _date(values.get("ISU_DT"), field="ISU_DT")
    maturity_date = _date(values.get("MAT_DT"), field="MAT_DT")
    dynamic_source_date = _date(
        values.get("PD_STD_INFO_UPDATE"),
        field="PD_STD_INFO_UPDATE",
    )
    dynamic_as_of = dynamic_source_date or snapshot
    remaining_days = None if maturity_date is None else (maturity_date - snapshot).days
    currently_buyable = (
        None
        if buyable_quantity is None or maturity_date is None
        else buyable_quantity > 0 and maturity_date >= snapshot
    )

    buy_yield = _micro_decimal(values.get("BUY_YIELD"), field="BUY_YIELD")
    after_tax_yield = _micro_decimal(
        values.get("AFTER_TAX_YIELD"),
        field="AFTER_TAX_YIELD",
    )
    short_name = _text(values.get("PD_ABRV_NM"), field="PD_ABRV_NM")
    issuer = _text(values.get("PD_PBCM"), field="PD_PBCM")
    bond_subclass = _text(values.get("STD_PD_SCLS_NM"), field="STD_PD_SCLS_NM")
    bond_type = _text(values.get("BD_KND"), field="BD_KND")
    credit_rating = _text(values.get("CRD_GRD"), field="CRD_GRD")
    currency = _text(values.get("CURR_CD"), field="CURR_CD", required=True)

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
            present_quality=QualityStatus.PARTIAL,
            present_reason=DYNAMIC_STALE_REASON,
        ),
        "after_tax_yield_pct": _quality(
            after_tax_yield,
            missing_reason="after_tax_yield_missing",
            present_quality=QualityStatus.PARTIAL,
            present_reason=DYNAMIC_STALE_REASON,
        ),
        "buyable_quantity": _quality(
            buyable_quantity,
            missing_reason="buyable_quantity_missing",
            present_quality=QualityStatus.PARTIAL,
            present_reason=DYNAMIC_STALE_REASON,
        ),
        "currently_buyable": _quality(
            currently_buyable,
            missing_reason="buyable_quantity_or_maturity_missing",
            present_quality=QualityStatus.PARTIAL,
            present_reason=DYNAMIC_STALE_REASON,
        ),
        "remaining_days": _quality(
            remaining_days,
            missing_reason="maturity_date_missing",
        ),
        "duration_years": _quality(
            duration,
            missing_reason="duration_missing",
            present_quality=QualityStatus.PARTIAL,
            present_reason=DYNAMIC_STALE_REASON,
        ),
        "static_as_of": (QualityStatus.VALID, None),
        "dynamic_as_of": _quality(
            dynamic_source_date,
            missing_reason="dynamic_as_of_missing_snapshot_fallback",
            present_quality=QualityStatus.PARTIAL,
            present_reason=DYNAMIC_STALE_REASON,
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
        ticker=product_id or "",
        short_name=short_name,
        bond_market=bond_market,
        issuer=issuer,
        bond_major_class=_text(
            values.get("STD_PD_MCLS_NM"),
            field="STD_PD_MCLS_NM",
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
            values.get("PD_RISK_GCD"),
            field="PD_RISK_GCD",
            required=True,
        )
        or "",
        buy_yield_pct=buy_yield,
        after_tax_yield_pct=after_tax_yield,
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
