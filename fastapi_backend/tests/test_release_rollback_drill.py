from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path

from fastapi_backend.scripts import rollback_drill


def _repository_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _write_binding(path: Path, binding: dict[str, object]) -> str:
    data = (json.dumps(binding, sort_keys=True, separators=(",", ":")) + "\n").encode()
    path.write_bytes(data)
    path.chmod(0o444)
    return hashlib.sha256(data).hexdigest()


def _release_pair(tmp_path: Path) -> tuple[Path, Path, dict[str, object], dict[str, object]]:
    previous_manifest_data = b'{"release_id":"finance-agent-previous-v7"}\n'
    current_manifest_data = b'{"release_id":"finance-agent-current-v8"}\n'
    previous_binding: dict[str, object] = {
        "activation_generation": 7,
        "environment": "evaluation",
        "image_reference": "registry.example/finance-agent@sha256:" + "a" * 64,
        "platform": "linux/amd64",
        "release_id": "finance-agent-previous-v7",
        "release_manifest_sha256": hashlib.sha256(previous_manifest_data).hexdigest(),
        "rollback": {
            "mode": "pinned_previous_release",
            "target_activation_generation": 6,
            "target_binding_sha256": "c" * 64,
            "target_environment": "evaluation",
            "target_image_reference": ("registry.example/finance-agent@sha256:" + "d" * 64),
            "target_manifest_sha256": "e" * 64,
            "target_platform": "linux/amd64",
            "target_release_id": "finance-agent-older-v6",
        },
        "schema_version": "1.0",
        "source_commit": "f" * 40,
    }
    previous_binding_path = tmp_path / "previous-binding.json"
    previous_binding_sha256 = _write_binding(previous_binding_path, previous_binding)
    current_binding: dict[str, object] = {
        "activation_generation": 8,
        "environment": "evaluation",
        "image_reference": "registry.example/finance-agent@sha256:" + "1" * 64,
        "platform": "linux/amd64",
        "release_id": "finance-agent-current-v8",
        "release_manifest_sha256": hashlib.sha256(current_manifest_data).hexdigest(),
        "rollback": {
            "mode": "pinned_previous_release",
            "target_activation_generation": 7,
            "target_binding_sha256": previous_binding_sha256,
            "target_environment": "evaluation",
            "target_image_reference": previous_binding["image_reference"],
            "target_manifest_sha256": previous_binding["release_manifest_sha256"],
            "target_platform": "linux/amd64",
            "target_release_id": previous_binding["release_id"],
        },
        "schema_version": "1.0",
        "source_commit": "3" * 40,
    }
    current_binding_path = tmp_path / "current-binding.json"
    current_binding_sha256 = _write_binding(current_binding_path, current_binding)
    manifest_files = {
        "finance-agent-previous-v7": tmp_path / "previous-manifest.json",
        "finance-agent-current-v8": tmp_path / "current-manifest.json",
    }
    manifest_files["finance-agent-previous-v7"].write_bytes(previous_manifest_data)
    manifest_files["finance-agent-current-v8"].write_bytes(current_manifest_data)

    def write_environment(
        path: Path,
        binding: dict[str, object],
        binding_path: Path,
        binding_sha256: str,
    ) -> None:
        manifest_sha256 = str(binding["release_manifest_sha256"])
        release_id = str(binding["release_id"])
        manifest_path = manifest_files[release_id]
        manifest_bundle = tmp_path / f"{release_id}-manifest.sigstore.json"
        binding_bundle = tmp_path / f"{release_id}-binding.sigstore.json"
        manifest_bundle.write_text("{}\n", encoding="utf-8")
        binding_bundle.write_text("{}\n", encoding="utf-8")
        path.write_text(
            "\n".join(
                [
                    "APP_ENV=evaluation",
                    f"FINANCE_IMAGE_REFERENCE={binding['image_reference']}",
                    f"FINANCE_SOURCE_COMMIT={binding['source_commit']}",
                    "FINANCE_RUNTIME_PLATFORM=linux/amd64",
                    f"FINANCE_DEPLOYMENT_BINDING_HOST_FILE={binding_path}",
                    f"FINANCE_DEPLOYMENT_BINDING_SHA256={binding_sha256}",
                    f"FINANCE_RELEASE_MANIFEST_HOST_FILE={manifest_path}",
                    f"FINANCE_RELEASE_MANIFEST_SIGSTORE_BUNDLE_HOST_FILE={manifest_bundle}",
                    f"FINANCE_DEPLOYMENT_BINDING_SIGSTORE_BUNDLE_HOST_FILE={binding_bundle}",
                    (
                        "FINANCE_DATA_VOLUME_NAME=finance-data-"
                        f"{binding['release_id']}-{manifest_sha256[:12]}"
                    ),
                    "FINANCE_BACKEND_ANSWER_PROVIDER=deterministic",
                    "FINANCE_BACKEND_HCX_QUERY_PLAN_ENABLED=false",
                    "FINANCE_AGENT_LLM_MODE=disabled",
                    "LLM_PROVIDER=disabled",
                ]
            )
            + "\n",
            encoding="utf-8",
        )

    previous_env = tmp_path / "previous.env"
    current_env = tmp_path / "current.env"
    write_environment(
        previous_env,
        previous_binding,
        previous_binding_path,
        previous_binding_sha256,
    )
    write_environment(
        current_env,
        current_binding,
        current_binding_path,
        current_binding_sha256,
    )
    return previous_env, current_env, previous_binding, current_binding


