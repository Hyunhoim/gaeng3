from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
import subprocess
import sys
import tempfile
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

_IMAGE_REFERENCE = re.compile(r"[a-z0-9][a-z0-9._:/-]{2,255}@sha256:[0-9a-f]{64}")
_RELEASE_ID = re.compile(r"[a-z0-9][a-z0-9._-]{7,127}")
_SHA256 = re.compile(r"[0-9a-f]{64}")
_SOURCE_COMMIT = re.compile(r"[0-9a-f]{40}")
_PROJECT_NAME = re.compile(r"finance-agent-rollback-drill-[a-z0-9][a-z0-9-]{2,31}")
_PROTECTED_PROJECT = "hyunholim-finance-agent"
_BINDING_MOUNT = "/run/finance-release/deployment-binding.json"
_ANSWER_PROBE_MARKER = "FINANCE_ROLLBACK_PROBE_RESULT="
_ANSWER_PROBE = """
import json
import urllib.request

payload = json.dumps({
    "question": "매수 가능한 국내채권을 매수수익률 높은 순으로 1개 보여줘.",
    "request_id": "rollback-drill-probe",
}).encode()
request = urllib.request.Request(
    "http://127.0.0.1:8000/answer",
    data=payload,
    headers={"content-type": "application/json"},
    method="POST",
)
with urllib.request.urlopen(request, timeout=20) as response:
    body = json.load(response)
if body.get("status") != "success" or body.get("intent") != "search":
    raise SystemExit("representative answer contract failed")
print("FINANCE_ROLLBACK_PROBE_RESULT=" + json.dumps({
    "status": body["status"],
    "intent": body["intent"],
}, separators=(",", ":")))
""".strip()
_ALLOWED_RELEASE_KEYS = {
    "APP_ENV",
    "FINANCE_IMAGE_REFERENCE",
    "FINANCE_SOURCE_COMMIT",
    "FINANCE_RUNTIME_PLATFORM",
    "FINANCE_DEPLOYMENT_BINDING_HOST_FILE",
    "FINANCE_DEPLOYMENT_BINDING_SHA256",
    "FINANCE_DATA_VOLUME_NAME",
    "FINANCE_RELEASE_MANIFEST_HOST_FILE",
    "FINANCE_RELEASE_MANIFEST_SIGSTORE_BUNDLE_HOST_FILE",
    "FINANCE_DEPLOYMENT_BINDING_SIGSTORE_BUNDLE_HOST_FILE",
    "BACKEND_BIND_ADDRESS",
    "BACKEND_PORT",
    "LOG_LEVEL",
    "WEB_CONCURRENCY",
    "OFFICIAL_ANSWER_TIMEOUT_SECONDS",
    "OFFICIAL_ANSWER_MAX_INFLIGHT",
    "FINANCE_BACKEND_FUND_EXECUTION_POLICY",
    "FINANCE_BACKEND_ANSWER_PROVIDER",
    "FINANCE_BACKEND_HCX_QUERY_PLAN_ENABLED",
    "FINANCE_AGENT_LLM_MODE",
    "LLM_PROVIDER",
    "HCX_MODEL",
    "HCX_TIMEOUT_SECONDS",
    "CLOVASTUDIO_API_KEY_HOST_FILE",
    "CLOVASTUDIO_API_KEY_FILE",
}


class RollbackDrillError(RuntimeError):
    """A fail-closed rollback drill precondition or verification failure."""


@dataclass(frozen=True, slots=True)
class ReleaseTarget:
    env_file: Path
    environment: dict[str, str]
    binding_file: Path
    binding_sha256: str
    binding: dict[str, Any]

    @property
    def release_id(self) -> str:
        return str(self.binding["release_id"])

    @property
    def image_reference(self) -> str:
        return str(self.binding["image_reference"])

    @property
    def data_volume(self) -> str:
        return self.environment["FINANCE_DATA_VOLUME_NAME"]

    @property
    def activation_generation(self) -> int:
        return int(self.binding["activation_generation"])


def _strict_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise RollbackDrillError(f"duplicate JSON key in DeploymentBinding: {key}")
        result[key] = value
    return result


def _require_regular_file(path: Path, *, read_only: bool) -> Path:
    resolved = path.expanduser()
    if not resolved.is_absolute():
        raise RollbackDrillError(f"release artifact path must be absolute: {path}")
    if resolved.is_symlink() or not resolved.is_file():
        raise RollbackDrillError(f"release artifact must be a regular non-symlink file: {path}")
    metadata = resolved.stat()
    if metadata.st_nlink != 1:
        raise RollbackDrillError(f"release artifact must not be hard-linked: {path}")
    if read_only and metadata.st_mode & (stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH):
        raise RollbackDrillError(f"DeploymentBinding must be read-only: {path}")
    return resolved.resolve(strict=True)


