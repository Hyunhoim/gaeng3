from __future__ import annotations

from pathlib import Path

import pytest

from scripts.isolated_performance_run import (
    PerformanceRunConfig,
    validate_rendered_config,
)


def _config(tmp_path: Path, *, port: int = 18_081) -> PerformanceRunConfig:
    repository = tmp_path / "repository"
    (repository / "fastapi_backend").mkdir(parents=True)
    (repository / "docker-compose.yml").touch()
    (repository / "fastapi_backend/docker-compose.performance.yml").touch()
    audit = tmp_path / "audit"
    audit.mkdir(mode=0o700)
    raw = tmp_path / "raw"
    raw.mkdir()
    return PerformanceRunConfig(
        repository=repository,
        project="finance-perf-unit",
        container_name="finance-perf-unit-backend",
        data_init_container_name="finance-perf-unit-data-init",
        port=port,
        audit_directory=audit,
        raw_data_directory=raw,
        image_reference="localhost:50141/finance-perf-unit@sha256:" + "a" * 64,
        source_commit="b" * 40,
    )


def _rendered(config: PerformanceRunConfig) -> dict[str, object]:
    return {
        "services": {
            "data-init": {
                "container_name": config.data_init_container_name,
                "image": config.image_reference,
                "cpus": 1.0,
                "mem_limit": "536870912",
                "pids_limit": 128,
                "restart": "no",
            },
            "backend": {
                "container_name": config.container_name,
                "image": config.image_reference,
                "cpus": 2.0,
                "mem_limit": "1073741824",
                "pids_limit": 256,
                "ulimits": {"nofile": {"soft": 4096, "hard": 4096}},
                "restart": "no",
                "environment": {
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
                },
                "ports": [
                    {
                        "target": 8_000,
                        "published": str(config.port),
                        "host_ip": "127.0.0.1",
                    }
                ],
                "volumes": [
                    {
                        "type": "volume",
                        "source": "finance-data",
                        "target": "/data",
                    },
                    {
                        "type": "bind",
                        "source": str(config.audit_directory),
                        "target": "/audit",
                    },
                ],
            },
        }
    }


def test_config_rejects_shared_port_and_non_private_audit_directory(tmp_path: Path) -> None:
    for port in (18_001, 18_002):
        shared_port = _config(tmp_path / f"shared-{port}", port=port)
        with pytest.raises(ValueError, match="shared 18001 or 18002"):
            shared_port.validate()

    insecure = _config(tmp_path / "insecure")
    insecure.audit_directory.chmod(0o755)
    with pytest.raises(ValueError, match="caller-owned"):
        insecure.validate()

    mutable = _config(tmp_path / "mutable")
    object.__setattr__(mutable, "image_reference", "finance-perf-unit:candidate")
    with pytest.raises(ValueError, match="repository@sha256"):
        mutable.validate()


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("cpu_limit", 1.5, "CPU limit must be exactly 2.0"),
        ("memory_limit", "2g", "memory limit must be exactly 1g"),
        ("pids_limit", 512, "PID limit must be exactly 256"),
    ],
)
def test_config_rejects_resource_limit_override(
    tmp_path: Path,
    field: str,
    value: object,
    message: str,
) -> None:
    config = _config(tmp_path)
    object.__setattr__(config, field, value)

    with pytest.raises(ValueError, match=message):
        config.validate()


def test_config_requires_fresh_audit_file_for_up(tmp_path: Path) -> None:
    config = _config(tmp_path)
    (config.audit_directory / "events.jsonl").touch(mode=0o600)

    config.validate(require_fresh_audit=False)
    with pytest.raises(ValueError, match="fresh run directory"):
        config.validate(require_fresh_audit=True)


def test_rendered_config_requires_exact_isolated_loopback_binding(tmp_path: Path) -> None:
    config = _config(tmp_path)
    config.validate()

    summary = validate_rendered_config(_rendered(config), expected=config)

    assert summary["published_port"] == 18_081
    assert summary["shared_port_18001_exposed"] is False
    assert summary["shared_port_18002_exposed"] is False
    assert summary["resource_contract"]["backend_memory_bytes"] == 1024**3

    for port in (18_001, 18_002):
        unsafe = _rendered(config)
        backend = unsafe["services"]["backend"]  # type: ignore[index]
        backend["ports"].append(  # type: ignore[index, union-attr]
            {"target": 8_000, "published": str(port), "host_ip": "127.0.0.1"}
        )
        with pytest.raises(ValueError, match="shared port 18001 or 18002"):
            validate_rendered_config(unsafe, expected=config)


def test_rendered_config_rejects_disabled_fsync(tmp_path: Path) -> None:
    config = _config(tmp_path)
    rendered = _rendered(config)
    backend = rendered["services"]["backend"]  # type: ignore[index]
    backend["environment"]["FINANCE_AUDIT_FSYNC_EACH_EVENT"] = "false"  # type: ignore[index]

    with pytest.raises(ValueError, match="deterministic or audit controls"):
        validate_rendered_config(rendered, expected=config)


@pytest.mark.parametrize(
    ("service", "field", "value", "message"),
    [
        ("backend", "cpus", 1.0, "backend CPU"),
        ("backend", "mem_limit", "512m", "backend memory"),
        ("backend", "pids_limit", 128, "backend PID"),
        ("backend", "ulimits", {"nofile": {"soft": 4096, "hard": 8192}}, "nofile"),
        ("backend", "restart", "unless-stopped", "backend restart"),
        ("data-init", "cpus", 2.0, "data-init CPU"),
        ("data-init", "mem_limit", "1g", "data-init memory"),
        ("data-init", "pids_limit", 256, "data-init PID"),
        ("data-init", "restart", "always", "data-init restart"),
    ],
)
def test_rendered_config_rejects_resource_or_restart_drift(
    tmp_path: Path,
    service: str,
    field: str,
    value: object,
    message: str,
) -> None:
    config = _config(tmp_path)
    rendered = _rendered(config)
    rendered["services"][service][field] = value  # type: ignore[index]

    with pytest.raises(ValueError, match=message):
        validate_rendered_config(rendered, expected=config)
