from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path

SCRIPT_PATH = Path(__file__).with_name("check-docs.py")
SPEC = importlib.util.spec_from_file_location("frozen_check_docs", SCRIPT_PATH)
if SPEC is None or SPEC.loader is None:
    raise RuntimeError("cannot load the frozen documentation checker")
FROZEN = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(FROZEN)

EXPECTED_HISTORICAL_BASELINE_ERRORS_SHA256 = (
    "5e92f7fc86908fc8520338956ca0e18606a476b7054a90ad7a433156691c4624"
)


def main() -> int:
    baseline_errors = FROZEN._check_baselines()
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
