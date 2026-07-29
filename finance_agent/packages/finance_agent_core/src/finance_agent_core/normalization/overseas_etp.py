from __future__ import annotations

import datetime as dt
from collections.abc import Iterator, Mapping
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from finance_agent_core.audit.xlsx import XlsxStream
from finance_agent_core.config import QualityStatus, load_field_registry
from finance_agent_core.domain import NormalizedOverseasEtpRecord

SOURCE_COLUMNS = (
    "pd_exg_mkt_cd",
    "pd_itm_no",
    "pd_grp_no",
    "pd_nm",
    "pd_isin_cd",
    "pd_sale_yn",
    "pd_tr_yn",
    "wu_inv_ast_type",
    "wu_inv_rgn",
    "cu_charge_rt",
    "du_last_aum",
    "pd_trd_ccy",
    "cu_upt_dt",
    "du_upt_dt",
)
SPARSE_PRESENT_FIELD_THRESHOLD = 40


class NormalizationError(ValueError):
    """Raised when a source row cannot be normalized without guessing."""


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
            raise NormalizationError(f"{field} is required")
        return None
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, dt.datetime):
        return value.isoformat(sep=" ")
    return str(value).strip()


def _decimal(value: Any, *, field: str, required: bool = False) -> Decimal | None:
    text = _text(value, field=field, required=required)
    if text is None:
        return None
    try:
        return Decimal(text.replace(",", ""))
    except InvalidOperation as error:
        raise NormalizationError(f"{field} is not numeric: {text!r}") from error


def _date(value: Any, *, field: str) -> dt.date:
    if isinstance(value, dt.datetime):
        return value.date()
    if isinstance(value, dt.date):
        return value
    text = _text(value, field=field, required=True)
    assert text is not None
    if text.endswith(".0") and text[:-2].isdigit():
        text = text[:-2]
    for date_format in ("%Y%m%d", "%Y-%m-%d", "%Y-%m-%d %H:%M:%S"):
        try:
            return dt.datetime.strptime(text, date_format).date()
        except ValueError:
            continue
    raise NormalizationError(f"{field} is not a supported date: {text!r}")


def _boolean_code(value: Any, *, field: str) -> bool | None:
    text = _text(value, field=field)
    if text is None:
        return None
    if text not in {"0", "1"}:
        raise NormalizationError(f"{field} has an unknown boolean code: {text!r}")
    return text == "1"


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


def normalize_overseas_etp_row(
    *,
    source_row: int,
    values: Mapping[str, Any],
    present_source_fields: int,
    source_snapshot_date: dt.date | None = None,
) -> NormalizedOverseasEtpRecord:
    registry = load_field_registry()
    snapshot_date = source_snapshot_date or registry.datasets["overseas_etp"].snapshot_date

    exchange_code = _text(values.get("pd_exg_mkt_cd"), field="pd_exg_mkt_cd", required=True)
    ticker = _text(values.get("pd_itm_no"), field="pd_itm_no", required=True)
    product_type = _text(values.get("pd_grp_no"), field="pd_grp_no", required=True)
    product_name = _text(values.get("pd_nm"), field="pd_nm", required=True)
    trading_currency = _text(values.get("pd_trd_ccy"), field="pd_trd_ccy", required=True)
    assert exchange_code is not None
    assert ticker is not None
    assert product_type is not None
    assert product_name is not None
    assert trading_currency is not None

    fee = _decimal(values.get("cu_charge_rt"), field="cu_charge_rt", required=True)
    assert fee is not None
    if fee < 0:
        raise NormalizationError("cu_charge_rt cannot be negative")
    if fee == 0:
        fee_quality = QualityStatus.UNKNOWN
        fee_reason = "fee_zero_semantics_unconfirmed"
    else:
        fee_quality = QualityStatus.VALID
        fee_reason = None

    aum = _decimal(values.get("du_last_aum"), field="du_last_aum")
    if aum is not None and aum < 0:
        raise NormalizationError("du_last_aum cannot be negative")
    if aum is None:
        aum_quality = QualityStatus.UNKNOWN
        aum_reason = "aum_missing"
    elif aum == 0:
        aum_quality = QualityStatus.UNKNOWN
        aum_reason = "aum_zero_semantics_unconfirmed"
    else:
        aum_quality = QualityStatus.VALID
        aum_reason = None

    is_quarantined = present_source_fields < SPARSE_PRESENT_FIELD_THRESHOLD
    quarantine_reason = "sparse_source_row" if is_quarantined else None

    return NormalizedOverseasEtpRecord(
        source_row=source_row,
        source_snapshot_date=snapshot_date,
        present_source_fields=present_source_fields,
        is_quarantined=is_quarantined,
        quarantine_reason=quarantine_reason,
        row_quality=QualityStatus.PARTIAL if is_quarantined else QualityStatus.VALID,
        source_values={column: _raw_scalar(values.get(column)) for column in SOURCE_COLUMNS},
        product_id=f"{exchange_code}:{ticker}",
        product_type=product_type,
        product_name=product_name,
        exchange_code=exchange_code,
        ticker=ticker,
        isin=_text(values.get("pd_isin_cd"), field="pd_isin_cd"),
        sellable=_boolean_code(values.get("pd_sale_yn"), field="pd_sale_yn"),
        trading_suspended=_boolean_code(values.get("pd_tr_yn"), field="pd_tr_yn"),
        asset_type=_text(values.get("wu_inv_ast_type"), field="wu_inv_ast_type"),
        investment_region=_text(values.get("wu_inv_rgn"), field="wu_inv_rgn"),
        total_expense_ratio_pct=fee,
        total_expense_ratio_quality=fee_quality,
        total_expense_ratio_quality_reason=fee_reason,
        aum=aum,
        aum_quality=aum_quality,
        aum_quality_reason=aum_reason,
        trading_currency=trading_currency,
        static_as_of=_date(values.get("cu_upt_dt"), field="cu_upt_dt"),
        dynamic_as_of=_date(values.get("du_upt_dt"), field="du_upt_dt"),
    )


def iter_normalized_overseas_etp(
    data_path: str | Path,
) -> Iterator[NormalizedOverseasEtpRecord]:
    path = Path(data_path)
    with XlsxStream(path) as workbook:
        rows = workbook.iter_rows(0)
        try:
            header_row_number, header_cells = next(rows)
        except StopIteration as error:
            raise NormalizationError(f"workbook has no header row: {path}") from error
        if header_row_number != 1:
            raise NormalizationError(f"expected header at row 1, got {header_row_number}")
        max_column = max(header_cells, default=-1)
        header = [
            _text(header_cells.get(index), field=f"header[{index}]", required=True)
            for index in range(max_column + 1)
        ]
        if len(header) != len(set(header)):
            raise NormalizationError("workbook has duplicate header names")
        header_names = [str(name) for name in header]
        missing = sorted(set(SOURCE_COLUMNS) - set(header_names))
        if missing:
            raise NormalizationError(f"workbook is missing source columns: {missing}")
        index = {name: position for position, name in enumerate(header_names)}

        for row_number, cells in rows:
            present_count = sum(
                not _semantic_missing(cells.get(column_index))
                for column_index in range(len(header_names))
            )
            values = {column: cells.get(index[column]) for column in SOURCE_COLUMNS}
            try:
                yield normalize_overseas_etp_row(
                    source_row=row_number,
                    values=values,
                    present_source_fields=present_count,
                )
            except (NormalizationError, ValueError) as error:
                raise NormalizationError(
                    f"failed to normalize {path.name} row {row_number}: {error}"
                ) from error
