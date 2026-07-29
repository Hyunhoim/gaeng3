from pathlib import Path

from finance_agent_core.agent import FinanceAgent
from finance_agent_core.agent.providers import (
    DomesticMockProvider,
    domestic_vertical_slice_plan,
)
from finance_agent_core.config import QualityStatus
from finance_agent_core.domain import DatabaseManifest, NormalizedDomesticEtpRecord
from finance_agent_core.execution import ResultVerifier, SQLiteOracle, build_product_evidence
from finance_agent_core.normalization import normalize_domestic_etp_row
from finance_agent_core.storage import connect_read_only, load_all_records, load_manifest


def test_domestic_sqlite_oracle_verifier_and_evidence_agree(
    domestic_sample_database: tuple[
        Path,
        list[NormalizedDomesticEtpRecord],
        DatabaseManifest,
    ],
) -> None:
    path, _, expected_manifest = domestic_sample_database
    plan = domestic_vertical_slice_plan("domestic-oracle-001")
    executed = SQLiteOracle(path).execute(plan)
    with connect_read_only(path) as connection:
        universe = load_all_records(connection)
        manifest = load_manifest(connection)

    verified = ResultVerifier().verify(plan, executed, universe)
    products = build_product_evidence(plan, verified)

    assert manifest == expected_manifest
    assert verified.candidate_count == 6
    assert [record.one_month_return_pct for record in verified.records] == [
        60,
        50,
        40,
        30,
        20,
    ]
    monthly_return = next(
        field for field in products[0].fields if field.canonical_field == "one_month_return_pct"
    )
    assert monthly_return.source_columns == ["du_er_1m"]
    assert monthly_return.source_key == {"pd_itm_no": "KR7000000007"}
    assert monthly_return.quality.value == "VALID"


def test_domestic_mock_agent_completes_verified_vertical_slice(
    domestic_sample_database: tuple[
        Path,
        list[NormalizedDomesticEtpRecord],
        DatabaseManifest,
    ],
) -> None:
    path, _, _ = domestic_sample_database
    response = FinanceAgent(path, DomesticMockProvider()).answer(
        "미국 주식형 국내 ETF 중 연금 거래 가능한 상품을 1개월 수익률 순으로 보여줘",
        "domestic-agent-001",
    )

    assert response.provider == "mock"
    assert response.candidate_count == 6
    assert "1개월 수익률 60%" in response.answer
    assert response.source_manifest.dataset == "domestic_etp"
    assert len(response.warnings) == 2


def test_domestic_corrupt_key_is_quarantined_without_repair_guess() -> None:
    record = normalize_domestic_etp_row(
        source_row=1155,
        present_source_fields=16,
        values={
            "pd_itm_no": "KR",
            "pd_itm_no_ma": "A0193MO",
            "pd_grp_no": "ETF",
            "pd_nm": ".",
            "pd_abrv_nm": "BNK 27-12 특수채",
            "pd_sale_yn": "0",
            "pd_pen_tr_yn": "N",
            "pd_risk_nm": "높은위험(2등급)",
            "wu_inv_ast_type": "주식",
            "wu_inv_rgn": "국내",
            "wu_core_yn": "N",
            "cu_fund_mgmt_co": ".",
        },
    )

    assert record.is_quarantined
    assert record.source_values["pd_itm_no"] == "KR"
    assert record.product_id == "QUARANTINED:PREF01N001:1155"
    assert record.row_quality is QualityStatus.INVALID
    assert record.total_expense_ratio_quality is QualityStatus.UNKNOWN
    assert record.aum_quality is QualityStatus.UNKNOWN
