from __future__ import annotations

import argparse
import json
import os
import re
import stat
import subprocess
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

_PROJECT_NAME = re.compile(r"^finance-perf-[a-z0-9][a-z0-9_-]{0,47}$")
_CONTAINER_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
_IMAGE_REFERENCE = re.compile(r"^[a-z0-9][a-z0-9._:/-]{2,255}@sha256:[0-9a-f]{64}$")
_MEMORY_LIMIT = re.compile(r"^[1-9][0-9]*(?:[kKmMgG])$")
_SOURCE_COMMIT = re.compile(r"^[0-9a-f]{40}$")
_SHARED_PORTS = frozenset({18_001, 18_002})
_MAX_CONFIG_BYTES = 4 * 1024 * 1024
_BACKEND_CPU_LIMIT = 2.0
_BACKEND_MEMORY_LIMIT = "1g"
_BACKEND_MEMORY_BYTES = 1024**3
_BACKEND_PIDS_LIMIT = 256
_BACKEND_NOFILE_LIMIT = 4096
_DATA_INIT_CPU_LIMIT = 1.0
_DATA_INIT_MEMORY_BYTES = 512 * 1024**2
_DATA_INIT_PIDS_LIMIT = 128


@dataclass(frozen=True, slots=True)
class PerformanceRunConfig:
    repository: Path
    project: str
    container_name: str
    data_init_container_name: str
    port: int
    audit_directory: Path
    raw_data_directory: Path
    image_reference: str
    source_commit: str
    cpu_limit: float = 2.0
    memory_limit: str = "1g"
    pids_limit: int = 256

    def validate(self, *, require_fresh_audit: bool = False) -> None:
        if _PROJECT_NAME.fullmatch(self.project) is None:
            raise ValueError("project must use the finance-perf-<run> namespace")
        for name in (self.container_name, self.data_init_container_name):
            if _CONTAINER_NAME.fullmatch(name) is None or not name.startswith(f"{self.project}-"):
                raise ValueError("container names must be valid and scoped under the project")
        if not 1_024 <= self.port <= 65_535 or self.port in _SHARED_PORTS:
            raise ValueError("port must be in [1024, 65535] and must not be shared 18001 or 18002")
        if _IMAGE_REFERENCE.fullmatch(self.image_reference) is None:
            raise ValueError("image reference must be an immutable repository@sha256 digest")
        if self.cpu_limit != _BACKEND_CPU_LIMIT:
            raise ValueError("performance backend CPU limit must be exactly 2.0")
        if (
            _MEMORY_LIMIT.fullmatch(self.memory_limit) is None
            or self.memory_limit.casefold() != _BACKEND_MEMORY_LIMIT
        ):
            raise ValueError("performance backend memory limit must be exactly 1g")
        if self.pids_limit != _BACKEND_PIDS_LIMIT:
            raise ValueError("performance backend PID limit must be exactly 256")
        if _SOURCE_COMMIT.fullmatch(self.source_commit) is None:
            raise ValueError("source commit must be a full 40-character lowercase Git hash")
        if (
            not (self.repository / "docker-compose.yml").is_file()
            or not (self.repository / "fastapi_backend/docker-compose.performance.yml").is_file()
        ):
            raise ValueError("repository does not contain the performance Compose files")
        _require_directory(self.raw_data_directory, owner_only=False)
        _require_directory(self.audit_directory, owner_only=True)
        if require_fresh_audit and (self.audit_directory / "events.jsonl").exists():
            raise ValueError(
                "audit directory already contains events.jsonl; use a fresh run directory"
            )

    def environment(self) -> dict[str, str]:
        environment = dict(os.environ)
        environment.pop("CLOVASTUDIO_API_KEY", None)
        environment.update(
            {
                "BACKEND_HOST_UID": str(os.getuid()),
                "BACKEND_HOST_GID": str(os.getgid()),
                "FINANCE_IMAGE_REFERENCE": self.image_reference,
                "FINANCE_RAW_DATA_DIR": str(self.raw_data_directory),
                "FINANCE_PERF_CONTAINER_NAME": self.container_name,
                "FINANCE_PERF_DATA_INIT_CONTAINER_NAME": self.data_init_container_name,
                "FINANCE_PERF_PORT": str(self.port),
                "FINANCE_PERF_AUDIT_HOST_DIR": str(self.audit_directory),
                "FINANCE_PERF_CPU_LIMIT": str(self.cpu_limit),
                "FINANCE_PERF_MEMORY_LIMIT": self.memory_limit,
                "FINANCE_PERF_PIDS_LIMIT": str(self.pids_limit),
                "FINANCE_AUDIT_QUEUE_CAPACITY": "8192",
            }
        )
        return environment

    def compose_prefix(self) -> list[str]:
        return [
            "docker",
            "compose",
            "--project-name",
            self.project,
            "--file",
            str(self.repository / "docker-compose.yml"),
            "--file",
            str(self.repository / "fastapi_backend/docker-compose.performance.yml"),
        ]


