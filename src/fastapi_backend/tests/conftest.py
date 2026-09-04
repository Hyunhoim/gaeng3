from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from finance_agent_core.agent import IntentRouter, RoutedFinanceAgent
from finance_agent_core.release import (
    PublicDocumentRetrievalRelease,
    PublicKnowledgeRetrievalRelease,
    PublicRelationRetrievalRelease,
    ResolvedAgentRelease,
)

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


def stub_resolved_release() -> ResolvedAgentRelease:
    """Exact nominal release object for assembly-only tests that never execute it."""

    knowledge_retrieval = PublicKnowledgeRetrievalRelease(
        relation=PublicRelationRetrievalRelease(status="disabled_not_activated"),
        document=PublicDocumentRetrievalRelease(),
    )
    return ResolvedAgentRelease(
        manifest=SimpleNamespace(
            release_id="finance-agent-test-v1",
            components=SimpleNamespace(knowledge_retrieval=knowledge_retrieval),
        ),  # type: ignore[arg-type]
        binding=SimpleNamespace(),  # type: ignore[arg-type]
        manifest_path=Path("/nonexistent/test-agent-release.json"),
        binding_path=Path("/nonexistent/test-deployment-binding.json"),
        manifest_file_sha256="a" * 64,
        binding_file_sha256="b" * 64,
        release_context_sha256="c" * 64,
        runtime_inputs=SimpleNamespace(),  # type: ignore[arg-type]
    )


@pytest.fixture
def fake_agent() -> FakeAgentService:
    return FakeAgentService()


@pytest.fixture
def client(fake_agent: FakeAgentService) -> Iterator[TestClient]:
    application = create_app(settings=Settings(), agent=fake_agent)
    with TestClient(application) as test_client:
        yield test_client
