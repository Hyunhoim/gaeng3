from __future__ import annotations

from pathlib import Path

import pytest

from finance_agent_core.agent.providers import (
    HyperClovaXAuthenticationError,
    HyperClovaXCallRecord,
    HyperClovaXClient,
    HyperClovaXConfigurationError,
    HyperClovaXFundComparisonDraftProvider,
    HyperClovaXQueryPlanProvider,
    HyperClovaXRateLimitError,
    HyperClovaXResponseError,
    HyperClovaXServiceError,
    HyperClovaXSettings,
    HyperClovaXStructuredRequest,
    HyperClovaXTimeoutError,
    HyperClovaXTokenUsage,
    HyperClovaXTransportError,
)
from finance_agent_core.agent.providers.mock import (
    domestic_vertical_slice_plan,
    first_vertical_slice_plan,
)
from finance_agent_core.answering import (
    ExpectedGroundedAnswerProvider,
    HyperClovaXGroundedAnswerProvider,
    build_grounded_answer_context,
)
from finance_agent_core.contracts.hcx_schema import validate_hcx_schema
from finance_agent_core.domain import DatabaseManifest, NormalizedDomesticEtpRecord
from finance_agent_core.execution import (
    ResultVerifier,
    SQLiteOracle,
    build_product_evidence,
)
from finance_agent_core.execution.verifier_projection import (
    load_projected_verifier_records,
)


class FakeHyperClovaXTransport:
    def __init__(self, *script: object) -> None:
        self.script = list(script)
        self.requests: list[HyperClovaXStructuredRequest] = []

    def complete(self, request: HyperClovaXStructuredRequest) -> object:
        self.requests.append(request)
        if not self.script:
            raise AssertionError("fake transport has no scripted response")
        response = self.script.pop(0)
        if isinstance(response, BaseException):
            raise response
        return response


def _settings() -> HyperClovaXSettings:
    return HyperClovaXSettings(model="HCX-CONTRACT-TEST", timeout_seconds=12)


def _response(
    content: str | None,
    *,
    status_code: int = 200,
) -> dict[str, object]:
    return {
        "status_code": status_code,
        "content": content,
        "request_id": "fake-request-001",
        "usage": {
            "input_tokens": 100,
            "output_tokens": 20,
            "total_tokens": 120,
        },
    }


def _simple_schema() -> dict[str, object]:
    return {
        "type": "object",
        "properties": {"value": {"type": "string"}},
        "required": ["value"],
    }


def test_hcx_settings_fail_closed_outside_official_mode() -> None:
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
    with pytest.raises(HyperClovaXConfigurationError, match="HCX_MODEL"):
        HyperClovaXSettings.from_environment(
            {
                "FINANCE_AGENT_LLM_MODE": "production",
                "LLM_PROVIDER": "hyperclova",
            }
        )


def test_hcx_settings_validate_timeout_and_do_not_require_guessed_http_config() -> None:
    settings = HyperClovaXSettings.from_environment(
        {
            "FINANCE_AGENT_LLM_MODE": "evaluation",
            "LLM_PROVIDER": "hyperclova",
            "HCX_MODEL": "HCX-CONTRACT-TEST",
            "HCX_TIMEOUT_SECONDS": "45",
        }
    )

    assert settings == HyperClovaXSettings(
        model="HCX-CONTRACT-TEST",
        timeout_seconds=45,
    )
    assert not hasattr(settings, "base_url")
    assert not hasattr(settings, "api_key")
    with pytest.raises(HyperClovaXConfigurationError, match="timeout"):
        HyperClovaXSettings(model="HCX-CONTRACT-TEST", timeout_seconds=0)
    with pytest.raises(HyperClovaXConfigurationError, match="HCX-"):
        HyperClovaXSettings(model="qwen3-local-test")


def test_hcx_query_plan_provider_uses_semantic_structured_request() -> None:
    content = first_vertical_slice_plan("model-generated-id").model_dump_json()
    transport = FakeHyperClovaXTransport(_response(content))
    records: list[HyperClovaXCallRecord] = []
    provider = HyperClovaXQueryPlanProvider(
        _settings(),
        transport,
        on_call=records.append,
    )

    plan = provider.generate_query_plan("테스트 질문", "trusted-request-id")

    assert provider.provider_name == "hyperclova"
    assert provider.model_name == "HCX-CONTRACT-TEST"
    assert plan.question_id == "trusted-request-id"
    assert len(transport.requests) == 1
    request = transport.requests[0]
    assert request.operation == "query_plan"
    assert request.schema_name == "finance_query_plan"
    assert request.model == "HCX-CONTRACT-TEST"
    assert request.timeout_seconds == 12
    validate_hcx_schema(request.response_schema)
    assert records[0].outcome == "success"
    assert records[0].usage == HyperClovaXTokenUsage(
        input_tokens=100,
        output_tokens=20,
        total_tokens=120,
    )
    assert "system_prompt" not in records[0].model_dump()
    assert "user_prompt" not in records[0].model_dump()


