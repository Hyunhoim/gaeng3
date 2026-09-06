from __future__ import annotations

import pytest

from finance_agent_core.agent.providers import (
    HyperClovaXAuthenticationError,
    HyperClovaXCallRecord,
    HyperClovaXClient,
    HyperClovaXConfigurationError,
    HyperClovaXRateLimitError,
    HyperClovaXResponseError,
    HyperClovaXServiceError,
    HyperClovaXSettings,
    HyperClovaXStructuredRequest,
    HyperClovaXTimeoutError,
    HyperClovaXTransportError,
)


class FakeHyperClovaXTransport:
    def __init__(self, *script: object) -> None:
        self.script = list(script)
        self.requests: list[HyperClovaXStructuredRequest] = []

    def complete(self, request: HyperClovaXStructuredRequest) -> object:
        self.requests.append(request)
        response = self.script.pop(0)
        if isinstance(response, BaseException):
            raise response
        return response


def _settings() -> HyperClovaXSettings:
    return HyperClovaXSettings(model="HCX-CONTRACT-TEST", timeout_seconds=12)


def _response(content: str | None, *, status_code: int = 200) -> dict[str, object]:
    return {
        "status_code": status_code,
        "content": content,
        "request_id": "fake-request-001",
        "usage": {"input_tokens": 100, "output_tokens": 20, "total_tokens": 120},
    }


def _simple_schema() -> dict[str, object]:
    return {
        "type": "object",
        "properties": {"value": {"type": "string"}},
        "required": ["value"],
    }


def _complete(client: HyperClovaXClient) -> dict[str, str]:
    return client.complete(
        operation="intent_analysis",
        system_prompt="system",
        user_prompt="question",
        schema_name="test_schema",
        response_schema=_simple_schema(),
        max_output_tokens=10,
        response_parser=lambda content: {"value": content},
    )


def test_hcx_settings_require_official_hyperclova_mode() -> None:
    with pytest.raises(HyperClovaXConfigurationError, match="official execution"):
        HyperClovaXSettings.from_environment({})
    with pytest.raises(HyperClovaXConfigurationError, match="official execution"):
        HyperClovaXSettings.from_environment(
            {
                "FINANCE_AGENT_LLM_MODE": "evaluation",
                "LLM_PROVIDER": "local_test",
                "HCX_MODEL": "HCX-CONTRACT-TEST",
            }
        )
    settings = HyperClovaXSettings.from_environment(
        {
            "FINANCE_AGENT_LLM_MODE": "evaluation",
            "LLM_PROVIDER": "hyperclova",
            "HCX_MODEL": "HCX-CONTRACT-TEST",
            "HCX_TIMEOUT_SECONDS": "45",
        }
    )
    assert settings.timeout_seconds == 45
    with pytest.raises(HyperClovaXConfigurationError, match="HCX-"):
        HyperClovaXSettings(model="qwen3-local-test")


@pytest.mark.parametrize(
    ("status_code", "error_type", "outcome"),
    [
        (401, HyperClovaXAuthenticationError, "authentication_error"),
        (403, HyperClovaXAuthenticationError, "authentication_error"),
        (429, HyperClovaXRateLimitError, "rate_limited"),
        (500, HyperClovaXServiceError, "service_error"),
    ],
)
def test_hcx_client_maps_http_status_without_exposing_body(
    status_code: int,
    error_type: type[Exception],
    outcome: str,
) -> None:
    secret = "DO_NOT_EXPOSE_SECRET_OR_QUESTION"
    records: list[HyperClovaXCallRecord] = []
    client = HyperClovaXClient(
        _settings(),
        FakeHyperClovaXTransport(_response(secret, status_code=status_code)),
        on_call=records.append,
    )

    with pytest.raises(error_type) as caught:
        _complete(client)

    assert secret not in str(caught.value)
    assert records[0].outcome == outcome


def test_hcx_client_retries_one_header_bound_rate_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    waits: list[float] = []
    monkeypatch.setattr(
        "finance_agent_core.agent.providers.hyperclova._wait_for_rate_limit_reset",
        waits.append,
    )
    records: list[HyperClovaXCallRecord] = []
    transport = FakeHyperClovaXTransport(
        {
            **_response(None, status_code=429),
            "retry_after_seconds": 1.5,
        },
        _response("ok"),
    )
    client = HyperClovaXClient(_settings(), transport, on_call=records.append)

    assert _complete(client) == {"value": "ok"}
    assert waits == [1.5]
    assert len(transport.requests) == 2
    assert [record.outcome for record in records] == ["rate_limited", "success"]


def test_hcx_client_stops_after_one_header_bound_rate_limit_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    waits: list[float] = []
    monkeypatch.setattr(
        "finance_agent_core.agent.providers.hyperclova._wait_for_rate_limit_reset",
        waits.append,
    )
    records: list[HyperClovaXCallRecord] = []
    limited = {
        **_response(None, status_code=429),
        "retry_after_seconds": 1.5,
    }
    transport = FakeHyperClovaXTransport(limited, limited)
    client = HyperClovaXClient(_settings(), transport, on_call=records.append)

    with pytest.raises(HyperClovaXRateLimitError):
        _complete(client)

    assert waits == [1.5]
    assert len(transport.requests) == 2
    assert [record.outcome for record in records] == ["rate_limited", "rate_limited"]


@pytest.mark.parametrize(
    ("failure", "error_type", "outcome"),
    [
        (TimeoutError("DO_NOT_EXPOSE_TIMEOUT"), HyperClovaXTimeoutError, "timeout"),
        (
            ConnectionError("DO_NOT_EXPOSE_CONNECTION"),
            HyperClovaXTransportError,
            "transport_error",
        ),
    ],
)
def test_hcx_client_sanitizes_transport_failures(
    failure: BaseException,
    error_type: type[Exception],
    outcome: str,
) -> None:
    records: list[HyperClovaXCallRecord] = []
    client = HyperClovaXClient(
        _settings(),
        FakeHyperClovaXTransport(failure),
        on_call=records.append,
    )

    with pytest.raises(error_type) as caught:
        _complete(client)

    assert "DO_NOT_EXPOSE" not in str(caught.value)
    assert records[0].outcome == outcome


def test_hcx_client_rejects_invalid_transport_response_and_schema() -> None:
    secret = "DO_NOT_EXPOSE_INVALID_RESPONSE"
    records: list[HyperClovaXCallRecord] = []
    transport = FakeHyperClovaXTransport(
        {
            "status_code": 200,
            "content": '{"value":"ok"}',
            "request_id": "fake-request-002",
            "usage": None,
            "unexpected": secret,
        }
    )
    client = HyperClovaXClient(_settings(), transport, on_call=records.append)
    with pytest.raises(HyperClovaXResponseError) as caught:
        _complete(client)
    assert secret not in str(caught.value)
    assert records[0].outcome == "response_error"

    no_call = FakeHyperClovaXTransport(_response('{"value":"ok"}'))
    with pytest.raises(HyperClovaXConfigurationError, match="documented"):
        HyperClovaXClient(_settings(), no_call).complete(
            operation="intent_analysis",
            system_prompt="system",
            user_prompt="question",
            schema_name="test_schema",
            response_schema={
                "type": "object",
                "properties": {"value": {"type": "string", "const": "forbidden"}},
                "required": ["value"],
            },
            max_output_tokens=10,
            response_parser=lambda content: content,
        )
    assert no_call.requests == []
