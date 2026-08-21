from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import Literal

import pytest
from fastapi.testclient import TestClient
from fastapi_backend.scripts import rollback_drill
from finance_agent_core.agent import IntentRouter, RoutedFinanceAgent, ServerQueryPlanCompiler
from finance_agent_core.agent.providers import (
    HyperClovaXCallObserver,
    HyperClovaXQueryPlanProvider,
    HyperClovaXSettings,
    HyperClovaXStructuredRequest,
)
from finance_agent_core.answering import HyperClovaXGroundedAnswerProvider
from finance_agent_core.contracts.official import OfficialAnswerResponse
from finance_agent_core.contracts.queryplan import ProductFamily, QueryPlan
from finance_agent_core.domain import DatabaseManifest
from finance_agent_core.normalization import normalize_bond_row
from finance_agent_core.observability import AuditEvent, AuditOutcome, AuditStage, MetricCounter
from finance_agent_core.storage import write_bond_database

from app.config import Settings
from app.main import create_app

type FailureMode = Literal[
    "authentication_error",
    "rate_limited",
    "response_error",
    "service_error",
    "structured_response_error",
    "success",
    "timeout",
    "transport_error",
]

_QUESTION = "매수 가능한 국내채권을 매수수익률 높은 순으로 2개 보여줘"
_HCX_SETTINGS = HyperClovaXSettings(model="HCX-007", timeout_seconds=10)
_PRIVATE_FAILURE = "PRIVATE-HCX-FAILURE-MUST-NOT-ENTER-AUDIT"


class _OfficialFakeHyperClovaXTransport:
    def __init__(
        self,
        plan: QueryPlan,
        *,
        query_mode: FailureMode = "success",
        generation_mode: FailureMode = "success",
    ) -> None:
        self.plan = plan
        self.query_mode = query_mode
        self.generation_mode = generation_mode
        self.operations: list[str] = []

    def complete(self, request: HyperClovaXStructuredRequest) -> object:
        self.operations.append(request.operation)
        mode = self.query_mode if request.operation == "query_plan" else self.generation_mode
        if mode == "timeout":
            raise TimeoutError(_PRIVATE_FAILURE)
        if mode == "transport_error":
            raise ConnectionError(_PRIVATE_FAILURE)
        if mode == "response_error":
            return {"unexpected": _PRIVATE_FAILURE}
        if mode == "structured_response_error":
            return self._response(content='{"schema_version":"1.0"}')
        if mode != "success":
            status_code = {
                "authentication_error": 401,
                "rate_limited": 429,
                "service_error": 500,
            }[mode]
            return self._response(status_code=status_code, content=None, usage=False)
        if request.operation == "query_plan":
            return self._response(content=self.plan.model_dump_json())
        if request.operation == "grounded_answer":
            return self._response(content=self._grounded_content(request))
        raise AssertionError(f"unexpected HCX operation: {request.operation}")

    def _grounded_content(self, request: HyperClovaXStructuredRequest) -> str:
        marker = "검증된 입력:\n"
        payload = json.loads(request.system_prompt.rsplit(marker, maxsplit=1)[1])
        return json.dumps(
            {
                "schema_version": "1.0",
                "lead": "검증된 조건과 데이터에 따라 결과를 정리했습니다.",
                "products": [
                    {
                        "result_ref": product["result_ref"],
                        "evidence_fields": product["required_evidence_fields"],
                        "explanation": payload["safe_explanation"],
                    }
                    for product in payload["products"]
                ],
                "acknowledged_warning_codes": payload["required_warning_codes"],
            },
            ensure_ascii=False,
        )

    def _response(
        self,
        *,
        content: str | None,
        status_code: int = 200,
        usage: bool = True,
    ) -> dict[str, object]:
        return {
            "status_code": status_code,
            "content": content,
            "request_id": f"fake-hcx-{len(self.operations):03d}",
            "usage": (
                {"input_tokens": 11, "output_tokens": 3, "total_tokens": 14} if usage else None
            ),
        }


