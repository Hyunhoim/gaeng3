from __future__ import annotations

import hashlib
import json
import math
import os
import re
import stat
from collections.abc import Mapping, Sequence
from importlib.resources import files
from pathlib import Path
from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, StrictBool, field_validator, model_validator

from finance_agent_core.config import FieldRegistry, load_field_registry
from finance_agent_core.contracts.queryplan import ProductFamily

_SHA256_PATTERN = r"^[0-9a-f]{64}$"
_FEATURE_FLAG = "FINANCE_DENSE_SCHEMA_LINKER_ENABLED"
_CORPUS_TEMPLATE_VERSION = "schema-field-text-v1"
_MAX_INDEX_ARTIFACT_BYTES = 32 * 1024 * 1024


class SchemaDenseContractError(RuntimeError):
    """Raised when a schema vector artifact is not safe to use."""


class SchemaDenseModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class EmbeddingProviderMetadata(SchemaDenseModel):
    provider_kind: Literal["fake_contract", "frozen_model"]
    provider_id: str = Field(pattern=r"^[a-z0-9][a-z0-9._-]{2,127}$")
    model_id: str = Field(min_length=3, max_length=256)
    model_revision: str = Field(min_length=3, max_length=256)
    license_id: str = Field(min_length=2, max_length=128)
    dimension: int = Field(ge=8, le=65536)
    pooling: str = Field(min_length=2, max_length=64)
    normalization: Literal["l2"] = "l2"
    similarity: Literal["cosine"] = "cosine"

    @field_validator("model_revision")
    @classmethod
    def reject_mutable_revision(cls, value: str) -> str:
        if value.strip().lower() in {"latest", "main", "master", "head"}:
            raise ValueError("embedding model revision must be immutable")
        return value

    @model_validator(mode="after")
    def require_frozen_model_commit_digest(self) -> EmbeddingProviderMetadata:
        if self.provider_kind == "frozen_model" and not re.fullmatch(
            r"(?:[0-9a-f]{40}|[0-9a-f]{64})",
            self.model_revision.casefold(),
        ):
            raise ValueError(
                "frozen embedding model revision must be a 40- or 64-hex commit digest"
            )
        return self


class EmbeddingProvider(Protocol):
    @property
    def metadata(self) -> EmbeddingProviderMetadata: ...

    def embed_documents(self, texts: Sequence[str]) -> list[list[float]]: ...

    def embed_query(self, text: str) -> list[float]: ...


class SchemaFieldEntry(SchemaDenseModel):
    document_id: str = Field(pattern=r"^(?:bond|domestic_etp|overseas_etp|fund):[a-z0-9_]+$")
    product_family: ProductFamily
    field_id: str = Field(pattern=r"^[a-z][a-z0-9_]{1,127}$")
    text: str = Field(min_length=3, max_length=8000)

    @model_validator(mode="after")
    def validate_document_id(self) -> SchemaFieldEntry:
        if self.document_id != f"{self.product_family.value}:{self.field_id}":
            raise ValueError("schema document ID must be product_family:field_id")
        return self


class SchemaVectorRecord(SchemaDenseModel):
    entry: SchemaFieldEntry
    vector: tuple[float, ...] = Field(min_length=8, max_length=65536)

    @field_validator("vector")
    @classmethod
    def validate_finite_vector(cls, value: tuple[float, ...]) -> tuple[float, ...]:
        if any(not math.isfinite(item) for item in value):
            raise ValueError("embedding vector must contain finite values")
        return value


