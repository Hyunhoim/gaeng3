import json
from importlib.resources import files
from threading import Event
from time import sleep

import pytest
from fastapi.testclient import TestClient
from finance_agent_core.agent import AnswerAdapterResult
from finance_agent_core.contracts.backend import BackendAgentResponse, BackendStatus
from finance_agent_core.contracts.official import OfficialAnswerResponse
from finance_agent_core.deadline import current_request_deadline
from finance_agent_core.storage import DatasetApprovalError

from app.config import Settings
from app.main import create_app
from app.request_execution import request_execution_stats
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
    assert response.headers["content-type"] == "application/json; charset=utf-8"
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


def test_router_exception_is_normalized_for_post_and_official_get() -> None:
    secret = "DO_NOT_LEAK_ROUTER_EXCEPTION"
    fake_agent = FakeAgentService()

    class ExplodingRouter:
        def route(self, question: str, request_id: str):
            raise RuntimeError(secret)

    fake_agent.router = ExplodingRouter()  # type: ignore[assignment]
    application = create_app(settings=Settings(), agent=fake_agent)

    with TestClient(application) as client:
        post = client.post(
            "/answer",
            json={"request_id": "ROUTER-POST", "question": "해외 ETF 조회"},
        )
        official = client.get(
            "/answer",
            params={"question_id": "ROUTER-GET", "question": "해외 ETF 조회"},
        )

    assert post.status_code == 500
    post_body = BackendAgentResponse.model_validate(post.json())
    assert post_body.status is BackendStatus.ERROR
    assert post_body.intent.value == "unsupported"
    assert post_body.product_families == []
    assert secret not in post.text

    assert official.status_code == 200
    official_body = OfficialAnswerResponse.model_validate(official.json())
    assert "내부 오류" in official_body.answer
    assert secret not in official.text
    assert fake_agent.calls == []


def test_request_time_approval_failure_is_safe_for_post_and_official_get() -> None:
    secret = "DO_NOT_LEAK_APPROVAL_FAILURE"
    application = create_app(
        settings=Settings(),
        agent=FakeAgentService(error=DatasetApprovalError(secret)),
    )

    with TestClient(application) as client:
        post = client.post(
            "/answer",
            json={"request_id": "APPROVAL-POST", "question": "국내채권 조회"},
        )
        official = client.get(
            "/answer",
            params={"question_id": "APPROVAL-GET", "question": "국내채권 조회"},
        )

    assert post.status_code == 503
    post_body = BackendAgentResponse.model_validate(post.json())
    assert post_body.status is BackendStatus.ERROR
    assert post_body.error is not None
    assert post_body.error.code.value == "dataset_unavailable"
    assert secret not in post.text

    assert official.status_code == 503
    official_body = OfficialAnswerResponse.model_validate(official.json())
    assert "데이터에 접근할 수 없습니다" in official_body.answer
    assert secret not in official.text


