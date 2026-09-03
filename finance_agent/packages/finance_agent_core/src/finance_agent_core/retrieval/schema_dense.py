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
from typing import Literal, Protocol, TypedDict

from pydantic import BaseModel, ConfigDict, Field, StrictBool, field_validator, model_validator

from finance_agent_core.config import FieldDefinition, FieldRegistry, load_field_registry
from finance_agent_core.contracts.queryplan import ProductFamily

_SHA256_PATTERN = r"^[0-9a-f]{64}$"
_FEATURE_FLAG = "FINANCE_DENSE_SCHEMA_LINKER_ENABLED"
_CORPUS_TEMPLATE_VERSION = "schema-field-text-v1"
_CORPUS_TEMPLATE_VERSION_V2 = "schema-field-text-v2"
_CORPUS_TEMPLATE_VERSION_V3 = "schema-field-prototype-v3"
_MAX_INDEX_ARTIFACT_BYTES = 32 * 1024 * 1024


class SchemaFieldSemanticContext(TypedDict):
    field_id: str
    label: str
    product_family: str
    dataset_label: str
    definition: str
    unit: str
    unit_gloss: str
    supported_operations: tuple[str, ...]
    aliases: tuple[str, ...]
    user_expressions: tuple[str, ...]
    contrasts: tuple[str, ...]
    counterexamples: tuple[str, ...]
    registry_notes: str
    enum_values: tuple[str, ...]


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
    embedding_prototypes: tuple[str, ...] = Field(default=(), max_length=32)

    @model_validator(mode="after")
    def validate_document_id(self) -> SchemaFieldEntry:
        if self.document_id != f"{self.product_family.value}:{self.field_id}":
            raise ValueError("schema document ID must be product_family:field_id")
        if len(self.embedding_prototypes) != len(set(self.embedding_prototypes)):
            raise ValueError("schema embedding prototypes must be unique")
        if any(not 3 <= len(item) <= 1000 for item in self.embedding_prototypes):
            raise ValueError("schema embedding prototype length differs")
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
    corpus_template_version: Literal[
        "schema-field-text-v1",
        "schema-field-text-v2",
        "schema-field-prototype-v3",
    ] = _CORPUS_TEMPLATE_VERSION
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


class SchemaDenseFieldThreshold(SchemaDenseModel):
    """Release-bound selective prediction policy for one family/field/operation."""

    product_family: ProductFamily
    field_id: str = Field(pattern=r"^[a-z][a-z0-9_]{1,127}$")
    operation: Literal["rank", "project", "filter", "aggregate"]
    semantic_group: str = Field(pattern=r"^[a-z][a-z0-9_]{1,63}$")
    dense_min_score: float = Field(ge=-1, le=1)
    hclx_candidate_min_score: float = Field(ge=-1, le=1)
    minimum_margin: float = Field(ge=0, le=2)
    auto_admit: StrictBool

    @model_validator(mode="after")
    def require_candidate_floor_below_dense_threshold(self) -> SchemaDenseFieldThreshold:
        if self.hclx_candidate_min_score > self.dense_min_score:
            raise ValueError("field HCLX candidate floor cannot exceed Dense threshold")
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
    field_thresholds: tuple[SchemaDenseFieldThreshold, ...] = Field(
        default=(),
        max_length=128,
    )

    @model_validator(mode="after")
    def require_hclx_floor_below_automatic_threshold(self) -> SchemaDenseActivationPolicy:
        if self.hclx_candidate_min_score > self.dense_min_score:
            raise ValueError("HCLX candidate floor cannot exceed the Dense auto-admit score")
        keys = [
            (item.product_family, item.field_id, item.operation) for item in self.field_thresholds
        ]
        if len(keys) != len(set(keys)):
            raise ValueError("Schema Dense field thresholds must be unique")
        return self

    def threshold_for(
        self,
        *,
        product_family: ProductFamily,
        field_id: str,
        operation: Literal["rank", "project", "filter", "aggregate"],
    ) -> SchemaDenseFieldThreshold:
        for item in self.field_thresholds:
            if (
                item.product_family is product_family
                and item.field_id == field_id
                and item.operation == operation
            ):
                return item
        return SchemaDenseFieldThreshold(
            product_family=product_family,
            field_id=field_id,
            operation=operation,
            semantic_group="global_fallback",
            dense_min_score=self.dense_min_score,
            hclx_candidate_min_score=self.hclx_candidate_min_score,
            minimum_margin=self.minimum_margin,
            auto_admit=False,
        )

    def has_explicit_threshold(
        self,
        *,
        product_family: ProductFamily,
        field_id: str,
        operation: Literal["rank", "project", "filter", "aggregate"],
    ) -> bool:
        """Return whether calibration explicitly covered this exact authority tuple."""

        return any(
            item.product_family is product_family
            and item.field_id == field_id
            and item.operation == operation
            for item in self.field_thresholds
        )

    @property
    def policy_sha256(self) -> str:
        return _canonical_sha256(self.model_dump(mode="json"))


