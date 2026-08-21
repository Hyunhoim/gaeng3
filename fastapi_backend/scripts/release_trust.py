from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import stat
import subprocess
import sys
import tempfile
from collections.abc import Sequence
from pathlib import Path
from typing import Any

COSIGN_VERSION = "v3.1.3"
COSIGN_BINARY = Path("/usr/local/bin/cosign")
COSIGN_BINARY_SHA256 = "4629c757b7618056f8ddd7e2625ae9fdd94c0372a65049520bc7d9df9efc7f71"
WORKFLOW_IDENTITY = (
    "https://github.com/Hyunhoim/gaeng3/.github/workflows/immutable-ncp-release.yml@refs/heads/main"
)
OIDC_ISSUER = "https://token.actions.githubusercontent.com"
_IMAGE_REFERENCE = re.compile(r"^[a-z0-9][a-z0-9._:/-]{2,255}@sha256:[0-9a-f]{64}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_MAX_CONTROL_FILE_BYTES = 2 * 1024 * 1024
_MAX_ENVIRONMENT_FILE_BYTES = 128 * 1024
_MAX_COSIGN_BINARY_BYTES = 256 * 1024 * 1024


class ReleaseTrustError(ValueError):
    pass


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


def _require_secure_metadata(path: Path, label: str) -> os.stat_result:
    if not path.is_absolute():
        raise ReleaseTrustError(f"{label} must be an absolute path")
    try:
        info = path.stat(follow_symlinks=False)
    except OSError as error:
        raise ReleaseTrustError(f"{label} is unavailable") from error
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_nlink != 1
        or info.st_uid not in {0, os.geteuid()}
        or info.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
    ):
        raise ReleaseTrustError(f"{label} is not a secure regular file")
    return info


def _read_secure_file(path: Path, label: str, *, maximum_bytes: int) -> bytes:
    before = _require_secure_metadata(path, label)
    descriptor: int | None = None
    try:
        descriptor = os.open(
            path,
            os.O_RDONLY | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0),
        )
        data = os.read(descriptor, maximum_bytes + 1)
        opened = os.fstat(descriptor)
        current = path.stat(follow_symlinks=False)
    except OSError as error:
        raise ReleaseTrustError(f"{label} is unreadable") from error
    finally:
        if descriptor is not None:
            os.close(descriptor)
    if (
        _fingerprint(before) != _fingerprint(opened)
        or _fingerprint(opened) != _fingerprint(current)
        or not data
        or len(data) > maximum_bytes
    ):
        raise ReleaseTrustError(f"{label} changed while reading or has an invalid size")
    return data


