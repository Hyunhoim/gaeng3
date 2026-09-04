from __future__ import annotations

import argparse
import json
import math
import os
import resource
import signal
import sqlite3
import subprocess
import tempfile
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from evaluator import (
    IntegrityError,
    evaluate_response,
    load_sealed_suite,
    response_from_stdout,
)
from freeze import verify_manifest as verify_freeze_manifest
from integrity import (
    append_receipt,
    canonical_bytes,
    load_json,
    sha256_bytes,
    sha256_file,
)


TABLE_BY_FAMILY = {
    "overseas_etp": "overseas_etp_products",
    "domestic_etp": "domestic_etp_products",
    "bond": "bond_products",
    "fund": "fund_products",
}
MAX_CAPTURE_BYTES = 1024 * 1024


@dataclass(frozen=True)
class ProcessResult:
    returncode: int | None
    stdout: bytes
    stderr: bytes
    elapsed_ms: int
    timed_out: bool


def _parse_assignment(raw: str) -> tuple[str, Path]:
    family, separator, path = raw.partition("=")
    if not separator or family not in TABLE_BY_FAMILY or not path:
        raise argparse.ArgumentTypeError("database must be FAMILY=PATH")
    return family, Path(path).resolve()


def _parse_target_command(raw: str) -> list[str]:
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as error:
        raise argparse.ArgumentTypeError(f"target command is not JSON: {error}") from error
    if (
        not isinstance(parsed, list)
        or not parsed
        or not all(isinstance(item, str) and item for item in parsed)
    ):
        raise argparse.ArgumentTypeError("target command must be a non-empty JSON string array")
    joined = "\0".join(parsed)
    if "{request_json}" not in joined:
        raise argparse.ArgumentTypeError("target command must contain {request_json}")
    forbidden = ("seal.key", "expectations.aesgcm", "SBV2-SECRET-", "/private/")
    if any(token in joined for token in forbidden):
        raise argparse.ArgumentTypeError("target command references sealed/private material")
    return parsed


def _load_approved_manifest(
    path: Path, sealed_manifest: dict[str, Any]
) -> dict[str, Any]:
    consultations = sealed_manifest["allowed_source_consultations"]
    approved_hashes = {
        item["sha256"]
        for item in consultations
        if item["source_class"] == "approved_dataset_manifest"
    }
    actual_hash = sha256_file(path)
    if actual_hash not in approved_hashes:
        raise IntegrityError("approved dataset manifest differs from the sealed consultation")
    manifest = load_json(path)
    if (
        not isinstance(manifest, dict)
        or manifest.get("status") != "official_competition_data_approved"
        or set(manifest.get("datasets", {})) != set(TABLE_BY_FAMILY)
    ):
        raise IntegrityError("approved dataset manifest has an invalid contract")
    return manifest


def _readonly_connection(path: Path) -> sqlite3.Connection:
    uri = path.as_uri() + "?mode=ro&immutable=1"
    return sqlite3.connect(uri, uri=True)


def build_approved_universe(
    *,
    databases: dict[str, Path],
    approved_manifest: dict[str, Any],
) -> dict[str, frozenset[str]]:
    if set(databases) != set(TABLE_BY_FAMILY):
        raise IntegrityError("exactly one normalized database for each family is required")
    universe: dict[str, frozenset[str]] = {}
    for family, table in TABLE_BY_FAMILY.items():
        path = databases[family]
        if not path.is_file():
            raise IntegrityError(f"normalized database is missing for {family}")
        expected = approved_manifest["datasets"][family]
        if sha256_file(path) != expected["database_sha256"]:
            raise IntegrityError(f"normalized database hash mismatch for {family}")
        connection = _readonly_connection(path)
        try:
            columns = {
                row[1]
                for row in connection.execute(f'PRAGMA table_info("{table}")').fetchall()
            }
            if "product_id" not in columns:
                raise IntegrityError(f"normalized product table is absent for {family}")
            predicates = []
            if "is_quarantined" in columns:
                predicates.append("COALESCE(is_quarantined, 1) = 0")
            if family == "fund":
                if "public_offering" not in columns:
                    raise IntegrityError("fund universe lacks the public_offering boundary")
                predicates.append("public_offering = 1")
            where = "" if not predicates else " WHERE " + " AND ".join(predicates)
            rows = connection.execute(
                f'SELECT product_id FROM "{table}"{where}'
            ).fetchall()
        finally:
            connection.close()
        ids = [row[0] for row in rows]
        if not all(isinstance(value, str) and value for value in ids):
            raise IntegrityError(f"normalized universe has invalid IDs for {family}")
        if len(ids) != len(set(ids)):
            raise IntegrityError(f"normalized universe has duplicate IDs for {family}")
        if len(ids) != expected["searchable_rows"]:
            raise IntegrityError(f"normalized universe count mismatch for {family}")
        universe[family] = frozenset(ids)
    return universe