def _make_bond_database(tmp_path: Path) -> Path:
    records = [
        normalize_bond_row(
            source_row=row,
            present_source_fields=40,
            values={
                "PD_NO": f"KRTEST{row:06d}",
                "PD_EXG_MKT": "장내",
                "PD_NM": f"테스트채권 {row}",
                "PD_ABRV_NM": f"테스트 {row}",
                "PD_PBCM": "테스트발행사",
                "STD_PD_MCLS_NM": "회사채",
                "STD_PD_SCLS_NM": "일반사채",
                "BD_KND": "일반회사채",
                "CURR_CD": "KRW",
                "ISU_BAL_AMT": 1_000_000_000,
                "ISU_DT": 20260101,
                "MAT_DT": 20280101,
                "SRFC_IRT": "3.5",
                "PD_RISK_GCD": 4,
                "PD_STD_INFO_UPDATE": 20260224,
                "BUY_YIELD": buy_yield,
                "AFTER_TAX_YIELD": "3.0",
                "BUYABLE_QUANTITY": 100,
                "REMAINING_DAYS": 9999,
                "DUR": "0.5",
                "CRD_GRD": "AA-",
            },
        )
        for row, buy_yield in ((2, "4.5"), (3, "3.5"))
    ]
    path = tmp_path / "official-hcx-bond.sqlite3"
    write_bond_database(
        path,
        records,
        DatabaseManifest(
            dataset="bond",
            registry_schema_version="1.2",
            source_file_name="synthetic_official_hcx_bond.xlsx",
            source_file_sha256="c" * 64,
            source_file_size_bytes=1234,
            source_snapshot_date=date(2026, 7, 11),
            total_rows=len(records),
            searchable_rows=len(records),
            quarantined_rows=0,
        ),
    )
    return path


def _application(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    question_id: str,
    query_mode: FailureMode = "success",
    generation_mode: FailureMode = "success",
    query_plan_enabled: bool = True,
    answer_enabled: bool = True,
):
    database = _make_bond_database(tmp_path)
    decision = IntentRouter().route(_QUESTION, question_id)
    plan = ServerQueryPlanCompiler({ProductFamily.BOND: database}).compile(decision)
    transport = _OfficialFakeHyperClovaXTransport(
        plan,
        query_mode=query_mode,
        generation_mode=generation_mode,
    )
    audit_path = tmp_path / "audit" / "events.jsonl"
    audit_path.parent.mkdir(mode=0o700)
    settings = Settings(
        FINANCE_AUDIT_MODE="jsonl",
        FINANCE_AUDIT_FILE=audit_path,
        FINANCE_AUDIT_FSYNC_EACH_EVENT=False,
        OFFICIAL_ANSWER_TIMEOUT_SECONDS=5,
    )

    def build_test_agent(
        _settings: Settings,
        *,
        release_guard=None,
        audit_sink=None,
    ) -> RoutedFinanceAgent:
        del release_guard
        assert audit_sink is not None
        observer = HyperClovaXCallObserver(audit_sink)
        return RoutedFinanceAgent(
            {ProductFamily.BOND: database},
            query_plan_provider=(
                HyperClovaXQueryPlanProvider(
                    _HCX_SETTINGS,
                    transport,
                    on_call=observer,
                )
                if query_plan_enabled
                else None
            ),
            answer_provider=(
                HyperClovaXGroundedAnswerProvider(
                    _HCX_SETTINGS,
                    transport,
                    on_call=observer,
                )
                if answer_enabled
                else None
            ),
            hclx_planning_enabled=query_plan_enabled,
            audit_sink=audit_sink,
        )

    monkeypatch.setattr("app.main.build_agent", build_test_agent)
    return create_app(settings=settings), audit_path, transport


def _events(path: Path) -> tuple[AuditEvent, ...]:
    return tuple(
        AuditEvent.model_validate_json(line)
        for line in path.read_text(encoding="utf-8").splitlines()
    )


