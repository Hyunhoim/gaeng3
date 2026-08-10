import json
from pathlib import Path

import pytest

from finance_agent_core.agent import FinanceAgent, RoutedFinanceAgent
from finance_agent_core.agent.providers import (
    LocalProviderError,
    LocalTestProvider,
    LocalTestSettings,
    MockProvider,
    first_vertical_slice_plan,
)
from finance_agent_core.answering import ExpectedGroundedAnswerProvider
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


def test_routed_agent_completes_server_compiled_grounded_path(
    sample_database: tuple[Path, list[NormalizedOverseasEtpRecord], DatabaseManifest],
) -> None:
    path, _, _ = sample_database
    agent = RoutedFinanceAgent(
        {"overseas_etp": path},
        answer_provider=ExpectedGroundedAnswerProvider(),
    )

    result = agent.answer(
        "미국 채권형 해외 ETF 중 현재 거래 가능한 상품에서 "
        "총보수 0.20% 이하를 AUM 높은 순으로 5개 보여줘",
        "routed-001",
    )

    assert result.status == "executed"
    assert result.decision.draft.intent.value == "search"
    assert result.query_plan is not None
    assert result.query_plan.question_id == "routed-001"
    assert result.candidate_count == 6
    assert [product.ticker for product in result.products] == [
        "B6",
        "B5",
        "B4",
        "B2",
        "B3",
    ]
    assert result.answer_composition is not None
    assert result.answer_composition.mode == "llm_grounded"
    assert result.answer_composition.verification.passed


@pytest.mark.parametrize("intent_word", ["상세 정보를 조회해줘", "총보수를 설명해줘"])
def test_routed_agent_lowers_exact_detail_and_explain_to_search(
    sample_database: tuple[Path, list[NormalizedOverseasEtpRecord], DatabaseManifest],
    intent_word: str,
) -> None:
    path, _, _ = sample_database
    result = RoutedFinanceAgent({"overseas_etp": path}).answer(
        f"종목코드 B2인 해외 ETF의 {intent_word}",
        f"routed-{intent_word[:2]}",
    )

    assert result.status == "executed"
    assert result.query_plan is not None
    assert result.query_plan.intent.value == "search"
    assert result.query_plan.limit == 1
    assert len(result.products) == 1
    assert result.products[0].ticker == "B2"


def test_routed_agent_resolves_exact_identity_before_a_reversed_label(
    sample_database: tuple[Path, list[NormalizedOverseasEtpRecord], DatabaseManifest],
) -> None:
    path, _, _ = sample_database
    result = RoutedFinanceAgent({"overseas_etp": path}).answer(
        "AMX:B2라는 상품 ID의 해외 ETF 상세 정보를 알려줘",
        "routed-reversed-identity-label",
    )

    assert result.status == "executed"
    assert result.query_plan is not None
    assert result.query_plan.limit == 1
    assert [product.product_id for product in result.products] == ["AMX:B2"]


def test_routed_agent_never_touches_tools_for_control_route() -> None:
    agent = RoutedFinanceAgent({})

    unsupported = agent.answer(
        "내일 가장 오를 해외 ETF를 예측하고 매수 추천해줘",
        "routed-control-001",
    )
    clarify = agent.answer(
        "안전한 국내채권을 찾아줘",
        "routed-control-002",
    )
    negated_public = agent.answer(
        "공모가 아닌 공모펀드를 보여줘",
        "routed-control-003",
    )
    negated_trade = agent.answer(
        "현재 거래 가능하지 않은 해외 ETF를 보여줘",
        "routed-control-004",
    )
    negated_rank = agent.answer(
        "AUM이 크지 않은 해외 ETF를 보여줘",
        "routed-control-005",
    )
    excluded_identity = agent.answer(
        "B2를 제외한 해외 ETF를 보여줘",
        "routed-control-006",
    )

    assert unsupported.status == "unsupported"
    assert unsupported.query_plan is None
    assert unsupported.products == []
    assert clarify.status == "clarify"
    assert clarify.query_plan is None
    assert negated_public.status == "unsupported"
    assert negated_public.query_plan is None
    assert negated_public.products == []
    assert negated_trade.status == "clarify"
    assert negated_trade.query_plan is None
    assert negated_trade.products == []
    assert negated_rank.status == "clarify"
    assert negated_rank.query_plan is None
    assert negated_rank.products == []
    assert excluded_identity.status == "clarify"
    assert excluded_identity.query_plan is None
    assert excluded_identity.products == []


def test_routed_agent_falls_back_when_answer_provider_fails(
    sample_database: tuple[Path, list[NormalizedOverseasEtpRecord], DatabaseManifest],
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
            raise RuntimeError("simulated provider failure")

    result = RoutedFinanceAgent(
        {"overseas_etp": path},
        answer_provider=FailingAnswerProvider(),
    ).answer(
        "미국 채권형 해외 ETF 중 총보수 0.20% 이하를 AUM 높은 순으로 3개 보여줘",
        "routed-fallback-001",
    )

    assert result.status == "executed"
    assert result.answer_composition is not None
    assert result.answer_composition.mode == "deterministic_fallback"
    assert not result.answer_composition.verification.passed
    assert "검증된 후보" in result.answer


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
