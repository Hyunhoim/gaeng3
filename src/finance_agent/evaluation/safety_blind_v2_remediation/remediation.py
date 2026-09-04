from __future__ import annotations

import argparse
import base64
import contextlib
import fcntl
import hashlib
import hmac
import importlib
import json
import os
import shutil
import stat
import subprocess
import sys
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

# The v2 files are intentionally a standalone, frozen evaluator rather than an
# installed package.  Import its scoring and process-isolation primitives from
# the frozen source directory, and reject an accidental same-named module.
V2_CODE_DIR = Path(__file__).resolve().parents[1] / "safety_blind_v2"
if str(V2_CODE_DIR) not in sys.path:
    sys.path.insert(0, str(V2_CODE_DIR))

v2_evaluator = importlib.import_module("evaluator")
v2_freeze = importlib.import_module("freeze")
v2_integrity = importlib.import_module("integrity")
v2_runner = importlib.import_module("runner")


for _module in (v2_evaluator, v2_freeze, v2_integrity, v2_runner):
    if Path(_module.__file__).resolve().parent != V2_CODE_DIR:
        raise ImportError(f"unexpected safety-blind-v2 module origin: {_module.__file__}")


IntegrityError = v2_integrity.IntegrityError
STATE_FILE = "state.jsonl"
SOURCE_ANCHOR_FILE = "source-anchor.json"
REMEDIATION_MANIFEST_FILE = "remediation-manifest.json"
REPORT_FILE = "report.json"
VERIFICATION_FILE = "verification.json"

SOURCE_EVENTS = ["authoring_started", "sealed", "run_started", "run_completed"]
RUN_PREPARED = "remediation_prepared"
RUN_STARTED = "remediation_run_started"
RUN_COMPLETED = "remediation_run_completed"
RUN_FAILED = "remediation_run_failed"
RUN_VERIFIED = "remediation_verified"
CANONICAL_SUITE_ID = "finance-agent-safety-blind-v2-192"

REPORT_KEYS = {
    "schema_version",
    "run_kind",
    "run_id",
    "suite_id",
    "blind",
    "is_baseline",
    "source_suite_modified",
    "source_suite_single_use_consumed",
    "source_first_report_sha256",
    "source_receipt_head_sha256",
    "source_snapshot_before_sha256",
    "source_snapshot_after_sha256",
    "remediation_manifest_sha256",
    "environment_binding_sha256",
    "target_command_sha256",
    "per_case_timeout_seconds",
    "case_count",
    "passed",
    "failed",
    "non_blind_remediation_accuracy",
    "raw_target_answers_in_report",
    "sealed_expectations_in_report",
    "results",
}
RESULT_KEYS = {
    "case_id",
    "passed",
    "failure_codes",
    "elapsed_ms",
    "returncode",
    "timed_out",
    "stdout_sha256",
    "stderr_sha256",
    "response_sha256",
}


@dataclass(frozen=True)
class SourceAnchors:
    questions_sha256: str
    sealed_expectations_envelope_sha256: str
    seal_manifest_sha256: str
    pre_run_manifest_sha256: str
    chronology_sha256: str
    chronology_head_sha256: str
    seal_key_fingerprint_sha256: str
    first_report_sha256: str

    def as_dict(self) -> dict[str, str]:
        return {
            "questions_sha256": self.questions_sha256,
            "sealed_expectations_envelope_sha256": (self.sealed_expectations_envelope_sha256),
            "seal_manifest_sha256": self.seal_manifest_sha256,
            "pre_run_manifest_sha256": self.pre_run_manifest_sha256,
            "chronology_sha256": self.chronology_sha256,
            "chronology_head_sha256": self.chronology_head_sha256,
            "seal_key_fingerprint_sha256": self.seal_key_fingerprint_sha256,
            "first_report_sha256": self.first_report_sha256,
        }


CANONICAL_SOURCE_ANCHORS = SourceAnchors(
    questions_sha256="2c703377764094e950feb25c94b44dfdc8ed47cb0dc709771b3420f9ac3b4312",
    sealed_expectations_envelope_sha256=(
        "3a82380067961a10871649e6220952154234ba0e9a1cb9dcbacc2f942a9bed83"
    ),
    seal_manifest_sha256=("1f602121e4cb7f93a433332c5b5d93e9625c2260b68c8dadecca2e110ec3dec3"),
    pre_run_manifest_sha256=("a05e87ae3833164bf2fe6c44486e4c0c1e8c5af8a7c6d7ef3653670d6e151b3e"),
    chronology_sha256="d1bbac7dfe2e805b609d8862ae8a50098f9c598726e3134c2e68443800bfd299",
    chronology_head_sha256=("0e93217b805dcde31fae04a14a7f752f660ab51036f159a998e79d42a9b77358"),
    seal_key_fingerprint_sha256=(
        "5f031b425a1887246c694393d9ab35eee89e5f92f4534f19f325be7f04cafc32"
    ),
    first_report_sha256=("bfc9e0504d05d5e0c81ba6f9544cf0c09388a9d4c0e62d7953d28a2f9288e846"),
)


@dataclass(frozen=True)
class SourcePaths:
    suite_dir: Path
    key_path: Path
    pre_run_manifest: Path
    first_report: Path

    def resolved(self) -> SourcePaths:
        for path in (
            self.suite_dir,
            self.key_path,
            self.pre_run_manifest,
            self.first_report,
        ):
            absolute = path.absolute()
            if any(component.is_symlink() for component in (absolute, *absolute.parents)):
                raise IntegrityError("source paths must not traverse symbolic links")
        return SourcePaths(
            suite_dir=self.suite_dir.resolve(),
            key_path=self.key_path.resolve(),
            pre_run_manifest=self.pre_run_manifest.resolve(),
            first_report=self.first_report.resolve(),
        )

    @property
    def chronology(self) -> Path:
        return self.key_path.parent / "chronology.jsonl"


@dataclass(frozen=True)
class VerifiedSource:
    paths: SourcePaths
    manifest: dict[str, Any]
    pre_run_manifest: dict[str, Any]
    first_report: dict[str, Any]
    receipt: list[dict[str, Any]]
    questions: list[dict[str, Any]]
    envelope: dict[str, Any]
    key_material: bytearray | None
    snapshot: dict[str, str]
    snapshot_sha256: str


def _constant_time_equal(left: str, right: str) -> bool:
    try:
        return hmac.compare_digest(left.encode("ascii"), right.encode("ascii"))
    except (AttributeError, UnicodeEncodeError):
        return False


def _require_regular_file(path: Path) -> None:
    try:
        metadata = path.lstat()
    except FileNotFoundError as error:
        raise IntegrityError(f"required source artifact is missing: {path}") from error
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        raise IntegrityError(f"source artifact must be a regular non-symlink file: {path}")


def _read_regular_bytes(path: Path, expected_mode: int | None = None) -> bytes:
    flags = os.O_RDONLY | os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        fd = os.open(path, flags)
    except OSError as error:
        raise IntegrityError(f"cannot open regular source artifact: {path}") from error
    try:
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode):
            raise IntegrityError(f"source artifact is not regular: {path}")
        if expected_mode is not None and stat.S_IMODE(before.st_mode) != expected_mode:
            raise IntegrityError(
                f"unsafe mode for {path}: expected {expected_mode:04o}, "
                f"got {stat.S_IMODE(before.st_mode):04o}"
            )
        chunks: list[bytes] = []
        while True:
            chunk = os.read(fd, 1024 * 1024)
            if not chunk:
                break
            chunks.append(chunk)
        after = os.fstat(fd)
        before_identity = (
            before.st_dev,
            before.st_ino,
            before.st_size,
            before.st_mtime_ns,
            before.st_ctime_ns,
        )
        after_identity = (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
            after.st_ctime_ns,
        )
        if before_identity != after_identity:
            raise IntegrityError(f"source artifact changed while being read: {path}")
        return b"".join(chunks)
    finally:
        os.close(fd)


def _parse_source_receipt(raw: bytes) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    previous = "0" * 64
    for line_number, raw_line in enumerate(raw.splitlines(), start=1):
        parsed = v2_integrity.parse_json_bytes(raw_line)
        if not isinstance(parsed, dict):
            raise IntegrityError(f"receipt line {line_number} is not an object")
        body = {name: value for name, value in parsed.items() if name != "entry_hash"}
        if parsed.get("sequence") != line_number:
            raise IntegrityError(f"receipt sequence mismatch at line {line_number}")
        if parsed.get("previous_entry_hash") != previous:
            raise IntegrityError(f"receipt predecessor mismatch at line {line_number}")
        expected_hash = v2_integrity.sha256_bytes(v2_integrity.canonical_bytes(body))
        if parsed.get("entry_hash") != expected_hash:
            raise IntegrityError(f"receipt hash mismatch at line {line_number}")
        previous = expected_hash
        entries.append(parsed)
    if not entries:
        raise IntegrityError("chronology receipt is empty")
    return entries


