import json
from pathlib import Path

import pytest

from finance_agent_core.agent import FinanceAgent
from finance_agent_core.agent.providers import (
    LocalProviderError,
    LocalTestProvider,
    LocalTestSettings,
    MockProvider,
    first_vertical_slice_plan,
)
from finance_agent_core.domain import DatabaseManifest, NormalizedOverseasEtpRecord


def test_mock_agent_completes_verified_vertical_slice(
    sample_database: tuple[Path, list[NormalizedOverseasEtpRecord], DatabaseManifest],
) -> None:
    path, _, _ = sample_database
    response = FinanceAgent(path, MockProvider()).answer(
        "미국 채권형 해외 ETF 중 총보수 0.20% 이하를 AUM 순으로 보여줘",
        "agent-001",
    )

    assert response.provider == "mock"
    assert response.candidate_count == 6
    assert [product.ticker for product in response.products] == [
        "B6",
        "B5",
        "B4",
        "B2",
        "B3",
    ]
    assert "수익을 보장" in response.answer
    assert len(response.warnings) == 3


def test_local_settings_require_double_opt_in() -> None:
    with pytest.raises(LocalProviderError, match="double opt-in"):
        LocalTestSettings.from_environment({})
    with pytest.raises(LocalProviderError, match="loopback"):
        LocalTestSettings.from_environment(
            {
                "FINANCE_AGENT_LLM_MODE": "local_test",
                "ENABLE_NON_HCX_TEST_LLM": "1",
                "LLM_PROVIDER": "local_test",
                "LOCAL_TEST_LLM_BASE_URL": "https://example.com/v1",
                "LOCAL_TEST_LLM_MODEL": "qwen3-local-test",
            }
        )


def test_local_provider_validates_generated_plan(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = LocalTestSettings.from_environment(
        {
            "FINANCE_AGENT_LLM_MODE": "local_test",
            "ENABLE_NON_HCX_TEST_LLM": "1",
            "LLM_PROVIDER": "local_test",
            "LOCAL_TEST_LLM_BASE_URL": "http://127.0.0.1:18000/v1",
            "LOCAL_TEST_LLM_MODEL": "qwen3-local-test",
        }
    )
    provider = LocalTestProvider(settings)
    content = first_vertical_slice_plan("model-generated-id").model_dump_json()
    captured: dict[str, object] = {}

    def fake_request(path: str, payload: dict[str, object]) -> dict[str, object]:
        captured["path"] = path
        captured["payload"] = payload
        return {"choices": [{"message": {"content": content}}]}

    monkeypatch.setattr(
        provider,
        "_request_json",
        fake_request,
    )

    plan = provider.generate_query_plan("테스트 질문", "trusted-request-id")

    assert plan.question_id == "trusted-request-id"
    assert json.loads(plan.model_dump_json())["limit"] == 5
    assert captured["path"] == "chat/completions"
    payload = captured["payload"]
    assert isinstance(payload, dict)
    assert payload["response_format"]["type"] == "json_schema"
    assert "overseas_etp와 domestic_etp" in payload["messages"][0]["content"]
    assert "one_month_return_pct, aum" in payload["messages"][0]["content"]
    assert "required_rankings" in payload["messages"][0]["content"]


def test_local_provider_uses_fund_schema_only_for_internal_evaluation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = LocalTestSettings.from_environment(
        {
            "FINANCE_AGENT_LLM_MODE": "local_test",
            "ENABLE_NON_HCX_TEST_LLM": "1",
            "LLM_PROVIDER": "local_test",
            "LOCAL_TEST_LLM_BASE_URL": "http://127.0.0.1:18000/v1",
            "LOCAL_TEST_LLM_MODEL": "qwen3-local-test",
        }
    )
    provider = LocalTestProvider(settings, internal_evaluation_family="fund")
    content = first_vertical_slice_plan("model-generated-id").model_dump_json()
    captured: dict[str, object] = {}

    def fake_request(path: str, payload: dict[str, object]) -> dict[str, object]:
        captured["payload"] = payload
        return {"choices": [{"message": {"content": content}}]}

    monkeypatch.setattr(provider, "_request_json", fake_request)
    plan = provider.generate_query_plan(
        "해외 주식형 공모펀드를 3개월 수익률 높은 순으로 5개 보여줘",
        "fund-local-001",
    )

    assert plan.product_families[0].value == "fund"
    assert any(
        constraint.field == "public_offering" and constraint.value is True
        for constraint in plan.constraints
    )
    payload = captured["payload"]
    assert isinstance(payload, dict)
    schema = payload["response_format"]["json_schema"]["schema"]
    assert schema["properties"]["product_families"]["items"]["enum"] == ["fund"]
    assert "공식 Agent의 fund 실행은 여전히 비활성" in payload["messages"][0]["content"]