def _require_directory(path: Path, *, owner_only: bool) -> None:
    if not path.is_absolute() or path.is_symlink():
        raise ValueError("run directories must be absolute, existing, non-symlink directories")
    try:
        metadata = path.stat()
    except OSError as error:
        raise ValueError("run directory is unavailable") from error
    if not stat.S_ISDIR(metadata.st_mode):
        raise ValueError("run directory is not a directory")
    if owner_only and (metadata.st_uid != os.getuid() or stat.S_IMODE(metadata.st_mode) & 0o077):
        raise ValueError("audit directory must be caller-owned with no group/other permissions")


def _matches_exact_number(value: object, expected: float) -> bool:
    if isinstance(value, bool):
        return False
    if isinstance(value, (int, float)):
        return float(value) == float(expected)
    if isinstance(value, str):
        try:
            return float(value) == float(expected)
        except ValueError:
            return False
    return False


def _matches_exact_memory(value: object, expected_bytes: int) -> bool:
    if isinstance(value, bool):
        return False
    if isinstance(value, int):
        return value == expected_bytes
    if not isinstance(value, str):
        return False
    normalized = value.strip().casefold()
    aliases = {
        str(expected_bytes),
        f"{expected_bytes // 1024**2}m",
    }
    if expected_bytes % 1024**3 == 0:
        aliases.add(f"{expected_bytes // 1024**3}g")
    return normalized in aliases


def _validate_resource_contract(
    backend: Mapping[str, object],
    data_init: Mapping[str, object],
) -> None:
    if not _matches_exact_number(backend.get("cpus"), _BACKEND_CPU_LIMIT):
        raise ValueError("rendered backend CPU limit must be exactly 2.0")
    if not _matches_exact_memory(backend.get("mem_limit"), _BACKEND_MEMORY_BYTES):
        raise ValueError("rendered backend memory limit must be exactly 1 GiB")
    if not _matches_exact_number(backend.get("pids_limit"), _BACKEND_PIDS_LIMIT):
        raise ValueError("rendered backend PID limit must be exactly 256")
    ulimits = backend.get("ulimits")
    nofile = ulimits.get("nofile") if isinstance(ulimits, dict) else None
    if (
        not isinstance(nofile, dict)
        or not _matches_exact_number(nofile.get("soft"), _BACKEND_NOFILE_LIMIT)
        or not _matches_exact_number(nofile.get("hard"), _BACKEND_NOFILE_LIMIT)
    ):
        raise ValueError("rendered backend nofile soft/hard limits must both be 4096")
    if backend.get("restart") != "no":
        raise ValueError("rendered backend restart policy must be no")

    if not _matches_exact_number(data_init.get("cpus"), _DATA_INIT_CPU_LIMIT):
        raise ValueError("rendered data-init CPU limit must be exactly 1.0")
    if not _matches_exact_memory(data_init.get("mem_limit"), _DATA_INIT_MEMORY_BYTES):
        raise ValueError("rendered data-init memory limit must be exactly 512 MiB")
    if not _matches_exact_number(data_init.get("pids_limit"), _DATA_INIT_PIDS_LIMIT):
        raise ValueError("rendered data-init PID limit must be exactly 128")
    if data_init.get("restart") != "no":
        raise ValueError("rendered data-init restart policy must be no")


