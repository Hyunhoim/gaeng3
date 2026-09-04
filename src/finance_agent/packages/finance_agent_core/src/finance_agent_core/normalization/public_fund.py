from __future__ import annotations

import datetime as dt
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from finance_agent_core.audit.xlsx import XlsxStream
from finance_agent_core.config import QualityStatus, load_field_registry
from finance_agent_core.domain import (
    NormalizedPublicFundAttribute,
    NormalizedPublicFundRecord,
    QuarantinedPublicFundRow,
)

SOURCE_COLUMNS = (
    "bmrk_eng_nm",
    "bmrk_nm",
    "curr_cd",
    "exchdg_yn",
    "fd_daily_bas_dt",
    "fd_estb_ctry_cd",
    "fd_ivst_rgn_desc",
    "fd_last_dstb_r",
    "fd_mm18_ern_r",
    "fd_mm1_ern_r",
    "fd_mm3_ern_r",
    "fd_mm6_ern_r",
    "fd_nast_suma",
    "fd_price_bas_dt",
    "fd_prsv_r",
    "fd_set_pcd",
    "fd_wk1_ern_r",
    "fd_yr1_ern_r",
    "fd_yr2_ern_r",
    "fd_yr3_ern_r",
    "fd_yr5_ern_r",
    "frc_bpr_itm_yn",
    "fss_itm_no",
    "hdge_fd_yn",
    "int_dvd_desc",
    "han_clas_sales_channel",
    "han_clas_fee_type",
    "itm_abrv_nm",
    "itm_eabrv_nm",
    "itm_eng_nm",
    "itm_nm",
    "itm_no",
    "kofia_fd_ccd",
    "ksd_itm_no",
    "mtco_itm_no",
    "ofsfd_yn",
    "or_attr_desc",
    "or_co_rwrd_r",
    "ofwk_trus_rwrd_r",
    "sale_co_rwrd_r",
    "or_co_xtn_itt_cd",
    "ovrs_fd_desc",
    "pers_corp_desc",
    "pfiv_sale_cntl_tcd",
    "prfd_attr_cds",
    "prfd_attr_cnt",
    "prfd_attr_search_text",
    "prvo_fd_desc",
    "prvo_pbff_desc",
    "rptt_ksd_itm_no",
    "sale_yn",
    "std_itm_no",
    "thco_sale_yn",
    "trusc_xtn_itt_cd",
    "trusc_rwrd_r",
    "zrin_btyp_nm",
    "zrin_dmst_bd_cmst_rt",
    "zrin_dmst_stk_cmst_rt",
    "zrin_fd_ivst_risk_gcd",
    "zrin_fd_ivst_risk_grd_nm",
    "zrin_ovrs_bd_cmst_rt",
    "zrin_ovrs_stk_cmst_rt",
)
PRODUCT_COLUMNS = SOURCE_COLUMNS
VALID_PRODUCT_KEY = re.compile(r"KR[A-Z0-9]{10}")
RISK_LEVEL_BY_CODE = {
    "1": "매우높은위험(1등급)",
    "2": "높은위험(2등급)",
    "3": "다소높은위험(3등급)",
    "4": "보통위험(4등급)",
    "5": "낮은위험(5등급)",
    "6": "매우낮은위험(6등급)",
}
FUND_ATTRIBUTES = {
    "MMF",
    "대출형",
    "임대형",
    "재간접",
    "주식형",
    "주식혼합",
    "채권형",
    "채권혼합",
    "특별자산",
    "혼합자산",
    "파생상품",
    "기타",
    "개발형",
}
SHORT_RETURN_SOURCES = {
    "one_week_return_pct": "fd_wk1_ern_r",
    "one_month_return_pct": "fd_mm1_ern_r",
    "three_month_return_pct": "fd_mm3_ern_r",
    "six_month_return_pct": "fd_mm6_ern_r",
}
LONG_RETURN_SOURCES = {
    "eighteen_month_return_pct": "fd_mm18_ern_r",
    "one_year_return_pct": "fd_yr1_ern_r",
    "two_year_return_pct": "fd_yr2_ern_r",
    "three_year_return_pct": "fd_yr3_ern_r",
    "five_year_return_pct": "fd_yr5_ern_r",
}
MICRO_PRECISION = Decimal("0.000001")
AUM_PRECISION = Decimal("0.0001")


class PublicFundNormalizationError(ValueError):
    """Raised when public-fund rows cannot be normalized without guessing."""


