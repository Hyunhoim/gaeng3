from __future__ import annotations

import argparse
import hashlib
import json
import os
import stat
import time
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from finance_agent_core.evaluation.schema_embedding_models import (
    SchemaEmbeddingModelSpec,
    SentenceTransformerCpuProvider,
    load_schema_embedding_model_registry,
)
from finance_agent_core.retrieval.schema_dense import EmbeddingProviderMetadata

_SHA256_PATTERN = r"^[0-9a-f]{64}$"
_REVISION_PATTERN = r"^[0-9a-f]{40}$"
_SUPPORTED_ALIASES = ("bge-m3", "kure-v1")
_CATEGORIES = ("weights", "tokenizer", "config", "other")
_REQUIRED_CATEGORIES = frozenset(("weights", "tokenizer", "config"))
_HASH_CHUNK_SIZE = 1024 * 1024
_MAX_SNAPSHOT_MANIFEST_BYTES = 2 * 1024 * 1024
DEFAULT_SCHEMA_EMBEDDING_MANIFEST_DIRECTORY = Path(
    "artifacts/evaluation/schema-embedding/snapshot-manifests"
)

ArtifactCategory = Literal["weights", "tokenizer", "config", "other"]
ApprovalMode = Literal["shadow", "production"]


class SchemaEmbeddingArtifactError(RuntimeError):
    """Raised when a local embedding snapshot cannot be trusted."""


class SchemaEmbeddingArtifactModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class SchemaEmbeddingCandidateLink(SchemaEmbeddingArtifactModel):
    alias: Literal["bge-m3", "kure-v1"]
    model_id: str = Field(min_length=3, max_length=256)
    revision: str = Field(pattern=_REVISION_PATTERN)
    linkage_key: str = Field(pattern=_SHA256_PATTERN)

    @model_validator(mode="after")
    def validate_linkage_key(self) -> SchemaEmbeddingCandidateLink:
        expected = schema_embedding_linkage_key(self.model_id, self.revision)
        if self.linkage_key != expected:
            raise ValueError("schema embedding candidate linkage key differs")
        return self


class SchemaEmbeddingSnapshotFile(SchemaEmbeddingArtifactModel):
    relative_path: str = Field(min_length=1, max_length=4096)
    size_bytes: int = Field(ge=0)
    sha256: str = Field(pattern=_SHA256_PATTERN)
    category: ArtifactCategory

    @field_validator("relative_path")
    @classmethod
    def require_canonical_relative_path(cls, value: str) -> str:
        if "\\" in value or "\x00" in value:
            raise ValueError("snapshot file path must use safe POSIX separators")
        parts = value.split("/")
        path = PurePosixPath(value)
        if (
            path.is_absolute()
            or value.startswith("/")
            or value.endswith("/")
            or any(part in {"", ".", ".."} for part in parts)
            or path.as_posix() != value
        ):
            raise ValueError("snapshot file path must be canonical and relative")
        return value


class SchemaEmbeddingCategoryDigest(SchemaEmbeddingArtifactModel):
    file_count: int = Field(ge=0)
    total_size_bytes: int = Field(ge=0)
    file_manifest_sha256: str = Field(pattern=_SHA256_PATTERN)


class SchemaEmbeddingSnapshotManifestV2(SchemaEmbeddingArtifactModel):
    schema_version: Literal["2.0"] = "2.0"
    manifest_kind: Literal["schema_embedding_snapshot_artifact"] = (
        "schema_embedding_snapshot_artifact"
    )
    candidate: SchemaEmbeddingCandidateLink
    files: tuple[SchemaEmbeddingSnapshotFile, ...] = Field(min_length=3)
    file_count: int = Field(ge=3)
    total_size_bytes: int = Field(ge=0)
    category_digests: dict[str, SchemaEmbeddingCategoryDigest]
    snapshot_file_manifest_sha256: str = Field(pattern=_SHA256_PATTERN)

    @model_validator(mode="after")
    def validate_canonical_manifest(self) -> SchemaEmbeddingSnapshotManifestV2:
        paths = [item.relative_path for item in self.files]
        if paths != sorted(paths) or len(paths) != len(set(paths)):
            raise ValueError("snapshot files must have unique, sorted relative paths")
        if self.file_count != len(self.files):
            raise ValueError("snapshot file_count differs")
        if self.total_size_bytes != sum(item.size_bytes for item in self.files):
            raise ValueError("snapshot total_size_bytes differs")
        if set(self.category_digests) != set(_CATEGORIES):
            raise ValueError("snapshot category digest keys differ")

        for category in _CATEGORIES:
            expected = _category_digest(self.files, category)
            if self.category_digests[category] != expected:
                raise ValueError(f"snapshot {category} category digest differs")
            if category in _REQUIRED_CATEGORIES and expected.file_count == 0:
                raise ValueError(f"snapshot requires at least one {category} file")

        expected_snapshot_sha256 = _file_manifest_sha256(self.files)
        if self.snapshot_file_manifest_sha256 != expected_snapshot_sha256:
            raise ValueError("snapshot file manifest SHA-256 differs")
        return self


