from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from fastapi_backend.scripts import release_trust


def _repository_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _fixture(tmp_path: Path) -> tuple[Path, dict[str, str], Path]:
    image = "registry.example/finance-agent@sha256:" + "a" * 64
    manifest = tmp_path / "agent-release-manifest.json"
    manifest_data = b'{"release_id":"finance-agent-test-v1"}\n'
    manifest.write_bytes(manifest_data)
    binding = tmp_path / "deployment-binding.json"
    binding_data = (
        json.dumps(
            {
                "image_reference": image,
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
                f"FINANCE_RELEASE_MANIFEST_HOST_FILE={manifest}",
                f"FINANCE_RELEASE_MANIFEST_SIGSTORE_BUNDLE_HOST_FILE={manifest_bundle}",
                f"FINANCE_DEPLOYMENT_BINDING_HOST_FILE={binding}",
                "FINANCE_DEPLOYMENT_BINDING_SHA256=" + hashlib.sha256(binding_data).hexdigest(),
                f"FINANCE_DEPLOYMENT_BINDING_SIGSTORE_BUNDLE_HOST_FILE={binding_bundle}",
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
