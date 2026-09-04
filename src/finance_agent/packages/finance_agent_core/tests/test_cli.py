from pathlib import Path

import pytest

from finance_agent_core.audit.cli import _filter_expectations, _validate_paths


def test_filter_expectations_keeps_only_selected_dataset() -> None:
    expectations = {
        "checks": [
            {"path": "summary.dataset_count", "expected": 4},
            {"path": "datasets.bond.structure.data_rows", "expected": 42},
            {"path": "datasets.fund.structure.data_rows", "expected": 95},
        ]
    }

    filtered = _filter_expectations(expectations, {"bond"})

    assert filtered["checks"] == [{"path": "datasets.bond.structure.data_rows", "expected": 42}]


def test_validate_paths_rejects_output_inside_raw_data(tmp_path: Path) -> None:
    data_dir = tmp_path / "raw"
    data_dir.mkdir()

    with pytest.raises(ValueError, match="must not be inside"):
        _validate_paths(data_dir, data_dir / "audit-output")
