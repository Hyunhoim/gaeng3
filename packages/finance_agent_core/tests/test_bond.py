from datetime import date
from pathlib import Path

from finance_agent_core.agent import FinanceAgent
from finance_agent_core.agent.providers import BondMockProvider, bond_vertical_slice_plan
from finance_agent_core.config import QualityStatus
from finance_agent_core.domain import DatabaseManifest, NormalizedBondRecord
from finance_agent_core.execution import ResultVerifier, SQLiteOracle, build_product_evidence
from finance_agent_core.normalization import normalize_bond_row
from finance_agent_core.storage import connect_read_only, load_all_records, write_bond_database


def make_bond_record(
    *,
    row: int,
    product_id: str,
    buy_yield: str,
    quantity: int | None,
    maturity: int,
    major_class: str = "회사채",
) -> NormalizedBondRecord:
    return normalize_bond_row(
        source_row=row,
        present_source_fields=40,
        values={
            "PD_NO": product_id,
            "PD_EXG_MKT": "장내",
            "PD_NM": f"테스트채권 {product_id}",
            "PD_ABRV_NM": f"테스트 {product_id}",
            "PD_PBCM": "테스트발행사",
            "STD_PD_MCLS_NM": major_class,
            "STD_PD_SCLS_NM": "일반사채",
            "BD_KND": "일반회사채",
            "CURR_CD": "KRW",
            "ISU_BAL_AMT": 1_000_000_000,
            "ISU_DT": 20260101,
            "MAT_DT": maturity,
            "SRFC_IRT": "3.5",
            "PD_RISK_GCD": 4,
            "PD_STD_INFO_UPDATE": 20260224,
            "BUY_YIELD": buy_yield,
            "AFTER_TAX_YIELD": "3.0",
            "BUYABLE_QUANTITY": quantity,
            "REMAINING_DAYS": 9999,
            "DUR": "0.5",
            "CRD_GRD": "AA-",
        },
    )


def make_bond_database(tmp_path: Path) -> tuple[Path, list[NormalizedBondRecord]]:
    records = [
        make_bond_record(
            row=2,
            product_id="KRTEST000001",
            buy_yield="4.5",
            quantity=100,
            maturity=20270101,
        ),
        make_bond_record(
            row=3,
            product_id="KRTEST000002",
            buy_yield="3.5",
            quantity=200,
            maturity=20280101,
        ),
        make_bond_record(
            row=4,
            product_id="KRTEST000003",
            buy_yield="5.5",
            quantity=0,
            maturity=20290101,
        ),
        make_bond_record(
            row=5,
            product_id="KRTEST000004",
            buy_yield="6.5",
            quantity=100,
            maturity=20260101,
        ),
        make_bond_record(
            row=6,
            product_id="KRTEST000005",
            buy_yield="7.5",
            quantity=None,
            maturity=20300101,
        ),
        make_bond_record(
            row=7,
            product_id="KRTEST000006",
            buy_yield="5.0",
            quantity=300,
            maturity=20310101,
            major_class="국공채",
        ),
    ]
    path = tmp_path / "bond.sqlite3"
    manifest = DatabaseManifest(
        dataset="bond",
        registry_schema_version="1.2",
        source_file_name="synthetic_bond.xlsx",
        source_file_sha256="c" * 64,
        source_file_size_bytes=1234,
        source_snapshot_date=date(2026, 7, 11),
        total_rows=len(records),
        searchable_rows=len(records),
        quarantined_rows=0,
    )
    write_bond_database(path, records, manifest)
    return path, records


def test_bond_normalization_recomputes_availability_and_remaining_days() -> None:
    record = make_bond_record(
        row=2,
        product_id="KRTEST000001",
        buy_yield="4.5",
        quantity=100,
        maturity=20270101,
    )
    unknown = make_bond_record(
        row=3,
        product_id="KRTEST000002",
        buy_yield="3.5",
        quantity=None,
        maturity=20280101,
    )

    assert record.currently_buyable is True
    assert record.remaining_days == 174
    assert record.source_values["REMAINING_DAYS"] == 9999
    assert record.field_quality["buy_yield_pct"] is QualityStatus.PARTIAL
    assert unknown.currently_buyable is None
    assert unknown.field_quality["currently_buyable"] is QualityStatus.UNKNOWN


def test_bond_sqlite_oracle_verifier_and_evidence_agree(tmp_path: Path) -> None:
    path, _ = make_bond_database(tmp_path)
    plan = bond_vertical_slice_plan("bond-oracle-001")
    executed = SQLiteOracle(path).execute(plan)
    with connect_read_only(path) as connection:
        universe = load_all_records(connection)
    verified = ResultVerifier().verify(plan, executed, universe)
    products = build_product_evidence(plan, verified)

    assert verified.candidate_count == 3
    assert [record.product_id for record in verified.records] == [
        "KRTEST000006",
        "KRTEST000001",
        "KRTEST000002",
    ]
    buy_yield = next(
        field for field in products[0].fields if field.canonical_field == "buy_yield_pct"
    )
    assert buy_yield.source_columns == ["BUY_YIELD"]
    assert buy_yield.as_of.isoformat() == "2026-02-24"
    assert buy_yield.quality is QualityStatus.PARTIAL


def test_bond_mock_agent_completes_verified_vertical_slice(tmp_path: Path) -> None:
    path, _ = make_bond_database(tmp_path)
    response = FinanceAgent(path, BondMockProvider()).answer(
        "매수 가능한 국내채권을 매수수익률 높은 순으로 보여줘",
        "bond-agent-001",
    )

    assert response.candidate_count == 3
    assert response.products[0].ticker == "KRTEST000006"
    assert "매수수익률 5%" in response.answer
    assert response.source_manifest.dataset == "bond"
    assert len(response.warnings) == 2