def _command(previous_env: Path, current_env: Path, *extra: str) -> list[str]:
    return [
        sys.executable,
        str(_repository_root() / "fastapi_backend" / "scripts" / "rollback_drill.py"),
        "--previous-env",
        str(previous_env),
        "--current-env",
        str(current_env),
        "--project-name",
        "finance-agent-rollback-drill-test01",
        "--port",
        "19081",
        *extra,
    ]


def test_rollback_drill_dry_run_validates_exact_chain_without_docker(tmp_path: Path) -> None:
    previous_env, current_env, previous, current = _release_pair(tmp_path)
    environment = os.environ.copy()
    environment["PATH"] = str(tmp_path)

    completed = subprocess.run(
        _command(previous_env, current_env),
        cwd=_repository_root(),
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0
    result = json.loads(completed.stdout)
    assert result["status"] == "validated"
    assert result["mode"] == "dry_run"
    assert result["activation_sequence"] == [
        previous["release_id"],
        current["release_id"],
        previous["release_id"],
    ]
    assert result["artifacts_preserved"] is False


def test_rollback_snapshot_is_traversable_and_binding_readable_under_private_umask(
    tmp_path: Path,
) -> None:
    previous_env, _, _, _ = _release_pair(tmp_path)
    target = rollback_drill._load_target(previous_env)
    snapshot_root = tmp_path / "snapshots"
    snapshot_root.mkdir(mode=0o711)
    old_umask = os.umask(0o077)
    try:
        snapshot = rollback_drill._snapshot_target(target, snapshot_root, "previous")
    finally:
        os.umask(old_umask)

    assert stat.S_IMODE(snapshot_root.stat().st_mode) == 0o711
    assert stat.S_IMODE(snapshot.binding_file.parent.stat().st_mode) == 0o711
    assert stat.S_IMODE(snapshot.binding_file.stat().st_mode) == 0o444
    assert stat.S_IMODE(snapshot.env_file.stat().st_mode) == 0o600


def test_rollback_drill_rejects_binding_that_does_not_pin_previous(tmp_path: Path) -> None:
    previous_env, current_env, _, current = _release_pair(tmp_path)
    current_binding_path = Path(
        next(
            line.split("=", 1)[1]
            for line in current_env.read_text(encoding="utf-8").splitlines()
            if line.startswith("FINANCE_DEPLOYMENT_BINDING_HOST_FILE=")
        )
    )
    current_binding_path.chmod(0o644)
    current["rollback"] = dict(current["rollback"], target_binding_sha256="0" * 64)  # type: ignore[arg-type]
    changed_sha256 = _write_binding(current_binding_path, current)
    contents = current_env.read_text(encoding="utf-8")
    contents = re_sub_binding_sha(contents, changed_sha256)
    current_env.write_text(contents, encoding="utf-8")

    completed = subprocess.run(
        _command(previous_env, current_env),
        cwd=_repository_root(),
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 2
    assert "does not pin the exact previous release" in completed.stderr


def test_rollback_drill_rejects_hcx_secret_on_deterministic_release(tmp_path: Path) -> None:
    previous_env, current_env, _, _ = _release_pair(tmp_path)
    previous_env.write_text(
        previous_env.read_text(encoding="utf-8")
        + "CLOVASTUDIO_API_KEY_FILE=/run/secrets/clovastudio_api_key\n",
        encoding="utf-8",
    )

    completed = subprocess.run(
        _command(previous_env, current_env),
        cwd=_repository_root(),
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 2
    assert "must not configure HCLX credentials" in completed.stderr


def re_sub_binding_sha(contents: str, replacement: str) -> str:
    lines = []
    for line in contents.splitlines():
        if line.startswith("FINANCE_DEPLOYMENT_BINDING_SHA256="):
            line = f"FINANCE_DEPLOYMENT_BINDING_SHA256={replacement}"
        lines.append(line)
    return "\n".join(lines) + "\n"


def test_fake_docker_drill_activates_n_minus_one_n_then_n_minus_one(
    tmp_path: Path,
) -> None:
    previous_env, current_env, previous, current = _release_pair(tmp_path)
    capture = tmp_path / "docker-calls.txt"
    state = tmp_path / "docker-state.txt"
    fake_bin = _repository_root() / "fastapi_backend" / "tests" / "fixtures" / "rollback_drill_bin"
    trust_bin = (
        _repository_root() / "fastapi_backend" / "tests" / "fixtures" / "release_launcher_bin"
    )
    environment = os.environ.copy()
    environment.update(
        {
            "PATH": f"{fake_bin}:{trust_bin}:{environment['PATH']}",
            "ROLLBACK_DRILL_CAPTURE_FILE": str(capture),
            "ROLLBACK_DRILL_STATE_FILE": str(state),
        }
    )
    harness = tmp_path / "rollback-harness"
    scripts = harness / "fastapi_backend" / "scripts"
    scripts.mkdir(parents=True)
    shutil.copy2(
        _repository_root() / "fastapi_backend" / "scripts" / "rollback_drill.py",
        scripts / "rollback_drill.py",
    )
    (scripts / "release_trust.py").write_text("raise SystemExit(0)\n", encoding="utf-8")
    command = _command(previous_env, current_env, "--execute")
    command[1] = str(scripts / "rollback_drill.py")

    completed = subprocess.run(
        command,
        cwd=harness,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr
    result = json.loads(completed.stdout)
    assert result["status"] == "verified"
    assert result["artifacts_preserved"] is True
    assert result["containers_stopped_after_verification"] is True
    calls = capture.read_text(encoding="utf-8").splitlines()
    activations = [line for line in calls if " up --detach --wait " in f" {line} "]
    assert len(activations) == 3
    assert "/previous/release.env" in activations[0]
    assert "/current/release.env" in activations[1]
    assert "/previous/release.env" in activations[2]
    assert sum(line.startswith("image inspect ") for line in calls) == 4
    assert sum(line.startswith("volume inspect ") for line in calls) == 4
    assert sum(line.startswith("exec rollback-drill-backend python -c ") for line in calls) == 3
    assert any(line.endswith(" down") for line in calls)
    assert calls[-2].startswith("ps --all --quiet --filter ")
    assert calls[-1] == "network inspect finance-agent-rollback-drill-test01_default"
    assert previous["release_id"] in result["activation_sequence"]
    assert current["release_id"] in result["activation_sequence"]


def test_fake_docker_drill_fails_closed_when_cleanup_is_incomplete(tmp_path: Path) -> None:
    previous_env, current_env, _, _ = _release_pair(tmp_path)
    capture = tmp_path / "docker-calls.txt"
    state = tmp_path / "docker-state.txt"
    fake_bin = _repository_root() / "fastapi_backend" / "tests" / "fixtures" / "rollback_drill_bin"
    environment = os.environ.copy()
    environment.update(
        {
            "PATH": f"{fake_bin}:{environment['PATH']}",
            "ROLLBACK_DRILL_CAPTURE_FILE": str(capture),
            "ROLLBACK_DRILL_STATE_FILE": str(state),
            "ROLLBACK_DRILL_FAIL_DOWN": "1",
        }
    )
    harness = tmp_path / "rollback-cleanup-harness"
    scripts = harness / "fastapi_backend" / "scripts"
    scripts.mkdir(parents=True)
    shutil.copy2(
        _repository_root() / "fastapi_backend" / "scripts" / "rollback_drill.py",
        scripts / "rollback_drill.py",
    )
    (scripts / "release_trust.py").write_text("raise SystemExit(0)\n", encoding="utf-8")
    command = _command(previous_env, current_env, "--execute")
    command[1] = str(scripts / "rollback_drill.py")

    completed = subprocess.run(
        command,
        cwd=harness,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 2
    assert "cleanup was incomplete" in completed.stderr


def test_execute_rejects_live_project_name_before_docker(tmp_path: Path) -> None:
    previous_env, current_env, _, _ = _release_pair(tmp_path)
    command = _command(previous_env, current_env, "--execute")
    project_index = command.index("--project-name") + 1
    command[project_index] = "hyunholim-finance-agent"

    completed = subprocess.run(
        command,
        cwd=_repository_root(),
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 2
    assert "project name must use" in completed.stderr
