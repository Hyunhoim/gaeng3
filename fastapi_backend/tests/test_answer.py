from importlib.resources import files
from time import sleep

import pytest
from fastapi.testclient import TestClient
from finance_agent_core.agent import AnswerAdapterResult
from finance_agent_core.contracts.backend import BackendAgentResponse, BackendStatus
from finance_agent_core.contracts.official import OfficialAnswerResponse

from app.config import Settings
from app.main import create_app
from app.routes import answer as answer_routes
from tests.conftest import FakeAgentService


def test_answer_returns_adapter_dto_for_database_free_control_result(
    client: TestClient,
    fake_agent: FakeAgentService,
) -> None:
    payload = {
        "schema_version": "1.0",
        "request_id": "http-control-001",
        "question": "안전한 상품을 추천해 주세요.",
        "locale": "ko-KR",
    }

    response = client.post("/answer", json=payload)

    assert response.status_code == 200
    body = BackendAgentResponse.model_validate(response.json())
    assert body.request_id == payload["request_id"]
    assert body.status.value == "clarification"
    assert body.answer_mode.value == "control"
    assert fake_agent.calls == [(payload["question"], payload["request_id"])]


def test_answer_preserves_adapter_error_http_status_and_safe_dto() -> None:
    secret = "DO_NOT_LEAK_INTERNAL_EXCEPTION"
    fake_agent = FakeAgentService(error=RuntimeError(secret))
    application = create_app(settings=Settings(), agent=fake_agent)

    with TestClient(application) as client:
        response = client.post(
            "/answer",
            json={
                "request_id": "http-error-001",
                "question": "해외 ETF를 알려 주세요.",
            },
        )

    assert response.status_code == 500
    body = BackendAgentResponse.model_validate(response.json())
    assert body.status.value == "error"
    assert body.error is not None
    assert body.error.code.value == "internal_error"
    assert secret not in response.text


def test_answer_rejects_invalid_input_before_calling_agent(
    client: TestClient,
    fake_agent: FakeAgentService,
) -> None:
    response = client.post(
        "/answer",
        json={
            "request_id": "http-invalid-001",
            "question": "   ",
            "unexpected": "not allowed",
        },
    )

    assert response.status_code == 422
    body = BackendAgentResponse.model_validate(response.json())
    assert body.request_id == "http-invalid-001"
    assert body.status.value == "error"
    assert body.intent.value == "unsupported"
    assert body.error is not None
    assert body.error.code.value == "invalid_request"
    assert body.error.retryable is False
    assert "detail" not in response.json()
    assert fake_agent.calls == []


def test_answer_uses_safe_request_id_for_malformed_identifier(
    client: TestClient,
    fake_agent: FakeAgentService,
) -> None:
    response = client.post(
        "/answer",
        json={
            "request_id": "   ",
            "question": "해외 ETF를 알려 주세요.",
        },
    )

    assert response.status_code == 422
    body = BackendAgentResponse.model_validate(response.json())
    assert body.request_id == "invalid-request"
    assert body.error is not None
    assert body.error.code.value == "invalid_request"
    assert fake_agent.calls == []


def test_answer_openapi_documents_validation_error_dto(
    fake_agent: FakeAgentService,
) -> None:
    application = create_app(settings=Settings(), agent=fake_agent)

    response_schema = application.openapi()["paths"]["/answer"]["post"]["responses"]["422"]

    assert response_schema["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/BackendAgentResponse"
    }


def test_official_get_answer_returns_exact_five_string_contract(
    client: TestClient,
    fake_agent: FakeAgentService,
) -> None:
    question = "안전한 채권을 추천해 주세요."

    response = client.get(
        "/answer",
        params={"question_id": "Q-001", "question": question},
    )

    assert response.status_code == 200
    body = OfficialAnswerResponse.model_validate(response.json())
    assert body.question_id == "Q-001"
    assert body.question == question
    assert all(isinstance(value, str) for value in response.json().values())
    assert fake_agent.calls == [(question, "Q-001")]


def test_official_get_answer_converts_internal_exception_to_safe_http_200() -> None:
    secret = "DO_NOT_LEAK_OFFICIAL_EXCEPTION"
    application = create_app(
        settings=Settings(),
        agent=FakeAgentService(error=RuntimeError(secret)),
    )

    with TestClient(application) as client:
        response = client.get(
            "/answer",
            params={"question_id": "Q-ERR", "question": "해외 ETF를 알려줘"},
        )

    assert response.status_code == 200
    body = OfficialAnswerResponse.model_validate(response.json())
    assert secret not in response.text
    assert "내부 오류" in body.answer


