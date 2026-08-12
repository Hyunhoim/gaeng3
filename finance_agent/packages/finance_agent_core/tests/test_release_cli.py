from __future__ import annotations

import argparse
import hashlib
import stat
from pathlib import Path

import pytest

from finance_agent_core import release_cli
from finance_agent_core.release import (
    DeploymentBinding,
    RollbackRelease,
    deployment_binding_file_bytes,
)


def test_release_output_is_exclusive_read_only_and_durable(tmp_path: Path) -> None:
    output = tmp_path / "agent-release-manifest.json"

    digest = release_cli._write_immutable(output, b'{"schema_version":"1.0"}\n')

    assert len(digest) == 64
    assert output.read_bytes() == b'{"schema_version":"1.0"}\n'
    assert output.stat().st_mode & (stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH) == 0
    with pytest.raises(SystemExit, match="already exists"):
        release_cli._write_immutable(output, b"replacement")


def test_release_source_roots_must_belong_to_verified_checkout(
    tmp_path: Path,
    monkeypatch,
) -> None:
    core = (
        tmp_path
        / "finance_agent"
        / "packages"
        / "finance_agent_core"
        / "src"
        / "finance_agent_core"
    )
    backend = tmp_path / "fastapi_backend" / "app"
    core.mkdir(parents=True)
    backend.mkdir(parents=True)
    module = core / "release.py"
    module.write_text("# test module\n", encoding="utf-8")
    monkeypatch.setattr(release_cli.release_contract, "__file__", str(module))

    release_cli._require_release_source_roots(tmp_path, backend)

    unrelated = tmp_path / "unrelated-backend"
    unrelated.mkdir()
    with pytest.raises(SystemExit, match="verified Git checkout"):
        release_cli._require_release_source_roots(tmp_path, unrelated)


def test_manifest_generation_rechecks_clean_source_after_hashing(
    tmp_path: Path,
    monkeypatch,
) -> None:
    backend = tmp_path / "fastapi_backend" / "app"
    backend.mkdir(parents=True)
    observed: list[str] = []
    monkeypatch.setattr(
        release_cli,
        "_require_release_source_roots",
        lambda *_args: observed.append("roots"),
    )
    monkeypatch.setattr(
        release_cli,
        "_require_clean_source",
        lambda *_args: observed.append("clean"),
    )
    monkeypatch.setattr(release_cli, "_runtime_inputs", lambda _args: object())
    monkeypatch.setattr(
        release_cli,
        "build_agent_release_manifest",
        lambda *_args, **_kwargs: object(),
    )
    monkeypatch.setattr(release_cli, "manifest_file_bytes", lambda _manifest: b"manifest\n")
    monkeypatch.setattr(
        release_cli,
        "_write_immutable",
        lambda *_args: "a" * 64,
    )
    arguments = argparse.Namespace(
        git_root=tmp_path,
        backend_root=backend,
        source_commit="b" * 40,
        release_id="finance-agent-test-v1",
        output=tmp_path / "manifest.json",
    )

    release_cli._create_manifest(arguments)

    assert observed == ["roots", "clean", "clean"]


def test_binding_cli_derives_rollback_chain_from_trusted_previous_binding(
    tmp_path: Path,
    monkeypatch,
) -> None:
    current_manifest = argparse.Namespace(
        release_id="finance-agent-new-v1",
        environment="evaluation",
        source_commit="a" * 40,
    )
    previous = DeploymentBinding(
        release_id="finance-agent-old-v1",
        environment="evaluation",
        source_commit="b" * 40,
        release_manifest_sha256="c" * 64,
        image_reference="registry.example/finance-agent@sha256:" + "d" * 64,
        platform="linux/amd64",
        activation_generation=1,
        rollback=RollbackRelease(mode="initial_bootstrap"),
    )
    previous_path = tmp_path / "previous-binding.json"
    previous_data = deployment_binding_file_bytes(previous)
    previous_path.write_bytes(previous_data)
    manifest_path = tmp_path / "current-manifest.json"
    manifest_path.write_bytes(b"{}\n")
    reads = {
        manifest_path: (manifest_path, b"{}\n", "e" * 64),
        previous_path: (
            previous_path,
            previous_data,
            hashlib.sha256(previous_data).hexdigest(),
        ),
    }
    monkeypatch.setattr(release_cli, "_read_release_file", lambda path: reads[Path(path)])
    monkeypatch.setattr(
        release_cli.AgentReleaseManifest,
        "model_validate",
        lambda _payload: current_manifest,
    )
    monkeypatch.setattr(
        release_cli,
        "manifest_file_bytes",
        lambda _manifest: b"{}\n",
    )
    captured: list[DeploymentBinding] = []

    def write(_path: Path, data: bytes) -> str:
        captured.append(DeploymentBinding.model_validate_json(data))
        return "f" * 64

    monkeypatch.setattr(release_cli, "_write_immutable", write)
    arguments = argparse.Namespace(
        manifest=manifest_path,
        image_reference="registry.example/finance-agent@sha256:" + "1" * 64,
        platform="linux/amd64",
        activation_generation=2,
        rollback_mode="pinned_previous_release",
        rollback_binding=previous_path,
        rollback_binding_sha256=hashlib.sha256(previous_data).hexdigest(),
        output=tmp_path / "new-binding.json",
    )

    release_cli._create_binding(arguments)

    assert len(captured) == 1
    rollback = captured[0].rollback
    assert rollback.target_release_id == previous.release_id
    assert rollback.target_manifest_sha256 == previous.release_manifest_sha256
    assert rollback.target_binding_sha256 == hashlib.sha256(previous_data).hexdigest()
    assert rollback.target_image_reference == previous.image_reference
    assert rollback.target_activation_generation == 1