@dataclass(frozen=True)
class PublicFundNormalizationResult:
    products: tuple[NormalizedPublicFundRecord, ...]
    attributes: tuple[NormalizedPublicFundAttribute, ...]
    quarantine: tuple[QuarantinedPublicFundRow, ...]
    raw_rows: int


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
            raise PublicFundNormalizationError(f"{field} is required")
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
        raise PublicFundNormalizationError(f"{field} is not numeric: {rendered!r}") from error


def _quantized_decimal(value: Any, *, field: str, precision: Decimal) -> Decimal | None:
    parsed = _decimal(value, field=field)
    return None if parsed is None else parsed.quantize(precision)


def _date(value: Any, *, field: str) -> dt.date | None:
    rendered = _text(value, field=field)
    if rendered is None:
        return None
    if rendered.endswith(".0") and rendered[:-2].isdigit():
        rendered = rendered[:-2]
    for date_format in ("%Y%m%d", "%Y-%m-%d", "%Y-%m-%d %H:%M:%S"):
        try:
            return dt.datetime.strptime(rendered, date_format).date()
        except ValueError:
            continue
    raise PublicFundNormalizationError(f"{field} is not a supported date: {rendered!r}")


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


def _mapped_boolean(
    value: Any,
    *,
    field: str,
    mapping: Mapping[str, bool],
    required: bool = False,
) -> bool | None:
    rendered = _text(value, field=field, required=required)
    if rendered is None:
        return None
    try:
        return mapping[rendered]
    except KeyError as error:
        raise PublicFundNormalizationError(
            f"{field} has an unsupported boolean value: {rendered!r}"
        ) from error


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


