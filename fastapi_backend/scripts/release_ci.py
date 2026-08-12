from __future__ import annotations

import argparse
import base64
import binascii
import hashlib
import json
import os
import re
import stat
from pathlib import Path
from typing import Any

_RELEASE_ID = re.compile(r"^[a-z0-9][a-z0-9._-]{7,99}$")
_NCP_REGISTRY = re.compile(r"^[a-z0-9](?:[a-z0-9.-]{1,250}[a-z0-9])?\.ncr\.ntruss\.com$")
_REPOSITORY = re.compile(
    r"^[a-z0-9](?:[a-z0-9._-]{0,62}[a-z0-9])?"
    r"(?:/[a-z0-9](?:[a-z0-9._-]{0,62}[a-z0-9])?)*$"
)
_SOURCE_COMMIT = re.compile(r"^[0-9a-f]{40}$")
_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
_IMAGE_REFERENCE = re.compile(r"^[a-z0-9][a-z0-9._:/-]{2,255}@sha256:[0-9a-f]{64}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_PLATFORMS = {"linux/amd64": ("linux", "amd64"), "linux/arm64": ("linux", "arm64")}


class ReleaseCIError(ValueError):
    pass


def _require_match(pattern: re.Pattern[str], value: str, label: str) -> str:
    if pattern.fullmatch(value) is None:
        raise ReleaseCIError(f"{label} is invalid")
    return value


def _write_github_outputs(path: Path, values: dict[str, str]) -> None:
    if not path.is_absolute():
        raise ReleaseCIError("GitHub output path must be absolute")
    with path.open("a", encoding="utf-8") as stream:
        for name, value in values.items():
            if re.fullmatch(r"[a-z][a-z0-9_]{0,63}", name) is None:
                raise ReleaseCIError("GitHub output name is invalid")
            if not value or "\n" in value or "\r" in value:
                raise ReleaseCIError(f"GitHub output {name} is unsafe")
            stream.write(f"{name}={value}\n")


def validate_inputs(arguments: argparse.Namespace) -> None:
    release_id = _require_match(_RELEASE_ID, arguments.release_id, "release ID")
    registry = _require_match(_NCP_REGISTRY, arguments.registry, "NCP registry")
    repository = _require_match(_REPOSITORY, arguments.repository, "repository")
    source_commit = _require_match(_SOURCE_COMMIT, arguments.source_commit, "source commit")
    if arguments.github_ref != "refs/heads/main":
        raise ReleaseCIError("immutable releases are allowed only from refs/heads/main")
    if arguments.github_ref_protected.lower() != "true":
        raise ReleaseCIError("the main branch must be protected before a release")
    if arguments.environment not in {"evaluation", "production"}:
        raise ReleaseCIError("environment is invalid")
    if arguments.platform not in _PLATFORMS:
        raise ReleaseCIError("platform is invalid")
    if arguments.answer_provider not in {"deterministic", "hyperclova"}:
        raise ReleaseCIError("answer provider is invalid")
    if arguments.hcx_queryplan_enabled not in {"true", "false"}:
        raise ReleaseCIError("HCLX QueryPlan flag is invalid")
    if arguments.answer_provider == "deterministic" and arguments.hcx_queryplan_enabled == "false":
        model_id = "disabled"
    else:
        model_id = "HCX-007"
    try:
        generation = int(arguments.activation_generation)
    except ValueError as error:
        raise ReleaseCIError("activation generation is invalid") from error
    if str(generation) != arguments.activation_generation or not 1 <= generation <= 2_147_483_647:
        raise ReleaseCIError("activation generation is invalid")
    previous_binding_sha256 = arguments.previous_binding_sha256
    if generation == 1:
        if previous_binding_sha256:
            raise ReleaseCIError("initial bootstrap is forbidden after a trusted release exists")
    else:
        _require_match(
            _SHA256,
            previous_binding_sha256,
            "previous Binding SHA-256",
        )
    python_base_image = _require_match(
        _IMAGE_REFERENCE,
        arguments.python_base_image,
        "Python base image",
    )
    image_repository = f"{registry}/{repository}"
    if len(image_repository) > 190:
        raise ReleaseCIError("NCP image repository is too long")
    _write_github_outputs(
        arguments.github_output,
        {
            "release_id": release_id,
            "environment": arguments.environment,
            "registry": registry,
            "repository": repository,
            "image_repository": image_repository,
            "source_commit": source_commit,
            "platform": arguments.platform,
            "answer_provider": arguments.answer_provider,
            "hcx_queryplan_enabled": arguments.hcx_queryplan_enabled,
            "model_id": model_id,
            "activation_generation": str(generation),
            "rollback_mode": "initial_bootstrap" if generation == 1 else "pinned_previous_release",
            "python_base_image": python_base_image,
            "base_tag": f"{image_repository}:base-{source_commit}",
            "release_tag": f"{image_repository}:{release_id}",
        },
    )


def bind_digest(arguments: argparse.Namespace) -> None:
    digest = _require_match(_DIGEST, arguments.digest, "registry digest")
    image_repository = arguments.image_repository
    if (
        "/" not in image_repository
        or re.fullmatch(r"[a-z0-9][a-z0-9._:/-]{2,190}", image_repository) is None
    ):
        raise ReleaseCIError("image repository is invalid")
    image_reference = f"{image_repository}@{digest}"
    _require_match(_IMAGE_REFERENCE, image_reference, "image reference")
    _write_github_outputs(
        arguments.github_output,
        {"digest": digest, "image_reference": image_reference},
    )


def _strict_json(data: bytes, label: str) -> Any:
    def reject_constant(value: str) -> None:
        raise ReleaseCIError(f"{label} contains a non-finite number: {value}")

    def reject_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ReleaseCIError(f"{label} contains a duplicate JSON key")
            result[key] = value
        return result

    try:
        return json.loads(
            data,
            object_pairs_hook=reject_duplicates,
            parse_constant=reject_constant,
        )
    except (UnicodeError, json.JSONDecodeError) as error:
        raise ReleaseCIError(f"{label} is not strict JSON") from error


def inspect_image(arguments: argparse.Namespace) -> None:
    expected_reference = _require_match(
        _IMAGE_REFERENCE,
        arguments.image_reference,
        "image reference",
    )
    _require_match(_SOURCE_COMMIT, arguments.source_commit, "source commit")
    _require_match(_RELEASE_ID, arguments.release_id, "release ID")
    _require_match(_IMAGE_REFERENCE, arguments.backend_base_image, "backend base image")
    _require_match(_IMAGE_REFERENCE, arguments.python_base_image, "Python base image")
    expected_os, expected_architecture = _PLATFORMS.get(arguments.platform, (None, None))
    if expected_os is None:
        raise ReleaseCIError("platform is invalid")
    payload = _strict_json(arguments.inspect_file.read_bytes(), "docker image inspect")
    if not isinstance(payload, list) or len(payload) != 1 or not isinstance(payload[0], dict):
        raise ReleaseCIError("docker image inspect must contain exactly one image")
    image = payload[0]
    if image.get("Os") != expected_os or image.get("Architecture") != expected_architecture:
        raise ReleaseCIError("registry image platform differs from the release platform")
    repo_digests = image.get("RepoDigests")
    if not isinstance(repo_digests, list) or expected_reference not in repo_digests:
        raise ReleaseCIError("docker inspect did not resolve the exact registry digest")
    config = image.get("Config")
    labels = config.get("Labels") if isinstance(config, dict) else None
    if not isinstance(labels, dict):
        raise ReleaseCIError("release image has no OCI labels")
    expected_labels = {
        "org.opencontainers.image.revision": arguments.source_commit,
        "ai.gaeng3.finance.release.id": arguments.release_id,
        "org.opencontainers.image.base.name": arguments.backend_base_image,
        "ai.gaeng3.finance.python-base.name": arguments.python_base_image,
    }
    for name, expected in expected_labels.items():
        if labels.get(name) != expected:
            raise ReleaseCIError(f"release image label differs: {name}")


def inspect_remote_manifest(arguments: argparse.Namespace) -> None:
    expected_reference = _require_match(
        _IMAGE_REFERENCE,
        arguments.image_reference,
        "image reference",
    )
    if arguments.platform != "linux/amd64":
        raise ReleaseCIError("official NCP release platform must be linux/amd64")
    payload = _strict_json(arguments.manifest_file.read_bytes(), "remote OCI manifest")
    if not isinstance(payload, dict):
        raise ReleaseCIError("remote OCI manifest must be a JSON object")
    if payload.get("schemaVersion") != 2:
        raise ReleaseCIError("remote OCI manifest schema version is invalid")
    if payload.get("mediaType") not in {
        "application/vnd.oci.image.index.v1+json",
        "application/vnd.docker.distribution.manifest.list.v2+json",
    }:
        raise ReleaseCIError("remote release digest must resolve to an OCI image index")
    descriptors = payload.get("manifests")
    if not isinstance(descriptors, list) or not descriptors:
        raise ReleaseCIError("remote OCI image index has no descriptors")
    runnable: list[tuple[str, str]] = []
    for descriptor in descriptors:
        if not isinstance(descriptor, dict):
            raise ReleaseCIError("remote OCI image descriptor is invalid")
        digest = descriptor.get("digest")
        if not isinstance(digest, str) or _DIGEST.fullmatch(digest) is None:
            raise ReleaseCIError("remote OCI image descriptor digest is invalid")
        platform = descriptor.get("platform")
        if not isinstance(platform, dict):
            raise ReleaseCIError("remote OCI image descriptor has no platform")
        os_name = platform.get("os")
        architecture = platform.get("architecture")
        annotations = descriptor.get("annotations")
        reference_type = (
            annotations.get("vnd.docker.reference.type") if isinstance(annotations, dict) else None
        )
        if reference_type == "attestation-manifest" or (os_name, architecture) == (
            "unknown",
            "unknown",
        ):
            continue
        if not isinstance(os_name, str) or not isinstance(architecture, str):
            raise ReleaseCIError("remote OCI image descriptor platform is invalid")
        runnable.append((os_name, architecture))
    if runnable != [("linux", "amd64")]:
        raise ReleaseCIError(
            f"{expected_reference} must expose exactly one linux/amd64 runnable manifest"
        )


def materialize_previous_binding(arguments: argparse.Namespace) -> None:
    encoded = os.environ.get("PREVIOUS_DEPLOYMENT_BINDING_B64", "")
    expected_sha256 = os.environ.get("PREVIOUS_DEPLOYMENT_BINDING_SHA256", "")
    _require_match(_SHA256, expected_sha256, "previous Binding SHA-256")
    try:
        data = base64.b64decode(encoded, validate=True)
    except (ValueError, binascii.Error) as error:
        raise ReleaseCIError("previous DeploymentBinding is not strict base64") from error
    if not data or len(data) > 2 * 1024 * 1024:
        raise ReleaseCIError("previous DeploymentBinding size is invalid")
    if hashlib.sha256(data).hexdigest() != expected_sha256:
        raise ReleaseCIError("previous DeploymentBinding differs from its trusted SHA-256")
    if not isinstance(_strict_json(data, "previous DeploymentBinding"), dict):
        raise ReleaseCIError("previous DeploymentBinding must be a JSON object")
    path = arguments.output
    if not path.is_absolute() or not path.parent.is_dir():
        raise ReleaseCIError("previous DeploymentBinding output path is invalid")
    descriptor = os.open(
        path,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
        0o444,
    )
    try:
        view = memoryview(data)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                raise ReleaseCIError("cannot write previous DeploymentBinding")
            view = view[written:]
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    if os.stat(path).st_mode & (stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH):
        raise ReleaseCIError("previous DeploymentBinding must be read-only")


def write_metadata(arguments: argparse.Namespace) -> None:
    fields = {
        "schema_version": "1.0",
        "workflow_identity": arguments.workflow_identity,
        "oidc_issuer": "https://token.actions.githubusercontent.com",
        "source_commit": _require_match(_SOURCE_COMMIT, arguments.source_commit, "source commit"),
        "release_id": _require_match(_RELEASE_ID, arguments.release_id, "release ID"),
        "environment": arguments.environment,
        "platform": arguments.platform,
        "base_image_reference": _require_match(
            _IMAGE_REFERENCE, arguments.base_image_reference, "base image reference"
        ),
        "release_image_reference": _require_match(
            _IMAGE_REFERENCE, arguments.release_image_reference, "release image reference"
        ),
        "agent_release_manifest_sha256": _require_match(
            _SHA256, arguments.manifest_sha256, "manifest SHA-256"
        ),
        "deployment_binding_sha256": _require_match(
            _SHA256, arguments.binding_sha256, "Binding SHA-256"
        ),
        "github_run_id": arguments.github_run_id,
        "github_run_attempt": arguments.github_run_attempt,
    }
    if fields["environment"] not in {"evaluation", "production"}:
        raise ReleaseCIError("environment is invalid")
    if fields["platform"] not in _PLATFORMS:
        raise ReleaseCIError("platform is invalid")
    if (
        re.fullmatch(
            r"https://github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+/\.github/workflows/[A-Za-z0-9_.-]+\.yml@refs/heads/main",
            arguments.workflow_identity,
        )
        is None
    ):
        raise ReleaseCIError("workflow identity is invalid")
    if not arguments.github_run_id.isdigit() or not arguments.github_run_attempt.isdigit():
        raise ReleaseCIError("GitHub run identity is invalid")
    if arguments.output.exists():
        raise ReleaseCIError("metadata output already exists")
    arguments.output.write_text(
        json.dumps(fields, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Fail-closed helpers for immutable release CI.")
    commands = parser.add_subparsers(dest="command", required=True)

    validate = commands.add_parser("validate-inputs")
    validate.add_argument("--release-id", required=True)
    validate.add_argument("--environment", required=True)
    validate.add_argument("--registry", required=True)
    validate.add_argument("--repository", required=True)
    validate.add_argument("--platform", required=True)
    validate.add_argument("--activation-generation", required=True)
    validate.add_argument("--previous-binding-sha256", required=True)
    validate.add_argument("--answer-provider", required=True)
    validate.add_argument("--hcx-queryplan-enabled", required=True)
    validate.add_argument("--python-base-image", required=True)
    validate.add_argument("--source-commit", required=True)
    validate.add_argument("--github-ref", required=True)
    validate.add_argument("--github-ref-protected", required=True)
    validate.add_argument("--github-output", type=Path, required=True)
    validate.set_defaults(handler=validate_inputs)

    digest = commands.add_parser("bind-digest")
    digest.add_argument("--digest", required=True)
    digest.add_argument("--image-repository", required=True)
    digest.add_argument("--github-output", type=Path, required=True)
    digest.set_defaults(handler=bind_digest)

    inspect = commands.add_parser("inspect-image")
    inspect.add_argument("--inspect-file", type=Path, required=True)
    inspect.add_argument("--image-reference", required=True)
    inspect.add_argument("--platform", required=True)
    inspect.add_argument("--source-commit", required=True)
    inspect.add_argument("--release-id", required=True)
    inspect.add_argument("--backend-base-image", required=True)
    inspect.add_argument("--python-base-image", required=True)
    inspect.set_defaults(handler=inspect_image)

    remote_manifest = commands.add_parser("inspect-remote-manifest")
    remote_manifest.add_argument("--manifest-file", type=Path, required=True)
    remote_manifest.add_argument("--image-reference", required=True)
    remote_manifest.add_argument("--platform", required=True)
    remote_manifest.set_defaults(handler=inspect_remote_manifest)

    previous = commands.add_parser("materialize-previous-binding")
    previous.add_argument("--output", type=Path, required=True)
    previous.set_defaults(handler=materialize_previous_binding)

    metadata = commands.add_parser("write-metadata")
    metadata.add_argument("--output", type=Path, required=True)
    metadata.add_argument("--workflow-identity", required=True)
    metadata.add_argument("--source-commit", required=True)
    metadata.add_argument("--release-id", required=True)
    metadata.add_argument("--environment", required=True)
    metadata.add_argument("--platform", required=True)
    metadata.add_argument("--base-image-reference", required=True)
    metadata.add_argument("--release-image-reference", required=True)
    metadata.add_argument("--manifest-sha256", required=True)
    metadata.add_argument("--binding-sha256", required=True)
    metadata.add_argument("--github-run-id", required=True)
    metadata.add_argument("--github-run-attempt", required=True)
    metadata.set_defaults(handler=write_metadata)
    return parser


def main() -> None:
    arguments = _parser().parse_args()
    try:
        arguments.handler(arguments)
    except (OSError, ReleaseCIError) as error:
        raise SystemExit(str(error)) from error


if __name__ == "__main__":
    main()
