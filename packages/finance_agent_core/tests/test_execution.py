from pathlib import Path

import pytest

from finance_agent_core.agent.providers import first_vertical_slice_plan
from finance_agent_core.domain import DatabaseManifest, NormalizedOverseasEtpRecord
from finance_agent_core.execution import (
    ResultVerificationError,
    ResultVerifier,
    SQLiteOracle,
    build_product_evidence,
)
from finance_agent_core.storage import connect_read_only, load_all_records


def test_sql_oracle_and_python_verifier_agree(
    sample_database: tuple[Path, list[NormalizedOverseasEtpRecord], DatabaseManifest],
) -> None:
    path, _, _ = sample_database
    plan = first_vertical_slice_plan("oracle-001")
    executed = SQLiteOracle(path).execute(plan)
    with connect_read_only(path) as connection:
        universe = load_all_records(connection)

    verified = ResultVerifier().verify(plan, executed, universe)

    assert verified.candidate_count == 6
    assert [record.ticker for record in verified.records] == [
        "B6",
        "B5",
        "B4",
        "B2",
        "B3",
    ]
    assert "Z0" not in {record.ticker for record in verified.records}


def test_verifier_rejects_tampered_order(
    sample_database: tuple[Path, list[NormalizedOverseasEtpRecord], DatabaseManifest],
) -> None:
    path, _, _ = sample_database
    plan = first_vertical_slice_plan("oracle-002")
    executed = SQLiteOracle(path).execute(plan)
    tampered = executed.model_copy(update={"records": list(reversed(executed.records))})
    with connect_read_only(path) as connection:
        universe = load_all_records(connection)

    with pytest.raises(ResultVerificationError, match="top results mismatch"):
        ResultVerifier().verify(plan, tampered, universe)


def test_field_evidence_contains_raw_source_and_field_date(
    sample_database: tuple[Path, list[NormalizedOverseasEtpRecord], DatabaseManifest],
) -> None:
    path, _, _ = sample_database
    plan = first_vertical_slice_plan("oracle-003")
    executed = SQLiteOracle(path).execute(plan)
    with connect_read_only(path) as connection:
        universe = load_all_records(connection)
    verified = ResultVerifier().verify(plan, executed, universe)

    products = build_product_evidence(plan, verified)
    aum = next(field for field in products[0].fields if field.canonical_field == "aum")

    assert aum.source_columns == ["du_last_aum"]
    assert aum.raw_values == {"du_last_aum": "6000"}
    assert aum.normalized_value == "6000"
    assert aum.as_of.isoformat() == "2026-06-16"
    assert aum.quality.value == "VALID"
