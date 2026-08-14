from __future__ import annotations

from pathlib import Path

import pytest
from finance_agent_core.agent import RoutedFinanceAgent
from finance_agent_core.storage import DatasetApprovalError

from app.config import Settings
from app.dependencies import build_agent
from app.main import create_app
from tests.conftest import FakeAgentService, stub_resolved_release


def _evaluation_settings(tmp_path: Path) -> Settings:
    return Settings(
        APP_ENV="evaluation",
        overseas_etp_db=tmp_path / "overseas.sqlite3",
        domestic_etp_db=tmp_path / "domestic.sqlite3",
        bond_db=tmp_path / "bond.sqlite3",
        fund_db=tmp_path / "fund.sqlite3",
    )


def _allow_release(monkeypatch):
    release = stub_resolved_release()
    monkeypatch.setattr(
        "app.main.resolve_runtime_release",
        lambda _settings: release,
    )
    return release


def test_evaluation_startup_checks_approved_databases_before_agent_build(
    tmp_path: Path,
    monkeypatch,
) -> None:
    calls: list[dict[object, Path]] = []

    def approve(paths: dict[object, Path]) -> None:
        calls.append(paths)

    _allow_release(monkeypatch)
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

    _allow_release(monkeypatch)
    monkeypatch.setattr("app.main.require_approved_database_paths", reject)

    with pytest.raises(DatasetApprovalError, match="unapproved test database"):
        create_app(settings=_evaluation_settings(tmp_path))


def test_evaluation_rejects_an_externally_injected_agent(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        "app.main.require_approved_database_paths",
        lambda _paths: None,
    )
    agent = FakeAgentService()
    with pytest.raises(RuntimeError, match="forbids externally injected"):
        create_app(
            settings=_evaluation_settings(tmp_path),
            agent=agent,
        )
    assert agent.calls == []


def test_evaluation_rejects_a_misassembled_internal_agent(
    tmp_path: Path,
    monkeypatch,
) -> None:
    _allow_release(monkeypatch)
    monkeypatch.setattr(
        "app.main.require_approved_database_paths",
        lambda _paths: None,
    )
    monkeypatch.setattr(
        "app.main.build_agent",
        lambda _settings, *, release_guard: FakeAgentService(),
    )

    with pytest.raises(RuntimeError, match="approved RoutedFinanceAgent"):
        create_app(settings=_evaluation_settings(tmp_path))


def test_evaluation_rejects_a_routed_agent_without_approval_authority(
    tmp_path: Path,
    monkeypatch,
) -> None:
    settings = _evaluation_settings(tmp_path)
    _allow_release(monkeypatch)
    monkeypatch.setattr(
        "app.main.require_approved_database_paths",
        lambda _paths: None,
    )
    monkeypatch.setattr(
        "app.main.build_agent",
        lambda _settings, *, release_guard: RoutedFinanceAgent(
            settings.database_paths,
            require_approved_databases=False,
        ),
    )

    with pytest.raises(RuntimeError, match="approved RoutedFinanceAgent"):
        create_app(settings=settings)


def test_evaluation_rejects_a_mutated_outer_approval_flag(
    tmp_path: Path,
    monkeypatch,
) -> None:
    settings = _evaluation_settings(tmp_path)
    _allow_release(monkeypatch)
    misassembled = RoutedFinanceAgent(
        settings.database_paths,
        require_approved_databases=False,
    )
    misassembled.require_approved_databases = True
    monkeypatch.setattr(
        "app.main.require_approved_database_paths",
        lambda _paths: None,
    )
    monkeypatch.setattr(
        "app.main.build_agent",
        lambda _settings, *, release_guard: misassembled,
    )

    with pytest.raises(RuntimeError, match="approved RoutedFinanceAgent"):
        create_app(settings=settings)


def test_evaluation_verifies_release_before_database_or_agent(
    tmp_path: Path,
    monkeypatch,
) -> None:
    calls: list[str] = []

    def reject_release(_settings):
        calls.append("release")
        raise RuntimeError("release mismatch")

    monkeypatch.setattr("app.main.resolve_runtime_release", reject_release)
    monkeypatch.setattr(
        "app.main.require_approved_database_paths",
        lambda _paths: calls.append("database"),
    )
    monkeypatch.setattr(
        "app.main.build_agent",
        lambda _settings, *, release_guard: calls.append("agent"),
    )

    with pytest.raises(RuntimeError, match="release mismatch"):
        create_app(settings=_evaluation_settings(tmp_path))

    assert calls == ["release"]


