import hashlib
import json
import os
import re
from pathlib import Path

import pytest

from finance_agent_core.evaluation.schema_embedding_artifacts import (
    SchemaEmbeddingArtifactError,
    SchemaEmbeddingSnapshotFile,
    SchemaEmbeddingSnapshotManifestV2,
    create_schema_embedding_snapshot_manifest,
    default_schema_embedding_snapshot_manifest_path,
    load_schema_embedding_candidate_link,
    load_schema_embedding_snapshot_manifest,
    require_schema_embedding_artifact_gate,
    schema_embedding_linkage_key,
    verify_schema_embedding_snapshot_manifest,
    write_schema_embedding_snapshot_manifest,
)
from finance_agent_core.evaluation.schema_embedding_models import (
    load_schema_embedding_model_registry,
)


def _build_materialized_snapshot(root: Path) -> Path:
    snapshot = root / "snapshot"
    (snapshot / "1_Pooling").mkdir(parents=True)
    (snapshot / "model.safetensors").write_bytes(b"frozen-model-weights")
    (snapshot / "tokenizer.json").write_text('{"version":"1.0"}\n', encoding="utf-8")
    (snapshot / "config.json").write_text('{"hidden_size":1024}\n', encoding="utf-8")
    (snapshot / "1_Pooling" / "config.json").write_text(
        '{"pooling_mode_cls_token":true}\n',
        encoding="utf-8",
    )
    (snapshot / "README.md").write_text("local materialized snapshot\n", encoding="utf-8")
    return snapshot


def _write_manifest(tmp_path: Path, *, alias: str = "bge-m3") -> tuple[Path, Path]:
    snapshot = _build_materialized_snapshot(tmp_path)
    manifest = create_schema_embedding_snapshot_manifest(snapshot, alias=alias)
    manifest_path = tmp_path / "manifests" / f"{alias}-snapshot-v2.json"
    write_schema_embedding_snapshot_manifest(manifest, manifest_path)
    return snapshot, manifest_path


def test_snapshot_manifest_v2_binds_registry_revision_and_canonical_file_hashes(
    tmp_path: Path,
) -> None:
    snapshot = _build_materialized_snapshot(tmp_path)

    manifest = create_schema_embedding_snapshot_manifest(snapshot, alias="bge-m3")
    candidate = load_schema_embedding_candidate_link("bge-m3")

    assert manifest.candidate == candidate
    assert candidate.linkage_key == schema_embedding_linkage_key(
        "BAAI/bge-m3",
        "5617a9f61b028005a4858fdac845db406aefb181",
    )
    assert [item.relative_path for item in manifest.files] == sorted(
        item.relative_path for item in manifest.files
    )
    assert manifest.file_count == 5
    assert manifest.total_size_bytes == sum(item.size_bytes for item in manifest.files)
    assert manifest.category_digests["weights"].file_count == 1
    assert manifest.category_digests["tokenizer"].file_count == 1
    assert manifest.category_digests["config"].file_count == 2
    assert manifest.category_digests["other"].file_count == 1
    weight = next(item for item in manifest.files if item.category == "weights")
    assert weight.relative_path == "model.safetensors"
    assert weight.sha256 == hashlib.sha256(b"frozen-model-weights").hexdigest()
    assert (
        default_schema_embedding_snapshot_manifest_path("bge-m3").as_posix()
        == "artifacts/evaluation/schema-embedding/snapshot-manifests/"
        "bge-m3-5617a9f61b028005a4858fdac845db406aefb181-snapshot-v2.json"
    )


