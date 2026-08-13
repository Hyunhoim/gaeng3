from __future__ import annotations

import hashlib
import json
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest
from fastapi_backend.scripts import rollback_drill
from finance_agent_core.observability import AuditEvent


def _repository_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _copy_test_rollback_script(destination: Path) -> Path:
    source = _repository_root() / "fastapi_backend" / "scripts" / "rollback_drill.py"
    contents = source.read_text(encoding="utf-8")
    marker = "_RELEASE_BACKEND_UID = 10001"
    assert contents.count(marker) == 1
    destination.write_text(
        contents.replace(marker, f"_RELEASE_BACKEND_UID = {os.geteuid()}"),
        encoding="utf-8",
    )
    destination.chmod(0o755)
    return destination


def _load_test_target(
    monkeypatch: pytest.MonkeyPatch,
    environment_path: Path,
) -> rollback_drill.ReleaseTarget:
    monkeypatch.setattr(rollback_drill, "_RELEASE_BACKEND_UID", os.geteuid())
    return rollback_drill._load_target(environment_path)


def _write_binding(path: Path, binding: dict[str, object]) -> str:
    data = (json.dumps(binding, sort_keys=True, separators=(",", ":")) + "\n").encode()
    path.write_bytes(data)
    path.chmod(0o444)
    return hashlib.sha256(data).hexdigest()