def _load_environment(path: Path) -> tuple[Path, dict[str, str]]:
    env_file = _require_regular_file(path, read_only=False)
    environment: dict[str, str] = {}
    for line_number, raw_line in enumerate(env_file.read_text(encoding="utf-8").splitlines(), 1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            raise RollbackDrillError(
                f"invalid release environment line {line_number} in {env_file}"
            )
        key, value = line.split("=", 1)
        key = key.strip()
        if not re.fullmatch(r"[A-Z][A-Z0-9_]*", key):
            raise RollbackDrillError(f"invalid environment key at line {line_number}")
        if key in environment:
            raise RollbackDrillError(f"duplicate environment key: {key}")
        environment[key] = value.strip()
    if "CLOVASTUDIO_API_KEY" in environment:
        raise RollbackDrillError(
            "rollback drill forbids inline CLOVASTUDIO_API_KEY; use a secret file"
        )
    extra = sorted(set(environment) - _ALLOWED_RELEASE_KEYS)
    if extra:
        raise RollbackDrillError(
            "rollback drill environment contains unsupported settings: " + ", ".join(extra)
        )
    return env_file, environment


def _require_pattern(value: str | None, pattern: re.Pattern[str], name: str) -> str:
    if value is None or pattern.fullmatch(value) is None:
        raise RollbackDrillError(f"invalid or missing {name}")
    return value


def _validate_provider_profile(environment: dict[str, str]) -> None:
    answer_provider = environment.get("FINANCE_BACKEND_ANSWER_PROVIDER", "deterministic")
    hcx_query_plan = environment.get("FINANCE_BACKEND_HCX_QUERY_PLAN_ENABLED", "false").lower()
    if answer_provider not in {"deterministic", "hyperclova"}:
        raise RollbackDrillError("release answer provider is invalid")
    if hcx_query_plan not in {"true", "false"}:
        raise RollbackDrillError("release HCLX QueryPlan flag is invalid")
    uses_hcx = answer_provider == "hyperclova" or hcx_query_plan == "true"
    if uses_hcx:
        if (
            environment.get("FINANCE_AGENT_LLM_MODE") != environment["APP_ENV"]
            or environment.get("LLM_PROVIDER") != "hyperclova"
            or environment.get("HCX_MODEL") != "HCX-007"
            or environment.get("CLOVASTUDIO_API_KEY_FILE") != "/run/secrets/clovastudio_api_key"
        ):
            raise RollbackDrillError("HyperCLOVA release provider profile is incomplete")
        secret_text = environment.get("CLOVASTUDIO_API_KEY_HOST_FILE", "")
        if not secret_text:
            raise RollbackDrillError("HyperCLOVA release secret file is missing")
        secret = Path(secret_text)
        try:
            metadata = secret.stat(follow_symlinks=False)
        except OSError as error:
            raise RollbackDrillError("HyperCLOVA release secret is unavailable") from error
        if (
            not secret.is_absolute()
            or not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != 10001
            or metadata.st_nlink != 1
            or metadata.st_mode & 0o077
        ):
            raise RollbackDrillError("HyperCLOVA release secret file is insecure")
        return

    if environment.get("FINANCE_AGENT_LLM_MODE", "disabled") != "disabled":
        raise RollbackDrillError("deterministic release must disable LLM mode")
    if environment.get("LLM_PROVIDER", "disabled") != "disabled":
        raise RollbackDrillError("deterministic release must disable LLM provider")
    if any(
        environment.get(name)
        for name in (
            "CLOVASTUDIO_API_KEY_HOST_FILE",
            "CLOVASTUDIO_API_KEY_FILE",
            "HCX_MODEL",
        )
    ):
        raise RollbackDrillError("deterministic release must not configure HCLX credentials")


def _load_target(path: Path) -> ReleaseTarget:
    env_file, environment = _load_environment(path)
    required = {
        "APP_ENV",
        "FINANCE_IMAGE_REFERENCE",
        "FINANCE_SOURCE_COMMIT",
        "FINANCE_RUNTIME_PLATFORM",
        "FINANCE_DEPLOYMENT_BINDING_HOST_FILE",
        "FINANCE_DEPLOYMENT_BINDING_SHA256",
        "FINANCE_DATA_VOLUME_NAME",
    }
    missing = sorted(name for name in required if not environment.get(name))
    if missing:
        raise RollbackDrillError("missing release settings: " + ", ".join(missing))
    if environment["APP_ENV"] not in {"evaluation", "production"}:
        raise RollbackDrillError("APP_ENV must be evaluation or production")
    if environment["FINANCE_RUNTIME_PLATFORM"] != "linux/amd64":
        raise RollbackDrillError("official rollback platform must be linux/amd64")
    _validate_provider_profile(environment)
    _require_pattern(
        environment["FINANCE_IMAGE_REFERENCE"],
        _IMAGE_REFERENCE,
        "FINANCE_IMAGE_REFERENCE",
    )
    _require_pattern(
        environment["FINANCE_SOURCE_COMMIT"],
        _SOURCE_COMMIT,
        "FINANCE_SOURCE_COMMIT",
    )
    binding_sha256 = _require_pattern(
        environment["FINANCE_DEPLOYMENT_BINDING_SHA256"],
        _SHA256,
        "FINANCE_DEPLOYMENT_BINDING_SHA256",
    )
    binding_file = _require_regular_file(
        Path(environment["FINANCE_DEPLOYMENT_BINDING_HOST_FILE"]),
        read_only=True,
    )
    binding_data = binding_file.read_bytes()
    if hashlib.sha256(binding_data).hexdigest() != binding_sha256:
        raise RollbackDrillError("DeploymentBinding differs from its trusted SHA-256")
    try:
        binding = json.loads(binding_data, object_pairs_hook=_strict_object)
    except (UnicodeError, json.JSONDecodeError) as error:
        raise RollbackDrillError("DeploymentBinding is not strict JSON") from error
    if not isinstance(binding, dict):
        raise RollbackDrillError("DeploymentBinding root must be an object")
    canonical = (json.dumps(binding, sort_keys=True, separators=(",", ":")) + "\n").encode()
    if canonical != binding_data:
        raise RollbackDrillError("DeploymentBinding must use canonical JSON encoding")

    release_id = _require_pattern(str(binding.get("release_id", "")), _RELEASE_ID, "release_id")
    manifest_sha256 = _require_pattern(
        str(binding.get("release_manifest_sha256", "")),
        _SHA256,
        "release_manifest_sha256",
    )
    image_reference = _require_pattern(
        str(binding.get("image_reference", "")),
        _IMAGE_REFERENCE,
        "binding image_reference",
    )
    source_commit = _require_pattern(
        str(binding.get("source_commit", "")),
        _SOURCE_COMMIT,
        "binding source_commit",
    )
    generation = binding.get("activation_generation")
    if type(generation) is not int or generation < 1:
        raise RollbackDrillError("activation_generation must be a positive integer")
    if not isinstance(binding.get("rollback"), dict):
        raise RollbackDrillError("DeploymentBinding rollback must be an object")
    if binding.get("environment") != environment["APP_ENV"]:
        raise RollbackDrillError("DeploymentBinding and APP_ENV differ")
    if binding.get("platform") != environment["FINANCE_RUNTIME_PLATFORM"]:
        raise RollbackDrillError("DeploymentBinding and runtime platform differ")
    if image_reference != environment["FINANCE_IMAGE_REFERENCE"]:
        raise RollbackDrillError("DeploymentBinding and image reference differ")
    if source_commit != environment["FINANCE_SOURCE_COMMIT"]:
        raise RollbackDrillError("DeploymentBinding and source commit differ")
    expected_volume = f"finance-data-{release_id}-{manifest_sha256[:12]}"
    if environment["FINANCE_DATA_VOLUME_NAME"] != expected_volume:
        raise RollbackDrillError(f"release data volume must be named {expected_volume}")
    return ReleaseTarget(
        env_file=env_file,
        environment=environment,
        binding_file=binding_file,
        binding_sha256=binding_sha256,
        binding=binding,
    )


def _verify_chain(previous: ReleaseTarget, current: ReleaseTarget) -> None:
    if previous.release_id == current.release_id:
        raise RollbackDrillError("rollback releases must have distinct release IDs")
    if previous.image_reference == current.image_reference:
        raise RollbackDrillError("rollback releases must have distinct image digests")
    if previous.data_volume == current.data_volume:
        raise RollbackDrillError("rollback releases must have distinct data volumes")
    if previous.binding_sha256 == current.binding_sha256:
        raise RollbackDrillError("rollback releases must have distinct Binding files")
    if previous.binding["environment"] != current.binding["environment"]:
        raise RollbackDrillError("rollback releases must use the same environment")
    if previous.binding["platform"] != current.binding["platform"]:
        raise RollbackDrillError("rollback releases must use the same platform")
    if current.activation_generation != previous.activation_generation + 1:
        raise RollbackDrillError("current generation must immediately follow the previous one")
    rollback = current.binding["rollback"]
    expected = {
        "mode": "pinned_previous_release",
        "target_release_id": previous.release_id,
        "target_manifest_sha256": previous.binding["release_manifest_sha256"],
        "target_binding_sha256": previous.binding_sha256,
        "target_image_reference": previous.image_reference,
        "target_activation_generation": previous.activation_generation,
        "target_environment": previous.binding["environment"],
        "target_platform": previous.binding["platform"],
    }
    if rollback != expected:
        raise RollbackDrillError("current Binding does not pin the exact previous release")


def _snapshot_target(target: ReleaseTarget, root: Path, name: str) -> ReleaseTarget:
    target_root = root / name
    # The Binding is mounted into a container running as UID 10001.  Every host
    # directory in the bind source must therefore be traversable, while listing
    # stays disabled and the environment file remains host-only (0600).
    target_root.mkdir(mode=0o711)
    target_root.chmod(0o711)
    binding_file = target_root / "deployment-binding.json"
    binding_data = (
        json.dumps(target.binding, sort_keys=True, separators=(",", ":")) + "\n"
    ).encode()
    descriptor = os.open(
        binding_file,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
        0o444,
    )
    try:
        # Shared research hosts commonly run with umask 0077.  The container's
        # fixed UID 10001 still needs read-only access to this bind-mounted file.
        os.fchmod(descriptor, 0o444)
        view = memoryview(binding_data)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                raise RollbackDrillError("cannot create rollback Binding snapshot")
            view = view[written:]
        os.fsync(descriptor)
    finally:
        os.close(descriptor)

    environment = dict(target.environment)
    environment["FINANCE_DEPLOYMENT_BINDING_HOST_FILE"] = str(binding_file)
    env_file = target_root / "release.env"
    descriptor = os.open(
        env_file,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    try:
        os.fchmod(descriptor, 0o600)
        for key in sorted(environment):
            value = environment[key]
            if any(character in value for character in "'$\r\n"):
                raise RollbackDrillError("rollback environment contains an unsafe value")
            payload = f"{key}={value}\n".encode()
            view = memoryview(payload)
            while view:
                written = os.write(descriptor, view)
                if written <= 0:
                    raise RollbackDrillError("cannot create rollback environment snapshot")
                view = view[written:]
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    return ReleaseTarget(
        env_file=env_file,
        environment=environment,
        binding_file=binding_file,
        binding_sha256=target.binding_sha256,
        binding=target.binding,
    )


class DockerClient:
    def __init__(self, *, root: Path, project: str, port: int) -> None:
        self.root = root
        self.project = project
        self.port = port
        self._all_release_keys: set[str] = set()

    def register(self, *targets: ReleaseTarget) -> None:
        for target in targets:
            self._all_release_keys.update(target.environment)

    def _environment(self) -> dict[str, str]:
        environment = os.environ.copy()
        for key in tuple(environment):
            if (
                key in self._all_release_keys
                or key.startswith(("FINANCE_", "COMPOSE_", "HCX_", "CLOVASTUDIO_"))
                or key
                in {
                    "APP_ENV",
                    "LLM_PROVIDER",
                    "BACKEND_BIND_ADDRESS",
                    "BACKEND_PORT",
                    "GITHUB_TOKEN",
                    "NCP_REGISTRY_PASSWORD",
                    "NCP_REGISTRY_USERNAME",
                }
            ):
                environment.pop(key, None)
        environment["BACKEND_BIND_ADDRESS"] = "127.0.0.1"
        environment["BACKEND_PORT"] = str(self.port)
        return environment

    def run(
        self,
        arguments: Sequence[str],
        *,
        allow_failure: bool = False,
    ) -> subprocess.CompletedProcess[str]:
        completed = subprocess.run(
            ["docker", *arguments],
            cwd=self.root,
            env=self._environment(),
            check=False,
            capture_output=True,
            text=True,
            timeout=240,
        )
        if completed.returncode != 0 and not allow_failure:
            operation = " ".join(arguments[:3])
            raise RollbackDrillError(
                f"Docker operation failed closed ({completed.returncode}): {operation}"
            )
        return completed

    def compose(
        self,
        target: ReleaseTarget,
        arguments: Sequence[str],
        *,
        allow_failure: bool = False,
    ) -> subprocess.CompletedProcess[str]:
        return self.run(
            [
                "compose",
                "-p",
                self.project,
                "--env-file",
                str(target.env_file),
                "-f",
                "docker-compose.yml",
                "-f",
                "fastapi_backend/docker-compose.release.yml",
                *arguments,
            ],
            allow_failure=allow_failure,
        )

    def reject_existing_project(self) -> None:
        containers = self.run(
            [
                "ps",
                "--all",
                "--quiet",
                "--filter",
                f"label=com.docker.compose.project={self.project}",
            ]
        ).stdout.strip()
        if containers:
            raise RollbackDrillError("isolated rollback drill project already has containers")
        network = self.run(
            ["network", "inspect", f"{self.project}_default"],
            allow_failure=True,
        )
        if network.returncode == 0:
            raise RollbackDrillError("isolated rollback drill project network already exists")

    def require_artifacts(self, target: ReleaseTarget) -> None:
        trust = subprocess.run(
            [
                sys.executable,
                str(self.root / "fastapi_backend/scripts/release_trust.py"),
                "--env-file",
                str(target.env_file),
            ],
            cwd=self.root,
            env=self._environment(),
            check=False,
            capture_output=True,
            text=True,
            timeout=180,
        )
        if trust.returncode != 0:
            raise RollbackDrillError("release trust verification failed before rollback")
        self.run(["image", "inspect", target.image_reference])
        self.run(["volume", "inspect", target.data_volume])
        self.compose(target, ["config", "--quiet"])

    def activate_and_verify(self, target: ReleaseTarget) -> None:
        self.compose(
            target,
            ["up", "--detach", "--wait", "--no-build", "--force-recreate"],
        )
        container_id = self.compose(target, ["ps", "--quiet", "backend"]).stdout.strip()
        if not container_id or "\n" in container_id:
            raise RollbackDrillError("rollback drill did not resolve exactly one backend container")
        health = self.run(
            ["inspect", "--format", "{{.State.Health.Status}}", container_id]
        ).stdout.strip()
        image = self.run(["inspect", "--format", "{{.Config.Image}}", container_id]).stdout.strip()
        volume = self.run(
            [
                "inspect",
                "--format",
                '{{range .Mounts}}{{if eq .Destination "/data"}}{{.Name}}{{end}}{{end}}',
                container_id,
            ]
        ).stdout.strip()
        binding = self.run(
            [
                "inspect",
                "--format",
                (
                    "{{range .Mounts}}{{if eq .Destination "
                    f'"{_BINDING_MOUNT}"}}}}{{{{.Source}}}}{{{{end}}}}{{{{end}}}}'
                ),
                container_id,
            ]
        ).stdout.strip()
        if health != "healthy":
            raise RollbackDrillError(f"release {target.release_id} did not become healthy")
        if image != target.image_reference:
            raise RollbackDrillError("active container image differs from DeploymentBinding")
        if volume != target.data_volume:
            raise RollbackDrillError("active container DB volume differs from the release volume")
        try:
            observed_binding = Path(binding).resolve(strict=True)
        except OSError as error:
            raise RollbackDrillError("active Binding mount source is not resolvable") from error
        if observed_binding != target.binding_file:
            raise RollbackDrillError("active container Binding mount differs from the release")
        probe = self.run(["exec", container_id, "python", "-c", _ANSWER_PROBE]).stdout
        result_lines = [
            line.removeprefix(_ANSWER_PROBE_MARKER)
            for line in probe.splitlines()
            if line.startswith(_ANSWER_PROBE_MARKER)
        ]
        if len(result_lines) != 1:
            raise RollbackDrillError(
                "representative /answer probe returned an ambiguous result marker"
            )
        try:
            probe_result = json.loads(result_lines[0], object_pairs_hook=_strict_object)
        except json.JSONDecodeError as error:
            raise RollbackDrillError(
                "representative /answer probe returned invalid JSON"
            ) from error
        if probe_result != {"status": "success", "intent": "search"}:
            raise RollbackDrillError("representative /answer probe failed")

    def stop_isolated_project(self, target: ReleaseTarget) -> None:
        down = self.compose(target, ["down"], allow_failure=True)
        containers = self.run(
            [
                "ps",
                "--all",
                "--quiet",
                "--filter",
                f"label=com.docker.compose.project={self.project}",
            ]
        ).stdout.strip()
        network = self.run(
            ["network", "inspect", f"{self.project}_default"],
            allow_failure=True,
        )
        if down.returncode != 0 or containers or network.returncode == 0:
            raise RollbackDrillError("isolated rollback drill cleanup was incomplete")


def _short_digest(image_reference: str) -> str:
    return image_reference.rsplit("sha256:", 1)[1][:12]


def _result(
    *,
    mode: str,
    project: str,
    port: int,
    previous: ReleaseTarget,
    current: ReleaseTarget,
    stopped: bool,
) -> dict[str, Any]:
    return {
        "schema_version": "1.0",
        "status": "verified" if mode == "execute" else "validated",
        "mode": mode,
        "isolated_project": project,
        "bind_address": "127.0.0.1",
        "port": port,
        "activation_sequence": [
            previous.release_id,
            current.release_id,
            previous.release_id,
        ],
        "previous": {
            "release_id": previous.release_id,
            "generation": previous.activation_generation,
            "image_digest_prefix": _short_digest(previous.image_reference),
            "data_volume": previous.data_volume,
        },
        "current": {
            "release_id": current.release_id,
            "generation": current.activation_generation,
            "image_digest_prefix": _short_digest(current.image_reference),
            "data_volume": current.data_volume,
        },
        "artifacts_preserved": mode == "execute",
        "containers_stopped_after_verification": stopped,
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Fail-closed N-1 -> N -> N-1 release rollback drill",
    )
    parser.add_argument("--previous-env", type=Path, required=True)
    parser.add_argument("--current-env", type=Path, required=True)
    parser.add_argument("--project-name", required=True)
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument(
        "--execute",
        action="store_true",
        help="Run the isolated Docker drill; the default validates and prints the plan only.",
    )
    parser.add_argument(
        "--leave-running",
        action="store_true",
        help="Leave the verified N-1 container running; valid only with --execute.",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    try:
        if _PROJECT_NAME.fullmatch(arguments.project_name) is None:
            raise RollbackDrillError(
                "project name must use finance-agent-rollback-drill-<unique-lowercase-suffix>"
            )
        if arguments.project_name == _PROTECTED_PROJECT:
            raise RollbackDrillError("the live finance project is protected from rollback drills")
        if not 1024 <= arguments.port <= 65535:
            raise RollbackDrillError("rollback drill port must be between 1024 and 65535")
        if arguments.leave_running:
            raise RollbackDrillError(
                "--leave-running is incompatible with immutable rollback snapshots"
            )
        previous = _load_target(arguments.previous_env)
        current = _load_target(arguments.current_env)
        _verify_chain(previous, current)
        if not arguments.execute:
            print(
                json.dumps(
                    _result(
                        mode="dry_run",
                        project=arguments.project_name,
                        port=arguments.port,
                        previous=previous,
                        current=current,
                        stopped=False,
                    ),
                    ensure_ascii=False,
                    sort_keys=True,
                )
            )
            return 0

        root = Path(__file__).resolve().parents[2]
        with tempfile.TemporaryDirectory(prefix="finance-agent-rollback-snapshot-") as temporary:
            snapshot_root = Path(temporary)
            # 0711 lets the non-root container traverse the bind source without
            # exposing a directory listing of immutable control files.
            snapshot_root.chmod(0o711)
            previous = _snapshot_target(previous, snapshot_root, "previous")
            current = _snapshot_target(current, snapshot_root, "current")
            docker = DockerClient(root=root, project=arguments.project_name, port=arguments.port)
            docker.register(previous, current)
            docker.reject_existing_project()
            docker.require_artifacts(previous)
            docker.require_artifacts(current)
            activated = False
            try:
                activated = True
                docker.activate_and_verify(previous)
                docker.activate_and_verify(current)
                docker.run(["image", "inspect", previous.image_reference])
                docker.run(["volume", "inspect", previous.data_volume])
                docker.activate_and_verify(previous)
                docker.run(["image", "inspect", current.image_reference])
                docker.run(["volume", "inspect", current.data_volume])
            finally:
                if activated:
                    docker.stop_isolated_project(previous)
        print(
            json.dumps(
                _result(
                    mode="execute",
                    project=arguments.project_name,
                    port=arguments.port,
                    previous=previous,
                    current=current,
                    stopped=True,
                ),
                ensure_ascii=False,
                sort_keys=True,
            )
        )
        return 0
    except (OSError, RollbackDrillError, subprocess.TimeoutExpired) as error:
        print(f"rollback drill failed closed: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