class DenseSchemaIndexManifest(SchemaDenseModel):
    schema_version: Literal["1.0"] = "1.0"
    index_kind: Literal["dense_schema_field"] = "dense_schema_field"
    scope: Literal["offline_evaluation_only", "production_candidate"] = "offline_evaluation_only"
    production_enabled: StrictBool = False
    abstention_policy: Literal["not_calibrated", "calibrated_threshold_v1"] = "not_calibrated"
    activation_status: Literal[
        "blocked_until_real_embedding_and_abstention_calibration",
        "approved_candidate_only",
    ] = "blocked_until_real_embedding_and_abstention_calibration"
    activation_policy_sha256: str | None = Field(default=None, pattern=_SHA256_PATTERN)
    field_registry_schema_version: str = Field(min_length=1, max_length=32)
    field_registry_sha256: str = Field(pattern=_SHA256_PATTERN)
    corpus_template_version: Literal["schema-field-text-v1"] = _CORPUS_TEMPLATE_VERSION
    corpus_sha256: str = Field(pattern=_SHA256_PATTERN)
    field_key_count: int = Field(gt=0)
    field_keys_sha256: str = Field(pattern=_SHA256_PATTERN)
    provider: EmbeddingProviderMetadata
    vector_count: int = Field(gt=0)
    vector_artifact_sha256: str = Field(pattern=_SHA256_PATTERN)

    @model_validator(mode="after")
    def validate_counts(self) -> DenseSchemaIndexManifest:
        if self.vector_count != self.field_key_count:
            raise ValueError("field and vector counts must agree")
        production_profile = (
            self.scope == "production_candidate"
            and self.production_enabled
            and self.abstention_policy == "calibrated_threshold_v1"
            and self.activation_status == "approved_candidate_only"
            and self.activation_policy_sha256 is not None
        )
        offline_profile = (
            self.scope == "offline_evaluation_only"
            and not self.production_enabled
            and self.abstention_policy == "not_calibrated"
            and self.activation_status == "blocked_until_real_embedding_and_abstention_calibration"
            and self.activation_policy_sha256 is None
        )
        if not (production_profile or offline_profile):
            raise ValueError("schema index activation fields form a mixed trust profile")
        if production_profile and self.provider.provider_kind != "frozen_model":
            raise ValueError("production schema index requires a frozen embedding model")
        return self


class SchemaDenseActivationPolicy(SchemaDenseModel):
    """KURE-only abstention policy that must be independently hashed into a Release."""

    schema_version: Literal["1.0"] = "1.0"
    policy_id: Literal["kure-schema-dense-threshold-v1"] = "kure-schema-dense-threshold-v1"
    model_id: Literal["nlpai-lab/KURE-v1"] = "nlpai-lab/KURE-v1"
    model_revision: Literal["d14c8a9423946e268a0c9952fecf3a7aabd73bd9"] = (
        "d14c8a9423946e268a0c9952fecf3a7aabd73bd9"
    )
    snapshot_file_manifest_sha256: Literal[
        "b0b6229e5d2593371b7ac31519da186ccac3fcdfa8fb4e98fa6a430cc92bd597"
    ] = "b0b6229e5d2593371b7ac31519da186ccac3fcdfa8fb4e98fa6a430cc92bd597"
    weights_sha256: Literal["c18156e80caf8ff45eb84a24a853130c3bca03087ccb41b051f86e7556bae02c"] = (
        "c18156e80caf8ff45eb84a24a853130c3bca03087ccb41b051f86e7556bae02c"
    )
    tokenizer_sha256: Literal[
        "fb3c3b93c46fd5a8634e262e1b7de7da11a18b527aa2282b312952b692781dfd"
    ] = "fb3c3b93c46fd5a8634e262e1b7de7da11a18b527aa2282b312952b692781dfd"
    dimension: Literal[1024] = 1024
    pooling: Literal["cls"] = "cls"
    dense_min_score: float = Field(ge=-1, le=1)
    hclx_candidate_min_score: float = Field(ge=-1, le=1)
    minimum_margin: float = Field(ge=0, le=2)
    top_k: int = Field(ge=2, le=10)
    maximum_residual_spans: int = Field(default=1, ge=1, le=4)
    maximum_concurrent_queries: Literal[1] = 1
    queue_timeout_seconds: float = Field(default=1.0, gt=0, le=10)
    calibration_report_sha256: str = Field(pattern=_SHA256_PATTERN)

    @model_validator(mode="after")
    def require_hclx_floor_below_automatic_threshold(self) -> SchemaDenseActivationPolicy:
        if self.hclx_candidate_min_score > self.dense_min_score:
            raise ValueError("HCLX candidate floor cannot exceed the Dense auto-admit score")
        return self

    @property
    def policy_sha256(self) -> str:
        return _canonical_sha256(self.model_dump(mode="json"))