class SchemaEmbeddingArtifactGateEvidence(SchemaEmbeddingArtifactModel):
    mode: ApprovalMode
    status: Literal["verified_prerequisite"] = "verified_prerequisite"
    approval_scope: Literal["artifact_identity_only_not_activation_approval"] = (
        "artifact_identity_only_not_activation_approval"
    )
    candidate: SchemaEmbeddingCandidateLink
    snapshot_file_manifest_sha256: str = Field(pattern=_SHA256_PATTERN)
    manifest_file_sha256: str = Field(pattern=_SHA256_PATTERN)


class VerifiedSentenceTransformerCpuProvider(SentenceTransformerCpuProvider):
    """CPU provider that can load only an already verified local snapshot."""

    def __init__(
        self,
        spec: SchemaEmbeddingModelSpec,
        *,
        model_source_path: Path,
        artifact_gate_evidence: SchemaEmbeddingArtifactGateEvidence,
        batch_size: int = 16,
        cpu_threads: int = 12,
        cache_dir: Path | None = None,
    ) -> None:
        if not 1 <= batch_size <= 256:
            raise ValueError("batch_size must be between 1 and 256")
        if not 1 <= cpu_threads <= 256:
            raise ValueError("cpu_threads must be between 1 and 256")
        if spec.trust_remote_code:
            raise ValueError("verified snapshot provider forbids remote model code")
        candidate = artifact_gate_evidence.candidate
        if (
            candidate.alias != spec.alias
            or candidate.model_id != spec.model_id
            or candidate.revision != spec.revision
        ):
            raise ValueError("artifact gate evidence differs from the embedding model spec")

        try:
            import torch
            from sentence_transformers import SentenceTransformer
        except ImportError as exc:  # pragma: no cover - optional evaluation environment
            raise RuntimeError(
                "CPU embedding evaluation dependencies are absent; install "
                "requirements/embedding-eval.txt in gaeng3-embedding-eval"
            ) from exc

        source = model_source_path.resolve(strict=True)
        if not source.is_dir():
            raise ValueError("verified embedding model source must be a directory")
        torch.set_num_threads(cpu_threads)
        self.spec = spec
        self.batch_size = batch_size
        self.cpu_threads = cpu_threads
        self.document_calls = 0
        self.document_text_count = 0
        self.query_calls = 0
        self.query_text_count = 0
        load_started = time.perf_counter()
        self._model = SentenceTransformer(
            str(source),
            revision=None,
            device="cpu",
            trust_remote_code=False,
            cache_folder=str(cache_dir) if cache_dir else None,
            local_files_only=True,
            model_kwargs=None,
            config_kwargs=None,
        )
        self.model_load_ms = (time.perf_counter() - load_started) * 1000
        self._model.max_seq_length = spec.max_sequence_length
        dimension_getter = getattr(self._model, "get_embedding_dimension", None)
        if dimension_getter is None:
            dimension_getter = self._model.get_sentence_embedding_dimension
        actual_dimension = dimension_getter()
        if actual_dimension != spec.dimension:
            raise RuntimeError(
                f"embedding dimension differs for {spec.alias}: "
                f"expected {spec.dimension}, got {actual_dimension}"
            )
        if str(self._model.device) != "cpu":
            raise RuntimeError(f"CPU evaluation loaded an unexpected device: {self._model.device}")
        self._metadata = EmbeddingProviderMetadata(
            provider_kind="frozen_model",
            provider_id=f"verified_sentence_transformers_{spec.alias.replace('-', '_')}",
            model_id=spec.model_id,
            model_revision=spec.revision,
            license_id=spec.license_id,
            dimension=spec.dimension,
            pooling=spec.pooling,
        )
        self._artifact_gate_evidence = artifact_gate_evidence

    @property
    def artifact_gate_evidence(self) -> SchemaEmbeddingArtifactGateEvidence:
        return self._artifact_gate_evidence


