from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from pydantic import ValidationError

from finance_agent_core.agent import (
    AnswerAdapterResult,
    IntentRouter,
    RoutedFinanceAgent,
    execute_answer_request,
)
from finance_agent_core.agent.providers import (
    HyperClovaXConfigurationError,
    HyperClovaXQueryPlanProvider,
    HyperClovaXSettings,
    HyperClovaXStructuredRequest,
)
from finance_agent_core.agent.providers.mock import first_vertical_slice_plan
from finance_agent_core.contracts.backend import (
    BackendAgentRequest,
    BackendAgentResponse,
    BackendAnswerMode,
    BackendErrorCode,
    BackendStatus,
)
from finance_agent_core.contracts.queryplan import ProductFamily
from finance_agent_core.contracts.routing import InteractionIntent
from finance_agent_core.deadline import RequestDeadline, bind_request_deadline
from finance_agent_core.domain import (
    DatabaseManifest,
    NormalizedOverseasEtpRecord,
)
from finance_agent_core.release import AgentReleaseCode, AgentReleaseError

_QUESTION = (
    "미국 채권형 해외 ETF 중 현재 거래 가능한 상품에서 "
    "총보수 0.20% 이하를 AUM 높은 순으로 5개 보여줘"
)
_SECRET = "DO_NOT_EXPOSE_PROVIDER_DETAIL"
_SETTINGS = HyperClovaXSettings(
    model="HCX-ANSWER-ADAPTER-TEST",
    timeout_seconds=12,
)


class ScriptedTransport:
    def __init__(self, result: object) -> None:
        self.result = result
        self.requests: list[HyperClovaXStructuredRequest] = []

    def complete(self, request: HyperClovaXStructuredRequest) -> object:
        self.requests.append(request)
        if isinstance(self.result, BaseException):
            raise self.result
        return self.result


def _transport_response(
    *,
    status_code: int,
    content: str | None,
) -> dict[str, object]:
    return {
        "status_code": status_code,
        "content": content,
        "request_id": "answer-adapter-test-001",
        "usage": {
            "input_tokens": 100,
            "output_tokens": 20,
            "total_tokens": 120,
        },
    }


def _request(request_id: str) -> BackendAgentRequest:
    return BackendAgentRequest(
        request_id=request_id,
        question=f"{_QUESTION} {_SECRET}",
    )


def _service(
    database_path: Path,
    transport: ScriptedTransport,
) -> RoutedFinanceAgent:
    return RoutedFinanceAgent(
        {"overseas_etp": database_path},
        query_plan_provider=HyperClovaXQueryPlanProvider(
            _SETTINGS,
            transport,
        ),
        hclx_planning_enabled=True,
    )


def test_answer_adapter_returns_http_200_for_verified_result(
    sample_database: tuple[
        Path,
        list[NormalizedOverseasEtpRecord],
        DatabaseManifest,
    ],
) -> None:
    path, _, _ = sample_database
    transport = ScriptedTransport(
        _transport_response(
            status_code=200,
            content=first_vertical_slice_plan("model-id").model_dump_json(),
        )
    )
    result = execute_answer_request(
        _service(path, transport),
        BackendAgentRequest(
            request_id="answer-adapter-success-001",
            question=_QUESTION,
        ),
    )

    assert result.http_status_code == 200
    assert result.response.status is BackendStatus.SUCCESS
    assert result.response.answer_mode is BackendAnswerMode.DETERMINISTIC
    assert result.response.error is None
    assert result.response.citations


@pytest.mark.parametrize(
    "transport_result",
    [
        _transport_response(status_code=401, content=_SECRET),
        _transport_response(status_code=429, content=_SECRET),
        _transport_response(status_code=500, content=_SECRET),
        ConnectionError(_SECRET),
        _transport_response(status_code=200, content="not-json"),
    ],
    ids=[
        "authentication",
        "rate_limit",
        "service",
        "transport",
        "invalid_response",
    ],
)
def test_answer_adapter_keeps_non_timeout_planning_failure_on_server_plan(
    sample_database: tuple[
        Path,
        list[NormalizedOverseasEtpRecord],
        DatabaseManifest,
    ],
    transport_result: object,
) -> None:
    path, _, _ = sample_database
    transport = ScriptedTransport(transport_result)

    result = execute_answer_request(
        _service(path, transport),
        _request("answer-adapter-provider-server-plan"),
    )
    serialized = result.model_dump_json()

    assert result.http_status_code == 200
    assert result.response.status is BackendStatus.SUCCESS
    assert result.response.error is None
    assert result.response.answer_mode is BackendAnswerMode.DETERMINISTIC
    assert not result.response.fallback_used
    assert result.response.products
    assert _SECRET not in serialized


def test_answer_adapter_maps_planning_timeout_without_leaking_details(
    sample_database: tuple[
        Path,
        list[NormalizedOverseasEtpRecord],
        DatabaseManifest,
    ],
) -> None:
    path, _, _ = sample_database
    result = execute_answer_request(
        _service(path, ScriptedTransport(TimeoutError(_SECRET))),
        _request("answer-adapter-provider-timeout"),
    )

    assert result.http_status_code == 504
    assert result.response.status is BackendStatus.ERROR
    assert result.response.error is not None
    assert result.response.error.code is BackendErrorCode.PROVIDER_UNAVAILABLE
    assert result.response.error.retryable
    assert result.response.answer_mode is BackendAnswerMode.CONTROL
    assert not result.response.fallback_used
    assert result.response.products == []
    assert _SECRET not in result.model_dump_json()