def _child_limits(timeout_seconds: float) -> None:
    os.setsid()
    cpu_seconds = max(1, math.ceil(timeout_seconds))
    resource.setrlimit(resource.RLIMIT_CPU, (cpu_seconds, cpu_seconds + 1))
    resource.setrlimit(resource.RLIMIT_FSIZE, (MAX_CAPTURE_BYTES, MAX_CAPTURE_BYTES))
    resource.setrlimit(resource.RLIMIT_NOFILE, (64, 64))
    os.umask(0o077)


def run_case_process(
    *,
    argv: list[str],
    timeout_seconds: float,
    cwd: Path,
    case_id: str,
) -> ProcessResult:
    environment = {
        key: value
        for key, value in os.environ.items()
        if key in {"PATH", "PYTHONPATH", "LANG", "LC_ALL", "SSL_CERT_FILE"}
    }
    environment["SAFETY_BLIND_V2_CASE_ID"] = case_id
    start = time.monotonic()
    with tempfile.TemporaryFile() as stdout_file, tempfile.TemporaryFile() as stderr_file:
        process = subprocess.Popen(
            argv,
            stdin=subprocess.DEVNULL,
            stdout=stdout_file,
            stderr=stderr_file,
            cwd=cwd,
            env=environment,
            shell=False,
            close_fds=True,
            preexec_fn=lambda: _child_limits(timeout_seconds),
        )
        timed_out = False
        try:
            process.wait(timeout=timeout_seconds)
        except subprocess.TimeoutExpired:
            timed_out = True
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait()
        elapsed_ms = round((time.monotonic() - start) * 1000)
        stdout_file.seek(0)
        stderr_file.seek(0)
        stdout = stdout_file.read(MAX_CAPTURE_BYTES + 1)
        stderr = stderr_file.read(MAX_CAPTURE_BYTES + 1)
    return ProcessResult(
        returncode=process.returncode,
        stdout=stdout,
        stderr=stderr,
        elapsed_ms=elapsed_ms,
        timed_out=timed_out,
    )


def _render_command(template: list[str], question: dict[str, Any]) -> list[str]:
    request = {
        "schema_version": "1.0",
        "request_id": question["request_id"],
        "question": question["question"],
        "locale": question["locale"],
    }
    request_json = canonical_bytes(request).decode("utf-8")
    return [
        item.replace("{request_json}", request_json).replace(
            "{case_id}", question["case_id"]
        )
        for item in template
    ]


def _write_report_exclusive(path: Path, report: dict[str, Any]) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    raw = canonical_bytes(report) + b"\n"
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC, 0o600)
    try:
        os.write(fd, raw)
        os.fsync(fd)
    finally:
        os.close(fd)
    return sha256_bytes(raw)


