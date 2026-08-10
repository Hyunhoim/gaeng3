from __future__ import annotations

from pathlib import Path
from typing import Protocol, cast

from fastapi import Request
from finance_agent_core.agent import IntentRouter, RoutedAgentResult, RoutedFinanceAgent
from finance_agent_core.agent.providers import LocalTestSettings
from finance_agent_core.answering import LocalGroundedAnswerProvider
from finance_agent_core.contracts.queryplan import ProductFamily
from finance_agent_core.storage import require_approved_database_paths

from app.config import Settings


class AgentService(Protocol):
    """Small interface used by the HTTP layer and replaceable in tests."""

    router: IntentRouter

    def answer(self, question: str, request_id: str) -> RoutedAgentResult: ...


class ApprovalValidatedAgentService:
    """Apply the immutable release guard to an injected deployment service."""

    def __init__(
        self,
        delegate: AgentService,
        database_paths: dict[ProductFamily, Path],
    ) -> None:
        self._delegate = delegate
        self._database_paths = dict(database_paths)
        self.router = delegate.router

    def answer(self, question: str, request_id: str) -> RoutedAgentResult:
        require_approved_database_paths(self._database_paths)
        try:
            return self._delegate.answer(question, request_id)
        finally:
            require_approved_database_paths(self._database_paths)


def require_approval_guard(
    service: AgentService,
    settings: Settings,
) -> AgentService:
    """Ensure evaluation/production injection cannot bypass request-time approval."""

    if settings.app_env not in {"evaluation", "production"}:
        return service
    if isinstance(service, RoutedFinanceAgent) and service.require_approved_databases:
        return service
    return ApprovalValidatedAgentService(service, settings.database_paths)


def build_agent(settings: Settings) -> RoutedFinanceAgent:
    """Create the core Agent without making an eager database connection."""

    answer_provider = None
    if settings.answer_provider == "local_test":
        answer_provider = LocalGroundedAnswerProvider(LocalTestSettings.from_environment())
        answer_provider.healthcheck()
    return RoutedFinanceAgent(
        settings.database_paths,
        answer_provider=answer_provider,
        capability_execution_overrides=settings.capability_execution_overrides,
        require_approved_databases=settings.app_env in {"evaluation", "production"},
    )


def get_settings(request: Request) -> Settings:
    return cast(Settings, request.app.state.settings)


def get_agent(request: Request) -> AgentService:
    return cast(AgentService, request.app.state.agent)
