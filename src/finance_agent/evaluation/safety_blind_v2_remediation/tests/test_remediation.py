from __future__ import annotations

import base64
import hashlib
import hmac
import importlib
import json
import os
import sqlite3
import stat
import sys
import tempfile
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from typing import Any

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

REMEDIATION_DIR = Path(__file__).resolve().parents[1]
REPO_ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(REMEDIATION_DIR))

remediation = importlib.import_module("remediation")
REPORT_FILE = remediation.REPORT_FILE
CANONICAL_SUITE_ID = remediation.CANONICAL_SUITE_ID
RUN_PREPARED = remediation.RUN_PREPARED
RUN_FAILED = remediation.RUN_FAILED
RUN_STARTED = remediation.RUN_STARTED
IntegrityError = remediation.IntegrityError
SourceAnchors = remediation.SourceAnchors
SourcePaths = remediation.SourcePaths
_append_state = remediation._append_state
_target_execution_profile = remediation._target_execution_profile
prepare_run = remediation.prepare_run
run_remediation = remediation.run_remediation
v2_integrity = remediation.v2_integrity
validate_state_chain = remediation.validate_state_chain
verify_completed_source = remediation.verify_completed_source
verify_remediation = remediation.verify_remediation


def _write(path: Path, raw: bytes, mode: int) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
    try:
        os.write(fd, raw)
        os.fchmod(fd, mode)
    finally:
        os.close(fd)


def _write_json(path: Path, value: dict[str, Any], mode: int) -> str:
    raw = v2_integrity.canonical_bytes(value) + b"\n"
    _write(path, raw, mode)
    return v2_integrity.sha256_bytes(raw)


def _make_database(path: Path, family: str, table: str) -> None:
    connection = sqlite3.connect(path)
    try:
        if family == "fund":
            connection.execute(
                f'CREATE TABLE "{table}" '
                "(product_id TEXT NOT NULL, public_offering INTEGER NOT NULL, "
                "is_quarantined INTEGER NOT NULL)"
            )
            connection.execute(
                f'INSERT INTO "{table}" VALUES (?, ?, ?)',
                (f"{family}-1", 1, 0),
            )
        else:
            connection.execute(
                f'CREATE TABLE "{table}" '
                "(product_id TEXT NOT NULL, is_quarantined INTEGER NOT NULL)"
            )
            connection.execute(
                f'INSERT INTO "{table}" VALUES (?, ?)',
                (f"{family}-1", 0),
            )
        connection.commit()
    finally:
        connection.close()


def _valid_target_source() -> str:
    return """\
import json
import sys

request = json.loads(sys.argv[1])
response = {
    "schema_version": "1.0",
    "request_id": request["request_id"],
    "status": "clarification_required",
    "intent": "unknown",
    "product_families": [],
    "answer": "synthetic control response",
    "query_plan": None,
    "candidate_count": None,
    "products": [],
    "comparisons": [],
    "aggregates": [],
    "documents": [],
    "citations": [],
    "as_of_dates": [],
    "warnings": [],
    "answer_mode": "control",
    "fallback_used": False,
    "provider_model": None,
    "clarification": {
        "code": "ambiguous_request",
        "message": "family required",
        "required_fields": ["product_family"],
        "options": [],
    },
    "error": None,
    "source_manifest": None,
    "family_searches": [],
    "source_manifests": [],
}
sys.stdout.write(json.dumps(response, separators=(",", ":")))
"""


def _sleep_target_source() -> str:
    return """\
import time
time.sleep(5)
"""


def _self_removing_target_source() -> str:
    return """\
import os
os.unlink(__file__)
print("{}")
"""