def _normalize_product(
    *,
    source_row: int,
    present_source_fields: int,
    values: Mapping[str, Any],
    attribute_count: int,
    snapshot: dt.date,
) -> NormalizedPublicFundRecord:
    product_id = _text(values.get("itm_no"), field="itm_no", required=True) or ""
    product_name = _text(values.get("itm_nm"), field="itm_nm", required=True) or ""
    short_name = _text(values.get("itm_abrv_nm"), field="itm_abrv_nm", required=True) or ""
    currency = _text(values.get("curr_cd"), field="curr_cd", required=True) or ""
    if not re.fullmatch(r"[A-Z]{3}", currency):
        raise PublicFundNormalizationError(
            f"curr_cd is not an ISO-style currency code: {currency!r}"
        )

    public_offering = _mapped_boolean(
        values.get("prvo_pbff_desc"),
        field="prvo_pbff_desc",
        mapping={"공모": True, "사모": False},
    )
    sellable = _mapped_boolean(
        values.get("sale_yn"),
        field="sale_yn",
        mapping={"판매중": True, "판매완료": False},
        required=True,
    )
    company_sellable = _mapped_boolean(
        values.get("thco_sale_yn"),
        field="thco_sale_yn",
        mapping={"Y": True, "N": False},
    )
    currency_hedged = _mapped_boolean(
        values.get("exchdg_yn"),
        field="exchdg_yn",
        mapping={"Y": True, "N": False},
    )

    investment_region = _text(values.get("fd_ivst_rgn_desc"), field="fd_ivst_rgn_desc")
    geography = _text(values.get("ovrs_fd_desc"), field="ovrs_fd_desc")
    if geography == "해당없음":
        geography = None
    elif geography is not None and geography not in {"국내", "해외", "국내외혼합"}:
        raise PublicFundNormalizationError(
            f"ovrs_fd_desc is outside the frozen enum: {geography!r}"
        )
    investor_type = _text(values.get("pers_corp_desc"), field="pers_corp_desc")
    if investor_type is not None and investor_type not in {"해당없음", "개인", "법인"}:
        raise PublicFundNormalizationError(
            f"pers_corp_desc is outside the frozen enum: {investor_type!r}"
        )

    raw_fund_attribute = _text(values.get("or_attr_desc"), field="or_attr_desc")
    if raw_fund_attribute in {None, "06", "해당없음"}:
        fund_attribute = None
        fund_attribute_reason = (
            "fund_management_attribute_missing"
            if raw_fund_attribute is None
            else "fund_management_attribute_code_06_unconfirmed"
        )
    elif raw_fund_attribute in FUND_ATTRIBUTES:
        fund_attribute = raw_fund_attribute
        fund_attribute_reason = None
    else:
        raise PublicFundNormalizationError(
            f"or_attr_desc is outside the frozen enum: {raw_fund_attribute!r}"
        )

    raw_risk_name = _text(
        values.get("zrin_fd_ivst_risk_grd_nm"),
        field="zrin_fd_ivst_risk_grd_nm",
    )
    raw_risk_code = _text(
        values.get("zrin_fd_ivst_risk_gcd"),
        field="zrin_fd_ivst_risk_gcd",
    )
    if raw_risk_name is not None:
        risk_level = raw_risk_name
    elif raw_risk_code is None:
        risk_level = None
    else:
        try:
            risk_level = RISK_LEVEL_BY_CODE[raw_risk_code]
        except KeyError as error:
            raise PublicFundNormalizationError(
                f"zrin_fd_ivst_risk_gcd is outside 1..6: {raw_risk_code!r}"
            ) from error

    aum = _quantized_decimal(
        values.get("fd_nast_suma"), field="fd_nast_suma", precision=AUM_PRECISION
    )
    if aum is not None and aum < 0:
        raise PublicFundNormalizationError("fd_nast_suma cannot be negative")
    if aum is None:
        aum_quality = (QualityStatus.UNKNOWN, "aum_missing")
    elif aum == 0:
        aum_quality = (QualityStatus.UNKNOWN, "aum_zero_semantics_unconfirmed")
    else:
        aum_quality = (QualityStatus.VALID, None)

    short_returns = {
        name: _quantized_decimal(values.get(source), field=source, precision=MICRO_PRECISION)
        for name, source in SHORT_RETURN_SOURCES.items()
    }
    long_returns = {
        name: _quantized_decimal(values.get(source), field=source, precision=MICRO_PRECISION)
        for name, source in LONG_RETURN_SOURCES.items()
    }
    numeric_metrics = {
        "sale_company_fee_pct": _quantized_decimal(
            values.get("sale_co_rwrd_r"),
            field="sale_co_rwrd_r",
            precision=MICRO_PRECISION,
        ),
        "trustee_fee_pct": _quantized_decimal(
            values.get("trusc_rwrd_r"),
            field="trusc_rwrd_r",
            precision=MICRO_PRECISION,
        ),
        "domestic_stock_ratio_pct": _quantized_decimal(
            values.get("zrin_dmst_stk_cmst_rt"),
            field="zrin_dmst_stk_cmst_rt",
            precision=MICRO_PRECISION,
        ),
        "domestic_bond_ratio_pct": _quantized_decimal(
            values.get("zrin_dmst_bd_cmst_rt"),
            field="zrin_dmst_bd_cmst_rt",
            precision=MICRO_PRECISION,
        ),
        "overseas_stock_ratio_pct": _quantized_decimal(
            values.get("zrin_ovrs_stk_cmst_rt"),
            field="zrin_ovrs_stk_cmst_rt",
            precision=MICRO_PRECISION,
        ),
        "overseas_bond_ratio_pct": _quantized_decimal(
            values.get("zrin_ovrs_bd_cmst_rt"),
            field="zrin_ovrs_bd_cmst_rt",
            precision=MICRO_PRECISION,
        ),
        "latest_distribution_pct": _quantized_decimal(
            values.get("fd_last_dstb_r"),
            field="fd_last_dstb_r",
            precision=MICRO_PRECISION,
        ),
        "preservation_ratio_pct": _quantized_decimal(
            values.get("fd_prsv_r"),
            field="fd_prsv_r",
            precision=MICRO_PRECISION,
        ),
    }
    dynamic_source_date = _date(
        values.get("fd_price_bas_dt") or values.get("fd_daily_bas_dt"),
        field="fd_price_bas_dt_or_fd_daily_bas_dt",
    )
    dynamic_as_of = dynamic_source_date or snapshot

    quality_pairs: dict[str, tuple[QualityStatus, str | None]] = {
        "public_offering": _quality(
            public_offering,
            missing_reason="public_offering_missing",
        ),
        "sellable": (QualityStatus.VALID, None),
        "company_sellable": _quality(
            company_sellable,
            missing_reason="company_sellable_missing_not_false",
        ),
        "trading_currency": (QualityStatus.VALID, None),
        "investment_region": _quality(
            investment_region,
            missing_reason="investment_region_missing",
        ),
        "fund_geography_scope": _quality(
            geography,
            missing_reason="fund_geography_scope_missing",
        ),
        "fund_management_attribute": (
            QualityStatus.UNKNOWN if fund_attribute is None else QualityStatus.VALID,
            fund_attribute_reason,
        ),
        "investor_type": _quality(
            investor_type,
            missing_reason="investor_type_missing",
        ),
        "currency_hedged": _quality(
            currency_hedged,
            missing_reason="currency_hedged_missing",
        ),
        "risk_level": _quality(
            risk_level,
            missing_reason="risk_level_missing",
        ),
        "aum": aum_quality,
        "base_index": (
            QualityStatus.UNKNOWN,
            "benchmark_semantics_unconfirmed",
        ),
        "sales_channel": _quality(
            _text(values.get("han_clas_sales_channel"), field="han_clas_sales_channel"),
            missing_reason="sales_channel_missing",
        ),
        "interest_dividend_type": _quality(
            _text(values.get("int_dvd_desc"), field="int_dvd_desc"),
            missing_reason="interest_dividend_type_missing",
        ),
        "static_as_of": (QualityStatus.VALID, None),
        "dynamic_as_of": (
            QualityStatus.VALID if dynamic_source_date is not None else QualityStatus.PARTIAL,
            None if dynamic_source_date is not None else "dynamic_date_missing_uses_file_snapshot",
        ),
    }
    for field_name, value in short_returns.items():
        if value is None:
            quality_pairs[field_name] = (
                QualityStatus.UNKNOWN,
                f"{field_name}_missing",
            )
        else:
            quality_pairs[field_name] = (
                QualityStatus.PARTIAL,
                "raw_source_value_preserved_without_outlier_capping",
            )
    for field_name, value in long_returns.items():
        quality_pairs[field_name] = (
            (QualityStatus.UNKNOWN, f"{field_name}_missing")
            if value is None
            else (
                QualityStatus.PARTIAL,
                "raw_source_value_preserved_without_outlier_capping",
            )
        )
    for field_name, value in numeric_metrics.items():
        quality_pairs[field_name] = _numeric_quality(value, field=field_name)

    return NormalizedPublicFundRecord(
        source_row=source_row,
        source_snapshot_date=snapshot,
        present_source_fields=present_source_fields,
        source_values={column: _raw_scalar(values.get(column)) for column in PRODUCT_COLUMNS},
        attribute_count=attribute_count,
        product_id=product_id,
        product_name=product_name,
        short_name=short_name,
        public_offering=public_offering,
        sellable=bool(sellable),
        company_sellable=company_sellable,
        trading_currency=currency,
        investment_region=investment_region,
        fund_geography_scope=geography,
        fund_management_attribute=fund_attribute,
        investor_type=investor_type,
        currency_hedged=currency_hedged,
        risk_level=risk_level,
        aum=aum,
        base_index=_text(values.get("bmrk_nm"), field="bmrk_nm"),
        sales_channel=_text(
            values.get("han_clas_sales_channel"),
            field="han_clas_sales_channel",
        ),
        interest_dividend_type=_text(values.get("int_dvd_desc"), field="int_dvd_desc"),
        sale_company_fee_pct=numeric_metrics["sale_company_fee_pct"],
        trustee_fee_pct=numeric_metrics["trustee_fee_pct"],
        domestic_stock_ratio_pct=numeric_metrics["domestic_stock_ratio_pct"],
        domestic_bond_ratio_pct=numeric_metrics["domestic_bond_ratio_pct"],
        overseas_stock_ratio_pct=numeric_metrics["overseas_stock_ratio_pct"],
        overseas_bond_ratio_pct=numeric_metrics["overseas_bond_ratio_pct"],
        latest_distribution_pct=numeric_metrics["latest_distribution_pct"],
        preservation_ratio_pct=numeric_metrics["preservation_ratio_pct"],
        one_week_return_pct=short_returns["one_week_return_pct"],
        one_month_return_pct=short_returns["one_month_return_pct"],
        three_month_return_pct=short_returns["three_month_return_pct"],
        six_month_return_pct=short_returns["six_month_return_pct"],
        eighteen_month_return_pct=long_returns["eighteen_month_return_pct"],
        one_year_return_pct=long_returns["one_year_return_pct"],
        two_year_return_pct=long_returns["two_year_return_pct"],
        three_year_return_pct=long_returns["three_year_return_pct"],
        five_year_return_pct=long_returns["five_year_return_pct"],
        static_as_of=snapshot,
        dynamic_as_of=dynamic_as_of,
        field_quality={name: quality for name, (quality, _) in quality_pairs.items()},
        field_quality_reasons={name: reason for name, (_, reason) in quality_pairs.items()},
    )