def test_answer_adapter_maps_configuration_failure_as_non_retryable() -> None:
    class ConfigurationFailureService:
        router = IntentRouter()

        def answer(self, question: str, request_id: str):
            raise HyperClovaXConfigurationError(_SECRET)

    result = execute_answer_request(
        ConfigurationFailureService(),
        _request("answer-adapter-config-001"),
    )

    assert result.http_status_code == 503
    assert result.response.error is not None
    assert result.response.error.code is BackendErrorCode.PROVIDER_UNAVAILABLE
    assert not result.response.error.retryable
    assert _SECRET not in result.model_dump_json()


def test_answer_adapter_keeps_answer_provider_failure_as_safe_http_200_fallback(
    sample_database: tuple[
        Path,
        list[NormalizedOverseasEtpRecord],
        DatabaseManifest,
    ],
) -> None:
    path, _, _ = sample_database

    class FailingAnswerProvider:
        @property
        def provider_name(self) -> str:
            return "failing_test"

        @property
        def model_name(self) -> str:
            return "failing-model"

        def generate_grounded_answer(self, context):
            raise RuntimeError(_SECRET)

    service = RoutedFinanceAgent(
        {"overseas_etp": path},
        answer_provider=FailingAnswerProvider(),
    )
    result = execute_answer_request(
        service,
        BackendAgentRequest(
            request_id="answer-adapter-fallback-001",
            question=_QUESTION,
        ),
    )

    assert result.http_status_code == 200
    assert result.response.status is BackendStatus.SUCCESS
    assert result.response.answer_mode is BackendAnswerMode.DETERMINISTIC_FALLBACK
    assert result.response.fallback_used
    assert result.response.error is None
    assert _SECRET not in result.model_dump_json()


def test_answer_adapter_maps_missing_database_to_retryable_dataset_error(
    tmp_path: Path,
) -> None:
    missing = tmp_path / "missing.sqlite3"
    service = RoutedFinanceAgent({"overseas_etp": missing})

    result = execute_answer_request(
        service,
        BackendAgentRequest(
            request_id="answer-adapter-data-001",
            question=_QUESTION,
        ),
    )

    assert result.http_status_code == 503
    assert result.response.error is not None
    assert result.response.error.code is BackendErrorCode.DATASET_UNAVAILABLE
    assert result.response.error.retryable
    assert result.response.intent is InteractionIntent.SEARCH
    assert result.response.product_families == [ProductFamily.OVERSEAS_ETP]
    assert str(missing) not in result.model_dump_json()


def test_answer_adapter_maps_stale_release_without_exposing_details() -> None:
    class StaleReleaseService:
        router = IntentRouter()

        def answer(self, question: str, request_id: str):
            raise AgentReleaseError(
                AgentReleaseCode.STALE_RELEASE,
                f"{_SECRET} /private/release/path",
            )

    result = execute_answer_request(
        StaleReleaseService(),
        BackendAgentRequest(
            request_id="answer-adapter-release-001",
            question=_QUESTION,
        ),
    )

    assert result.http_status_code == 503
    assert result.response.error is not None
    assert result.response.error.code is BackendErrorCode.INTERNAL_ERROR
    assert result.response.error.retryable
    assert _SECRET not in result.model_dump_json()
    assert "/private/release/path" not in result.model_dump_json()


def test_answer_adapter_maps_deadline_sqlite_interrupt_to_timeout() -> None:
    class InterruptedService:
        router = IntentRouter()

        def answer(self, question: str, request_id: str):
            raise sqlite3.OperationalError("interrupted")

    deadline = RequestDeadline.after(5)
    deadline.cancel()
    with bind_request_deadline(deadline):
        result = execute_answer_request(
            InterruptedService(),
            BackendAgentRequest(
                request_id="answer-adapter-deadline-sqlite-001",
                question=_QUESTION,
            ),
        )

    assert result.http_status_code == 504
    assert result.response.error is not None
    assert result.response.error.code is BackendErrorCode.PROVIDER_UNAVAILABLE
    assert result.response.error.retryable
    assert "시간이 초과" in result.response.answer


def test_answer_adapter_maps_unexpected_failure_without_exception_text() -> None:
    class UnexpectedFailureService:
        router = IntentRouter()

        def answer(self, question: str, request_id: str):
            raise RuntimeError(_SECRET)

    result = execute_answer_request(
        UnexpectedFailureService(),
        _request("answer-adapter-internal-001"),
    )

    assert result.http_status_code == 500
    assert result.response.error is not None
    assert result.response.error.code is BackendErrorCode.INTERNAL_ERROR
    assert not result.response.error.retryable
    assert _SECRET not in result.model_dump_json()


def test_backend_error_contract_rejects_executed_evidence_and_http_200() -> None:
    class UnexpectedFailureService:
        router = IntentRouter()

        def answer(self, question: str, request_id: str):
            raise RuntimeError(_SECRET)

    result = execute_answer_request(
        UnexpectedFailureService(),
        _request("answer-adapter-state-001"),
    )
    payload = result.response.model_dump(mode="json")
    payload["candidate_count"] = 0

    with pytest.raises(
        ValidationError,
        match="error response cannot contain executed or control evidence",
    ):
        BackendAgentResponse.model_validate(payload)
    with pytest.raises(
        ValidationError,
        match="HTTP status and Backend error status differ",
    ):
        AnswerAdapterResult(
            http_status_code=200,
            response=result.response,
        )