_P1_FIELD_GROUPS: dict[str, tuple[str, frozenset[ProductFamily], bool]] = {
    "buy_yield_pct": ("bond_yield_tax_basis", frozenset({ProductFamily.BOND}), True),
    "after_tax_yield_pct": ("bond_yield_tax_basis", frozenset({ProductFamily.BOND}), True),
    "coupon_rate_pct": ("bond_coupon", frozenset({ProductFamily.BOND}), False),
    "issue_amount": ("bond_quantity_basis", frozenset({ProductFamily.BOND}), False),
    "total_issue_amount": ("bond_quantity_basis", frozenset({ProductFamily.BOND}), False),
    "duration_years": ("bond_time_sensitivity", frozenset({ProductFamily.BOND}), False),
    "remaining_days": ("bond_maturity_basis", frozenset({ProductFamily.BOND}), False),
    "maturity_date": ("bond_maturity_basis", frozenset({ProductFamily.BOND}), False),
    "aum": (
        "asset_scale",
        frozenset({ProductFamily.DOMESTIC_ETP, ProductFamily.OVERSEAS_ETP, ProductFamily.FUND}),
        False,
    ),
    "daily_trading_value": (
        "market_activity",
        frozenset({ProductFamily.DOMESTIC_ETP}),
        False,
    ),
    "total_expense_ratio_pct": (
        "holding_cost",
        frozenset({ProductFamily.DOMESTIC_ETP, ProductFamily.OVERSEAS_ETP}),
        False,
    ),
    "close_price": (
        "market_price",
        frozenset({ProductFamily.DOMESTIC_ETP, ProductFamily.OVERSEAS_ETP}),
        False,
    ),
    "leverage_factor": (
        "leverage_design",
        frozenset({ProductFamily.DOMESTIC_ETP}),
        False,
    ),
    "one_day_return_pct": (
        "return_period",
        frozenset({ProductFamily.DOMESTIC_ETP, ProductFamily.FUND}),
        True,
    ),
    "one_week_return_pct": (
        "return_period",
        frozenset({ProductFamily.FUND}),
        True,
    ),
    "one_month_return_pct": (
        "return_period",
        frozenset({ProductFamily.DOMESTIC_ETP, ProductFamily.FUND}),
        True,
    ),
    "three_month_return_pct": (
        "return_period",
        frozenset({ProductFamily.DOMESTIC_ETP, ProductFamily.FUND}),
        True,
    ),
    "six_month_return_pct": (
        "return_period",
        frozenset({ProductFamily.DOMESTIC_ETP, ProductFamily.FUND}),
        True,
    ),
    "one_year_return_pct": (
        "return_period",
        frozenset({ProductFamily.DOMESTIC_ETP, ProductFamily.FUND}),
        True,
    ),
    "ytd_return_pct": (
        "return_period",
        frozenset({ProductFamily.DOMESTIC_ETP}),
        True,
    ),
}


