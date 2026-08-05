from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from finance_agent_core.config import QualityStatus
from finance_agent_core.domain import (
    DatabaseManifest,
    NormalizedDomesticEtpRecord,
    NormalizedOverseasEtpRecord,
)
from finance_agent_core.storage import write_database, write_domestic_etp_database


def make_overseas_etp_record(
    *,
    row: int,
    ticker: str,
    aum: str | None,
    fee: str,
    product_type: str = "ETF",
    sellable: bool | None = True,
    trading_suspended: bool | None = False,
    asset_type: str | None = "Bond",
    region: str | None = "United States of America",
    quarantined: bool = False,
) -> NormalizedOverseasEtpRecord:
    fee_decimal = Decimal(fee)
    aum_decimal = None if aum is None else Decimal(aum)
    return NormalizedOverseasEtpRecord(
        source_row=row,
        source_snapshot_date=date(2026, 7, 11),
        present_source_fields=20 if quarantined else 49,
        is_quarantined=quarantined,
        quarantine_reason="sparse_source_row" if quarantined else None,
        row_quality=QualityStatus.PARTIAL if quarantined else QualityStatus.VALID,
        source_values={
            "pd_exg_mkt_cd": "AMX",
            "pd_itm_no": ticker,
            "pd_grp_no": product_type,
            "pd_nm": f"Test {ticker}",
            "pd_isin_cd": f"US{row:010d}",
            "pd_sale_yn": None if sellable is None else str(int(sellable)),
            "pd_tr_yn": None if trading_suspended is None else str(int(trading_suspended)),
            "wu_inv_ast_type": asset_type,
            "wu_inv_rgn": region,
            "cu_charge_rt": fee,
            "du_last_aum": aum,
            "pd_trd_ccy": "USD",
            "cu_upt_dt": "20260614",
            "du_upt_dt": "20260616",
        },
        product_id=f"AMX:{ticker}",
        product_type=product_type,
        product_name=f"Test {ticker}",
        exchange_code="AMX",
        ticker=ticker,
        isin=f"US{row:010d}",
        sellable=sellable,
        trading_suspended=trading_suspended,
        asset_type=asset_type,
        investment_region=region,
        total_expense_ratio_pct=fee_decimal,
        total_expense_ratio_quality=(
            QualityStatus.UNKNOWN if fee_decimal == 0 else QualityStatus.VALID
        ),
        total_expense_ratio_quality_reason=(
            "fee_zero_semantics_unconfirmed" if fee_decimal == 0 else None
        ),
        aum=aum_decimal,
        aum_quality=(
            QualityStatus.UNKNOWN
            if aum_decimal is None or aum_decimal == 0
            else QualityStatus.VALID
        ),
        aum_quality_reason=(
            "aum_missing"
            if aum_decimal is None
            else "aum_zero_semantics_unconfirmed"
            if aum_decimal == 0
            else None
        ),
        trading_currency="USD",
        static_as_of=date(2026, 6, 14),
        dynamic_as_of=date(2026, 6, 16),
    )


@pytest.fixture
def sample_records() -> list[NormalizedOverseasEtpRecord]:
    return [
        make_overseas_etp_record(row=2, ticker="B1", aum="1000", fee="0.10"),
        make_overseas_etp_record(row=3, ticker="B2", aum="3000", fee="0.15"),
        make_overseas_etp_record(row=4, ticker="B3", aum="2000", fee="0.20"),
        make_overseas_etp_record(row=5, ticker="B4", aum="4000", fee="0.05"),
        make_overseas_etp_record(row=6, ticker="B5", aum="5000", fee="0.12"),
        make_overseas_etp_record(row=7, ticker="B6", aum="6000", fee="0.18"),
        make_overseas_etp_record(row=8, ticker="Z0", aum="10000", fee="0"),
        make_overseas_etp_record(row=9, ticker="H1", aum="9000", fee="0.25"),
        make_overseas_etp_record(row=10, ticker="E1", aum="8000", fee="0.10", product_type="ETN"),
        make_overseas_etp_record(row=11, ticker="S0", aum="7000", fee="0.10", sellable=False),
        make_overseas_etp_record(
            row=12, ticker="T1", aum="7000", fee="0.10", trading_suspended=True
        ),
        make_overseas_etp_record(row=13, ticker="Q1", aum="20000", fee="0.10", quarantined=True),
    ]


@pytest.fixture
def sample_database(
    tmp_path: Path,
    sample_records: list[NormalizedOverseasEtpRecord],
) -> tuple[Path, list[NormalizedOverseasEtpRecord], DatabaseManifest]:
    path = tmp_path / "overseas_etp.sqlite3"
    quarantined = sum(record.is_quarantined for record in sample_records)
    manifest = DatabaseManifest(
        registry_schema_version="1.0",
        source_file_name="synthetic_overseas_etp.xlsx",
        source_file_sha256="a" * 64,
        source_file_size_bytes=1234,
        source_snapshot_date=date(2026, 7, 11),
        total_rows=len(sample_records),
        searchable_rows=len(sample_records) - quarantined,
        quarantined_rows=quarantined,
    )
    write_database(path, sample_records, manifest)
    return path, sample_records, manifest