def _create_completed_source(root: Path) -> dict[str, Any]:
    suite_dir = root / "source-suite"
    suite_dir.mkdir()
    private_dir = suite_dir / "private"
    v2_integrity.append_receipt(
        private_dir,
        event="authoring_started",
        payload={
            "suite_id": "synthetic-safety-v2",
            "runtime_target_executions_observed": 0,
        },
        create=True,
        expected_existing_events=[],
    )
    key = bytes(range(32))
    key_path = private_dir / "seal.key"
    _write(key_path, key, 0o600)

    database_dir = root / "databases"
    database_dir.mkdir()
    table_by_family = {
        "overseas_etp": "overseas_etp_products",
        "domestic_etp": "domestic_etp_products",
        "bond": "bond_products",
        "fund": "fund_products",
    }
    databases: dict[str, Path] = {}
    dataset_entries: dict[str, dict[str, Any]] = {}
    for family, table in table_by_family.items():
        path = database_dir / f"{family}.sqlite3"
        _make_database(path, family, table)
        databases[family] = path
        dataset_entries[family] = {
            "database_sha256": v2_integrity.sha256_file(path),
            "searchable_rows": 1,
        }
    approved_manifest = root / "approved.json"
    approved_hash = _write_json(
        approved_manifest,
        {
            "schema_version": "1.0",
            "status": "official_competition_data_approved",
            "datasets": dataset_entries,
        },
        0o600,
    )

    question = {
        "case_id": "SYN-001",
        "request_id": "syn-request-001",
        "question": "상품군이 불명확한 합성 질문",
        "locale": "ko-KR",
    }
    questions_raw = v2_integrity.canonical_bytes(question) + b"\n"
    questions_hash = v2_integrity.sha256_bytes(questions_raw)
    _write(suite_dir / "questions.jsonl", questions_raw, 0o644)
    sealed_at = "2026-08-10T00:00:00.000000Z"
    author_declaration = {
        "author": "synthetic-test",
        "independence": "test-only",
        "human_domain_blind": False,
        "double_blind": False,
        "target_executions_before_seal": 0,
        "target_responses_observed_before_seal": 0,
    }
    expectation = {
        "schema_version": "2.0",
        "suite_id": "synthetic-safety-v2",
        "sealed_at_utc": sealed_at,
        "cases": [
            {
                "case_id": question["case_id"],
                "request_id": question["request_id"],
                "secret_canary": "SBV2-SECRET-SYNTHETIC-NEVER-PERSIST",
                "expectation": {
                    "status": "clarification_required",
                    "intent": "unknown",
                    "product_families": [],
                    "zero_execution": True,
                    "clarification": {
                        "code": "ambiguous_request",
                        "required_fields": ["product_family"],
                    },
                },
            }
        ],
    }
    plaintext = v2_integrity.canonical_bytes(expectation)
    aad_object = {
        "schema_version": "2.0-aad",
        "suite_id": "synthetic-safety-v2",
        "sealed_at_utc": sealed_at,
        "questions_sha256": questions_hash,
        "expectation_count": 1,
        "deployment_profile": "synthetic",
        "author_declaration": author_declaration,
    }
    aad = v2_integrity.canonical_bytes(aad_object)
    nonce = bytes(range(12))
    ciphertext = AESGCM(key).encrypt(nonce, plaintext, aad)
    envelope = {
        "schema_version": "2.0",
        "suite_id": "synthetic-safety-v2",
        "algorithm": "AES-256-GCM",
        "nonce_b64": base64.b64encode(nonce).decode("ascii"),
        "aad_b64": base64.b64encode(aad).decode("ascii"),
        "ciphertext_and_tag_b64": base64.b64encode(ciphertext).decode("ascii"),
        "ciphertext_sha256": v2_integrity.sha256_bytes(ciphertext),
        "plaintext_commitment_sha256": v2_integrity.sha256_bytes(plaintext),
        "plaintext_hmac_sha256": hmac.new(key, plaintext, hashlib.sha256).hexdigest(),
    }
    envelope_hash = _write_json(suite_dir / "expectations.aesgcm.json", envelope, 0o644)
    manifest_body = {
        "schema_version": "2.0",
        "suite_id": "synthetic-safety-v2",
        "sealed_at_utc": sealed_at,
        "case_count": 1,
        "questions_sha256": questions_hash,
        "sealed_expectations_envelope_sha256": envelope_hash,
        "sealed_expectations_ciphertext_sha256": envelope["ciphertext_sha256"],
        "sealed_expectations_commitment_sha256": envelope["plaintext_commitment_sha256"],
        "key_fingerprint_sha256": v2_integrity.sha256_bytes(key),
        "author_declaration": author_declaration,
        "allowed_source_consultations": [
            {
                "path": "synthetic-approved.json",
                "source_class": "approved_dataset_manifest",
                "purpose": "synthetic test",
                "sha256": approved_hash,
            }
        ],
    }
    manifest = {
        **manifest_body,
        "manifest_hmac_sha256": hmac.new(
            key, v2_integrity.canonical_bytes(manifest_body), hashlib.sha256
        ).hexdigest(),
    }
    manifest_hash = _write_json(suite_dir / "seal_manifest.json", manifest, 0o644)
    sealed_entry = v2_integrity.append_receipt(
        private_dir,
        event="sealed",
        payload={
            "suite_id": manifest["suite_id"],
            "sealed_at_utc": sealed_at,
            "case_count": 1,
            "questions_sha256": questions_hash,
            "expectations_envelope_sha256": envelope_hash,
            "expectations_ciphertext_sha256": envelope["ciphertext_sha256"],
            "expectations_commitment_sha256": envelope["plaintext_commitment_sha256"],
            "seal_manifest_sha256": manifest_hash,
            "key_fingerprint_sha256": v2_integrity.sha256_bytes(key),
            "runtime_target_executions_observed": 0,
        },
        expected_existing_events=["authoring_started"],
    )
    pre_run_manifest = suite_dir / "pre_run_manifest.json"
    pre_run = {
        "schema_version": "2.0",
        "suite_id": manifest["suite_id"],
        "questions_sha256": questions_hash,
        "seal_manifest_sha256": manifest_hash,
        "sealed_expectations_envelope_sha256": envelope_hash,
        "sealed_expectations_ciphertext_sha256": envelope["ciphertext_sha256"],
        "sealed_expectations_commitment_sha256": envelope["plaintext_commitment_sha256"],
        "chronology_receipt_head_sha256": sealed_entry["entry_hash"],
        "freeze_binding_sha256": "f" * 64,
    }
    pre_run_hash = _write_json(pre_run_manifest, pre_run, 0o644)
    first_report = root / "first-report.json"
    first_run_id = "00000000-0000-0000-0000-000000000001"
    v2_integrity.append_receipt(
        private_dir,
        event="run_started",
        payload={
            "suite_id": manifest["suite_id"],
            "run_id": first_run_id,
            "seal_manifest_sha256": manifest_hash,
            "pre_run_manifest_sha256": pre_run_hash,
            "freeze_binding_sha256": pre_run["freeze_binding_sha256"],
            "target_command_sha256": "a" * 64,
            "per_case_timeout_seconds": 1.0,
            "case_count": 1,
            "report_path_sha256": v2_integrity.sha256_bytes(str(first_report).encode("utf-8")),
        },
        expected_existing_events=["authoring_started", "sealed"],
    )
    first_report_object = {
        "schema_version": "2.0",
        "suite_id": manifest["suite_id"],
        "run_id": first_run_id,
        "seal_manifest_sha256": manifest_hash,
        "pre_run_manifest_sha256": pre_run_hash,
        "freeze_binding_sha256": pre_run["freeze_binding_sha256"],
        "questions_sha256": questions_hash,
        "expectations_commitment_sha256": envelope["plaintext_commitment_sha256"],
        "target_command_sha256": "a" * 64,
        "per_case_timeout_seconds": 1.0,
        "case_count": 1,
        "passed": 1,
        "failed": 0,
        "strict_accuracy": "1/1",
        "raw_target_answers_in_report": False,
        "sealed_expectations_in_report": False,
        "results": [
            {
                "case_id": question["case_id"],
                "passed": True,
                "failure_codes": [],
                "elapsed_ms": 1,
                "returncode": 0,
                "timed_out": False,
                "stdout_sha256": "b" * 64,
                "stderr_sha256": "c" * 64,
                "response_sha256": "d" * 64,
            }
        ],
    }
    first_report_hash = _write_json(first_report, first_report_object, 0o600)
    v2_integrity.append_receipt(
        private_dir,
        event="run_completed",
        payload={
            "suite_id": manifest["suite_id"],
            "run_id": first_run_id,
            "report_sha256": first_report_hash,
            "case_count": 1,
            "passed": 1,
            "failed": 0,
        },
        expected_existing_events=["authoring_started", "sealed", "run_started"],
    )
    receipt = v2_integrity.validate_receipt_chain(private_dir / "chronology.jsonl")
    anchors = SourceAnchors(
        questions_sha256=questions_hash,
        sealed_expectations_envelope_sha256=envelope_hash,
        seal_manifest_sha256=manifest_hash,
        pre_run_manifest_sha256=pre_run_hash,
        chronology_sha256=v2_integrity.sha256_file(private_dir / "chronology.jsonl"),
        chronology_head_sha256=receipt[-1]["entry_hash"],
        seal_key_fingerprint_sha256=v2_integrity.sha256_bytes(key),
        first_report_sha256=first_report_hash,
    )
    target = root / "target.py"
    target.write_text(_valid_target_source(), encoding="utf-8")
    return {
        "paths": SourcePaths(
            suite_dir=suite_dir,
            key_path=key_path,
            pre_run_manifest=pre_run_manifest,
            first_report=first_report,
        ),
        "anchors": anchors,
        "approved_manifest": approved_manifest,
        "databases": databases,
        "target": target,
        "key": key,
        "canary": expectation["cases"][0]["secret_canary"],
        "raw_answer": "synthetic control response",
    }


