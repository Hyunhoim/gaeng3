from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from urllib.error import URLError

from finance_agent_core.evaluation.official_mock import load_official_mock_suite
from finance_agent_core.evaluation.official_mock_http import OfficialMockHttpRunner


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run the frozen 30-case mock through the real Docker GET /answer path."
    )
    parser.add_argument("--base-url", default="http://127.0.0.1:18001")
    parser.add_argument(
        "--backend-profile",
        choices=("deterministic", "local_test"),
        default="deterministic",
    )
    parser.add_argument("--declared-model")
    parser.add_argument("--request-timeout-seconds", type=float, default=60.0)
    parser.add_argument("--response-budget-seconds", type=float, default=60.0)
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("artifacts/evaluation/official-mock-http-v1-30.json"),
    )
    parser.add_argument("--require-perfect", action="store_true")
    parser.add_argument("--require-no-fallback", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    arguments = parser.parse_args(argv)
    if arguments.backend_profile == "local_test" and not arguments.declared_model:
        parser.error("local_test requires --declared-model")
    if arguments.backend_profile == "deterministic" and arguments.declared_model:
        parser.error("--declared-model is not valid for deterministic profile")
    try:
        report = OfficialMockHttpRunner(
            loaded_suite=load_official_mock_suite(),
            base_url=arguments.base_url,
            backend_profile=arguments.backend_profile,
            declared_model=arguments.declared_model,
            request_timeout_seconds=arguments.request_timeout_seconds,
            response_budget_seconds=arguments.response_budget_seconds,
        ).run()
    except (OSError, RuntimeError, TypeError, ValueError, URLError) as error:
        print(f"Official mock HTTP evaluation failed before completion: {error}", file=sys.stderr)
        return 2
    arguments.output.parent.mkdir(parents=True, exist_ok=True)
    arguments.output.write_text(f"{report.model_dump_json(indent=2)}\n", encoding="utf-8")
    print(
        json.dumps(
            {
                "output": str(arguments.output),
                "backend_profile": report.backend_profile,
                "passed": report.summary.passed,
                "total": report.summary.total,
                "strict_accuracy": report.summary.strict_accuracy,
                "official_contract_pass_rate": report.summary.official_contract_pass_rate,
                "semantic_pass_rate": report.summary.semantic_pass_rate,
                "unanswerable_safety_rate": report.summary.unanswerable_safety_rate,
                "fallback_count": report.summary.fallback_count,
                "latency_ms": report.summary.latency_ms,
                "perfect": report.summary.perfect,
            },
            ensure_ascii=False,
        )
    )
    if arguments.require_perfect and not report.summary.perfect:
        return 1
    if arguments.require_no_fallback and report.summary.fallback_count:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