def test_evaluation_rejects_agent_bound_to_a_different_release(
    tmp_path: Path,
    monkeypatch,
) -> None:
    settings = _evaluation_settings(tmp_path)
    expected_release = _allow_release(monkeypatch)
    other_release = stub_resolved_release()
    assert other_release is not expected_release
    misassembled = RoutedFinanceAgent(
        settings.database_paths,
        require_approved_databases=True,
        release_guard=other_release,
        require_agent_release=True,
    )
    monkeypatch.setattr("app.main.require_approved_database_paths", lambda _paths: None)
    monkeypatch.setattr(
        "app.main.build_agent",
        lambda _settings, *, release_guard: misassembled,
    )

    with pytest.raises(RuntimeError, match="approved RoutedFinanceAgent"):
        create_app(settings=settings)


def test_evaluation_rejects_provider_injection_outside_release_profile(
    tmp_path: Path,
    monkeypatch,
) -> None:
    settings = _evaluation_settings(tmp_path)
    release = _allow_release(monkeypatch)
    misassembled = RoutedFinanceAgent(
        settings.database_paths,
        require_approved_databases=True,
        release_guard=release,
        require_agent_release=True,
    )
    misassembled.query_plan_provider = object()
    monkeypatch.setattr("app.main.require_approved_database_paths", lambda _paths: None)
    monkeypatch.setattr(
        "app.main.build_agent",
        lambda _settings, *, release_guard: misassembled,
    )

    with pytest.raises(RuntimeError, match="approved RoutedFinanceAgent"):
        create_app(settings=settings)


def test_evaluation_rejects_unapproved_schema_shadow_observer(
    tmp_path: Path,
    monkeypatch,
) -> None:
    settings = _evaluation_settings(tmp_path)
    release = _allow_release(monkeypatch)
    misassembled = build_agent(settings, release_guard=release)
    misassembled.schema_link_shadow_observer = object()  # type: ignore[assignment]
    monkeypatch.setattr("app.main.require_approved_database_paths", lambda _paths: None)
    monkeypatch.setattr(
        "app.main.build_agent",
        lambda _settings, *, release_guard: misassembled,
    )

    with pytest.raises(RuntimeError, match="approved RoutedFinanceAgent"):
        create_app(settings=settings)


def test_evaluation_rejects_hclx_planning_authority_outside_release_profile(
    tmp_path: Path,
    monkeypatch,
) -> None:
    settings = _evaluation_settings(tmp_path)
    release = _allow_release(monkeypatch)
    misassembled = RoutedFinanceAgent(
        settings.database_paths,
        hclx_planning_enabled=True,
        require_approved_databases=True,
        release_guard=release,
        require_agent_release=True,
    )
    monkeypatch.setattr("app.main.require_approved_database_paths", lambda _paths: None)
    monkeypatch.setattr(
        "app.main.build_agent",
        lambda _settings, *, release_guard: misassembled,
    )

    with pytest.raises(RuntimeError, match="approved RoutedFinanceAgent"):
        create_app(settings=settings)


def test_evaluation_rejects_record_cache_outside_release_profile(
    tmp_path: Path,
    monkeypatch,
) -> None:
    settings = _evaluation_settings(tmp_path)
    release = _allow_release(monkeypatch)
    misassembled = build_agent(settings, release_guard=release)
    misassembled._record_cache_enabled = True
    monkeypatch.setattr("app.main.require_approved_database_paths", lambda _paths: None)
    monkeypatch.setattr(
        "app.main.build_agent",
        lambda _settings, *, release_guard: misassembled,
    )

    with pytest.raises(RuntimeError, match="approved RoutedFinanceAgent"):
        create_app(settings=settings)


@pytest.mark.parametrize(
    "mutation",
    [
        "outer_internal_dataset",
        "outer_capabilities",
        "split_identity_cache",
        "custom_record_cache",
    ],
)
def test_evaluation_rejects_mutated_outer_policy_and_cache_wiring(
    tmp_path: Path,
    monkeypatch,
    mutation: str,
) -> None:
    settings = _evaluation_settings(tmp_path)
    release = _allow_release(monkeypatch)
    misassembled = build_agent(settings, release_guard=release)
    if mutation == "outer_internal_dataset":
        misassembled.allow_internal_disabled_dataset = True
    elif mutation == "outer_capabilities":
        misassembled.capability_execution_overrides = frozenset({"fund"})
    elif mutation == "split_identity_cache":
        misassembled.grounded_plan_gate.identity_cache = object()
    else:
        misassembled.record_cache = object()
    monkeypatch.setattr("app.main.require_approved_database_paths", lambda _paths: None)
    monkeypatch.setattr(
        "app.main.build_agent",
        lambda _settings, *, release_guard: misassembled,
    )

    with pytest.raises(RuntimeError, match="approved RoutedFinanceAgent"):
        create_app(settings=settings)
