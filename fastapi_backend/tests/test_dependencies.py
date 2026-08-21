from __future__ import annotations

import os
from pathlib import Path

import pytest
from finance_agent_core.agent.providers import (
    HyperClovaXCallObserver,
    HyperClovaXHTTPTransport,
    HyperClovaXQueryPlanProvider,
    HyperClovaXStructuredRequest,
    LocalProviderError,
)
from finance_agent_core.answering import HyperClovaXGroundedAnswerProvider
from finance_agent_core.observability import BoundedAsyncAuditSink, InMemoryAuditSink
from pydantic import ValidationError

from app.config import Settings
from app.dependencies import _load_hcx_api_key, build_agent, require_approval_guard
from tests.conftest import stub_resolved_release


class HealthyLocalAnswerProvider:
    def __init__(self, settings) -> None:
        self.settings = settings
        self.healthcheck_calls = 0

    def healthcheck(self) -> dict[str, str]:
        self.healthcheck_calls += 1
        return {"status": "ok"}


class NoCallHyperClovaXTransport:
    def __init__(self) -> None:
        self.calls = 0

    def complete(self, request: HyperClovaXStructuredRequest) -> object:
        self.calls += 1
        raise AssertionError(f"unexpected HCLX call for {request.operation}")


def _hcx_settings(**overrides: object) -> Settings:
    values: dict[str, object] = {
        "APP_ENV": "evaluation",
        "FINANCE_BACKEND_ANSWER_PROVIDER": "hyperclova",
        "FINANCE_AGENT_LLM_MODE": "evaluation",
        "LLM_PROVIDER": "hyperclova",
        "HCX_MODEL": "HCX-007",
        "CLOVASTUDIO_API_KEY_FILE": "/nonexistent/test-clovastudio-api-key",
    }
    values.update(overrides)
    return Settings(**values)


