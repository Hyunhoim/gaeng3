from __future__ import annotations

import argparse
import hashlib
import os
import subprocess
from collections.abc import Callable
from pathlib import Path

import finance_agent_core.release as release_contract
from finance_agent_core.release import (
    AgentReleaseError,
    AgentReleaseManifest,
    DeploymentBinding,
    RollbackRelease,
    RuntimeReleaseInputs,
    _read_release_file,
    _reject_symlink_path,
    _strict_json_object,
    build_agent_release_manifest,
    deployment_binding_file_bytes,
    load_relation_retrieval_artifact_release,
    manifest_file_bytes,
)


def _require_clean_source(root: Path, expected_commit: str) -> None:
    try:
        top_level = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "--show-toplevel"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        actual_commit = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        status = subprocess.run(
            ["git", "-C", str(root), "status", "--porcelain=v1", "--untracked-files=all"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout
    except (OSError, subprocess.CalledProcessError) as error:
        raise SystemExit("cannot verify the release source checkout") from error
    if Path(top_level).resolve() != root:
        raise SystemExit("git root must be the repository top-level directory")
    if actual_commit != expected_commit:
        raise SystemExit("source commit differs from the checked-out Git HEAD")
    if status.strip():
        raise SystemExit("release manifest generation requires a clean Git checkout")


def _require_release_source_roots(git_root: Path, backend_root: Path) -> None:
    expected_core = (
        git_root
        / "finance_agent"
        / "packages"
        / "finance_agent_core"
        / "src"
        / "finance_agent_core"
    ).resolve(strict=True)
    expected_backend = (git_root / "fastapi_backend" / "app").resolve(strict=True)
    imported_core = Path(release_contract.__file__).resolve(strict=True).parent
    if imported_core != expected_core or backend_root != expected_backend:
        raise SystemExit("release Core and Backend roots must come from the verified Git checkout")


def _write_immutable(path: Path, data: bytes) -> str:
    if not path.is_absolute():
        raise SystemExit("release output path must be absolute")
    if not path.parent.is_dir():
        raise SystemExit("release output parent must be an existing regular directory")
    try:
        _reject_symlink_path(path.parent)
    except AgentReleaseError as error:
        raise SystemExit("release output parent must not contain symbolic links") from error
    flags = (
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    try:
        descriptor = os.open(path, flags, 0o444)
    except OSError as error:
        raise SystemExit("release output already exists or cannot be created") from error
    try:
        view = memoryview(data)
        while view:
            written = os.write(descriptor, view)
            view = view[written:]
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    try:
        parent_descriptor = os.open(
            path.parent,
            os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_CLOEXEC", 0),
        )
        try:
            os.fsync(parent_descriptor)
        finally:
            os.close(parent_descriptor)
    except OSError as error:
        raise SystemExit("cannot durably record the release output") from error
    return hashlib.sha256(data).hexdigest()


def _runtime_inputs(arguments: argparse.Namespace) -> RuntimeReleaseInputs:
    model = arguments.hcx_model
    artifact_path = arguments.relation_retrieval_artifact
    artifact_file_sha256 = arguments.relation_retrieval_artifact_sha256
    if (artifact_path is None) != (artifact_file_sha256 is None):
        raise SystemExit(
            "relation retrieval activation requires both artifact path and trusted SHA-256"
        )
    relation_retrieval_artifact = None
    if artifact_path is not None:
        try:
            relation_retrieval_artifact = load_relation_retrieval_artifact_release(
                artifact_path=artifact_path,
                expected_file_sha256=artifact_file_sha256,
            )
        except AgentReleaseError as error:
            raise SystemExit(f"relation retrieval artifact is invalid: {error}") from error
    return RuntimeReleaseInputs(
        environment=arguments.environment,
        source_commit=arguments.source_commit,
        image_reference=(
            "unbound.invalid/finance-agent@sha256:" + "0" * 64
            if getattr(arguments, "image_reference", None) is None
            else arguments.image_reference
        ),
        backend_version=arguments.backend_version,
        backend_root=arguments.backend_root.resolve(strict=True),
        answer_provider=arguments.answer_provider,
        hcx_queryplan_enabled=arguments.hcx_queryplan_enabled,
        hcx_model=model,
        fund_execution_policy=arguments.fund_execution_policy,
        relation_retrieval_artifact=relation_retrieval_artifact,
        relation_retrieval_artifact_file_sha256=artifact_file_sha256,
        platform=arguments.platform,
        hcx_timeout_seconds=arguments.hcx_timeout_seconds,
        official_answer_timeout_seconds=arguments.official_answer_timeout_seconds,
        official_answer_max_inflight=arguments.official_answer_max_inflight,
        worker_count=arguments.worker_count,
        audit_queue_capacity=arguments.audit_queue_capacity,
        audit_shutdown_timeout_seconds=arguments.audit_shutdown_timeout_seconds,
        audit_fsync_each_event=arguments.audit_fsync_each_event,
    )


def _create_manifest(arguments: argparse.Namespace) -> None:
    git_root = arguments.git_root.resolve(strict=True)
    backend_root = arguments.backend_root.resolve(strict=True)
    _require_release_source_roots(git_root, backend_root)
    _require_clean_source(git_root, arguments.source_commit)
    manifest = build_agent_release_manifest(
        _runtime_inputs(arguments),
        release_id=arguments.release_id,
    )
    _require_clean_source(git_root, arguments.source_commit)
    digest = _write_immutable(arguments.output, manifest_file_bytes(manifest))
    print(f"agent_release_manifest_sha256={digest}")


def _create_binding(arguments: argparse.Namespace) -> None:
    _, manifest_data, manifest_sha256 = _read_release_file(arguments.manifest)
    try:
        manifest = AgentReleaseManifest.model_validate(
            _strict_json_object(manifest_data, "AgentReleaseManifest")
        )
    except ValueError as error:
        raise SystemExit("AgentReleaseManifest violates the strict schema") from error
    if manifest_data != manifest_file_bytes(manifest):
        raise SystemExit("AgentReleaseManifest is not in canonical file form")
    if arguments.rollback_mode == "initial_bootstrap":
        if arguments.rollback_binding is not None or arguments.rollback_binding_sha256 is not None:
            raise SystemExit("initial bootstrap cannot accept a rollback binding")
        rollback = RollbackRelease(mode="initial_bootstrap")
    else:
        if arguments.rollback_binding is None or arguments.rollback_binding_sha256 is None:
            raise SystemExit("pinned rollback requires a trusted previous DeploymentBinding")
        _, previous_data, previous_sha256 = _read_release_file(arguments.rollback_binding)
        if previous_sha256 != arguments.rollback_binding_sha256:
            raise SystemExit("previous DeploymentBinding differs from its trusted SHA-256")
        try:
            previous = DeploymentBinding.model_validate(
                _strict_json_object(previous_data, "previous DeploymentBinding")
            )
        except ValueError as error:
            raise SystemExit("previous DeploymentBinding violates the strict schema") from error
        if previous_data != deployment_binding_file_bytes(previous):
            raise SystemExit("previous DeploymentBinding is not in canonical file form")
        if previous.environment != manifest.environment or previous.platform != arguments.platform:
            raise SystemExit("rollback target environment or platform differs")
        if arguments.activation_generation != previous.activation_generation + 1:
            raise SystemExit("activation generation must immediately follow the rollback target")
        rollback = RollbackRelease(
            mode="pinned_previous_release",
            target_release_id=previous.release_id,
            target_manifest_sha256=previous.release_manifest_sha256,
            target_binding_sha256=previous_sha256,
            target_image_reference=previous.image_reference,
            target_activation_generation=previous.activation_generation,
            target_environment=previous.environment,
            target_platform=previous.platform,
        )
    binding = DeploymentBinding(
        release_id=manifest.release_id,
        environment=manifest.environment,
        source_commit=manifest.source_commit,
        release_manifest_sha256=manifest_sha256,
        image_reference=arguments.image_reference,
        platform=arguments.platform,
        activation_generation=arguments.activation_generation,
        rollback=rollback,
    )
    digest = _write_immutable(arguments.output, deployment_binding_file_bytes(binding))
    print(f"deployment_binding_sha256={digest}")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Build immutable Agent release artifacts without making network calls."
    )
    commands = parser.add_subparsers(dest="command", required=True)

    manifest = commands.add_parser("manifest")
    manifest.add_argument("--release-id", required=True)
    manifest.add_argument("--environment", choices=("evaluation", "production"), required=True)
    manifest.add_argument("--source-commit", required=True)
    manifest.add_argument("--git-root", type=Path, required=True)
    manifest.add_argument("--backend-root", type=Path, required=True)
    manifest.add_argument("--backend-version", default="0.1.0")
    manifest.add_argument(
        "--platform",
        choices=("linux/amd64", "linux/arm64"),
        default="linux/amd64",
    )
    manifest.add_argument(
        "--answer-provider",
        choices=("deterministic", "hyperclova"),
        default="deterministic",
    )
    manifest.add_argument("--hcx-queryplan-enabled", action="store_true")
    manifest.add_argument("--hcx-model")
    manifest.add_argument("--hcx-timeout-seconds", type=float, default=45.0)
    manifest.add_argument("--official-answer-timeout-seconds", type=float, default=270.0)
    manifest.add_argument("--official-answer-max-inflight", type=int, default=2)
    manifest.add_argument("--worker-count", type=int, default=1)
    manifest.add_argument("--audit-queue-capacity", type=int, default=2_048)
    manifest.add_argument("--audit-shutdown-timeout-seconds", type=float, default=5.0)
    manifest.add_argument(
        "--audit-fsync-each-event",
        action=argparse.BooleanOptionalAction,
        default=True,
    )
    manifest.add_argument(
        "--fund-execution-policy",
        choices=("locked", "public_fund_v1_approved"),
        default="locked",
    )
    manifest.add_argument(
        "--relation-retrieval-artifact",
        type=Path,
        help="absolute path to one canonical read-only relation artifact release",
    )
    manifest.add_argument(
        "--relation-retrieval-artifact-sha256",
        help="trusted SHA-256 of --relation-retrieval-artifact",
    )
    manifest.add_argument("--output", type=Path, required=True)
    manifest.set_defaults(handler=_create_manifest)

    binding = commands.add_parser("binding")
    binding.add_argument("--manifest", type=Path, required=True)
    binding.add_argument("--image-reference", required=True)
    binding.add_argument("--platform", choices=("linux/amd64", "linux/arm64"), required=True)
    binding.add_argument("--activation-generation", type=int, required=True)
    binding.add_argument(
        "--rollback-mode",
        choices=("initial_bootstrap", "pinned_previous_release"),
        required=True,
    )
    binding.add_argument("--rollback-binding", type=Path)
    binding.add_argument("--rollback-binding-sha256")
    binding.add_argument("--output", type=Path, required=True)
    binding.set_defaults(handler=_create_binding)
    return parser


def main() -> None:
    arguments = _parser().parse_args()
    handler: Callable[[argparse.Namespace], None] = arguments.handler
    handler(arguments)


if __name__ == "__main__":
    main()