def _assert_official_contract(response, *, question_id: str) -> dict[str, object]:
    body = OfficialAnswerResponse.model_validate(response.json())
    assert body.question_id == question_id
    assert body.question == _QUESTION
    assert set(response.json()) == {
        "question_id",
        "question",
        "retrieved_context",
        "think_trace",
        "answer",
    }
    assert all(isinstance(value, str) for value in response.json().values())
    return json.loads(body.think_trace)


@pytest.mark.parametrize(
    ("query_plan_enabled", "answer_enabled", "expected_path", "expected_operations"),
    [
        (False, False, rollback_drill._EXPECTED_PROBE_AUDIT_PATH, []),
        (
            False,
            True,
            rollback_drill._EXPECTED_HCLX_ANSWER_PROBE_AUDIT_PATH,
            ["grounded_answer"],
        ),
        (
            True,
            True,
            rollback_drill._EXPECTED_HCLX_QUERYPLAN_ANSWER_PROBE_AUDIT_PATH,
            ["query_plan", "grounded_answer"],
        ),
    ],
)
def test_official_get_success_paths_equal_the_frozen_rollback_audit_contract(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    query_plan_enabled: bool,
    answer_enabled: bool,
    expected_path: tuple[tuple[str, str, str], ...],
    expected_operations: list[str],
) -> None:
    question_id = f"Q-PATH-{int(query_plan_enabled)}-{int(answer_enabled)}"
    application, audit_path, transport = _application(
        tmp_path,
        monkeypatch,
        question_id=question_id,
        query_plan_enabled=query_plan_enabled,
        answer_enabled=answer_enabled,
    )

    with TestClient(application) as client:
        response = client.get(
            "/answer",
            params={"question_id": question_id, "question": _QUESTION},
        )

    assert response.status_code == 200
    trace = _assert_official_contract(response, question_id=question_id)
    assert trace["status"] == "success"
    assert transport.operations == expected_operations
    observed_path = tuple(
        (event.stage.value, event.outcome.value, event.reason_code) for event in _events(audit_path)
    )
    assert observed_path == expected_path


@pytest.mark.parametrize(
    ("mode", "reason", "counter"),
    [
        (
            "authentication_error",
            "planning_authentication_failed",
            MetricCounter.HCLX_AUTHENTICATION_FAILURES,
        ),
        ("rate_limited", "planning_rate_limited", MetricCounter.HCLX_RATE_LIMITS),
        ("service_error", "planning_service_failed", MetricCounter.HCLX_SERVICE_FAILURES),
        ("transport_error", "planning_transport_failed", MetricCounter.HCLX_TRANSPORT_FAILURES),
        ("response_error", "planning_response_rejected", MetricCounter.HCLX_RESPONSE_FAILURES),
        (
            "structured_response_error",
            "planning_response_rejected",
            MetricCounter.HCLX_RESPONSE_FAILURES,
        ),
    ],
)
def test_official_get_queryplan_failures_use_the_safe_server_plan(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mode: FailureMode,
    reason: str,
    counter: MetricCounter,
) -> None:
    question_id = f"Q-PLAN-{mode}"
    application, audit_path, transport = _application(
        tmp_path,
        monkeypatch,
        question_id=question_id,
        query_mode=mode,
    )

    with TestClient(application) as client:
        response = client.get(
            "/answer",
            params={"question_id": question_id, "question": _QUESTION},
        )

    assert response.status_code == 200
    trace = _assert_official_contract(response, question_id=question_id)
    assert trace["status"] == "success"
    assert trace["answer_mode"] == "llm_grounded"
    assert transport.operations == ["query_plan", "grounded_answer"]
    events = _events(audit_path)
    assert any(
        event.stage is AuditStage.HCLX
        and event.outcome is AuditOutcome.FAILED
        and event.reason_code == reason
        for event in events
    )
    counters = application.state.audit_sink.metrics.snapshot().counters
    assert counters[counter.value] == 1
    assert counters[MetricCounter.HCLX_SUCCESSES.value] == 1
    serialized = audit_path.read_text(encoding="utf-8")
    assert _PRIVATE_FAILURE not in serialized
    assert question_id not in serialized
    assert _QUESTION not in serialized