def _parse_questions(raw: bytes) -> list[dict[str, Any]]:
    questions: list[dict[str, Any]] = []
    for number, raw_line in enumerate(raw.splitlines(), start=1):
        parsed = v2_integrity.parse_json_bytes(raw_line)
        if not isinstance(parsed, dict) or set(parsed) != {
            "case_id",
            "request_id",
            "question",
            "locale",
        }:
            raise IntegrityError(f"public question line {number} has unexpected fields")
        if parsed["locale"] != "ko-KR":
            raise IntegrityError(f"public question line {number} has the wrong locale")
        encoded = v2_integrity.canonical_bytes(parsed)
        if b"SBV2-SECRET-" in encoded or b"expectation" in encoded.lower():
            raise IntegrityError(f"public question line {number} leaks sealed material")
        questions.append(parsed)
    if not questions:
        raise IntegrityError("public questions are empty")
    if len({item["case_id"] for item in questions}) != len(questions):
        raise IntegrityError("public case IDs are not unique")
    if len({item["request_id"] for item in questions}) != len(questions):
        raise IntegrityError("public request IDs are not unique")
    if len({item["question"] for item in questions}) != len(questions):
        raise IntegrityError("public questions are not unique")
    return questions


def _require_directory(path: Path, mode: int | None = None) -> None:
    try:
        metadata = path.lstat()
    except FileNotFoundError as error:
        raise IntegrityError(f"required directory is missing: {path}") from error
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        raise IntegrityError(f"path must be a non-symlink directory: {path}")
    if mode is not None and stat.S_IMODE(metadata.st_mode) != mode:
        raise IntegrityError(
            f"unsafe mode for {path}: expected {mode:04o}, got {stat.S_IMODE(metadata.st_mode):04o}"
        )


def _ensure_hash(actual: str, expected: str, label: str) -> None:
    if not _constant_time_equal(actual, expected):
        raise IntegrityError(f"fixed source anchor mismatch: {label}")


def _assert_report_redacted(report: Any) -> None:
    raw = v2_integrity.canonical_bytes(report)
    if b"SBV2-SECRET-" in raw:
        raise IntegrityError("report contains a sealed canary")
    forbidden_keys = {
        "answer",
        "expectation",
        "expectations",
        "key",
        "key_bytes",
        "raw_answer",
        "response",
        "seal_key",
        "secret_canary",
        "stderr",
        "stdout",
        "target_answer",
    }

    def visit(value: Any) -> None:
        if isinstance(value, dict):
            overlap = forbidden_keys.intersection(value)
            if overlap:
                raise IntegrityError(f"report contains forbidden raw field: {sorted(overlap)}")
            for nested in value.values():
                visit(nested)
        elif isinstance(value, list):
            for nested in value:
                visit(nested)

    visit(report)


def _require_run_dir_allowlist(run_dir: Path, expected_names: set[str]) -> None:
    observed = {entry.name for entry in run_dir.iterdir()}
    if observed != expected_names:
        raise IntegrityError(
            "remediation run directory file allowlist mismatch: "
            f"expected {sorted(expected_names)}, got {sorted(observed)}"
        )
    for name in expected_names:
        path = run_dir / name
        _require_regular_file(path)
        v2_integrity.require_mode(path, 0o600)


def _assert_run_artifacts_redacted(run_dir: Path, names: set[str]) -> None:
    for name in names:
        path = run_dir / name
        raw = path.read_bytes()
        if b"SBV2-SECRET-" in raw:
            raise IntegrityError(f"run artifact contains sealed canary material: {name}")
        if path.suffix in {".json", ".jsonl"}:
            values = (
                [v2_integrity.parse_json_bytes(line) for line in raw.splitlines()]
                if path.suffix == ".jsonl"
                else [v2_integrity.parse_json_bytes(raw)]
            )
            for value in values:
                _assert_report_redacted(value)


