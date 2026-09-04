from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import os
from pathlib import Path
from typing import Any

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from integrity import (
    IntegrityError,
    append_receipt,
    canonical_bytes,
    load_json,
    sha256_bytes,
    sha256_file,
    utc_now,
    validate_receipt_chain,
)


SUITE_ID = "finance-agent-safety-blind-v2-192"
MINIMUM_CASES = 160


def _write_exclusive(path: Path, raw: bytes, mode: int) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC, mode)
    try:
        os.write(fd, raw)
        os.fsync(fd)
    finally:
        os.close(fd)


def _validate_authoring(payload: Any) -> list[dict[str, Any]]:
    if not isinstance(payload, dict) or payload.get("schema_version") != "2.0-authoring":
        raise IntegrityError("authoring payload has the wrong schema")
    cases = payload.get("cases")
    if not isinstance(cases, list) or len(cases) < MINIMUM_CASES:
        raise IntegrityError(f"at least {MINIMUM_CASES} authored cases are required")
    case_ids: set[str] = set()
    request_ids: set[str] = set()
    questions: set[str] = set()
    canaries: set[str] = set()
    for index, case in enumerate(cases, start=1):
        if not isinstance(case, dict) or set(case) != {
            "case_id",
            "request_id",
            "question",
            "locale",
            "expectation",
            "secret_canary",
        }:
            raise IntegrityError(f"authoring case {index} has unexpected keys")
        if case["locale"] != "ko-KR":
            raise IntegrityError(f"case {case['case_id']} uses an unsupported locale")
        if not isinstance(case["question"], str) or not case["question"].strip():
            raise IntegrityError(f"case {case['case_id']} has a blank question")
        if not isinstance(case["expectation"], dict):
            raise IntegrityError(f"case {case['case_id']} has no expectation object")
        if not isinstance(case["secret_canary"], str) or not case[
            "secret_canary"
        ].startswith("SBV2-SECRET-"):
            raise IntegrityError(f"case {case['case_id']} has an invalid secret canary")
        for value, seen, label in (
            (case["case_id"], case_ids, "case ID"),
            (case["request_id"], request_ids, "request ID"),
            (case["question"], questions, "question"),
            (case["secret_canary"], canaries, "secret canary"),
        ):
            if value in seen:
                raise IntegrityError(f"duplicate {label}: {value}")
            seen.add(value)
    return cases


def _question_bytes(cases: list[dict[str, Any]]) -> bytes:
    lines = []
    for case in cases:
        public = {
            "case_id": case["case_id"],
            "request_id": case["request_id"],
            "question": case["question"],
            "locale": case["locale"],
        }
        lines.append(canonical_bytes(public))
    return b"\n".join(lines) + b"\n"


def initialize_chronology(private_dir: Path) -> dict[str, Any]:
    return append_receipt(
        private_dir,
        event="authoring_started",
        payload={
            "suite_id": SUITE_ID,
            "runtime_target_executions_observed": 0,
            "declaration": (
                "Agent-authored independent runtime-behavior-blind evaluation; "
                "not human-domain blind and not double-blind."
            ),
        },
        create=True,
    )


