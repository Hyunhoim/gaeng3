from pathlib import Path

import pytest

from finance_agent_core.audit.registry import InputDiscoveryError, discover_workbook


def test_discover_workbook_accepts_filename_without_copy_suffix(tmp_path: Path) -> None:
    workbook = tmp_path / "PRBD01N001_국내채권마스터_20260711_datarows.xlsx"
    workbook.touch()

    assert discover_workbook(tmp_path, "PRBD01N001_*datarows*.xlsx") == workbook


def test_discover_workbook_rejects_ambiguous_matches(tmp_path: Path) -> None:
    (tmp_path / "PRBD01N001_a_datarows.xlsx").touch()
    (tmp_path / "PRBD01N001_b_datarows(1).xlsx").touch()

    with pytest.raises(InputDiscoveryError, match="exactly one workbook"):
        discover_workbook(tmp_path, "PRBD01N001_*datarows*.xlsx")
