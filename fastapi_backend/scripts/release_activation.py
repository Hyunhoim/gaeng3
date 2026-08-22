from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import re
import stat
import subprocess
import sys
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Literal

# These are deliberately not configurable through the release environment.  A
# deployer must provision them as root-controlled host paths.  Tests exercise a
# copied harness with these literals replaced; production has no path override.
ACTIVE_STATE_FILE = Path("/var/lib/finance-agent-release/active-binding.json")
ACTIVATION_LOCK_FILE = Path("/run/lock/finance-agent-release/activation.lock")
REQUIRED_OWNER_UID = 0

_IMAGE_REFERENCE = re.compile(r"^[a-z0-9][a-z0-9._:/-]{2,255}@sha256:[0-9a-f]{64}$")
_RELEASE_ID = re.compile(r"^[a-z0-9][a-z0-9._-]{7,127}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_MAX_FILE_BYTES = 2 * 1024 * 1024
_STATE_KEYS = {
    "schema_version",
    "binding_sha256",
    "release_id",
    "release_manifest_sha256",
    "image_reference",
    "environment",
    "platform",
    "activation_generation",
}
_BINDING_KEYS = {
    "schema_version",
    "release_id",
    "environment",
    "source_commit",
    "release_manifest_sha256",
    "image_reference",
    "platform",
    "activation_generation",
    "rollback",
}
_ROLLBACK_KEYS = {
    "mode",
    "target_release_id",
    "target_manifest_sha256",
    "target_binding_sha256",
    "target_image_reference",
    "target_activation_generation",
    "target_environment",
    "target_platform",
}


class ReleaseActivationError(RuntimeError):
    """Fail-closed host activation state or sequencing failure."""


@dataclass(frozen=True, slots=True)
class ActivationRecord:
    schema_version: Literal["1.0"]
    binding_sha256: str
    release_id: str
    release_manifest_sha256: str
    image_reference: str
    environment: Literal["evaluation", "production"]
    platform: Literal["linux/amd64", "linux/arm64"]
    activation_generation: int


def _strict_object(data: bytes, label: str) -> dict[str, Any]:
    def reject_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ReleaseActivationError(f"{label} contains a duplicate JSON key")
            result[key] = value
        return result

    def reject_constant(value: str) -> None:
        raise ReleaseActivationError(f"{label} contains a non-finite number: {value}")

    try:
        value = json.loads(
            data,
            object_pairs_hook=reject_duplicates,
            parse_constant=reject_constant,
        )
    except (UnicodeError, json.JSONDecodeError) as error:
        raise ReleaseActivationError(f"{label} is not strict JSON") from error
    if not isinstance(value, dict):
        raise ReleaseActivationError(f"{label} must be a JSON object")
    return value


def _fingerprint(info: os.stat_result) -> tuple[int, ...]:
    return (
        info.st_dev,
        info.st_ino,
        info.st_mode,
        info.st_uid,
        info.st_nlink,
        info.st_size,
        info.st_mtime_ns,
        info.st_ctime_ns,
    )


def _read_secure_file(
    path: Path,
    label: str,
    *,
    read_only: bool,
    required_owner: int | None = None,
) -> bytes:
    if not path.is_absolute():
        raise ReleaseActivationError(f"{label} must use an absolute path")
    try:
        before = path.stat(follow_symlinks=False)
    except OSError as error:
        raise ReleaseActivationError(f"{label} is unavailable") from error
    forbidden_write_bits = stat.S_IWGRP | stat.S_IWOTH
    if read_only:
        forbidden_write_bits |= stat.S_IWUSR
    if (
        not stat.S_ISREG(before.st_mode)
        or before.st_nlink != 1
        or (required_owner is not None and before.st_uid != required_owner)
        or before.st_mode & forbidden_write_bits
        or before.st_size <= 0
        or before.st_size > _MAX_FILE_BYTES
    ):
        raise ReleaseActivationError(f"{label} is not a secure regular file")
    descriptor: int | None = None
    try:
        descriptor = os.open(
            path,
            os.O_RDONLY | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0),
        )
        chunks: list[bytes] = []
        remaining = before.st_size
        while remaining:
            chunk = os.read(descriptor, min(remaining, 1024 * 1024))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        data = b"".join(chunks)
        opened = os.fstat(descriptor)
        current = path.stat(follow_symlinks=False)
    except OSError as error:
        raise ReleaseActivationError(f"{label} is unreadable") from error
    finally:
        if descriptor is not None:
            os.close(descriptor)
    if (
        _fingerprint(before) != _fingerprint(opened)
        or _fingerprint(opened) != _fingerprint(current)
        or len(data) != before.st_size
    ):
        raise ReleaseActivationError(f"{label} changed while reading or has an invalid size")
    return data


