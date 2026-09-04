from __future__ import annotations

from pathlib import Path

from scripts.audit_fsync_probe import run_probe


def test_fsync_probe_keeps_separate_write_and_durable_artifacts(tmp_path: Path) -> None:
    directory = tmp_path / "audit"
    directory.mkdir(mode=0o700)

    report = run_probe(
        directory=directory,
        warmup_events=1,
        measured_events=3,
        run_id="unit",
    )

    assert report["suite_id"] == "audit-fsync-probe-v1"
    assert report["write_only_latency_ms"]["sample_count"] == 3
    assert report["append_and_fsync_latency_ms"]["sample_count"] == 3
    assert report["artifacts"]["write_only"]["event_count"] == 4
    assert report["artifacts"]["durable"]["event_count"] == 4
    assert (directory / report["artifacts"]["write_only"]["basename"]).is_file()
    assert (directory / report["artifacts"]["durable"]["basename"]).is_file()
