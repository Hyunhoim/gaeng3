from __future__ import annotations

import argparse
import base64
import json
import os
import re
import subprocess
from pathlib import Path
from typing import Any

from integrity import (
    IntegrityError,
    canonical_bytes,
    load_json,
    sha256_bytes,
    sha256_file,
    utc_now,
    validate_receipt_chain,
)


EVALUATOR_RELATIVE_FILES = (
    ".gitignore",
    "README.md",
    "__init__.py",
    "evaluator.py",
    "freeze.py",
    "http_adapter.py",
    "integrity.py",
    "runner.py",
    "seal.py",
    "tests/test_evaluator.py",
)


def _records_hash(repo_root: Path, paths: list[Path]) -> tuple[str, int]:
    records = [
        {
            "path": path.relative_to(repo_root).as_posix(),
            "sha256": sha256_file(path),
        }
        for path in sorted(paths)
    ]
    return sha256_bytes(canonical_bytes(records)), len(records)


def evaluator_tree_hash(repo_root: Path, suite_dir: Path) -> tuple[str, int]:
    paths = [suite_dir / relative for relative in EVALUATOR_RELATIVE_FILES]
    missing = [path for path in paths if not path.is_file()]
    if missing:
        raise IntegrityError(f"evaluator freeze inputs are missing: {missing}")
    return _records_hash(repo_root, paths)


def runtime_tree_hash(repo_root: Path) -> tuple[str, int]:
    """Opaque post-seal measurement; callers do not parse runtime source content."""

    core_root = (
        repo_root
        / "finance_agent/packages/finance_agent_core/src/finance_agent_core"
    )
    backend_root = repo_root / "fastapi_backend/app"
    extensions = {".py", ".json", ".yaml", ".yml"}
    paths = [
        path
        for path in core_root.rglob("*")
        if path.is_file()
        and path.suffix in extensions
        and "evaluation" not in path.relative_to(core_root).parts
    ]
    paths.extend(
        path
        for path in backend_root.rglob("*.py")
        if path.is_file()
    )
    for relative in (
        "finance_agent/packages/finance_agent_core/pyproject.toml",
        "fastapi_backend/start.sh",
    ):
        path = repo_root / relative
        if path.is_file():
            paths.append(path)
    if not paths:
        raise IntegrityError("runtime freeze scope is empty")
    return _records_hash(repo_root, list(dict.fromkeys(paths)))


def _git(repo_root: Path, *arguments: str) -> bytes:
    completed = subprocess.run(
        ["git", *arguments],
        cwd=repo_root,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if completed.returncode != 0:
        raise IntegrityError(f"git metadata command failed: {' '.join(arguments)}")
    return completed.stdout


def dirty_status_fingerprint(repo_root: Path) -> tuple[str, int, bool]:
    raw = _git(repo_root, "status", "--porcelain=v1", "-z", "--untracked-files=all")
    entries = [entry for entry in raw.split(b"\0") if entry]
    filtered = [
        entry
        for entry in entries
        if not entry.endswith(
            b"finance_agent/evaluation/safety_blind_v2/pre_run_manifest.json"
        )
        and b"finance_agent/evaluation/safety_blind_v2/private/" not in entry
    ]
    normalized = b"\0".join(sorted(filtered)) + (b"\0" if filtered else b"")
    return sha256_bytes(normalized), len(filtered), bool(filtered)


def _run_evaluator_tests(repo_root: Path, command: list[str]) -> dict[str, Any]:
    completed = subprocess.run(
        command,
        cwd=repo_root,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=30,
        check=False,
    )
    combined = completed.stdout + b"\n" + completed.stderr
    match = re.search(rb"Ran ([0-9]+) tests?", combined)
    if completed.returncode != 0 or match is None:
        raise IntegrityError("evaluator-only tests did not pass during freeze")
    return {
        "command_sha256": sha256_bytes(canonical_bytes(command)),
        "output_sha256": sha256_bytes(combined),
        "returncode": completed.returncode,
        "tests_run": int(match.group(1)),
        "status": "passed",
        "imports_or_executes_target_agent": False,
    }


def create_manifest(
    *,
    repo_root: Path,
    suite_dir: Path,
    output: Path,
    test_command: list[str],
) -> dict[str, Any]:
    if output.exists():
        raise IntegrityError("refusing to overwrite a pre-run freeze manifest")
    seal = load_json(suite_dir / "seal_manifest.json")
    receipt = validate_receipt_chain(suite_dir / "private" / "chronology.jsonl")
    if [entry["event"] for entry in receipt] != ["authoring_started", "sealed"]:
        raise IntegrityError("freeze must occur after sealing and before the first run")
    evaluator_hash, evaluator_count = evaluator_tree_hash(repo_root, suite_dir)
    runtime_hash, runtime_count = runtime_tree_hash(repo_root)
    dirty_hash, dirty_count, dirty = dirty_status_fingerprint(repo_root)
    envelope = load_json(suite_dir / "expectations.aesgcm.json")
    nonce = base64.b64decode(envelope["nonce_b64"], validate=True)
    if len(nonce) != 12 or nonce == b"\x00" * 12:
        raise IntegrityError("sealed nonce failed freeze validation")
    tests = _run_evaluator_tests(repo_root, test_command)
    body = {
        "schema_version": "2.0",
        "suite_id": seal["suite_id"],
        "sealed_at_utc": seal["sealed_at_utc"],
        "frozen_at_utc": utc_now(),
        "chronology_receipt_head_sha256": receipt[-1]["entry_hash"],
        "seal_manifest_sha256": sha256_file(suite_dir / "seal_manifest.json"),
        "questions_sha256": seal["questions_sha256"],
        "sealed_expectations_envelope_sha256": seal[
            "sealed_expectations_envelope_sha256"
        ],
        "sealed_expectations_ciphertext_sha256": seal[
            "sealed_expectations_ciphertext_sha256"
        ],
        "sealed_expectations_commitment_sha256": seal[
            "sealed_expectations_commitment_sha256"
        ],
        "aes_gcm_nonce_sha256": sha256_bytes(nonce),
        "aes_gcm_nonce_count": 1,
        "aes_gcm_nonce_unique": True,
        "evaluator_source_tree_sha256": evaluator_hash,
        "evaluator_source_file_count": evaluator_count,
        "runtime_code_tree_sha256": runtime_hash,
        "runtime_code_file_count": runtime_count,
        "runtime_code_measurement": "opaque_hash_only_after_seal",
        "git_head_commit": _git(repo_root, "rev-parse", "HEAD").decode().strip(),
        "git_head_tree": _git(repo_root, "rev-parse", "HEAD^{tree}").decode().strip(),
        "dirty_tree_status_sha256": dirty_hash,
        "dirty_tree_entry_count": dirty_count,
        "workspace_dirty": dirty,
        "evaluator_only_tests": tests,
        "target_agent_imported_or_executed_during_freeze": False,
        "target_responses_observed_during_freeze": 0,
        "raw_prompts_in_manifest": False,
        "plaintext_expectations_in_manifest": False,
    }
    manifest = {
        **body,
        "freeze_binding_sha256": sha256_bytes(canonical_bytes(body)),
    }
    raw = canonical_bytes(manifest) + b"\n"
    fd = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC, 0o644)
    try:
        os.write(fd, raw)
        os.fsync(fd)
    finally:
        os.close(fd)
    return manifest


