"""CLI for verifying and running the independent sealed safety-blind suite."""

from __future__ import annotations

import argparse
import json
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from finance_agent_core.evaluation.safety_blind import (
    APPROVED_FAMILIES,
    ApprovedUniverseIndex,
    SafetyBlindBundle,
    sha256_file,
)
from finance_agent_core.evaluation.safety_blind_adapter import DATABASES_ENV
from finance_agent_core.evaluation.safety_blind_runner import (
    IsolatedSafetyBlindRunner,
    build_report,
    write_report_once,
)

DEFAULT_ADAPTER = "finance_agent_core.evaluation.safety_blind_adapter:current_core_adapter"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Verify or run the locally sealed finance safety-blind diagnostic."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    verify = subparsers.add_parser("verify")
    verify.add_argument("--bundle-dir", type=Path, required=True)

    run = subparsers.add_parser("run")
    run.add_argument("--bundle-dir", type=Path, required=True)
    run.add_argument("--key-file", type=Path, required=True)
    run.add_argument("--approved-manifest", type=Path, required=True)
    for family in sorted(APPROVED_FAMILIES):
        run.add_argument(f"--db-{family.replace('_', '-')}", type=Path, required=True)
    run.add_argument("--adapter", default=DEFAULT_ADAPTER)
    run.add_argument("--workers", type=int, default=4)
    run.add_argument("--case-timeout", type=float, default=10)
    run.add_argument("--output", type=Path, required=True)
    run.add_argument("--first-run-state", type=Path, required=True)
    return parser


def _database_paths(arguments: argparse.Namespace) -> dict[str, Path]:
    return {family: getattr(arguments, f"db_{family}") for family in sorted(APPROVED_FAMILIES)}


def _now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _write_json_once(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8") as stream:
        json.dump(payload, stream, ensure_ascii=False, indent=2, sort_keys=True)
        stream.write("\n")


def _claim_first_run(
    path: Path,
    *,
    bundle: SafetyBlindBundle,
    adapter: str,
    output: Path,
) -> None:
    _write_json_once(
        path,
        {
            "schema_version": "1.0",
            "status": "started",
            "suite_id": bundle.manifest.suite_id,
            "questions_sha256": bundle.manifest.files["questions.jsonl"],
            "sealed_expectations_sha256": bundle.manifest.files["expectations.sealed.jsonl"],
            "approved_release_id": bundle.universe.release_id,
            "adapter": adapter,
            "output": str(output),
            "started_at_utc": _now(),
            "is_baseline": False,
        },
    )


def _finish_first_run(
    path: Path,
    *,
    status: str,
    output: Path | None = None,
    error_type: str | None = None,
) -> None:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("status") != "started":
        raise RuntimeError("first-run state is not in started state")
    payload["status"] = status
    payload["completed_at_utc"] = _now()
    if output is not None:
        payload["report_sha256"] = sha256_file(output)
        payload["diagnostic_report"] = str(output)
    if error_type is not None:
        payload["error_type"] = error_type
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _reject_baseline_output(path: Path) -> None:
    lowered = {part.casefold() for part in path.parts}
    if "baselines" in lowered:
        raise ValueError("safety-blind first-run output cannot be written as a baseline")


def _public_summary(bundle: SafetyBlindBundle) -> dict[str, Any]:
    return {
        "suite_id": bundle.manifest.suite_id,
        "case_count": len(bundle.cases),
        "family_quotas": bundle.manifest.family_quotas,
        "disposition_quotas_committed": bundle.manifest.disposition_quotas,
        "approved_release_id": bundle.universe.release_id,
        "questions_sha256": bundle.manifest.files["questions.jsonl"],
        "sealed_expectations_sha256": bundle.manifest.files["expectations.sealed.jsonl"],
        "authorship": bundle.manifest.authorship,
    }


def main(argv: list[str] | None = None) -> int:
    arguments = build_parser().parse_args(argv)
    bundle = SafetyBlindBundle.load(arguments.bundle_dir)
    if arguments.command == "verify":
        print(json.dumps(_public_summary(bundle), ensure_ascii=False, indent=2))
        return 0

    _reject_baseline_output(arguments.output)
    if arguments.output.exists():
        raise FileExistsError(f"diagnostic output already exists: {arguments.output}")
    if arguments.first_run_state.exists():
        raise FileExistsError(f"first-run state already exists: {arguments.first_run_state}")
    database_paths = _database_paths(arguments)
    universe_index = ApprovedUniverseIndex.load(
        bundle.universe,
        approved_manifest_path=arguments.approved_manifest,
        database_paths=database_paths,
    )
    unlocked = bundle.unlock(arguments.key_file)
    os.environ[DATABASES_ENV] = json.dumps(
        {family: str(path.resolve()) for family, path in database_paths.items()},
        sort_keys=True,
    )
    _claim_first_run(
        arguments.first_run_state,
        bundle=bundle,
        adapter=arguments.adapter,
        output=arguments.output,
    )
    try:
        runner = IsolatedSafetyBlindRunner(
            arguments.adapter,
            workers=arguments.workers,
            case_timeout_seconds=arguments.case_timeout,
        )
        envelopes = runner.run(unlocked)
        report = build_report(
            unlocked,
            envelopes,
            universe_index,
            workers=arguments.workers,
            case_timeout_seconds=arguments.case_timeout,
        )
        write_report_once(report, arguments.output)
    except Exception as exc:
        _finish_first_run(
            arguments.first_run_state,
            status="failed_before_report",
            error_type=type(exc).__name__,
        )
        raise
    _finish_first_run(
        arguments.first_run_state,
        status="completed",
        output=arguments.output,
    )
    print(
        json.dumps(
            {
                "diagnostic_status": report.diagnostic_status,
                "summary": report.summary.model_dump(mode="json"),
                "report": str(arguments.output),
                "first_run_state": str(arguments.first_run_state),
                "is_baseline": False,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0 if report.diagnostic_status == "passed" else 2


if __name__ == "__main__":
    raise SystemExit(main())
