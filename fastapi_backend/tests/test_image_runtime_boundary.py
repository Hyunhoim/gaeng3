from __future__ import annotations

import hashlib
import json

from fastapi_backend.scripts import image_runtime_boundary


def _safe_runtime(monkeypatch) -> None:
    monkeypatch.setattr(
        image_runtime_boundary,
        "_installed_distributions",
        lambda: {"fastapi": "0.116.1", "finance-agent-core": "0.1.0"},
    )
    monkeypatch.setattr(image_runtime_boundary, "_forbidden_files", list)
    monkeypatch.setattr(image_runtime_boundary.os, "getuid", lambda: 10001)
    monkeypatch.setattr(image_runtime_boundary.os, "getgid", lambda: 10001)
    monkeypatch.setattr(image_runtime_boundary.shutil, "which", lambda _name: None)
    monkeypatch.setattr(
        image_runtime_boundary,
        "_embedded_manifest_binding",
        lambda **values: {
            "verified": True,
            "adaptive_semantic_enabled": False,
            "expected_sha256": values["expected_sha256"],
            "observed_sha256": values["expected_sha256"],
            "failure_code": None,
        },
    )
    monkeypatch.setenv(
        "FINANCE_RUNTIME_IMAGE_REFERENCE",
        "registry.example/finance-agent@sha256:" + "a" * 64,
    )
    monkeypatch.setenv("FINANCE_RELEASE_ID", "finance-agent-eval-001")
    monkeypatch.setenv("FINANCE_SOURCE_COMMIT", "b" * 40)
    monkeypatch.setenv("FINANCE_RELEASE_MANIFEST_SHA256", "c" * 64)
    for name in image_runtime_boundary._FORBIDDEN_ENVIRONMENT_NAMES:
        monkeypatch.delenv(name, raising=False)


def test_runtime_boundary_accepts_non_root_hcx_only_dependency_image(monkeypatch) -> None:
    _safe_runtime(monkeypatch)

    report = image_runtime_boundary.build_report()

    assert report["passed"] is True
    assert report["uid"] == 10001
    assert report["release_identity_valid"] is True
    assert report["forbidden_distributions"] == []
    assert report["forbidden_executables"] == []
    assert report["forbidden_files"] == []
    assert report["embedded_release_manifest"]["verified"] is True
    assert all(report["runtime_guards"].values())


def test_runtime_boundary_rejects_local_model_distribution(monkeypatch) -> None:
    _safe_runtime(monkeypatch)
    monkeypatch.setattr(
        image_runtime_boundary,
        "_installed_distributions",
        lambda: {"fastapi": "0.116.1", "torch": "2.9.0"},
    )

    report = image_runtime_boundary.build_report()

    assert report["passed"] is False
    assert report["forbidden_distributions"] == ["torch"]


def test_runtime_boundary_rejects_missing_release_identity(monkeypatch) -> None:
    _safe_runtime(monkeypatch)
    monkeypatch.delenv("FINANCE_RUNTIME_IMAGE_REFERENCE")

    report = image_runtime_boundary.build_report()

    assert report["passed"] is False
    assert report["release_identity_valid"] is False


def test_runtime_boundary_rejects_unbound_embedded_manifest(monkeypatch) -> None:
    _safe_runtime(monkeypatch)
    monkeypatch.setattr(
        image_runtime_boundary,
        "_embedded_manifest_binding",
        lambda **_values: {
            "verified": False,
            "expected_sha256": "c" * 64,
            "observed_sha256": "d" * 64,
            "failure_code": "manifest_sha256_mismatch",
        },
    )

    report = image_runtime_boundary.build_report()

    assert report["passed"] is False
    assert report["embedded_release_manifest"]["failure_code"] == "manifest_sha256_mismatch"


def test_embedded_manifest_binding_requires_exact_bytes_and_release_identity(tmp_path) -> None:
    release_id = "finance-agent-eval-001"
    source_commit = "b" * 40
    manifest = tmp_path / "agent-release-manifest.json"
    data = (
        json.dumps(
            {
                "schema_version": "1.2",
                "release_id": release_id,
                "source_commit": source_commit,
                "components": {
                    "runtime_features": {"retrieval": {"schema_dense": "disabled_offline_only"}}
                },
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    ).encode()
    manifest.write_bytes(data)

    verified = image_runtime_boundary._embedded_manifest_binding(
        expected_sha256=hashlib.sha256(data).hexdigest(),
        release_id=release_id,
        source_commit=source_commit,
        manifest_path=manifest,
    )
    tampered_hash = image_runtime_boundary._embedded_manifest_binding(
        expected_sha256="c" * 64,
        release_id=release_id,
        source_commit=source_commit,
        manifest_path=manifest,
    )
    wrong_identity = image_runtime_boundary._embedded_manifest_binding(
        expected_sha256=hashlib.sha256(data).hexdigest(),
        release_id="finance-agent-eval-002",
        source_commit=source_commit,
        manifest_path=manifest,
    )

    assert verified["verified"] is True
    assert tampered_hash["failure_code"] == "manifest_sha256_mismatch"
    assert wrong_identity["failure_code"] == "manifest_identity_mismatch"


def test_runtime_boundary_allows_only_exact_adaptive_dependencies(monkeypatch) -> None:
    _safe_runtime(monkeypatch)
    monkeypatch.setattr(
        image_runtime_boundary,
        "_embedded_manifest_binding",
        lambda **values: {
            "verified": True,
            "adaptive_semantic_enabled": True,
            "expected_sha256": values["expected_sha256"],
            "observed_sha256": values["expected_sha256"],
            "failure_code": None,
        },
    )
    monkeypatch.setattr(
        image_runtime_boundary,
        "_installed_distributions",
        lambda: {
            "fastapi": "0.116.1",
            "finance-agent-core": "0.1.0",
            **image_runtime_boundary._ADAPTIVE_DISTRIBUTIONS,
        },
    )

    report = image_runtime_boundary.build_report()

    assert report["passed"] is True
    assert report["forbidden_distributions"] == []
    assert report["adaptive_dependency_mismatches"] == []


def test_runtime_boundary_settings_guards_reject_non_hcx_release_paths() -> None:
    assert image_runtime_boundary._settings_reject(
        "FINANCE_BACKEND_ANSWER_PROVIDER=local_test is allowed only in development",
        APP_ENV="evaluation",
        FINANCE_BACKEND_ANSWER_PROVIDER="local_test",
    )
    assert image_runtime_boundary._settings_reject(
        "Schema Dense artifacts cannot be configured while adaptive semantics is off",
        APP_ENV="evaluation",
        FINANCE_DENSE_SCHEMA_LINKER_ENABLED=True,
    )
    assert image_runtime_boundary._settings_reject(
        "Product Dense remains disabled in the evaluation runtime",
        APP_ENV="evaluation",
        FINANCE_PRODUCT_DENSE_ENABLED=True,
    )
    assert image_runtime_boundary._settings_accept_safe_development()


def test_runtime_boundary_normalizes_distribution_names() -> None:
    assert image_runtime_boundary._normalized_distribution_name("Torch_CPU") == "torch-cpu"
