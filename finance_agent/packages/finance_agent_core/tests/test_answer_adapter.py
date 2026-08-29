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
)
from finance_agent_core.contracts.backend import (
    BackendAgentRequest,
    BackendAgentResponse,
    BackendErrorCode,
)
from finance_agent_core.contracts.queryplan import ProductFamily
from finance_agent_core.contracts.routing import InteractionIntent
from finance_agent_core.deadline import RequestDeadline, bind_request_deadline
from finance_agent_core.release import AgentReleaseCode, AgentReleaseError

_QUESTION = (
    "미국 채권형 해외 ETF 중 현재 거래 가능한 상품에서 "
    "총보수 0.20% 이하를 AUM 높은 순으로 5개 보여줘"
)
_SECRET = "DO_NOT_EXPOSE_PROVIDER_DETAIL"


def _request(request_id: str) -> BackendAgentRequest:
    return BackendAgentRequest(
        request_id=request_id,
        question=f"{_QUESTION} {_SECRET}",
    )


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


@pytest.mark.parametrize(
    "retired_kwargs",
    [
        {"query_plan_provider": object()},
        {"grounded_plan_provider": object()},
        {"answer_provider": object()},
        {"hclx_planning_enabled": True},
    ],
)
def test_retired_generation_seams_are_blocked(retired_kwargs: dict[str, object]) -> None:
    with pytest.raises(ValueError, match="legacy QueryPlan and grounded-answer"):
        RoutedFinanceAgent({}, **retired_kwargs)


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