def seal_suite(
    *,
    suite_dir: Path,
    authoring_path: Path,
    source_specs: list[tuple[Path, str, str]],
    consume_authoring: bool,
) -> dict[str, Any]:
    private_dir = suite_dir / "private"
    entries = validate_receipt_chain(private_dir / "chronology.jsonl")
    if [entry["event"] for entry in entries] != ["authoring_started"]:
        raise IntegrityError("suite can be sealed only once, before any run event")
    cases = _validate_authoring(load_json(authoring_path))
    questions_raw = _question_bytes(cases)
    questions_sha = sha256_bytes(questions_raw)
    sealed_at = utc_now()
    consultations = []
    for path, source_class, purpose in source_specs:
        consultations.append(
            {
                "path": str(path),
                "source_class": source_class,
                "purpose": purpose,
                "sha256": sha256_file(path),
            }
        )
    consultations.sort(key=lambda item: item["path"])

    expectations = {
        "schema_version": "2.0",
        "suite_id": SUITE_ID,
        "sealed_at_utc": sealed_at,
        "cases": [
            {
                "case_id": case["case_id"],
                "request_id": case["request_id"],
                "secret_canary": case["secret_canary"],
                "expectation": case["expectation"],
            }
            for case in cases
        ],
    }
    plaintext = canonical_bytes(expectations)
    key = AESGCM.generate_key(bit_length=256)
    nonce = os.urandom(12)
    aad_object = {
        "schema_version": "2.0-aad",
        "suite_id": SUITE_ID,
        "sealed_at_utc": sealed_at,
        "questions_sha256": questions_sha,
        "expectation_count": len(cases),
        "deployment_profile": "default_locked",
        "author_declaration": {
            "author": "OpenAI Codex Agent",
            "independence": "independent_runtime_behavior_blind",
            "human_domain_blind": False,
            "double_blind": False,
            "target_executions_before_seal": 0,
            "target_responses_observed_before_seal": 0,
        },
        "allowed_source_consultations": consultations,
    }
    aad = canonical_bytes(aad_object)
    ciphertext = AESGCM(key).encrypt(nonce, plaintext, aad)
    envelope = {
        "schema_version": "2.0",
        "suite_id": SUITE_ID,
        "algorithm": "AES-256-GCM",
        "nonce_b64": base64.b64encode(nonce).decode("ascii"),
        "aad_b64": base64.b64encode(aad).decode("ascii"),
        "ciphertext_and_tag_b64": base64.b64encode(ciphertext).decode("ascii"),
        "ciphertext_sha256": sha256_bytes(ciphertext),
        "plaintext_commitment_sha256": sha256_bytes(plaintext),
        "plaintext_hmac_sha256": hmac.new(key, plaintext, hashlib.sha256).hexdigest(),
    }
    envelope_raw = canonical_bytes(envelope) + b"\n"
    manifest_body = {
        "schema_version": "2.0",
        "suite_id": SUITE_ID,
        "sealed_at_utc": sealed_at,
        "case_count": len(cases),
        "minimum_case_count": MINIMUM_CASES,
        "questions_file": "questions.jsonl",
        "questions_sha256": questions_sha,
        "sealed_expectations_file": "expectations.aesgcm.json",
        "sealed_expectations_envelope_sha256": sha256_bytes(envelope_raw),
        "sealed_expectations_ciphertext_sha256": envelope["ciphertext_sha256"],
        "sealed_expectations_commitment_sha256": envelope[
            "plaintext_commitment_sha256"
        ],
        "key_fingerprint_sha256": sha256_bytes(key),
        "encryption": {
            "algorithm": "AES-256-GCM",
            "nonce_bytes": 12,
            "key_bytes": 32,
            "aad_bound": True,
        },
        "deployment_profile": "default_locked",
        "author_declaration": aad_object["author_declaration"],
        "allowed_source_consultations": consultations,
        "product_content_consulted_before_seal": False,
        "exact_positive_product_ids_selected_before_seal": False,
        "runtime_target_executions_before_seal": 0,
        "target_responses_observed_before_seal": 0,
        "hard_per_case_process_timeout_required": True,
        "single_use_first_run_required": True,
    }
    manifest = {
        **manifest_body,
        "manifest_hmac_sha256": hmac.new(
            key, canonical_bytes(manifest_body), hashlib.sha256
        ).hexdigest(),
    }
    manifest_raw = canonical_bytes(manifest) + b"\n"

    output_paths = [
        suite_dir / "questions.jsonl",
        suite_dir / "expectations.aesgcm.json",
        suite_dir / "seal_manifest.json",
        private_dir / "seal.key",
    ]
    if any(path.exists() for path in output_paths):
        raise IntegrityError("refusing to overwrite an existing sealed-suite artifact")
    _write_exclusive(suite_dir / "questions.jsonl", questions_raw, 0o644)
    _write_exclusive(
        suite_dir / "expectations.aesgcm.json", envelope_raw, 0o644
    )
    _write_exclusive(suite_dir / "seal_manifest.json", manifest_raw, 0o644)
    _write_exclusive(private_dir / "seal.key", key, 0o600)
    sealed_entry = append_receipt(
        private_dir,
        event="sealed",
        payload={
            "suite_id": SUITE_ID,
            "sealed_at_utc": sealed_at,
            "case_count": len(cases),
            "questions_sha256": questions_sha,
            "expectations_envelope_sha256": sha256_bytes(envelope_raw),
            "expectations_ciphertext_sha256": envelope["ciphertext_sha256"],
            "expectations_commitment_sha256": envelope[
                "plaintext_commitment_sha256"
            ],
            "seal_manifest_sha256": sha256_bytes(manifest_raw),
            "key_fingerprint_sha256": sha256_bytes(key),
            "runtime_target_executions_observed": 0,
        },
    )
    if consume_authoring:
        authoring_path.unlink()
    return {"manifest": manifest, "receipt_entry": sealed_entry}


def _parse_source_spec(raw: str) -> tuple[Path, str, str]:
    parts = raw.split("::", 2)
    if len(parts) != 3 or not all(parts):
        raise argparse.ArgumentTypeError("source must be PATH::CLASS::PURPOSE")
    return Path(parts[0]).resolve(), parts[1], parts[2]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    init_parser = subparsers.add_parser("init-chronology")
    init_parser.add_argument("--suite-dir", type=Path, required=True)
    seal_parser = subparsers.add_parser("seal")
    seal_parser.add_argument("--suite-dir", type=Path, required=True)
    seal_parser.add_argument("--authoring", type=Path, required=True)
    seal_parser.add_argument(
        "--source", type=_parse_source_spec, action="append", required=True
    )
    seal_parser.add_argument("--consume-authoring", action="store_true")
    args = parser.parse_args()
    if args.command == "init-chronology":
        entry = initialize_chronology(args.suite_dir.resolve() / "private")
        print(canonical_bytes(entry).decode("utf-8"))
        return 0
    result = seal_suite(
        suite_dir=args.suite_dir.resolve(),
        authoring_path=args.authoring.resolve(),
        source_specs=args.source,
        consume_authoring=args.consume_authoring,
    )
    print(canonical_bytes(result).decode("utf-8"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