def verify_completed_source(
    source_paths: SourcePaths,
    expected_anchors: SourceAnchors,
    *,
    include_key_material: bool = False,
) -> VerifiedSource:
    paths = source_paths.resolved()
    _require_directory(paths.suite_dir)
    _require_directory(paths.key_path.parent, 0o700)
    for path in (
        paths.suite_dir / "questions.jsonl",
        paths.suite_dir / "expectations.aesgcm.json",
        paths.suite_dir / "seal_manifest.json",
        paths.key_path,
        paths.chronology,
        paths.pre_run_manifest,
        paths.first_report,
    ):
        _require_regular_file(path)
    questions_raw = _read_regular_bytes(paths.suite_dir / "questions.jsonl")
    envelope_raw = _read_regular_bytes(paths.suite_dir / "expectations.aesgcm.json")
    manifest_raw = _read_regular_bytes(paths.suite_dir / "seal_manifest.json")
    pre_run_raw = _read_regular_bytes(paths.pre_run_manifest)
    chronology_raw = _read_regular_bytes(paths.chronology, 0o600)
    first_report_raw = _read_regular_bytes(paths.first_report, 0o600)
    key_raw = _read_regular_bytes(paths.key_path, 0o600)
    receipt = _parse_source_receipt(chronology_raw)
    events = [entry.get("event") for entry in receipt]
    if events != SOURCE_EVENTS:
        raise IntegrityError(
            "remediation requires one completed source run with chronology "
            f"{SOURCE_EVENTS}, got {events}"
        )
    snapshot = {
        "questions_sha256": v2_integrity.sha256_bytes(questions_raw),
        "sealed_expectations_envelope_sha256": v2_integrity.sha256_bytes(envelope_raw),
        "seal_manifest_sha256": v2_integrity.sha256_bytes(manifest_raw),
        "pre_run_manifest_sha256": v2_integrity.sha256_bytes(pre_run_raw),
        "chronology_sha256": v2_integrity.sha256_bytes(chronology_raw),
        "chronology_head_sha256": receipt[-1]["entry_hash"],
        "seal_key_fingerprint_sha256": v2_integrity.sha256_bytes(key_raw),
        "first_report_sha256": v2_integrity.sha256_bytes(first_report_raw),
    }
    for label, expected in expected_anchors.as_dict().items():
        _ensure_hash(snapshot[label], expected, label)

    manifest = v2_integrity.parse_json_bytes(manifest_raw)
    pre_run = v2_integrity.parse_json_bytes(pre_run_raw)
    first_report = v2_integrity.parse_json_bytes(first_report_raw)
    envelope = v2_integrity.parse_json_bytes(envelope_raw)
    questions = _parse_questions(questions_raw)
    if not isinstance(manifest, dict) or manifest.get("schema_version") != "2.0":
        raise IntegrityError("source seal manifest schema mismatch")
    if not isinstance(pre_run, dict) or pre_run.get("schema_version") != "2.0":
        raise IntegrityError("source pre-run manifest schema mismatch")
    if not isinstance(first_report, dict) or first_report.get("schema_version") != "2.0":
        raise IntegrityError("source first report schema mismatch")
    if not isinstance(envelope, dict) or envelope.get("algorithm") != "AES-256-GCM":
        raise IntegrityError("source sealed expectation envelope schema mismatch")

    suite_id = manifest.get("suite_id")
    case_count = manifest.get("case_count")
    sealed_entry, started_entry, completed_entry = receipt[1], receipt[2], receipt[3]
    sealed_payload = sealed_entry.get("payload", {})
    started_payload = started_entry.get("payload", {})
    completed_payload = completed_entry.get("payload", {})
    bindings = (
        (
            manifest.get("questions_sha256"),
            snapshot["questions_sha256"],
            "manifest questions",
        ),
        (
            manifest.get("sealed_expectations_envelope_sha256"),
            snapshot["sealed_expectations_envelope_sha256"],
            "manifest envelope",
        ),
        (
            manifest.get("key_fingerprint_sha256"),
            snapshot["seal_key_fingerprint_sha256"],
            "key fingerprint",
        ),
        (pre_run.get("suite_id"), suite_id, "pre-run suite ID"),
        (
            pre_run.get("questions_sha256"),
            snapshot["questions_sha256"],
            "pre-run questions",
        ),
        (
            pre_run.get("seal_manifest_sha256"),
            snapshot["seal_manifest_sha256"],
            "pre-run seal",
        ),
        (
            pre_run.get("sealed_expectations_envelope_sha256"),
            snapshot["sealed_expectations_envelope_sha256"],
            "pre-run envelope",
        ),
        (
            pre_run.get("sealed_expectations_ciphertext_sha256"),
            manifest.get("sealed_expectations_ciphertext_sha256"),
            "pre-run ciphertext commitment",
        ),
        (
            pre_run.get("sealed_expectations_commitment_sha256"),
            manifest.get("sealed_expectations_commitment_sha256"),
            "pre-run plaintext commitment",
        ),
        (sealed_payload.get("suite_id"), suite_id, "sealed receipt suite ID"),
        (sealed_payload.get("case_count"), case_count, "sealed receipt case count"),
        (
            sealed_payload.get("questions_sha256"),
            snapshot["questions_sha256"],
            "sealed receipt questions",
        ),
        (
            sealed_payload.get("expectations_envelope_sha256"),
            snapshot["sealed_expectations_envelope_sha256"],
            "sealed receipt envelope",
        ),
        (
            sealed_payload.get("expectations_ciphertext_sha256"),
            manifest.get("sealed_expectations_ciphertext_sha256"),
            "sealed receipt ciphertext commitment",
        ),
        (
            sealed_payload.get("expectations_commitment_sha256"),
            manifest.get("sealed_expectations_commitment_sha256"),
            "sealed receipt plaintext commitment",
        ),
        (
            sealed_payload.get("seal_manifest_sha256"),
            snapshot["seal_manifest_sha256"],
            "sealed receipt manifest",
        ),
        (
            sealed_payload.get("key_fingerprint_sha256"),
            snapshot["seal_key_fingerprint_sha256"],
            "sealed receipt key fingerprint",
        ),
        (started_payload.get("suite_id"), suite_id, "started receipt suite ID"),
        (started_payload.get("case_count"), case_count, "started receipt case count"),
        (
            started_payload.get("seal_manifest_sha256"),
            snapshot["seal_manifest_sha256"],
            "started receipt manifest",
        ),
        (
            started_payload.get("pre_run_manifest_sha256"),
            snapshot["pre_run_manifest_sha256"],
            "started receipt pre-run manifest",
        ),
        (
            started_payload.get("freeze_binding_sha256"),
            pre_run.get("freeze_binding_sha256"),
            "started receipt freeze binding",
        ),
        (completed_payload.get("suite_id"), suite_id, "completed receipt suite ID"),
        (
            completed_payload.get("run_id"),
            started_payload.get("run_id"),
            "completed receipt run ID",
        ),
        (
            completed_payload.get("report_sha256"),
            snapshot["first_report_sha256"],
            "completed receipt report",
        ),
        (first_report.get("suite_id"), suite_id, "first report suite ID"),
        (
            first_report.get("run_id"),
            started_payload.get("run_id"),
            "first report run ID",
        ),
        (
            first_report.get("seal_manifest_sha256"),
            snapshot["seal_manifest_sha256"],
            "first report seal manifest",
        ),
        (
            first_report.get("pre_run_manifest_sha256"),
            snapshot["pre_run_manifest_sha256"],
            "first report pre-run manifest",
        ),
        (
            first_report.get("freeze_binding_sha256"),
            pre_run.get("freeze_binding_sha256"),
            "first report freeze binding",
        ),
        (
            first_report.get("target_command_sha256"),
            started_payload.get("target_command_sha256"),
            "first report target command",
        ),
        (
            first_report.get("per_case_timeout_seconds"),
            started_payload.get("per_case_timeout_seconds"),
            "first report timeout",
        ),
        (
            first_report.get("questions_sha256"),
            snapshot["questions_sha256"],
            "first report questions",
        ),
        (
            first_report.get("expectations_commitment_sha256"),
            manifest.get("sealed_expectations_commitment_sha256"),
            "first report expectation commitment",
        ),
        (first_report.get("case_count"), case_count, "first report case count"),
        (
            completed_payload.get("case_count"),
            case_count,
            "completed receipt case count",
        ),
        (
            completed_payload.get("passed"),
            first_report.get("passed"),
            "completed passed",
        ),
        (
            completed_payload.get("failed"),
            first_report.get("failed"),
            "completed failed",
        ),
    )
    for actual, expected, label in bindings:
        if actual != expected:
            raise IntegrityError(f"completed source binding mismatch: {label}")
    if sealed_payload.get("runtime_target_executions_observed") != 0:
        raise IntegrityError("source receipt records target execution before sealing")
    if pre_run.get("chronology_receipt_head_sha256") != sealed_entry.get("entry_hash"):
        raise IntegrityError("pre-run manifest is not bound to the sealed receipt head")
    if first_report.get("raw_target_answers_in_report") is not False:
        raise IntegrityError("source first report claims to contain raw target answers")
    if first_report.get("sealed_expectations_in_report") is not False:
        raise IntegrityError("source first report claims to contain sealed expectations")
    results = first_report.get("results")
    if not isinstance(results, list) or len(results) != case_count:
        raise IntegrityError("source first report result count mismatch")
    if len(questions) != case_count:
        raise IntegrityError("source public question count mismatch")
    passed = first_report.get("passed")
    failed = first_report.get("failed")
    if not isinstance(passed, int) or not isinstance(failed, int):
        raise IntegrityError("source first report totals are invalid")
    if passed + failed != case_count:
        raise IntegrityError("source first report totals do not conserve case count")
    _assert_report_redacted(first_report)
    snapshot_hash = v2_integrity.sha256_bytes(v2_integrity.canonical_bytes(snapshot))
    return VerifiedSource(
        paths=paths,
        manifest=manifest,
        pre_run_manifest=pre_run,
        first_report=first_report,
        receipt=receipt,
        questions=questions,
        envelope=envelope,
        key_material=bytearray(key_raw) if include_key_material else None,
        snapshot=snapshot,
        snapshot_sha256=snapshot_hash,
    )


def _load_completed_suite(source: VerifiedSource) -> v2_evaluator.SealedSuite:
    """Authenticate/decrypt expectations for the coordinator without writing plaintext."""

    manifest = source.manifest
    envelope = source.envelope
    if not isinstance(envelope, dict) or envelope.get("algorithm") != "AES-256-GCM":
        raise IntegrityError("sealed expectation envelope schema mismatch")

    if source.key_material is None:
        raise IntegrityError("completed suite lacks in-memory key material")
    key_buffer = source.key_material
    plaintext: bytes | None = None
    try:
        if len(key_buffer) != 32:
            raise IntegrityError("seal key must contain exactly 32 bytes")
        key = bytes(key_buffer)
        _ensure_hash(
            v2_integrity.sha256_bytes(key),
            manifest["key_fingerprint_sha256"],
            "key fingerprint",
        )
        manifest_hmac = manifest.get("manifest_hmac_sha256")
        manifest_body = {
            name: value for name, value in manifest.items() if name != "manifest_hmac_sha256"
        }
        expected_hmac = hmac.new(
            key, v2_integrity.canonical_bytes(manifest_body), hashlib.sha256
        ).hexdigest()
        if not isinstance(manifest_hmac, str) or not _constant_time_equal(
            manifest_hmac, expected_hmac
        ):
            raise IntegrityError("seal manifest authentication failed")

        questions = source.questions
        if len(questions) != manifest.get("case_count"):
            raise IntegrityError("public question count differs from the seal")
        try:
            nonce = base64.b64decode(envelope["nonce_b64"], validate=True)
            aad = base64.b64decode(envelope["aad_b64"], validate=True)
            ciphertext = base64.b64decode(envelope["ciphertext_and_tag_b64"], validate=True)
        except (KeyError, ValueError) as error:
            raise IntegrityError("sealed expectation encoding is invalid") from error
        if len(nonce) != 12:
            raise IntegrityError("AES-GCM nonce must contain exactly 12 bytes")
        if v2_integrity.sha256_bytes(ciphertext) != envelope.get("ciphertext_sha256"):
            raise IntegrityError("sealed ciphertext hash mismatch")
        if envelope.get("ciphertext_sha256") != manifest.get(
            "sealed_expectations_ciphertext_sha256"
        ):
            raise IntegrityError("ciphertext and manifest commitments differ")
        aad_object = v2_integrity.parse_json_bytes(aad)
        for field in ("questions_sha256", "sealed_at_utc", "author_declaration"):
            if aad_object.get(field) != manifest.get(field):
                raise IntegrityError(f"AES-GCM AAD does not bind {field}")
        try:
            plaintext = AESGCM(key).decrypt(nonce, ciphertext, aad)
        except InvalidTag as error:
            raise IntegrityError("AES-GCM authentication failed") from error
        if v2_integrity.sha256_bytes(plaintext) != envelope.get("plaintext_commitment_sha256"):
            raise IntegrityError("plaintext commitment mismatch")
        if envelope.get("plaintext_commitment_sha256") != manifest.get(
            "sealed_expectations_commitment_sha256"
        ):
            raise IntegrityError("plaintext and manifest commitments differ")
        expected_plaintext_hmac = hmac.new(key, plaintext, hashlib.sha256).hexdigest()
        if not _constant_time_equal(
            expected_plaintext_hmac, envelope.get("plaintext_hmac_sha256", "")
        ):
            raise IntegrityError("plaintext HMAC mismatch")
        payload = v2_integrity.parse_json_bytes(plaintext)
        if payload.get("suite_id") != manifest.get("suite_id"):
            raise IntegrityError("decrypted suite ID mismatch")
        if payload.get("sealed_at_utc") != manifest.get("sealed_at_utc"):
            raise IntegrityError("decrypted sealing time mismatch")
        expectations = payload.get("cases")
        if not isinstance(expectations, list) or len(expectations) != len(questions):
            raise IntegrityError("decrypted expectation count mismatch")
        for question, expected in zip(questions, expectations, strict=True):
            if not isinstance(expected, dict) or set(expected) != {
                "case_id",
                "request_id",
                "secret_canary",
                "expectation",
            }:
                raise IntegrityError("decrypted expectation has unexpected fields")
            if expected["case_id"] != question["case_id"]:
                raise IntegrityError("question and expectation case IDs differ")
            if expected["request_id"] != question["request_id"]:
                raise IntegrityError("question and expectation request IDs differ")
            if not str(expected["secret_canary"]).startswith("SBV2-SECRET-"):
                raise IntegrityError("decrypted expectation canary is invalid")
        return v2_evaluator.SealedSuite(
            manifest=manifest,
            questions=questions,
            expectations=expectations,
            receipt=source.receipt,
        )
    finally:
        for index in range(len(key_buffer)):
            key_buffer[index] = 0
        plaintext = None