def validate_rendered_config(
    payload: object,
    *,
    expected: PerformanceRunConfig,
) -> dict[str, Any]:
    if not isinstance(payload, dict) or not isinstance(payload.get("services"), dict):
        raise TypeError("Compose rendered an invalid configuration")
    services: Mapping[str, object] = payload["services"]
    backend = services.get("backend")
    data_init = services.get("data-init")
    if not isinstance(backend, dict) or not isinstance(data_init, dict):
        raise TypeError("rendered configuration is missing required services")
    if backend.get("container_name") != expected.container_name:
        raise ValueError("backend container name differs from the isolated name")
    if data_init.get("container_name") != expected.data_init_container_name:
        raise ValueError("data-init container name differs from the isolated name")
    if (
        backend.get("image") != expected.image_reference
        or data_init.get("image") != expected.image_reference
    ):
        raise ValueError("rendered services must use the immutable image digest")
    _validate_resource_contract(backend, data_init)

    published_ports: list[int] = []
    for service in services.values():
        if not isinstance(service, dict):
            continue
        ports = service.get("ports", [])
        if not isinstance(ports, list):
            raise TypeError("rendered ports have an invalid shape")
        for port in ports:
            if not isinstance(port, dict):
                raise TypeError("rendered port has an invalid shape")
            try:
                published_ports.append(int(port.get("published")))
            except (TypeError, ValueError) as error:
                raise ValueError("rendered published port is invalid") from error
    exposed_shared_ports = sorted(_SHARED_PORTS.intersection(published_ports))
    if exposed_shared_ports:
        raise ValueError("rendered configuration exposes shared port 18001 or 18002")
    backend_ports = backend.get("ports")
    if not isinstance(backend_ports, list) or len(backend_ports) != 1:
        raise ValueError("backend must expose exactly one isolated port")
    backend_port = backend_ports[0]
    if (
        not isinstance(backend_port, dict)
        or int(backend_port.get("target", -1)) != 8_000
        or int(backend_port.get("published", -1)) != expected.port
        or backend_port.get("host_ip") != "127.0.0.1"
    ):
        raise ValueError("backend port binding differs from the isolated loopback binding")

    volumes = backend.get("volumes")
    audit_volumes = (
        [
            volume
            for volume in volumes
            if isinstance(volume, dict) and volume.get("target") == "/audit"
        ]
        if isinstance(volumes, list)
        else []
    )
    if len(audit_volumes) != 1 or (
        audit_volumes[0].get("type") != "bind"
        or Path(str(audit_volumes[0].get("source"))).resolve() != expected.audit_directory.resolve()
    ):
        raise ValueError("backend audit bind differs from the owner-only run directory")

    environment = backend.get("environment")
    required_environment = {
        "APP_ENV": "development",
        "WEB_CONCURRENCY": "1",
        "OFFICIAL_ANSWER_TIMEOUT_SECONDS": "55",
        "OFFICIAL_ANSWER_MAX_INFLIGHT": "4",
        "FINANCE_BACKEND_ANSWER_PROVIDER": "deterministic",
        "FINANCE_BACKEND_HCX_QUERY_PLAN_ENABLED": "false",
        "FINANCE_AGENT_LLM_MODE": "disabled",
        "LLM_PROVIDER": "disabled",
        "FINANCE_BACKEND_FUND_EXECUTION_POLICY": "locked",
        "FINANCE_DENSE_SCHEMA_LINKER_ENABLED": "false",
        "FINANCE_PRODUCT_DENSE_ENABLED": "false",
        "FINANCE_AUDIT_MODE": "jsonl",
        "FINANCE_AUDIT_FILE": "/audit/events.jsonl",
        "FINANCE_AUDIT_QUEUE_CAPACITY": "8192",
        "FINANCE_AUDIT_SHUTDOWN_TIMEOUT_SECONDS": "30",
        "FINANCE_AUDIT_FSYNC_EACH_EVENT": "true",
    }
    if not isinstance(environment, dict) or any(
        str(environment.get(name)).casefold() != value
        for name, value in required_environment.items()
    ):
        raise ValueError("rendered deterministic or audit controls differ from the run contract")
    return {
        "project": expected.project,
        "container_name": expected.container_name,
        "published_port": expected.port,
        "shared_port_18001_exposed": False,
        "shared_port_18002_exposed": False,
        "audit_directory": str(expected.audit_directory),
        "image_reference": expected.image_reference,
        "source_commit": expected.source_commit,
        "resource_contract": {
            "backend_cpus": _BACKEND_CPU_LIMIT,
            "backend_memory_bytes": _BACKEND_MEMORY_BYTES,
            "backend_pids_limit": _BACKEND_PIDS_LIMIT,
            "backend_nofile_soft": _BACKEND_NOFILE_LIMIT,
            "backend_nofile_hard": _BACKEND_NOFILE_LIMIT,
            "backend_restart": "no",
            "data_init_cpus": _DATA_INIT_CPU_LIMIT,
            "data_init_memory_bytes": _DATA_INIT_MEMORY_BYTES,
            "data_init_pids_limit": _DATA_INIT_PIDS_LIMIT,
            "data_init_restart": "no",
        },
        "release_linkage_capable": False,
    }


def _run(
    arguments: Sequence[str],
    *,
    environment: Mapping[str, str],
    cwd: Path,
    capture_output: bool = False,
) -> subprocess.CompletedProcess[bytes]:
    try:
        completed = subprocess.run(
            list(arguments),
            cwd=cwd,
            env=dict(environment),
            check=False,
            capture_output=capture_output,
            timeout=600,
        )
    except FileNotFoundError as error:
        raise RuntimeError("docker executable is unavailable") from error
    except (OSError, subprocess.TimeoutExpired) as error:
        raise RuntimeError("isolated Docker command failed") from error
    if completed.returncode != 0:
        raise RuntimeError("isolated Docker command returned a non-zero status")
    return completed


