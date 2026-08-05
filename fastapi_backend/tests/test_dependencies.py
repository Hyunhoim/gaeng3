from __future__ import annotations

import pytest
from finance_agent_core.agent.providers import LocalProviderError
from pydantic import ValidationError

from app.config import Settings
from app.dependencies import build_agent


class HealthyLocalAnswerProvider:
    def __init__(self, settings) -> None:
        self.settings = settings
        self.healthcheck_calls = 0

    def healthcheck(self) -> dict[str, str]:
        self.healthcheck_calls += 1
        return {"status": "ok"}


def test_build_agent_defaults_to_deterministic_answer() -> None:
    agent = build_agent(Settings())

    assert agent.answer_provider is None


def test_build_agent_requires_all_local_provider_opt_ins(monkeypatch) -> None:
    for name in (
        "FINANCE_AGENT_LLM_MODE",
        "ENABLE_NON_HCX_TEST_LLM",
        "LLM_PROVIDER",
        "LOCAL_TEST_LLM_MODEL",
    ):
        monkeypatch.delenv(name, raising=False)

    with pytest.raises(LocalProviderError, match="explicit double opt-in"):
        build_agent(Settings(answer_provider="local_test"))


def test_build_agent_healthchecks_opted_in_local_provider(monkeypatch) -> None:
    monkeypatch.setenv("FINANCE_AGENT_LLM_MODE", "local_test")
    monkeypatch.setenv("ENABLE_NON_HCX_TEST_LLM", "1")
    monkeypatch.setenv("LLM_PROVIDER", "local_test")
    monkeypatch.setenv("LOCAL_TEST_LLM_MODEL", "qwen-test")
    monkeypatch.setattr(
        "app.dependencies.LocalGroundedAnswerProvider",
        HealthyLocalAnswerProvider,
    )

    agent = build_agent(Settings(answer_provider="local_test"))

    assert isinstance(agent.answer_provider, HealthyLocalAnswerProvider)
    assert agent.answer_provider.settings.model == "qwen-test"
    assert agent.answer_provider.healthcheck_calls == 1


@pytest.mark.parametrize("app_env", ["test", "evaluation", "production"])
def test_settings_reject_local_provider_outside_development(app_env: str) -> None:
    with pytest.raises(ValidationError, match="allowed only in development"):
        Settings(
            APP_ENV=app_env,
            FINANCE_BACKEND_ANSWER_PROVIDER="local_test",
        )