class DenseSchemaIndexArtifact(SchemaDenseModel):
    manifest: DenseSchemaIndexManifest
    records: tuple[SchemaVectorRecord, ...] = Field(min_length=1)


def approve_schema_index_for_production(
    index: DenseSchemaIndex,
    policy: SchemaDenseActivationPolicy,
) -> DenseSchemaIndexArtifact:
    """Promote one already verified KURE index without changing any vector bytes."""

    provider = index.provider
    evidence = getattr(provider, "artifact_gate_evidence", None)
    candidate = getattr(evidence, "candidate", None)
    if (
        getattr(evidence, "mode", None) != "production"
        or getattr(evidence, "snapshot_file_manifest_sha256", None)
        != policy.snapshot_file_manifest_sha256
        or getattr(candidate, "model_id", None) != policy.model_id
        or getattr(candidate, "revision", None) != policy.model_revision
        or provider.metadata.model_id != policy.model_id
        or provider.metadata.model_revision != policy.model_revision
        or provider.metadata.dimension != policy.dimension
        or provider.metadata.pooling != policy.pooling
    ):
        raise SchemaDenseContractError(
            "production schema index requires exact KURE production artifact evidence"
        )
    payload = index.manifest.model_dump(mode="json")
    payload.update(
        {
            "scope": "production_candidate",
            "production_enabled": True,
            "abstention_policy": "calibrated_threshold_v1",
            "activation_status": "approved_candidate_only",
            "activation_policy_sha256": policy.policy_sha256,
        }
    )
    manifest = DenseSchemaIndexManifest.model_validate(payload)
    artifact = DenseSchemaIndexArtifact(manifest=manifest, records=index.artifact.records)
    DenseSchemaIndex(artifact, provider)
    return artifact


def dense_schema_index_file_bytes(artifact: DenseSchemaIndexArtifact) -> bytes:
    """Return the sole canonical on-disk representation of a schema index."""

    detached = DenseSchemaIndexArtifact.model_validate(artifact.model_dump(mode="python"))
    return (
        json.dumps(
            detached.model_dump(mode="json"),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        + "\n"
    ).encode("utf-8")


def load_dense_schema_index_artifact(
    path: str | Path,
    *,
    expected_file_sha256: str,
) -> DenseSchemaIndexArtifact:
    """Load an immutable canonical index behind an explicit SHA-256 trust anchor."""

    if re.fullmatch(_SHA256_PATTERN, expected_file_sha256) is None:
        raise SchemaDenseContractError("schema index trust anchor is invalid")
    target = Path(path)
    if not target.is_absolute():
        raise SchemaDenseContractError("schema index path must be absolute")
    descriptor: int | None = None
    try:
        descriptor = os.open(
            target,
            os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
        )
        before = os.fstat(descriptor)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_nlink != 1
            or before.st_mode & (stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH)
            or before.st_size <= 0
            or before.st_size > _MAX_INDEX_ARTIFACT_BYTES
        ):
            raise SchemaDenseContractError("schema index file is not immutable and regular")
        chunks: list[bytes] = []
        remaining = before.st_size
        while remaining:
            chunk = os.read(descriptor, min(1024 * 1024, remaining))
            if not chunk:
                raise SchemaDenseContractError("schema index changed while loading")
            chunks.append(chunk)
            remaining -= len(chunk)
        if os.read(descriptor, 1):
            raise SchemaDenseContractError("schema index changed while loading")
        after = os.fstat(descriptor)
        current = target.stat(follow_symlinks=False)

        def identity(item: os.stat_result) -> tuple[int, ...]:
            return (
                item.st_dev,
                item.st_ino,
                item.st_mode,
                item.st_uid,
                item.st_nlink,
                item.st_size,
                item.st_mtime_ns,
                item.st_ctime_ns,
            )

        if identity(before) != identity(after) or identity(after) != identity(current):
            raise SchemaDenseContractError("schema index changed while loading")
        data = b"".join(chunks)
    except SchemaDenseContractError:
        raise
    except OSError:
        raise SchemaDenseContractError("schema index file is unavailable") from None
    finally:
        if descriptor is not None:
            os.close(descriptor)
    if hashlib.sha256(data).hexdigest() != expected_file_sha256:
        raise SchemaDenseContractError("schema index differs from its trusted SHA-256")
    try:
        artifact = DenseSchemaIndexArtifact.model_validate_json(data)
    except ValueError:
        raise SchemaDenseContractError("schema index artifact is invalid") from None
    if data != dense_schema_index_file_bytes(artifact):
        raise SchemaDenseContractError("schema index artifact is not canonical JSON")
    return artifact