def _load_environment(path: Path) -> dict[str, str]:
    raw = _read_secure_file(path, "release environment snapshot", read_only=False)
    try:
        text = raw.decode("utf-8")
    except UnicodeError as error:
        raise ReleaseActivationError("release environment snapshot is not UTF-8") from error
    result: dict[str, str] = {}
    for line_number, raw_line in enumerate(text.splitlines(), 1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            raise ReleaseActivationError(
                f"release environment snapshot line {line_number} is invalid"
            )
        key, value = line.split("=", 1)
        if re.fullmatch(r"[A-Z][A-Z0-9_]*", key) is None or key in result:
            raise ReleaseActivationError("release environment key is invalid or duplicated")
        if len(value) < 2 or not (value.startswith("'") and value.endswith("'")):
            raise ReleaseActivationError("release environment value is not safely quoted")
        value = value[1:-1]
        if "'" in value or "\r" in value or "\n" in value:
            raise ReleaseActivationError("release environment value is unsafe")
        result[key] = value
    return result


def _require_exact_keys(value: dict[str, Any], expected: set[str], label: str) -> None:
    if set(value) != expected:
        raise ReleaseActivationError(f"{label} fields differ from the activation contract")


def _require_pattern(value: object, pattern: re.Pattern[str], label: str) -> str:
    if not isinstance(value, str) or pattern.fullmatch(value) is None:
        raise ReleaseActivationError(f"{label} is invalid")
    return value


def _record_from_binding(binding: dict[str, Any], binding_sha256: str) -> ActivationRecord:
    _require_exact_keys(binding, _BINDING_KEYS, "DeploymentBinding")
    if binding.get("schema_version") != "1.0":
        raise ReleaseActivationError("DeploymentBinding schema version is invalid")
    environment = binding.get("environment")
    if environment not in {"evaluation", "production"}:
        raise ReleaseActivationError("DeploymentBinding environment is invalid")
    platform = binding.get("platform")
    if platform not in {"linux/amd64", "linux/arm64"}:
        raise ReleaseActivationError("DeploymentBinding platform is invalid")
    generation = binding.get("activation_generation")
    if isinstance(generation, bool) or not isinstance(generation, int) or generation < 1:
        raise ReleaseActivationError("DeploymentBinding activation generation is invalid")
    rollback = binding.get("rollback")
    if not isinstance(rollback, dict):
        raise ReleaseActivationError("DeploymentBinding rollback object is invalid")
    _require_exact_keys(rollback, _ROLLBACK_KEYS, "DeploymentBinding rollback")
    return ActivationRecord(
        schema_version="1.0",
        binding_sha256=_require_pattern(binding_sha256, _SHA256, "Binding SHA-256"),
        release_id=_require_pattern(binding.get("release_id"), _RELEASE_ID, "release ID"),
        release_manifest_sha256=_require_pattern(
            binding.get("release_manifest_sha256"),
            _SHA256,
            "release manifest SHA-256",
        ),
        image_reference=_require_pattern(
            binding.get("image_reference"),
            _IMAGE_REFERENCE,
            "image reference",
        ),
        environment=environment,
        platform=platform,
        activation_generation=generation,
    )


def _load_candidate(env_file: Path) -> tuple[ActivationRecord, dict[str, Any]]:
    environment = _load_environment(env_file)
    try:
        binding_path = Path(environment["FINANCE_DEPLOYMENT_BINDING_HOST_FILE"])
        expected_sha256 = environment["FINANCE_DEPLOYMENT_BINDING_SHA256"]
    except KeyError as error:
        raise ReleaseActivationError("release environment omits the DeploymentBinding") from error
    _require_pattern(expected_sha256, _SHA256, "expected Binding SHA-256")
    data = _read_secure_file(binding_path, "DeploymentBinding snapshot", read_only=True)
    actual_sha256 = hashlib.sha256(data).hexdigest()
    if actual_sha256 != expected_sha256:
        raise ReleaseActivationError("DeploymentBinding differs from its trusted SHA-256")
    binding = _strict_object(data, "DeploymentBinding")
    return _record_from_binding(binding, actual_sha256), binding


def _record_from_state(data: bytes) -> ActivationRecord:
    value = _strict_object(data, "active release state")
    _require_exact_keys(value, _STATE_KEYS, "active release state")
    try:
        record = ActivationRecord(**value)
    except TypeError as error:
        raise ReleaseActivationError("active release state fields are invalid") from error
    # Reuse all value checks applied to a candidate Binding.
    _require_pattern(record.binding_sha256, _SHA256, "active Binding SHA-256")
    _require_pattern(record.release_id, _RELEASE_ID, "active release ID")
    _require_pattern(record.release_manifest_sha256, _SHA256, "active manifest SHA-256")
    _require_pattern(record.image_reference, _IMAGE_REFERENCE, "active image reference")
    if record.schema_version != "1.0":
        raise ReleaseActivationError("active release state schema version is invalid")
    if record.environment not in {"evaluation", "production"}:
        raise ReleaseActivationError("active release environment is invalid")
    if record.platform not in {"linux/amd64", "linux/arm64"}:
        raise ReleaseActivationError("active release platform is invalid")
    if (
        isinstance(record.activation_generation, bool)
        or not isinstance(record.activation_generation, int)
        or record.activation_generation < 1
    ):
        raise ReleaseActivationError("active release generation is invalid")
    return record


def _load_active_state() -> ActivationRecord | None:
    try:
        ACTIVE_STATE_FILE.stat(follow_symlinks=False)
    except FileNotFoundError:
        return None
    except OSError as error:
        raise ReleaseActivationError("active release state is unavailable") from error
    data = _read_secure_file(
        ACTIVE_STATE_FILE,
        "active release state",
        read_only=True,
        required_owner=REQUIRED_OWNER_UID,
    )
    return _record_from_state(data)


def _validate_transition(
    candidate: ActivationRecord,
    binding: dict[str, Any],
    active: ActivationRecord | None,
) -> Literal["bootstrap", "advance", "idempotent"]:
    rollback = binding["rollback"]
    if active is None:
        if candidate.activation_generation != 1:
            raise ReleaseActivationError("first host activation must use generation 1")
        if rollback != {
            "mode": "initial_bootstrap",
            "target_release_id": None,
            "target_manifest_sha256": None,
            "target_binding_sha256": None,
            "target_image_reference": None,
            "target_activation_generation": None,
            "target_environment": None,
            "target_platform": None,
        }:
            raise ReleaseActivationError("first host activation must be initial_bootstrap")
        return "bootstrap"

    if candidate.binding_sha256 == active.binding_sha256:
        if candidate != active:
            raise ReleaseActivationError("active Binding identity is internally inconsistent")
        return "idempotent"

    if candidate.activation_generation != active.activation_generation + 1:
        raise ReleaseActivationError(
            "release activation generation must immediately follow the active host state"
        )
    expected_rollback = {
        "mode": "pinned_previous_release",
        "target_release_id": active.release_id,
        "target_manifest_sha256": active.release_manifest_sha256,
        "target_binding_sha256": active.binding_sha256,
        "target_image_reference": active.image_reference,
        "target_activation_generation": active.activation_generation,
        "target_environment": active.environment,
        "target_platform": active.platform,
    }
    if rollback != expected_rollback:
        raise ReleaseActivationError(
            "DeploymentBinding rollback does not exactly pin the active host release"
        )
    return "advance"


def _ensure_control_directory(path: Path) -> None:
    if not path.is_absolute():
        raise ReleaseActivationError("release activation control path must be absolute")
    try:
        path.mkdir(mode=0o700)
    except FileExistsError:
        pass
    except OSError as error:
        raise ReleaseActivationError(
            "release activation control directory is unavailable"
        ) from error
    try:
        info = path.stat(follow_symlinks=False)
    except OSError as error:
        raise ReleaseActivationError(
            "release activation control directory is unavailable"
        ) from error
    if (
        not stat.S_ISDIR(info.st_mode)
        or info.st_uid != REQUIRED_OWNER_UID
        or info.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
    ):
        raise ReleaseActivationError("release activation control directory is not root-controlled")


def _open_lock() -> int:
    _ensure_control_directory(ACTIVATION_LOCK_FILE.parent)
    flags = os.O_RDWR | os.O_CREAT | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0)
    descriptor: int | None = None
    try:
        descriptor = os.open(ACTIVATION_LOCK_FILE, flags, 0o600)
        os.fchmod(descriptor, 0o600)
        info = os.fstat(descriptor)
        current = ACTIVATION_LOCK_FILE.stat(follow_symlinks=False)
    except OSError as error:
        if descriptor is not None:
            os.close(descriptor)
        raise ReleaseActivationError("release activation lock is unavailable") from error
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_uid != REQUIRED_OWNER_UID
        or info.st_nlink != 1
        or info.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
        or _fingerprint(info) != _fingerprint(current)
    ):
        os.close(descriptor)
        raise ReleaseActivationError("release activation lock is not root-controlled")
    fcntl.flock(descriptor, fcntl.LOCK_EX)
    return descriptor


