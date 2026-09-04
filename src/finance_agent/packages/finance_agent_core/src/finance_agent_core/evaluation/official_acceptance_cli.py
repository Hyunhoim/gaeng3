from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from urllib.error import URLError

from finance_agent_core.evaluation.briefing_examples import load_briefing_example_suite
from finance_agent_core.evaluation.official_acceptance import (
    OfficialAcceptanceRunner,
    sha256_file,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Run the frozen public examples and transport edges through the real "
            "unauthenticated sequential GET /answer path."
        )
    )
    parser.add_argument("--base-url", default="http://127.0.0.1:18001")
    parser.add_argument("--request-timeout-seconds", type=float, default=60.0)
    parser.add_argument("--implementation-commit", required=True)
    parser.add_argument("--runtime-image-reference")
    parser.add_argument("--source-artifact", type=Path)
    parser.add_argument("--generated-at-utc")
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("artifacts/evaluation/official-acceptance-p0-4-v1.json"),
    )
    parser.add_argument("--require-perfect", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = build_parser().parse_args(argv)
    observed_source_sha256 = None
    if arguments.source_artifact is not None:
        try:
            observed_source_sha256 = sha256_file(arguments.source_artifact)
        except OSError as error:
            print(f"Official acceptance source verification failed: {error}", file=sys.stderr)
            return 2
    try:
        report = OfficialAcceptanceRunner(
            loaded_suite=load_briefing_example_suite(),
            base_url=arguments.base_url,
            implementation_commit=arguments.implementation_commit,
            runtime_image_reference=arguments.runtime_image_reference,
            observed_source_artifact_sha256=observed_source_sha256,
            request_timeout_seconds=arguments.request_timeout_seconds,
        ).run(generated_at_utc=(arguments.generated_at_utc or datetime.now(UTC).isoformat()))
    except (OSError, RuntimeError, TypeError, ValueError, URLError) as error:
        print(f"Official acceptance failed before completion: {error}", file=sys.stderr)
        return 2
    arguments.output.parent.mkdir(parents=True, exist_ok=True)
    arguments.output.write_text(f"{report.model_dump_json(indent=2)}\n", encoding="utf-8")
    print(
        json.dumps(
            {
                "output": str(arguments.output),
                "source_artifact_status": report.source_artifact.status,
                "passed": report.summary.passed,
                "total": report.summary.total,
                "contract_passed": report.summary.contract_passed,
                "public_examples_passed": report.summary.public_examples_passed,
                "transport_edge_passed": report.summary.transport_edge_passed,
                "no_execution_passed": report.summary.no_execution_passed,
                "api_perfect": report.summary.api_perfect,
                "perfect": report.summary.perfect,
            },
            ensure_ascii=False,
        )
    )
    if arguments.require_perfect and not report.summary.perfect:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