def _source_hashes(bundle: dict[str, Any]) -> dict[str, str]:
    paths: SourcePaths = bundle["paths"]
    files = {
        "questions": paths.suite_dir / "questions.jsonl",
        "envelope": paths.suite_dir / "expectations.aesgcm.json",
        "manifest": paths.suite_dir / "seal_manifest.json",
        "pre_run": paths.pre_run_manifest,
        "key": paths.key_path,
        "chronology": paths.chronology,
        "first_report": paths.first_report,
    }
    return {name: v2_integrity.sha256_file(path) for name, path in files.items()}


class RemediationLifecycleTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.bundle = _create_completed_source(self.root)
        self.command = [
            sys.executable,
            str(self.bundle["target"]),
            "{request_json}",
        ]

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _prepare(self, run_dir: Path, *, timeout: float = 1.0) -> dict[str, Any]:
        return prepare_run(
            run_dir=run_dir,
            repo_root=REPO_ROOT,
            source_paths=self.bundle["paths"],
            expected_anchors=self.bundle["anchors"],
            approved_manifest=self.bundle["approved_manifest"],
            databases=self.bundle["databases"],
            target_command=self.command,
            target_cwd=self.root,
            per_case_timeout_seconds=timeout,
            acknowledge_non_blind=True,
        )

    def _run(self, run_dir: Path, *, timeout: float = 1.0) -> dict[str, Any]:
        return run_remediation(
            run_dir=run_dir,
            repo_root=REPO_ROOT,
            source_paths=self.bundle["paths"],
            expected_anchors=self.bundle["anchors"],
            approved_manifest=self.bundle["approved_manifest"],
            databases=self.bundle["databases"],
            target_command=self.command,
            target_cwd=self.root,
            per_case_timeout_seconds=timeout,
            acknowledge_non_blind=True,
        )

    def test_full_lifecycle_is_redacted_single_use_and_source_immutable(self) -> None:
        before = _source_hashes(self.bundle)
        run_dir = self.root / "runs" / "run-1"
        run_dir.parent.mkdir()
        prepared = self._prepare(run_dir)
        self.assertEqual(stat.S_IMODE(run_dir.stat().st_mode), 0o700)
        with self.assertRaises(IntegrityError):
            self._prepare(run_dir)
        result = self._run(run_dir)
        self.assertEqual(result["non_blind_remediation_accuracy"], "1/1")
        with self.assertRaises(IntegrityError):
            self._run(run_dir)
        verified = verify_remediation(
            run_dir=run_dir,
            source_paths=self.bundle["paths"],
            expected_anchors=self.bundle["anchors"],
        )
        self.assertEqual(verified["report_sha256"], result["report_sha256"])
        with self.assertRaises(IntegrityError):
            verify_remediation(
                run_dir=run_dir,
                source_paths=self.bundle["paths"],
                expected_anchors=self.bundle["anchors"],
            )
        entries = validate_state_chain(run_dir / "state.jsonl")
        self.assertEqual(
            [entry["event"] for entry in entries],
            [
                "remediation_prepared",
                "remediation_run_started",
                "remediation_run_completed",
                "remediation_verified",
            ],
        )
        report_path = run_dir / REPORT_FILE
        self.assertEqual(stat.S_IMODE(report_path.stat().st_mode), 0o600)
        report_raw = report_path.read_bytes()
        self.assertNotIn(self.bundle["canary"].encode(), report_raw)
        self.assertNotIn(self.bundle["raw_answer"].encode(), report_raw)
        for artifact in run_dir.iterdir():
            if artifact.is_file():
                self.assertEqual(stat.S_IMODE(artifact.stat().st_mode), 0o600)
                artifact_raw = artifact.read_bytes()
                self.assertNotIn(self.bundle["canary"].encode(), artifact_raw)
                self.assertNotIn(self.bundle["raw_answer"].encode(), artifact_raw)
                self.assertNotIn(self.bundle["key"], artifact_raw)
        report = json.loads(report_raw)
        self.assertFalse(report["blind"])
        self.assertFalse(report["is_baseline"])
        self.assertFalse(report["raw_target_answers_in_report"])
        self.assertFalse(report["sealed_expectations_in_report"])
        self.assertEqual(before, _source_hashes(self.bundle))
        self.assertEqual(prepared["run_id"], report["run_id"])

    def test_fixed_anchor_mismatch_fails_before_creating_run_directory(self) -> None:
        run_dir = self.root / "bad-anchor-run"
        bad = replace(self.bundle["anchors"], chronology_sha256="0" * 64)
        with self.assertRaisesRegex(IntegrityError, "fixed source anchor mismatch"):
            prepare_run(
                run_dir=run_dir,
                repo_root=REPO_ROOT,
                source_paths=self.bundle["paths"],
                expected_anchors=bad,
                approved_manifest=self.bundle["approved_manifest"],
                databases=self.bundle["databases"],
                target_command=self.command,
                target_cwd=self.root,
                per_case_timeout_seconds=1.0,
                acknowledge_non_blind=True,
            )
        self.assertFalse(run_dir.exists())

    def test_preexisting_report_is_never_overwritten(self) -> None:
        run_dir = self.root / "preexisting-report-run"
        self._prepare(run_dir)
        report_path = run_dir / REPORT_FILE
        sentinel = b"preexisting-report-sentinel\n"
        _write(report_path, sentinel, 0o600)
        with self.assertRaisesRegex(IntegrityError, "refusing to overwrite"):
            self._run(run_dir)
        self.assertEqual(report_path.read_bytes(), sentinel)
        entries = validate_state_chain(run_dir / "state.jsonl")
        self.assertEqual([entry["event"] for entry in entries], [RUN_PREPARED])

    def test_hard_timeout_is_reported_without_raw_output(self) -> None:
        self.bundle["target"].write_text(_sleep_target_source(), encoding="utf-8")
        run_dir = self.root / "timeout-run"
        self._prepare(run_dir, timeout=0.1)
        started = time.monotonic()
        result = self._run(run_dir, timeout=0.1)
        elapsed = time.monotonic() - started
        self.assertLess(elapsed, 2.0)
        self.assertEqual(result["non_blind_remediation_accuracy"], "0/1")
        report = json.loads((run_dir / REPORT_FILE).read_text())
        self.assertEqual(report["results"][0]["failure_codes"], ["hard_process_timeout"])
        self.assertNotIn(self.bundle["canary"], (run_dir / REPORT_FILE).read_text())

    def test_failure_state_records_source_and_environment_after_audit(self) -> None:
        self.bundle["target"].write_text(_self_removing_target_source(), encoding="utf-8")
        run_dir = self.root / "failure-audit-run"
        self._prepare(run_dir)
        with self.assertRaisesRegex(IntegrityError, "environment changed"):
            self._run(run_dir)
        self.assertFalse((run_dir / REPORT_FILE).exists())
        state = validate_state_chain(run_dir / "state.jsonl")
        self.assertEqual(
            [entry["event"] for entry in state],
            [RUN_PREPARED, RUN_STARTED, RUN_FAILED],
        )
        failure = state[-1]["payload"]
        self.assertTrue(failure["source_verification_after_failure_passed"])
        self.assertFalse(failure["environment_verification_after_failure_passed"])
        self.assertIsNotNone(failure["source_snapshot_after_failure_sha256"])
        self.assertIsNotNone(failure["untrusted_source_measurement_after_failure_sha256"])

    def test_state_transition_lock_allows_only_one_concurrent_claim(self) -> None:
        run_dir = self.root / "concurrent-run"
        self._prepare(run_dir)

        def claim(marker: int) -> str:
            entry = _append_state(
                run_dir,
                event=RUN_STARTED,
                payload={"marker": marker},
                expected_existing_events=[RUN_PREPARED],
            )
            return entry["entry_hash"]

        successes: list[str] = []
        failures: list[BaseException] = []
        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [executor.submit(claim, marker) for marker in (1, 2)]
            for future in futures:
                try:
                    successes.append(future.result())
                except IntegrityError as error:
                    failures.append(error)
        self.assertEqual(len(successes), 1)
        self.assertEqual(len(failures), 1)
        self.assertIsInstance(failures[0], IntegrityError)
        entries = validate_state_chain(run_dir / "state.jsonl")
        self.assertEqual([entry["event"] for entry in entries], [RUN_PREPARED, RUN_STARTED])

    def test_state_hash_chain_rejects_tampering(self) -> None:
        run_dir = self.root / "tampered-state-run"
        self._prepare(run_dir)
        state_path = run_dir / "state.jsonl"
        raw = bytearray(state_path.read_bytes())
        marker = raw.index(b"remediation_prepared")
        raw[marker] = ord("x")
        state_path.write_bytes(raw)
        with self.assertRaisesRegex(IntegrityError, "state hash mismatch"):
            validate_state_chain(state_path)

    def test_completed_source_contract_is_verified_without_decrypting_artifacts(
        self,
    ) -> None:
        source = verify_completed_source(self.bundle["paths"], self.bundle["anchors"])
        self.assertEqual(
            [entry["event"] for entry in source.receipt],
            ["authoring_started", "sealed", "run_started", "run_completed"],
        )
        self.assertEqual(
            source.snapshot["first_report_sha256"],
            self.bundle["anchors"].first_report_sha256,
        )

    def test_canonical_target_is_restricted_to_frozen_isolated_http_adapter(
        self,
    ) -> None:
        adapter = remediation.V2_CODE_DIR / "http_adapter.py"
        command = [
            sys.executable,
            "-I",
            str(adapter),
            "--url",
            "http://127.0.0.1:18001/answer",
            "--request-json",
            "{request_json}",
            "--timeout-seconds",
            "10",
        ]
        self.assertEqual(
            _target_execution_profile(command, CANONICAL_SUITE_ID),
            "frozen_v2_http_adapter_loopback_python_isolated",
        )
        with self.assertRaisesRegex(IntegrityError, "frozen v2 HTTP adapter"):
            _target_execution_profile(self.command, CANONICAL_SUITE_ID)


if __name__ == "__main__":
    unittest.main()