def normalize_public_fund_rows(
    rows: Iterable[tuple[int, int, Mapping[str, Any]]],
    *,
    source_snapshot_date: dt.date | None = None,
) -> PublicFundNormalizationResult:
    registry = load_field_registry()
    snapshot = source_snapshot_date or registry.datasets["fund"].snapshot_date
    product_keys: set[str] = set()
    attributes: list[NormalizedPublicFundAttribute] = []
    quarantine: list[QuarantinedPublicFundRow] = []
    products: list[NormalizedPublicFundRecord] = []
    raw_row_count = 0

    for source_row, present_source_fields, row_values in rows:
        raw_row_count += 1
        values = {column: row_values.get(column) for column in SOURCE_COLUMNS}
        item_number = _text(values.get("itm_no"), field="itm_no")
        aggregate_attribute_code = (
            _text(
                values.get("prfd_attr_cds"),
                field="prfd_attr_cds",
            )
            or "__NO_ATTRIBUTE__"
        )
        if item_number is None or VALID_PRODUCT_KEY.fullmatch(item_number) is None:
            quarantine.append(
                QuarantinedPublicFundRow(
                    source_row=source_row,
                    source_snapshot_date=snapshot,
                    present_source_fields=present_source_fields,
                    raw_item_number=item_number,
                    raw_attribute_code=aggregate_attribute_code,
                    quarantine_reason="invalid_product_key",
                    source_values={
                        column: _raw_scalar(values.get(column)) for column in SOURCE_COLUMNS
                    },
                )
            )
            continue

        if item_number in product_keys:
            raise PublicFundNormalizationError(
                f"duplicate product key at source row {source_row}: {item_number!r}"
            )
        product_keys.add(item_number)
        attributes.append(
            NormalizedPublicFundAttribute(
                source_row=source_row,
                product_id=item_number,
                attribute_code=aggregate_attribute_code,
            )
        )
        products.append(
            _normalize_product(
                source_row=source_row,
                present_source_fields=present_source_fields,
                values=values,
                attribute_count=1,
                snapshot=snapshot,
            )
        )
    return PublicFundNormalizationResult(
        products=tuple(products),
        attributes=tuple(
            sorted(
                attributes,
                key=lambda value: (
                    value.product_id,
                    value.attribute_code,
                    value.source_row,
                ),
            )
        ),
        quarantine=tuple(sorted(quarantine, key=lambda value: value.source_row)),
        raw_rows=raw_row_count,
    )


