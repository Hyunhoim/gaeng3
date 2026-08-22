from __future__ import annotations

import hashlib
import importlib.metadata
import importlib.util
import json
import os
import re
import shutil
import stat
import sys
import sysconfig
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from app.config import Settings

_FORBIDDEN_DISTRIBUTIONS = {
    "accelerate",
    "bitsandbytes",
    "ctransformers",
    "jax",
    "jaxlib",
    "llama-cpp-python",
    "onnxruntime",
    "pytorch",
    "sentence-transformers",
    "tensorflow",
    "torch",
    "transformers",
    "vllm",
}
_FORBIDDEN_DISTRIBUTION_PREFIXES = (
    "sentence-transformers",
    "tensorflow",
    "torch",
    "transformers",
    "vllm",
)
_FORBIDDEN_EXECUTABLES = (
    "llama-cli",
    "llama-server",
    "ollama",
    "torchrun",
    "transformers-cli",
    "vllm",
)
_ADAPTIVE_DISTRIBUTIONS = {
    "einops": "0.8.2",
    "filelock": "3.32.2",
    "fsspec": "2026.7.0",
    "huggingface-hub": "1.27.0",
    "joblib": "1.5.3",
    "numpy": "2.5.2",
    "regex": "2026.7.19",
    "safetensors": "0.8.0",
    "scikit-learn": "1.9.0",
    "scipy": "1.18.0",
    "sentence-transformers": "5.7.0",
    "tokenizers": "0.22.2",
    "torch": "2.13.0+cpu",
    "tqdm": "4.70.0",
    "transformers": "5.15.0",
}
_ADAPTIVE_EXECUTABLES = {"torchrun", "transformers-cli"}
_FORBIDDEN_ENVIRONMENT_NAMES = (
    "CLOVASTUDIO_API_KEY",
    "ENABLE_NON_HCX_TEST_LLM",
    "LOCAL_TEST_LLM_BASE_URL",
)
_MODEL_SUFFIXES = {".bin", ".ckpt", ".gguf", ".onnx", ".pt", ".pth", ".safetensors"}
_APP_FORBIDDEN_NAMES = {".env", ".env.release"}
_APP_FORBIDDEN_SUFFIXES = {".sqlite", ".sqlite3", ".xls", ".xlsx"}
_IMAGE_REFERENCE_PATTERN = re.compile(r"[a-z0-9][a-z0-9._:/-]{2,255}@sha256:[0-9a-f]{64}")
_RELEASE_ID_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")
_SOURCE_COMMIT_PATTERN = re.compile(r"[0-9a-f]{40}")
_SHA256_PATTERN = re.compile(r"[0-9a-f]{64}")
_EMBEDDED_RELEASE_MANIFEST = Path("/app/release/agent-release-manifest.json")
_MAX_RELEASE_MANIFEST_BYTES = 2 * 1024 * 1024
_SETTINGS_RELEASE_NULLS = {
    "FINANCE_DEPLOYMENT_BINDING_FILE": None,
    "FINANCE_DEPLOYMENT_BINDING_SHA256": None,
    "FINANCE_RELEASE_MANIFEST_FILE": None,
    "FINANCE_RUNTIME_IMAGE_REFERENCE": None,
    "FINANCE_SOURCE_COMMIT": None,
}


def _normalized_distribution_name(value: str) -> str:
    return re.sub(r"[-_.]+", "-", value.casefold())


def _installed_distributions() -> dict[str, str]:
    installed: dict[str, str] = {}
    for distribution in importlib.metadata.distributions():
        name = distribution.metadata.get("Name")
        if not name:
            continue
        installed[_normalized_distribution_name(name)] = distribution.version
    return dict(sorted(installed.items()))


def _forbidden_files() -> list[str]:
    roots = {
        "app": Path("/app"),
        "site-packages": Path(sysconfig.get_path("purelib")),
    }
    findings: list[str] = []
    for label, root in roots.items():
        if not root.is_dir():
            findings.append(f"{label}:missing")
            continue
        for candidate in root.rglob("*"):
            if not candidate.is_file():
                continue
            suffix = candidate.suffix.casefold()
            if suffix in _MODEL_SUFFIXES or (
                label == "app"
                and (
                    candidate.name.casefold() in _APP_FORBIDDEN_NAMES
                    or suffix in _APP_FORBIDDEN_SUFFIXES
                )
            ):
                findings.append(f"{label}:{candidate.relative_to(root).as_posix()}")
    return sorted(findings)