def p1_precision_field_thresholds() -> tuple[SchemaDenseFieldThreshold, ...]:
    """Return the versioned P1 selective policy; final scores remain Release inputs."""

    items: list[SchemaDenseFieldThreshold] = []
    for field_id, (semantic_group, families, deterministic_anchor) in _P1_FIELD_GROUPS.items():
        for family in sorted(families, key=lambda item: item.value):
            items.append(
                SchemaDenseFieldThreshold(
                    product_family=family,
                    field_id=field_id,
                    operation="rank",
                    semantic_group=semantic_group,
                    dense_min_score=(0.34 if deterministic_anchor else 0.42),
                    hclx_candidate_min_score=0.30,
                    minimum_margin=(0.0 if deterministic_anchor else 0.02),
                    auto_admit=deterministic_anchor,
                )
            )
    return tuple(
        sorted(
            items,
            key=lambda item: (item.product_family.value, item.field_id, item.operation),
        )
    )


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


_UNIT_GLOSS = {
    "none": "단위 없음",
    "code": "식별 코드",
    "boolean": "참·거짓 여부",
    "pct_point": "퍼센트포인트(%)",
    "source_currency_amount": "원천 통화 금액",
    "source_quantity": "원천 수량",
    "day": "일",
    "year": "년",
    "date": "날짜",
}