class SchemaFieldCandidate(SchemaDenseModel):
    product_family: ProductFamily
    field_id: str
    score: float = Field(ge=-1.000001, le=1.000001)
    rank: int = Field(ge=1)


class SchemaLinkResponse(SchemaDenseModel):
    status: Literal["disabled", "found", "not_found"]
    candidates: tuple[SchemaFieldCandidate, ...]

    @model_validator(mode="after")
    def validate_status(self) -> SchemaLinkResponse:
        if (self.status == "found") != bool(self.candidates):
            if self.status != "disabled" or self.candidates:
                raise ValueError("schema link status and candidates must agree")
        return self


class DenseSchemaLinkerSettings(SchemaDenseModel):
    enabled: StrictBool = False

    @classmethod
    def from_environment(
        cls,
        environment: Mapping[str, str] | None = None,
    ) -> DenseSchemaLinkerSettings:
        source = os.environ if environment is None else environment
        raw = source.get(_FEATURE_FLAG)
        if raw is None:
            return cls()
        normalized = raw.strip().lower()
        if normalized in {"0", "false", "no", "off"}:
            return cls(enabled=False)
        if normalized in {"1", "true", "yes", "on"}:
            return cls(enabled=True)
        raise ValueError(f"{_FEATURE_FLAG} must be an explicit boolean")


def _canonical_sha256(payload: object) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def packaged_field_registry_sha256() -> str:
    resource = files("finance_agent_core.config").joinpath("field_registry.yaml")
    return hashlib.sha256(resource.read_bytes()).hexdigest()


def _entry_payload(entries: Sequence[SchemaFieldEntry]) -> list[dict[str, object]]:
    return [entry.model_dump(mode="json") for entry in entries]


def _record_payload(records: Sequence[SchemaVectorRecord]) -> list[dict[str, object]]:
    return [record.model_dump(mode="json") for record in records]


def _normalize_vector(vector: Sequence[float], dimension: int) -> tuple[float, ...]:
    if len(vector) != dimension:
        raise SchemaDenseContractError(
            f"embedding dimension differs: expected {dimension}, got {len(vector)}"
        )
    if any(not math.isfinite(float(item)) for item in vector):
        raise SchemaDenseContractError("embedding contains a non-finite value")
    norm = math.sqrt(sum(float(item) * float(item) for item in vector))
    if norm <= 0:
        raise SchemaDenseContractError("embedding vector cannot be zero")
    return tuple(float(item) / norm for item in vector)


