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
    return Path(__file__).resolve().parents[2]


def _script() -> Path:
    return _repository_root() / "fastapi_backend" / "scripts" / "release_ci.py"


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
        "--hcx-semantic-resolver-enabled",
        "true",
        "--adaptive-semantic-enabled",
        "true",
        "--fund-execution-policy",
        "public_fund_v1_approved",
        "--schema-dense-index-sha256",
        "9463ce21d14341e3dca0e44bc5ca3e2e085309ef81513a3802e175fa198de306",
        "--schema-dense-calibration-report-sha256",
        "b6cd6e1c4c371929306499ec4efaba8b9a29934ec40d6d300ad8a9d2d93c4d60",
        "--schema-dense-min-score",
        "1.0",
        "--schema-dense-hclx-candidate-min-score",
        "0.361907478",
        "--schema-dense-minimum-margin",
        "2.0",
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
    assert values["hcx_semantic_resolver_enabled"] == "true"
    assert values["adaptive_semantic_enabled"] == "true"
    assert values["fund_execution_policy"] == "public_fund_v1_approved"
    assert values["schema_dense_index_sha256"] == (
        "9463ce21d14341e3dca0e44bc5ca3e2e085309ef81513a3802e175fa198de306"
    )
    assert values["schema_dense_calibration_report_sha256"] == (
        "b6cd6e1c4c371929306499ec4efaba8b9a29934ec40d6d300ad8a9d2d93c4d60"
    )
    assert values["schema_dense_min_score"] == "1.0"
    assert values["schema_dense_hclx_candidate_min_score"] == "0.361907478"
    assert values["schema_dense_minimum_margin"] == "2.0"
    assert values["schema_dense_top_k"] == "10"
    assert values["model_id"] == "HCX-007"


