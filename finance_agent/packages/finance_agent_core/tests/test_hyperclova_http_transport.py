from __future__ import annotations

import http.client
import json
import urllib.error
import urllib.request
from email.message import Message
from typing import Any

import pytest

from finance_agent_core.agent.providers.hyperclova import (
    HyperClovaXConfigurationError,
    HyperClovaXStructuredRequest,
    HyperClovaXTokenUsage,
)
from finance_agent_core.agent.providers.hyperclova_http import (
    HyperClovaXHTTPTransport,
    _NoRedirectHandler,
)


class FakeHTTPResponse:
    def __init__(
        self,
        payload: bytes,
        *,
        status: int = 200,
        close_error: Exception | None = None,
        read_error: Exception | None = None,
    ) -> None:
        self.payload = payload
        self.status = status
        self.close_error = close_error
        self.read_error = read_error
        self.read_limit: int | None = None
        self.closed = False

    def read(self, limit: int) -> bytes:
        self.read_limit = limit
        if self.read_error is not None:
            raise self.read_error
        return self.payload[:limit]

    def close(self) -> None:
        self.closed = True
        if self.close_error is not None:
            raise self.close_error


class FakeOpener:
    def __init__(self, response: object) -> None:
        self.response = response
        self.request: urllib.request.Request | None = None
        self.timeout: float | None = None

    def __call__(
        self,
        request: urllib.request.Request,
        *,
        timeout: float,
    ) -> Any:
        self.request = request
        self.timeout = timeout
        if isinstance(self.response, BaseException):
            raise self.response
        return self.response


def _semantic_request(*, model: str = "HCX-007") -> HyperClovaXStructuredRequest:
    return HyperClovaXStructuredRequest(
        operation="query_plan",
        model=model,
        system_prompt="시스템 지시",
        user_prompt="사용자 질문",
        schema_name="test_schema",
        response_schema={
            "type": "object",
            "properties": {"value": {"type": "string"}},
            "required": ["value"],
        },
        max_output_tokens=321,
        timeout_seconds=7.5,
    )


def _official_response(
    *,
    service_code: str | int = "20000",
    finish_reason: str = "stop",
    content: object = '{"value":"ok"}',
    role: object = "assistant",
    usage: object | None = None,
) -> bytes:
    return json.dumps(
        {
            "status": {"code": service_code, "message": "OK"},
            "result": {
                "message": {"role": role, "content": content},
                "finishReason": finish_reason,
                "usage": usage
                or {
                    "promptTokens": 12,
                    "completionTokens": 3,
                    "totalTokens": 15,
                },
            },
        },
        ensure_ascii=False,
    ).encode("utf-8")


def _transport(opener: FakeOpener) -> HyperClovaXHTTPTransport:
    return HyperClovaXHTTPTransport(
        "test-api-key-DO-NOT-LOG",
        opener=opener,
        request_id_factory=lambda: "request-id-001",
    )


def test_http_transport_builds_fixed_official_v3_structured_post() -> None:
    response = FakeHTTPResponse(_official_response())
    opener = FakeOpener(response)

    result = _transport(opener).complete(_semantic_request(model="HCX-007/model"))

    assert opener.request is not None
    assert opener.request.full_url == (
        "https://clovastudio.stream.ntruss.com/v3/chat-completions/HCX-007%2Fmodel"
    )
    assert opener.request.get_method() == "POST"
    headers = {name.lower(): value for name, value in opener.request.header_items()}
    assert headers["authorization"] == "Bearer test-api-key-DO-NOT-LOG"
    assert headers["content-type"] == "application/json"
    assert headers["x-ncp-clovastudio-request-id"] == "request-id-001"
    assert opener.timeout == 7.5
    assert json.loads(opener.request.data or b"") == {
        "messages": [
            {"role": "system", "content": "시스템 지시"},
            {"role": "user", "content": "사용자 질문"},
        ],
        "topP": 0.8,
        "topK": 0,
        "maxCompletionTokens": 321,
        "temperature": 0.0,
        "repetitionPenalty": 1.0,
        "stop": [],
        "seed": 1,
        "thinking": {"effort": "none"},
        "responseFormat": {
            "type": "json",
            "schema": _semantic_request().response_schema,
        },
    }
    assert result.status_code == 200
    assert result.content == '{"value":"ok"}'
    assert result.request_id == "request-id-001"
    assert result.usage == HyperClovaXTokenUsage(
        input_tokens=12,
        output_tokens=3,
        total_tokens=15,
    )
    assert response.read_limit == 2_000_001
    assert response.closed is True