@pytest.mark.parametrize("alias", ["bge-m3", "kure-v1"])
def test_round_trip_manifest_verification_and_artifact_gate(alias: str, tmp_path: Path) -> None:
    snapshot, manifest_path = _write_manifest(tmp_path, alias=alias)

    loaded = load_schema_embedding_snapshot_manifest(manifest_path)
    verify_schema_embedding_snapshot_manifest(loaded, snapshot, alias=alias)
    shadow = require_schema_embedding_artifact_gate(
        mode="shadow",
        alias=alias,
        snapshot_dir=snapshot,
        manifest_path=manifest_path,
    )
    production = require_schema_embedding_artifact_gate(
        mode="production",
        alias=alias,
        snapshot_dir=snapshot,
        manifest_path=manifest_path,
    )

    assert shadow.status == production.status == "verified_prerequisite"
    assert shadow.approval_scope == "artifact_identity_only_not_activation_approval"
    assert shadow.candidate == loaded.candidate
    assert shadow.snapshot_file_manifest_sha256 == loaded.snapshot_file_manifest_sha256
    assert shadow.manifest_file_sha256 == hashlib.sha256(manifest_path.read_bytes()).hexdigest()


@pytest.mark.parametrize("mode", ["shadow", "production"])
@pytest.mark.parametrize("manifest_kind", ["none", "missing"])
def test_shadow_and_production_fail_closed_without_manifest(
    mode: str,
    manifest_kind: str,
    tmp_path: Path,
) -> None:
    snapshot = _build_materialized_snapshot(tmp_path)
    manifest_path = None if manifest_kind == "none" else tmp_path / "absent-v2.json"

    with pytest.raises(SchemaEmbeddingArtifactError, match="manifest v2 is required"):
        require_schema_embedding_artifact_gate(
            mode=mode,  # type: ignore[arg-type]
            alias="bge-m3",
            snapshot_dir=snapshot,
            manifest_path=manifest_path,
        )


@pytest.mark.parametrize("mutation", ["missing", "tampered", "unexpected"])
def test_snapshot_verification_rejects_missing_tampered_and_unexpected_files(
    mutation: str,
    tmp_path: Path,
) -> None:
    snapshot, manifest_path = _write_manifest(tmp_path)
    manifest = load_schema_embedding_snapshot_manifest(manifest_path)

    if mutation == "missing":
        (snapshot / "tokenizer.json").unlink()
    elif mutation == "tampered":
        (snapshot / "model.safetensors").write_bytes(b"different-weights")
    else:
        (snapshot / "untracked.txt").write_text("unexpected\n", encoding="utf-8")

    with pytest.raises(SchemaEmbeddingArtifactError):
        verify_schema_embedding_snapshot_manifest(manifest, snapshot, alias="bge-m3")


def test_snapshot_creation_rejects_symlink_escape(tmp_path: Path) -> None:
    snapshot = _build_materialized_snapshot(tmp_path)
    outside = tmp_path / "outside-model.safetensors"
    outside.write_bytes(b"outside")
    os.symlink(outside, snapshot / "escape.safetensors")

    with pytest.raises(SchemaEmbeddingArtifactError, match="symlink escapes"):
        create_schema_embedding_snapshot_manifest(snapshot, alias="bge-m3")


def test_snapshot_creation_accepts_internal_regular_file_symlinks(tmp_path: Path) -> None:
    snapshot = _build_materialized_snapshot(tmp_path)
    os.symlink(snapshot / "config.json", snapshot / "config-alias.json")

    manifest = create_schema_embedding_snapshot_manifest(snapshot, alias="bge-m3")

    alias_file = next(item for item in manifest.files if item.relative_path == "config-alias.json")
    config_file = next(item for item in manifest.files if item.relative_path == "config.json")
    assert alias_file.sha256 == config_file.sha256


def test_hugging_face_blob_symlink_requires_and_respects_trusted_cache_root(
    tmp_path: Path,
) -> None:
    cache_root = tmp_path / "hf-cache"
    model_root = cache_root / "models--BAAI--bge-m3"
    snapshot = model_root / "snapshots" / ("a" * 40)
    blobs = model_root / "blobs"
    snapshot.mkdir(parents=True)
    blobs.mkdir()
    (blobs / "weights").write_bytes(b"cached-weights")
    os.symlink("../../blobs/weights", snapshot / "model.safetensors")
    (snapshot / "tokenizer.json").write_text("{}\n", encoding="utf-8")
    (snapshot / "config.json").write_text("{}\n", encoding="utf-8")

    with pytest.raises(SchemaEmbeddingArtifactError, match="escapes trusted cache root"):
        create_schema_embedding_snapshot_manifest(snapshot, alias="bge-m3")

    manifest = create_schema_embedding_snapshot_manifest(
        snapshot,
        alias="bge-m3",
        trusted_cache_root=cache_root,
    )
    verify_schema_embedding_snapshot_manifest(
        manifest,
        snapshot,
        alias="bge-m3",
        trusted_cache_root=cache_root,
    )

    weight = next(item for item in manifest.files if item.category == "weights")
    assert weight.sha256 == hashlib.sha256(b"cached-weights").hexdigest()


