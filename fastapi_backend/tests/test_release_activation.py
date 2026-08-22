from __future__ import annotations

import hashlib
import json
import multiprocessing
import os
import subprocess
import time
from pathlib import Path
from typing import Any

import pytest
from fastapi_backend.scripts import release_activation


def _binding(
    *,
    release_id: str,
    generation: int,
    image_character: str,
    rollback: dict[str, Any],
) -> dict[str, Any]:
    return {
        "schema_version": "1.0",
        "release_id": release_id,
        "environment": "evaluation",
        "source_commit": image_character * 40,
        "release_manifest_sha256": image_character * 64,
        "image_reference": ("registry.example/finance-agent@sha256:" + image_character * 64),
        "platform": "linux/amd64",
        "activation_generation": generation,
        "rollback": rollback,
    }


def _bootstrap_rollback() -> dict[str, Any]:
    return {
        "mode": "initial_bootstrap",
        "target_release_id": None,
        "target_manifest_sha256": None,
        "target_binding_sha256": None,
        "target_image_reference": None,
        "target_activation_generation": None,
        "target_environment": None,
        "target_platform": None,
    }


def _pinned_rollback(
    previous: release_activation.ActivationRecord,
) -> dict[str, Any]:
    return {
        "mode": "pinned_previous_release",
        "target_release_id": previous.release_id,
        "target_manifest_sha256": previous.release_manifest_sha256,
        "target_binding_sha256": previous.binding_sha256,
        "target_image_reference": previous.image_reference,
        "target_activation_generation": previous.activation_generation,
        "target_environment": previous.environment,
        "target_platform": previous.platform,
    }


def _write_candidate(tmp_path: Path, name: str, binding: dict[str, Any]) -> tuple[Path, str]:
    binding_path = tmp_path / f"{name}-binding.json"
    binding_data = (json.dumps(binding, sort_keys=True, separators=(",", ":")) + "\n").encode()
    binding_path.write_bytes(binding_data)
    binding_path.chmod(0o444)
    binding_sha256 = hashlib.sha256(binding_data).hexdigest()
    environment_path = tmp_path / f"{name}.env"
    environment_path.write_text(
        "\n".join(
            (
                f"FINANCE_DEPLOYMENT_BINDING_HOST_FILE='{binding_path}'",
                f"FINANCE_DEPLOYMENT_BINDING_SHA256='{binding_sha256}'",
            )
        )
        + "\n",
        encoding="utf-8",
    )
    environment_path.chmod(0o600)
    return environment_path, binding_sha256