def _load_environment(path: Path) -> dict[str, str]:
    data = _read_secure_file(
        path,
        "release environment file",
        maximum_bytes=_MAX_ENVIRONMENT_FILE_BYTES,
    )
    try:
        raw = data.decode("utf-8")
    except UnicodeError as error:
        raise ReleaseTrustError("release environment file is not UTF-8") from error
    values: dict[str, str] = {}
    for line_number, raw_line in enumerate(raw.splitlines(), 1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            raise ReleaseTrustError(f"invalid release environment line {line_number}")
        key, value = line.split("=", 1)
        key = key.strip()
        if re.fullmatch(r"[A-Z][A-Z0-9_]*", key) is None or key in values:
            raise ReleaseTrustError("release environment key is invalid or duplicated")
        value = value.strip()
        if value.startswith("'") or value.endswith("'"):
            if len(value) < 2 or not (value.startswith("'") and value.endswith("'")):
                raise ReleaseTrustError("release environment quoting is invalid")
            value = value[1:-1]
        if "'" in value:
            raise ReleaseTrustError("release environment value is invalid")
        values[key] = value
    return values


def _secure_regular_file(path_text: str, label: str) -> Path:
    path = Path(path_text)
    _require_secure_metadata(path, label)
    return path


def _write_snapshot(root: Path, name: str, data: bytes) -> Path:
    path = root / name
    descriptor = os.open(
        path,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
        0o400,
    )
    try:
        view = memoryview(data)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                raise ReleaseTrustError("cannot create release verification snapshot")
            view = view[written:]
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    return path


def _require_root_owned_path(path: Path) -> None:
    current = path
    while True:
        try:
            info = current.stat(follow_symlinks=False)
        except OSError as error:
            raise ReleaseTrustError("cosign installation path is unavailable") from error
        if info.st_uid != 0 or info.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
            raise ReleaseTrustError("cosign installation path is not root-controlled")
        if current == current.parent:
            break
        current = current.parent


def _strict_object(data: bytes, label: str) -> dict[str, Any]:
    def reject_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ReleaseTrustError(f"{label} contains a duplicate JSON key")
            result[key] = value
        return result

    def reject_constant(value: str) -> None:
        raise ReleaseTrustError(f"{label} contains a non-finite number: {value}")

    try:
        value = json.loads(
            data,
            object_pairs_hook=reject_duplicates,
            parse_constant=reject_constant,
        )
    except (UnicodeError, json.JSONDecodeError) as error:
        raise ReleaseTrustError(f"{label} is not strict JSON") from error
    if not isinstance(value, dict):
        raise ReleaseTrustError(f"{label} must be a JSON object")
    return value


def _cosign_binary() -> Path:
    binary = COSIGN_BINARY
    if binary != Path("/usr/local/bin/cosign"):
        raise ReleaseTrustError("cosign binary path is not the deployment contract")
    _require_root_owned_path(binary)
    data = _read_secure_file(
        binary,
        "cosign binary",
        maximum_bytes=_MAX_COSIGN_BINARY_BYTES,
    )
    if hashlib.sha256(data).hexdigest() != COSIGN_BINARY_SHA256:
        raise ReleaseTrustError("cosign binary SHA-256 differs from v3.1.3")
    completed = subprocess.run(
        [str(binary), "version"],
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
        env=_version_environment(),
    )
    if completed.returncode != 0 or COSIGN_VERSION not in completed.stdout:
        raise ReleaseTrustError(f"cosign {COSIGN_VERSION} is required")
    return binary


def _version_environment() -> dict[str, str]:
    return {
        "HOME": "/nonexistent",
        "PATH": "/usr/bin:/bin",
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
    }


def _verification_environment() -> dict[str, str]:
    environment = {
        "PATH": "/usr/bin:/bin",
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "COSIGN_DOCKER_MEDIA_TYPES": "1",
    }
    for key in (
        "HOME",
        "DOCKER_CONFIG",
        "SSL_CERT_FILE",
        "SSL_CERT_DIR",
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "NO_PROXY",
        "http_proxy",
        "https_proxy",
        "no_proxy",
        "XDG_CACHE_HOME",
    ):
        value = os.environ.get(key)
        if value:
            environment[key] = value
    return environment


def _run_verification(binary: Path, arguments: Sequence[str]) -> None:
    completed = subprocess.run(
        [str(binary), *arguments],
        check=False,
        capture_output=True,
        text=True,
        timeout=90,
        env=_verification_environment(),
    )
    if completed.returncode != 0:
        raise ReleaseTrustError("cosign release verification failed closed")


def _object_member(value: object, key: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ReleaseTrustError("signed release profile is invalid")
    member = value.get(key)
    if not isinstance(member, dict):
        raise ReleaseTrustError("signed release profile is invalid")
    return member


def _boolean_environment(values: dict[str, str], name: str, *, default: str) -> bool:
    raw = values.get(name, default).casefold()
    if raw not in {"true", "false"}:
        raise ReleaseTrustError("release environment profile is invalid")
    return raw == "true"


def _integer_environment(
    values: dict[str, str],
    name: str,
    *,
    default: str,
) -> int:
    raw = values.get(name, default)
    if re.fullmatch(r"(?:0|[1-9][0-9]*)", raw) is None:
        raise ReleaseTrustError("release environment profile is invalid")
    return int(raw)


def _float_environment(
    values: dict[str, str],
    name: str,
    *,
    default: str,
) -> float:
    try:
        value = float(values.get(name, default))
    except ValueError:
        raise ReleaseTrustError("release environment profile is invalid") from None
    if not math.isfinite(value):
        raise ReleaseTrustError("release environment profile is invalid")
    return value


def _signed_manifest_profile(
    manifest: dict[str, Any],
    binding: dict[str, Any],
) -> dict[str, Any]:
    """Extract the signed runtime authority without importing application packages."""

    components = _object_member(manifest, "components")
    features = _object_member(components, "runtime_features")
    model = _object_member(features, "model")
    retrieval = _object_member(features, "retrieval")
    controls = _object_member(components, "runtime_controls")
    knowledge = _object_member(components, "knowledge_retrieval")
    relation = _object_member(knowledge, "relation")
    document = _object_member(knowledge, "document")

    queryplan_enabled = model.get("queryplan_operation_enabled")
    answer_enabled = model.get("grounded_answer_operation_enabled")
    if type(queryplan_enabled) is not bool or type(answer_enabled) is not bool:
        raise ReleaseTrustError("signed release profile is invalid")
    provider = model.get("provider")
    model_id = model.get("model_id")
    revision_status = model.get("revision_status")
    if provider == "disabled":
        if (
            queryplan_enabled
            or answer_enabled
            or model_id is not None
            or revision_status != "not_used"
        ):
            raise ReleaseTrustError("signed release profile is invalid")
        answer_provider = "deterministic"
    elif provider == "hyperclova":
        if (
            not (queryplan_enabled or answer_enabled)
            or model_id != "HCX-007"
            or revision_status != "provider_revision_not_exposed"
        ):
            raise ReleaseTrustError("signed release profile is invalid")
        answer_provider = "hyperclova" if answer_enabled else "deterministic"
    else:
        raise ReleaseTrustError("signed release profile is invalid")

    if retrieval != {
        "schema_dense": "disabled_offline_only",
        "schema_dense_manifest_sha256": None,
        "embedding_model_revision": None,
        "product_dense": "disabled_not_implemented",
        "reranker": "disabled_not_implemented",
        "document_bm25": "disabled_no_approved_corpus",
    }:
        raise ReleaseTrustError("signed release profile is invalid")
    if relation.get("status") != "activated" or not isinstance(
        relation.get("artifact_file_sha256"), str
    ):
        raise ReleaseTrustError("signed release profile is invalid")
    if document != {"status": "disabled_no_approved_corpus", "artifact": None}:
        raise ReleaseTrustError("signed release profile is invalid")
    if (
        controls.get("audit_schema_version") != "1.2"
        or controls.get("audit_sink_kind") != "append_only_jsonl"
        or type(controls.get("audit_fsync_each_event")) is not bool
    ):
        raise ReleaseTrustError("signed release profile is invalid")
    numeric_fields = {
        "hcx_timeout_seconds": (int, float),
        "official_answer_timeout_seconds": (int, float),
        "official_answer_max_inflight": (int,),
        "worker_count": (int,),
        "audit_queue_capacity": (int,),
        "audit_shutdown_timeout_seconds": (int, float),
    }
    for name, accepted_types in numeric_fields.items():
        value = controls.get(name)
        if type(value) not in accepted_types or not math.isfinite(float(value)):
            raise ReleaseTrustError("signed release profile is invalid")

    environment = manifest.get("environment")
    source_commit = manifest.get("source_commit")
    release_id = manifest.get("release_id")
    if (
        manifest.get("schema_version") != "1.2"
        or environment not in {"evaluation", "production"}
        or not isinstance(source_commit, str)
        or not isinstance(release_id, str)
        or binding.get("environment") != environment
        or binding.get("source_commit") != source_commit
        or binding.get("release_id") != release_id
    ):
        raise ReleaseTrustError("signed release profile is invalid")

    fund_policy = features.get("fund_execution_policy")
    if fund_policy not in {"locked", "public_fund_v1_approved"}:
        raise ReleaseTrustError("signed release profile is invalid")
    return {
        "environment": environment,
        "source_commit": source_commit,
        "platform": binding.get("platform"),
        "answer_provider": answer_provider,
        "hcx_queryplan_enabled": queryplan_enabled,
        "hcx_model": model_id,
        "fund_execution_policy": fund_policy,
        "relation_artifact_sha256": relation["artifact_file_sha256"],
        "hcx_timeout_seconds": float(controls["hcx_timeout_seconds"]),
        "official_answer_timeout_seconds": float(controls["official_answer_timeout_seconds"]),
        "official_answer_max_inflight": controls["official_answer_max_inflight"],
        "worker_count": controls["worker_count"],
        "audit_queue_capacity": controls["audit_queue_capacity"],
        "audit_shutdown_timeout_seconds": float(controls["audit_shutdown_timeout_seconds"]),
        "audit_fsync_each_event": controls["audit_fsync_each_event"],
    }


def _release_environment_profile(values: dict[str, str]) -> dict[str, Any]:
    answer_provider = values["FINANCE_BACKEND_ANSWER_PROVIDER"]
    if answer_provider not in {"deterministic", "hyperclova"}:
        raise ReleaseTrustError("release environment profile is invalid")
    queryplan_enabled = _boolean_environment(
        values,
        "FINANCE_BACKEND_HCX_QUERY_PLAN_ENABLED",
        default="false",
    )
    uses_hcx = answer_provider == "hyperclova" or queryplan_enabled
    if uses_hcx:
        if (
            values.get("FINANCE_AGENT_LLM_MODE") != values["APP_ENV"]
            or values.get("LLM_PROVIDER") != "hyperclova"
            or values.get("HCX_MODEL") != "HCX-007"
        ):
            raise ReleaseTrustError("release environment profile is invalid")
        model_id: str | None = "HCX-007"
    else:
        if (
            values.get("FINANCE_AGENT_LLM_MODE", "disabled") != "disabled"
            or values.get("LLM_PROVIDER", "disabled") != "disabled"
            or values.get("HCX_MODEL") not in {None, ""}
        ):
            raise ReleaseTrustError("release environment profile is invalid")
        model_id = None
    fund_policy = values["FINANCE_BACKEND_FUND_EXECUTION_POLICY"]
    if fund_policy not in {"locked", "public_fund_v1_approved"}:
        raise ReleaseTrustError("release environment profile is invalid")
    platform = values["FINANCE_RUNTIME_PLATFORM"]
    if platform not in {"linux/amd64", "linux/arm64"}:
        raise ReleaseTrustError("release environment profile is invalid")
    return {
        "environment": values["APP_ENV"],
        "source_commit": values["FINANCE_SOURCE_COMMIT"],
        "platform": platform,
        "answer_provider": answer_provider,
        "hcx_queryplan_enabled": queryplan_enabled,
        "hcx_model": model_id,
        "fund_execution_policy": fund_policy,
        "relation_artifact_sha256": values["FINANCE_RELATION_RETRIEVAL_ARTIFACT_SHA256"],
        "hcx_timeout_seconds": _float_environment(values, "HCX_TIMEOUT_SECONDS", default="45"),
        "official_answer_timeout_seconds": _float_environment(
            values, "OFFICIAL_ANSWER_TIMEOUT_SECONDS", default="270"
        ),
        "official_answer_max_inflight": _integer_environment(
            values, "OFFICIAL_ANSWER_MAX_INFLIGHT", default="2"
        ),
        "worker_count": _integer_environment(values, "WEB_CONCURRENCY", default="1"),
        "audit_queue_capacity": _integer_environment(
            values, "FINANCE_AUDIT_QUEUE_CAPACITY", default="2048"
        ),
        "audit_shutdown_timeout_seconds": _float_environment(
            values,
            "FINANCE_AUDIT_SHUTDOWN_TIMEOUT_SECONDS",
            default="5",
        ),
        "audit_fsync_each_event": _boolean_environment(
            values,
            "FINANCE_AUDIT_FSYNC_EACH_EVENT",
            default="true",
        ),
    }


def _verify_signed_runtime_profile(
    manifest: dict[str, Any],
    binding: dict[str, Any],
    values: dict[str, str],
) -> None:
    if _signed_manifest_profile(manifest, binding) != _release_environment_profile(values):
        raise ReleaseTrustError(
            "release environment does not match the signed AgentReleaseManifest"
        )


def verify_release_trust(env_file: Path) -> None:
    values = _load_environment(env_file)
    required = {
        "APP_ENV",
        "FINANCE_IMAGE_REFERENCE",
        "FINANCE_SOURCE_COMMIT",
        "FINANCE_RUNTIME_PLATFORM",
        "FINANCE_RELEASE_MANIFEST_HOST_FILE",
        "FINANCE_RELEASE_MANIFEST_SIGSTORE_BUNDLE_HOST_FILE",
        "FINANCE_DEPLOYMENT_BINDING_HOST_FILE",
        "FINANCE_DEPLOYMENT_BINDING_SHA256",
        "FINANCE_DEPLOYMENT_BINDING_SIGSTORE_BUNDLE_HOST_FILE",
        "FINANCE_RELATION_RETRIEVAL_ARTIFACT_SHA256",
        "FINANCE_BACKEND_ANSWER_PROVIDER",
        "FINANCE_BACKEND_HCX_QUERY_PLAN_ENABLED",
        "FINANCE_BACKEND_FUND_EXECUTION_POLICY",
        "HCX_TIMEOUT_SECONDS",
        "WEB_CONCURRENCY",
    }
    missing = sorted(name for name in required if not values.get(name))
    if missing:
        raise ReleaseTrustError("missing release trust settings: " + ", ".join(missing))
    image_reference = values["FINANCE_IMAGE_REFERENCE"]
    if _IMAGE_REFERENCE.fullmatch(image_reference) is None:
        raise ReleaseTrustError("release image reference is invalid")
    expected_binding_sha256 = values["FINANCE_DEPLOYMENT_BINDING_SHA256"]
    if _SHA256.fullmatch(expected_binding_sha256) is None:
        raise ReleaseTrustError("DeploymentBinding SHA-256 is invalid")

    manifest_path = Path(values["FINANCE_RELEASE_MANIFEST_HOST_FILE"])
    manifest_bundle_path = Path(values["FINANCE_RELEASE_MANIFEST_SIGSTORE_BUNDLE_HOST_FILE"])
    binding_path = Path(values["FINANCE_DEPLOYMENT_BINDING_HOST_FILE"])
    binding_bundle_path = Path(values["FINANCE_DEPLOYMENT_BINDING_SIGSTORE_BUNDLE_HOST_FILE"])
    manifest_data = _read_secure_file(
        manifest_path,
        "AgentReleaseManifest",
        maximum_bytes=_MAX_CONTROL_FILE_BYTES,
    )
    manifest_bundle_data = _read_secure_file(
        manifest_bundle_path,
        "AgentReleaseManifest Sigstore bundle",
        maximum_bytes=_MAX_CONTROL_FILE_BYTES,
    )
    binding_data = _read_secure_file(
        binding_path,
        "DeploymentBinding",
        maximum_bytes=_MAX_CONTROL_FILE_BYTES,
    )
    binding_bundle_data = _read_secure_file(
        binding_bundle_path,
        "DeploymentBinding Sigstore bundle",
        maximum_bytes=_MAX_CONTROL_FILE_BYTES,
    )
    manifest = _strict_object(manifest_data, "AgentReleaseManifest")
    binding = _strict_object(binding_data, "DeploymentBinding")
    if hashlib.sha256(binding_data).hexdigest() != expected_binding_sha256:
        raise ReleaseTrustError("DeploymentBinding differs from its trusted SHA-256")
    if binding.get("image_reference") != image_reference:
        raise ReleaseTrustError("DeploymentBinding and image reference differ")
    if binding.get("release_manifest_sha256") != hashlib.sha256(manifest_data).hexdigest():
        raise ReleaseTrustError("DeploymentBinding and AgentReleaseManifest differ")

    with tempfile.TemporaryDirectory(prefix="finance-agent-trust-") as temporary:
        snapshot_root = Path(temporary)
        snapshot_root.chmod(0o700)
        manifest_file = _write_snapshot(snapshot_root, "agent-release-manifest.json", manifest_data)
        manifest_bundle = _write_snapshot(
            snapshot_root,
            "agent-release-manifest.sigstore.json",
            manifest_bundle_data,
        )
        binding_file = _write_snapshot(snapshot_root, "deployment-binding.json", binding_data)
        binding_bundle = _write_snapshot(
            snapshot_root,
            "deployment-binding.sigstore.json",
            binding_bundle_data,
        )

        binary = _cosign_binary()
        identity_arguments = [
            "--certificate-identity",
            WORKFLOW_IDENTITY,
            "--certificate-oidc-issuer",
            OIDC_ISSUER,
        ]
        _run_verification(
            binary,
            ["verify", "--new-bundle-format=false", *identity_arguments, image_reference],
        )
        _run_verification(
            binary,
            [
                "verify-blob",
                "--bundle",
                str(manifest_bundle),
                *identity_arguments,
                str(manifest_file),
            ],
        )
        _run_verification(
            binary,
            [
                "verify-blob",
                "--bundle",
                str(binding_bundle),
                *identity_arguments,
                str(binding_file),
            ],
        )
        _verify_signed_runtime_profile(manifest, binding, values)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Verify immutable release trust before activation")
    parser.add_argument("--env-file", type=Path, required=True)
    arguments = parser.parse_args(argv)
    try:
        verify_release_trust(arguments.env_file)
    except (OSError, ReleaseTrustError, subprocess.TimeoutExpired) as error:
        print(f"release trust verification failed closed: {error}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