def build_schema_field_entries(
    registry: FieldRegistry | None = None,
) -> list[SchemaFieldEntry]:
    active_registry = registry or load_field_registry()
    entries: list[SchemaFieldEntry] = []
    for family in ProductFamily:
        dataset = active_registry.require_dataset(family.value)
        for field_id, definition in sorted(active_registry.fields.items()):
            if family.value not in definition.datasets:
                continue
            resolved = definition.resolve(family.value)
            if not any(
                (
                    resolved.queryable,
                    resolved.selectable,
                    resolved.sortable,
                    resolved.aggregatable,
                    resolved.comparable,
                )
            ):
                continue
            text_parts = [
                field_id,
                resolved.label,
                *resolved.aliases,
                dataset.source_label,
                resolved.value_type.value,
                resolved.unit,
                *resolved.enum_values,
                resolved.notes,
            ]
            text = " | ".join(part.strip() for part in text_parts if part.strip())
            entries.append(
                SchemaFieldEntry(
                    document_id=f"{family.value}:{field_id}",
                    product_family=family,
                    field_id=field_id,
                    text=text,
                )
            )
    return sorted(entries, key=lambda item: item.document_id)


class DenseSchemaIndex:
    """Offline-only exact cosine index over approved canonical schema fields."""

    def __init__(
        self,
        artifact: DenseSchemaIndexArtifact,
        provider: EmbeddingProvider,
    ) -> None:
        # Revalidate and detach nested containers so a caller cannot mutate a
        # previously validated list through an external reference.
        self.artifact = DenseSchemaIndexArtifact.model_validate(artifact.model_dump(mode="python"))
        self.provider = provider
        self._validate_contract()

    @property
    def manifest(self) -> DenseSchemaIndexManifest:
        return self.artifact.manifest

    @classmethod
    def build(
        cls,
        entries: Sequence[SchemaFieldEntry],
        provider: EmbeddingProvider,
        *,
        registry_schema_version: str | None = None,
        registry_sha256: str | None = None,
    ) -> DenseSchemaIndex:
        ordered = sorted(entries, key=lambda item: item.document_id)
        if not ordered:
            raise SchemaDenseContractError("schema index requires at least one field")
        keys = [entry.document_id for entry in ordered]
        if len(keys) != len(set(keys)):
            raise SchemaDenseContractError("schema field keys must be unique")
        raw_vectors = provider.embed_documents([entry.text for entry in ordered])
        if len(raw_vectors) != len(ordered):
            raise SchemaDenseContractError("embedding provider returned the wrong vector count")
        records = [
            SchemaVectorRecord(
                entry=entry,
                vector=_normalize_vector(vector, provider.metadata.dimension),
            )
            for entry, vector in zip(ordered, raw_vectors, strict=True)
        ]
        active_registry = load_field_registry()
        manifest = DenseSchemaIndexManifest(
            field_registry_schema_version=(
                registry_schema_version or active_registry.schema_version
            ),
            field_registry_sha256=(registry_sha256 or packaged_field_registry_sha256()),
            corpus_sha256=_canonical_sha256(_entry_payload(ordered)),
            field_key_count=len(keys),
            field_keys_sha256=_canonical_sha256(keys),
            provider=provider.metadata,
            vector_count=len(records),
            vector_artifact_sha256=_canonical_sha256(_record_payload(records)),
        )
        return cls(DenseSchemaIndexArtifact(manifest=manifest, records=records), provider)

    @classmethod
    def from_artifact(
        cls,
        payload: DenseSchemaIndexArtifact | dict[str, object],
        provider: EmbeddingProvider,
    ) -> DenseSchemaIndex:
        artifact = (
            payload
            if isinstance(payload, DenseSchemaIndexArtifact)
            else DenseSchemaIndexArtifact.model_validate(payload)
        )
        return cls(artifact, provider)

    def _validate_contract(self) -> None:
        manifest = self.artifact.manifest
        records = self.artifact.records
        entries = [record.entry for record in records]
        keys = [entry.document_id for entry in entries]
        active_registry = load_field_registry()
        canonical_entries = build_schema_field_entries(active_registry)
        checks = {
            "registry schema version": (
                manifest.field_registry_schema_version == active_registry.schema_version
            ),
            "registry SHA-256": (
                manifest.field_registry_sha256 == packaged_field_registry_sha256()
            ),
            "provider metadata": manifest.provider == self.provider.metadata,
            "record count": manifest.vector_count == len(records),
            "unique sorted field keys": keys == sorted(set(keys)),
            "field key SHA-256": manifest.field_keys_sha256 == _canonical_sha256(keys),
            "corpus SHA-256": manifest.corpus_sha256 == _canonical_sha256(_entry_payload(entries)),
            "vector SHA-256": (
                manifest.vector_artifact_sha256 == _canonical_sha256(_record_payload(records))
            ),
            "canonical complete field corpus": (
                _entry_payload(entries) == _entry_payload(canonical_entries)
            ),
        }
        known_fields = active_registry.fields
        checks["registry field membership"] = all(
            entry.field_id in known_fields
            and entry.product_family.value in known_fields[entry.field_id].datasets
            for entry in entries
        )
        checks["vector dimension"] = all(
            len(record.vector) == manifest.provider.dimension for record in records
        )
        checks["unit vector"] = all(
            abs(sum(value * value for value in record.vector) - 1.0) <= 1e-6 for record in records
        )
        failures = [name for name, passed in checks.items() if not passed]
        if failures:
            raise SchemaDenseContractError(
                "schema dense index contract differs: " + ", ".join(failures)
            )

    def search(
        self,
        query: str,
        product_family: ProductFamily,
        *,
        top_k: int = 5,
        allowed_field_ids: frozenset[str] | set[str] | None = None,
    ) -> list[SchemaFieldCandidate]:
        if not query.strip():
            raise ValueError("schema link query cannot be blank")
        if not 1 <= top_k <= 20:
            raise ValueError("schema link top_k must be between 1 and 20")
        query_vector = _normalize_vector(
            self.provider.embed_query(query),
            self.manifest.provider.dimension,
        )
        scored = []
        for record in self.artifact.records:
            if record.entry.product_family is not product_family:
                continue
            if allowed_field_ids is not None and record.entry.field_id not in allowed_field_ids:
                continue
            score = sum(
                query_value * field_value
                for query_value, field_value in zip(query_vector, record.vector, strict=True)
            )
            scored.append((score, record.entry.field_id))
        scored.sort(key=lambda item: (-item[0], item[1]))
        return [
            SchemaFieldCandidate(
                product_family=product_family,
                field_id=field_id,
                score=round(score, 9),
                rank=rank,
            )
            for rank, (score, field_id) in enumerate(scored[:top_k], start=1)
        ]