def test_hcx_fund_comparison_provider_uses_minimum_privilege_schema() -> None:
    transport = FakeHyperClovaXTransport(
        _response(
            '{"target_mentions":["테스트 펀드 A","테스트 펀드 B"],'
            '"comparison_fields":["risk_level","aum"]}'
        )
    )
    provider = HyperClovaXFundComparisonDraftProvider(_settings(), transport)

    draft = provider.generate_comparison_draft(
        '"테스트 펀드 A"와 "테스트 펀드 B"의 위험등급과 AUM을 비교해줘',
        "hcx-compare-001",
    )

    assert draft.target_mentions == ["테스트 펀드 A", "테스트 펀드 B"]
    assert draft.comparison_fields == ["risk_level", "aum"]
    request = transport.requests[0]
    assert request.operation == "fund_comparison_draft"
    validate_hcx_schema(request.response_schema)
    field_enum = request.response_schema["properties"]["comparison_fields"]["items"]["enum"]
    assert "risk_level" in field_enum
    assert "total_expense_ratio_pct" not in field_enum


def test_hcx_grounded_answer_provider_uses_evidence_only_hcx_schema(
    domestic_sample_database: tuple[
        Path,
        list[NormalizedDomesticEtpRecord],
        DatabaseManifest,
    ],
) -> None:
    path, _, _ = domestic_sample_database
    plan = domestic_vertical_slice_plan("hcx-answer-001")
    executed = SQLiteOracle(path).execute(plan)
    universe = load_projected_verifier_records(path, plan)
    verified = ResultVerifier().verify(plan, executed, universe)
    products = build_product_evidence(plan, verified)
    context = build_grounded_answer_context(
        question="미국 주식형 국내 ETF를 수익률 순으로 보여줘",
        plan=plan,
        verified=verified,
        products=products,
    )
    expected = ExpectedGroundedAnswerProvider().generate_grounded_answer(context)
    transport = FakeHyperClovaXTransport(_response(expected.model_dump_json()))
    provider = HyperClovaXGroundedAnswerProvider(_settings(), transport)

    draft = provider.generate_grounded_answer(context)

    assert draft == expected
    request = transport.requests[0]
    assert request.operation == "grounded_answer"
    validate_hcx_schema(request.response_schema)
    assert context.products[0].product_id not in request.system_prompt
    assert context.products[0].product_name not in request.system_prompt
    assert context.products[0].ticker not in request.system_prompt
    assert context.products[0].fields[3].normalized_value not in request.system_prompt
    assert request.response_schema["properties"]["products"]["items"]["properties"][
        "explanation"
    ]["enum"] == ["선택한 근거 항목이 요청한 정렬 근거로 사용됐습니다."]


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
    transport = FakeHyperClovaXTransport(_response(secret, status_code=status_code))
    records: list[HyperClovaXCallRecord] = []
    client = HyperClovaXClient(_settings(), transport, on_call=records.append)

    with pytest.raises(error_type) as caught:
        client.complete(
            operation="query_plan",
            system_prompt=secret,
            user_prompt=secret,
            schema_name="test_schema",
            response_schema=_simple_schema(),
            max_output_tokens=10,
        )

    assert secret not in str(caught.value)
    assert records[0].outcome == outcome


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
    transport = FakeHyperClovaXTransport(failure)
    records: list[HyperClovaXCallRecord] = []
    client = HyperClovaXClient(_settings(), transport, on_call=records.append)

    with pytest.raises(error_type) as caught:
        client.complete(
            operation="query_plan",
            system_prompt="secret prompt",
            user_prompt="secret question",
            schema_name="test_schema",
            response_schema=_simple_schema(),
            max_output_tokens=10,
        )

    assert "DO_NOT_EXPOSE" not in str(caught.value)
    assert records[0].outcome == outcome


def test_hcx_client_rejects_invalid_transport_response_without_echoing_values() -> None:
    secret = "DO_NOT_EXPOSE_INVALID_RESPONSE"
    transport = FakeHyperClovaXTransport(
        {
            "status_code": 200,
            "content": '{"value":"ok"}',
            "request_id": "fake-request-002",
            "usage": None,
            "unexpected": secret,
        }
    )
    records: list[HyperClovaXCallRecord] = []
    client = HyperClovaXClient(_settings(), transport, on_call=records.append)

    with pytest.raises(HyperClovaXResponseError) as caught:
        client.complete(
            operation="query_plan",
            system_prompt="system",
            user_prompt="question",
            schema_name="test_schema",
            response_schema=_simple_schema(),
            max_output_tokens=10,
        )

    assert secret not in str(caught.value)
    assert records[0].outcome == "response_error"


def test_hcx_client_rejects_non_hcx_schema_before_transport() -> None:
    transport = FakeHyperClovaXTransport(_response('{"value":"ok"}'))
    client = HyperClovaXClient(_settings(), transport)

    with pytest.raises(HyperClovaXConfigurationError, match="documented"):
        client.complete(
            operation="query_plan",
            system_prompt="system",
            user_prompt="question",
            schema_name="test_schema",
            response_schema={
                "type": "object",
                "properties": {"value": {"type": "string", "const": "forbidden"}},
                "required": ["value"],
            },
            max_output_tokens=10,
        )

    assert transport.requests == []


@pytest.mark.parametrize(
    "content",
    [
        "not-json",
        "[]",
        '{"schema_version":"1.0","intent":"search","unexpected":"forbidden"}',
    ],
)
def test_hcx_query_plan_provider_rejects_invalid_model_output(content: str) -> None:
    transport = FakeHyperClovaXTransport(_response(content))
    provider = HyperClovaXQueryPlanProvider(_settings(), transport)

    with pytest.raises(HyperClovaXResponseError) as caught:
        provider.generate_query_plan("테스트 질문", "trusted-request-id")

    assert content not in str(caught.value)
