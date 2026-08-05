from __future__ import annotations

from typing import Protocol, cast

from fastapi import Request

from finance_agent_core.agent import IntentRouter, RoutedAgentResult, RoutedFinanceAgent

from app.config import Settings


class AgentService(Protocol):
    """Small interface used by the HTTP layer and replaceable in tests."""

    router: IntentRouter

    def answer(self, question: str, request_id: str) -> RoutedAgentResult: ...


def build_agent(settings: Settings) -> RoutedFinanceAgent:
    """Create the core Agent without making an eager database connection."""

    return RoutedFinanceAgent(settings.database_paths)


def get_settings(request: Request) -> Settings:
    return cast(Settings, request.app.state.settings)


def get_agent(request: Request) -> AgentService:
    return cast(AgentService, request.app.state.agent)