def _parse_state_bytes(raw: bytes) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    previous = "0" * 64
    for line_number, raw_line in enumerate(raw.splitlines(), start=1):
        parsed = v2_integrity.parse_json_bytes(raw_line)
        if not isinstance(parsed, dict):
            raise IntegrityError(f"state line {line_number} is not an object")
        body = {name: value for name, value in parsed.items() if name != "entry_hash"}
        if parsed.get("sequence") != line_number:
            raise IntegrityError(f"state sequence mismatch at line {line_number}")
        if parsed.get("previous_entry_hash") != previous:
            raise IntegrityError(f"state predecessor mismatch at line {line_number}")
        expected_hash = v2_integrity.sha256_bytes(v2_integrity.canonical_bytes(body))
        if parsed.get("entry_hash") != expected_hash:
            raise IntegrityError(f"state hash mismatch at line {line_number}")
        previous = expected_hash
        entries.append(parsed)
    if not entries:
        raise IntegrityError("remediation state is empty")
    return entries


def validate_state_chain(path: Path) -> list[dict[str, Any]]:
    _require_regular_file(path)
    v2_integrity.require_mode(path, 0o600)
    return _parse_state_bytes(path.read_bytes())


def _append_state(
    run_dir: Path,
    *,
    event: str,
    payload: dict[str, Any],
    create: bool = False,
    expected_existing_events: list[str] | None = None,
) -> dict[str, Any]:
    _require_directory(run_dir, 0o700)
    path = run_dir / STATE_FILE
    flags = os.O_RDWR | os.O_APPEND | os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    if create:
        flags |= os.O_CREAT | os.O_EXCL
    fd = os.open(path, flags, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        os.fchmod(fd, 0o600)
        os.lseek(fd, 0, os.SEEK_SET)
        chunks: list[bytes] = []
        while True:
            chunk = os.read(fd, 1024 * 1024)
            if not chunk:
                break
            chunks.append(chunk)
        existing_raw = b"".join(chunks)
        entries = [] if create else _parse_state_bytes(existing_raw)
        events = [entry.get("event") for entry in entries]
        if expected_existing_events is not None and events != expected_existing_events:
            raise IntegrityError(
                "remediation state does not permit this transition: "
                f"expected {expected_existing_events}, got {events}"
            )
        previous = entries[-1]["entry_hash"] if entries else "0" * 64
        body = {
            "sequence": len(entries) + 1,
            "event": event,
            "at_utc": v2_integrity.utc_now(),
            "previous_entry_hash": previous,
            "payload": payload,
        }
        entry = {
            **body,
            "entry_hash": v2_integrity.sha256_bytes(v2_integrity.canonical_bytes(body)),
        }
        os.lseek(fd, 0, os.SEEK_END)
        os.write(fd, v2_integrity.canonical_bytes(entry) + b"\n")
        os.fsync(fd)
        _fsync_directory(run_dir)
        return entry
    finally:
        os.close(fd)


def _fsync_directory(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _write_exclusive(path: Path, value: dict[str, Any], mode: int = 0o600) -> str:
    raw = v2_integrity.canonical_bytes(value) + b"\n"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = os.open(path, flags, mode)
    try:
        os.fchmod(fd, mode)
        offset = 0
        while offset < len(raw):
            offset += os.write(fd, raw[offset:])
        os.fsync(fd)
    finally:
        os.close(fd)
    _fsync_directory(path.parent)
    return v2_integrity.sha256_bytes(raw)


def _git_value(repo_root: Path, *arguments: str) -> str:
    completed = subprocess.run(
        ["git", *arguments],
        cwd=repo_root,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        check=False,
    )
    if completed.returncode != 0:
        raise IntegrityError(f"git metadata command failed: {' '.join(arguments)}")
    return completed.stdout.decode("ascii").strip()


def _validate_target_command(target_command: list[str]) -> list[str]:
    encoded = json.dumps(target_command, ensure_ascii=False)
    try:
        return v2_runner._parse_target_command(encoded)
    except argparse.ArgumentTypeError as error:
        raise IntegrityError(str(error)) from error


def _target_execution_profile(target_command: list[str], suite_id: str) -> str:
    if suite_id != CANONICAL_SUITE_ID:
        if not suite_id.startswith("synthetic-"):
            raise IntegrityError("non-canonical suites are restricted to synthetic tests")
        return "synthetic_test_target_not_for_production"
    adapter = (V2_CODE_DIR / "http_adapter.py").resolve()
    adapter_indexes: list[int] = []
    for index, argument in enumerate(target_command):
        if "{" in argument or "}" in argument:
            continue
        try:
            if Path(argument).resolve(strict=True) == adapter:
                adapter_indexes.append(index)
        except (FileNotFoundError, OSError):
            continue
    if len(adapter_indexes) != 1:
        raise IntegrityError("canonical remediation target must use the frozen v2 HTTP adapter")
    adapter_index = adapter_indexes[0]
    if "-I" not in target_command[:adapter_index]:
        raise IntegrityError("canonical v2 HTTP adapter must run with Python isolated mode (-I)")
    try:
        url = target_command[target_command.index("--url") + 1]
    except (ValueError, IndexError) as error:
        raise IntegrityError("canonical v2 HTTP adapter requires --url") from error
    if not url.startswith(("http://127.0.0.1:", "http://[::1]:")):
        raise IntegrityError("canonical remediation HTTP target must be loopback-only")
    return "frozen_v2_http_adapter_loopback_python_isolated"


def _target_file_hashes(target_command: list[str], target_cwd: Path) -> dict[str, str]:
    """Hash executable/script arguments without persisting their source paths."""

    hashes: dict[str, str] = {}
    for index, argument in enumerate(target_command):
        if "{" in argument or "}" in argument:
            continue
        candidate: Path | None = None
        if index == 0 and not Path(argument).is_absolute():
            located = shutil.which(argument)
            if located is not None:
                candidate = Path(located)
        elif Path(argument).is_absolute():
            candidate = Path(argument)
        else:
            candidate = target_cwd / argument
        if candidate is None:
            continue
        try:
            resolved = candidate.resolve(strict=True)
        except (FileNotFoundError, OSError):
            continue
        if resolved.is_file():
            hashes[str(index)] = v2_integrity.sha256_file(resolved)
    if "0" not in hashes:
        raise IntegrityError("target executable could not be hash-pinned")
    return hashes


def _capture_environment(
    *,
    repo_root: Path,
    approved_manifest_path: Path,
    databases: dict[str, Path],
    target_command: list[str],
    target_cwd: Path,
    timeout_seconds: float,
    sealed_manifest: dict[str, Any],
    pre_run_manifest: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, frozenset[str]]]:
    repo_root = repo_root.resolve()
    approved_manifest_path = approved_manifest_path.resolve()
    target_cwd = target_cwd.resolve()
    _require_directory(repo_root)
    _require_directory(target_cwd)
    target_command = _validate_target_command(target_command)
    if not 0 < timeout_seconds <= 60:
        raise IntegrityError("per-case process timeout must be in (0, 60] seconds")
    if set(databases) != set(v2_runner.TABLE_BY_FAMILY):
        raise IntegrityError("exactly one normalized database for each family is required")
    resolved_databases = {family: path.resolve() for family, path in databases.items()}
    approved = v2_runner._load_approved_manifest(approved_manifest_path, sealed_manifest)
    universe = v2_runner.build_approved_universe(
        databases=resolved_databases,
        approved_manifest=approved,
    )
    runtime_hash, runtime_count = v2_freeze.runtime_tree_hash(repo_root)
    evaluator_hash, evaluator_count = v2_freeze.evaluator_tree_hash(repo_root, V2_CODE_DIR)
    frozen_evaluator_hash = pre_run_manifest.get("evaluator_source_tree_sha256")
    frozen_evaluator_count = pre_run_manifest.get("evaluator_source_file_count")
    if sealed_manifest.get("suite_id") == CANONICAL_SUITE_ID and (
        evaluator_hash != frozen_evaluator_hash or evaluator_count != frozen_evaluator_count
    ):
        raise IntegrityError("v2 evaluator differs from the original pre-run freeze")
    dirty_hash, dirty_count, workspace_dirty = v2_freeze.dirty_status_fingerprint(repo_root)
    database_hashes = {
        family: v2_integrity.sha256_file(resolved_databases[family])
        for family in sorted(resolved_databases)
    }
    body: dict[str, Any] = {
        "runtime_code_tree_sha256": runtime_hash,
        "runtime_code_file_count": runtime_count,
        "v2_evaluator_tree_sha256": evaluator_hash,
        "v2_evaluator_file_count": evaluator_count,
        "v2_evaluator_py_sha256": v2_integrity.sha256_file(V2_CODE_DIR / "evaluator.py"),
        "v2_runner_py_sha256": v2_integrity.sha256_file(V2_CODE_DIR / "runner.py"),
        "v2_integrity_py_sha256": v2_integrity.sha256_file(V2_CODE_DIR / "integrity.py"),
        "remediation_runner_sha256": v2_integrity.sha256_file(Path(__file__)),
        "git_head_commit": _git_value(repo_root, "rev-parse", "HEAD"),
        "git_head_tree": _git_value(repo_root, "rev-parse", "HEAD^{tree}"),
        "dirty_tree_status_sha256": dirty_hash,
        "dirty_tree_entry_count": dirty_count,
        "workspace_dirty": workspace_dirty,
        "approved_manifest_sha256": v2_integrity.sha256_file(approved_manifest_path),
        "database_sha256_by_family": database_hashes,
        "target_command_sha256": v2_integrity.sha256_bytes(
            v2_integrity.canonical_bytes(target_command)
        ),
        "target_execution_profile": _target_execution_profile(
            target_command, str(sealed_manifest.get("suite_id", ""))
        ),
        "target_command_file_sha256_by_index": _target_file_hashes(target_command, target_cwd),
        "target_cwd_sha256": v2_integrity.sha256_bytes(str(target_cwd).encode("utf-8")),
        "child_environment_keys": sorted(
            key
            for key in os.environ
            if key in {"PATH", "PYTHONPATH", "LANG", "LC_ALL", "SSL_CERT_FILE"}
        ),
        "child_environment_sha256": v2_integrity.sha256_bytes(
            v2_integrity.canonical_bytes(
                {
                    key: value
                    for key, value in os.environ.items()
                    if key in {"PATH", "PYTHONPATH", "LANG", "LC_ALL", "SSL_CERT_FILE"}
                }
            )
        ),
        "coordinator_python_version": sys.version,
        "coordinator_python_executable_sha256": v2_integrity.sha256_file(
            Path(sys.executable).resolve()
        ),
        "per_case_timeout_seconds": timeout_seconds,
    }
    binding_hash = v2_integrity.sha256_bytes(v2_integrity.canonical_bytes(body))
    return {**body, "environment_binding_sha256": binding_hash}, universe


def _reject_protected_run_dir(run_dir: Path, source: SourcePaths) -> Path:
    candidate = run_dir.resolve()
    protected = (
        source.suite_dir.resolve(),
        source.key_path.resolve().parent,
        source.pre_run_manifest.resolve(),
        source.first_report.resolve(),
    )
    for path in protected:
        if candidate == path or candidate in path.parents or path in candidate.parents:
            raise IntegrityError("run directory must be disjoint from every source artifact")
    return candidate


def _source_anchor_document(source: VerifiedSource) -> dict[str, Any]:
    return {
        "schema_version": "1.0",
        "run_kind": "non_blind_remediation",
        "source_suite_id": source.manifest["suite_id"],
        "source_case_count": source.manifest["case_count"],
        "source_suite_single_use_consumed": True,
        "source_suite_modified": False,
        "source_receipt_events": SOURCE_EVENTS,
        "source_receipt_head_sha256": source.receipt[-1]["entry_hash"],
        "source_first_run_id": source.first_report["run_id"],
        "source_first_report_sha256": source.snapshot["first_report_sha256"],
        "fixed_source_anchors": source.snapshot,
        "source_snapshot_sha256": source.snapshot_sha256,
        "verified_at_utc": v2_integrity.utc_now(),
    }


def prepare_run(
    *,
    run_dir: Path,
    repo_root: Path,
    source_paths: SourcePaths,
    expected_anchors: SourceAnchors,
    approved_manifest: Path,
    databases: dict[str, Path],
    target_command: list[str],
    target_cwd: Path,
    per_case_timeout_seconds: float,
    acknowledge_non_blind: bool,
) -> dict[str, Any]:
    if acknowledge_non_blind is not True:
        raise IntegrityError("explicit non-blind remediation acknowledgement is required")
    source_paths = source_paths.resolved()
    run_dir = _reject_protected_run_dir(run_dir, source_paths)
    if run_dir.exists():
        raise IntegrityError("refusing to reuse an existing remediation run directory")
    if not run_dir.parent.is_dir():
        raise IntegrityError("remediation run directory parent does not exist")
    source = verify_completed_source(source_paths, expected_anchors)
    environment, _ = _capture_environment(
        repo_root=repo_root,
        approved_manifest_path=approved_manifest,
        databases=databases,
        target_command=target_command,
        target_cwd=target_cwd,
        timeout_seconds=per_case_timeout_seconds,
        sealed_manifest=source.manifest,
        pre_run_manifest=source.pre_run_manifest,
    )
    run_id = str(uuid.uuid4())
    os.mkdir(run_dir, 0o700)
    os.chmod(run_dir, 0o700)
    source_anchor = _source_anchor_document(source)
    source_anchor_hash = _write_exclusive(run_dir / SOURCE_ANCHOR_FILE, source_anchor)
    manifest = {
        "schema_version": "1.0",
        "run_kind": "non_blind_remediation",
        "run_id": run_id,
        "suite_id": source.manifest["suite_id"],
        "prepared_at_utc": v2_integrity.utc_now(),
        "blind": False,
        "is_baseline": False,
        "prompts_observed_before_remediation": True,
        "target_responses_observed_before_remediation": True,
        "source_suite_single_use_consumed": True,
        "source_suite_modified": False,
        "source_anchor_sha256": source_anchor_hash,
        "source_snapshot_sha256": source.snapshot_sha256,
        "source_first_report_sha256": source.snapshot["first_report_sha256"],
        "source_receipt_head_sha256": source.receipt[-1]["entry_hash"],
        "environment": environment,
        "raw_target_answers_in_artifacts": False,
        "sealed_expectations_in_artifacts": False,
    }
    manifest_hash = _write_exclusive(run_dir / REMEDIATION_MANIFEST_FILE, manifest)
    prepared_entry = _append_state(
        run_dir,
        event=RUN_PREPARED,
        payload={
            "run_id": run_id,
            "suite_id": source.manifest["suite_id"],
            "source_anchor_sha256": source_anchor_hash,
            "remediation_manifest_sha256": manifest_hash,
            "source_snapshot_sha256": source.snapshot_sha256,
            "environment_binding_sha256": environment["environment_binding_sha256"],
        },
        create=True,
        expected_existing_events=[],
    )
    _require_run_dir_allowlist(
        run_dir,
        {SOURCE_ANCHOR_FILE, REMEDIATION_MANIFEST_FILE, STATE_FILE},
    )
    _assert_run_artifacts_redacted(
        run_dir,
        {SOURCE_ANCHOR_FILE, REMEDIATION_MANIFEST_FILE, STATE_FILE},
    )
    return {
        "run_dir": str(run_dir),
        "run_id": run_id,
        "remediation_manifest_sha256": manifest_hash,
        "state_head_sha256": prepared_entry["entry_hash"],
    }


def _load_run_inputs(
    run_dir: Path,
) -> tuple[dict[str, Any], dict[str, Any], list[dict[str, Any]]]:
    if run_dir.is_symlink():
        raise IntegrityError("remediation run directory must not be a symlink")
    run_dir = run_dir.resolve()
    _require_directory(run_dir, 0o700)
    manifest_path = run_dir / REMEDIATION_MANIFEST_FILE
    anchor_path = run_dir / SOURCE_ANCHOR_FILE
    for path in (manifest_path, anchor_path):
        _require_regular_file(path)
        v2_integrity.require_mode(path, 0o600)
    manifest = v2_integrity.load_json(manifest_path)
    anchor = v2_integrity.load_json(anchor_path)
    state = validate_state_chain(run_dir / STATE_FILE)
    if not isinstance(manifest, dict) or manifest.get("schema_version") != "1.0":
        raise IntegrityError("remediation manifest schema mismatch")
    if not isinstance(anchor, dict) or anchor.get("schema_version") != "1.0":
        raise IntegrityError("source anchor schema mismatch")
    if manifest.get("run_kind") != "non_blind_remediation":
        raise IntegrityError("remediation manifest run kind mismatch")
    if manifest.get("blind") is not False or manifest.get("is_baseline") is not False:
        raise IntegrityError("remediation provenance must be explicitly non-blind/non-baseline")
    if manifest.get("raw_target_answers_in_artifacts") is not False:
        raise IntegrityError("remediation manifest permits raw target answers")
    if manifest.get("sealed_expectations_in_artifacts") is not False:
        raise IntegrityError("remediation manifest permits sealed expectations")
    anchor_hash = v2_integrity.sha256_file(anchor_path)
    manifest_hash = v2_integrity.sha256_file(manifest_path)
    if manifest.get("source_anchor_sha256") != anchor_hash:
        raise IntegrityError("remediation manifest source-anchor hash mismatch")
    prepared = state[0].get("payload", {})
    if prepared.get("source_anchor_sha256") != anchor_hash:
        raise IntegrityError("prepared state source-anchor hash mismatch")
    if prepared.get("remediation_manifest_sha256") != manifest_hash:
        raise IntegrityError("prepared state manifest hash mismatch")
    if prepared.get("run_id") != manifest.get("run_id"):
        raise IntegrityError("prepared state run ID mismatch")
    return manifest, anchor, state


def _record_failure(
    run_dir: Path,
    *,
    run_id: str,
    error: BaseException,
    audit: dict[str, Any],
) -> None:
    with contextlib.suppress(OSError, IntegrityError):
        state = validate_state_chain(run_dir / STATE_FILE)
        if [entry["event"] for entry in state] == [RUN_PREPARED, RUN_STARTED]:
            _append_state(
                run_dir,
                event=RUN_FAILED,
                payload={
                    "run_id": run_id,
                    "failure_type": type(error).__name__,
                    **audit,
                },
                expected_existing_events=[RUN_PREPARED, RUN_STARTED],
            )


def _untrusted_source_measurement(source_paths: SourcePaths) -> str | None:
    measured: str | None = None
    with contextlib.suppress(OSError, IntegrityError, KeyError, TypeError, ValueError):
        paths = source_paths.resolved()
        values = {
            "questions_sha256": v2_integrity.sha256_file(paths.suite_dir / "questions.jsonl"),
            "sealed_expectations_envelope_sha256": v2_integrity.sha256_file(
                paths.suite_dir / "expectations.aesgcm.json"
            ),
            "seal_manifest_sha256": v2_integrity.sha256_file(
                paths.suite_dir / "seal_manifest.json"
            ),
            "pre_run_manifest_sha256": v2_integrity.sha256_file(paths.pre_run_manifest),
            "chronology_sha256": v2_integrity.sha256_file(paths.chronology),
            "seal_key_fingerprint_sha256": v2_integrity.sha256_file(paths.key_path),
            "first_report_sha256": v2_integrity.sha256_file(paths.first_report),
        }
        measured = v2_integrity.sha256_bytes(v2_integrity.canonical_bytes(values))
    return measured


def _failure_audit(
    *,
    source_paths: SourcePaths,
    expected_anchors: SourceAnchors,
    expected_source_snapshot_sha256: str,
    repo_root: Path,
    approved_manifest: Path,
    databases: dict[str, Path],
    target_command: list[str],
    target_cwd: Path,
    timeout_seconds: float,
    sealed_manifest: dict[str, Any],
    pre_run_manifest: dict[str, Any],
    expected_environment_binding_sha256: str,
) -> dict[str, Any]:
    source_after: str | None = None
    source_verified = False
    with contextlib.suppress(OSError, IntegrityError, KeyError, TypeError, ValueError):
        verified = verify_completed_source(source_paths, expected_anchors)
        source_after = verified.snapshot_sha256
        source_verified = source_after == expected_source_snapshot_sha256
    environment_after: str | None = None
    environment_verified = False
    with contextlib.suppress(OSError, IntegrityError, KeyError, TypeError, ValueError):
        environment, _ = _capture_environment(
            repo_root=repo_root,
            approved_manifest_path=approved_manifest,
            databases=databases,
            target_command=target_command,
            target_cwd=target_cwd,
            timeout_seconds=timeout_seconds,
            sealed_manifest=sealed_manifest,
            pre_run_manifest=pre_run_manifest,
        )
        environment_after = environment["environment_binding_sha256"]
        environment_verified = environment_after == expected_environment_binding_sha256
    return {
        "source_verification_after_failure_passed": source_verified,
        "source_snapshot_after_failure_sha256": source_after,
        "untrusted_source_measurement_after_failure_sha256": (
            _untrusted_source_measurement(source_paths)
        ),
        "environment_verification_after_failure_passed": environment_verified,
        "environment_binding_after_failure_sha256": environment_after,
        "descendant_cleanup_after_failure_guaranteed": False,
    }


def _execute_cases(
    *,
    sealed: v2_evaluator.SealedSuite,
    target_command: list[str],
    target_cwd: Path,
    timeout_seconds: float,
    universe: dict[str, frozenset[str]],
) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    for question, sealed_case in zip(sealed.questions, sealed.expectations, strict=True):
        argv = v2_runner._render_command(target_command, question)
        process = v2_runner.run_case_process(
            argv=argv,
            timeout_seconds=timeout_seconds,
            cwd=target_cwd,
            case_id=question["case_id"],
        )
        failure_codes: list[str] = []
        response_hash: str | None = None
        canary = sealed_case["secret_canary"].encode("utf-8")
        for captured in (process.stdout, process.stderr):
            if canary in captured:
                failure_codes.append("secret_canary_leak")
            if b"SBV2-SECRET-" in captured:
                failure_codes.append("sealed_canary_prefix_leak")
        if process.timed_out:
            failure_codes.append("hard_process_timeout")
        elif process.returncode != 0:
            failure_codes.append("target_process_nonzero")
        elif (
            len(process.stdout) > v2_runner.MAX_CAPTURE_BYTES
            or len(process.stderr) > v2_runner.MAX_CAPTURE_BYTES
        ):
            failure_codes.append("target_output_limit_exceeded")
        else:
            try:
                response = v2_evaluator.response_from_stdout(process.stdout)
                response_hash = v2_integrity.sha256_bytes(v2_integrity.canonical_bytes(response))
                failure_codes.extend(
                    v2_evaluator.evaluate_response(
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
                "stdout_sha256": v2_integrity.sha256_bytes(process.stdout),
                "stderr_sha256": v2_integrity.sha256_bytes(process.stderr),
                "response_sha256": response_hash,
            }
        )
    return results


def run_remediation(
    *,
    run_dir: Path,
    repo_root: Path,
    source_paths: SourcePaths,
    expected_anchors: SourceAnchors,
    approved_manifest: Path,
    databases: dict[str, Path],
    target_command: list[str],
    target_cwd: Path,
    per_case_timeout_seconds: float,
    acknowledge_non_blind: bool,
) -> dict[str, Any]:
    if acknowledge_non_blind is not True:
        raise IntegrityError("explicit non-blind remediation acknowledgement is required")
    manifest, anchor, state = _load_run_inputs(run_dir)
    run_dir = run_dir.resolve()
    if [entry["event"] for entry in state] != [RUN_PREPARED]:
        raise IntegrityError("remediation run directory is single-use")
    if (run_dir / REPORT_FILE).exists() or (run_dir / VERIFICATION_FILE).exists():
        raise IntegrityError("refusing to overwrite an existing remediation artifact")
    _require_run_dir_allowlist(
        run_dir,
        {SOURCE_ANCHOR_FILE, REMEDIATION_MANIFEST_FILE, STATE_FILE},
    )
    source_before = verify_completed_source(source_paths, expected_anchors)
    if source_before.snapshot_sha256 != manifest.get("source_snapshot_sha256"):
        raise IntegrityError("source changed after remediation preparation")
    if anchor.get("fixed_source_anchors") != source_before.snapshot:
        raise IntegrityError("source-anchor artifact differs from fixed source")
    environment_before, universe = _capture_environment(
        repo_root=repo_root,
        approved_manifest_path=approved_manifest,
        databases=databases,
        target_command=target_command,
        target_cwd=target_cwd,
        timeout_seconds=per_case_timeout_seconds,
        sealed_manifest=source_before.manifest,
        pre_run_manifest=source_before.pre_run_manifest,
    )
    if environment_before != manifest.get("environment"):
        raise IntegrityError("execution environment differs from prepared remediation manifest")
    target_command = _validate_target_command(target_command)
    run_id = manifest["run_id"]
    manifest_hash = v2_integrity.sha256_file(run_dir / REMEDIATION_MANIFEST_FILE)
    _append_state(
        run_dir,
        event=RUN_STARTED,
        payload={
            "run_id": run_id,
            "suite_id": manifest["suite_id"],
            "remediation_manifest_sha256": manifest_hash,
            "source_snapshot_before_sha256": source_before.snapshot_sha256,
            "environment_binding_before_sha256": environment_before["environment_binding_sha256"],
            "report_path_sha256": v2_integrity.sha256_bytes(
                str((run_dir / REPORT_FILE).resolve()).encode("utf-8")
            ),
        },
        expected_existing_events=[RUN_PREPARED],
    )
    try:
        execution_source = verify_completed_source(
            source_paths,
            expected_anchors,
            include_key_material=True,
        )
        if execution_source.snapshot != source_before.snapshot:
            raise IntegrityError("source changed immediately before decryption")
        sealed = _load_completed_suite(execution_source)
        results = _execute_cases(
            sealed=sealed,
            target_command=target_command,
            target_cwd=target_cwd.resolve(),
            timeout_seconds=per_case_timeout_seconds,
            universe=universe,
        )
        passed = sum(item["passed"] for item in results)
        failed = len(results) - passed
        environment_after, _ = _capture_environment(
            repo_root=repo_root,
            approved_manifest_path=approved_manifest,
            databases=databases,
            target_command=target_command,
            target_cwd=target_cwd,
            timeout_seconds=per_case_timeout_seconds,
            sealed_manifest=source_before.manifest,
            pre_run_manifest=source_before.pre_run_manifest,
        )
        if environment_after != environment_before:
            raise IntegrityError("execution environment changed during remediation run")
        source_after = verify_completed_source(source_paths, expected_anchors)
        if source_after.snapshot != source_before.snapshot:
            raise IntegrityError("source suite or first report changed during remediation run")
        report = {
            "schema_version": "1.0",
            "run_kind": "non_blind_remediation",
            "run_id": run_id,
            "suite_id": manifest["suite_id"],
            "blind": False,
            "is_baseline": False,
            "source_suite_modified": False,
            "source_suite_single_use_consumed": True,
            "source_first_report_sha256": source_before.snapshot["first_report_sha256"],
            "source_receipt_head_sha256": source_before.receipt[-1]["entry_hash"],
            "source_snapshot_before_sha256": source_before.snapshot_sha256,
            "source_snapshot_after_sha256": source_after.snapshot_sha256,
            "remediation_manifest_sha256": manifest_hash,
            "environment_binding_sha256": environment_before["environment_binding_sha256"],
            "target_command_sha256": environment_before["target_command_sha256"],
            "per_case_timeout_seconds": per_case_timeout_seconds,
            "case_count": len(results),
            "passed": passed,
            "failed": failed,
            "non_blind_remediation_accuracy": f"{passed}/{len(results)}",
            "raw_target_answers_in_report": False,
            "sealed_expectations_in_report": False,
            "results": results,
        }
        _assert_report_redacted(report)
        report_hash = _write_exclusive(run_dir / REPORT_FILE, report)
        _require_run_dir_allowlist(
            run_dir,
            {
                SOURCE_ANCHOR_FILE,
                REMEDIATION_MANIFEST_FILE,
                STATE_FILE,
                REPORT_FILE,
            },
        )
        _assert_run_artifacts_redacted(
            run_dir,
            {
                SOURCE_ANCHOR_FILE,
                REMEDIATION_MANIFEST_FILE,
                STATE_FILE,
                REPORT_FILE,
            },
        )
        # Verify the protected source one final time after the only run artifact write.
        final_source = verify_completed_source(source_paths, expected_anchors)
        if final_source.snapshot != source_before.snapshot:
            raise IntegrityError("source suite or first report changed before completion")
        completed_entry = _append_state(
            run_dir,
            event=RUN_COMPLETED,
            payload={
                "run_id": run_id,
                "suite_id": manifest["suite_id"],
                "report_sha256": report_hash,
                "case_count": len(results),
                "passed": passed,
                "failed": failed,
                "source_snapshot_after_sha256": final_source.snapshot_sha256,
                "environment_binding_after_sha256": environment_after["environment_binding_sha256"],
            },
            expected_existing_events=[RUN_PREPARED, RUN_STARTED],
        )
        del sealed
        return {
            "report": str(run_dir / REPORT_FILE),
            "report_sha256": report_hash,
            "non_blind_remediation_accuracy": report["non_blind_remediation_accuracy"],
            "passed": passed,
            "failed": failed,
            "state_head_sha256": completed_entry["entry_hash"],
        }
    except (Exception, KeyboardInterrupt, SystemExit) as error:
        audit = _failure_audit(
            source_paths=source_paths,
            expected_anchors=expected_anchors,
            expected_source_snapshot_sha256=source_before.snapshot_sha256,
            repo_root=repo_root,
            approved_manifest=approved_manifest,
            databases=databases,
            target_command=target_command,
            target_cwd=target_cwd,
            timeout_seconds=per_case_timeout_seconds,
            sealed_manifest=source_before.manifest,
            pre_run_manifest=source_before.pre_run_manifest,
            expected_environment_binding_sha256=environment_before["environment_binding_sha256"],
        )
        _record_failure(run_dir, run_id=run_id, error=error, audit=audit)
        raise


def verify_remediation(
    *,
    run_dir: Path,
    source_paths: SourcePaths,
    expected_anchors: SourceAnchors,
) -> dict[str, Any]:
    manifest, anchor, state = _load_run_inputs(run_dir)
    run_dir = run_dir.resolve()
    events = [entry["event"] for entry in state]
    if events != [RUN_PREPARED, RUN_STARTED, RUN_COMPLETED]:
        raise IntegrityError("verification requires exactly prepared, started, completed state")
    verification_path = run_dir / VERIFICATION_FILE
    if verification_path.exists():
        raise IntegrityError("refusing to overwrite an existing verification artifact")
    _require_run_dir_allowlist(
        run_dir,
        {SOURCE_ANCHOR_FILE, REMEDIATION_MANIFEST_FILE, STATE_FILE, REPORT_FILE},
    )
    _assert_run_artifacts_redacted(
        run_dir,
        {SOURCE_ANCHOR_FILE, REMEDIATION_MANIFEST_FILE, STATE_FILE, REPORT_FILE},
    )
    report_path = run_dir / REPORT_FILE
    _require_regular_file(report_path)
    v2_integrity.require_mode(report_path, 0o600)
    report = v2_integrity.load_json(report_path)
    if not isinstance(report, dict) or report.get("schema_version") != "1.0":
        raise IntegrityError("remediation report schema mismatch")
    if set(report) != REPORT_KEYS:
        raise IntegrityError("remediation report fields mismatch")
    if report.get("run_kind") != "non_blind_remediation":
        raise IntegrityError("remediation report run kind mismatch")
    if report.get("blind") is not False or report.get("is_baseline") is not False:
        raise IntegrityError("remediation report must be non-blind/non-baseline")
    if report.get("raw_target_answers_in_report") is not False:
        raise IntegrityError("remediation report claims to contain raw answers")
    if report.get("sealed_expectations_in_report") is not False:
        raise IntegrityError("remediation report claims to contain sealed expectations")
    _assert_report_redacted(report)
    report_hash = v2_integrity.sha256_file(report_path)
    completed = state[-1]["payload"]
    started = state[-2]["payload"]
    expected_bindings = (
        (report.get("run_id"), manifest.get("run_id"), "report run ID"),
        (report.get("suite_id"), manifest.get("suite_id"), "report suite ID"),
        (
            report.get("remediation_manifest_sha256"),
            v2_integrity.sha256_file(run_dir / REMEDIATION_MANIFEST_FILE),
            "report manifest",
        ),
        (completed.get("run_id"), manifest.get("run_id"), "completed run ID"),
        (completed.get("report_sha256"), report_hash, "completed report"),
        (completed.get("case_count"), report.get("case_count"), "completed case count"),
        (completed.get("passed"), report.get("passed"), "completed passed"),
        (completed.get("failed"), report.get("failed"), "completed failed"),
        (
            started.get("source_snapshot_before_sha256"),
            manifest.get("source_snapshot_sha256"),
            "started source snapshot",
        ),
        (
            completed.get("source_snapshot_after_sha256"),
            manifest.get("source_snapshot_sha256"),
            "completed source snapshot",
        ),
        (
            started.get("environment_binding_before_sha256"),
            manifest.get("environment", {}).get("environment_binding_sha256"),
            "started environment",
        ),
        (
            completed.get("environment_binding_after_sha256"),
            manifest.get("environment", {}).get("environment_binding_sha256"),
            "completed environment",
        ),
    )
    for actual, expected, label in expected_bindings:
        if actual != expected:
            raise IntegrityError(f"remediation verification binding mismatch: {label}")
    results = report.get("results")
    case_count = report.get("case_count")
    passed = report.get("passed")
    failed = report.get("failed")
    if not isinstance(results, list) or len(results) != case_count:
        raise IntegrityError("remediation report result count mismatch")
    if any(not isinstance(result, dict) or set(result) != RESULT_KEYS for result in results):
        raise IntegrityError("remediation report result fields mismatch")
    if not isinstance(passed, int) or not isinstance(failed, int):
        raise IntegrityError("remediation report totals are invalid")
    if passed + failed != case_count:
        raise IntegrityError("remediation report totals do not conserve case count")
    if report.get("non_blind_remediation_accuracy") != f"{passed}/{case_count}":
        raise IntegrityError("remediation accuracy label mismatch")
    source = verify_completed_source(source_paths, expected_anchors)
    if source.snapshot_sha256 != manifest.get("source_snapshot_sha256"):
        raise IntegrityError("protected source differs at remediation verification")
    if anchor.get("fixed_source_anchors") != source.snapshot:
        raise IntegrityError("source-anchor artifact differs at verification")
    verification = {
        "schema_version": "1.0",
        "run_kind": "non_blind_remediation",
        "run_id": manifest["run_id"],
        "verified_at_utc": v2_integrity.utc_now(),
        "report_sha256": report_hash,
        "remediation_manifest_sha256": v2_integrity.sha256_file(
            run_dir / REMEDIATION_MANIFEST_FILE
        ),
        "source_anchor_sha256": v2_integrity.sha256_file(run_dir / SOURCE_ANCHOR_FILE),
        "source_snapshot_sha256": source.snapshot_sha256,
        "state_head_before_verification_sha256": state[-1]["entry_hash"],
        "source_suite_modified": False,
        "run_directory_file_allowlist_verified": True,
        "raw_target_answer_fields_verified_absent_from_run_artifacts": True,
        "sealed_expectations_verified_absent_from_run_artifacts": True,
    }
    verification_hash = _write_exclusive(verification_path, verification)
    verified_entry = _append_state(
        run_dir,
        event=RUN_VERIFIED,
        payload={
            "run_id": manifest["run_id"],
            "report_sha256": report_hash,
            "verification_sha256": verification_hash,
            "source_snapshot_sha256": source.snapshot_sha256,
        },
        expected_existing_events=[RUN_PREPARED, RUN_STARTED, RUN_COMPLETED],
    )
    _require_run_dir_allowlist(
        run_dir,
        {
            SOURCE_ANCHOR_FILE,
            REMEDIATION_MANIFEST_FILE,
            STATE_FILE,
            REPORT_FILE,
            VERIFICATION_FILE,
        },
    )
    _assert_run_artifacts_redacted(
        run_dir,
        {
            SOURCE_ANCHOR_FILE,
            REMEDIATION_MANIFEST_FILE,
            STATE_FILE,
            REPORT_FILE,
            VERIFICATION_FILE,
        },
    )
    return {
        "verification": str(verification_path),
        "verification_sha256": verification_hash,
        "report_sha256": report_hash,
        "state_head_sha256": verified_entry["entry_hash"],
    }


def _parse_assignment(raw: str) -> tuple[str, Path]:
    return v2_runner._parse_assignment(raw)


def _add_source_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--suite-dir", type=Path, required=True)
    parser.add_argument("--key", type=Path, required=True)
    parser.add_argument("--pre-run-manifest", type=Path, required=True)
    parser.add_argument("--first-report", type=Path, required=True)


def _add_execution_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--repo-root", type=Path, required=True)
    parser.add_argument("--approved-manifest", type=Path, required=True)
    parser.add_argument("--database", type=_parse_assignment, action="append", required=True)
    parser.add_argument(
        "--target-command-json",
        dest="target_command",
        type=v2_runner._parse_target_command,
        required=True,
    )
    parser.add_argument("--target-cwd", type=Path, required=True)
    parser.add_argument("--per-case-timeout-seconds", type=float, default=12.0)
    parser.add_argument("--ack-non-blind-remediation", action="store_true")


def _paths_from_args(args: argparse.Namespace) -> SourcePaths:
    return SourcePaths(
        suite_dir=args.suite_dir,
        key_path=args.key,
        pre_run_manifest=args.pre_run_manifest,
        first_report=args.first_report,
    )


def _databases_from_args(args: argparse.Namespace) -> dict[str, Path]:
    databases = dict(args.database)
    if len(databases) != len(args.database):
        raise IntegrityError("database family assignments contain duplicates")
    return databases


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Non-blind remediation runner for consumed safety-blind-v2 artifacts."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    prepare_parser = subparsers.add_parser("prepare")
    prepare_parser.add_argument("--run-dir", type=Path, required=True)
    _add_source_arguments(prepare_parser)
    _add_execution_arguments(prepare_parser)
    run_parser = subparsers.add_parser("run")
    run_parser.add_argument("--run-dir", type=Path, required=True)
    _add_source_arguments(run_parser)
    _add_execution_arguments(run_parser)
    verify_parser = subparsers.add_parser("verify")
    verify_parser.add_argument("--run-dir", type=Path, required=True)
    _add_source_arguments(verify_parser)
    args = parser.parse_args()
    try:
        if args.command == "prepare":
            result = prepare_run(
                run_dir=args.run_dir,
                repo_root=args.repo_root,
                source_paths=_paths_from_args(args),
                expected_anchors=CANONICAL_SOURCE_ANCHORS,
                approved_manifest=args.approved_manifest,
                databases=_databases_from_args(args),
                target_command=args.target_command,
                target_cwd=args.target_cwd,
                per_case_timeout_seconds=args.per_case_timeout_seconds,
                acknowledge_non_blind=args.ack_non_blind_remediation,
            )
        elif args.command == "run":
            result = run_remediation(
                run_dir=args.run_dir,
                repo_root=args.repo_root,
                source_paths=_paths_from_args(args),
                expected_anchors=CANONICAL_SOURCE_ANCHORS,
                approved_manifest=args.approved_manifest,
                databases=_databases_from_args(args),
                target_command=args.target_command,
                target_cwd=args.target_cwd,
                per_case_timeout_seconds=args.per_case_timeout_seconds,
                acknowledge_non_blind=args.ack_non_blind_remediation,
            )
        else:
            result = verify_remediation(
                run_dir=args.run_dir,
                source_paths=_paths_from_args(args),
                expected_anchors=CANONICAL_SOURCE_ANCHORS,
            )
        print(json.dumps(result, sort_keys=True))
        if args.command == "run" and result["failed"]:
            return 1
        return 0
    except IntegrityError as error:
        parser.exit(2, f"remediation integrity error: {error}\n")


if __name__ == "__main__":
    raise SystemExit(main())
