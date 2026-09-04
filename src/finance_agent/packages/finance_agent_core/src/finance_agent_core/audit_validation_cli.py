from __future__ import annotations

import argparse
import hashlib
import json
import os
import stat
import sys
from collections.abc import Sequence
from pathlib import Path

from finance_agent_core.audit_validation import (
    AuditValidationInputError,
    AuditValidationPolicy,
    AuditValidationStatus,
    audit_validation_commitment,
    audit_validation_commitment_bytes,
    audit_validation_report_bytes,
    load_expected_audit_release_linkage,
    validate_audit_jsonl,
)


class AuditValidationOutputError(RuntimeError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="finance-validate-audit",
        description=(
            "Validate redacted Finance Agent Audit JSONL and bind a deterministic report "
            "to its source bytes."
        ),
    )
    parser.add_argument("--audit", required=True, type=Path)
    parser.add_argument("--report", required=True, type=Path)
    parser.add_argument("--commitment", required=True, type=Path)
    parser.add_argument("--release-manifest", type=Path)
    parser.add_argument("--deployment-binding", type=Path)
    parser.add_argument("--expected-binding-sha256")
    parser.add_argument(
        "--require-release-linkage",
        action="store_true",
        help="Require one consistent four-hash release tuple on every valid event.",
    )
    parser.add_argument(
        "--require-dataset-linkage",
        action="store_true",
        help=(
            "Require approved dataset and DB fingerprints on successful execution stages; "
            "trusted release manifest, deployment binding, and binding SHA-256 are mandatory."
        ),
    )
    return parser


def _emit_status(stream: object, payload: dict[str, object]) -> None:
    rendered = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    print(rendered, file=stream)


def _validate_output_target(path: Path, *, label: str) -> None:
    if not path.is_absolute():
        raise AuditValidationOutputError(f"{label}_path_not_absolute")
    try:
        parent = path.parent.resolve(strict=True)
        parent_stat = path.parent.stat(follow_symlinks=False)
    except OSError:
        raise AuditValidationOutputError(f"{label}_parent_unavailable") from None
    if parent != path.parent or not stat.S_ISDIR(parent_stat.st_mode):
        raise AuditValidationOutputError(f"{label}_parent_unsafe")
    try:
        if path.exists() or path.is_symlink():
            raise AuditValidationOutputError(f"{label}_already_exists")
    except OSError:
        raise AuditValidationOutputError(f"{label}_target_unavailable") from None


def _write_new_file(path: Path, data: bytes, *, label: str) -> None:
    _validate_output_target(path, label=label)
    flags = (
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    try:
        descriptor = os.open(path, flags, 0o600)
    except OSError:
        raise AuditValidationOutputError(f"{label}_create_failed") from None
    try:
        remaining = memoryview(data)
        while remaining:
            try:
                written = os.write(descriptor, remaining)
            except InterruptedError:
                continue
            if written <= 0:
                raise AuditValidationOutputError(f"{label}_write_failed")
            remaining = remaining[written:]
        os.fsync(descriptor)
    except AuditValidationOutputError:
        try:
            os.close(descriptor)
        finally:
            try:
                path.unlink()
            except OSError:
                pass
        raise
    except OSError:
        try:
            os.close(descriptor)
        finally:
            try:
                path.unlink()
            except OSError:
                pass
        raise AuditValidationOutputError(f"{label}_write_failed") from None
    else:
        os.close(descriptor)
    try:
        parent_descriptor = os.open(
            path.parent,
            os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_CLOEXEC", 0),
        )
        try:
            os.fsync(parent_descriptor)
        finally:
            os.close(parent_descriptor)
    except OSError:
        raise AuditValidationOutputError(f"{label}_directory_fsync_failed") from None


def _release_arguments(args: argparse.Namespace) -> bool:
    values = (
        args.release_manifest,
        args.deployment_binding,
        args.expected_binding_sha256,
    )
    if not any(value is not None for value in values):
        return False
    if not all(value is not None for value in values):
        raise AuditValidationInputError("release_linkage_arguments_incomplete")
    return True


def run(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        _validate_output_target(args.report, label="report")
        _validate_output_target(args.commitment, label="commitment")
        if args.report == args.commitment:
            raise AuditValidationOutputError("output_targets_must_differ")
        expected_release = None
        if _release_arguments(args):
            expected_release = load_expected_audit_release_linkage(
                manifest_path=args.release_manifest,
                binding_path=args.deployment_binding,
                expected_binding_sha256=args.expected_binding_sha256,
            )
        if args.require_dataset_linkage and expected_release is None:
            raise AuditValidationInputError("dataset_linkage_requires_trusted_release")
        report = validate_audit_jsonl(
            args.audit,
            expected_release=expected_release,
            policy=AuditValidationPolicy(
                require_request_lifecycle=True,
                require_execution_path=True,
                require_release_linkage=(
                    args.require_release_linkage or expected_release is not None
                ),
                require_dataset_linkage=(
                    args.require_dataset_linkage or expected_release is not None
                ),
            ),
        )
        report_bytes = audit_validation_report_bytes(report)
        commitment = audit_validation_commitment(report)
        commitment_bytes = audit_validation_commitment_bytes(commitment)
        _write_new_file(args.report, report_bytes, label="report")
        _write_new_file(args.commitment, commitment_bytes, label="commitment")
    except (AuditValidationInputError, AuditValidationOutputError) as error:
        _emit_status(
            sys.stderr,
            {"schema_version": "1.0", "status": "error", "code": error.code},
        )
        return 2
    _emit_status(
        sys.stdout,
        {
            "schema_version": "1.0",
            "status": report.status.value,
            "audit_file_sha256": report.audit_file_sha256,
            "report_file_sha256": commitment.report_file_sha256,
            "commitment_file_sha256": hashlib.sha256(commitment_bytes).hexdigest(),
            "issue_count": report.issue_count,
        },
    )
    return 0 if report.status is AuditValidationStatus.PASSED else 1


def main() -> None:
    raise SystemExit(run())


if __name__ == "__main__":
    main()


__all__ = ["main", "run"]
