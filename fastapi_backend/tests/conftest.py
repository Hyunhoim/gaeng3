from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from finance_agent_core.agent import IntentRouter, RoutedFinanceAgent

from app.config import Settings
from app.main import create_app


class FakeAgentService:
    """Database-free service that returns real control-path Agent contracts."""

    def __init__(self, error: Exception | None = None) -> None:
        self._delegate = RoutedFinanceAgent({})
        self.router: IntentRouter = self._delegate.router
        self.error = error
        self.calls: list[tuple[str, str]] = []

    def answer(self, question: str, request_id: str):
        self.calls.append((question, request_id))
        if self.error is not None:
            raise self.error
        return self._delegate.answer(question, request_id)


@pytest.fixture
def fake_agent() -> FakeAgentService:
    return FakeAgentService()


@pytest.fixture
def client(fake_agent: FakeAgentService) -> Iterator[TestClient]:
    application = create_app(settings=Settings(), agent=fake_agent)
    with TestClient(application) as test_client:
        yield test_client