def normalize_public_fund_workbook(
    data_path: str | Path,
) -> PublicFundNormalizationResult:
    path = Path(data_path)
    with XlsxStream(path) as workbook:
        rows = workbook.iter_rows(0)
        try:
            header_row_number, header_cells = next(rows)
        except StopIteration as error:
            raise PublicFundNormalizationError(f"workbook has no header row: {path}") from error
        if header_row_number != 1:
            raise PublicFundNormalizationError(f"expected header at row 1, got {header_row_number}")
        max_column = max(header_cells, default=-1)
        header = [
            _text(header_cells.get(index), field=f"header[{index}]", required=True)
            for index in range(max_column + 1)
        ]
        header_names = [str(name) for name in header]
        if len(header_names) != len(set(header_names)):
            raise PublicFundNormalizationError("workbook has duplicate header names")
        missing = sorted(set(SOURCE_COLUMNS) - set(header_names))
        if missing:
            raise PublicFundNormalizationError(
                f"workbook is missing required source columns: {missing}"
            )
        index = {name: position for position, name in enumerate(header_names)}

        def normalized_rows() -> Iterable[tuple[int, int, Mapping[str, Any]]]:
            for row_number, cells in rows:
                present_count = sum(
                    not _semantic_missing(cells.get(column_index))
                    for column_index in range(len(header_names))
                )
                yield (
                    row_number,
                    present_count,
                    {column: cells.get(index[column]) for column in SOURCE_COLUMNS},
                )

        try:
            return normalize_public_fund_rows(normalized_rows())
        except (PublicFundNormalizationError, ValueError) as error:
            raise PublicFundNormalizationError(
                f"failed to normalize {path.name}: {error}"
            ) from error