def _write_active_state(record: ActivationRecord) -> None:
    _ensure_control_directory(ACTIVE_STATE_FILE.parent)
    payload = (
        json.dumps(asdict(record), sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n"
    ).encode("utf-8")
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=".active-binding.",
        dir=ACTIVE_STATE_FILE.parent,
    )
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, 0o400)
        view = memoryview(payload)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                raise ReleaseActivationError("cannot write active release state")
            view = view[written:]
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = -1
        os.replace(temporary, ACTIVE_STATE_FILE)
        directory = os.open(
            ACTIVE_STATE_FILE.parent,
            os.O_RDONLY | os.O_CLOEXEC | getattr(os, "O_DIRECTORY", 0),
        )
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    except Exception:
        if descriptor >= 0:
            os.close(descriptor)
        temporary.unlink(missing_ok=True)
        raise


def _validate_compose_arguments(arguments: list[str]) -> None:
    if arguments.count("up") != 1:
        raise ReleaseActivationError("activation helper requires exactly one Compose up command")
    up_index = arguments.index("up")
    prefix = arguments[:up_index]
    expects_value = False
    for argument in prefix:
        if expects_value:
            if not argument or argument.startswith("-"):
                raise ReleaseActivationError("Compose global option value is invalid")
            expects_value = False
        elif argument in {"--ansi", "--progress"}:
            expects_value = True
        else:
            raise ReleaseActivationError("activation helper rejects a Compose global override")
    if expects_value:
        raise ReleaseActivationError("Compose global option value is missing")

    allowed_flags = {
        "-d",
        "--detach",
        "--wait",
        "--remove-orphans",
        "--quiet-pull",
        "--timestamps",
        "--no-color",
        "--yes",
        "--no-build",
        "--force-recreate",
    }
    allowed_values = {"--wait-timeout", "--timeout", "--pull"}
    expects_value = False
    for argument in arguments[up_index + 1 :]:
        if expects_value:
            if not argument or argument.startswith("-"):
                raise ReleaseActivationError("Compose up option value is invalid")
            expects_value = False
        elif argument in allowed_flags:
            continue
        elif argument in allowed_values:
            expects_value = True
        elif any(argument.startswith(name + "=") for name in allowed_values):
            continue
        else:
            raise ReleaseActivationError("activation helper rejects a Compose up override")
    if expects_value:
        raise ReleaseActivationError("Compose up option value is missing")
    for required in ("--wait", "--no-build", "--force-recreate"):
        if required not in arguments[up_index + 1 :]:
            raise ReleaseActivationError(f"activation helper requires {required}")


