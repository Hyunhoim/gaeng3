from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from finance_agent_core.storage import DatasetApprovalError

from app.config import Settings
from app.main import create_app
from tests.conftest import FakeAgentService


def _evaluation_settings(tmp_path: Path) -> Settings:
    return Settings(
        APP_ENV="evaluation",
        overseas_etp_db=tmp_path / "overseas.sqlite3",
        domestic_etp_db=tmp_path / "domestic.sqlite3",
        bond_db=tmp_path / "bond.sqlite3",
        fund_db=tmp_path / "fund.sqlite3",
    )


def test_evaluation_startup_checks_approved_databases_before_agent_build(
    tmp_path: Path,
    monkeypatch,
) -> None:
    calls: list[dict[object, Path]] = []

    def approve(paths: dict[object, Path]) -> None:
        calls.append(paths)

    monkeypatch.setattr("app.main.require_approved_database_paths", approve)

    application = create_app(settings=_evaluation_settings(tmp_path))

    assert application.state.agent is not None
    assert len(calls) == 1
    assert len(calls[0]) == 4


def test_evaluation_startup_fails_closed_on_unapproved_database(
    tmp_path: Path,
    monkeypatch,
) -> None:
    def reject(_: dict[object, Path]) -> None:
        raise DatasetApprovalError("unapproved test database")

    monkeypatch.setattr("app.main.require_approved_database_paths", reject)

    with pytest.raises(DatasetApprovalError, match="unapproved test database"):
        create_app(settings=_evaluation_settings(tmp_path))


def test_injected_evaluation_agent_gets_request_time_pre_and_post_approval(
    tmp_path: Path,
    monkeypatch,
) -> None:
    runtime_calls: list[dict[object, Path]] = []
    monkeypatch.setattr(
        "app.main.require_approved_database_paths",
        lambda _paths: None,
    )
    monkeypatch.setattr(
        "app.dependencies.require_approved_database_paths",
        lambda paths: runtime_calls.append(paths),
    )
    agent = FakeAgentService()
    application = create_app(
        settings=_evaluation_settings(tmp_path),
        agent=agent,
    )

    with TestClient(application) as client:
        response = client.post(
            "/answer",
            json={"request_id": "approved-injected", "question": "김치 레시피를 알려줘"},
        )

    assert response.status_code == 200
    assert len(runtime_calls) == 2
    assert all(len(paths) == 4 for paths in runtime_calls)
    assert agent.calls == [("김치 레시피를 알려줘", "approved-injected")]


@pytest.mark.parametrize("method", ["post", "get"])
def test_postcheck_replacement_discards_injected_agent_result_safely(
    method: str,
    tmp_path: Path,
    monkeypatch,
) -> None:
    calls = 0

    def runtime_approval(_: dict[object, Path]) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise DatasetApprovalError("DO_NOT_LEAK_REPLACEMENT_DETAIL")

    monkeypatch.setattr(
        "app.main.require_approved_database_paths",
        lambda _paths: None,
    )
    monkeypatch.setattr(
        "app.dependencies.require_approved_database_paths",
        runtime_approval,
    )
    application = create_app(
        settings=_evaluation_settings(tmp_path),
        agent=FakeAgentService(),
    )

    with TestClient(application) as client:
        if method == "post":
            response = client.post(
                "/answer",
                json={"request_id": "swap-post", "question": "김치 레시피를 알려줘"},
            )
        else:
            response = client.get(
                "/answer",
                params={"question_id": "swap-get", "question": "김치 레시피를 알려줘"},
            )

    assert calls == 2
    assert "DO_NOT_LEAK_REPLACEMENT_DETAIL" not in response.text
    if method == "post":
        assert response.status_code == 503
        assert response.json()["error"]["code"] == "dataset_unavailable"
        assert response.json()["products"] == []
    else:
        assert response.status_code == 200
        assert set(response.json()) == {
            "question_id",
            "question",
            "retrieved_context",
            "think_trace",
            "answer",
        }