def verify_manifest(*, repo_root: Path, suite_dir: Path, path: Path) -> dict[str, Any]:
    manifest = load_json(path)
    body = {
        key: value for key, value in manifest.items() if key != "freeze_binding_sha256"
    }
    if sha256_bytes(canonical_bytes(body)) != manifest.get("freeze_binding_sha256"):
        raise IntegrityError("pre-run freeze binding mismatch")
    expected_files = {
        "seal_manifest_sha256": sha256_file(suite_dir / "seal_manifest.json"),
        "questions_sha256": sha256_file(suite_dir / "questions.jsonl"),
        "sealed_expectations_envelope_sha256": sha256_file(
            suite_dir / "expectations.aesgcm.json"
        ),
    }
    for field, actual in expected_files.items():
        if manifest.get(field) != actual:
            raise IntegrityError(f"pre-run freeze artifact mismatch: {field}")
    evaluator_hash, evaluator_count = evaluator_tree_hash(repo_root, suite_dir)
    runtime_hash, runtime_count = runtime_tree_hash(repo_root)
    dirty_hash, dirty_count, dirty = dirty_status_fingerprint(repo_root)
    checks = {
        "evaluator_source_tree_sha256": evaluator_hash,
        "evaluator_source_file_count": evaluator_count,
        "runtime_code_tree_sha256": runtime_hash,
        "runtime_code_file_count": runtime_count,
        "dirty_tree_status_sha256": dirty_hash,
        "dirty_tree_entry_count": dirty_count,
        "workspace_dirty": dirty,
        "git_head_commit": _git(repo_root, "rev-parse", "HEAD").decode().strip(),
        "git_head_tree": _git(repo_root, "rev-parse", "HEAD^{tree}").decode().strip(),
    }
    for field, actual in checks.items():
        if manifest.get(field) != actual:
            raise IntegrityError(f"pre-run implementation freeze mismatch: {field}")
    if manifest.get("evaluator_only_tests", {}).get("status") != "passed":
        raise IntegrityError("pre-run evaluator test attestation is not passing")
    return manifest


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    create = subparsers.add_parser("create")
    create.add_argument("--repo-root", type=Path, required=True)
    create.add_argument("--suite-dir", type=Path, required=True)
    create.add_argument("--output", type=Path, required=True)
    create.add_argument("--test-command-json", required=True)
    verify = subparsers.add_parser("verify")
    verify.add_argument("--repo-root", type=Path, required=True)
    verify.add_argument("--suite-dir", type=Path, required=True)
    verify.add_argument("--manifest", type=Path, required=True)
    args = parser.parse_args()
    try:
        if args.command == "create":
            test_command = json.loads(args.test_command_json)
            if not isinstance(test_command, list) or not all(
                isinstance(item, str) for item in test_command
            ):
                raise IntegrityError("test command must be a JSON string array")
            result = create_manifest(
                repo_root=args.repo_root.resolve(),
                suite_dir=args.suite_dir.resolve(),
                output=args.output.resolve(),
                test_command=test_command,
            )
        else:
            result = verify_manifest(
                repo_root=args.repo_root.resolve(),
                suite_dir=args.suite_dir.resolve(),
                path=args.manifest.resolve(),
            )
    except IntegrityError as error:
        parser.exit(2, f"freeze integrity error: {error}\n")
    print(
        json.dumps(
            {
                "suite_id": result["suite_id"],
                "freeze_binding_sha256": result["freeze_binding_sha256"],
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