@dataclass(frozen=True)
class _SnapshotFileSource:
    relative_path: str
    read_path: Path
    symlink_path: Path | None = None
    symlink_target: Path | None = None
    symlink_identity: tuple[int, int, int, int] | None = None


def _canonical_sha256(payload: object) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def schema_embedding_linkage_key(model_id: str, revision: str) -> str:
    """Bind a model repository to one immutable revision without changing registry v1."""

    return hashlib.sha256(f"{model_id}\n{revision}".encode()).hexdigest()


def load_schema_embedding_candidate_link(alias: str) -> SchemaEmbeddingCandidateLink:
    if alias not in _SUPPORTED_ALIASES:
        raise SchemaEmbeddingArtifactError(
            f"snapshot artifact v2 only supports: {', '.join(_SUPPORTED_ALIASES)}"
        )
    spec = load_schema_embedding_model_registry().require(alias)
    return SchemaEmbeddingCandidateLink(
        alias=alias,
        model_id=spec.model_id,
        revision=spec.revision,
        linkage_key=schema_embedding_linkage_key(spec.model_id, spec.revision),
    )


def default_schema_embedding_snapshot_manifest_path(alias: str) -> Path:
    candidate = load_schema_embedding_candidate_link(alias)
    return DEFAULT_SCHEMA_EMBEDDING_MANIFEST_DIRECTORY / (
        f"{candidate.alias}-{candidate.revision}-snapshot-v2.json"
    )


def _classify_snapshot_file(relative_path: str) -> ArtifactCategory:
    lowered = relative_path.casefold()
    name = PurePosixPath(lowered).name
    suffix = PurePosixPath(lowered).suffix

    weight_markers = (
        ".safetensors",
        "pytorch_model",
        "model.bin",
        "model.pt",
        "model.pth",
        "model.ckpt",
        "model.onnx",
        "tf_model.h5",
        "flax_model.msgpack",
    )
    if any(marker in name for marker in weight_markers):
        return "weights"

    tokenizer_markers = (
        "tokenizer",
        "tokenizer_config",
        "special_tokens_map",
        "added_tokens",
        "vocab",
        "merges",
        "sentencepiece",
        "spiece",
    )
    if any(marker in lowered for marker in tokenizer_markers) or name in {
        "sentencepiece.bpe.model",
        "tokenizer.model",
    }:
        return "tokenizer"

    if "config" in name or name == "modules.json" or suffix in {".json", ".yaml", ".yml"}:
        return "config"
    return "other"


def _file_payload(item: SchemaEmbeddingSnapshotFile) -> dict[str, object]:
    return item.model_dump(mode="json")


def _file_manifest_sha256(files: Iterable[SchemaEmbeddingSnapshotFile]) -> str:
    return _canonical_sha256([_file_payload(item) for item in files])


def _category_digest(
    files: Iterable[SchemaEmbeddingSnapshotFile],
    category: str,
) -> SchemaEmbeddingCategoryDigest:
    selected = [item for item in files if item.category == category]
    return SchemaEmbeddingCategoryDigest(
        file_count=len(selected),
        total_size_bytes=sum(item.size_bytes for item in selected),
        file_manifest_sha256=_file_manifest_sha256(selected),
    )


def _require_safe_directory(path: Path, *, label: str) -> Path:
    try:
        root_lstat = path.lstat()
    except FileNotFoundError as exc:
        raise SchemaEmbeddingArtifactError(f"{label} directory is missing") from exc
    if stat.S_ISLNK(root_lstat.st_mode):
        raise SchemaEmbeddingArtifactError(f"{label} root cannot be a symlink")
    if not stat.S_ISDIR(root_lstat.st_mode):
        raise SchemaEmbeddingArtifactError(f"{label} root must be a directory")
    return path.resolve(strict=True)


