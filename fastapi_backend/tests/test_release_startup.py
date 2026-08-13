from __future__ import annotations

import hashlib
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from finance_agent_core.release import (
    AgentReleaseError,
    DeploymentBinding,
    RollbackRelease,
    RuntimeReleaseInputs,
    build_agent_release_manifest,
    deployment_binding_file_bytes,
    manifest_file_bytes,
)
from pydantic import ValidationError

from app.config import Settings
from app.dependencies import resolve_runtime_release
from app.main import create_app

_SOURCE_COMMIT = "a" * 40
_IMAGE_REFERENCE = "registry.example/finance-agent@sha256:" + "b" * 64


def _write_read_only(path: Path, data: bytes) -> None:
    if path.exists():
        path.chmod(0o644)
    path.write_bytes(data)
    path.chmod(0o444)


def _release_settings(tmp_path: Path, **overrides: object) -> tuple[Settings, Path]:
    values: dict[str, object] = {
        "APP_ENV": "evaluation",
        "FINANCE_BACKEND_ANSWER_PROVIDER": "deterministic",
        "FINANCE_SOURCE_COMMIT": _SOURCE_COMMIT,
        "FINANCE_RUNTIME_IMAGE_REFERENCE": _IMAGE_REFERENCE,
        "FINANCE_DB_OVERSEAS_ETP": tmp_path / "overseas.sqlite3",
        "FINANCE_DB_DOMESTIC_ETP": tmp_path / "domestic.sqlite3",
        "FINANCE_DB_BOND": tmp_path / "bond.sqlite3",
        "FINANCE_DB_FUND": tmp_path / "fund.sqlite3",
        "FINANCE_AUDIT_MODE": "jsonl",
        "FINANCE_AUDIT_FILE": tmp_path / "audit.jsonl",
    }
    values.update(overrides)
    answer_provider = values["FINANCE_BACKEND_ANSWER_PROVIDER"]
    hcx_enabled = bool(values.get("FINANCE_BACKEND_HCX_QUERY_PLAN_ENABLED", False))
    runtime = RuntimeReleaseInputs(
        environment="evaluation",
        source_commit=_SOURCE_COMMIT,
        image_reference=_IMAGE_REFERENCE,
        backend_version="0.1.0",
        backend_root=Path(__file__).resolve().parents[1] / "app",
        answer_provider=answer_provider,  # type: ignore[arg-type]
        hcx_queryplan_enabled=hcx_enabled,
        hcx_model=values.get("HCX_MODEL"),  # type: ignore[arg-type]
        fund_execution_policy=values.get(
            "FINANCE_BACKEND_FUND_EXECUTION_POLICY",
            "locked",
        ),  # type: ignore[arg-type]
        platform=values.get("FINANCE_RUNTIME_PLATFORM", "linux/amd64"),  # type: ignore[arg-type]
        hcx_timeout_seconds=float(values.get("HCX_TIMEOUT_SECONDS", 45.0)),
        official_answer_timeout_seconds=float(values.get("OFFICIAL_ANSWER_TIMEOUT_SECONDS", 55.0)),
        official_answer_max_inflight=int(values.get("OFFICIAL_ANSWER_MAX_INFLIGHT", 2)),
        worker_count=int(values.get("WEB_CONCURRENCY", 1)),
    )
    manifest = build_agent_release_manifest(runtime, release_id="finance-agent-test-v1")
    manifest_path = tmp_path / "agent-release-manifest.json"
    manifest_data = manifest_file_bytes(manifest)
    _write_read_only(manifest_path, manifest_data)
    binding = DeploymentBinding(
        release_id=manifest.release_id,
        environment=manifest.environment,
        source_commit=manifest.source_commit,
        release_manifest_sha256=hashlib.sha256(manifest_data).hexdigest(),
        image_reference=_IMAGE_REFERENCE,
        platform="linux/amd64",
        activation_generation=1,
        rollback=RollbackRelease(mode="initial_bootstrap"),
    )
    binding_path = tmp_path / "deployment-binding.json"
    binding_data = deployment_binding_file_bytes(binding)
    _write_read_only(binding_path, binding_data)
    values.update(
        {
            "FINANCE_RELEASE_MANIFEST_FILE": manifest_path,
            "FINANCE_DEPLOYMENT_BINDING_FILE": binding_path,
            "FINANCE_DEPLOYMENT_BINDING_SHA256": hashlib.sha256(binding_data).hexdigest(),
        }
    )
    return Settings(**values), binding_path


def test_resolve_runtime_release_matches_backend_code_and_profile(tmp_path: Path) -> None:
    settings, _ = _release_settings(tmp_path)

    release = resolve_runtime_release(settings)

    assert release is not None
    assert release.release_id == "finance-agent-test-v1"
    assert release.manifest.components.runtime_features.model.provider == "disabled"


def test_deployment_startup_resolves_release_before_database_and_agent(
    tmp_path: Path,
    monkeypatch,
) -> None:
    settings, _ = _release_settings(tmp_path)
    calls: list[str] = []
    original_resolver = resolve_runtime_release

    def resolve(observed: Settings):
        calls.append("release")
        return original_resolver(observed)

    monkeypatch.setattr("app.main.resolve_runtime_release", resolve)
    monkeypatch.setattr(
        "app.main.require_approved_database_paths",
        lambda _paths: calls.append("database"),
    )

    application = create_app(settings=settings)

    assert calls == ["release", "database"]
    assert application.state.agent.release_guard is application.state.release_guard
    assert (
        application.state.agent.plan_authority_gate.release_guard is application.state.release_guard
    )


