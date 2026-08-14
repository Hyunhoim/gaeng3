from __future__ import annotations

import json
import tempfile
from pathlib import Path

import pytest

from finance_agent_core.evaluation.schema_embedding_external_v2 import (
    ExternalBlindQuestionOnlySet,
)
from finance_agent_core.evaluation.schema_embedding_external_v2_cli import main
from finance_agent_core.evaluation.schema_embedding_external_v2_rehearsal import (
    SyntheticRehearsalIntegrityError,
    run_synthetic_external_blind_v2_rehearsal,
    verify_synthetic_external_blind_v2_rehearsal,
)

_IMPLEMENTATION_COMMIT = "1234567890abcdef1234567890abcdef12345678"


def test_synthetic_rehearsal_is_explicitly_not_blind_and_uses_caller_path(
    tmp_path: Path,
) -> None:
    output_dir = tmp_path / "caller-selected-rehearsal"

    report = run_synthetic_external_blind_v2_rehearsal(
        output_dir=output_dir,
        implementation_commit=_IMPLEMENTATION_COMMIT,
    )

    assert report.status == "internal_synthetic_not_blind"
    assert report.never_model_selection_evidence is True
    assert report.external_independence_present is False
    assert report.real_model_inference_performed is False
    assert report.case_count == 100
    assert report.mechanics.control_operational_dense_call_count == 0
    assert all(value == 1 for value in report.mechanics.field_recall_at_5.values())
    assert all(
        item.filename.startswith("internal-synthetic-not-blind-") for item in report.artifacts
    )
    assert verify_synthetic_external_blind_v2_rehearsal(output_dir=output_dir) == report

    report_payload = json.loads(
        (output_dir / "internal-synthetic-not-blind-rehearsal-report.json").read_text(
            encoding="utf-8"
        )
    )
    assert report_payload["status"] == "internal_synthetic_not_blind"
    assert report_payload["never_model_selection_evidence"] is True
    with pytest.raises(ValueError):
        ExternalBlindQuestionOnlySet.model_validate_json(
            (output_dir / "internal-synthetic-not-blind-questions.json").read_bytes()
        )


def test_synthetic_rehearsal_refuses_output_collision_without_overwrite(
    tmp_path: Path,
) -> None:
    output_dir = tmp_path / "non-empty"
    output_dir.mkdir()
    sentinel = output_dir / "keep.txt"
    sentinel.write_text("owned-by-caller", encoding="utf-8")

    with pytest.raises(FileExistsError, match="not empty"):
        run_synthetic_external_blind_v2_rehearsal(
            output_dir=output_dir,
            implementation_commit=_IMPLEMENTATION_COMMIT,
        )

    assert sentinel.read_text(encoding="utf-8") == "owned-by-caller"


def test_synthetic_rehearsal_has_no_repository_relative_output_default() -> None:
    output_dir = Path(Path(tempfile.gettempdir()).anchor) / (
        "must-not-create-synthetic-blind-artifacts-here"
    )

    with pytest.raises(ValueError, match="temp subdirectory"):
        run_synthetic_external_blind_v2_rehearsal(
            output_dir=output_dir,
            implementation_commit=_IMPLEMENTATION_COMMIT,
        )

    assert not output_dir.exists()


def test_synthetic_rehearsal_integrity_check_detects_tamper(tmp_path: Path) -> None:
    output_dir = tmp_path / "tamper"
    report = run_synthetic_external_blind_v2_rehearsal(
        output_dir=output_dir,
        implementation_commit=_IMPLEMENTATION_COMMIT,
    )
    target = output_dir / report.artifacts[0].filename
    target.write_bytes(target.read_bytes() + b"tampered")

    with pytest.raises(SyntheticRehearsalIntegrityError, match="hash differs"):
        verify_synthetic_external_blind_v2_rehearsal(output_dir=output_dir)


def test_synthetic_rehearsal_cli_prints_non_blind_marker(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    output_dir = tmp_path / "cli"

    exit_code = main(
        [
            "rehearse",
            "--output-dir",
            str(output_dir),
            "--implementation-commit",
            _IMPLEMENTATION_COMMIT,
        ]
    )

    assert exit_code == 0
    stdout = json.loads(capsys.readouterr().out)
    assert stdout["status"] == "internal_synthetic_not_blind"
    assert stdout["never_model_selection_evidence"] is True
    assert stdout["output_dir"] == str(output_dir)