def _settings_reject(expected_message: str, **values: object) -> bool:
    try:
        Settings(_env_file=None, **_SETTINGS_RELEASE_NULLS, **values)
    except (TypeError, ValidationError, ValueError) as error:
        return expected_message in str(error)
    return False


def _settings_accept_safe_development() -> bool:
    try:
        Settings(
            _env_file=None,
            **_SETTINGS_RELEASE_NULLS,
            APP_ENV="development",
            FINANCE_BACKEND_ANSWER_PROVIDER="deterministic",
            FINANCE_DENSE_SCHEMA_LINKER_ENABLED=False,
            FINANCE_PRODUCT_DENSE_ENABLED=False,
        )
    except (TypeError, ValidationError, ValueError):
        return False
    return True


def _strict_json_object(data: bytes) -> dict[str, Any]:
    def reject_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        value: dict[str, Any] = {}
        for key, item in pairs:
            if key in value:
                raise ValueError("duplicate JSON key")
            value[key] = item
        return value

    def reject_constant(value: str) -> None:
        raise ValueError(f"non-finite JSON number: {value}")

    decoded = json.loads(
        data,
        object_pairs_hook=reject_duplicates,
        parse_constant=reject_constant,
    )
    if not isinstance(decoded, dict):
        raise TypeError("release manifest must be a JSON object")
    return decoded


def _embedded_manifest_binding(
    *,
    expected_sha256: str,
    release_id: str,
    source_commit: str,
    manifest_path: Path = _EMBEDDED_RELEASE_MANIFEST,
) -> dict[str, object]:
    result: dict[str, object] = {
        "verified": False,
        "adaptive_semantic_enabled": False,
        "expected_sha256": expected_sha256,
        "observed_sha256": None,
        "failure_code": None,
    }
    if _SHA256_PATTERN.fullmatch(expected_sha256) is None:
        result["failure_code"] = "invalid_expected_sha256"
        return result
    descriptor: int | None = None
    try:
        before = manifest_path.stat(follow_symlinks=False)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_nlink != 1
            or before.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
        ):
            result["failure_code"] = "unsafe_manifest_file"
            return result
        descriptor = os.open(
            manifest_path,
            os.O_RDONLY | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0),
        )
        data = os.read(descriptor, _MAX_RELEASE_MANIFEST_BYTES + 1)
        opened = os.fstat(descriptor)
        current = manifest_path.stat(follow_symlinks=False)
    except OSError:
        result["failure_code"] = "manifest_unavailable"
        return result
    finally:
        if descriptor is not None:
            os.close(descriptor)
    fingerprint = lambda value: (
        value.st_dev,
        value.st_ino,
        value.st_mode,
        value.st_uid,
        value.st_nlink,
        value.st_size,
        value.st_mtime_ns,
        value.st_ctime_ns,
    )
    if (
        fingerprint(before) != fingerprint(opened)
        or fingerprint(opened) != fingerprint(current)
        or not data
        or len(data) > _MAX_RELEASE_MANIFEST_BYTES
    ):
        result["failure_code"] = "manifest_changed_or_invalid_size"
        return result
    observed_sha256 = hashlib.sha256(data).hexdigest()
    result["observed_sha256"] = observed_sha256
    if observed_sha256 != expected_sha256:
        result["failure_code"] = "manifest_sha256_mismatch"
        return result
    try:
        manifest = _strict_json_object(data)
    except (UnicodeError, json.JSONDecodeError, TypeError, ValueError):
        result["failure_code"] = "manifest_not_strict_json"
        return result
    if (
        manifest.get("schema_version") != "1.2"
        or manifest.get("release_id") != release_id
        or manifest.get("source_commit") != source_commit
    ):
        result["failure_code"] = "manifest_identity_mismatch"
        return result
    try:
        schema_dense = manifest["components"]["runtime_features"]["retrieval"]["schema_dense"]
    except (KeyError, TypeError):
        result["failure_code"] = "manifest_runtime_profile_missing"
        return result
    if schema_dense not in {
        "disabled_offline_only",
        "activated_kure_candidate_only",
    }:
        result["failure_code"] = "manifest_runtime_profile_invalid"
        return result
    result["adaptive_semantic_enabled"] = schema_dense == "activated_kure_candidate_only"
    result["verified"] = True
    return result


