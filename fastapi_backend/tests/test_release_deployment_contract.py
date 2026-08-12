from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import subprocess
from pathlib import Path

import pytest


def _repository_root() -> Path:
    return Path(__file__).resolve().parents[2]


def test_release_image_embeds_manifest_without_self_referential_digest() -> None:
    dockerfile = (_repository_root() / "fastapi_backend" / "Dockerfile.release").read_text(
        encoding="utf-8"
    )

    assert "agent-release-manifest.json" in dockerfile
    assert "org.opencontainers.image.revision" in dockerfile
    assert "container_image_digest" not in dockerfile
    assert "FINANCE_IMAGE_REFERENCE" not in dockerfile


def test_release_compose_requires_digest_binding_and_disabled_dense() -> None:
    compose = (_repository_root() / "fastapi_backend" / "docker-compose.release.yml").read_text(
        encoding="utf-8"
    )

    assert "FINANCE_IMAGE_REFERENCE:?" in compose
    assert "FINANCE_DEPLOYMENT_BINDING_SHA256:?" in compose
    assert "FINANCE_RUNTIME_IMAGE_REFERENCE" in compose
    assert 'FINANCE_DENSE_SCHEMA_LINKER_ENABLED: "false"' in compose
    assert 'FINANCE_PRODUCT_DENSE_ENABLED: "false"' in compose
    assert "read_only: true" in compose
    assert "FINANCE_DATA_VOLUME_NAME:?" in compose
    assert compose.count("build: !reset null") == 2
    assert "FINANCE_RUNTIME_PLATFORM" in compose
    assert "CLOVASTUDIO_API_KEY_FILE:" in compose
    assert "\n      CLOVASTUDIO_API_KEY:" not in compose
    assert compose.count("read_only: true") >= 2
    assert "require_approved_database_paths" in compose
    assert "volumes: !override" in compose
    assert "target: /raw" not in compose


def test_release_launcher_forbids_build_and_forces_no_build() -> None:
    launcher = (_repository_root() / "compose-release.sh").read_text(encoding="utf-8")

    assert "build|--build" in launcher
    assert '"${before_up[@]}" up "${after_up[@]}"' in launcher
    assert "--no-build --force-recreate --wait" in launcher
    assert "repository@sha256" in launcher
    assert "DeploymentBinding differs" in launcher
    assert "hyunholim-finance-agent" in launcher
    assert "manifest_sha256[:12]" in launcher
    assert "build|--build" in launcher
    assert "unset APP_ENV" in launcher
    assert "HyperCLOVA release provider profile is incomplete" in launcher
    assert "release_trust.py" in launcher
    assert "release_activation.py" in launcher
    assert "RELEASE_ENV_SNAPSHOT" in launcher
    assert "forbids command" in launcher
    assert "--force-recreate" in launcher
    assert "--no-recreate" in launcher
    assert "-v|-v=*|-v?*|--volumes" in launcher


def test_base_compose_is_explicitly_the_development_path() -> None:
    compose = (_repository_root() / "docker-compose.yml").read_text(encoding="utf-8")

    assert "APP_ENV: ${APP_ENV:-development}" in compose
    assert "docker-compose.release.yml" in compose


def test_release_build_context_is_allowlisted_and_base_has_no_mutable_default() -> None:
    root = _repository_root()
    release_ignore = (root / "fastapi_backend" / "Dockerfile.release.dockerignore").read_text(
        encoding="utf-8"
    )
    base_ignore = (root / "fastapi_backend" / "Dockerfile.dockerignore").read_text(encoding="utf-8")
    dockerfile = (root / "fastapi_backend" / "Dockerfile.release").read_text(encoding="utf-8")

    assert release_ignore.splitlines()[0] == "**"
    assert "!fastapi_backend/release/agent-release-manifest.json" in release_ignore
    assert "!fastapi_backend/.env" not in release_ignore
    assert base_ignore.splitlines()[0] == "**"
    assert "!finance_agent/packages/finance_agent_core/**" in base_ignore
    assert "!fastapi_backend/app/**" in base_ignore
    assert "!fastapi_backend/.env" not in base_ignore
    assert "ARG BACKEND_BASE_IMAGE=gaeng3-backend:local" not in dockerfile


