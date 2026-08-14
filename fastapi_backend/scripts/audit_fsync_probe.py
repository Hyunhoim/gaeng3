from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import secrets
import stat
import sys
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from time import perf_counter
from typing import Any

from finance_agent_core.observability import (
    AppendOnlyJsonlAuditSink,
    AuditEvent,
    AuditOutcome,
    AuditStage,
)

from scripts.deterministic_performance_analysis import numeric_summary


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _secure_output_directory(path: Path) -> Path:
    resolved = path.resolve(strict=True)
    metadata = path.stat(follow_symlinks=False)
    if (
        resolved != path
        or not stat.S_ISDIR(metadata.st_mode)
        or metadata.st_uid != os.geteuid()
        or metadata.st_mode & (stat.S_IRWXG | stat.S_IRWXO)
    ):
        raise ValueError("probe directory must be an owner-only absolute local directory")
    return resolved


def _event(sequence: int) -> AuditEvent:
    return AuditEvent.redacted(
        stage=AuditStage.REQUEST,
        outcome=AuditOutcome.SUCCEEDED,
        reason_code="fsync_probe_completed",
        duration_ms=0,
        request_id="deterministic-fsync-probe",
        question="synthetic timing probe",
        invocation_id="deterministic-fsync-probe",
        event_sequence=sequence,
    )


def _measure_sink(
    path: Path,
    *,
    fsync_each_event: bool,
    warmup_events: int,
    measured_events: int,
) -> tuple[list[float], dict[str, Any]]:
    if path.exists():
        raise FileExistsError(path.name)
    sink = AppendOnlyJsonlAuditSink(path, fsync_each_event=fsync_each_event)
    latencies: list[float] = []
    try:
        for sequence in range(1, warmup_events + measured_events + 1):
            started = perf_counter()
            sink.emit(_event(sequence))
            elapsed_ms = (perf_counter() - started) * 1000
            if sequence > warmup_events:
                latencies.append(elapsed_ms)
    finally:
        sink.close()
    return latencies, {
        "basename": path.name,
        "sha256": _sha256_file(path),
        "size_bytes": path.stat().st_size,
        "event_count": warmup_events + measured_events,
    }


def run_probe(
    *,
    directory: Path,
    warmup_events: int,
    measured_events: int,
    run_id: str | None = None,
) -> dict[str, Any]:
    if not 0 <= warmup_events <= 10_000:
        raise ValueError("warmup event count must be in [0, 10000]")
    if not 1 <= measured_events <= 100_000:
        raise ValueError("measured event count must be in [1, 100000]")
    resolved = _secure_output_directory(directory)
    identifier = run_id or secrets.token_hex(8)
    if re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,63}", identifier) is None:
        raise ValueError("run ID has an invalid safe identifier shape")
    write_path = resolved / f"fsync-probe-{identifier}-write-only.jsonl"
    durable_path = resolved / f"fsync-probe-{identifier}-durable.jsonl"
    write_latencies, write_artifact = _measure_sink(
        write_path,
        fsync_each_event=False,
        warmup_events=warmup_events,
        measured_events=measured_events,
    )
    durable_latencies, durable_artifact = _measure_sink(
        durable_path,
        fsync_each_event=True,
        warmup_events=warmup_events,
        measured_events=measured_events,
    )
    write_summary = numeric_summary(write_latencies)
    durable_summary = numeric_summary(durable_latencies)
    return {
        "schema_version": "1.0",
        "suite_id": "audit-fsync-probe-v1",
        "generated_at_utc": datetime.now(UTC).isoformat(),
        "warmup_events_per_mode": warmup_events,
        "measured_events_per_mode": measured_events,
        "write_only_latency_ms": write_summary,
        "append_and_fsync_latency_ms": durable_summary,
        "estimated_fsync_increment_ms": {
            key: round(float(durable_summary[key]) - float(write_summary[key]), 6)
            for key in ("mean", "p50", "p95", "p99", "max")
        },
        "artifacts": {
            "write_only": write_artifact,
            "durable": durable_artifact,
        },
        "interpretation": (
            "append_and_fsync is the synchronous downstream cost on the same filesystem; "
            "the API uses a bounded async queue, so it is not response latency by itself"
        ),
    }


def _write_new(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(
        path,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(payload, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
    except BaseException:
        try:
            path.unlink()
        except OSError:
            pass
        raise


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Measure append+fsync cost in an isolated owner-only audit directory."
    )
    parser.add_argument("--directory", type=Path, required=True)
    parser.add_argument("--warmup-events", type=int, default=10)
    parser.add_argument("--measured-events", type=int, default=100)
    parser.add_argument("--run-id")
    parser.add_argument("--output", type=Path, required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = build_parser().parse_args(argv)
    try:
        report = run_probe(
            directory=arguments.directory,
            warmup_events=arguments.warmup_events,
            measured_events=arguments.measured_events,
            run_id=arguments.run_id,
        )
        _write_new(arguments.output, report)
    except (OSError, ValueError) as error:
        print(f"audit fsync probe failed: {error}", file=sys.stderr)
        return 2
    print(
        json.dumps(
            {"suite_id": report["suite_id"], "output": str(arguments.output)},
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
