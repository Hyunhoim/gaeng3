from __future__ import annotations

from pathlib import Path

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
    assert agent.capability_execution_overrides == frozenset()
    assert agent.require_approved_databases is False


@pytest.mark.parametrize("app_env", ["evaluation", "production"])
def test_build_agent_enables_request_time_approval_in_deployments(app_env: str) -> None:
    agent = build_agent(Settings(APP_ENV=app_env))

    assert agent.require_approved_databases is True


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


def test_fund_execution_policy_requires_database_and_enables_only_fund(
    tmp_path: Path,
) -> None:
    with pytest.raises(ValidationError, match="requires FINANCE_DB_FUND"):
        Settings(FINANCE_BACKEND_FUND_EXECUTION_POLICY="public_fund_v1_approved")

    settings = Settings(
        FINANCE_BACKEND_FUND_EXECUTION_POLICY="public_fund_v1_approved",
        FINANCE_DB_FUND=tmp_path / "fund.sqlite3",
    )
    agent = build_agent(settings)

    assert {family.value for family in settings.capability_execution_overrides} == {"fund"}
    assert {family.value for family in agent.capability_execution_overrides} == {"fund"}
