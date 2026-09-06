from __future__ import annotations

import base64
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
import yaml


def _repository_root() -> Path:
    return Path(__file__).resolve().parents[3]


def _script() -> Path:
    return _repository_root() / "src" / "fastapi_backend" / "scripts" / "release_ci.py"


def _run(
    *arguments: str, environment: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(_script()), *arguments],
        cwd=_repository_root(),
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )


def _valid_input_arguments(output: Path) -> list[str]:
    return [
        "validate-inputs",
        "--release-id",
        "finance-agent-eval-001",
        "--environment",
        "evaluation",
        "--registry",
        "team.kr.ncr.ntruss.com",
        "--repository",
        "finance-agent/backend",
        "--platform",
        "linux/amd64",
        "--activation-generation",
        "1",
        "--previous-binding-sha256",
        "",
        "--answer-provider",
        "hyperclova",
        "--hcx-queryplan-enabled",
        "false",
        "--hcx-intent-analysis-enabled",
        "true",
        "--hcx-semantic-resolver-enabled",
        "true",
        "--adaptive-semantic-enabled",
        "true",
        "--fund-execution-policy",
        "public_fund_v1_approved",
        "--schema-dense-index-sha256",
        "2807195ef8c233ab3218ff742a4f53e11728aa63b56bbeb6dce62bb178f6ae57",
        "--schema-dense-calibration-report-sha256",
        "8c41295bf0746bf395b0898863b406f18f6cbd8a42a4cc379069e88ab85c3a09",
        "--schema-dense-min-score",
        "0.340015",
        "--schema-dense-hclx-candidate-min-score",
        "0.3",
        "--schema-dense-minimum-margin",
        "0.034684",
        "--schema-dense-top-k",
        "10",
        "--python-base-image",
        "docker.io/library/python@sha256:" + "a" * 64,
        "--source-commit",
        "b" * 40,
        "--github-ref",
        "refs/heads/main",
        "--github-ref-protected",
        "true",
        "--github-output",
        str(output),
    ]


def _outputs(path: Path) -> dict[str, str]:
    return dict(line.split("=", 1) for line in path.read_text(encoding="utf-8").splitlines())


def _relation_artifact_data(**updates: str) -> bytes:
    payload = {
        "artifact_kind": "provided_product_relations",
        "index_sha256": "1" * 64,
        "approval_manifest_sha256": "2" * 64,
        "relation_set_sha256": "3" * 64,
        **updates,
    }
    return (
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
    ).encode("utf-8")


def _relation_artifact_environment(data: bytes) -> dict[str, str]:
    environment = os.environ.copy()
    environment.update(
        {
            "RELATION_RETRIEVAL_ARTIFACT_B64": base64.b64encode(data).decode("ascii"),
            "RELATION_RETRIEVAL_ARTIFACT_SHA256": hashlib.sha256(data).hexdigest(),
        }
    )
    return environment