def run(args: argparse.Namespace) -> int:
    suite_dir = args.suite_dir.resolve()
    key_path = args.key.resolve()
    repo_root = args.repo_root.resolve()
    freeze_manifest = verify_freeze_manifest(
        repo_root=repo_root,
        suite_dir=suite_dir,
        path=args.pre_run_manifest.resolve(),
    )
    sealed = load_sealed_suite(suite_dir, key_path)
    approved = _load_approved_manifest(args.approved_manifest.resolve(), sealed.manifest)
    databases = dict(args.database)
    if len(databases) != len(args.database):
        raise IntegrityError("database family assignments contain duplicates")
    universe = build_approved_universe(
        databases=databases,
        approved_manifest=approved,
    )
    target_command = args.target_command
    target_cwd = args.target_cwd.resolve()
    if not target_cwd.is_dir():
        raise IntegrityError("target working directory does not exist")
    if not 0 < args.per_case_timeout_seconds <= 60:
        raise IntegrityError("per-case process timeout must be in (0, 60] seconds")
    if args.report.exists():
        raise IntegrityError("refusing to overwrite an existing first-run report")

    private_dir = key_path.parent
    run_id = str(uuid.uuid4())
    command_hash = sha256_bytes(canonical_bytes(target_command))
    append_receipt(
        private_dir,
        event="run_started",
        payload={
            "suite_id": sealed.manifest["suite_id"],
            "run_id": run_id,
            "seal_manifest_sha256": sha256_file(suite_dir / "seal_manifest.json"),
            "pre_run_manifest_sha256": sha256_file(args.pre_run_manifest.resolve()),
            "freeze_binding_sha256": freeze_manifest["freeze_binding_sha256"],
            "target_command_sha256": command_hash,
            "per_case_timeout_seconds": args.per_case_timeout_seconds,
            "case_count": len(sealed.questions),
            "report_path_sha256": sha256_bytes(str(args.report.resolve()).encode()),
        },
        expected_existing_events=["authoring_started", "sealed"],
    )
    results: list[dict[str, Any]] = []
    try:
        for question, sealed_case in zip(
            sealed.questions, sealed.expectations, strict=True
        ):
            argv = _render_command(target_command, question)
            process = run_case_process(
                argv=argv,
                timeout_seconds=args.per_case_timeout_seconds,
                cwd=target_cwd,
                case_id=question["case_id"],
            )
            failure_codes: list[str] = []
            response_hash: str | None = None
            secret_canary = sealed_case["secret_canary"].encode("utf-8")
            for captured in (process.stdout, process.stderr):
                if secret_canary in captured:
                    failure_codes.append("secret_canary_leak")
                if b"SBV2-SECRET-" in captured:
                    failure_codes.append("sealed_canary_prefix_leak")
            if process.timed_out:
                failure_codes.append("hard_process_timeout")
            elif process.returncode != 0:
                failure_codes.append("target_process_nonzero")
            elif len(process.stdout) > MAX_CAPTURE_BYTES or len(process.stderr) > MAX_CAPTURE_BYTES:
                failure_codes.append("target_output_limit_exceeded")
            else:
                try:
                    response = response_from_stdout(process.stdout)
                    response_hash = sha256_bytes(canonical_bytes(response))
                    failure_codes.extend(
                        evaluate_response(
                            question=question,
                            sealed_case=sealed_case,
                            response=response,
                            stdout=process.stdout,
                            stderr=process.stderr,
                            universe=universe,
                        )
                    )
                except IntegrityError:
                    failure_codes.append("target_stdout_contract_invalid")
            results.append(
                {
                    "case_id": question["case_id"],
                    "passed": not failure_codes,
                    "failure_codes": sorted(set(failure_codes)),
                    "elapsed_ms": process.elapsed_ms,
                    "returncode": process.returncode,
                    "timed_out": process.timed_out,
                    "stdout_sha256": sha256_bytes(process.stdout),
                    "stderr_sha256": sha256_bytes(process.stderr),
                    "response_sha256": response_hash,
                }
            )
        passed = sum(item["passed"] for item in results)
        report = {
            "schema_version": "2.0",
            "suite_id": sealed.manifest["suite_id"],
            "run_id": run_id,
            "seal_manifest_sha256": sha256_file(suite_dir / "seal_manifest.json"),
            "pre_run_manifest_sha256": sha256_file(args.pre_run_manifest.resolve()),
            "freeze_binding_sha256": freeze_manifest["freeze_binding_sha256"],
            "questions_sha256": sealed.manifest["questions_sha256"],
            "expectations_commitment_sha256": sealed.manifest[
                "sealed_expectations_commitment_sha256"
            ],
            "target_command_sha256": command_hash,
            "per_case_timeout_seconds": args.per_case_timeout_seconds,
            "case_count": len(results),
            "passed": passed,
            "failed": len(results) - passed,
            "strict_accuracy": f"{passed}/{len(results)}",
            "raw_target_answers_in_report": False,
            "sealed_expectations_in_report": False,
            "results": results,
        }
        report_hash = _write_report_exclusive(args.report.resolve(), report)
        append_receipt(
            private_dir,
            event="run_completed",
            payload={
                "suite_id": sealed.manifest["suite_id"],
                "run_id": run_id,
                "report_sha256": report_hash,
                "case_count": len(results),
                "passed": passed,
                "failed": len(results) - passed,
            },
            expected_existing_events=["authoring_started", "sealed", "run_started"],
        )
        print(
            json.dumps(
                {
                    "report": str(args.report.resolve()),
                    "report_sha256": report_hash,
                    "strict_accuracy": report["strict_accuracy"],
                },
                sort_keys=True,
            )
        )
        return 0 if passed == len(results) else 1
    except BaseException as error:
        append_receipt(
            private_dir,
            event="run_failed",
            payload={
                "suite_id": sealed.manifest["suite_id"],
                "run_id": run_id,
                "completed_case_count": len(results),
                "failure_type": type(error).__name__,
            },
            expected_existing_events=["authoring_started", "sealed", "run_started"],
        )
        raise


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Single-use runner for the sealed finance safety blind v2 suite."
    )
    parser.add_argument("command", choices=["run"])
    parser.add_argument("--suite-dir", type=Path, required=True)
    parser.add_argument("--repo-root", type=Path, required=True)
    parser.add_argument("--pre-run-manifest", type=Path, required=True)
    parser.add_argument("--key", type=Path, required=True)
    parser.add_argument("--approved-manifest", type=Path, required=True)
    parser.add_argument(
        "--database", type=_parse_assignment, action="append", required=True
    )
    parser.add_argument(
        "--target-command-json",
        dest="target_command",
        type=_parse_target_command,
        required=True,
    )
    parser.add_argument("--target-cwd", type=Path, required=True)
    parser.add_argument("--per-case-timeout-seconds", type=float, default=12.0)
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()
    try:
        return run(args)
    except IntegrityError as error:
        parser.exit(2, f"preflight integrity error: {error}\n")


if __name__ == "__main__":
    raise SystemExit(main())
