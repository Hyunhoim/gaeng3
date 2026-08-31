from pathlib import Path

import pytest

from finance_agent_core.audit.registry import (
    DATASET_BY_NAME,
    InputDiscoveryError,
    discover_workbook,
    resolve_inputs,
)


def test_resolve_inputs_accepts_official_2026_08_24_filenames(tmp_path: Path) -> None:
    """The organizer's 2026-08-24 redistribution uses lowercase `<prefix>_data.xlsx`."""

    data = tmp_path / "prbd01n001_data.xlsx"
    schema = tmp_path / "prbd01n001_schema.xlsx"
    data.touch()
    schema.touch()

    assert resolve_inputs(tmp_path, DATASET_BY_NAME["bond"]) == (data, schema)


@pytest.mark.parametrize("dataset", sorted(DATASET_BY_NAME))
def test_resolve_inputs_accepts_every_official_family(tmp_path: Path, dataset: str) -> None:
    spec = DATASET_BY_NAME[dataset]
    data = tmp_path / f"{spec.prefix.lower()}_data.xlsx"
    schema = tmp_path / f"{spec.prefix.lower()}_schema.xlsx"
    data.touch()
    schema.touch()

    assert resolve_inputs(tmp_path, spec) == (data, schema)


def test_resolve_inputs_rejects_retired_2026_07_11_filenames(tmp_path: Path) -> None:
    """The 2026-07-11 batch was replaced by the organizer and must not load silently."""

    (tmp_path / "PRBD01N001_국내채권마스터_20260711_datarows.xlsx").touch()
    (tmp_path / "PRBD01N001_국내채권마스터_schema.xlsx").touch()

    with pytest.raises(InputDiscoveryError, match="exactly one workbook"):
        resolve_inputs(tmp_path, DATASET_BY_NAME["bond"])


def test_resolve_inputs_rejects_uppercase_official_prefix(tmp_path: Path) -> None:
    """Discovery is case-sensitive, so an uppercase copy must not satisfy the contract."""

    (tmp_path / "PRBD01N001_data.xlsx").touch()
    (tmp_path / "PRBD01N001_schema.xlsx").touch()

    with pytest.raises(InputDiscoveryError, match="exactly one workbook"):
        resolve_inputs(tmp_path, DATASET_BY_NAME["bond"])


def test_discover_workbook_rejects_ambiguous_matches(tmp_path: Path) -> None:
    (tmp_path / "prbd01n001_a_data.xlsx").touch()
    (tmp_path / "prbd01n001_b_data.xlsx").touch()

    with pytest.raises(InputDiscoveryError, match="exactly one workbook"):
        discover_workbook(tmp_path, "prbd01n001_*data*.xlsx")


def test_discover_workbook_ignores_office_lock_files(tmp_path: Path) -> None:
    workbook = tmp_path / "prbd01n001_data.xlsx"
    workbook.touch()
    (tmp_path / "~$prbd01n001_data.xlsx").touch()

    assert discover_workbook(tmp_path, "prbd01n001_data.xlsx") == workbook
