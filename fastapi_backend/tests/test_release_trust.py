from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi_backend.scripts import release_trust
from finance_agent_core.release import (
    DeploymentBinding,
    RelationRetrievalArtifactRelease,
    RollbackRelease,
    RuntimeReleaseInputs,
    build_agent_release_manifest,
    deployment_binding_file_bytes,
    manifest_file_bytes,
    relation_retrieval_artifact_file_bytes,
)


def _repository_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _fixture(
    tmp_path: Path,
    *,
    answer_provider: str = "deterministic",
    queryplan_enabled: bool = False,
    fund_execution_policy: str = "locked",
    hcx_timeout_seconds: float = 45.0,
    environment_name: str = "evaluation",
) -> tuple[Path, dict[str, str], Path]:
    image = "registry.example/finance-agent@sha256:" + "a" * 64
    source_commit = "b" * 40
    release_id = "finance-agent-test-v1"
    relation_artifact_sha256 = "c" * 64
    uses_hcx = answer_provider == "hyperclova" or queryplan_enabled
    manifest = tmp_path / "agent-release-manifest.json"
    manifest_payload = {
        "schema_version": "1.2",
        "release_id": release_id,
        "environment": environment_name,
        "source_commit": source_commit,
        "components": {
            "knowledge_retrieval": {
                "relation": {
                    "status": "activated",
                    "artifact_file_sha256": relation_artifact_sha256,
                },
                "document": {
                    "status": "disabled_no_approved_corpus",
                    "artifact": None,
                },
            },
            "runtime_features": {
                "fund_execution_policy": fund_execution_policy,
                "model": {
                    "provider": "hyperclova" if uses_hcx else "disabled",
                    "model_id": "HCX-007" if uses_hcx else None,
                    "revision_status": (
                        "provider_revision_not_exposed" if uses_hcx else "not_used"
                    ),
                    "queryplan_operation_enabled": queryplan_enabled,
                    "grounded_answer_operation_enabled": answer_provider == "hyperclova",
                },
                "retrieval": {
                    "schema_dense": "disabled_offline_only",
                    "schema_dense_manifest_sha256": None,
                    "embedding_model_revision": None,
                    "product_dense": "disabled_not_implemented",
                    "reranker": "disabled_not_implemented",
                    "document_bm25": "disabled_no_approved_corpus",
                },
            },
            "runtime_controls": {
                "hcx_timeout_seconds": hcx_timeout_seconds,
                "official_answer_timeout_seconds": 270.0,
                "official_answer_max_inflight": 2,
                "worker_count": 1,
                "audit_schema_version": "1.2",
                "audit_sink_kind": "append_only_jsonl",
                "audit_queue_capacity": 2048,
                "audit_shutdown_timeout_seconds": 5.0,
                "audit_fsync_each_event": True,
            },
        },
    }
    manifest_data = (
        json.dumps(manifest_payload, sort_keys=True, separators=(",", ":")) + "\n"
    ).encode()
    manifest.write_bytes(manifest_data)
    binding = tmp_path / "deployment-binding.json"
    binding_data = (
        json.dumps(
            {
                "release_id": release_id,
                "environment": environment_name,
                "source_commit": source_commit,
                "image_reference": image,
                "platform": "linux/amd64",
                "release_manifest_sha256": hashlib.sha256(manifest_data).hexdigest(),
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    ).encode()
    binding.write_bytes(binding_data)
    manifest_bundle = tmp_path / "agent-release-manifest.sigstore.json"
    binding_bundle = tmp_path / "deployment-binding.sigstore.json"
    manifest_bundle.write_text("{}\n", encoding="utf-8")
    binding_bundle.write_text("{}\n", encoding="utf-8")
    env_file = tmp_path / ".env.release"
    env_file.write_text(
        "\n".join(
            [
                f"FINANCE_IMAGE_REFERENCE={image}",
                f"APP_ENV={environment_name}",
                f"FINANCE_SOURCE_COMMIT={source_commit}",
                "FINANCE_RUNTIME_PLATFORM=linux/amd64",
                f"FINANCE_RELEASE_MANIFEST_HOST_FILE={manifest}",
                f"FINANCE_RELEASE_MANIFEST_SIGSTORE_BUNDLE_HOST_FILE={manifest_bundle}",
                f"FINANCE_DEPLOYMENT_BINDING_HOST_FILE={binding}",
                "FINANCE_DEPLOYMENT_BINDING_SHA256=" + hashlib.sha256(binding_data).hexdigest(),
                f"FINANCE_DEPLOYMENT_BINDING_SIGSTORE_BUNDLE_HOST_FILE={binding_bundle}",
                "FINANCE_RELATION_RETRIEVAL_ARTIFACT_SHA256=" + relation_artifact_sha256,
                f"FINANCE_BACKEND_ANSWER_PROVIDER={answer_provider}",
                "FINANCE_BACKEND_HCX_QUERY_PLAN_ENABLED=" + str(queryplan_enabled).lower(),
                f"FINANCE_BACKEND_FUND_EXECUTION_POLICY={fund_execution_policy}",
                f"HCX_TIMEOUT_SECONDS={hcx_timeout_seconds:g}",
                "WEB_CONCURRENCY=1",
                *(
                    [
                        f"FINANCE_AGENT_LLM_MODE={environment_name}",
                        "LLM_PROVIDER=hyperclova",
                        "HCX_MODEL=HCX-007",
                    ]
                    if uses_hcx
                    else []
                ),
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    capture = tmp_path / "cosign-calls.txt"
    fake_bin = _repository_root() / "fastapi_backend/tests/fixtures/release_launcher_bin"
    environment = os.environ.copy()
    environment.update(
        {
            "PATH": f"{fake_bin}:{environment['PATH']}",
            "COSIGN_CAPTURE_FILE": str(capture),
        }
    )
    return env_file, environment, capture


def _run(env_file: Path, environment: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            sys.executable,
            str(_repository_root() / "fastapi_backend/scripts/release_trust.py"),
            "--env-file",
            str(env_file),
        ],
        cwd=_repository_root(),
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )


def _update_environment(path: Path, **updates: str | None) -> None:
    values = dict(
        line.split("=", 1) for line in path.read_text(encoding="utf-8").splitlines() if line
    )
    for name, value in updates.items():
        if value is None:
            values.pop(name, None)
        else:
            values[name] = value
    path.write_text(
        "\n".join(f"{name}={value}" for name, value in values.items()) + "\n",
        encoding="utf-8",
    )


def test_release_trust_verifies_image_manifest_and_binding(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    env_file, _, _ = _fixture(tmp_path)
    calls: list[list[str]] = []

    monkeypatch.setattr(
        release_trust,
        "_cosign_binary",
        lambda: Path("/usr/local/bin/cosign"),
    )

    def capture(binary: Path, arguments: list[str]) -> None:
        assert binary == Path("/usr/local/bin/cosign")
        for argument in arguments:
            if argument.startswith("/tmp/finance-agent-trust-"):
                assert Path(argument).is_file()
        calls.append(arguments)

    monkeypatch.setattr(release_trust, "_run_verification", capture)

    release_trust.verify_release_trust(env_file)

    assert len(calls) == 3
    assert calls[0][0] == "verify"
    assert calls[1][0:2] == ["verify-blob", "--bundle"]
    assert calls[2][0:2] == ["verify-blob", "--bundle"]
    assert all(any("refs/heads/main" in value for value in call) for call in calls)
    assert all("https://token.actions.githubusercontent.com" in call for call in calls)


def test_release_trust_accepts_final_hcx_answer_only_profile(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    env_file, _, _ = _fixture(tmp_path, answer_provider="hyperclova")
    calls: list[list[str]] = []
    monkeypatch.setattr(
        release_trust,
        "_cosign_binary",
        lambda: Path("/usr/local/bin/cosign"),
    )
    monkeypatch.setattr(
        release_trust,
        "_run_verification",
        lambda _binary, arguments: calls.append(arguments),
    )

    release_trust.verify_release_trust(env_file)

    assert len(calls) == 3


def test_release_trust_accepts_manifest_emitted_by_release_builder(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    env_file, _, _ = _fixture(tmp_path, answer_provider="hyperclova")
    image_reference = "registry.example/finance-agent@sha256:" + "a" * 64
    source_commit = "b" * 40
    relation_artifact = RelationRetrievalArtifactRelease(
        index_sha256="d" * 64,
        approval_manifest_sha256="e" * 64,
        relation_set_sha256="f" * 64,
    )
    relation_artifact_data = relation_retrieval_artifact_file_bytes(relation_artifact)
    relation_artifact_sha256 = hashlib.sha256(relation_artifact_data).hexdigest()
    runtime_inputs = RuntimeReleaseInputs(
        environment="evaluation",
        source_commit=source_commit,
        image_reference=image_reference,
        backend_version="0.1.0",
        backend_root=_repository_root() / "fastapi_backend/app",
        answer_provider="hyperclova",
        hcx_queryplan_enabled=False,
        hcx_model="HCX-007",
        fund_execution_policy="locked",
        relation_retrieval_artifact=relation_artifact,
        relation_retrieval_artifact_file_sha256=relation_artifact_sha256,
    )
    manifest = build_agent_release_manifest(
        runtime_inputs,
        release_id="finance-agent-test-v1",
        generated_at_utc=datetime(2026, 8, 21, tzinfo=UTC),
    )
    manifest_data = manifest_file_bytes(manifest)
    (tmp_path / "agent-release-manifest.json").write_bytes(manifest_data)
    binding = DeploymentBinding(
        release_id=manifest.release_id,
        environment=manifest.environment,
        source_commit=manifest.source_commit,
        release_manifest_sha256=hashlib.sha256(manifest_data).hexdigest(),
        image_reference=image_reference,
        platform="linux/amd64",
        activation_generation=1,
        rollback=RollbackRelease(mode="initial_bootstrap"),
    )
    binding_data = deployment_binding_file_bytes(binding)
    (tmp_path / "deployment-binding.json").write_bytes(binding_data)
    _update_environment(
        env_file,
        FINANCE_RELATION_RETRIEVAL_ARTIFACT_SHA256=relation_artifact_sha256,
        FINANCE_DEPLOYMENT_BINDING_SHA256=hashlib.sha256(binding_data).hexdigest(),
    )
    calls: list[list[str]] = []
    monkeypatch.setattr(
        release_trust,
        "_cosign_binary",
        lambda: Path("/usr/local/bin/cosign"),
    )
    monkeypatch.setattr(
        release_trust,
        "_run_verification",
        lambda _binary, arguments: calls.append(arguments),
    )

    release_trust.verify_release_trust(env_file)

    assert len(calls) == 3
    assert manifest.components.knowledge_retrieval.relation.status == "activated"
    assert manifest.components.runtime_features.model.grounded_answer_operation_enabled is True
    assert manifest.components.runtime_features.model.queryplan_operation_enabled is False


@pytest.mark.parametrize(
    "updates",
    [
        {"FINANCE_BACKEND_FUND_EXECUTION_POLICY": "public_fund_v1_approved"},
        {"HCX_TIMEOUT_SECONDS": "30"},
        {"APP_ENV": "production"},
        {
            "FINANCE_BACKEND_HCX_QUERY_PLAN_ENABLED": "true",
            "FINANCE_AGENT_LLM_MODE": "evaluation",
            "LLM_PROVIDER": "hyperclova",
            "HCX_MODEL": "HCX-007",
        },
        {
            "FINANCE_BACKEND_ANSWER_PROVIDER": "hyperclova",
            "FINANCE_AGENT_LLM_MODE": "evaluation",
            "LLM_PROVIDER": "hyperclova",
            "HCX_MODEL": "HCX-007",
        },
    ],
)
def test_release_trust_rejects_environment_that_differs_from_signed_profile(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    updates: dict[str, str],
) -> None:
    env_file, _, _ = _fixture(tmp_path)
    _update_environment(env_file, **updates)
    calls: list[list[str]] = []
    monkeypatch.setattr(
        release_trust,
        "_cosign_binary",
        lambda: Path("/usr/local/bin/cosign"),
    )
    monkeypatch.setattr(
        release_trust,
        "_run_verification",
        lambda _binary, arguments: calls.append(arguments),
    )

    with pytest.raises(
        release_trust.ReleaseTrustError,
        match="does not match the signed AgentReleaseManifest",
    ):
        release_trust.verify_release_trust(env_file)

    assert len(calls) == 3


@pytest.mark.parametrize(
    "name",
    [
        "FINANCE_BACKEND_ANSWER_PROVIDER",
        "FINANCE_BACKEND_HCX_QUERY_PLAN_ENABLED",
        "FINANCE_BACKEND_FUND_EXECUTION_POLICY",
        "HCX_TIMEOUT_SECONDS",
    ],
)
def test_release_trust_rejects_missing_explicit_profile_identity(
    tmp_path: Path,
    name: str,
) -> None:
    env_file, _, _ = _fixture(tmp_path)
    _update_environment(env_file, **{name: None})

    with pytest.raises(
        release_trust.ReleaseTrustError,
        match=f"missing release trust settings: {name}",
    ):
        release_trust.verify_release_trust(env_file)


def test_release_trust_ignores_path_injected_cosign(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = tmp_path / "cosign"
    fake.write_text("#!/bin/sh\necho cosign v3.1.3\n", encoding="utf-8")
    fake.chmod(0o755)
    monkeypatch.setenv("PATH", f"{tmp_path}:{os.environ['PATH']}")
    monkeypatch.setattr(release_trust, "COSIGN_BINARY", fake)

    with pytest.raises(release_trust.ReleaseTrustError, match="deployment contract"):
        release_trust._cosign_binary()


def test_release_trust_rejects_manifest_not_bound_by_deployment_binding(tmp_path: Path) -> None:
    env_file, environment, capture = _fixture(tmp_path)
    manifest = tmp_path / "agent-release-manifest.json"
    manifest.write_text('{"release_id":"tampered"}\n', encoding="utf-8")

    completed = _run(env_file, environment)

    assert completed.returncode == 2
    assert "DeploymentBinding and AgentReleaseManifest differ" in completed.stderr
    assert not capture.exists()
