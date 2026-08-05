from fastapi.testclient import TestClient
from finance_agent_core.contracts.backend import BackendAgentResponse

from app.config import Settings
from app.main import create_app
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