def test_deployment_without_release_configuration_fails_before_database(
    tmp_path: Path,
    monkeypatch,
) -> None:
    calls: list[str] = []
    settings = Settings(
        APP_ENV="evaluation",
        FINANCE_DB_BOND=tmp_path / "bond.sqlite3",
    )
    monkeypatch.setattr(
        "app.main.require_approved_database_paths",
        lambda _paths: calls.append("database"),
    )

    with pytest.raises(RuntimeError, match="complete Agent release"):
        create_app(settings=settings)

    assert calls == []


def test_release_profile_mismatch_stops_before_credential_or_agent_build(
    tmp_path: Path,
    monkeypatch,
) -> None:
    settings, _ = _release_settings(tmp_path)
    mismatched = settings.model_copy(
        update={
            "answer_provider": "hyperclova",
            "llm_mode": "evaluation",
            "llm_provider": "hyperclova",
            "hcx_model": "HCX-007",
        }
    )
    calls: list[str] = []
    monkeypatch.setattr(
        "app.main.require_approved_database_paths",
        lambda _paths: calls.append("database"),
    )
    monkeypatch.setattr(
        "app.main.build_agent",
        lambda *_args, **_kwargs: calls.append("agent"),
    )

    with pytest.raises(AgentReleaseError):
        create_app(settings=mismatched)

    assert calls == []


@pytest.mark.parametrize(
    ("setting", "value"),
    [
        ("runtime_platform", "linux/arm64"),
        ("hcx_timeout_seconds", 46.0),
        ("official_answer_timeout_seconds", 54.0),
        ("official_answer_max_inflight", 3),
    ],
)
def test_release_runtime_control_mismatch_stops_startup(
    tmp_path: Path,
    setting: str,
    value: object,
) -> None:
    settings, _ = _release_settings(tmp_path)

    with pytest.raises(AgentReleaseError):
        resolve_runtime_release(settings.model_copy(update={setting: value}))


def test_release_resolver_rejects_multiple_workers_before_manifest_resolution(
    tmp_path: Path,
) -> None:
    settings, _ = _release_settings(tmp_path)

    with pytest.raises(RuntimeError, match="one web worker"):
        resolve_runtime_release(settings.model_copy(update={"web_concurrency": 2}))


def test_hcx_release_assembles_exact_provider_without_network_call(
    tmp_path: Path,
    monkeypatch,
) -> None:
    key_file = tmp_path / "clovastudio-api-key"
    key_file.write_text("nv-release-contract-test-secret\n", encoding="utf-8")
    settings, _ = _release_settings(
        tmp_path,
        FINANCE_BACKEND_ANSWER_PROVIDER="hyperclova",
        FINANCE_AGENT_LLM_MODE="evaluation",
        LLM_PROVIDER="hyperclova",
        HCX_MODEL="HCX-007",
        CLOVASTUDIO_API_KEY_FILE=key_file,
    )
    calls: list[str] = []
    original_resolver = resolve_runtime_release

    def resolve(observed: Settings):
        calls.append("release")
        return original_resolver(observed)

    monkeypatch.setattr("app.main.resolve_runtime_release", resolve)
    monkeypatch.setattr(
        "app.main.require_approved_database_paths",
        lambda _paths: calls.append("database"),
    )
    monkeypatch.setattr(
        "app.dependencies._load_hcx_api_key",
        lambda _settings: calls.append("credential") or "nv-release-contract-test-secret",
    )

    application = create_app(settings=settings)

    assert calls == ["release", "database", "credential"]
    assert application.state.agent.answer_provider.provider_name == "hyperclova"
    assert application.state.agent.answer_provider.model_name == "HCX-007"
    assert (
        application.state.release_guard.manifest.components.runtime_features.model.provider
        == "hyperclova"
    )


def test_health_becomes_degraded_if_release_changes_after_startup(
    tmp_path: Path,
    monkeypatch,
) -> None:
    settings, binding_path = _release_settings(tmp_path)
    monkeypatch.setattr("app.main.require_approved_database_paths", lambda _paths: None)
    monkeypatch.setattr("app.routes.health._database_is_ready", lambda *_args, **_kwargs: True)
    application = create_app(settings=settings)

    with TestClient(application) as client:
        healthy = client.get("/health")
        changed = binding_path.read_bytes().replace(
            b'"activation_generation":1',
            b'"activation_generation":2',
        )
        _write_read_only(binding_path, changed)
        degraded = client.get("/health")

    assert healthy.status_code == 200
    assert healthy.json()["status"] == "ok"
    assert degraded.status_code == 503
    assert degraded.json()["status"] == "degraded"
    assert "release" not in degraded.json()


def test_partial_release_configuration_is_rejected() -> None:
    with pytest.raises(ValidationError, match="one complete set"):
        Settings(
            FINANCE_RELEASE_MANIFEST_FILE="/app/release/agent-release-manifest.json",
        )


@pytest.mark.parametrize("app_env", ["evaluation", "production"])
def test_deployment_settings_reject_multiple_web_workers(app_env: str) -> None:
    with pytest.raises(ValidationError, match="WEB_CONCURRENCY=1"):
        Settings(APP_ENV=app_env, WEB_CONCURRENCY=2)


def test_development_settings_still_allow_multiple_web_workers() -> None:
    assert Settings(APP_ENV="development", WEB_CONCURRENCY=2).web_concurrency == 2


@pytest.mark.parametrize(
    "flag",
    ["FINANCE_DENSE_SCHEMA_LINKER_ENABLED", "FINANCE_PRODUCT_DENSE_ENABLED"],
)
def test_deployment_settings_keep_unapproved_dense_modules_off(flag: str) -> None:
    with pytest.raises(ValidationError, match="Dense retrieval remains disabled"):
        Settings(APP_ENV="production", **{flag: True})
