from finance_agent_core.audit.verification import get_path, verify_expectations


def test_get_path_reads_nested_value() -> None:
    assert get_path({"datasets": {"bond": {"rows": 42}}}, "datasets.bond.rows") == 42


def test_verify_expectations_reports_mismatch_and_missing_path() -> None:
    report = {"datasets": {"bond": {"rows": 42}}}
    expectations = {
        "checks": [
            {"path": "datasets.bond.rows", "expected": 42},
            {"path": "datasets.bond.columns", "expected": 40},
            {"path": "datasets.fund.rows", "expected": 95},
        ]
    }

    verification = verify_expectations(report, expectations)

    assert verification["passed"] is False
    assert verification["passed_count"] == 1
    assert verification["total_count"] == 3
    assert verification["results"][1]["actual"] is None
    assert verification["results"][1]["error"] == "missing_path"