def _configure_control_paths(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(
        release_activation,
        "ACTIVE_STATE_FILE",
        tmp_path / "state" / "active-binding.json",
    )
    monkeypatch.setattr(
        release_activation,
        "ACTIVATION_LOCK_FILE",
        tmp_path / "lock" / "activation.lock",
    )
    monkeypatch.setattr(release_activation, "REQUIRED_OWNER_UID", os.geteuid())
    monkeypatch.setattr(release_activation, "_run_trust_verification", lambda _: None)


def _compose_arguments() -> list[str]:
    return ["up", "--detach", "--no-build", "--force-recreate", "--wait"]


def test_compose_command_adds_adaptive_overlay_only_for_explicit_activation(
    tmp_path: Path,
) -> None:
    disabled = tmp_path / "disabled.env"
    disabled.write_text("FINANCE_ADAPTIVE_SEMANTIC_ENABLED='false'\n", encoding="utf-8")
    disabled.chmod(0o600)
    enabled = tmp_path / "enabled.env"
    enabled.write_text("FINANCE_ADAPTIVE_SEMANTIC_ENABLED='true'\n", encoding="utf-8")
    enabled.chmod(0o600)

    disabled_command = release_activation._compose_command(disabled, _compose_arguments())
    enabled_command = release_activation._compose_command(enabled, _compose_arguments())

    assert "fastapi_backend/docker-compose.adaptive.yml" not in disabled_command
    assert enabled_command.count("fastapi_backend/docker-compose.adaptive.yml") == 1


def test_compose_command_rejects_invalid_adaptive_flag(tmp_path: Path) -> None:
    env_file = tmp_path / "invalid.env"
    env_file.write_text("FINANCE_ADAPTIVE_SEMANTIC_ENABLED='yes'\n", encoding="utf-8")
    env_file.chmod(0o600)

    with pytest.raises(
        release_activation.ReleaseActivationError,
        match="must be true or false",
    ):
        release_activation._compose_command(env_file, _compose_arguments())


def test_activation_rejects_signed_old_binding_and_allows_exact_idempotent_restart(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _configure_control_paths(monkeypatch, tmp_path)
    compose_calls: list[list[str]] = []

    def successful_run(command: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        compose_calls.append(command)
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(release_activation.subprocess, "run", successful_run)
    first_binding = _binding(
        release_id="finance-agent-eval-001",
        generation=1,
        image_character="a",
        rollback=_bootstrap_rollback(),
    )
    first_env, first_sha256 = _write_candidate(tmp_path, "first", first_binding)
    first_record = release_activation._record_from_binding(first_binding, first_sha256)
    second_binding = _binding(
        release_id="finance-agent-eval-002",
        generation=2,
        image_character="b",
        rollback=_pinned_rollback(first_record),
    )
    second_env, _ = _write_candidate(tmp_path, "second", second_binding)

    assert release_activation.activate(first_env, _compose_arguments()) == "bootstrap"
    assert release_activation.activate(second_env, _compose_arguments()) == "advance"
    assert release_activation.activate(second_env, _compose_arguments()) == "idempotent"

    with pytest.raises(
        release_activation.ReleaseActivationError,
        match="immediately follow",
    ):
        release_activation.activate(first_env, _compose_arguments())

    assert len(compose_calls) == 3
    assert all("--wait" in command for command in compose_calls)
    active = release_activation._load_active_state()
    assert active is not None
    assert active.activation_generation == 2
    assert active.binding_sha256 != first_sha256
    assert release_activation.ACTIVE_STATE_FILE.stat().st_mode & 0o777 == 0o400


@pytest.mark.parametrize(
    "field",
    [
        "target_binding_sha256",
        "target_release_id",
        "target_manifest_sha256",
        "target_image_reference",
        "target_activation_generation",
        "target_environment",
        "target_platform",
    ],
)
def test_activation_requires_next_binding_to_pin_every_active_identity_field(
    field: str,
) -> None:
    first_binding = _binding(
        release_id="finance-agent-eval-001",
        generation=1,
        image_character="a",
        rollback=_bootstrap_rollback(),
    )
    first = release_activation._record_from_binding(first_binding, "1" * 64)
    rollback = _pinned_rollback(first)
    rollback[field] = 999 if field == "target_activation_generation" else "invalid"
    second_binding = _binding(
        release_id="finance-agent-eval-002",
        generation=2,
        image_character="b",
        rollback=rollback,
    )
    second = release_activation._record_from_binding(second_binding, "2" * 64)

    with pytest.raises(release_activation.ReleaseActivationError, match="exactly pin"):
        release_activation._validate_transition(second, second_binding, first)


def test_activation_does_not_commit_state_before_compose_health_success(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _configure_control_paths(monkeypatch, tmp_path)
    binding = _binding(
        release_id="finance-agent-eval-001",
        generation=1,
        image_character="a",
        rollback=_bootstrap_rollback(),
    )
    env_file, _ = _write_candidate(tmp_path, "failed", binding)
    monkeypatch.setattr(
        release_activation.subprocess,
        "run",
        lambda command, **_: subprocess.CompletedProcess(command, 1),
    )

    with pytest.raises(release_activation.ReleaseActivationError, match="health readiness"):
        release_activation.activate(env_file, _compose_arguments())

    assert not release_activation.ACTIVE_STATE_FILE.exists()


def test_activation_never_calls_compose_when_release_trust_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _configure_control_paths(monkeypatch, tmp_path)
    binding = _binding(
        release_id="finance-agent-eval-001",
        generation=1,
        image_character="a",
        rollback=_bootstrap_rollback(),
    )
    env_file, _ = _write_candidate(tmp_path, "untrusted", binding)

    def fail_trust(_path: Path) -> None:
        raise release_activation.ReleaseActivationError("release profile mismatch")

    monkeypatch.setattr(release_activation, "_run_trust_verification", fail_trust)
    compose_calls: list[list[str]] = []
    monkeypatch.setattr(
        release_activation.subprocess,
        "run",
        lambda command, **_: compose_calls.append(command),
    )

    with pytest.raises(
        release_activation.ReleaseActivationError,
        match="release profile mismatch",
    ):
        release_activation.activate(env_file, _compose_arguments())

    assert compose_calls == []
    assert not release_activation.ACTIVE_STATE_FILE.exists()


def test_activation_lock_serializes_parallel_attempts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    if "fork" not in multiprocessing.get_all_start_methods():
        pytest.skip("the host activation lock process test requires fork")
    _configure_control_paths(monkeypatch, tmp_path)
    binding = _binding(
        release_id="finance-agent-eval-001",
        generation=1,
        image_character="a",
        rollback=_bootstrap_rollback(),
    )
    env_file, _ = _write_candidate(tmp_path, "parallel", binding)
    events = tmp_path / "events.log"

    def delayed_run(command: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        with events.open("a", encoding="utf-8") as stream:
            stream.write(f"start:{os.getpid()}\n")
        time.sleep(0.15)
        with events.open("a", encoding="utf-8") as stream:
            stream.write(f"end:{os.getpid()}\n")
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(release_activation.subprocess, "run", delayed_run)
    context = multiprocessing.get_context("fork")
    results = context.Queue()

    def run_activation() -> None:
        try:
            results.put(release_activation.activate(env_file, _compose_arguments()))
        except (OSError, RuntimeError) as error:  # pragma: no cover - surfaced by parent
            results.put(f"error:{error}")

    processes = [context.Process(target=run_activation) for _ in range(2)]
    for process in processes:
        process.start()
    for process in processes:
        process.join(timeout=5)

    assert all(process.exitcode == 0 for process in processes)
    assert sorted(results.get(timeout=1) for _ in processes) == ["bootstrap", "idempotent"]
    event_kinds = [line.split(":", 1)[0] for line in events.read_text().splitlines()]
    assert event_kinds == ["start", "end", "start", "end"]