@pytest.mark.parametrize(
    ("option", "value"),
    [
        ("--answer-provider", "deterministic"),
        ("--hcx-queryplan-enabled", "true"),
        ("--hcx-semantic-resolver-enabled", "false"),
        ("--adaptive-semantic-enabled", "false"),
        ("--fund-execution-policy", "locked"),
        ("--schema-dense-index-sha256", "a" * 64),
        ("--schema-dense-calibration-report-sha256", "b" * 64),
        ("--schema-dense-min-score", "0.9"),
        ("--schema-dense-hclx-candidate-min-score", "0.3"),
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


def test_release_metadata_records_the_promoted_relation_artifact_sha256(tmp_path: Path) -> None:
    output = tmp_path / "release-metadata.json"
    relation_sha256 = "9" * 64
    arguments = [
        "write-metadata",
        "--output",
        str(output),
        "--workflow-identity",
        (
            "https://github.com/Hyunhoim/gaeng3/.github/workflows/"
            "immutable-ncp-release.yml@refs/heads/main"
        ),
        "--source-commit",
        "a" * 40,
        "--release-id",
        "finance-agent-eval-001",
        "--environment",
        "evaluation",
        "--platform",
        "linux/amd64",
        "--base-image-reference",
        "team.kr.ncr.ntruss.com/finance-agent@sha256:" + "b" * 64,
        "--release-image-reference",
        "team.kr.ncr.ntruss.com/finance-agent@sha256:" + "c" * 64,
        "--manifest-sha256",
        "d" * 64,
        "--binding-sha256",
        "e" * 64,
        "--relation-artifact-sha256",
        relation_sha256,
        "--github-run-id",
        "12345",
        "--github-run-attempt",
        "1",
    ]

    completed = _run(*arguments)

    assert completed.returncode == 0, completed.stderr
    metadata = json.loads(output.read_bytes())
    assert metadata["schema_version"] == "1.1"
    assert metadata["relation_retrieval_artifact_sha256"] == relation_sha256

    invalid_output = tmp_path / "invalid-release-metadata.json"
    invalid = list(arguments)
    invalid[invalid.index("--output") + 1] = str(invalid_output)
    invalid[invalid.index("--relation-artifact-sha256") + 1] = "not-a-sha256"
    rejected = _run(*invalid)
    assert rejected.returncode != 0
    assert "relation retrieval artifact SHA-256 is invalid" in rejected.stderr
    assert not invalid_output.exists()


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


def test_release_workflow_materializes_and_binds_the_approved_relation_artifact() -> None:
    workflow_path = _repository_root() / ".github" / "workflows" / "immutable-ncp-release.yml"
    workflow = yaml.safe_load(workflow_path.read_text(encoding="utf-8"))
    event_config = workflow.get("on", workflow.get(True))
    dispatch_inputs = event_config["workflow_dispatch"]["inputs"]
    steps = workflow["jobs"]["release"]["steps"]
    relation_step = next(step for step in steps if step.get("id") == "relation_artifact")
    manifest_step = next(
        step
        for step in steps
        if step.get("name") == "Create AgentReleaseManifest from the clean checkout"
    )
    metadata_step = next(
        step for step in steps if step.get("name") == "Record non-secret release evidence"
    )
    registry_login_step = next(
        step for step in steps if str(step.get("uses", "")).startswith("docker/login-action@")
    )

    assert "relation_artifact" not in dispatch_inputs
    assert "relation_artifact_sha256" not in dispatch_inputs
    assert steps.index(relation_step) < steps.index(registry_login_step)
    assert steps.index(manifest_step) < steps.index(registry_login_step)
    assert relation_step["env"] == {
        "RELATION_RETRIEVAL_ARTIFACT_B64": (
            "${{ secrets.APPROVED_RELATION_RETRIEVAL_ARTIFACT_B64 }}"
        ),
        "RELATION_RETRIEVAL_ARTIFACT_SHA256": (
            "${{ vars.APPROVED_RELATION_RETRIEVAL_ARTIFACT_SHA256 }}"
        ),
    }
    assert "release_ci.py materialize-relation-artifact" in relation_step["run"]
    assert '"$RUNNER_TEMP/relation-retrieval-artifact.json"' in relation_step["run"]
    assert "RELATION_RETRIEVAL_ARTIFACT_B64" not in relation_step["run"]
    assert manifest_step["env"]["RELATION_RETRIEVAL_ARTIFACT_FILE"] == (
        "${{ steps.relation_artifact.outputs.artifact_path }}"
    )
    assert manifest_step["env"]["RELATION_RETRIEVAL_ARTIFACT_SHA256"] == (
        "${{ steps.relation_artifact.outputs.artifact_sha256 }}"
    )
    assert (
        '--relation-retrieval-artifact "$RELATION_RETRIEVAL_ARTIFACT_FILE"' in manifest_step["run"]
    )
    assert (
        '--relation-retrieval-artifact-sha256 "$RELATION_RETRIEVAL_ARTIFACT_SHA256"'
        in manifest_step["run"]
    )
    assert '--fund-execution-policy "$FUND_EXECUTION_POLICY"' in manifest_step["run"]
    assert "release_ci.py verify-relation-manifest-binding" in manifest_step["run"]
    assert '--manifest "$RELEASE_OUTPUT_DIR/agent-release-manifest.json"' in manifest_step["run"]
    assert '--relation-artifact "$RELATION_RETRIEVAL_ARTIFACT_FILE"' in manifest_step["run"]
    assert (
        '--relation-artifact-sha256 "$RELATION_RETRIEVAL_ARTIFACT_SHA256"' in manifest_step["run"]
    )
    assert metadata_step["env"]["RELATION_RETRIEVAL_ARTIFACT_SHA256"] == (
        "${{ steps.relation_artifact.outputs.artifact_sha256 }}"
    )
    assert (
        '--relation-artifact-sha256 "$RELATION_RETRIEVAL_ARTIFACT_SHA256"' in metadata_step["run"]
    )


def test_release_workflow_binds_exact_adaptive_runtime_artifacts() -> None:
    workflow_path = _repository_root() / ".github" / "workflows" / "immutable-ncp-release.yml"
    workflow = yaml.safe_load(workflow_path.read_text(encoding="utf-8"))
    release = workflow["jobs"]["release"]
    steps = release["steps"]
    adaptive_step = next(step for step in steps if step.get("id") == "adaptive_artifacts")
    manifest_step = next(
        step
        for step in steps
        if step.get("name") == "Create AgentReleaseManifest from the clean checkout"
    )
    base_build = next(step for step in steps if step.get("id") == "base_build")
    evidence_step = next(
        step for step in steps if step.get("name") == "Record non-secret release evidence"
    )
    upload_step = next(
        step for step in steps if step.get("name") == "Upload non-secret immutable release evidence"
    )

    assert release["env"]["FINAL_HCX_SEMANTIC_RESOLVER_ENABLED"] == "true"
    assert release["env"]["FINAL_ADAPTIVE_SEMANTIC_ENABLED"] == "true"
    assert release["env"]["FINAL_SCHEMA_DENSE_MIN_SCORE"] == "1.0"
    assert release["env"]["FINAL_SCHEMA_DENSE_MINIMUM_MARGIN"] == "2.0"
    assert base_build["with"]["target"] == "adaptive-runtime"
    assert steps.index(adaptive_step) < steps.index(base_build)
    assert "sha256sum" in adaptive_step["run"]
    assert "install -m 0444" in adaptive_step["run"]
    assert '--schema-dense-index "$SCHEMA_DENSE_INDEX"' in manifest_step["run"]
    assert "--schema-dense-calibration-report-sha256" in manifest_step["run"]
    for artifact_name in (
        "schema-dense-index.json",
        "schema-dense-calibration-report.json",
        "kure-snapshot-manifest.json",
    ):
        assert artifact_name in evidence_step["run"]
        assert artifact_name in upload_step["with"]["path"]


def test_release_workflow_has_a_commit_pinned_keyless_trust_boundary() -> None:
    workflow_path = _repository_root() / ".github" / "workflows" / "immutable-ncp-release.yml"
    text = workflow_path.read_text(encoding="utf-8")
    workflow = yaml.safe_load(text)
    release = workflow["jobs"]["release"]
    steps = release["steps"]
    uses = [step["uses"] for step in steps if "uses" in step]

    assert set(uses) == {
        "actions/checkout@11d5960a326750d5838078e36cf38b85af677262",
        "docker/login-action@c94ce9fb468520275223c153574b00df6fe4bcc9",
        "docker/setup-buildx-action@8d2750c68a42422c14e847fe6c8ac0403b4cbd6f",
        "docker/build-push-action@10e90e3645eae34f1e60eeb005ba3a3d33f178e8",
        "sigstore/cosign-installer@6f9f17788090df1f26f669e9d70d6ae9567deba6",
        "actions/upload-artifact@ea165f8d65b6e75b540449e92b4886f43607fa02",
    }
    assert release["permissions"] == {"contents": "read", "id-token": "write"}
    assert release["runs-on"] == "ubuntu-24.04"
    assert "pull_request:" not in text
    assert "refs/heads/main" in release["env"]["WORKFLOW_IDENTITY"]
    assert '--certificate-identity "$WORKFLOW_IDENTITY"' in text
    assert '--certificate-oidc-issuer "$OIDC_ISSUER"' in text
    assert "cosign sign-blob --yes" in text
    image_sign_step = next(
        step
        for step in steps
        if step.get("name") == "Keyless-sign and verify both immutable images"
    )
    assert image_sign_step["env"]["COSIGN_DOCKER_MEDIA_TYPES"] == "1"
    assert image_sign_step["run"].count("--registry-referrers-mode=legacy") == 2
    assert image_sign_step["run"].count("--new-bundle-format=false") == 4
    assert image_sign_step["run"].count("--use-signing-config=false") == 2
    assert "PREVIOUS_DEPLOYMENT_BINDING_SHA256" in text
    assert "TRUSTED_NCP_REGISTRY: ${{ vars.NCP_REGISTRY_HOST }}" in text
    assert "TRUSTED_NCP_REPOSITORY: ${{ vars.NCP_IMAGE_REPOSITORY }}" in text
    event_config = workflow.get("on", workflow.get(True))
    dispatch_inputs = event_config["workflow_dispatch"]["inputs"]
    assert "registry" not in dispatch_inputs
    assert "repository" not in dispatch_inputs
    assert "answer_provider" not in dispatch_inputs
    assert "hcx_queryplan_enabled" not in dispatch_inputs
    assert "fund_execution_policy" not in dispatch_inputs
    assert release["env"]["FINAL_ANSWER_PROVIDER"] == "hyperclova"
    assert release["env"]["FINAL_HCX_QUERYPLAN_ENABLED"] == "false"
    assert release["env"]["FINAL_HCX_SEMANTIC_RESOLVER_ENABLED"] == "true"
    assert release["env"]["FINAL_ADAPTIVE_SEMANTIC_ENABLED"] == "true"
    assert release["env"]["FINAL_FUND_EXECUTION_POLICY"] == "public_fund_v1_approved"
    assert "check-submission-boundary.py" in text
    assert "--profile development" in text

    for step in steps:
        shell = step.get("run", "")
        assert "${{ inputs." not in shell
        assert "${{ secrets." not in shell


def test_release_workflow_pushes_then_uses_only_exact_image_digests() -> None:
    text = (_repository_root() / ".github" / "workflows" / "immutable-ncp-release.yml").read_text(
        encoding="utf-8"
    )

    assert text.count("push: true") == 2
    assert text.count("provenance: mode=max") == 2
    assert text.count("sbom: true") == 2
    assert text.count("release_ci.py bind-digest") == 2
    assert 'docker pull --platform "$RUNTIME_PLATFORM" "$IMAGE_REFERENCE"' in text
    assert "release_ci.py inspect-image" in text
    assert "release_ci.py inspect-remote-manifest" in text
    assert "Verify the exact image runtime boundary" in text
    assert "fastapi_backend/scripts/image_runtime_boundary.py" in text
    assert "--network none" in text
    assert "--read-only" in text
    assert "--cap-drop ALL" in text
    assert "no-new-privileges:true" in text
    assert '--env FINANCE_RUNTIME_IMAGE_REFERENCE="$IMAGE_REFERENCE"' in text
    assert '--env FINANCE_RELEASE_ID="$RELEASE_ID"' in text
    assert '--env FINANCE_SOURCE_COMMIT="$SOURCE_COMMIT"' in text
    assert '--env FINANCE_RELEASE_MANIFEST_SHA256="$manifest_sha256"' in text
    assert 'sha256sum "$RELEASE_OUTPUT_DIR/agent-release-manifest.json"' in text
    assert "image-runtime-boundary.json" in text
    assert "BACKEND_BASE_IMAGE=${{ steps.base_image.outputs.image_reference }}" in text
    assert "image_reference: ${{ steps.release_image.outputs.image_reference }}" not in text


def test_base_dockerfile_pins_the_python_index_and_records_source_revision() -> None:
    dockerfile = (_repository_root() / "fastapi_backend" / "Dockerfile").read_text(encoding="utf-8")

    assert "ARG PYTHON_BASE_IMAGE=docker.io/library/python@sha256:" in dockerfile
    assert "FROM ${PYTHON_BASE_IMAGE}" in dockerfile
    assert 'org.opencontainers.image.revision="${FINANCE_SOURCE_COMMIT}"' in dockerfile
    assert "python:3.12-slim-bookworm" not in dockerfile
