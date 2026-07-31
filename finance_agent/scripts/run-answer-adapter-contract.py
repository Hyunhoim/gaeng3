from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SUITE_PATH = (
    PROJECT_ROOT
    / "packages"
    / "finance_agent_core"
    / "src"
    / "finance_agent_core"
    / "evaluation"
    / "suites"
    / "answer_adapter_contract_12.json"
)
TEST_PATH = (
    PROJECT_ROOT
    / "packages"
    / "finance_agent_core"
    / "tests"
    / "test_answer_adapter.py"
)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class ContractResultCollector:
    def __init__(self) -> None:
        self.outcomes: dict[str, str] = {}

    def pytest_runtest_logreport(self, report: Any) -> None:
        if report.when != "call":
            return
        test_name = report.nodeid.rsplit("::", maxsplit=1)[-1]
        self.outcomes[test_name] = report.outcome


def _load_suite() -> dict[str, Any]:
    payload = json.loads(SUITE_PATH.read_text(encoding="utf-8"))
    if payload.get("schema_version") != "1.0":
        raise ValueError("unsupported answer adapter suite schema")
    cases = payload.get("cases")
    if not isinstance(cases, list) or not cases:
        raise ValueError("answer adapter suite must contain cases")
    test_names = [case.get("test_name") for case in cases]
    if any(not isinstance(name, str) or not name for name in test_names):
        raise ValueError("every answer adapter case requires a test_name")
    if len(test_names) != len(set(test_names)):
        raise ValueError("answer adapter test_name values must be unique")
    return payload


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run the framework-neutral /answer adapter contract suite."
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=PROJECT_ROOT
        / "artifacts"
        / "evaluation"
        / "answer-adapter-contract-v1.json",
    )
    parser.add_argument("--require-perfect", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = build_parser().parse_args(argv)
    suite = _load_suite()
    collector = ContractResultCollector()
    pytest_exit = int(
        pytest.main(
            ["-q", str(TEST_PATH)],
            plugins=[collector],
        )
    )
    cases = suite["cases"]
    expected_tests = {case["test_name"] for case in cases}
    observed_tests = set(collector.outcomes)
    case_outcomes = [
        {
            "id": case["id"],
            "test_name": case["test_name"],
            "outcome": collector.outcomes.get(case["test_name"], "not_run"),
        }
        for case in cases
    ]
    passed = sum(case["outcome"] == "passed" for case in case_outcomes)
    perfect = (
        pytest_exit == 0 and expected_tests == observed_tests and passed == len(cases)
    )
    report = {
        "schema_version": "1.0",
        "report_id": "answer-adapter-contract-v1",
        "suite": {
            "id": suite["suite_id"],
            "version": suite["suite_version"],
            "sha256": _sha256(SUITE_PATH),
        },
        "source_test_sha256": _sha256(TEST_PATH),
        "adapter": {
            "framework": None,
            "network_used": False,
            "raw_exception_text_returned": False,
        },
        "metrics": {
            "total": len(cases),
            "passed": passed,
            "strict_accuracy": passed / len(cases),
            "perfect": perfect,
        },
        "case_outcomes": case_outcomes,
    }
    arguments.output.parent.mkdir(parents=True, exist_ok=True)
    arguments.output.write_text(
        f"{json.dumps(report, ensure_ascii=False, indent=2)}\n",
        encoding="utf-8",
    )
    print(
        json.dumps(
            {
                "output": arguments.output.name,
                "total": len(cases),
                "passed": passed,
                "perfect": perfect,
            },
            ensure_ascii=False,
        )
    )
    if arguments.require_perfect and not perfect:
        return 1
    return pytest_exit


if __name__ == "__main__":
    raise SystemExit(main())
