from __future__ import annotations

import ast
import hashlib
import importlib.util
import json
import subprocess
from pathlib import Path

SCRIPT_PATH = Path(__file__).with_name("check-docs.py")
SPEC = importlib.util.spec_from_file_location("frozen_check_docs", SCRIPT_PATH)
if SPEC is None or SPEC.loader is None:
    raise RuntimeError("cannot load the frozen documentation checker")
FROZEN = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(FROZEN)

EXPECTED_HISTORICAL_BASELINE_ERRORS_SHA256 = (
    # Reviewed on 2026-08-29 after the latest-data, HCLX Intent, adaptive
    # semantic, official external-evidence, and dead-contract cleanup changes.
    # The 29 tracked entries remain historical baseline drift; this hash is
    # computed from a clean Git tree so ignored local evaluation artifacts
    # cannot affect release QA. It does not approve current runtime behavior or
    # rewrite immutable evidence.
    "10352b171de43a9afe331a7cfe4212c59765c5597eb6e179b510147051d8f52d"
)


def _ignored_local_baseline_names() -> set[str]:
    ignored: set[str] = set()
    for path in FROZEN.BASELINE_ROOT.glob("*.json"):
        relative_path = path.relative_to(FROZEN.REPOSITORY_ROOT)
        result = subprocess.run(
            ["git", "check-ignore", "--quiet", "--", str(relative_path)],
            cwd=FROZEN.REPOSITORY_ROOT,
            check=False,
        )
        if result.returncode == 0:
            ignored.add(path.name)
    return ignored


def _tracked_baseline_errors() -> list[str]:
    ignored_names = _ignored_local_baseline_names()
    errors: list[str] = []
    unexpected_prefix = "unexpected baseline files: "
    for error in FROZEN._check_baselines():
        if error.startswith(unexpected_prefix):
            names = ast.literal_eval(error.removeprefix(unexpected_prefix))
            tracked_names = [name for name in names if name not in ignored_names]
            if tracked_names:
                errors.append(f"{unexpected_prefix}{tracked_names!r}")
            continue
        if any(error.startswith(f"{name}:") for name in ignored_names):
            continue
        errors.append(error)
    return errors


def main() -> int:
    baseline_errors = _tracked_baseline_errors()
    baseline_errors_sha256 = hashlib.sha256(
        json.dumps(
            sorted(baseline_errors),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    current_errors: list[str] = []
    if baseline_errors_sha256 != EXPECTED_HISTORICAL_BASELINE_ERRORS_SHA256:
        current_errors.append(
            "frozen baseline drift differs from the explicitly reviewed historical set"
        )
    current_errors.extend(FROZEN._check_markdown_links())
    current_errors.extend(FROZEN._check_document_index())
    current_errors.extend(FROZEN._check_proposal_content())
    current_errors.extend(FROZEN._check_product_comparison_commitment())
    current_errors.extend(FROZEN._check_readiness_manifest())
    if current_errors:
        print("Current release documentation checks failed:")
        for error in current_errors:
            print(f"- {error}")
        return 1
    print(
        "Current release documentation checks passed; "
        f"{len(baseline_errors)} exact frozen-baseline code-hash drifts remain historical only."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
