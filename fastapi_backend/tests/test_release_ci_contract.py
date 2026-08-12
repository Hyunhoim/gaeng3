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
        "deterministic",
        "--hcx-queryplan-enabled",
        "false",
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


def test_release_ci_accepts_only_normalized_protected_main_inputs(tmp_path: Path) -> None:
    output = tmp_path / "github-output"
    output.touch()

    completed = _run(*_valid_input_arguments(output))

    assert completed.returncode == 0, completed.stderr
    values = _outputs(output)
    assert values["image_repository"] == "team.kr.ncr.ntruss.com/finance-agent/backend"
    assert values["release_tag"].endswith(":finance-agent-eval-001")
    assert values["rollback_mode"] == "initial_bootstrap"
    assert values["model_id"] == "disabled"


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
    assert "PREVIOUS_DEPLOYMENT_BINDING_SHA256" in text
    assert "TRUSTED_NCP_REGISTRY: ${{ vars.NCP_REGISTRY_HOST }}" in text
    assert "TRUSTED_NCP_REPOSITORY: ${{ vars.NCP_IMAGE_REPOSITORY }}" in text
    event_config = workflow.get("on", workflow.get(True))
    dispatch_inputs = event_config["workflow_dispatch"]["inputs"]
    assert "registry" not in dispatch_inputs
    assert "repository" not in dispatch_inputs

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
    assert "BACKEND_BASE_IMAGE=${{ steps.base_image.outputs.image_reference }}" in text
    assert "image_reference: ${{ steps.release_image.outputs.image_reference }}" not in text


def test_base_dockerfile_pins_the_python_index_and_records_source_revision() -> None:
    dockerfile = (_repository_root() / "fastapi_backend" / "Dockerfile").read_text(encoding="utf-8")

    assert "ARG PYTHON_BASE_IMAGE=docker.io/library/python@sha256:" in dockerfile
    assert "FROM ${PYTHON_BASE_IMAGE}" in dockerfile
    assert 'org.opencontainers.image.revision="${FINANCE_SOURCE_COMMIT}"' in dockerfile
    assert "python:3.12-slim-bookworm" not in dockerfile