def test_manifest_loader_rejects_symlink_and_internal_digest_tampering(tmp_path: Path) -> None:
    _, manifest_path = _write_manifest(tmp_path)
    manifest_link = tmp_path / "manifest-link.json"
    os.symlink(manifest_path, manifest_link)
    with pytest.raises(SchemaEmbeddingArtifactError, match="non-symlink"):
        load_schema_embedding_snapshot_manifest(manifest_link)

    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    payload["category_digests"]["weights"]["file_manifest_sha256"] = "0" * 64
    manifest_path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(SchemaEmbeddingArtifactError, match="manifest v2 is invalid"):
        load_schema_embedding_snapshot_manifest(manifest_path)


def test_manifest_loader_rejects_oversized_input_before_json_parsing(tmp_path: Path) -> None:
    from finance_agent_core.evaluation import schema_embedding_artifacts as module

    manifest_path = tmp_path / "oversized-manifest.json"
    manifest_path.write_bytes(b"x" * (module._MAX_SNAPSHOT_MANIFEST_BYTES + 1))

    with pytest.raises(SchemaEmbeddingArtifactError, match="safety limit"):
        load_schema_embedding_snapshot_manifest(manifest_path)


def test_manifest_rejects_unsafe_relative_paths() -> None:
    with pytest.raises(ValueError, match="canonical and relative"):
        SchemaEmbeddingSnapshotFile(
            relative_path="../model.safetensors",
            size_bytes=1,
            sha256="a" * 64,
            category="weights",
        )


def test_snapshot_requires_weights_tokenizer_and_config_categories(tmp_path: Path) -> None:
    snapshot = tmp_path / "snapshot"
    snapshot.mkdir()
    (snapshot / "config.json").write_text("{}\n", encoding="utf-8")
    (snapshot / "model.safetensors").write_bytes(b"weights")
    (snapshot / "README.md").write_text("missing tokenizer\n", encoding="utf-8")

    with pytest.raises(SchemaEmbeddingArtifactError, match="does not satisfy"):
        create_schema_embedding_snapshot_manifest(snapshot, alias="bge-m3")


def test_manifest_candidate_must_match_requested_alias(tmp_path: Path) -> None:
    snapshot, manifest_path = _write_manifest(tmp_path, alias="bge-m3")
    manifest = load_schema_embedding_snapshot_manifest(manifest_path)

    with pytest.raises(SchemaEmbeddingArtifactError, match="candidate differs"):
        verify_schema_embedding_snapshot_manifest(manifest, snapshot, alias="kure-v1")


def test_v2_json_schema_tracks_registry_candidates_without_fake_snapshot_hashes() -> None:
    schema_path = (
        Path(__file__).resolve().parents[1]
        / "src"
        / "finance_agent_core"
        / "evaluation"
        / "schema_embedding_snapshot_manifest_v2.schema.json"
    )
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    registry = load_schema_embedding_model_registry()

    supported = schema["x-artifact-contract"]["supported_candidates"]
    assert supported == [
        {
            "alias": alias,
            "model_id": registry.require(alias).model_id,
            "revision": registry.require(alias).revision,
        }
        for alias in ("bge-m3", "kure-v1")
    ]
    assert schema["x-artifact-contract"]["tracked_snapshot_instance"] is False
    assert schema["x-artifact-contract"]["approval_modes_requiring_a_verified_manifest"] == [
        "shadow",
        "production",
    ]
    serialized = schema_path.read_text(encoding="utf-8").casefold()
    assert re.search(r'"[0-9a-f]{64}"', serialized) is None
    assert SchemaEmbeddingSnapshotManifestV2.model_fields["schema_version"].default == "2.0"
