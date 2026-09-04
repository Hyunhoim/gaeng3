from __future__ import annotations

import hashlib
from collections import Counter
from pathlib import Path

import pytest
from pydantic import ValidationError

from finance_agent_core.agent import RoutedFinanceAgent, execute_answer_request
from finance_agent_core.contracts.backend import BackendAgentRequest
from finance_agent_core.contracts.queryplan import ProductFamily
from finance_agent_core.evaluation.red_team_e2e import (
    ProviderTelemetry,
    RedTeamAttackClass,
    RedTeamSuite,
    _evaluate_case,
    load_internal_red_team_suite,
    verify_red_team_databases,
)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_internal_red_team_suite_is_balanced_and_explicitly_not_blind() -> None:
    loaded = load_internal_red_team_suite()

    assert loaded.suite.suite_id == "internal-red-team-v1"
    assert loaded.suite.status == "internal_red_team_not_blind"
    assert len(loaded.suite.cases) == 40
    assert Counter(case.coverage_family for case in loaded.suite.cases) == Counter(
        {family: 10 for family in ProductFamily}
    )
    assert Counter(case.attack_class for case in loaded.suite.cases) == Counter(
        {attack: 4 for attack in RedTeamAttackClass}
    )


def test_internal_red_team_suite_rejects_duplicate_question() -> None:
    payload = load_internal_red_team_suite().suite.model_dump(mode="json")
    payload["cases"][1]["question"] = payload["cases"][0]["question"]

    with pytest.raises(ValidationError, match="questions must be unique"):
        RedTeamSuite.model_validate(payload)


def test_red_team_database_verification_detects_tampering(tmp_path: Path) -> None:
    payload = load_internal_red_team_suite().suite.model_dump(mode="json")
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
    suite = RedTeamSuite.model_validate(payload)

    assert verify_red_team_databases(suite, paths) == {
        family.value: _sha256(path) for family, path in paths.items()
    }
    paths[ProductFamily.BOND].write_bytes(b"tampered")
    with pytest.raises(ValueError, match="bond database SHA-256 differs"):
        verify_red_team_databases(suite, paths)


def test_control_case_runs_through_backend_adapter_without_evidence() -> None:
    case = load_internal_red_team_suite().suite.cases[5]
    service = RoutedFinanceAgent({})
    request = BackendAgentRequest(request_id=case.id, question=case.question)

    result = _evaluate_case(
        case,
        execute_answer_request(service, request),
        latency_ms=1.0,
    )

    assert result.passed
    assert result.safety_passed
    assert result.evidence_passed
    assert result.actual_backend_status.value == "clarification"


def test_provider_telemetry_separates_query_and_answer_errors() -> None:
    telemetry = ProviderTelemetry()
    telemetry.record_query_plan("plan-001", 12.5, error=False, plan=None)
    telemetry.record_query_plan("plan-002", 7.5, error=True, plan=None)
    telemetry.record_answer(20.0, error=True)

    snapshot = telemetry.snapshot()

    assert snapshot.query_plan_calls == 2
    assert snapshot.query_plan_errors == 1
    assert snapshot.query_plan_latency_ms == 20.0
    assert snapshot.answer_calls == 1
    assert snapshot.answer_errors == 1
    assert snapshot.answer_latency_ms == 20.0