def _release_launcher_fixture(tmp_path: Path) -> tuple[Path, dict[str, str], Path]:
    image = "registry.example/finance-agent@sha256:" + "b" * 64
    source_commit = "a" * 40
    release_id = "finance-agent-test-v1"
    manifest_path = tmp_path / "agent-release-manifest.json"
    manifest_data = (json.dumps({"release_id": release_id}, separators=(",", ":")) + "\n").encode()
    manifest_path.write_bytes(manifest_data)
    manifest_sha256 = hashlib.sha256(manifest_data).hexdigest()
    binding = {
        "schema_version": "1.0",
        "release_id": release_id,
        "environment": "evaluation",
        "release_manifest_sha256": manifest_sha256,
        "image_reference": image,
        "source_commit": source_commit,
        "platform": "linux/amd64",
        "activation_generation": 1,
        "rollback": {
            "mode": "initial_bootstrap",
            "target_release_id": None,
            "target_manifest_sha256": None,
            "target_binding_sha256": None,
            "target_image_reference": None,
            "target_activation_generation": None,
            "target_environment": None,
            "target_platform": None,
        },
    }
    binding_path = tmp_path / "deployment-binding.json"
    binding_data = (json.dumps(binding, sort_keys=True, separators=(",", ":")) + "\n").encode()
    binding_path.write_bytes(binding_data)
    manifest_bundle = tmp_path / "agent-release-manifest.sigstore.json"
    binding_bundle = tmp_path / "deployment-binding.sigstore.json"
    manifest_bundle.write_text("{}\n", encoding="utf-8")
    binding_bundle.write_text("{}\n", encoding="utf-8")
    environment_path = tmp_path / ".env.release"
    environment_path.write_text(
        "\n".join(
            [
                "APP_ENV=evaluation",
                f"FINANCE_IMAGE_REFERENCE={image}",
                f"FINANCE_SOURCE_COMMIT={source_commit}",
                "FINANCE_RUNTIME_PLATFORM=linux/amd64",
                f"FINANCE_DEPLOYMENT_BINDING_HOST_FILE={binding_path}",
                "FINANCE_DEPLOYMENT_BINDING_SHA256=" + hashlib.sha256(binding_data).hexdigest(),
                f"FINANCE_RELEASE_MANIFEST_HOST_FILE={manifest_path}",
                f"FINANCE_RELEASE_MANIFEST_SIGSTORE_BUNDLE_HOST_FILE={manifest_bundle}",
                f"FINANCE_DEPLOYMENT_BINDING_SIGSTORE_BUNDLE_HOST_FILE={binding_bundle}",
                f"FINANCE_DATA_VOLUME_NAME=finance-data-{release_id}-{manifest_sha256[:12]}",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    capture = tmp_path / "docker-arguments.txt"
    fake_bin = (
        _repository_root() / "fastapi_backend" / "tests" / "fixtures" / "release_launcher_bin"
    )
    environment = os.environ.copy()
    environment.update(
        {
            "PATH": f"{fake_bin}:{environment['PATH']}",
            "RELEASE_ENV_FILE": str(environment_path),
            "CAPTURE_FILE": str(capture),
        }
    )
    return environment_path, environment, capture


def test_release_launcher_inserts_no_build_after_global_options(tmp_path: Path) -> None:
    _, environment, capture = _release_launcher_fixture(tmp_path)
    snapshot_modes = tmp_path / "snapshot-modes.json"
    environment["SNAPSHOT_MODE_CAPTURE"] = str(snapshot_modes)
    harness = tmp_path / "launcher-harness"
    (harness / "fastapi_backend" / "scripts").mkdir(parents=True)
    shutil.copy2(_repository_root() / "compose-release.sh", harness / "compose-release.sh")
    activation = (
        _repository_root() / "fastapi_backend" / "scripts" / "release_activation.py"
    ).read_text(encoding="utf-8")
    activation = (
        activation.replace(
            "/var/lib/finance-agent-release/active-binding.json",
            str(harness / "activation-state" / "active-binding.json"),
        )
        .replace(
            "/run/lock/finance-agent-release/activation.lock",
            str(harness / "activation-lock" / "activation.lock"),
        )
        .replace("REQUIRED_OWNER_UID = 0", f"REQUIRED_OWNER_UID = {os.geteuid()}")
    )
    (harness / "fastapi_backend" / "scripts" / "release_activation.py").write_text(
        activation,
        encoding="utf-8",
    )
    (harness / "fastapi_backend" / "scripts" / "release_trust.py").write_text(
        """\
import json
import os
import stat
import sys
from pathlib import Path

env_file = Path(sys.argv[sys.argv.index("--env-file") + 1])
values = {}
for line in env_file.read_text(encoding="utf-8").splitlines():
    key, value = line.split("=", 1)
    values[key] = value.strip("'")
binding = Path(values["FINANCE_DEPLOYMENT_BINDING_HOST_FILE"])
Path(os.environ["SNAPSHOT_MODE_CAPTURE"]).write_text(
    json.dumps({
        "root": stat.S_IMODE(env_file.parent.stat().st_mode),
        "environment": stat.S_IMODE(env_file.stat().st_mode),
        "binding": stat.S_IMODE(binding.stat().st_mode),
    }),
    encoding="utf-8",
)
""",
        encoding="utf-8",
    )

    completed = subprocess.run(
        [str(harness / "compose-release.sh"), "--ansi", "never", "up", "--detach"],
        cwd=harness,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0
    arguments = capture.read_text(encoding="utf-8").splitlines()
    assert arguments[:3] == ["compose", "-p", "hyunholim-finance-agent"]
    assert arguments[-6:] == [
        "never",
        "up",
        "--detach",
        "--no-build",
        "--force-recreate",
        "--wait",
    ]
    assert json.loads(snapshot_modes.read_text(encoding="utf-8")) == {
        "root": stat.S_IMODE(0o711),
        "environment": stat.S_IMODE(0o600),
        "binding": stat.S_IMODE(0o444),
    }


def test_release_launcher_rejects_unknown_global_option_before_trust(tmp_path: Path) -> None:
    _, environment, capture = _release_launcher_fixture(tmp_path)

    completed = subprocess.run(
        [str(_repository_root() / "compose-release.sh"), "--dry-run", "up", "--detach"],
        cwd=_repository_root(),
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 2
    assert "forbids unknown global option" in completed.stderr
    assert not capture.exists()


def test_release_launcher_rejects_global_option_build_bypass(tmp_path: Path) -> None:
    _, environment, capture = _release_launcher_fixture(tmp_path)

    completed = subprocess.run(
        [str(_repository_root() / "compose-release.sh"), "--ansi", "never", "up", "--build"],
        cwd=_repository_root(),
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 2
    assert "forbids mutable local image builds" in completed.stderr
    assert not capture.exists()


def test_release_launcher_rejects_compose_run_override(tmp_path: Path) -> None:
    _, environment, capture = _release_launcher_fixture(tmp_path)

    completed = subprocess.run(
        [
            str(_repository_root() / "compose-release.sh"),
            "run",
            "-e",
            "APP_ENV=development",
            "backend",
        ],
        cwd=_repository_root(),
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 2
    assert "forbids command: run" in completed.stderr
    assert not capture.exists()


def test_release_launcher_rejects_inline_hcx_credential(tmp_path: Path) -> None:
    environment_path, environment, capture = _release_launcher_fixture(tmp_path)
    with environment_path.open("a", encoding="utf-8") as stream:
        stream.write("CLOVASTUDIO_API_KEY=nv-inline-secret-must-not-be-used\n")

    completed = subprocess.run(
        [str(_repository_root() / "compose-release.sh"), "config", "--quiet"],
        cwd=_repository_root(),
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 1
    assert "forbid inline CLOVASTUDIO_API_KEY" in completed.stderr
    assert "nv-inline-secret" not in completed.stderr
    assert not capture.exists()


@pytest.mark.parametrize(
    "arguments",
    [
        ["up", "--no-recreate"],
        ["up", "--force-recreate=false"],
        ["up", "--no-build=false"],
        ["up", "data-init"],
        ["up", "--scale", "backend=0"],
        ["--profile", "down", "up", "--detach"],
        ["--profile=down", "up", "--detach"],
        ["restart"],
        ["down", "--volumes"],
        ["down", "--volumes=true"],
        ["down", "-v=true"],
        ["down", "--rmi", "all"],
        ["down", "--rmi=local"],
        ["down", "-vt", "3"],
    ],
)
def test_release_launcher_rejects_stale_activation_or_volume_deletion(
    tmp_path: Path,
    arguments: list[str],
) -> None:
    _, environment, capture = _release_launcher_fixture(tmp_path)

    completed = subprocess.run(
        [str(_repository_root() / "compose-release.sh"), *arguments],
        cwd=_repository_root(),
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 2
    assert not capture.exists()


def test_release_launcher_allows_down_without_destructive_options(tmp_path: Path) -> None:
    _, environment, capture = _release_launcher_fixture(tmp_path)

    completed = subprocess.run(
        [str(_repository_root() / "compose-release.sh"), "down"],
        cwd=_repository_root(),
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0
    assert capture.read_text(encoding="utf-8").splitlines()[-1] == "down"


def test_release_launcher_removes_inherited_release_and_compose_overrides(
    tmp_path: Path,
) -> None:
    _, environment, _ = _release_launcher_fixture(tmp_path)
    captured_environment = tmp_path / "docker-environment.txt"
    environment.update(
        {
            "BACKEND_BIND_ADDRESS": "0.0.0.0",
            "BACKEND_PORT": "1",
            "FINANCE_IMAGE_REFERENCE": "attacker-controlled",
            "HCX_MODEL": "attacker-controlled",
            "COMPOSE_PROFILES": "attacker-controlled",
            "CAPTURE_ENV_FILE": str(captured_environment),
        }
    )

    completed = subprocess.run(
        [str(_repository_root() / "compose-release.sh"), "down"],
        cwd=_repository_root(),
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr
    captured = captured_environment.read_text(encoding="utf-8")
    assert "BACKEND_BIND_ADDRESS=" not in captured
    assert "BACKEND_PORT=" not in captured
    assert "FINANCE_IMAGE_REFERENCE=" not in captured
    assert "HCX_MODEL=" not in captured
    assert "COMPOSE_PROFILES=" not in captured