def _manifest_with_relation(
    artifact_data: bytes,
    *,
    approved_contract_sha256: str | None = None,
    artifact_file_sha256: str | None = None,
) -> bytes:
    artifact = json.loads(artifact_data)
    payload = {
        "components": {
            "approved_datasets": {
                "manifest": {
                    "contract_sha256": approved_contract_sha256
                    or artifact["approval_manifest_sha256"]
                }
            },
            "knowledge_retrieval": {
                "relation": {
                    "status": "activated",
                    "artifact": artifact,
                    "artifact_file_sha256": artifact_file_sha256
                    or hashlib.sha256(artifact_data).hexdigest(),
                }
            },
        }
    }
    return (json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")


def test_release_ci_accepts_only_normalized_protected_main_inputs(tmp_path: Path) -> None:
    output = tmp_path / "github-output"
    output.touch()

    completed = _run(*_valid_input_arguments(output))

    assert completed.returncode == 0, completed.stderr
    values = _outputs(output)
    assert values["image_repository"] == "team.kr.ncr.ntruss.com/finance-agent/backend"
    assert values["release_tag"].endswith(":finance-agent-eval-001")
    assert values["rollback_mode"] == "initial_bootstrap"
    assert values["answer_provider"] == "hyperclova"
    assert values["hcx_queryplan_enabled"] == "false"
    assert values["hcx_intent_analysis_enabled"] == "true"
    assert values["hcx_semantic_resolver_enabled"] == "true"
    assert values["adaptive_semantic_enabled"] == "true"
    assert values["fund_execution_policy"] == "public_fund_v1_approved"
    assert values["schema_dense_index_sha256"] == (
        "2807195ef8c233ab3218ff742a4f53e11728aa63b56bbeb6dce62bb178f6ae57"
    )
    assert values["schema_dense_calibration_report_sha256"] == (
        "8c41295bf0746bf395b0898863b406f18f6cbd8a42a4cc379069e88ab85c3a09"
    )
    assert values["schema_dense_min_score"] == "0.340015"
    assert values["schema_dense_hclx_candidate_min_score"] == "0.3"
    assert values["schema_dense_minimum_margin"] == "0.034684"
    assert values["schema_dense_top_k"] == "10"
    assert values["model_id"] == "HCX-007"


@pytest.mark.parametrize(
    ("option", "value"),
    [
        ("--answer-provider", "deterministic"),
        ("--hcx-queryplan-enabled", "true"),
        ("--hcx-intent-analysis-enabled", "false"),
        ("--hcx-semantic-resolver-enabled", "false"),
        ("--adaptive-semantic-enabled", "false"),
        ("--fund-execution-policy", "locked"),
        ("--schema-dense-index-sha256", "a" * 64),
        ("--schema-dense-calibration-report-sha256", "b" * 64),
        ("--schema-dense-min-score", "0.9"),
        ("--schema-dense-hclx-candidate-min-score", "0.31"),
        ("--schema-dense-minimum-margin", "0.1"),
        ("--schema-dense-top-k", "5"),
    ],
)
def test_release_ci_rejects_profiles_outside_the_frozen_final_boundary(
    tmp_path: Path,
    option: str,
    value: str,
) -> None:
    output = tmp_path / "github-output"
    output.touch()
    arguments = _valid_input_arguments(output)
    arguments[arguments.index(option) + 1] = value

    completed = _run(*arguments)

    assert completed.returncode != 0
    assert "exact KURE candidate-only policy" in completed.stderr
    assert output.read_text(encoding="utf-8") == ""


def test_release_ci_prevents_rebootstrap_and_requires_previous_anchor(
    tmp_path: Path,
) -> None:
    output = tmp_path / "github-output"
    output.touch()
    bootstrap = _valid_input_arguments(output)
    bootstrap[bootstrap.index("--previous-binding-sha256") + 1] = "a" * 64

    rejected_bootstrap = _run(*bootstrap)

    assert rejected_bootstrap.returncode != 0
    assert "bootstrap is forbidden" in rejected_bootstrap.stderr

    output.write_text("", encoding="utf-8")
    continuation = _valid_input_arguments(output)
    continuation[continuation.index("--activation-generation") + 1] = "2"
    rejected_continuation = _run(*continuation)
    assert rejected_continuation.returncode != 0
    assert "previous Binding SHA-256 is invalid" in rejected_continuation.stderr

    continuation[continuation.index("--previous-binding-sha256") + 1] = "b" * 64
    accepted_continuation = _run(*continuation)
    assert accepted_continuation.returncode == 0, accepted_continuation.stderr
    assert _outputs(output)["rollback_mode"] == "pinned_previous_release"


@pytest.mark.parametrize(
    ("option", "value", "message"),
    [
        ("--registry", "team.kr.ncr.ntruss.com;id", "NCP registry is invalid"),
        ("--repository", "finance agent", "repository is invalid"),
        ("--release-id", "../../escape", "release ID is invalid"),
        ("--github-ref", "refs/pull/7/merge", "allowed only"),
        ("--github-ref-protected", "false", "must be protected"),
        ("--activation-generation", "01", "generation is invalid"),
        ("--python-base-image", "python:3.12", "base image is invalid"),
    ],
)
def test_release_ci_rejects_untrusted_or_mutable_inputs(
    tmp_path: Path,
    option: str,
    value: str,
    message: str,
) -> None:
    output = tmp_path / "github-output"
    output.touch()
    arguments = _valid_input_arguments(output)
    arguments[arguments.index(option) + 1] = value

    completed = _run(*arguments)

    assert completed.returncode != 0
    assert message in completed.stderr
    assert output.read_text(encoding="utf-8") == ""


def test_release_ci_binds_only_a_registry_sha256_digest(tmp_path: Path) -> None:
    output = tmp_path / "github-output"
    output.touch()
    completed = _run(
        "bind-digest",
        "--digest",
        "sha256:" + "c" * 64,
        "--image-repository",
        "team.kr.ncr.ntruss.com/finance-agent",
        "--github-output",
        str(output),
    )

    assert completed.returncode == 0, completed.stderr
    assert _outputs(output)["image_reference"] == (
        "team.kr.ncr.ntruss.com/finance-agent@sha256:" + "c" * 64
    )

    rejected = _run(
        "bind-digest",
        "--digest",
        "sha256:" + "c" * 63 + ";id",
        "--image-repository",
        "team.kr.ncr.ntruss.com/finance-agent",
        "--github-output",
        str(output),
    )
    assert rejected.returncode != 0


def test_release_ci_verifies_exact_image_platform_digest_and_labels(tmp_path: Path) -> None:
    image_reference = "team.kr.ncr.ntruss.com/finance-agent@sha256:" + "d" * 64
    source_commit = "e" * 40
    release_id = "finance-agent-eval-002"
    python_base = "docker.io/library/python@sha256:" + "f" * 64
    backend_base = "team.kr.ncr.ntruss.com/finance-agent@sha256:" + "a" * 64
    inspect_file = tmp_path / "inspect.json"
    inspect_file.write_text(
        json.dumps(
            [
                {
                    "Os": "linux",
                    "Architecture": "amd64",
                    "RepoDigests": [image_reference],
                    "Config": {
                        "Labels": {
                            "org.opencontainers.image.revision": source_commit,
                            "ai.gaeng3.finance.release.id": release_id,
                            "org.opencontainers.image.base.name": backend_base,
                            "ai.gaeng3.finance.python-base.name": python_base,
                        }
                    },
                }
            ]
        ),
        encoding="utf-8",
    )

    arguments = [
        "inspect-image",
        "--inspect-file",
        str(inspect_file),
        "--image-reference",
        image_reference,
        "--platform",
        "linux/amd64",
        "--source-commit",
        source_commit,
        "--release-id",
        release_id,
        "--backend-base-image",
        backend_base,
        "--python-base-image",
        python_base,
    ]
    completed = _run(*arguments)
    assert completed.returncode == 0, completed.stderr

    payload = json.loads(inspect_file.read_text(encoding="utf-8"))
    payload[0]["Config"]["Labels"]["org.opencontainers.image.revision"] = "0" * 40
    inspect_file.write_text(json.dumps(payload), encoding="utf-8")
    rejected = _run(*arguments)
    assert rejected.returncode != 0
    assert "image label differs" in rejected.stderr


def test_release_ci_requires_exactly_one_remote_linux_amd64_runtime_manifest(
    tmp_path: Path,
) -> None:
    image_reference = "team.kr.ncr.ntruss.com/finance-agent@sha256:" + "d" * 64
    manifest_file = tmp_path / "remote-manifest.json"
    payload = {
        "schemaVersion": 2,
        "mediaType": "application/vnd.oci.image.index.v1+json",
        "manifests": [
            {
                "mediaType": "application/vnd.oci.image.manifest.v1+json",
                "digest": "sha256:" + "1" * 64,
                "platform": {"os": "linux", "architecture": "amd64"},
            },
            {
                "mediaType": "application/vnd.oci.image.manifest.v1+json",
                "digest": "sha256:" + "2" * 64,
                "platform": {"os": "unknown", "architecture": "unknown"},
                "annotations": {"vnd.docker.reference.type": "attestation-manifest"},
            },
        ],
    }
    manifest_file.write_text(json.dumps(payload), encoding="utf-8")
    arguments = [
        "inspect-remote-manifest",
        "--manifest-file",
        str(manifest_file),
        "--image-reference",
        image_reference,
        "--platform",
        "linux/amd64",
    ]

    completed = _run(*arguments)
    assert completed.returncode == 0, completed.stderr

    payload["manifests"].append(
        {
            "mediaType": "application/vnd.oci.image.manifest.v1+json",
            "digest": "sha256:" + "3" * 64,
            "platform": {"os": "linux", "architecture": "arm64"},
        }
    )
    manifest_file.write_text(json.dumps(payload), encoding="utf-8")
    rejected = _run(*arguments)
    assert rejected.returncode != 0
    assert "exactly one linux/amd64" in rejected.stderr


def test_previous_binding_requires_external_sha256_anchor_and_read_only_output(
    tmp_path: Path,
) -> None:
    data = b'{"release_id":"finance-agent-eval-001"}\n'
    output = tmp_path / "previous-binding.json"
    environment = os.environ.copy()
    environment.update(
        {
            "PREVIOUS_DEPLOYMENT_BINDING_B64": base64.b64encode(data).decode("ascii"),
            "PREVIOUS_DEPLOYMENT_BINDING_SHA256": hashlib.sha256(data).hexdigest(),
        }
    )

    completed = _run(
        "materialize-previous-binding",
        "--output",
        str(output),
        environment=environment,
    )

    assert completed.returncode == 0, completed.stderr
    assert output.read_bytes() == data
    assert output.stat().st_mode & 0o222 == 0

    bad_environment = {**environment, "PREVIOUS_DEPLOYMENT_BINDING_SHA256": "0" * 64}
    rejected = _run(
        "materialize-previous-binding",
        "--output",
        str(tmp_path / "rejected.json"),
        environment=bad_environment,
    )
    assert rejected.returncode != 0
    assert "trusted SHA-256" in rejected.stderr


def test_relation_artifact_requires_external_anchor_and_canonical_read_only_output(
    tmp_path: Path,
) -> None:
    data = _relation_artifact_data()
    output = tmp_path / "relation-retrieval-artifact.json"
    github_output = tmp_path / "github-output"
    github_output.touch()
    environment = _relation_artifact_environment(data)

    completed = _run(
        "materialize-relation-artifact",
        "--output",
        str(output),
        "--github-output",
        str(github_output),
        environment=environment,
    )

    assert completed.returncode == 0, completed.stderr
    assert completed.stdout == ""
    assert completed.stderr == ""
    assert output.read_bytes() == data
    assert output.stat().st_mode & 0o222 == 0
    assert output.stat().st_nlink == 1
    assert _outputs(github_output) == {
        "artifact_path": str(output),
        "artifact_sha256": hashlib.sha256(data).hexdigest(),
    }


@pytest.mark.parametrize(
    ("encoded", "trusted_sha256", "message"),
    [
        ("", "1" * 64, "size is invalid"),
        ("not-base64%%%", "1" * 64, "not strict base64"),
        (
            base64.b64encode(_relation_artifact_data()).decode("ascii"),
            "0" * 64,
            "differs from its trusted SHA-256",
        ),
        (
            base64.b64encode(
                json.dumps(
                    json.loads(_relation_artifact_data()),
                    ensure_ascii=False,
                    indent=2,
                ).encode("utf-8")
            ).decode("ascii"),
            hashlib.sha256(
                json.dumps(
                    json.loads(_relation_artifact_data()),
                    ensure_ascii=False,
                    indent=2,
                ).encode("utf-8")
            ).hexdigest(),
            "not canonical JSON",
        ),
        (
            base64.b64encode(
                b'{"artifact_kind":"provided_product_relations",'
                b'"artifact_kind":"provided_product_relations",'
                b'"approval_manifest_sha256":"' + b"2" * 64 + b'",'
                b'"index_sha256":"' + b"1" * 64 + b'",'
                b'"relation_set_sha256":"' + b"3" * 64 + b'"}\n'
            ).decode("ascii"),
            hashlib.sha256(
                b'{"artifact_kind":"provided_product_relations",'
                b'"artifact_kind":"provided_product_relations",'
                b'"approval_manifest_sha256":"' + b"2" * 64 + b'",'
                b'"index_sha256":"' + b"1" * 64 + b'",'
                b'"relation_set_sha256":"' + b"3" * 64 + b'"}\n'
            ).hexdigest(),
            "duplicate JSON key",
        ),
        (
            base64.b64encode(_relation_artifact_data(extra="4" * 64)).decode("ascii"),
            hashlib.sha256(_relation_artifact_data(extra="4" * 64)).hexdigest(),
            "schema is invalid",
        ),
        (
            base64.b64encode(b"{" + b" " * (4 * 1024)).decode("ascii"),
            hashlib.sha256(b"{" + b" " * (4 * 1024)).hexdigest(),
            "size is invalid",
        ),
    ],
)
def test_relation_artifact_rejects_missing_malformed_tampered_or_noncanonical_material(
    tmp_path: Path,
    encoded: str,
    trusted_sha256: str,
    message: str,
) -> None:
    output = tmp_path / "relation-retrieval-artifact.json"
    github_output = tmp_path / "github-output"
    github_output.touch()
    environment = os.environ.copy()
    environment.update(
        {
            "RELATION_RETRIEVAL_ARTIFACT_B64": encoded,
            "RELATION_RETRIEVAL_ARTIFACT_SHA256": trusted_sha256,
        }
    )

    completed = _run(
        "materialize-relation-artifact",
        "--output",
        str(output),
        "--github-output",
        str(github_output),
        environment=environment,
    )

    assert completed.returncode != 0
    assert message in completed.stderr
    if encoded:
        assert encoded not in completed.stdout
        assert encoded not in completed.stderr
    assert not output.exists()
    assert github_output.read_text(encoding="utf-8") == ""


def test_relation_artifact_rejects_missing_sha256_and_unsafe_output_paths(
    tmp_path: Path,
) -> None:
    data = _relation_artifact_data()
    github_output = tmp_path / "github-output"
    github_output.touch()
    missing_sha = _relation_artifact_environment(data)
    missing_sha.pop("RELATION_RETRIEVAL_ARTIFACT_SHA256")
    missing_output = tmp_path / "missing-sha.json"

    rejected_missing = _run(
        "materialize-relation-artifact",
        "--output",
        str(missing_output),
        "--github-output",
        str(github_output),
        environment=missing_sha,
    )

    assert rejected_missing.returncode != 0
    assert "SHA-256 is invalid" in rejected_missing.stderr
    assert not missing_output.exists()

    existing = tmp_path / "existing.json"
    existing.write_bytes(b"do-not-replace\n")
    rejected_existing = _run(
        "materialize-relation-artifact",
        "--output",
        str(existing),
        "--github-output",
        str(github_output),
        environment=_relation_artifact_environment(data),
    )
    assert rejected_existing.returncode != 0
    assert existing.read_bytes() == b"do-not-replace\n"

    target = tmp_path / "target.json"
    target.write_bytes(b"do-not-follow\n")
    symlink = tmp_path / "relation-link.json"
    symlink.symlink_to(target)
    rejected_symlink = _run(
        "materialize-relation-artifact",
        "--output",
        str(symlink),
        "--github-output",
        str(github_output),
        environment=_relation_artifact_environment(data),
    )
    assert rejected_symlink.returncode != 0
    assert target.read_bytes() == b"do-not-follow\n"
    assert symlink.is_symlink()


def test_relation_artifact_must_belong_to_the_manifest_approved_dataset(tmp_path: Path) -> None:
    artifact_data = _relation_artifact_data()
    artifact = tmp_path / "relation-retrieval-artifact.json"
    artifact.write_bytes(artifact_data)
    artifact.chmod(0o444)
    artifact_sha256 = hashlib.sha256(artifact_data).hexdigest()
    manifest = tmp_path / "agent-release-manifest.json"
    manifest.write_bytes(_manifest_with_relation(artifact_data))

    accepted = _run(
        "verify-relation-manifest-binding",
        "--manifest",
        str(manifest),
        "--relation-artifact",
        str(artifact),
        "--relation-artifact-sha256",
        artifact_sha256,
    )

    assert accepted.returncode == 0, accepted.stderr
    assert accepted.stdout == ""
    assert accepted.stderr == ""

    stale_manifest = tmp_path / "stale-agent-release-manifest.json"
    stale_manifest.write_bytes(
        _manifest_with_relation(
            artifact_data,
            approved_contract_sha256="4" * 64,
        )
    )
    rejected_stale = _run(
        "verify-relation-manifest-binding",
        "--manifest",
        str(stale_manifest),
        "--relation-artifact",
        str(artifact),
        "--relation-artifact-sha256",
        artifact_sha256,
    )

    assert rejected_stale.returncode != 0
    assert "different approved dataset manifest" in rejected_stale.stderr


def test_relation_manifest_binding_rejects_a_tampered_public_artifact(tmp_path: Path) -> None:
    artifact_data = _relation_artifact_data()
    artifact = tmp_path / "relation-retrieval-artifact.json"
    artifact.write_bytes(artifact_data)
    artifact.chmod(0o444)
    artifact_sha256 = hashlib.sha256(artifact_data).hexdigest()
    manifest = tmp_path / "agent-release-manifest.json"
    manifest.write_bytes(
        _manifest_with_relation(
            artifact_data,
            artifact_file_sha256="5" * 64,
        )
    )

    rejected = _run(
        "verify-relation-manifest-binding",
        "--manifest",
        str(manifest),
        "--relation-artifact",
        str(artifact),
        "--relation-artifact-sha256",
        artifact_sha256,
    )

    assert rejected.returncode != 0
    assert "does not bind the trusted relation artifact" in rejected.stderr


def test_manual_release_path_supports_cache_only_and_adaptive_release() -> None:
    """2026-09-06 팀 결정: 서버 한 대에서 끝나는 수동 배포가 최종 평가 배포 경로다.

    스크립트는 자동 빌드와 같은 봉인 검사(AgentReleaseManifest·DeploymentBinding·
    외부지식 아티팩트)를 거치며, 캐시 전용 외부 근거 모드와 adaptive(KURE) 이미지
    타깃을 지원해야 한다. 키 값은 파일로만 전달하고 어떤 단계에서도 출력하지 않는다.
    """

    script = (
        _repository_root() / "src" / "fastapi_backend" / "scripts" / "manual-release.sh"
    ).read_text(encoding="utf-8")

    assert "set -euo pipefail" in script
    assert "approved_corpus_cache_only" in script
    assert "verified_live_official_with_relation_graph" in script
    assert '--target "$IMAGE_TARGET"' in script
    assert "adaptive-runtime" in script
    assert "finance_agent_core.release_cli manifest" in script
    assert "finance_agent_core.release_cli binding" in script
    assert "external_knowledge_release_cli" in script
    assert "--hcx-intent-analysis-enabled" in script
    assert "CLOVASTUDIO_API_KEY_FILE=/run/secrets/clovastudio_api_key" in script
    assert 'cat "$SECRETS_DIR' not in script
    assert 'echo "$SECRETS_DIR' not in script
    # 캐시 전용 모드 분기는 NAVER 자격증명을 마운트하지 않는다
    cache_branch = script.split(
        'if [ "$EXTERNAL_MODE" = approved_corpus_cache_only ]; then\n        EXT_ARGS+='
    )[1].split("    else")[0]
    assert "NAVER_API_HUB" not in cache_branch


def test_base_dockerfile_pins_the_python_index_and_records_source_revision() -> None:
    dockerfile = (_repository_root() / "src" / "fastapi_backend" / "Dockerfile").read_text(
        encoding="utf-8"
    )

    assert "ARG PYTHON_BASE_IMAGE=docker.io/library/python@sha256:" in dockerfile
    assert "FROM ${PYTHON_BASE_IMAGE}" in dockerfile
    assert 'org.opencontainers.image.revision="${FINANCE_SOURCE_COMMIT}"' in dockerfile
    assert "python:3.12-slim-bookworm" not in dockerfile