# This is a versioned retrieval corpus, not an execution ontology.  The server
# field registry remains the sole authority for operations and SQL compilation.
_FIELD_SEMANTIC_PROFILES: dict[str, dict[str, tuple[str, ...] | str]] = {
    "aum": {
        "definition": "상품에 모인 자금 규모를 나타내는 순자산총액 또는 운용자산 규모",
        "expressions": ("체급", "덩치", "자금이 많이 모인", "운용 규모가 큰", "순자산 규모"),
        "contrasts": (
            "daily_trading_value는 하루 거래 흐름이고 aum은 상품에 쌓인 자산 규모",
            "close_price는 한 주의 가격이고 aum은 전체 상품 규모",
            "issue_amount는 채권 발행 물량이고 aum은 ETF·펀드 운용자산",
        ),
        "counterexamples": ("거래가 활발한", "한 주 가격이 비싼"),
    },
    "daily_trading_value": {
        "definition": "하루 동안 시장에서 실제로 거래된 금액",
        "expressions": ("손바뀜이 많은", "활발하게 거래되는", "돈이 많이 오가는", "하루 거래대금"),
        "contrasts": ("aum은 쌓인 자산 규모이고 daily_trading_value는 하루 거래 흐름",),
        "counterexamples": ("체급이 큰", "자금이 많이 모인"),
    },
    "issue_amount": {
        "definition": "채권의 총발행금액 중 현재 남아 있는 발행잔액",
        "expressions": ("발행 잔액", "남은 발행 물량", "시장에 남은 발행액"),
        "contrasts": (
            "total_issue_amount는 총발행금액이고 issue_amount는 현재 남은 발행잔액",
        ),
        "counterexamples": ("총 발행 규모", "처음 발행한 금액", "지금 살 수 있는 수량"),
    },
    "total_issue_amount": {
        "definition": "채권이 처음 발행된 전체 금액인 총발행금액",
        "expressions": ("총 발행 규모", "전체 발행 물량", "처음 발행한 금액", "찍어낸 금액"),
        "contrasts": (
            "issue_amount는 현재 남은 발행잔액이고 total_issue_amount는 총발행금액",
        ),
        "counterexamples": ("남은 발행 물량", "발행 잔액", "지금 살 수 있는 수량"),
    },
    "buy_yield_pct": {
        "definition": "현재 가격으로 채권을 매수할 때의 세전 매수수익률",
        "expressions": ("지금 샀을 때 보상", "매수 기준 수익", "세전 매수 수익", "매수 수익률"),
        "contrasts": (
            "coupon_rate_pct는 액면 기준 표면금리이고 buy_yield_pct는 "
            "매수가격을 반영한 세전 수익률",
            "after_tax_yield_pct는 세후 수익률이고 buy_yield_pct는 세전 수익률",
        ),
        "counterexamples": ("정기적으로 붙는 이자율", "세금 떼고 남는 수익"),
    },
    "after_tax_yield_pct": {
        "definition": "세금을 반영한 뒤 남는 채권의 매수수익률",
        "expressions": ("세금 떼고 남는 보상", "세후 매수 수익", "세후 수익률"),
        "contrasts": ("buy_yield_pct는 세전이고 after_tax_yield_pct는 세금을 반영한 값",),
        "counterexamples": ("세전 매수 수익", "표면 이자율"),
    },
    "coupon_rate_pct": {
        "definition": "채권 액면금액을 기준으로 약정된 표면 이자율",
        "expressions": ("정기적으로 붙는 이자율", "표면 이자", "약정 금리", "쿠폰 금리"),
        "contrasts": (
            "buy_yield_pct는 매수가격을 반영하지만 coupon_rate_pct는 액면 기준 약정 금리",
        ),
        "counterexamples": ("지금 샀을 때 수익", "세후 수익"),
    },
    "duration_years": {
        "definition": "채권 가격이 금리 변화에 얼마나 민감한지를 나타내는 듀레이션",
        "expressions": ("금리 변화에 민감한", "금리 민감도", "듀레이션이 긴", "가격 변동 민감도"),
        "contrasts": ("remaining_days는 만기까지 남은 일수이고 duration_years는 금리 민감도",),
        "counterexamples": ("상환 시점이 빠른", "만기가 임박한"),
    },
    "remaining_days": {
        "definition": "기준일에서 채권 만기일까지 남은 일수",
        "expressions": ("만기가 임박한", "상환까지 얼마 안 남은", "잔존일수가 짧은"),
        "contrasts": ("duration_years는 금리 민감도이고 remaining_days는 만기까지 남은 시간",),
        "counterexamples": ("금리 변화에 민감한",),
    },
    "maturity_date": {
        "definition": "채권 원금 상환이 예정된 만기 날짜",
        "expressions": ("상환 시점이 빠른", "만기 날짜가 이른", "가장 늦게 만기되는"),
        "contrasts": ("remaining_days는 남은 일수이고 maturity_date는 달력상의 만기일",),
        "counterexamples": ("금리 민감도가 높은",),
    },
    "total_expense_ratio_pct": {
        "definition": "ETF 운용에 부과되는 총보수 또는 총비용 비율",
        "expressions": ("보유 부담", "관리 비용", "운영 비용", "운용 보수", "총비용이 낮은"),
        "contrasts": (
            "close_price는 매매 가격이고 total_expense_ratio_pct는 보유 중 부담하는 비율",
        ),
        "counterexamples": ("주가가 싼", "거래 가격이 낮은"),
    },
    "close_price": {
        "definition": "해당 기준일의 상품 종가 또는 한 주의 시장 가격",
        "expressions": ("종가", "마감 가격", "한 주 가격", "거래 가격"),
        "contrasts": ("total_expense_ratio_pct는 운용 비용 비율이고 close_price는 시장 가격",),
        "counterexamples": ("보유 비용이 낮은", "총보수가 싼"),
    },
    "leverage_factor": {
        "definition": "기초지수 움직임을 몇 배로 추종하도록 설계됐는지 나타내는 배율",
        "expressions": ("레버리지 배율", "몇 배 추종", "추종 배수가 큰"),
        "contrasts": ("수익률 실적이 아니라 상품 설계상의 추종 배율",),
        "counterexamples": ("최근 수익률이 높은",),
    },
    "one_day_return_pct": {
        "definition": "최근 1일 기간 수익률",
        "expressions": ("하루 성적", "1일 수익률", "오늘 기준 단기 성과"),
        "contrasts": ("one_month_return_pct는 한 달, one_year_return_pct는 1년 기간 수익률",),
        "counterexamples": ("기간을 말하지 않은 수익률",),
    },
    "one_week_return_pct": {
        "definition": "최근 1주 기간 수익률",
        "expressions": ("일주일 성적", "한 주 수익률", "최근 1주 성과"),
        "contrasts": ("one_month_return_pct는 한 달 기간 수익률",),
        "counterexamples": ("기간을 말하지 않은 수익률",),
    },
    "one_month_return_pct": {
        "definition": "최근 1개월 기간 수익률",
        "expressions": ("한 달 성적", "월간 수익률", "최근 한 달 성과"),
        "contrasts": ("three_month_return_pct는 3개월 기간 수익률",),
        "counterexamples": ("기간을 말하지 않은 수익률",),
    },
    "three_month_return_pct": {
        "definition": "최근 3개월 기간 수익률",
        "expressions": ("분기 성적", "석 달 수익률", "최근 3개월 성과"),
        "contrasts": ("six_month_return_pct는 6개월 기간 수익률",),
        "counterexamples": ("기간을 말하지 않은 수익률",),
    },
    "six_month_return_pct": {
        "definition": "최근 6개월 기간 수익률",
        "expressions": ("반년 성적", "6개월 수익률", "최근 반기 성과"),
        "contrasts": ("one_year_return_pct는 1년 기간 수익률",),
        "counterexamples": ("기간을 말하지 않은 수익률",),
    },
    "one_year_return_pct": {
        "definition": "최근 1년 기간 수익률",
        "expressions": ("1년 성적", "연간 수익률", "최근 일 년 성과"),
        "contrasts": ("ytd_return_pct는 올해 들어 현재까지의 수익률",),
        "counterexamples": ("올해 들어 수익률", "기간을 말하지 않은 수익률"),
    },
    "ytd_return_pct": {
        "definition": "연초부터 현재 기준일까지의 누적 수익률",
        "expressions": ("올해 성적", "연초 이후 수익률", "YTD", "올해 들어 성과"),
        "contrasts": ("one_year_return_pct는 최근 12개월이고 ytd_return_pct는 올해 연초 이후",),
        "counterexamples": ("최근 12개월 수익률",),
    },
}