def make_domestic_etp_record(
    *,
    row: int,
    ticker: str,
    one_month_return: str | None,
    product_type: str = "ETF",
    region: str = "미국",
    asset_type: str = "주식",
    pension_eligible: bool = True,
    quarantined: bool = False,
) -> NormalizedDomesticEtpRecord:
    product_id = f"KR7{row:09d}"
    one_month = None if one_month_return is None else Decimal(one_month_return)
    tracked = {
        "base_index": (None, "base_index_missing"),
        "strategy": ("실물복제", None),
        "leverage_factor": (Decimal("1"), None),
        "trading_currency": ("KRW", None),
        "close_price": (Decimal("10000"), None),
        "daily_trading_value": (Decimal("1000000"), None),
        "static_as_of": (date(2026, 6, 15), None),
        "dynamic_as_of": (date(2026, 6, 15), None),
        "one_day_return_pct": (Decimal("1"), None),
        "one_month_return_pct": (
            one_month,
            None if one_month is not None else "one_month_return_pct_missing",
        ),
        "three_month_return_pct": (Decimal("2"), None),
        "six_month_return_pct": (Decimal("3"), None),
        "one_year_return_pct": (Decimal("4"), None),
        "ytd_return_pct": (Decimal("5"), None),
    }
    quality = {
        field: QualityStatus.VALID if value is not None else QualityStatus.UNKNOWN
        for field, (value, _) in tracked.items()
    }
    return NormalizedDomesticEtpRecord(
        source_row=row,
        source_snapshot_date=date(2026, 7, 11),
        present_source_fields=16 if quarantined else 65,
        is_quarantined=quarantined,
        quarantine_reason="invalid_product_key" if quarantined else None,
        row_quality=QualityStatus.INVALID if quarantined else QualityStatus.VALID,
        source_values={
            "pd_itm_no": product_id,
            "pd_itm_no_ma": ticker,
            "pd_grp_no": product_type,
            "pd_nm": f"국내 테스트 {ticker}",
            "pd_abrv_nm": f"테스트 {ticker}",
            "pd_exg_mkt_cd": "EXG_MKT_NO_001",
            "pd_sale_yn": "1",
            "pd_tr_yn": "0",
            "pd_pen_tr_yn": "Y" if pension_eligible else "N",
            "pd_risk_nm": "높은위험(2등급)",
            "pd_curr_cd": "CURR_CD_KRW",
            "wu_inv_ast_type": asset_type,
            "wu_inv_rgn": region,
            "wu_core_yn": "N",
            "cu_fund_mgmt_co": "테스트운용",
            "cu_base_index": None,
            "cu_strtegy": "실물복제",
            "cu_lev_fector": "1",
            "cu_charge_rt": "0.4",
            "du_last_aum": "100000000",
            "du_clpr": "10000",
            "du_er_1d": "1",
            "du_er_1m": one_month_return,
            "du_er_3m": "2",
            "du_er_6m": "3",
            "du_er_1y": "4",
            "du_er_ytd": "5",
            "du_val_1d": "1000000",
            "wu_upt_dt": "20260615",
            "du_upt_dt": "20260615",
        },
        product_id=product_id,
        product_type=product_type,
        product_name=f"국내 테스트 {ticker}",
        short_name=f"테스트 {ticker}",
        exchange_code="EXG_MKT_NO_001",
        ticker=ticker,
        isin=product_id,
        sellable=True,
        trading_suspended=False,
        asset_type=asset_type,
        investment_region=region,
        manager="테스트운용",
        base_index=None,
        strategy="실물복제",
        leverage_factor=Decimal("1"),
        risk_level="높은위험(2등급)",
        pension_eligible=pension_eligible,
        core_etf=False,
        total_expense_ratio_pct=Decimal("0.4"),
        total_expense_ratio_quality=QualityStatus.VALID,
        total_expense_ratio_quality_reason=None,
        aum=Decimal("100000000"),
        aum_quality=QualityStatus.VALID,
        aum_quality_reason=None,
        close_price=Decimal("10000"),
        one_day_return_pct=Decimal("1"),
        one_month_return_pct=one_month,
        three_month_return_pct=Decimal("2"),
        six_month_return_pct=Decimal("3"),
        one_year_return_pct=Decimal("4"),
        ytd_return_pct=Decimal("5"),
        daily_trading_value=Decimal("1000000"),
        static_as_of=date(2026, 6, 15),
        dynamic_as_of=date(2026, 6, 15),
        field_quality=quality,
        field_quality_reasons={field: reason for field, (_, reason) in tracked.items()},
    )


@pytest.fixture
def domestic_sample_database(
    tmp_path: Path,
) -> tuple[Path, list[NormalizedDomesticEtpRecord], DatabaseManifest]:
    records = [
        make_domestic_etp_record(
            row=row,
            ticker=f"A{row:06d}",
            one_month_return=str(value),
        )
        for row, value in enumerate([10, 30, 20, 50, 40, 60], start=2)
    ]
    records.append(
        make_domestic_etp_record(
            row=8,
            ticker="Q000008",
            one_month_return="99",
            product_type="ETN",
        )
    )
    path = tmp_path / "domestic_etp.sqlite3"
    manifest = DatabaseManifest(
        dataset="domestic_etp",
        registry_schema_version="1.1",
        source_file_name="synthetic_domestic_etp.xlsx",
        source_file_sha256="b" * 64,
        source_file_size_bytes=1234,
        source_snapshot_date=date(2026, 7, 11),
        total_rows=len(records),
        searchable_rows=len(records),
        quarantined_rows=0,
    )
    write_domestic_etp_database(path, records, manifest)
    return path, records, manifest
