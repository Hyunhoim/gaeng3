from datetime import date
from decimal import Decimal

from finance_agent_core.config import QualityStatus
from finance_agent_core.normalization import normalize_overseas_etp_row


def test_zero_sentinels_and_sparse_rows_remain_explicit() -> None:
    record = normalize_overseas_etp_row(
        source_row=1956,
        present_source_fields=20,
        source_snapshot_date=date(2026, 7, 11),
        values={
            "pd_exg_mkt_cd": "AMX",
            "pd_itm_no": "ZERO",
            "pd_grp_no": "ETF",
            "pd_nm": "Zero Test ETF",
            "pd_isin_cd": None,
            "pd_sale_yn": None,
            "pd_tr_yn": None,
            "wu_inv_ast_type": None,
            "wu_inv_rgn": None,
            "cu_charge_rt": Decimal("0.000000"),
            "du_last_aum": Decimal("0.00"),
            "pd_trd_ccy": "USD",
            "cu_upt_dt": 20260614,
            "du_upt_dt": 20260616,
        },
    )

    assert record.product_id == "AMX:ZERO"
    assert record.is_quarantined
    assert record.quarantine_reason == "sparse_source_row"
    assert record.total_expense_ratio_quality is QualityStatus.UNKNOWN
    assert record.aum_quality is QualityStatus.UNKNOWN
    assert record.source_values["cu_charge_rt"] == "0.000000"
    assert record.static_as_of.isoformat() == "2026-06-14"