def _schema_field_text_v2_parts(
    *,
    family: ProductFamily,
    field_id: str,
    dataset_label: str,
    resolved: FieldDefinition,
) -> list[str]:
    context = schema_field_semantic_context(
        family=family,
        field_id=field_id,
        dataset_label=dataset_label,
        resolved=resolved,
    )
    return [
        f"공식 field ID: {context['field_id']}",
        f"공식 이름: {context['label']}",
        f"상품군: {context['product_family']} ({context['dataset_label']})",
        f"정의: {context['definition']}",
        f"단위: {context['unit_gloss']} ({context['unit']})",
        "서버가 허용한 operation: " + ", ".join(context["supported_operations"]),
        "공식 alias: " + ", ".join(context["aliases"]),
        "사용자 표현: " + ", ".join(context["user_expressions"]),
        "유사 field와의 차이: " + " / ".join(context["contrasts"]),
        "이 field로 자동 확정하면 안 되는 반례: " + ", ".join(context["counterexamples"]),
        "원천 계약 주석: " + context["registry_notes"],
        "허용 enum: " + ", ".join(context["enum_values"]),
    ]


def _schema_field_embedding_prototypes(
    *,
    family: ProductFamily,
    field_id: str,
    dataset_label: str,
    resolved: FieldDefinition,
) -> tuple[str, ...]:
    context = schema_field_semantic_context(
        family=family,
        field_id=field_id,
        dataset_label=dataset_label,
        resolved=resolved,
    )
    prefix = f"{field_id} | {family.value} | {context['label']}"
    prototypes = [
        f"{prefix} | 정의: {context['definition']} | 단위: {context['unit_gloss']}",
        *(
            f"{prefix} | 사용자 표현: {expression} | 의미: {context['definition']}"
            for expression in context["user_expressions"]
        ),
        *(f"{prefix} | 구분 기준: {contrast}" for contrast in context["contrasts"]),
    ]
    if not context["user_expressions"]:
        prototypes.extend(
            f"{prefix} | 공식 alias: {alias} | 의미: {context['definition']}"
            for alias in context["aliases"][:5]
        )
    return tuple(dict.fromkeys(prototypes[:16]))


def schema_field_semantic_context(
    *,
    family: ProductFamily,
    field_id: str,
    dataset_label: str,
    resolved: FieldDefinition,
) -> SchemaFieldSemanticContext:
    """Return the bounded, server-authored field context shared by Dense and HCLX."""

    profile = _FIELD_SEMANTIC_PROFILES.get(field_id, {})
    operations = [
        label
        for label, enabled in (
            ("조건 필터", resolved.queryable),
            ("결과 표시", resolved.selectable),
            ("정렬·순위", resolved.sortable),
            ("집계", resolved.aggregatable),
            ("상품 비교", resolved.comparable),
        )
        if enabled
    ]
    definition = str(profile.get("definition", resolved.notes)).strip()
    expressions = tuple(profile.get("expressions", ()))
    contrasts = tuple(profile.get("contrasts", ()))
    counterexamples = tuple(profile.get("counterexamples", ()))
    return {
        "field_id": field_id,
        "label": resolved.label,
        "product_family": family.value,
        "dataset_label": dataset_label,
        "definition": definition,
        "unit": resolved.unit,
        "unit_gloss": _UNIT_GLOSS[resolved.unit],
        "supported_operations": tuple(operations),
        "aliases": tuple(resolved.aliases),
        "user_expressions": expressions,
        "contrasts": contrasts,
        "counterexamples": counterexamples,
        "registry_notes": resolved.notes,
        "enum_values": tuple(resolved.enum_values),
    }