def build_report() -> dict[str, object]:
    distributions = _installed_distributions()
    forbidden_environment = sorted(
        name for name in _FORBIDDEN_ENVIRONMENT_NAMES if os.environ.get(name)
    )
    forbidden_files = _forbidden_files()
    image_reference = os.environ.get("FINANCE_RUNTIME_IMAGE_REFERENCE", "")
    release_id = os.environ.get("FINANCE_RELEASE_ID", "")
    source_commit = os.environ.get("FINANCE_SOURCE_COMMIT", "")
    manifest_sha256 = os.environ.get("FINANCE_RELEASE_MANIFEST_SHA256", "")
    release_identity_valid = (
        _IMAGE_REFERENCE_PATTERN.fullmatch(image_reference) is not None
        and _RELEASE_ID_PATTERN.fullmatch(release_id) is not None
        and _SOURCE_COMMIT_PATTERN.fullmatch(source_commit) is not None
    )
    embedded_manifest = _embedded_manifest_binding(
        expected_sha256=manifest_sha256,
        release_id=release_id,
        source_commit=source_commit,
    )
    adaptive = embedded_manifest.get("adaptive_semantic_enabled") is True
    allowed_distributions = set(_ADAPTIVE_DISTRIBUTIONS) if adaptive else set()
    forbidden_distributions = sorted(
        name
        for name in distributions
        if name not in allowed_distributions
        and (
            name in _FORBIDDEN_DISTRIBUTIONS
            or any(
                name == prefix or name.startswith(prefix + "-")
                for prefix in _FORBIDDEN_DISTRIBUTION_PREFIXES
            )
        )
    )
    adaptive_dependency_mismatches = (
        sorted(
            f"{name}:{distributions.get(name, 'missing')}!={expected}"
            for name, expected in _ADAPTIVE_DISTRIBUTIONS.items()
            if distributions.get(name) != expected
        )
        if adaptive
        else []
    )
    allowed_executables = _ADAPTIVE_EXECUTABLES if adaptive else set()
    forbidden_executables = sorted(
        executable
        for executable in _FORBIDDEN_EXECUTABLES
        if executable not in allowed_executables and shutil.which(executable)
    )
    guards = {
        "evaluation_rejects_inline_hcx_key": _settings_reject(
            "inline HyperCLOVA credential is forbidden",
            APP_ENV="evaluation",
            CLOVASTUDIO_API_KEY="must-not-be-loaded",
        ),
        "evaluation_rejects_local_answer_provider": _settings_reject(
            "FINANCE_BACKEND_ANSWER_PROVIDER=local_test is allowed only in development",
            APP_ENV="evaluation",
            FINANCE_BACKEND_ANSWER_PROVIDER="local_test",
        ),
        "evaluation_rejects_product_dense": _settings_reject(
            "Product Dense remains disabled in the evaluation runtime",
            APP_ENV="evaluation",
            FINANCE_PRODUCT_DENSE_ENABLED=True,
        ),
        "evaluation_rejects_partial_schema_dense": _settings_reject(
            "Schema Dense artifacts cannot be configured while adaptive semantics is off",
            APP_ENV="evaluation",
            FINANCE_DENSE_SCHEMA_LINKER_ENABLED=True,
        ),
        "safe_development_settings_accepted": _settings_accept_safe_development(),
    }
    passed = (
        sys.version_info[:2] == (3, 12)
        and os.getuid() == 10001
        and os.getgid() == 10001
        and not forbidden_distributions
        and not forbidden_executables
        and not adaptive_dependency_mismatches
        and not forbidden_environment
        and not forbidden_files
        and release_identity_valid
        and embedded_manifest["verified"] is True
        and all(guards.values())
    )
    return {
        "schema_version": "1.0",
        "passed": passed,
        "python_version": f"{sys.version_info.major}.{sys.version_info.minor}",
        "uid": os.getuid(),
        "gid": os.getgid(),
        "image_reference": image_reference,
        "release_id": release_id,
        "source_commit": source_commit,
        "release_identity_valid": release_identity_valid,
        "embedded_release_manifest": embedded_manifest,
        "installed_distributions": distributions,
        "forbidden_distributions": forbidden_distributions,
        "forbidden_executables": forbidden_executables,
        "adaptive_dependency_mismatches": adaptive_dependency_mismatches,
        "forbidden_environment_names": forbidden_environment,
        "forbidden_files": forbidden_files,
        "runtime_guards": guards,
        "local_development_provider_source_present": (
            importlib.util.find_spec("finance_agent_core.agent.providers.local_test") is not None
        ),
        "interpretation": (
            (
                "The release contains only the exact approved KURE CPU dependencies; model and "
                "index artifacts remain external read-only mounts."
                if adaptive
                else "The lightweight release contains no detected local-model dependency."
            )
            + " No image profile may contain a database, model weight, or inline credential."
        ),
    }


def main() -> int:
    report = build_report()
    print(json.dumps(report, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
    return 0 if report["passed"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