def _install_no_call_hcx_transport(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[Path, NoCallHyperClovaXTransport]:
    secret = "nv-test-file-secret-DO-NOT-LOG"
    key_file = tmp_path / "clovastudio_api_key"
    key_file.write_text(secret + "\n", encoding="utf-8")
    key_file.chmod(0o600)
    transport = NoCallHyperClovaXTransport()

    def build_transport(*, api_key: str) -> NoCallHyperClovaXTransport:
        assert api_key == secret
        return transport

    monkeypatch.setattr("app.dependencies.HyperClovaXHTTPTransport", build_transport)
    return key_file, transport


def test_build_agent_defaults_to_deterministic_answer() -> None:
    agent = build_agent(Settings())

    assert agent.answer_provider is None
    assert agent.capability_execution_overrides == frozenset()
    assert agent.require_approved_databases is False


@pytest.mark.parametrize("app_env", ["evaluation", "production"])
def test_build_agent_enables_request_time_approval_in_deployments(app_env: str) -> None:
    agent = build_agent(
        Settings(APP_ENV=app_env),
        release_guard=stub_resolved_release(),
    )

    assert agent.require_approved_databases is True
    assert agent.require_agent_release is True


def test_build_agent_rejects_deployment_without_resolved_release() -> None:
    with pytest.raises(RuntimeError, match="requires a resolved release"):
        build_agent(Settings(APP_ENV="evaluation"))


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


def test_build_agent_wires_hcx_answer_without_an_eager_network_call(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    key_file, transport = _install_no_call_hcx_transport(tmp_path, monkeypatch)
    settings = _hcx_settings(CLOVASTUDIO_API_KEY_FILE=key_file)

    agent = build_agent(
        settings,
        release_guard=stub_resolved_release(),
    )

    assert isinstance(agent.answer_provider, HyperClovaXGroundedAnswerProvider)
    assert agent.query_plan_provider is None
    assert agent.answer_provider.model_name == "HCX-007"
    assert transport.calls == 0
    assert "nv-test-secret" not in repr(settings)


def test_hcx_query_plan_is_a_separate_opt_in_and_shares_the_transport(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    key_file, transport = _install_no_call_hcx_transport(tmp_path, monkeypatch)
    settings = _hcx_settings(
        FINANCE_BACKEND_HCX_QUERY_PLAN_ENABLED=True,
        CLOVASTUDIO_API_KEY_FILE=key_file,
    )
    audit = BoundedAsyncAuditSink(InMemoryAuditSink(max_events=32), queue_capacity=32)

    agent = build_agent(
        settings,
        release_guard=stub_resolved_release(),
        audit_sink=audit,
    )

    assert isinstance(agent.query_plan_provider, HyperClovaXQueryPlanProvider)
    assert isinstance(agent.answer_provider, HyperClovaXGroundedAnswerProvider)
    assert agent.hclx_planning_enabled is True
    assert agent.query_plan_provider._client.transport is transport
    assert agent.answer_provider._client.transport is transport
    assert type(agent.query_plan_provider._client.on_call) is HyperClovaXCallObserver
    assert agent.query_plan_provider._client.on_call is agent.answer_provider._client.on_call
    assert agent.query_plan_provider._client.on_call.expected_audit_sink is audit
    assert transport.calls == 0
    assert audit.close(timeout_seconds=2)


def test_approval_guard_rejects_hcx_provider_without_the_bound_call_observer(
    tmp_path: Path,
) -> None:
    key_file = tmp_path / "clovastudio_api_key"
    key_file.write_text("nv-observer-guard-test-secret\n", encoding="utf-8")
    key_file.chmod(0o600)
    settings = _hcx_settings(CLOVASTUDIO_API_KEY_FILE=key_file)
    release = stub_resolved_release()
    audit = BoundedAsyncAuditSink(InMemoryAuditSink(max_events=32), queue_capacity=32)
    agent = build_agent(settings, release_guard=release, audit_sink=audit)

    assert (
        require_approval_guard(
            agent,
            settings,
            release_guard=release,
            audit_sink=audit,
        )
        is agent
    )
    assert isinstance(agent.answer_provider, HyperClovaXGroundedAnswerProvider)
    agent.answer_provider._client.on_call = None

    with pytest.raises(RuntimeError, match="approved RoutedFinanceAgent"):
        require_approval_guard(
            agent,
            settings,
            release_guard=release,
            audit_sink=audit,
        )

    assert audit.close(timeout_seconds=2)


def test_hcx_query_plan_can_be_evaluated_without_hcx_answer_generation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    key_file, transport = _install_no_call_hcx_transport(tmp_path, monkeypatch)
    settings = _hcx_settings(
        FINANCE_BACKEND_ANSWER_PROVIDER="deterministic",
        FINANCE_BACKEND_HCX_QUERY_PLAN_ENABLED=True,
        CLOVASTUDIO_API_KEY_FILE=key_file,
    )

    agent = build_agent(
        settings,
        release_guard=stub_resolved_release(),
    )

    assert isinstance(agent.query_plan_provider, HyperClovaXQueryPlanProvider)
    assert agent.answer_provider is None
    assert agent.hclx_planning_enabled is True
    assert transport.calls == 0


@pytest.mark.parametrize("app_env", ["evaluation", "production"])
def test_build_agent_rejects_inline_key_through_injected_hcx_transport(
    app_env: str,
) -> None:
    secret = "nv-inline-secret-must-not-bypass-file-boundary"
    transport = HyperClovaXHTTPTransport(api_key=secret)
    settings = _hcx_settings(APP_ENV=app_env, FINANCE_AGENT_LLM_MODE=app_env)

    with pytest.raises(
        RuntimeError,
        match="forbids caller-injected HyperCLOVA transport",
    ) as caught:
        build_agent(
            settings,
            hcx_transport=transport,
            release_guard=stub_resolved_release(),
        )

    assert secret not in str(caught.value)


def test_build_agent_loads_docker_secret_file_without_network(
    tmp_path: Path,
    monkeypatch,
) -> None:
    secret = "nv-test-file-secret-DO-NOT-LOG"
    key_file = tmp_path / "clovastudio_api_key"
    key_file.write_text(secret + "\n", encoding="utf-8")
    key_file.chmod(0o600)
    captured: list[str] = []

    def build_transport(*, api_key: str) -> NoCallHyperClovaXTransport:
        captured.append(api_key)
        return NoCallHyperClovaXTransport()

    monkeypatch.setattr("app.dependencies.HyperClovaXHTTPTransport", build_transport)
    settings = _hcx_settings(
        CLOVASTUDIO_API_KEY_FILE=key_file,
    )

    agent = build_agent(settings, release_guard=stub_resolved_release())

    assert isinstance(agent.answer_provider, HyperClovaXGroundedAnswerProvider)
    assert captured == [secret]
    assert secret not in repr(settings)


def test_build_agent_sanitizes_unreadable_hcx_secret_file(tmp_path: Path) -> None:
    missing = tmp_path / "missing-clovastudio-api-key"
    settings = _hcx_settings(
        CLOVASTUDIO_API_KEY_FILE=missing,
    )

    with pytest.raises(RuntimeError, match="credential file is unreadable") as caught:
        build_agent(settings, release_guard=stub_resolved_release())

    assert str(missing) not in str(caught.value)


def test_build_agent_sanitizes_non_utf8_hcx_secret_file(tmp_path: Path) -> None:
    key_file = tmp_path / "non-utf8-clovastudio-api-key"
    key_file.write_bytes(b"\xff\xfeDO_NOT_EXPOSE")
    key_file.chmod(0o600)
    settings = _hcx_settings(
        CLOVASTUDIO_API_KEY_FILE=key_file,
    )

    with pytest.raises(RuntimeError, match="credential file is unreadable") as caught:
        build_agent(settings, release_guard=stub_resolved_release())

    assert "DO_NOT_EXPOSE" not in str(caught.value)
    assert str(key_file) not in str(caught.value)


def test_hcx_secret_loader_completes_short_regular_file_reads(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret = "nv-short-read-test-secret"
    key_file = tmp_path / "short-read-key"
    key_file.write_text(secret + "\n", encoding="utf-8")
    key_file.chmod(0o600)
    settings = _hcx_settings(CLOVASTUDIO_API_KEY_FILE=key_file)
    original_read = os.read
    calls = 0

    def short_read(descriptor: int, size: int) -> bytes:
        nonlocal calls
        calls += 1
        return original_read(descriptor, min(size, 2))

    monkeypatch.setattr("app.dependencies.os.read", short_read)

    assert _load_hcx_api_key(settings) == secret
    assert calls > 2


def test_hcx_secret_loader_rejects_oversized_file_without_exposing_it(
    tmp_path: Path,
) -> None:
    key_file = tmp_path / "oversized-key"
    key_file.write_bytes(b"S" * 4097)
    key_file.chmod(0o600)
    settings = _hcx_settings(CLOVASTUDIO_API_KEY_FILE=key_file)

    with pytest.raises(RuntimeError, match="credential file is insecure") as caught:
        _load_hcx_api_key(settings)

    assert "SSSS" not in str(caught.value)


@pytest.mark.parametrize("kind", ["permissive", "symlink", "hardlink"])
def test_build_agent_rejects_insecure_hcx_secret_files(tmp_path: Path, kind: str) -> None:
    original = tmp_path / "original-key"
    original.write_text("nv-test-secret", encoding="utf-8")
    original.chmod(0o600)
    candidate = original
    if kind == "permissive":
        original.chmod(0o640)
    elif kind == "symlink":
        candidate = tmp_path / "linked-key"
        candidate.symlink_to(original)
    else:
        candidate = tmp_path / "hard-linked-key"
        candidate.hardlink_to(original)

    settings = _hcx_settings(CLOVASTUDIO_API_KEY_FILE=candidate)
    with pytest.raises(RuntimeError, match="credential file is (?:insecure|unreadable)"):
        build_agent(settings, release_guard=stub_resolved_release())


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"APP_ENV": "development"}, "allowed only"),
        ({"FINANCE_AGENT_LLM_MODE": "production"}, "match APP_ENV"),
        ({"LLM_PROVIDER": "disabled"}, "LLM_PROVIDER=hyperclova"),
        ({"HCX_MODEL": "HCX-CONTRACT-TEST"}, "HCX_MODEL=HCX-007"),
        ({"CLOVASTUDIO_API_KEY_FILE": None}, "requires CLOVASTUDIO_API_KEY_FILE"),
    ],
)
def test_settings_reject_incomplete_hcx_opt_in(
    overrides: dict[str, object],
    message: str,
) -> None:
    with pytest.raises(ValidationError, match=message):
        _hcx_settings(**overrides)


@pytest.mark.parametrize("app_env", ["evaluation", "production"])
def test_settings_reject_inline_hcx_credential_in_deployments(app_env: str) -> None:
    secret = "nv-inline-secret-must-not-be-accepted"

    with pytest.raises(
        ValidationError,
        match="inline HyperCLOVA credential is forbidden",
    ) as caught:
        Settings(APP_ENV=app_env, CLOVASTUDIO_API_KEY=secret)

    assert secret not in str(caught.value)


def test_settings_reject_inline_hcx_environment_by_name_without_exposing_value(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret = "nv-environment-secret-must-not-be-loaded"
    monkeypatch.setenv("CLOVASTUDIO_API_KEY", secret)

    with pytest.raises(
        ValidationError,
        match="inline HyperCLOVA credential is forbidden",
    ) as caught:
        Settings(APP_ENV="evaluation")

    assert secret not in str(caught.value)


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