def build_schema_field_entries(
    registry: FieldRegistry | None = None,
    *,
    corpus_template_version: Literal[
        "schema-field-text-v1",
        "schema-field-text-v2",
        "schema-field-prototype-v3",
    ] = _CORPUS_TEMPLATE_VERSION,
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
            embedding_prototypes: tuple[str, ...] = ()
            if corpus_template_version == _CORPUS_TEMPLATE_VERSION:
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
            else:
                text_parts = _schema_field_text_v2_parts(
                    family=family,
                    field_id=field_id,
                    dataset_label=dataset.source_label,
                    resolved=resolved,
                )
                if corpus_template_version == _CORPUS_TEMPLATE_VERSION_V3:
                    embedding_prototypes = _schema_field_embedding_prototypes(
                        family=family,
                        field_id=field_id,
                        dataset_label=dataset.source_label,
                        resolved=resolved,
                    )
            text = " | ".join(part.strip() for part in text_parts if part.strip())
            entries.append(
                SchemaFieldEntry(
                    document_id=f"{family.value}:{field_id}",
                    product_family=family,
                    field_id=field_id,
                    text=text,
                    embedding_prototypes=embedding_prototypes,
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
        corpus_template_version: Literal[
            "schema-field-text-v1",
            "schema-field-text-v2",
            "schema-field-prototype-v3",
        ] = _CORPUS_TEMPLATE_VERSION,
    ) -> DenseSchemaIndex:
        ordered = sorted(entries, key=lambda item: item.document_id)
        if not ordered:
            raise SchemaDenseContractError("schema index requires at least one field")
        keys = [entry.document_id for entry in ordered]
        if len(keys) != len(set(keys)):
            raise SchemaDenseContractError("schema field keys must be unique")
        if corpus_template_version == _CORPUS_TEMPLATE_VERSION_V3:
            if any(not entry.embedding_prototypes for entry in ordered):
                raise SchemaDenseContractError("prototype corpus requires every field prototype")
            flattened = [prototype for entry in ordered for prototype in entry.embedding_prototypes]
            prototype_vectors = provider.embed_documents(flattened)
            if len(prototype_vectors) != len(flattened):
                raise SchemaDenseContractError(
                    "embedding provider returned the wrong prototype vector count"
                )
            raw_vectors = []
            offset = 0
            for entry in ordered:
                count = len(entry.embedding_prototypes)
                normalized = [
                    _normalize_vector(vector, provider.metadata.dimension)
                    for vector in prototype_vectors[offset : offset + count]
                ]
                offset += count
                centroid = [
                    sum(vector[position] for vector in normalized) / count
                    for position in range(provider.metadata.dimension)
                ]
                raw_vectors.append(centroid)
        else:
            if any(entry.embedding_prototypes for entry in ordered):
                raise SchemaDenseContractError("text corpus cannot carry embedding prototypes")
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
            corpus_template_version=corpus_template_version,
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
        canonical_entries = build_schema_field_entries(
            active_registry,
            corpus_template_version=manifest.corpus_template_version,
        )
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
    "SchemaDenseFieldThreshold",
    "SchemaFieldCandidate",
    "SchemaFieldEntry",
    "SchemaFieldSemanticContext",
    "SchemaLinkResponse",
    "SchemaVectorRecord",
    "build_schema_field_entries",
    "approve_schema_index_for_production",
    "dense_schema_index_file_bytes",
    "load_dense_schema_index_artifact",
    "packaged_field_registry_sha256",
    "p1_precision_field_thresholds",
    "schema_field_semantic_context",
]