def _release_pair(tmp_path: Path) -> tuple[Path, Path, dict[str, object], dict[str, object]]:
    audit_root = tmp_path / "audit"
    audit_root.mkdir(mode=0o700)
    audit_root.chmod(0o700)
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
                    f"FINANCE_AUDIT_HOST_DIR={audit_root}",
                    "FINANCE_AUDIT_QUEUE_CAPACITY=2048",
                    "FINANCE_AUDIT_SHUTDOWN_TIMEOUT_SECONDS=5",
                    "FINANCE_AUDIT_FSYNC_EACH_EVENT=true",
                    (
                        "FINANCE_DATA_VOLUME_NAME=finance-data-"
                        f"{binding['release_id']}-{manifest_sha256[:12]}"
                    ),
                    "WEB_CONCURRENCY=1",
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
    test_script = _copy_test_rollback_script(previous_env.parent / "rollback_drill-test.py")
    return [
        sys.executable,
        str(test_script),
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
    assert result["audit_chain_verified"] is False
    assert result["audit_observations"] == []


def test_rollback_snapshot_is_traversable_and_binding_readable_under_private_umask(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    previous_env, _, _, _ = _release_pair(tmp_path)
    target = _load_test_target(monkeypatch, previous_env)
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


def test_rollback_drill_rejects_hclx_profile_until_its_audit_path_is_frozen(
    tmp_path: Path,
) -> None:
    previous_env, current_env, _, _ = _release_pair(tmp_path)
    for env_file in (previous_env, current_env):
        contents = env_file.read_text(encoding="utf-8").replace(
            "FINANCE_BACKEND_ANSWER_PROVIDER=deterministic",
            "FINANCE_BACKEND_ANSWER_PROVIDER=hyperclova",
        )
        env_file.write_text(contents, encoding="utf-8")

    completed = subprocess.run(
        _command(previous_env, current_env),
        cwd=_repository_root(),
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 2
    assert "supports only the deterministic HCLX-disabled profile" in completed.stderr


@pytest.mark.parametrize("effective_uid", [0, 10001])
def test_rollback_audit_reader_accepts_root_or_release_uid(
    monkeypatch: pytest.MonkeyPatch,
    effective_uid: int,
) -> None:
    monkeypatch.setattr(rollback_drill, "_RELEASE_BACKEND_UID", 10001)
    monkeypatch.setattr(rollback_drill.os, "geteuid", lambda: effective_uid)

    rollback_drill._require_audit_reader_identity()


@pytest.mark.parametrize("effective_uid", [1, 1000, 1002, 65534])
def test_rollback_audit_reader_rejects_other_uids(
    monkeypatch: pytest.MonkeyPatch,
    effective_uid: int,
) -> None:
    monkeypatch.setattr(rollback_drill, "_RELEASE_BACKEND_UID", 10001)
    monkeypatch.setattr(rollback_drill.os, "geteuid", lambda: effective_uid)

    with pytest.raises(rollback_drill.RollbackDrillError, match="root or UID 10001"):
        rollback_drill._require_audit_reader_identity()


def test_execute_rejects_unprivileged_audit_reader_before_loading_release_files(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(rollback_drill, "_RELEASE_BACKEND_UID", 10001)
    monkeypatch.setattr(rollback_drill.os, "geteuid", lambda: 1002)

    def fail_if_loaded(_path: Path) -> rollback_drill.ReleaseTarget:
        raise AssertionError("release files must not be read before the UID gate")

    monkeypatch.setattr(rollback_drill, "_load_target", fail_if_loaded)
    result = rollback_drill.main(
        [
            "--previous-env",
            "/not-read/previous.env",
            "--current-env",
            "/not-read/current.env",
            "--project-name",
            "finance-agent-rollback-drill-uid01",
            "--port",
            "19081",
            "--execute",
        ]
    )

    assert result == 2
    assert "root or UID 10001" in capsys.readouterr().err


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        (b'{"stage":"answer"}', "partial final record"),
        (b'{"stage":}\n', "not valid JSONL"),
    ],
)
def test_audit_chain_verification_fails_closed_for_malformed_or_partial_jsonl(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    payload: bytes,
    message: str,
) -> None:
    previous_env, _, _, _ = _release_pair(tmp_path)
    target = _load_test_target(monkeypatch, previous_env)

    with pytest.raises(rollback_drill.RollbackDrillError, match=message):
        rollback_drill.DockerClient._verify_audit_chain(target, payload)


def _valid_probe_events(
    target: rollback_drill.ReleaseTarget,
) -> list[dict[str, object]]:
    release_id_sha256 = hashlib.sha256(target.release_id.encode()).hexdigest()
    release_context_sha256 = rollback_drill._canonical_sha256(
        {
            "release_id": target.release_id,
            "manifest_file_sha256": target.binding["release_manifest_sha256"],
            "binding_file_sha256": target.binding_sha256,
            "image_reference": target.image_reference,
            "activation_generation": target.activation_generation,
        }
    )
    events: list[dict[str, object]] = []
    for sequence, (stage, outcome, reason_code) in enumerate(
        rollback_drill._EXPECTED_PROBE_AUDIT_PATH,
        1,
    ):
        event: dict[str, object] = {
            "schema_version": "1.1",
            "observed_at_utc": "2026-08-13T00:00:00Z",
            "stage": stage,
            "outcome": outcome,
            "reason_code": reason_code,
            "duration_ms": 0,
            "request_id_sha256": (
                rollback_drill._EMPTY_SHA256
                if sequence == 1
                else rollback_drill._PROBE_REQUEST_SHA256
            ),
            "question_sha256": (
                rollback_drill._EMPTY_SHA256
                if sequence == 1
                else rollback_drill._PROBE_QUESTION_SHA256
            ),
            "invocation_id_sha256": release_id_sha256,
            "event_sequence": sequence,
            "route_disposition": None,
            "interaction_intent": None,
            "product_families": [],
            "agent_release_id_sha256": release_id_sha256,
            "agent_release_manifest_sha256": target.binding["release_manifest_sha256"],
            "deployment_binding_sha256": target.binding_sha256,
            "release_context_sha256": release_context_sha256,
            "dataset_release_id_sha256": None,
            "approved_dataset_manifest_sha256": None,
            "database_manifest_sha256": None,
            "database_snapshot_sha256": None,
            "source_snapshot_sha256": None,
            "plan_sha256": None,
            "plan_bundle_sha256": None,
            "dataset_bundle_sha256": None,
            "model_revision_sha256": None,
            "model_snapshot_manifest_sha256": None,
            "index_manifest_sha256": None,
            "product_family_count": 0,
            "candidate_count": 0,
            "result_count": 0,
            "evidence_count": 0,
            "shadow_candidate_count": 0,
            "product_id_sha256s": [],
            "evidence_id_sha256s": [],
        }
        if stage == "answer":
            event.update(
                {
                    "route_disposition": "execute",
                    "interaction_intent": "search",
                    "product_families": ["bond"],
                    "dataset_release_id_sha256": "4" * 64,
                    "approved_dataset_manifest_sha256": "5" * 64,
                    "database_manifest_sha256": "6" * 64,
                    "database_snapshot_sha256": "7" * 64,
                    "source_snapshot_sha256": "8" * 64,
                    "plan_sha256": "a" * 64,
                    "product_family_count": 1,
                    "candidate_count": 254,
                    "result_count": 1,
                    "evidence_count": 1,
                    "product_id_sha256s": ["b" * 64],
                    "evidence_id_sha256s": ["c" * 64],
                }
            )
        AuditEvent.model_validate(event)
        events.append(event)
    return events


def test_rollback_stdlib_audit_contract_tracks_audit_event_v11_fields() -> None:
    assert rollback_drill._AUDIT_EVENT_V11_FIELDS == frozenset(AuditEvent.model_fields)


def _audit_payload(events: list[dict[str, object]], *, allow_nan: bool = False) -> bytes:
    return (
        "\n".join(
            json.dumps(
                event,
                ensure_ascii=False,
                separators=(",", ":"),
                allow_nan=allow_nan,
            )
            for event in events
        )
        + "\n"
    ).encode()


def test_audit_chain_requires_every_serialized_audit_event_v11_field(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    previous_env, _, _, _ = _release_pair(tmp_path)
    target = _load_test_target(monkeypatch, previous_env)
    events = _valid_probe_events(target)
    del events[1]["index_manifest_sha256"]

    with pytest.raises(rollback_drill.RollbackDrillError, match="AuditEvent v1.1"):
        rollback_drill.DockerClient._verify_audit_chain(target, _audit_payload(events))


def test_audit_chain_rejects_unknown_audit_event_field(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    previous_env, _, _, _ = _release_pair(tmp_path)
    target = _load_test_target(monkeypatch, previous_env)
    events = _valid_probe_events(target)
    events[1]["raw_question"] = "must never be accepted"

    with pytest.raises(rollback_drill.RollbackDrillError, match="AuditEvent v1.1"):
        rollback_drill.DockerClient._verify_audit_chain(target, _audit_payload(events))


@pytest.mark.parametrize(
    ("event_index", "field", "value", "message"),
    [
        (1, "stage", "unknown", "stage or outcome"),
        (1, "reason_code", "prompt_leaked", "reason code"),
        (1, "duration_ms", -1, "duration"),
        (11, "product_family_count", 2, "product family count"),
        (11, "result_count", 255, "result count exceeds"),
        (11, "route_disposition", "clarify", "non-executable route"),
    ],
)
def test_audit_chain_rejects_invalid_audit_event_v11_semantics(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    event_index: int,
    field: str,
    value: object,
    message: str,
) -> None:
    previous_env, _, _, _ = _release_pair(tmp_path)
    target = _load_test_target(monkeypatch, previous_env)
    events = _valid_probe_events(target)
    events[event_index][field] = value

    with pytest.raises(rollback_drill.RollbackDrillError, match=message):
        rollback_drill.DockerClient._verify_audit_chain(target, _audit_payload(events))


def test_audit_chain_rejects_non_finite_json_number(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    previous_env, _, _, _ = _release_pair(tmp_path)
    target = _load_test_target(monkeypatch, previous_env)
    events = _valid_probe_events(target)
    events[1]["duration_ms"] = float("nan")

    with pytest.raises(rollback_drill.RollbackDrillError, match="non-finite JSON"):
        rollback_drill.DockerClient._verify_audit_chain(
            target,
            _audit_payload(events, allow_nan=True),
        )


@pytest.mark.parametrize(
    ("event_index", "field", "value"),
    [
        (1, "outcome", "failed"),
        (4, "stage", "plan"),
        (6, "reason_code", "completed"),
    ],
)
def test_audit_chain_requires_exact_deterministic_stage_outcome_reason_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    event_index: int,
    field: str,
    value: object,
) -> None:
    previous_env, _, _, _ = _release_pair(tmp_path)
    target = _load_test_target(monkeypatch, previous_env)
    events = _valid_probe_events(target)
    events[event_index][field] = value

    with pytest.raises(rollback_drill.RollbackDrillError, match="exactly deterministic"):
        rollback_drill.DockerClient._verify_audit_chain(target, _audit_payload(events))


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("product_families", ["domestic_etp"], "answer semantics"),
        ("candidate_count", 0, "result count exceeds candidates"),
        ("result_count", 0, "answer semantics"),
        ("evidence_count", 0, "answer semantics"),
        ("product_id_sha256s", [], "answer semantics"),
        ("evidence_id_sha256s", [], "answer semantics"),
    ],
)
def test_audit_chain_requires_complete_answer_semantics(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    field: str,
    value: object,
    message: str,
) -> None:
    previous_env, _, _, _ = _release_pair(tmp_path)
    target = _load_test_target(monkeypatch, previous_env)
    events = _valid_probe_events(target)
    events[11][field] = value
    if field == "product_families":
        events[11]["product_family_count"] = 1
    if field == "result_count":
        events[11]["product_id_sha256s"] = []
    if field == "evidence_count":
        events[11]["evidence_id_sha256s"] = []

    with pytest.raises(rollback_drill.RollbackDrillError, match=message):
        rollback_drill.DockerClient._verify_audit_chain(target, _audit_payload(events))


def test_rollback_target_rejects_audit_directory_not_owned_by_uid_10001(
    tmp_path: Path,
) -> None:
    if os.geteuid() == 10001:
        pytest.skip("test process already owns files as release UID 10001")
    previous_env, _, _, _ = _release_pair(tmp_path)

    with pytest.raises(rollback_drill.RollbackDrillError, match="UID 10001"):
        rollback_drill._load_target(previous_env)


def test_secure_audit_file_requires_release_uid_10001(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    audit_file = tmp_path / "events.jsonl"
    audit_file.write_text("{}\n", encoding="utf-8")
    audit_file.chmod(0o600)
    fields = list(audit_file.stat())
    fields[4] = 10001
    release_owned = os.stat_result(fields)
    fields[4] = 10002
    other_owned = os.stat_result(fields)
    monkeypatch.setattr(rollback_drill, "_RELEASE_BACKEND_UID", 10001)

    assert rollback_drill._secure_audit_file(release_owned) is True
    assert rollback_drill._secure_audit_file(other_owned) is False


def test_rollback_target_rejects_symlinked_audit_directory(
    tmp_path: Path,
) -> None:
    previous_env, _, _, _ = _release_pair(tmp_path)
    real_audit = tmp_path / "real-audit"
    real_audit.mkdir(mode=0o700)
    linked_audit = tmp_path / "linked-audit"
    linked_audit.symlink_to(real_audit, target_is_directory=True)
    previous_env.write_text(
        previous_env.read_text(encoding="utf-8").replace(
            f"FINANCE_AUDIT_HOST_DIR={tmp_path / 'audit'}",
            f"FINANCE_AUDIT_HOST_DIR={linked_audit}",
        ),
        encoding="utf-8",
    )

    with pytest.raises(rollback_drill.RollbackDrillError, match="symlink"):
        rollback_drill._load_target(previous_env)


def test_audit_checkpoint_rejects_symlinked_or_hardlinked_jsonl(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    previous_env, _, _, _ = _release_pair(tmp_path)
    target = _load_test_target(monkeypatch, previous_env)
    audit_root = Path(target.environment["FINANCE_AUDIT_HOST_DIR"])
    victim = tmp_path / "victim.jsonl"
    victim.write_text("{}\n", encoding="utf-8")
    victim.chmod(0o600)
    audit_file = audit_root / "events.jsonl"
    audit_file.symlink_to(victim)
    with pytest.raises(rollback_drill.RollbackDrillError, match="unavailable"):
        rollback_drill.DockerClient._audit_checkpoint(target)

    audit_file.unlink()
    os.link(victim, audit_file)
    with pytest.raises(rollback_drill.RollbackDrillError, match="secure regular file"):
        rollback_drill.DockerClient._audit_checkpoint(target)


def test_audit_checkpoint_rejects_file_substitution_race(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    previous_env, _, _, _ = _release_pair(tmp_path)
    target = _load_test_target(monkeypatch, previous_env)
    audit_root = Path(target.environment["FINANCE_AUDIT_HOST_DIR"])
    audit_file = audit_root / "events.jsonl"
    audit_file.write_text("{}\n", encoding="utf-8")
    audit_file.chmod(0o600)
    replacement = tmp_path / "replacement.jsonl"
    replacement.write_text("{}\n", encoding="utf-8")
    replacement.chmod(0o600)
    replacement_metadata = replacement.stat()
    real_stat = rollback_drill.os.stat

    def substituted_stat(path: object, *args: object, **kwargs: object) -> os.stat_result:
        if path == "events.jsonl" and kwargs.get("dir_fd") is not None:
            return replacement_metadata
        return real_stat(path, *args, **kwargs)

    monkeypatch.setattr(rollback_drill.os, "stat", substituted_stat)
    with pytest.raises(rollback_drill.RollbackDrillError, match="secure regular file"):
        rollback_drill.DockerClient._audit_checkpoint(target)


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
    _copy_test_rollback_script(scripts / "rollback_drill.py")
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
    assert result["audit_chain_verified"] is True
    assert len(result["audit_observations"]) == 3
    assert [item["release_id"] for item in result["audit_observations"]] == [
        previous["release_id"],
        current["release_id"],
        previous["release_id"],
    ]
    assert all(item["event_count"] == 13 for item in result["audit_observations"])
    assert all(item["terminal_sequence"] == 13 for item in result["audit_observations"])
    audit_root = Path(
        next(
            line.split("=", 1)[1]
            for line in previous_env.read_text(encoding="utf-8").splitlines()
            if line.startswith("FINANCE_AUDIT_HOST_DIR=")
        )
    )
    audit_events = [
        AuditEvent.model_validate_json(line)
        for line in (audit_root / "events.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert len(audit_events) == 39
    assert [event.event_sequence for event in audit_events] == [*range(1, 14)] * 3
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
    _copy_test_rollback_script(scripts / "rollback_drill.py")
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