def _compose_command(env_file: Path, arguments: list[str]) -> list[str]:
    compose_files = [
        "-f",
        "docker-compose.yml",
        "-f",
        "fastapi_backend/docker-compose.release.yml",
    ]
    adaptive = (
        _load_environment(env_file).get("FINANCE_ADAPTIVE_SEMANTIC_ENABLED", "false").casefold()
    )
    if adaptive not in {"true", "false"}:
        raise ReleaseActivationError("FINANCE_ADAPTIVE_SEMANTIC_ENABLED must be true or false")
    if adaptive == "true":
        compose_files.extend(["-f", "fastapi_backend/docker-compose.adaptive.yml"])
    return [
        "docker",
        "compose",
        "-p",
        "hyunholim-finance-agent",
        "--env-file",
        str(env_file),
        *compose_files,
        *arguments,
    ]


def _run_trust_verification(env_file: Path) -> None:
    repository_root = Path(__file__).resolve().parents[2]
    completed = subprocess.run(
        [
            sys.executable,
            str(repository_root / "fastapi_backend/scripts/release_trust.py"),
            "--env-file",
            str(env_file),
        ],
        cwd=repository_root,
        check=False,
    )
    if completed.returncode != 0:
        raise ReleaseActivationError("release trust verification failed before activation")