class FeatureGatedDenseSchemaLinker:
    """Production-facing gate; v1 artifacts are deliberately offline-only."""

    def __init__(
        self,
        index: DenseSchemaIndex,
        settings: DenseSchemaLinkerSettings | None = None,
    ) -> None:
        self.index = index
        self.settings = settings or DenseSchemaLinkerSettings.from_environment()

    def link(
        self,
        query: str,
        product_family: ProductFamily,
        *,
        top_k: int = 5,
    ) -> SchemaLinkResponse:
        if not self.settings.enabled:
            return SchemaLinkResponse(status="disabled", candidates=())
        if not self.index.manifest.production_enabled:
            raise SchemaDenseContractError(
                "offline-only schema index cannot be enabled in the production path"
            )
        candidates = self.index.search(query, product_family, top_k=top_k)
        return SchemaLinkResponse(
            status="found" if candidates else "not_found",
            candidates=candidates,
        )


__all__ = [
    "DenseSchemaIndex",
    "DenseSchemaIndexArtifact",
    "DenseSchemaIndexManifest",
    "DenseSchemaLinkerSettings",
    "EmbeddingProvider",
    "EmbeddingProviderMetadata",
    "FeatureGatedDenseSchemaLinker",
    "SchemaDenseContractError",
    "SchemaDenseActivationPolicy",
    "SchemaFieldCandidate",
    "SchemaFieldEntry",
    "SchemaLinkResponse",
    "SchemaVectorRecord",
    "build_schema_field_entries",
    "approve_schema_index_for_production",
    "dense_schema_index_file_bytes",
    "load_dense_schema_index_artifact",
    "packaged_field_registry_sha256",
]