def test_official_get_answer_handles_invalid_and_extra_parameters_without_agent_call(
    client: TestClient,
    fake_agent: FakeAgentService,
) -> None:
    invalid = client.get(
        "/answer",
        params={"question_id": " ", "question": " ", "unexpected": "ignored"},
    )

    assert invalid.status_code == 200
    assert invalid.headers["content-type"] == "application/json; charset=utf-8"
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
    assert valid.headers["content-type"] == "application/json; charset=utf-8"
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
def test_official_get_answer_returns_5xx_only_for_retryable_internal_error(
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

    assert response.status_code == (503 if status is BackendStatus.ERROR else 200)
    body = OfficialAnswerResponse.model_validate(response.json())
    assert body.question_id == f"Q-{status.value}"
    assert set(response.json()) == {
        "question_id",
        "question",
        "retrieved_context",
        "think_trace",
        "answer",
    }


def test_official_get_answer_returns_504_before_outer_budget() -> None:
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

    assert response.status_code == 504
    body = OfficialAnswerResponse.model_validate(response.json())
    assert "시간이 초과" in body.answer
    assert "request_timeout" in body.think_trace


def test_official_get_answer_normalizes_inner_timeout_control_code(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    timed_out = AnswerAdapterResult(
        http_status_code=504,
        response=_backend_example("backend_error_response_v1.json"),
    )
    monkeypatch.setattr(
        answer_routes,
        "execute_answer_request",
        lambda _service, _request: timed_out,
    )

    response = client.get(
        "/answer",
        params={"question_id": "Q-INNER-TIMEOUT", "question": "해외 ETF 조회"},
    )

    assert response.status_code == 504
    body = OfficialAnswerResponse.model_validate(response.json())
    assert json.loads(body.think_trace)["control_code"] == "request_timeout"
    assert "시간이 초과" in body.answer


def _wait_for_active_requests(expected: int, timeout_seconds: float = 1.0) -> None:
    deadline = timeout_seconds
    while deadline > 0:
        if request_execution_stats().active == expected:
            return
        sleep(0.001)
        deadline -= 0.001
    assert request_execution_stats().active == expected


def test_get_and_post_reject_overload_without_starting_another_agent_call() -> None:
    _wait_for_active_requests(0)
    release = Event()
    started = Event()
    blocking_agent = FakeAgentService()
    original_answer = blocking_agent.answer

    def blocking_answer(question: str, request_id: str):
        started.set()
        if not release.wait(1):
            raise AssertionError("test did not release blocking Agent")
        return original_answer(question, request_id)

    blocking_agent.answer = blocking_answer  # type: ignore[method-assign]
    application = create_app(
        settings=Settings(
            official_answer_max_inflight=1,
            official_answer_timeout_seconds=0.01,
        ),
        agent=blocking_agent,
    )

    with TestClient(application) as client:
        timed_out = client.get(
            "/answer",
            params={"question_id": "Q-BLOCKING", "question": "오래 걸리는 질문"},
        )
        assert started.is_set()
        assert timed_out.status_code == 504
        assert (
            "request_timeout" in OfficialAnswerResponse.model_validate(timed_out.json()).think_trace
        )

        overloaded_get = client.get(
            "/answer",
            params={"question_id": "Q-OVERLOAD", "question": "두 번째 질문"},
        )
        assert overloaded_get.status_code == 503
        official_body = OfficialAnswerResponse.model_validate(overloaded_get.json())
        assert set(overloaded_get.json()) == {
            "question_id",
            "question",
            "retrieved_context",
            "think_trace",
            "answer",
        }
        assert json.loads(official_body.think_trace)["control_code"] == "request_overloaded"

        overloaded_post = client.post(
            "/answer",
            json={"request_id": "POST-OVERLOAD", "question": "세 번째 질문"},
        )
        assert overloaded_post.status_code == 503
        internal_body = BackendAgentResponse.model_validate(overloaded_post.json())
        assert internal_body.status is BackendStatus.ERROR
        assert internal_body.error is not None
        assert internal_body.error.retryable is True

        assert blocking_agent.calls == []
        assert request_execution_stats().active == 1
        release.set()

    _wait_for_active_requests(0)
    assert blocking_agent.calls == [("오래 걸리는 질문", "Q-BLOCKING")]


def test_timeout_signals_worker_deadline_and_releases_capacity_after_cleanup() -> None:
    _wait_for_active_requests(0)
    cleaned = Event()
    deadline_seen = Event()
    timeout_agent = FakeAgentService()
    original_answer = timeout_agent.answer

    def cancellation_aware_answer(question: str, request_id: str):
        deadline = current_request_deadline()
        assert deadline is not None
        deadline_seen.set()
        assert deadline.cancel_event.wait(1)
        try:
            return original_answer(question, request_id)
        finally:
            cleaned.set()

    timeout_agent.answer = cancellation_aware_answer  # type: ignore[method-assign]
    application = create_app(
        settings=Settings(
            official_answer_max_inflight=1,
            official_answer_timeout_seconds=0.01,
        ),
        agent=timeout_agent,
    )

    with TestClient(application) as client:
        response = client.get(
            "/answer",
            params={"question_id": "Q-CANCEL", "question": "취소 신호 테스트"},
        )

    assert response.status_code == 504
    assert deadline_seen.is_set()
    assert cleaned.wait(1)
    _wait_for_active_requests(0)
    assert timeout_agent.calls == [("취소 신호 테스트", "Q-CANCEL")]


def test_post_outer_timeout_returns_504_safe_backend_dto() -> None:
    _wait_for_active_requests(0)
    cleaned = Event()
    timeout_agent = FakeAgentService()
    original_answer = timeout_agent.answer

    def cancellation_aware_answer(question: str, request_id: str):
        deadline = current_request_deadline()
        assert deadline is not None
        assert deadline.cancel_event.wait(1)
        try:
            return original_answer(question, request_id)
        finally:
            cleaned.set()

    timeout_agent.answer = cancellation_aware_answer  # type: ignore[method-assign]
    application = create_app(
        settings=Settings(
            official_answer_max_inflight=1,
            official_answer_timeout_seconds=0.01,
        ),
        agent=timeout_agent,
    )

    with TestClient(application) as client:
        response = client.post(
            "/answer",
            json={"request_id": "POST-TIMEOUT", "question": "오래 걸리는 질문"},
        )

    assert response.status_code == 504
    body = BackendAgentResponse.model_validate(response.json())
    assert body.status is BackendStatus.ERROR
    assert body.products == []
    assert body.comparisons == []
    assert body.aggregates == []
    assert body.documents == []
    assert body.citations == []
    assert body.error is not None and body.error.retryable is True
    assert "시간이 초과" in body.answer
    assert cleaned.wait(1)
    _wait_for_active_requests(0)


@pytest.mark.parametrize("timeout_seconds", [0, 300])
def test_settings_rejects_official_budget_outside_safe_range(
    timeout_seconds: float,
) -> None:
    with pytest.raises(ValueError, match="official_answer_timeout_seconds"):
        Settings(official_answer_timeout_seconds=timeout_seconds)


@pytest.mark.parametrize("max_inflight", [0, 9])
def test_settings_rejects_official_inflight_outside_process_bound(
    max_inflight: int,
) -> None:
    with pytest.raises(ValueError, match="official_answer_max_inflight"):
        Settings(official_answer_max_inflight=max_inflight)


def test_default_request_admission_uses_benchmarked_safe_limit() -> None:
    assert Settings().official_answer_max_inflight == 2


def test_default_official_budget_leaves_margin_below_evaluator_timeout() -> None:
    assert Settings().official_answer_timeout_seconds == 270


def test_official_get_replays_safe_result_without_second_agent_execution(
    client: TestClient,
    fake_agent: FakeAgentService,
) -> None:
    params = {"question_id": "Q-IDEMPOTENT", "question": "안전한 상품을 추천해 주세요."}

    first = client.get("/answer", params=params)
    second = client.get("/answer", params=params)

    assert first.status_code == second.status_code == 200
    assert first.json() == second.json()
    assert fake_agent.calls == [(params["question"], params["question_id"])]


def test_official_get_rejects_request_id_reuse_with_different_question(
    client: TestClient,
    fake_agent: FakeAgentService,
) -> None:
    first = client.get(
        "/answer",
        params={"question_id": "Q-ID-CONFLICT", "question": "첫 번째 질문"},
    )
    conflict = client.get(
        "/answer",
        params={"question_id": "Q-ID-CONFLICT", "question": "다른 질문"},
    )

    assert first.status_code == 200
    assert conflict.status_code == 200
    body = OfficialAnswerResponse.model_validate(conflict.json())
    assert json.loads(body.think_trace)["control_code"] == "invalid_request"
    assert fake_agent.calls == [("첫 번째 질문", "Q-ID-CONFLICT")]


def test_official_get_does_not_replay_retryable_failure() -> None:
    agent = FakeAgentService(error=DatasetApprovalError("PRIVATE_TRANSIENT_FAILURE"))
    application = create_app(settings=Settings(), agent=agent)
    params = {"question_id": "Q-TRANSIENT", "question": "국내채권 조회"}

    with TestClient(application) as client:
        first = client.get("/answer", params=params)
        second = client.get("/answer", params=params)

    assert first.status_code == second.status_code == 503
    assert agent.calls == [
        (params["question"], params["question_id"]),
        (params["question"], params["question_id"]),
    ]