def test_official_get_queryplan_timeout_returns_504_before_oracle_and_generation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    question_id = "Q-PLAN-timeout"
    application, audit_path, transport = _application(
        tmp_path,
        monkeypatch,
        question_id=question_id,
        query_mode="timeout",
    )

    with TestClient(application) as client:
        response = client.get(
            "/answer",
            params={"question_id": question_id, "question": _QUESTION},
        )

    assert response.status_code == 504
    trace = _assert_official_contract(response, question_id=question_id)
    assert trace["control_code"] == "request_timeout"
    assert transport.operations == ["query_plan"]
    events = _events(audit_path)
    assert any(
        event.stage is AuditStage.HCLX
        and event.outcome is AuditOutcome.TIMED_OUT
        and event.reason_code == "deadline_exceeded"
        for event in events
    )
    assert not any(event.stage in {AuditStage.SQL, AuditStage.ORACLE} for event in events)
    counters = application.state.audit_sink.metrics.snapshot().counters
    assert counters[MetricCounter.HCLX_TIMEOUTS.value] == 1
    assert _PRIVATE_FAILURE not in audit_path.read_text(encoding="utf-8")


@pytest.mark.parametrize(
    ("mode", "reason", "outcome", "counter"),
    [
        (
            "authentication_error",
            "generation_authentication_failed",
            AuditOutcome.FAILED,
            MetricCounter.HCLX_AUTHENTICATION_FAILURES,
        ),
        (
            "rate_limited",
            "generation_rate_limited",
            AuditOutcome.FAILED,
            MetricCounter.HCLX_RATE_LIMITS,
        ),
        (
            "service_error",
            "generation_service_failed",
            AuditOutcome.FAILED,
            MetricCounter.HCLX_SERVICE_FAILURES,
        ),
        (
            "transport_error",
            "generation_transport_failed",
            AuditOutcome.FAILED,
            MetricCounter.HCLX_TRANSPORT_FAILURES,
        ),
        (
            "response_error",
            "generation_response_rejected",
            AuditOutcome.FAILED,
            MetricCounter.HCLX_RESPONSE_FAILURES,
        ),
        (
            "structured_response_error",
            "generation_response_rejected",
            AuditOutcome.FAILED,
            MetricCounter.HCLX_RESPONSE_FAILURES,
        ),
        ("timeout", "generation_timed_out", AuditOutcome.TIMED_OUT, MetricCounter.HCLX_TIMEOUTS),
    ],
)
def test_official_get_generation_failures_return_verified_deterministic_fallback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mode: FailureMode,
    reason: str,
    outcome: AuditOutcome,
    counter: MetricCounter,
) -> None:
    question_id = f"Q-GENERATION-{mode}"
    application, audit_path, transport = _application(
        tmp_path,
        monkeypatch,
        question_id=question_id,
        generation_mode=mode,
        query_plan_enabled=False,
    )

    with TestClient(application) as client:
        response = client.get(
            "/answer",
            params={"question_id": question_id, "question": _QUESTION},
        )

    assert response.status_code == 200
    trace = _assert_official_contract(response, question_id=question_id)
    assert trace["status"] == "success"
    assert trace["answer_mode"] == "deterministic_fallback"
    assert trace["fallback_used"] is True
    context = json.loads(response.json()["retrieved_context"])
    assert len(context["evidence"]["products"]) == 2
    assert context["citations"]
    assert transport.operations == ["grounded_answer"]
    events = _events(audit_path)
    assert any(
        event.stage is AuditStage.HCLX and event.outcome is outcome and event.reason_code == reason
        for event in events
    )
    counters = application.state.audit_sink.metrics.snapshot().counters
    assert counters[counter.value] == 1
    assert counters.get(MetricCounter.HCLX_SUCCESSES.value, 0) == 0
    serialized = audit_path.read_text(encoding="utf-8")
    assert _PRIVATE_FAILURE not in serialized
    assert question_id not in serialized
    assert _QUESTION not in serialized
