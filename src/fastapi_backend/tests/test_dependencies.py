from __future__ import annotations

import os
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest
from finance_agent_core.agent.providers import (
    HyperClovaXCallObserver,
    HyperClovaXFinalAnswerProvider,
    HyperClovaXHTTPTransport,
    HyperClovaXIntentAnalysisProvider,
    HyperClovaXStructuredRequest,
)
from finance_agent_core.config.knowledge_coverage import (
    load_approved_knowledge_coverage_manifest,
)
from finance_agent_core.config.source_registry import load_official_source_registry
from finance_agent_core.observability import BoundedAsyncAuditSink, InMemoryAuditSink
from finance_agent_core.release import (
    ExternalKnowledgeArtifactRelease,
    PublicDocumentRetrievalRelease,
    PublicExternalKnowledgeRelease,
    PublicKnowledgeRetrievalRelease,
    PublicRelationRetrievalRelease,
    external_knowledge_verifier_contract_sha256,
)
from finance_agent_core.retrieval.official_connector import OfficialConnectorError
from finance_agent_core.retrieval.temporal_relations import (
    seal_temporal_relation_bundle,
)
from pydantic import ValidationError

from app.config import Settings
from app.dependencies import (
    _build_external_knowledge_runtime,
    _load_hcx_api_key,
    build_agent,
    require_approval_guard,
)
from tests.conftest import stub_resolved_release


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
        "FINANCE_BACKEND_HCX_INTENT_ANALYSIS_ENABLED": True,
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
    assert agent.external_knowledge_router is None
    assert agent.external_knowledge_runtime is None


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


def test_settings_rejects_retired_local_provider() -> None:
    with pytest.raises(ValidationError, match="local_test/Qwen Runtime provider is retired"):
        build_agent(Settings(answer_provider="local_test"))


