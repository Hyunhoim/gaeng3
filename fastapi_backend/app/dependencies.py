from __future__ import annotations

from typing import Protocol, cast

from fastapi import Request
from finance_agent_core.agent import IntentRouter, RoutedAgentResult, RoutedFinanceAgent
from finance_agent_core.agent.providers import LocalTestSettings
from finance_agent_core.answering import LocalGroundedAnswerProvider

from app.config import Settings


class AgentService(Protocol):
    """Small interface used by the HTTP layer and replaceable in tests."""

    router: IntentRouter

    def answer(self, question: str, request_id: str) -> RoutedAgentResult: ...


def build_agent(settings: Settings) -> RoutedFinanceAgent:
    """Create the core Agent without making an eager database connection."""

    answer_provider = None
    if settings.answer_provider == "local_test":
        answer_provider = LocalGroundedAnswerProvider(LocalTestSettings.from_environment())
        answer_provider.healthcheck()
    return RoutedFinanceAgent(
        settings.database_paths,
        answer_provider=answer_provider,
    )


def get_settings(request: Request) -> Settings:
    return cast(Settings, request.app.state.settings)


def get_agent(request: Request) -> AgentService:
    return cast(AgentService, request.app.state.agent)