@pytest.mark.parametrize(
    "api_key",
    ["", " ", "key with space", "key\nheader", "키", "x" * 2049],
)
def test_http_transport_rejects_unsafe_keys_without_exposing_them(api_key: str) -> None:
    with pytest.raises(HyperClovaXConfigurationError) as caught:
        HyperClovaXHTTPTransport(api_key)

    assert str(caught.value) == (
        "CLOVA Studio API key must be 1-2048 printable ASCII characters without whitespace"
    )


def test_http_transport_repr_redacts_api_key() -> None:
    secret = "secret-api-key-DO-NOT-LOG"

    rendered = repr(HyperClovaXHTTPTransport(secret))

    assert secret not in rendered
    assert "redacted" in rendered


@pytest.mark.parametrize("status_code", [302, 401, 429, 500])
def test_http_transport_normalizes_http_errors_without_reading_body(
    status_code: int,
) -> None:
    secret = "DO_NOT_EXPOSE_HTTP_BODY"
    error = urllib.error.HTTPError(
        "https://clovastudio.stream.ntruss.com/redirect-target",
        status_code,
        secret,
        Message(),
        None,
    )
    transport = _transport(FakeOpener(error))

    result = transport.complete(_semantic_request())

    assert result.status_code == status_code
    assert result.content is None
    assert secret not in repr(result)


@pytest.mark.parametrize(
    ("reason", "error_type"),
    [
        (TimeoutError("DO_NOT_EXPOSE_TIMEOUT"), TimeoutError),
        (OSError("DO_NOT_EXPOSE_NETWORK"), ConnectionError),
    ],
)
def test_http_transport_sanitizes_url_errors(
    reason: BaseException,
    error_type: type[Exception],
) -> None:
    transport = _transport(FakeOpener(urllib.error.URLError(reason)))

    with pytest.raises(error_type) as caught:
        transport.complete(_semantic_request())

    assert "DO_NOT_EXPOSE" not in str(caught.value)


@pytest.mark.parametrize(
    "payload",
    [
        b"not-json",
        b"[]",
        _official_response(service_code="50000"),
        _official_response(finish_reason="length"),
        _official_response(content=None),
        _official_response(role="tool"),
        _official_response(
            usage={
                "promptTokens": 12,
                "completionTokens": 3,
                "totalTokens": 999,
            }
        ),
    ],
)
def test_http_transport_fails_closed_on_invalid_success_body(payload: bytes) -> None:
    result = _transport(FakeOpener(FakeHTTPResponse(payload))).complete(_semantic_request())

    assert result.status_code == 200
    assert result.content is None
    assert result.usage is None
    assert result.request_id == "request-id-001"


def test_http_transport_limits_response_to_two_megabytes() -> None:
    response = FakeHTTPResponse(b"x" * 2_000_001)

    result = _transport(FakeOpener(response)).complete(_semantic_request())

    assert response.read_limit == 2_000_001
    assert result.content is None


def test_http_transport_cleanup_error_does_not_replace_normalized_result() -> None:
    response = FakeHTTPResponse(
        _official_response(),
        close_error=RuntimeError("DO_NOT_EXPOSE_CLOSE_FAILURE"),
    )

    result = _transport(FakeOpener(response)).complete(_semantic_request())

    assert result.content == '{"value":"ok"}'
    assert response.closed is True


def test_http_transport_sanitizes_truncated_http_response() -> None:
    response = FakeHTTPResponse(
        b"",
        read_error=http.client.IncompleteRead(b"DO_NOT_EXPOSE_PARTIAL_RESPONSE"),
    )

    with pytest.raises(ConnectionError) as caught:
        _transport(FakeOpener(response)).complete(_semantic_request())

    assert "DO_NOT_EXPOSE" not in str(caught.value)
    assert response.closed is True


def test_default_redirect_handler_never_forwards_request() -> None:
    handler = _NoRedirectHandler()
    request = urllib.request.Request(
        "https://clovastudio.stream.ntruss.com/v3/chat-completions/HCX-007"
    )

    redirected = handler.redirect_request(
        request,
        None,
        302,
        "Found",
        Message(),
        "https://attacker.invalid/steal",
    )

    assert redirected is None


def test_http_transport_rejects_invalid_generated_request_id_without_leaking_it() -> None:
    secret = "invalid request id DO_NOT_EXPOSE"
    transport = HyperClovaXHTTPTransport(
        "test-api-key",
        opener=FakeOpener(FakeHTTPResponse(_official_response())),
        request_id_factory=lambda: secret,
    )

    with pytest.raises(OSError) as caught:
        transport.complete(_semantic_request())

    assert secret not in str(caught.value)