def test_official_get_answer_handles_invalid_and_extra_parameters_without_agent_call(
    client: TestClient,
    fake_agent: FakeAgentService,
) -> None:
    invalid = client.get(
        "/answer",
        params={"question_id": " ", "question": " ", "unexpected": "ignored"},
    )

    assert invalid.status_code == 200
    body = OfficialAnswerResponse.model_validate(invalid.json())
    assert body.question_id == "invalid-question-id"
    assert fake_agent.calls == []

    valid = client.get(
        "/answer",
        params={
            "question_id": "Q-EXTRA",
            "question": "안전한 상품을 추천해 주세요.",
            "unexpected": "ignored",
        },
    )
    assert valid.status_code == 200
    OfficialAnswerResponse.model_validate(valid.json())
    assert len(fake_agent.calls) == 1


def _backend_example(name: str) -> BackendAgentResponse:
    resource = files("finance_agent_core.contracts.examples").joinpath(name)
    return BackendAgentResponse.model_validate_json(resource.read_bytes())


def _backend_status_response(status: BackendStatus) -> tuple[int, BackendAgentResponse]:
    if status is BackendStatus.SUCCESS:
        return 200, _backend_example("backend_aggregate_response_v1.json")
    if status is BackendStatus.CLARIFICATION:
        return 200, _backend_example("backend_clarification_response_v1.json")
    if status is BackendStatus.ERROR:
        return 503, _backend_example("backend_error_response_v1.json")
    payload = _backend_example("backend_clarification_response_v1.json").model_dump(mode="json")
    payload.update(
        status=status.value,
        intent="unsupported" if status is BackendStatus.UNSUPPORTED else "search",
        answer=(
            "현재 데이터로 지원하지 않는 질문입니다."
            if status is BackendStatus.UNSUPPORTED
            else "조건에 맞는 상품을 찾지 못했습니다."
        ),
        candidate_count=0 if status is BackendStatus.NOT_FOUND else None,
        clarification=None,
    )
    return 200, BackendAgentResponse.model_validate(payload)


@pytest.mark.parametrize("status", list(BackendStatus))
def test_official_get_answer_normalizes_every_internal_status_to_http_200(
    status: BackendStatus,
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    internal_http_status, internal_response = _backend_status_response(status)
    adapter = AnswerAdapterResult(
        http_status_code=internal_http_status,
        response=internal_response,
    )
    monkeypatch.setattr(
        answer_routes,
        "execute_answer_request",
        lambda _service, _request: adapter,
    )

    response = client.get(
        "/answer",
        params={"question_id": f"Q-{status.value}", "question": "평가 질문"},
    )

    assert response.status_code == 200
    body = OfficialAnswerResponse.model_validate(response.json())
    assert body.question_id == f"Q-{status.value}"
    assert set(response.json()) == {
        "question_id",
        "question",
        "retrieved_context",
        "think_trace",
        "answer",
    }


def test_official_get_answer_returns_safe_http_200_before_outer_budget() -> None:
    slow_agent = FakeAgentService()
    original_answer = slow_agent.answer

    def slow_answer(question: str, request_id: str):
        sleep(0.05)
        return original_answer(question, request_id)

    slow_agent.answer = slow_answer  # type: ignore[method-assign]
    application = create_app(
        settings=Settings(official_answer_timeout_seconds=0.01),
        agent=slow_agent,
    )

    with TestClient(application) as client:
        response = client.get(
            "/answer",
            params={"question_id": "Q-TIMEOUT", "question": "오래 걸리는 평가 질문"},
        )

    assert response.status_code == 200
    body = OfficialAnswerResponse.model_validate(response.json())
    assert "시간이 초과" in body.answer
    assert "request_timeout" in body.think_trace


@pytest.mark.parametrize("timeout_seconds", [0, 60])
def test_settings_rejects_official_budget_outside_safe_range(
    timeout_seconds: float,
) -> None:
    with pytest.raises(ValueError, match="official_answer_timeout_seconds"):
        Settings(official_answer_timeout_seconds=timeout_seconds)
