from datetime import date
from pathlib import Path

from finance_agent_core.agent import FinanceAgent, RoutedFinanceAgent
from finance_agent_core.agent.providers import BondMockProvider, bond_vertical_slice_plan
from finance_agent_core.config import QualityStatus
from finance_agent_core.domain import DatabaseManifest, NormalizedBondRecord
from finance_agent_core.execution import (
    ResultVerifier,
    SQLiteOracle,
    authorize_internal_evaluation_plan,
    build_product_evidence,
)
from finance_agent_core.execution.verifier_projection import (
    load_projected_verifier_records,
    verifier_projection_fields,
)
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
    credit_rating: str = "AA-",
) -> NormalizedBondRecord:
    return normalize_bond_row(
        source_row=row,
        present_source_fields=40,
        values={
            "pd_no": product_id,
            "pd_exg_mkt": "장내",
            "info_base_dt": 20260824,
            "info_seq": row - 1,
            "pd_nm": f"테스트채권 {product_id}",
            "pd_abrv_nm": f"테스트 {product_id}",
            "pd_pbcm": "테스트발행사",
            "std_pd_mcls_nm": major_class,
            "std_pd_scls_nm": "일반사채",
            "bd_knd": "일반회사채",
            "curr_cd": "KRW",
            "isu_bal_amt": 1_000_000_000,
            "isu_dt": 20260101,
            "mat_dt": maturity,
            "srfc_irt": "3.5",
            "pd_risk_gcd": 4,
            "pd_std_info_update": 20260224,
            "buy_yield": buy_yield,
            "after_tax_yield": "3.0",
            "buyable_quantity": quantity,
            "remaining_days": 9999,
            "dur": "0.5",
            "crd_grd": credit_rating,
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
            credit_rating="A+",
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
            credit_rating="AAA",
        ),
    ]
    path = tmp_path / "bond.sqlite3"
    manifest = DatabaseManifest(
        dataset="bond",
        registry_schema_version="1.4",
        source_file_name="synthetic_bond.xlsx",
        source_file_sha256="c" * 64,
        source_file_size_bytes=1234,
        source_snapshot_date=date(2026, 8, 24),
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
    assert record.remaining_days == 130
    assert record.source_values["remaining_days"] == 9999
    assert record.field_quality["buy_yield_pct"] is QualityStatus.VALID
    assert unknown.currently_buyable is True
    assert unknown.field_quality["currently_buyable"] is QualityStatus.VALID


def test_bond_sqlite_oracle_verifier_and_evidence_agree(tmp_path: Path) -> None:
    path, _ = make_bond_database(tmp_path)
    plan = bond_vertical_slice_plan("bond-oracle-001")
    executed = SQLiteOracle(path).execute(authorize_internal_evaluation_plan(plan, path))
    with connect_read_only(path) as connection:
        universe = load_all_records(connection)
    verified = ResultVerifier().verify(plan, executed, universe)
    products = build_product_evidence(plan, verified)

    # The default search scope excludes a bond whose maturity is on or before
    # its own dynamic-as-of date; the organizer's buyability override does not
    # make an already matured instrument active again.
    assert verified.candidate_count == 5
    assert [record.product_id for record in verified.records] == [
        "KRTEST000005:장내:20260824:5",
        "KRTEST000003:장내:20260824:3",
        "KRTEST000006:장내:20260824:6",
        "KRTEST000001:장내:20260824:1",
        "KRTEST000002:장내:20260824:2",
    ]
    buy_yield = next(
        field for field in products[0].fields if field.canonical_field == "buy_yield_pct"
    )
    assert buy_yield.source_columns == ["buy_yield"]
    assert buy_yield.as_of.isoformat() == "2026-02-24"
    assert buy_yield.quality is QualityStatus.VALID


def test_bond_search_uses_first_source_row_per_official_product_id(tmp_path: Path) -> None:
    _, records = make_bond_database(tmp_path)
    records.append(
        make_bond_record(
            row=8,
            product_id="KRTEST000001",
            buy_yield="99.0",
            quantity=999,
            maturity=20350101,
        )
    )
    path = tmp_path / "bond-duplicate-lot.sqlite3"
    manifest = DatabaseManifest(
        dataset="bond",
        registry_schema_version="1.4",
        source_file_name="synthetic_bond.xlsx",
        source_file_sha256="d" * 64,
        source_file_size_bytes=1234,
        source_snapshot_date=date(2026, 8, 24),
        total_rows=len(records),
        searchable_rows=len(records),
        quarantined_rows=0,
    )
    write_bond_database(path, records, manifest)
    plan = bond_vertical_slice_plan("bond-logical-product")
    executed = SQLiteOracle(path).execute(authorize_internal_evaluation_plan(plan, path))
    with connect_read_only(path) as connection:
        universe = load_all_records(connection)
    verified = ResultVerifier().verify(plan, executed, universe)
    products = build_product_evidence(plan, verified)

    assert verified.candidate_count == 5
    assert all(record.source_row != 8 for record in verified.records)
    assert len({product.product_id for product in products}) == len(products)
    assert all(":" not in product.product_id for product in products)


def test_bond_mock_agent_completes_verified_vertical_slice(tmp_path: Path) -> None:
    path, _ = make_bond_database(tmp_path)
    response = FinanceAgent(
        path,
        BondMockProvider(),
        allow_unapproved_database=True,
    ).answer(
        "매수 가능한 국내채권을 매수수익률 높은 순으로 보여줘",
        "bond-agent-001",
    )

    assert response.candidate_count == 5
    assert response.products[0].ticker == "KRTEST000005"
    assert "매수수익률 7.5%" in response.answer
    assert response.source_manifest.dataset == "bond"
    assert len(response.warnings) == 1


def test_bond_briefing_query_executes_with_sellability_and_rating_threshold(
    tmp_path: Path,
) -> None:
    path, _ = make_bond_database(tmp_path)
    result = RoutedFinanceAgent({"bond": path}).answer(
        "현재 판매 가능한 원화채권 중 AA- 이상 종목 알려줘",
        "briefing-bond-001",
    )

    assert result.status == "executed"
    assert result.candidate_count == 4
    assert {product.ticker for product in result.products} == {
        "KRTEST000001",
        "KRTEST000003",
        "KRTEST000005",
        "KRTEST000006",
    }
    assert result.query_plan is not None
    constraints = {
        item.field: item.model_dump(mode="json") for item in result.query_plan.constraints
    }
    assert constraints == {
        "trading_currency": {
            "field": "trading_currency",
            "operator": "eq",
            "value": "KRW",
            "unit": "code",
            "strength": "locked",
        },
        "credit_rating": {
            "field": "credit_rating",
            "operator": "in",
            "value": ["AAA", "AA+", "AA0", "AA-"],
            "unit": "code",
            "strength": "locked",
        },
    }
    first_fields = {
        field.canonical_field: field.normalized_value for field in result.products[0].fields
    }
    assert first_fields["credit_rating"] == "AA-"


def test_bond_aggregate_uses_only_currently_buyable_yields(tmp_path: Path) -> None:
    path, _ = make_bond_database(tmp_path)
    result = RoutedFinanceAgent({"bond": path}).answer(
        "현재 매수 가능한 국내채권의 매수수익률 평균과 최댓값을 집계해줘",
        "aggregate-bond-001",
    )

    assert result.status == "executed"
    # KRTEST000004 matured on 2026-01-01, 235 days before the snapshot, so a
    # search never returns it. The aggregate used to count it anyway and
    # reported an average over six bonds that only five could be found for.
    assert result.candidate_count == 5
    assert [(item.function.value, item.value) for item in result.aggregates] == [
        ("avg", "5.2"),
        ("max", "7.5"),
    ]
    assert all(item.as_of_start.isoformat() == "2026-02-24" for item in result.aggregates)
    assert "기준일 2026-02-24" in result.answer


def test_bond_verifier_projection_matches_normalized_records(tmp_path: Path) -> None:
    path, records = make_bond_database(tmp_path)
    agent = RoutedFinanceAgent({"bond": path})
    decision = agent.router.route(
        "현재 매수 가능한 국내채권의 매수수익률 평균과 최댓값을 집계해줘",
        "projection-bond-001",
    )
    plan = agent.compiler.compile(decision)
    projected = load_projected_verifier_records(
        path,
        authorize_internal_evaluation_plan(plan, path),
    )
    expected = {record.product_id: record for record in records}

    assert [record.product_id for record in projected] == sorted(expected)
    for record in projected:
        original = expected[record.product_id]
        for field_name in verifier_projection_fields(plan):
            expected_value = (
                original.ticker
                if field_name == "product_id"
                else original.canonical_value(field_name)
            )
            assert record.canonical_value(field_name) == expected_value
            assert (
                record.row_level_quality(field_name)[0] == original.row_level_quality(field_name)[0]
            )


def test_bond_amount_aggregate_requires_currency_scope(tmp_path: Path) -> None:
    path, _ = make_bond_database(tmp_path)
    agent = RoutedFinanceAgent({"bond": path})

    blocked = agent.answer(
        "국내채권의 발행잔액 합계를 집계해줘",
        "aggregate-bond-002",
    )
    executed = agent.answer(
        "원화 국내채권의 발행잔액 합계를 집계해줘",
        "aggregate-bond-003",
    )

    assert blocked.status == "clarify"
    assert "trading_currency" in blocked.answer
    assert executed.status == "executed"
    # Five live bonds at 1,000,000,000 each. The matured KRTEST000004 is out of
    # scope for search and therefore out of scope for the sum as well.
    assert executed.aggregates[0].value == "5000000000"
    assert "KRW" in executed.answer


def test_bond_comparison_runs_through_common_engine(tmp_path: Path) -> None:
    path, _ = make_bond_database(tmp_path)
    result = RoutedFinanceAgent({"bond": path}).answer(
        "국내채권 KRTEST000002와 KRTEST000001의 매수수익률과 듀레이션, 신용등급을 비교해줘",
        "compare-bond-001",
    )

    assert result.status == "executed"
    assert [product.ticker for product in result.products] == [
        "KRTEST000002",
        "KRTEST000001",
    ]
    assert [item.canonical_field for item in result.comparisons] == [
        "buy_yield_pct",
        "duration_years",
        "credit_rating",
    ]
    assert [item.status for item in result.comparisons] == [
        "stale_input",
        "stale_input",
        "value_only",
    ]
    assert [item.delta for item in result.comparisons] == ["1.0", "0.0", None]
    assert "기준일 2026-02-24" in result.answer
