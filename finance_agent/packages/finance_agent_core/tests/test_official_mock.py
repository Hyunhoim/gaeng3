from __future__ import annotations

import hashlib
from collections import Counter
from pathlib import Path

import pytest
from pydantic import ValidationError

from finance_agent_core.agent import RoutedFinanceAgent, execute_answer_request
from finance_agent_core.contracts.backend import BackendAgentRequest
from finance_agent_core.contracts.queryplan import ProductFamily
from finance_agent_core.evaluation.briefing_examples import (
    BriefingAnswerability,
    BriefingDifficulty,
)
from finance_agent_core.evaluation.official_mock import (
    OfficialMockSuite,
    evaluate_official_mock_case,
    load_official_mock_suite,
    verify_official_mock_databases,
)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_official_mock_suite_preserves_announced_shape_without_blind_claim() -> None:
    loaded = load_official_mock_suite()

    assert loaded.suite.suite_id == "official-mock-v1-30"
    assert loaded.suite.status == "public_official_shape_mock_not_blind"
    assert loaded.suite.is_blind is False
    assert len(loaded.suite.cases) == 30
    assert Counter(case.difficulty for case in loaded.suite.cases) == Counter(
        {
            BriefingDifficulty.LOW: 10,
            BriefingDifficulty.MEDIUM: 10,
            BriefingDifficulty.HIGH: 10,
        }
    )
    assert Counter(case.answerability for case in loaded.suite.cases) == Counter(
        {
            BriefingAnswerability.ANSWERABLE: 25,
            BriefingAnswerability.UNANSWERABLE: 5,
        }
    )


def test_official_mock_answerable_cases_cover_all_families() -> None:
    suite = load_official_mock_suite().suite

    assert Counter(
        case.coverage_family
        for case in suite.cases
        if case.answerability is BriefingAnswerability.ANSWERABLE
    ) == Counter(
        {
            ProductFamily.OVERSEAS_ETP: 7,
            ProductFamily.DOMESTIC_ETP: 6,
            ProductFamily.BOND: 6,
            ProductFamily.FUND: 6,
        }
    )
    assert any(len(case.expectation.product_families) > 1 for case in suite.cases)


def test_official_mock_suite_rejects_duplicate_question() -> None:
    payload = load_official_mock_suite().suite.model_dump(mode="json")
    payload["cases"][1]["question"] = payload["cases"][0]["question"]

    with pytest.raises(ValidationError, match="questions must be unique"):
        OfficialMockSuite.model_validate(payload)


def test_official_mock_suite_rejects_answerability_status_mismatch() -> None:
    payload = load_official_mock_suite().suite.model_dump(mode="json")
    payload["cases"][0]["answerability"] = "unanswerable"

    with pytest.raises(ValidationError, match="safe control status"):
        OfficialMockSuite.model_validate(payload)


def test_official_mock_database_verification_detects_tampering(tmp_path: Path) -> None:
    payload = load_official_mock_suite().suite.model_dump(mode="json")
    paths: dict[ProductFamily, Path] = {}
    for family in ProductFamily:
        path = tmp_path / f"{family.value}.sqlite3"
        path.write_bytes(f"database:{family.value}".encode())
        manifest = path.with_suffix(f"{path.suffix}.manifest.json")
        manifest.write_bytes(f"manifest:{family.value}".encode())
        paths[family] = path
        payload["data"][family.value] = {
            "database_sha256": _sha256(path),
            "manifest_sha256": _sha256(manifest),
        }
    suite = OfficialMockSuite.model_validate(payload)

    assert verify_official_mock_databases(suite, paths) == {
        family.value: _sha256(path) for family, path in paths.items()
    }
    paths[ProductFamily.FUND].write_bytes(b"tampered")
    with pytest.raises(ValueError, match="fund database SHA-256 differs"):
        verify_official_mock_databases(suite, paths)


def test_unanswerable_case_passes_internal_and_five_string_contract() -> None:
    case = load_official_mock_suite().suite.cases[9]
    service = RoutedFinanceAgent({})
    adapter = execute_answer_request(
        service,
        BackendAgentRequest(request_id=case.id, question=case.question),
    )

    result = evaluate_official_mock_case(case, adapter, latency_ms=1.0)

    assert result.passed
    assert result.system.actual_backend_status.value == "clarification"
    assert result.system.actual_candidate_count is None
    assert result.official_contract_passed
    assert all(result.official_checks.values())