def render_and_validate(config: PerformanceRunConfig) -> dict[str, Any]:
    completed = _run(
        [*config.compose_prefix(), "config", "--format", "json"],
        environment=config.environment(),
        cwd=config.repository,
        capture_output=True,
    )
    if len(completed.stdout) > _MAX_CONFIG_BYTES:
        raise RuntimeError("rendered Compose configuration is oversized")
    try:
        payload = json.loads(completed.stdout)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise RuntimeError("Compose did not return a JSON configuration") from error
    return validate_rendered_config(payload, expected=config)


def inspect_image_identity(config: PerformanceRunConfig) -> dict[str, str]:
    completed = _run(
        ["docker", "image", "inspect", config.image_reference],
        environment=config.environment(),
        cwd=config.repository,
        capture_output=True,
    )
    if len(completed.stdout) > _MAX_CONFIG_BYTES:
        raise RuntimeError("Docker image identity output is oversized")
    try:
        payload = json.loads(completed.stdout)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise RuntimeError("Docker image identity is invalid") from error
    if not isinstance(payload, list) or len(payload) != 1 or not isinstance(payload[0], dict):
        raise RuntimeError("Docker image identity has an invalid shape")
    image = payload[0]
    image_id = image.get("Id")
    repo_digests = image.get("RepoDigests")
    image_config = image.get("Config")
    labels = image_config.get("Labels") if isinstance(image_config, dict) else None
    if (
        not isinstance(image_id, str)
        or re.fullmatch(r"sha256:[0-9a-f]{64}", image_id) is None
        or not isinstance(repo_digests, list)
        or config.image_reference not in repo_digests
        or not isinstance(labels, dict)
        or labels.get("org.opencontainers.image.revision") != config.source_commit
        or image.get("Os") != "linux"
        or image.get("Architecture") != "amd64"
    ):
        raise RuntimeError("Docker image digest, platform, or source label differs")
    return {
        "image_reference": config.image_reference,
        "image_id": image_id,
        "source_commit": config.source_commit,
        "platform": "linux/amd64",
    }


def execute(action: str, config: PerformanceRunConfig) -> dict[str, Any]:
    config.validate(require_fresh_audit=action == "up")
    summary = render_and_validate(config)
    environment = config.environment()
    if action == "config":
        return summary
    if action == "up":
        identity = inspect_image_identity(config)
        _run(
            [
                *config.compose_prefix(),
                "up",
                "--detach",
                "--no-build",
                "--wait",
                "--wait-timeout",
                "120",
            ],
            environment=environment,
            cwd=config.repository,
        )
        return {**summary, "action": "up", "image_identity": identity}
    if action == "down":
        _run(
            [
                *config.compose_prefix(),
                "down",
                "--volumes",
                "--remove-orphans",
                "--timeout",
                "35",
            ],
            environment=environment,
            cwd=config.repository,
        )
        return {**summary, "action": "down"}
    raise ValueError("unsupported action")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Build and control one resource-bounded performance container without exposing 18001."
        )
    )
    parser.add_argument("action", choices=("config", "up", "down"))
    parser.add_argument("--repository", type=Path, default=Path(__file__).resolve().parents[2])
    parser.add_argument("--project", required=True)
    parser.add_argument("--container-name", required=True)
    parser.add_argument("--data-init-container-name")
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--audit-dir", type=Path, required=True)
    parser.add_argument("--raw-data-dir", type=Path, required=True)
    parser.add_argument("--image-reference", required=True)
    parser.add_argument("--source-commit", required=True)
    parser.add_argument("--cpu-limit", type=float, default=2.0)
    parser.add_argument("--memory-limit", default="1g")
    parser.add_argument("--pids-limit", type=int, default=256)
    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = build_parser().parse_args(argv)
    container_name = arguments.container_name
    config = PerformanceRunConfig(
        repository=arguments.repository.resolve(),
        project=arguments.project,
        container_name=container_name,
        data_init_container_name=(
            arguments.data_init_container_name or f"{container_name}-data-init"
        ),
        port=arguments.port,
        audit_directory=arguments.audit_dir.resolve(),
        raw_data_directory=arguments.raw_data_dir.resolve(),
        image_reference=arguments.image_reference,
        source_commit=arguments.source_commit,
        cpu_limit=arguments.cpu_limit,
        memory_limit=arguments.memory_limit,
        pids_limit=arguments.pids_limit,
    )
    try:
        report = execute(arguments.action, config)
    except (RuntimeError, TypeError, ValueError) as error:
        print(f"isolated performance run failed: {error}", file=sys.stderr)
        return 2
    print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