def _snapshot_and_trusted_roots(
    snapshot_dir: Path,
    trusted_cache_root: Path | None,
) -> tuple[Path, Path]:
    snapshot_root = _require_safe_directory(snapshot_dir, label="schema embedding snapshot")
    trusted_root = (
        snapshot_root
        if trusted_cache_root is None
        else _require_safe_directory(trusted_cache_root, label="trusted embedding cache")
    )
    try:
        snapshot_root.relative_to(trusted_root)
    except ValueError as exc:
        raise SchemaEmbeddingArtifactError(
            "schema embedding snapshot must be inside the trusted cache root"
        ) from exc
    return snapshot_root, trusted_root


def _lstat_identity(metadata: os.stat_result) -> tuple[int, int, int, int]:
    return (metadata.st_dev, metadata.st_ino, metadata.st_size, metadata.st_mtime_ns)


def _require_path_within(path: Path, root: Path, *, message: str) -> None:
    try:
        path.relative_to(root)
    except ValueError as exc:
        raise SchemaEmbeddingArtifactError(message) from exc


def _walk_snapshot_files(
    snapshot_dir: Path,
    trusted_cache_root: Path | None,
) -> tuple[Path, list[_SnapshotFileSource]]:
    root, trusted_root = _snapshot_and_trusted_roots(snapshot_dir, trusted_cache_root)
    files: list[_SnapshotFileSource] = []
    pending = [root]
    while pending:
        directory = pending.pop()
        with os.scandir(directory) as entries:
            for entry in entries:
                path = Path(entry.path)
                relative_path = path.relative_to(root).as_posix()
                if entry.is_symlink():
                    try:
                        resolved = path.resolve(strict=True)
                    except (FileNotFoundError, RuntimeError) as exc:
                        raise SchemaEmbeddingArtifactError(
                            f"snapshot contains a broken or cyclic symlink: {relative_path}"
                        ) from exc
                    _require_path_within(
                        resolved,
                        trusted_root,
                        message=f"snapshot symlink escapes trusted cache root: {relative_path}",
                    )
                    target_metadata = resolved.stat()
                    if not stat.S_ISREG(target_metadata.st_mode):
                        raise SchemaEmbeddingArtifactError(
                            f"snapshot symlink target is not a regular file: {relative_path}"
                        )
                    symlink_metadata = path.lstat()
                    files.append(
                        _SnapshotFileSource(
                            relative_path=relative_path,
                            read_path=resolved,
                            symlink_path=path,
                            symlink_target=resolved,
                            symlink_identity=_lstat_identity(symlink_metadata),
                        )
                    )
                if entry.is_dir(follow_symlinks=False):
                    pending.append(path)
                elif entry.is_file(follow_symlinks=False):
                    files.append(
                        _SnapshotFileSource(
                            relative_path=relative_path,
                            read_path=path,
                        )
                    )
                elif entry.is_symlink():
                    continue
                else:
                    raise SchemaEmbeddingArtifactError(
                        f"snapshot contains an unsupported filesystem entry: {relative_path}"
                    )
    return trusted_root, sorted(files, key=lambda item: item.relative_path)