def test_build_agent_wires_hcx_intent_and_final_answer_without_eager_network_call(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    key_file, transport = _install_no_call_hcx_transport(tmp_path, monkeypatch)
    settings = _hcx_settings(CLOVASTUDIO_API_KEY_FILE=key_file)

    agent = build_agent(
        settings,
        release_guard=stub_resolved_release(),
    )

    assert agent.answer_provider is None
    assert agent.query_plan_provider is None
    assert isinstance(agent.intent_analysis_provider, HyperClovaXIntentAnalysisProvider)
    assert isinstance(agent.official_answer_provider, HyperClovaXFinalAnswerProvider)
    assert agent.intent_analysis_provider.model_name == "HCX-007"
    assert agent.intent_analysis_provider.require_execution_structure is True
    assert agent.official_answer_provider.model_name == "HCX-007"
    assert agent.require_official_hclx
    assert transport.calls == 0
    assert "nv-test-secret" not in repr(settings)


def test_hcx_intent_and_final_answer_share_transport_and_observer(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    key_file, transport = _install_no_call_hcx_transport(tmp_path, monkeypatch)
    settings = _hcx_settings(CLOVASTUDIO_API_KEY_FILE=key_file)
    audit = BoundedAsyncAuditSink(InMemoryAuditSink(max_events=32), queue_capacity=32)

    agent = build_agent(
        settings,
        release_guard=stub_resolved_release(),
        audit_sink=audit,
    )

    assert isinstance(agent.intent_analysis_provider, HyperClovaXIntentAnalysisProvider)
    assert isinstance(agent.official_answer_provider, HyperClovaXFinalAnswerProvider)
    assert agent.query_plan_provider is None
    assert agent.answer_provider is None
    assert agent.hclx_planning_enabled is False
    assert agent.intent_analysis_provider._client.transport is transport
    assert agent.official_answer_provider._client.transport is transport
    assert type(agent.intent_analysis_provider._client.on_call) is HyperClovaXCallObserver
    assert (
        agent.intent_analysis_provider._client.on_call
        is agent.official_answer_provider._client.on_call
    )
    assert agent.intent_analysis_provider._client.on_call.expected_audit_sink is audit
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
    assert isinstance(agent.official_answer_provider, HyperClovaXFinalAnswerProvider)
    agent.official_answer_provider._client.on_call = None

    with pytest.raises(RuntimeError, match="approved RoutedFinanceAgent"):
        require_approval_guard(
            agent,
            settings,
            release_guard=release,
            audit_sink=audit,
        )

    assert audit.close(timeout_seconds=2)


def test_approval_guard_rejects_hcx_entity_alias_authority_mismatch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    key_file, _ = _install_no_call_hcx_transport(tmp_path, monkeypatch)
    settings = _hcx_settings(CLOVASTUDIO_API_KEY_FILE=key_file)
    release = stub_resolved_release()
    audit = BoundedAsyncAuditSink(InMemoryAuditSink(max_events=32), queue_capacity=32)
    agent = build_agent(settings, release_guard=release, audit_sink=audit)

    assert isinstance(agent.intent_analysis_provider, HyperClovaXIntentAnalysisProvider)
    assert agent.intent_analysis_provider.allow_entity_alias_repair is False
    agent.intent_analysis_provider.allow_entity_alias_repair = True

    with pytest.raises(RuntimeError, match="approved RoutedFinanceAgent"):
        require_approval_guard(
            agent,
            settings,
            release_guard=release,
            audit_sink=audit,
        )

    assert audit.close(timeout_seconds=2)


def test_retired_hcx_query_plan_flag_is_rejected(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    key_file, _ = _install_no_call_hcx_transport(tmp_path, monkeypatch)
    with pytest.raises(ValidationError, match="QueryPlan generation is retired"):
        _hcx_settings(
            FINANCE_BACKEND_HCX_QUERY_PLAN_ENABLED=True,
            CLOVASTUDIO_API_KEY_FILE=key_file,
        )


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

    assert isinstance(agent.intent_analysis_provider, HyperClovaXIntentAnalysisProvider)
    assert isinstance(agent.official_answer_provider, HyperClovaXFinalAnswerProvider)
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


@pytest.mark.parametrize(
    "variable",
    ["NAVER_API_HUB_CLIENT_ID", "NAVER_API_HUB_CLIENT_SECRET"],
)
def test_settings_reject_inline_naver_api_hub_environment_without_exposing_value(
    monkeypatch: pytest.MonkeyPatch,
    variable: str,
) -> None:
    secret = "naver-inline-secret-must-not-be-loaded"
    monkeypatch.setenv(variable, secret)

    with pytest.raises(
        ValidationError,
        match="inline NAVER API HUB credential is forbidden",
    ) as caught:
        Settings(APP_ENV="evaluation")

    assert secret not in str(caught.value)


@pytest.mark.parametrize("app_env", ["test", "evaluation", "production"])
def test_settings_reject_local_provider_outside_development(app_env: str) -> None:
    with pytest.raises(ValidationError, match="local_test/Qwen Runtime provider is retired"):
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


def _document_cache_settings_values(tmp_path: Path) -> dict[str, object]:
    return {
        "FINANCE_RELEASE_MANIFEST_FILE": tmp_path / "release.json",
        "FINANCE_DEPLOYMENT_BINDING_FILE": tmp_path / "binding.json",
        "FINANCE_DEPLOYMENT_BINDING_SHA256": "1" * 64,
        "FINANCE_SOURCE_COMMIT": "2" * 40,
        "FINANCE_RUNTIME_IMAGE_REFERENCE": "registry.example/finance@sha256:" + "3" * 64,
        "FINANCE_ADAPTIVE_SEMANTIC_ENABLED": True,
        "FINANCE_DENSE_SCHEMA_LINKER_ENABLED": True,
        "FINANCE_SCHEMA_DENSE_INDEX_FILE": tmp_path / "schema.json",
        "FINANCE_SCHEMA_DENSE_INDEX_SHA256": "4" * 64,
        "FINANCE_SCHEMA_DENSE_CALIBRATION_REPORT_SHA256": "5" * 64,
        "FINANCE_SCHEMA_DENSE_MIN_SCORE": 0.7,
        "FINANCE_SCHEMA_DENSE_HCLX_CANDIDATE_MIN_SCORE": 0.5,
        "FINANCE_SCHEMA_DENSE_MINIMUM_MARGIN": 0.1,
        "FINANCE_KURE_SNAPSHOT_DIR": tmp_path / "kure",
        "FINANCE_KURE_SNAPSHOT_MANIFEST_FILE": tmp_path / "kure.json",
        "FINANCE_KURE_TRUSTED_CACHE_ROOT": tmp_path / "cache",
        "FINANCE_EXTERNAL_KNOWLEDGE_ENABLED": True,
        "FINANCE_EXTERNAL_KNOWLEDGE_ARTIFACT_FILE": tmp_path / "external.json",
        "FINANCE_EXTERNAL_KNOWLEDGE_ARTIFACT_SHA256": "6" * 64,
        "FINANCE_EXTERNAL_TEMPORAL_RELATION_FILE": tmp_path / "relations.json",
        "FINANCE_EXTERNAL_TEMPORAL_RELATION_SHA256": "7" * 64,
        "NAVER_API_HUB_CLIENT_ID_FILE": tmp_path / "naver-id",
        "NAVER_API_HUB_CLIENT_SECRET_FILE": tmp_path / "naver-secret",
    }


def test_settings_require_document_cache_artifacts_as_one_release_set(tmp_path: Path) -> None:
    values = _document_cache_settings_values(tmp_path)
    with pytest.raises(ValidationError, match="requires index, activation receipt"):
        Settings(
            **values,
            FINANCE_EXTERNAL_DOCUMENT_DENSE_ARTIFACT_FILE=tmp_path / "dense.json",
        )

    settings = Settings(
        **values,
        FINANCE_EXTERNAL_DOCUMENT_DENSE_ARTIFACT_FILE=tmp_path / "dense.json",
        FINANCE_EXTERNAL_DOCUMENT_CACHE_ACTIVATION_FILE=tmp_path / "activation.json",
        FINANCE_EXTERNAL_DOCUMENT_CORPUS_MANIFEST_FILE=tmp_path / "approved.json",
        FINANCE_EXTERNAL_DOCUMENT_CORPUS_ROOT=tmp_path / "approved",
    )
    assert settings.document_cache_configured


@pytest.mark.parametrize(
    "retrieval_mode",
    ["approved_corpus_cache_first", "approved_corpus_cache_only"],
)
def test_external_runtime_selects_release_pinned_cache_builder(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    retrieval_mode: str,
) -> None:
    registry = load_official_source_registry()
    coverage = load_approved_knowledge_coverage_manifest()
    corpus_sha = "8" * 64
    relation_bundle = seal_temporal_relation_bundle(
        (),
        corpus_manifest_sha256=corpus_sha,
        registry=registry,
    )
    external = ExternalKnowledgeArtifactRelease(
        retrieval_mode=retrieval_mode,
        source_registry_sha256=registry.canonical_sha256,
        coverage_manifest_sha256=coverage.canonical_sha256,
        corpus_manifest_sha256=corpus_sha,
        document_bm25_sha256="9" * 64,
        document_dense_sha256="a" * 64,
        document_activation_receipt_sha256="b" * 64,
        temporal_relation_sha256=relation_bundle.manifest.relation_set_sha256,
        verifier_contract_sha256=external_knowledge_verifier_contract_sha256(),
        embedding_model_revision="d14c8a9423946e268a0c9952fecf3a7aabd73bd9",
    )
    artifact_file_sha = "c" * 64
    knowledge = PublicKnowledgeRetrievalRelease(
        relation=PublicRelationRetrievalRelease(status="disabled_not_activated"),
        document=PublicDocumentRetrievalRelease(),
        external=PublicExternalKnowledgeRelease(
            status="activated",
            artifact=external,
            artifact_file_sha256=artifact_file_sha,
        ),
    )
    base_guard = stub_resolved_release()
    guard = replace(
        base_guard,
        manifest=SimpleNamespace(
            release_id="finance-agent-test-v1",
            components=SimpleNamespace(knowledge_retrieval=knowledge),
        ),
    )
    values = _document_cache_settings_values(tmp_path)
    values["FINANCE_EXTERNAL_KNOWLEDGE_ARTIFACT_SHA256"] = artifact_file_sha
    if retrieval_mode == "approved_corpus_cache_only":
        values.pop("NAVER_API_HUB_CLIENT_ID_FILE")
        values.pop("NAVER_API_HUB_CLIENT_SECRET_FILE")
    settings = Settings(
        **values,
        FINANCE_EXTERNAL_DOCUMENT_DENSE_ARTIFACT_FILE=tmp_path / "dense.json",
        FINANCE_EXTERNAL_DOCUMENT_CACHE_ACTIVATION_FILE=tmp_path / "activation.json",
        FINANCE_EXTERNAL_DOCUMENT_CORPUS_MANIFEST_FILE=tmp_path / "approved.json",
        FINANCE_EXTERNAL_DOCUMENT_CORPUS_ROOT=tmp_path / "approved",
    )
    if retrieval_mode == "approved_corpus_cache_first":
        for name in ("naver-id", "naver-secret"):
            secret = tmp_path / name
            secret.write_text("test-secret", encoding="utf-8")
            secret.chmod(0o600)
    else:
        monkeypatch.setattr(
            "app.dependencies.NaverApiHubWebSearchTransport",
            lambda **_kwargs: (_ for _ in ()).throw(
                AssertionError("cache-only mode created a NAVER transport")
            ),
        )
        monkeypatch.setattr(
            "app.dependencies.UrllibOfficialHttpTransport",
            lambda: (_ for _ in ()).throw(
                AssertionError("cache-only mode created an HTTP transport")
            ),
        )
    monkeypatch.setattr(
        "app.dependencies.load_external_knowledge_artifact_release",
        lambda **_kwargs: external,
    )
    monkeypatch.setattr(
        "app.dependencies.load_verified_temporal_relation_bundle",
        lambda *_args, **_kwargs: relation_bundle,
    )
    captured: dict[str, object] = {}

    class FakeCache:
        production_approved = True
        activation_binding_sha256 = "b" * 64
        artifact = SimpleNamespace(canonical_sha256="a" * 64)
        live_fallback_enabled = retrieval_mode == "approved_corpus_cache_first"

    def build_cache(**kwargs: object) -> object:
        captured.update(kwargs)
        return FakeCache()

    monkeypatch.setattr(
        "app.dependencies.build_release_pinned_cache_first_discovery",
        build_cache,
    )
    monkeypatch.setattr(
        "finance_agent_core.execution.external_knowledge_runtime."
        "CacheFirstOfficialDiscoveryProvider",
        FakeCache,
    )
    monkeypatch.setattr(
        "app.dependencies.SqliteOfficialProductFactProvider",
        lambda *_args, **_kwargs: object(),
    )
    monkeypatch.setattr(
        "app.dependencies.ProvidedProductUniverseBuilder",
        lambda *_args, **_kwargs: object(),
    )
    embedding = SimpleNamespace(metadata=SimpleNamespace())

    runtime, universe, compiler = _build_external_knowledge_runtime(
        settings,
        guard,
        embedding_provider=embedding,  # type: ignore[arg-type]
    )

    assert runtime is not None and universe is not None and compiler is not None
    assert captured["expected_activation_receipt_sha256"] == "b" * 64
    assert captured["expected_document_dense_sha256"] == "a" * 64
    assert captured["expected_document_bm25_sha256"] == "9" * 64
    assert captured["embedding"] is embedding
    assert captured["approved_corpus_manifest_path"] == tmp_path / "approved.json"
    assert captured["approved_corpus_root"] == tmp_path / "approved"
    if retrieval_mode == "approved_corpus_cache_only":
        assert captured["cache_only_coverage_manifest"] is coverage
        assert captured["fallback"] is None
        with pytest.raises(OfficialConnectorError, match="network fetch is disabled"):
            runtime.acquisition.connector.transport.fetch(
                "https://example.invalid/",
                timeout_seconds=1.0,
                max_bytes=1,
            )
    else:
        assert captured["cache_only_coverage_manifest"] is None
        assert captured["fallback"] is not None

    if retrieval_mode == "approved_corpus_cache_only":
        credential_values = _document_cache_settings_values(tmp_path)
        credential_values.update(
            {
                "FINANCE_EXTERNAL_KNOWLEDGE_ARTIFACT_SHA256": artifact_file_sha,
                "FINANCE_EXTERNAL_DOCUMENT_DENSE_ARTIFACT_FILE": tmp_path / "dense.json",
                "FINANCE_EXTERNAL_DOCUMENT_CACHE_ACTIVATION_FILE": tmp_path / "activation.json",
                "FINANCE_EXTERNAL_DOCUMENT_CORPUS_MANIFEST_FILE": tmp_path / "approved.json",
                "FINANCE_EXTERNAL_DOCUMENT_CORPUS_ROOT": tmp_path / "approved",
            }
        )
        settings_with_live_credentials = Settings(**credential_values)
        with pytest.raises(RuntimeError, match="cache-only mode forbids"):
            _build_external_knowledge_runtime(
                settings_with_live_credentials,
                guard,
                embedding_provider=embedding,  # type: ignore[arg-type]
            )