def activate(
    env_file: Path, compose_arguments: list[str]
) -> Literal["bootstrap", "advance", "idempotent"]:
    _validate_compose_arguments(compose_arguments)
    lock_descriptor = _open_lock()
    try:
        _ensure_control_directory(ACTIVE_STATE_FILE.parent)
        _run_trust_verification(env_file)
        candidate, binding = _load_candidate(env_file)
        decision = _validate_transition(candidate, binding, _load_active_state())
        repository_root = Path(__file__).resolve().parents[2]
        completed = subprocess.run(
            _compose_command(env_file, compose_arguments),
            cwd=repository_root,
            check=False,
        )
        if completed.returncode != 0:
            raise ReleaseActivationError("Compose activation failed health readiness")
        try:
            _write_active_state(candidate)
        except Exception:
            # Never leave an unrecorded new release serving after an atomic state
            # failure.  Volumes and images remain intact for explicit recovery.
            command = _compose_command(env_file, ["up", "--wait", "--no-build", "--force-recreate"])
            down_prefix = command[: command.index("up")]
            subprocess.run([*down_prefix, "down"], cwd=repository_root, check=False)
            raise
        return decision
    finally:
        os.close(lock_descriptor)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Serialize and anti-replay a trusted release activation."
    )
    parser.add_argument("--env-file", type=Path, required=True)
    parser.add_argument("compose_arguments", nargs=argparse.REMAINDER)
    return parser


def main() -> None:
    arguments = _parser().parse_args()
    compose_arguments = list(arguments.compose_arguments)
    if compose_arguments[:1] == ["--"]:
        compose_arguments = compose_arguments[1:]
    try:
        decision = activate(arguments.env_file, compose_arguments)
    except (OSError, ReleaseActivationError) as error:
        print(f"release activation failed closed: {error}", file=sys.stderr)
        raise SystemExit(1) from error
    print(f"release_activation={decision}")


if __name__ == "__main__":
    main()
