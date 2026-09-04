from __future__ import annotations

from typing import Any


class MissingExpectationPath(KeyError):
    """Raised when an expectation path is absent from an audit report."""


def get_path(value: Any, path: str) -> Any:
    current = value
    for part in path.split("."):
        if not isinstance(current, dict) or part not in current:
            raise MissingExpectationPath(path)
        current = current[part]
    return current


def verify_expectations(report: dict[str, Any], expectations: dict[str, Any]) -> dict[str, Any]:
    results: list[dict[str, Any]] = []
    for check in expectations.get("checks", []):
        path = check["path"]
        expected = check["expected"]
        try:
            actual = get_path(report, path)
            passed = actual == expected
            error = None
        except MissingExpectationPath:
            actual = None
            passed = False
            error = "missing_path"
        results.append(
            {
                "path": path,
                "expected": expected,
                "actual": actual,
                "passed": passed,
                "error": error,
                "reason": check.get("reason"),
            }
        )
    passed_count = sum(result["passed"] for result in results)
    return {
        "passed": passed_count == len(results),
        "passed_count": passed_count,
        "total_count": len(results),
        "results": results,
    }
