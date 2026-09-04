from pathlib import Path

from finance_agent_core.domain import DatabaseManifest, NormalizedOverseasEtpRecord
from finance_agent_core.storage import connect_read_only, load_all_records, load_manifest


def test_sqlite_round_trip_preserves_exact_values(
    sample_database: tuple[Path, list[NormalizedOverseasEtpRecord], DatabaseManifest],
) -> None:
    path, original, expected_manifest = sample_database

    with connect_read_only(path) as connection:
        loaded = load_all_records(connection)
        manifest = load_manifest(connection)

    assert manifest == expected_manifest
    assert len(loaded) == len(original)
    original_by_id = {record.product_id: record for record in original}
    loaded_by_id = {record.product_id: record for record in loaded}
    assert (
        loaded_by_id["AMX:B3"].total_expense_ratio_pct
        == original_by_id["AMX:B3"].total_expense_ratio_pct
    )
    assert loaded_by_id["AMX:B6"].aum == original_by_id["AMX:B6"].aum
    assert loaded_by_id["AMX:Z0"].total_expense_ratio_quality.value == "UNKNOWN"