def _hash_regular_file(
    source: _SnapshotFileSource,
    trusted_root: Path,
) -> tuple[int, str]:
    if source.symlink_path is not None:
        try:
            symlink_metadata = source.symlink_path.lstat()
            resolved = source.symlink_path.resolve(strict=True)
        except (FileNotFoundError, RuntimeError) as exc:
            raise SchemaEmbeddingArtifactError(
                f"snapshot symlink changed before hashing: {source.relative_path}"
            ) from exc
        _require_path_within(
            resolved,
            trusted_root,
            message=f"snapshot symlink escapes trusted cache root: {source.relative_path}",
        )
        if (
            not stat.S_ISLNK(symlink_metadata.st_mode)
            or _lstat_identity(symlink_metadata) != source.symlink_identity
            or resolved != source.symlink_target
        ):
            raise SchemaEmbeddingArtifactError(
                f"snapshot symlink changed before hashing: {source.relative_path}"
            )

    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(source.read_path, flags)
    except OSError as exc:
        raise SchemaEmbeddingArtifactError(
            f"cannot safely open snapshot file: {source.relative_path}"
        ) from exc
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode):
            raise SchemaEmbeddingArtifactError(
                f"snapshot entry is not a regular file: {source.relative_path}"
            )
        digest = hashlib.sha256()
        with os.fdopen(descriptor, "rb", closefd=False) as stream:
            while chunk := stream.read(_HASH_CHUNK_SIZE):
                digest.update(chunk)
        after = os.fstat(descriptor)
        identity_before = (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
        identity_after = (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
        if identity_before != identity_after:
            raise SchemaEmbeddingArtifactError(
                f"snapshot file changed while hashing: {source.relative_path}"
            )
        if source.symlink_path is not None:
            try:
                symlink_metadata = source.symlink_path.lstat()
                resolved = source.symlink_path.resolve(strict=True)
            except (FileNotFoundError, RuntimeError) as exc:
                raise SchemaEmbeddingArtifactError(
                    f"snapshot symlink changed while hashing: {source.relative_path}"
                ) from exc
            if (
                _lstat_identity(symlink_metadata) != source.symlink_identity
                or resolved != source.symlink_target
            ):
                raise SchemaEmbeddingArtifactError(
                    f"snapshot symlink changed while hashing: {source.relative_path}"
                )
        return after.st_size, digest.hexdigest()
    finally:
        os.close(descriptor)


def _scan_snapshot(
    snapshot_dir: Path,
    trusted_cache_root: Path | None,
) -> tuple[SchemaEmbeddingSnapshotFile, ...]:
    scanned = []
    trusted_root, sources = _walk_snapshot_files(snapshot_dir, trusted_cache_root)
    for source in sources:
        size_bytes, sha256 = _hash_regular_file(source, trusted_root)
        scanned.append(
            SchemaEmbeddingSnapshotFile(
                relative_path=source.relative_path,
                size_bytes=size_bytes,
                sha256=sha256,
                category=_classify_snapshot_file(source.relative_path),
            )
        )
    return tuple(scanned)


def create_schema_embedding_snapshot_manifest(
    snapshot_dir: Path,
    *,
    alias: str,
    trusted_cache_root: Path | None = None,
) -> SchemaEmbeddingSnapshotManifestV2:
    candidate = load_schema_embedding_candidate_link(alias)
    files = _scan_snapshot(snapshot_dir, trusted_cache_root)
    category_digests = {category: _category_digest(files, category) for category in _CATEGORIES}
    try:
        return SchemaEmbeddingSnapshotManifestV2(
            candidate=candidate,
            files=files,
            file_count=len(files),
            total_size_bytes=sum(item.size_bytes for item in files),
            category_digests=category_digests,
            snapshot_file_manifest_sha256=_file_manifest_sha256(files),
        )
    except ValueError as exc:
        raise SchemaEmbeddingArtifactError(
            "schema embedding snapshot does not satisfy artifact manifest v2"
        ) from exc


def write_schema_embedding_snapshot_manifest(
    manifest: SchemaEmbeddingSnapshotManifestV2,
    output_path: Path,
) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with output_path.open("x", encoding="utf-8") as stream:
            stream.write(f"{manifest.model_dump_json(indent=2)}\n")
    except FileExistsError as exc:
        raise SchemaEmbeddingArtifactError("snapshot manifest already exists") from exc


def _read_non_symlink_regular_file(
    path: Path,
    *,
    label: str,
    maximum_bytes: int = _MAX_SNAPSHOT_MANIFEST_BYTES,
) -> bytes:
    if maximum_bytes < 1:
        raise ValueError("maximum_bytes must be positive")
    try:
        path_metadata = path.lstat()
    except FileNotFoundError as exc:
        raise SchemaEmbeddingArtifactError(
            "snapshot manifest v2 is required before shadow or production approval"
        ) from exc
    if stat.S_ISLNK(path_metadata.st_mode) or not stat.S_ISREG(path_metadata.st_mode):
        raise SchemaEmbeddingArtifactError(f"{label} must be a regular non-symlink file")

    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise SchemaEmbeddingArtifactError(f"cannot safely open {label}") from exc
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode):
            raise SchemaEmbeddingArtifactError(f"{label} must remain a regular file")
        if before.st_size > maximum_bytes:
            raise SchemaEmbeddingArtifactError(
                f"{label} exceeds the {maximum_bytes}-byte safety limit"
            )
        chunks = []
        observed_bytes = 0
        with os.fdopen(descriptor, "rb", closefd=False) as stream:
            while chunk := stream.read(_HASH_CHUNK_SIZE):
                observed_bytes += len(chunk)
                if observed_bytes > maximum_bytes:
                    raise SchemaEmbeddingArtifactError(
                        f"{label} exceeds the {maximum_bytes}-byte safety limit"
                    )
                chunks.append(chunk)
        after = os.fstat(descriptor)
        if _lstat_identity(before) != _lstat_identity(after):
            raise SchemaEmbeddingArtifactError(f"{label} changed while reading")
        final_path_metadata = path.lstat()
        if stat.S_ISLNK(final_path_metadata.st_mode) or _lstat_identity(
            final_path_metadata
        ) != _lstat_identity(path_metadata):
            raise SchemaEmbeddingArtifactError(f"{label} changed while reading")
        return b"".join(chunks)
    finally:
        os.close(descriptor)


def _load_schema_embedding_snapshot_manifest_with_digest(
    manifest_path: Path,
) -> tuple[SchemaEmbeddingSnapshotManifestV2, str]:
    raw = _read_non_symlink_regular_file(manifest_path, label="snapshot manifest")
    try:
        manifest = SchemaEmbeddingSnapshotManifestV2.model_validate_json(raw)
    except ValueError as exc:
        raise SchemaEmbeddingArtifactError("snapshot manifest v2 is invalid") from exc
    return manifest, hashlib.sha256(raw).hexdigest()


def load_schema_embedding_snapshot_manifest(
    manifest_path: Path,
) -> SchemaEmbeddingSnapshotManifestV2:
    manifest, _ = _load_schema_embedding_snapshot_manifest_with_digest(manifest_path)
    return manifest


def verify_schema_embedding_snapshot_manifest(
    manifest: SchemaEmbeddingSnapshotManifestV2,
    snapshot_dir: Path,
    *,
    alias: str,
    trusted_cache_root: Path | None = None,
) -> None:
    expected_candidate = load_schema_embedding_candidate_link(alias)
    if manifest.candidate != expected_candidate:
        raise SchemaEmbeddingArtifactError(
            "snapshot manifest candidate differs from the model ID and revision lock"
        )
    observed_files = _scan_snapshot(snapshot_dir, trusted_cache_root)
    observed_paths = {item.relative_path for item in observed_files}
    expected_paths = {item.relative_path for item in manifest.files}
    missing = sorted(expected_paths - observed_paths)
    unexpected = sorted(observed_paths - expected_paths)
    if missing:
        raise SchemaEmbeddingArtifactError(f"snapshot files are missing: {missing[:5]}")
    if unexpected:
        raise SchemaEmbeddingArtifactError(f"snapshot has unexpected files: {unexpected[:5]}")
    if observed_files != manifest.files:
        raise SchemaEmbeddingArtifactError("snapshot file size, category, or SHA-256 differs")


def require_schema_embedding_artifact_gate(
    *,
    mode: ApprovalMode,
    alias: str,
    snapshot_dir: Path,
    manifest_path: Path | None,
    trusted_cache_root: Path | None = None,
) -> SchemaEmbeddingArtifactGateEvidence:
    if mode not in {"shadow", "production"}:
        raise SchemaEmbeddingArtifactError("artifact gate mode must be shadow or production")
    if manifest_path is None:
        raise SchemaEmbeddingArtifactError(
            "snapshot manifest v2 is required before shadow or production approval"
        )
    manifest, manifest_file_sha256 = _load_schema_embedding_snapshot_manifest_with_digest(
        manifest_path
    )
    verify_schema_embedding_snapshot_manifest(
        manifest,
        snapshot_dir,
        alias=alias,
        trusted_cache_root=trusted_cache_root,
    )
    return SchemaEmbeddingArtifactGateEvidence(
        mode=mode,
        candidate=manifest.candidate,
        snapshot_file_manifest_sha256=manifest.snapshot_file_manifest_sha256,
        manifest_file_sha256=manifest_file_sha256,
    )


def load_verified_schema_embedding_cpu_provider(
    *,
    alias: str,
    snapshot_dir: Path,
    manifest_path: Path,
    trusted_cache_root: Path,
    batch_size: int = 16,
    cpu_threads: int = 12,
) -> VerifiedSentenceTransformerCpuProvider:
    """Load one exact local snapshot and reverify it after model construction.

    The deployment must mount ``trusted_cache_root`` read-only.  This double
    gate closes ordinary path substitution before/after load; the read-only
    mount is the external trust boundary against concurrent replacement.
    """

    before = require_schema_embedding_artifact_gate(
        mode="shadow",
        alias=alias,
        snapshot_dir=snapshot_dir,
        manifest_path=manifest_path,
        trusted_cache_root=trusted_cache_root,
    )
    spec = load_schema_embedding_model_registry().require(alias)
    provider = VerifiedSentenceTransformerCpuProvider(
        spec,
        model_source_path=snapshot_dir,
        artifact_gate_evidence=before,
        batch_size=batch_size,
        cpu_threads=cpu_threads,
        cache_dir=trusted_cache_root,
    )
    after = require_schema_embedding_artifact_gate(
        mode="shadow",
        alias=alias,
        snapshot_dir=snapshot_dir,
        manifest_path=manifest_path,
        trusted_cache_root=trusted_cache_root,
    )
    if after != before:
        raise SchemaEmbeddingArtifactError(
            "schema embedding snapshot changed across provider construction"
        )
    return provider


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Create or verify a fail-closed Schema Dense snapshot manifest v2."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    for command in ("create", "verify", "gate"):
        subparser = subparsers.add_parser(command)
        subparser.add_argument("--model", choices=_SUPPORTED_ALIASES, required=True)
        subparser.add_argument("--snapshot-dir", type=Path, required=True)
        subparser.add_argument("--trusted-cache-root", type=Path)
        subparser.add_argument("--manifest", type=Path, required=True)
        if command == "gate":
            subparser.add_argument("--mode", choices=("shadow", "production"), required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = _build_parser().parse_args(argv)
    if arguments.command == "create":
        manifest = create_schema_embedding_snapshot_manifest(
            arguments.snapshot_dir,
            alias=arguments.model,
            trusted_cache_root=arguments.trusted_cache_root,
        )
        snapshot_root = _require_safe_directory(
            arguments.snapshot_dir,
            label="schema embedding snapshot",
        )
        manifest_target = arguments.manifest.resolve(strict=False)
        try:
            manifest_target.relative_to(snapshot_root)
        except ValueError:
            pass
        else:
            raise SchemaEmbeddingArtifactError(
                "snapshot manifest must be stored outside the immutable snapshot directory"
            )
        write_schema_embedding_snapshot_manifest(manifest, arguments.manifest)
        print(manifest.model_dump_json(indent=2))
        return 0

    manifest = load_schema_embedding_snapshot_manifest(arguments.manifest)
    if arguments.command == "verify":
        verify_schema_embedding_snapshot_manifest(
            manifest,
            arguments.snapshot_dir,
            alias=arguments.model,
            trusted_cache_root=arguments.trusted_cache_root,
        )
        print(
            json.dumps(
                {
                    "status": "verified",
                    "candidate": manifest.candidate.model_dump(mode="json"),
                    "snapshot_file_manifest_sha256": (manifest.snapshot_file_manifest_sha256),
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0

    evidence = require_schema_embedding_artifact_gate(
        mode=arguments.mode,
        alias=arguments.model,
        snapshot_dir=arguments.snapshot_dir,
        manifest_path=arguments.manifest,
        trusted_cache_root=arguments.trusted_cache_root,
    )
    print(evidence.model_dump_json(indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "DEFAULT_SCHEMA_EMBEDDING_MANIFEST_DIRECTORY",
    "SchemaEmbeddingArtifactError",
    "SchemaEmbeddingArtifactGateEvidence",
    "SchemaEmbeddingCandidateLink",
    "SchemaEmbeddingCategoryDigest",
    "SchemaEmbeddingSnapshotFile",
    "SchemaEmbeddingSnapshotManifestV2",
    "VerifiedSentenceTransformerCpuProvider",
    "create_schema_embedding_snapshot_manifest",
    "default_schema_embedding_snapshot_manifest_path",
    "load_schema_embedding_candidate_link",
    "load_verified_schema_embedding_cpu_provider",
    "load_schema_embedding_snapshot_manifest",
    "require_schema_embedding_artifact_gate",
    "schema_embedding_linkage_key",
    "verify_schema_embedding_snapshot_manifest",
    "write_schema_embedding_snapshot_manifest",
]
