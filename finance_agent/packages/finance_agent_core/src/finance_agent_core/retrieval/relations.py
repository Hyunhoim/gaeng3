from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import sqlite3
import unicodedata
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date
from enum import StrEnum
from functools import lru_cache
from pathlib import Path
from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from finance_agent_core.config import load_field_registry
from finance_agent_core.contracts.queryplan import ProductFamily
from finance_agent_core.domain import DatabaseManifest
from finance_agent_core.storage.approval import (
    ApprovedDatasetManifest,
    load_approved_dataset_manifest,
    require_approved_database,
    sha256_file,
)
from finance_agent_core.storage.identity_cache import (
    ProductIdentityRecord,
    ProductIdentitySnapshotCache,
)
from finance_agent_core.storage.sqlite import connect_read_only

_SHA256_PATTERN = r"^[0-9a-f]{64}$"
_QUERY_TOKEN = re.compile(r"[0-9A-Za-z가-힣]+")
_SUPPORTED_FAMILIES = frozenset(
    {
        ProductFamily.BOND,
        ProductFamily.DOMESTIC_ETP,
        ProductFamily.OVERSEAS_ETP,
    }
)


class RelationIndexError(RuntimeError):
    """Raised when a relation index cannot cross a trust boundary."""


class RelationModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class RelationType(StrEnum):
    ISSUED_BY = "issued_by"
    MANAGED_BY = "managed_by"
    TRACKS_INDEX = "tracks_index"
    CLASSIFIED_AS_ASSET = "classified_as_asset"
    INVESTS_IN_REGION = "invests_in_region"


class RelationEntityKind(StrEnum):
    COMPANY = "company"
    INDEX = "index"
    ASSET_CLASS = "asset_class"
    REGION = "region"


@dataclass(frozen=True, slots=True)
class RelationSourceField:
    product_family: ProductFamily
    table: str
    canonical_field: str
    relation_type: RelationType
    entity_kind: RelationEntityKind
    quality_column: str | None = None


_RELATION_SOURCE_FIELDS = (
    RelationSourceField(
        ProductFamily.BOND,
        "bond_products",
        "issuer",
        RelationType.ISSUED_BY,
        RelationEntityKind.COMPANY,
        "issuer_quality",
    ),
    RelationSourceField(
        ProductFamily.DOMESTIC_ETP,
        "domestic_etp_products",
        "manager",
        RelationType.MANAGED_BY,
        RelationEntityKind.COMPANY,
    ),
    RelationSourceField(
        ProductFamily.DOMESTIC_ETP,
        "domestic_etp_products",
        "base_index",
        RelationType.TRACKS_INDEX,
        RelationEntityKind.INDEX,
        "base_index_quality",
    ),
    RelationSourceField(
        ProductFamily.DOMESTIC_ETP,
        "domestic_etp_products",
        "asset_type",
        RelationType.CLASSIFIED_AS_ASSET,
        RelationEntityKind.ASSET_CLASS,
    ),
    RelationSourceField(
        ProductFamily.DOMESTIC_ETP,
        "domestic_etp_products",
        "investment_region",
        RelationType.INVESTS_IN_REGION,
        RelationEntityKind.REGION,
    ),
    RelationSourceField(
        ProductFamily.OVERSEAS_ETP,
        "overseas_etp_products",
        "asset_type",
        RelationType.CLASSIFIED_AS_ASSET,
        RelationEntityKind.ASSET_CLASS,
    ),
    RelationSourceField(
        ProductFamily.OVERSEAS_ETP,
        "overseas_etp_products",
        "investment_region",
        RelationType.INVESTS_IN_REGION,
        RelationEntityKind.REGION,
    ),
)
_RELATION_SOURCE_BY_FIELD = {
    (item.product_family, item.canonical_field): item for item in _RELATION_SOURCE_FIELDS
}


class RelationDatabaseBinding(RelationModel):
    product_family: ProductFamily
    source_id: str
    source_snapshot_date: date
    source_file_sha256: str = Field(pattern=_SHA256_PATTERN)
    database_sha256: str = Field(pattern=_SHA256_PATTERN)
    searchable_rows: int = Field(ge=0)


class RelationIndexManifest(RelationModel):
    schema_version: Literal["1.0"] = "1.0"
    index_kind: Literal["provided_product_relations"] = "provided_product_relations"
    status: Literal["verified_not_agent_activated"] = "verified_not_agent_activated"
    registry_schema_version: str
    approval_manifest_sha256: str = Field(pattern=_SHA256_PATTERN)
    database_bindings: tuple[RelationDatabaseBinding, ...] = Field(min_length=1, max_length=4)
    relation_types: tuple[RelationType, ...] = Field(min_length=1)
    relation_count: int = Field(gt=0)
    relation_set_sha256: str = Field(pattern=_SHA256_PATTERN)
    external_relation_count: Literal[0] = 0

    @model_validator(mode="after")
    def validate_canonical_order(self) -> RelationIndexManifest:
        families = [item.product_family.value for item in self.database_bindings]
        if families != sorted(families) or len(families) != len(set(families)):
            raise ValueError("database bindings must be unique and sorted")
        relation_types = [item.value for item in self.relation_types]
        if relation_types != sorted(relation_types) or len(relation_types) != len(
            set(relation_types)
        ):
            raise ValueError("relation types must be unique and sorted")
        return self


class RelationSearchRequest(RelationModel):
    query: str = Field(min_length=1, max_length=500)
    top_k: int = Field(default=10, ge=1, le=50)
    product_families: tuple[ProductFamily, ...] = Field(default=(), max_length=4)
    relation_types: tuple[RelationType, ...] = Field(default=(), max_length=5)
    as_of_on_or_before: date | None = None

    @field_validator("query")
    @classmethod
    def reject_blank_query(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("relation query cannot be blank")
        return value

    @model_validator(mode="after")
    def validate_filters(self) -> RelationSearchRequest:
        families = [item.value for item in self.product_families]
        relations = [item.value for item in self.relation_types]
        if any(item not in _SUPPORTED_FAMILIES for item in self.product_families):
            raise ValueError("fund relation search is not enabled before its source contract")
        if len(families) != len(set(families)):
            raise ValueError("product family filters must be unique")
        if len(relations) != len(set(relations)):
            raise ValueError("relation type filters must be unique")
        return self


class RelationEvidence(RelationModel):
    evidence_id: str
    relation_id: str
    relation_type: RelationType
    entity_id: str
    entity_kind: RelationEntityKind
    entity_label: str
    product_family: ProductFamily
    product_id: str
    product_name: str
    ticker: str | None
    canonical_field: str
    source_dataset: str
    source_id: str
    source_row: int = Field(ge=2)
    source_columns: tuple[str, ...] = Field(min_length=1)
    as_of: date
    source_database_sha256: str = Field(pattern=_SHA256_PATTERN)
    approval_manifest_sha256: str = Field(pattern=_SHA256_PATTERN)
    relevance_score: float = Field(ge=0)


class RelationSearchResponse(RelationModel):
    schema_version: Literal["1.0"] = "1.0"
    status: Literal["found", "not_found"]
    query: str
    relation_index_sha256: str = Field(pattern=_SHA256_PATTERN)
    evidence: tuple[RelationEvidence, ...]

    @model_validator(mode="after")
    def validate_status(self) -> RelationSearchResponse:
        if (self.status == "found") != bool(self.evidence):
            raise ValueError("found status and relation evidence must agree")
        return self


class RelationIndexBuildReceipt(RelationModel):
    schema_version: Literal["1.0"] = "1.0"
    status: Literal["verified_index_not_agent_activated"] = (
        "verified_index_not_agent_activated"
    )
    database_sha256: str = Field(pattern=_SHA256_PATTERN)
    database_size_bytes: int = Field(gt=0)
    approval_manifest_sha256: str = Field(pattern=_SHA256_PATTERN)
    relation_set_sha256: str = Field(pattern=_SHA256_PATTERN)
    relation_count: int = Field(gt=0)


@dataclass(frozen=True, slots=True)
class VerifiedProductDatabase:
    product_family: ProductFamily
    path: Path
    manifest: DatabaseManifest
    database_sha256: str
    identities: tuple[ProductIdentityRecord, ...]


@dataclass(frozen=True, slots=True)
class _RelationIndexVersion:
    device: int
    inode: int
    size_bytes: int
    modified_ns: int
    changed_ns: int


class ProductDatabaseVerifier(Protocol):
    @property
    def approval_manifest_sha256(self) -> str: ...

    def verify(
        self,
        product_family: ProductFamily,
        path: str | Path,
    ) -> VerifiedProductDatabase: ...


class ApprovedProductDatabaseVerifier:
    """Production verifier backed by the packaged official approval manifest."""

    def __init__(self, approval: ApprovedDatasetManifest | None = None) -> None:
        self._uses_packaged_approval = approval is None
        self.approval = approval or load_approved_dataset_manifest()
        self._identity_cache = ProductIdentitySnapshotCache(
            max_entries=len(_SUPPORTED_FAMILIES)
        )

    @property
    def approval_manifest_sha256(self) -> str:
        return self.approval.canonical_sha256

    def verify(
        self,
        product_family: ProductFamily,
        path: str | Path,
    ) -> VerifiedProductDatabase:
        if product_family not in _SUPPORTED_FAMILIES:
            raise RelationIndexError(
                f"provided relation extraction is not enabled for {product_family.value}"
            )
        resolved = Path(path).resolve(strict=True)
        manifest = require_approved_database(
            product_family.value,
            resolved,
            approval=None if self._uses_packaged_approval else self.approval,
        )
        identity_snapshot = self._identity_cache.get(resolved)
        if identity_snapshot.manifest != manifest:
            raise RelationIndexError("product database changed during relation verification")
        return VerifiedProductDatabase(
            product_family=product_family,
            path=resolved,
            manifest=manifest,
            database_sha256=self.approval.datasets[product_family.value].database_sha256,
            identities=identity_snapshot.records,
        )


@lru_cache(maxsize=1)
def _default_product_database_verifier() -> ApprovedProductDatabaseVerifier:
    return ApprovedProductDatabaseVerifier()


@dataclass(frozen=True, slots=True)
class _RelationRow:
    relation_id: str
    relation_type: RelationType
    entity_id: str
    entity_kind: RelationEntityKind
    entity_label: str
    normalized_entity_label: str
    product_family: ProductFamily
    product_id: str
    canonical_field: str
    source_dataset: str
    source_id: str
    source_row: int
    source_columns: tuple[str, ...]
    as_of: date
    source_database_sha256: str

    def canonical_payload(self) -> dict[str, object]:
        return {
            "as_of": self.as_of.isoformat(),
            "canonical_field": self.canonical_field,
            "entity_id": self.entity_id,
            "entity_kind": self.entity_kind.value,
            "entity_label": self.entity_label,
            "product_family": self.product_family.value,
            "product_id": self.product_id,
            "relation_id": self.relation_id,
            "relation_type": self.relation_type.value,
            "source_columns": list(self.source_columns),
            "source_database_sha256": self.source_database_sha256,
            "source_dataset": self.source_dataset,
            "source_id": self.source_id,
            "source_row": self.source_row,
        }


def _canonical_json_bytes(payload: object) -> bytes:
    return (
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
    ).encode("utf-8")


def _normalize_label(value: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", value).split()).casefold()


def _stable_id(prefix: str, payload: object) -> str:
    return f"{prefix}-{hashlib.sha256(_canonical_json_bytes(payload)).hexdigest()[:32]}"


def _fts_query(query: str) -> str:
    tokens = list(dict.fromkeys(token.casefold() for token in _QUERY_TOKEN.findall(query)))
    if not tokens:
        raise ValueError("relation query contains no searchable tokens")
    return " OR ".join(f'"{token}"' for token in tokens)


def _lexemes(value: str) -> str:
    normalized = _normalize_label(value)
    compact = re.sub(r"[^0-9a-z가-힣]+", "", normalized)
    return normalized if compact == normalized else f"{normalized} {compact}"


def _normalized_database_paths(
    database_paths: Mapping[ProductFamily | str, str | Path],
) -> dict[ProductFamily, Path]:
    normalized: dict[ProductFamily, Path] = {}
    for key, value in database_paths.items():
        family = ProductFamily(key)
        if family in normalized:
            raise RelationIndexError(f"duplicate database path for {family.value}")
        if family not in _SUPPORTED_FAMILIES:
            raise RelationIndexError(
                f"provided relation extraction is not enabled for {family.value}"
            )
        normalized[family] = Path(value)
    if not normalized:
        raise RelationIndexError("at least one approved product database is required")
    return normalized


def _verify_databases(
    database_paths: Mapping[ProductFamily | str, str | Path],
    verifier: ProductDatabaseVerifier,
) -> dict[ProductFamily, VerifiedProductDatabase]:
    normalized = _normalized_database_paths(database_paths)
    return {
        family: verifier.verify(family, normalized[family])
        for family in sorted(normalized, key=lambda item: item.value)
    }


def _extract_relation_rows(
    snapshots: Mapping[ProductFamily, VerifiedProductDatabase],
) -> tuple[_RelationRow, ...]:
    registry = load_field_registry()
    rows: list[_RelationRow] = []
    seen: set[str] = set()
    for source in _RELATION_SOURCE_FIELDS:
        snapshot = snapshots.get(source.product_family)
        if snapshot is None:
            continue
        definition = registry.require_field(
            source.canonical_field,
            [source.product_family.value],
        )
        source_id = registry.require_dataset(source.product_family.value).source_id
        quality_projection = (
            f", {source.quality_column} AS relation_quality"
            if source.quality_column is not None
            else ", 'VALID' AS relation_quality"
        )
        sql = f"""
            SELECT
                product_id,
                source_row,
                static_as_of,
                {source.canonical_field} AS entity_label
                {quality_projection}
            FROM {source.table}
            WHERE
                is_quarantined = 0
                AND {source.canonical_field} IS NOT NULL
                AND TRIM({source.canonical_field}) != ''
            ORDER BY product_id
        """
        identity_by_id = {item.product_id: item for item in snapshot.identities}
        with connect_read_only(snapshot.path) as connection:
            source_rows = connection.execute(sql).fetchall()
        for source_row in source_rows:
            if str(source_row["relation_quality"]) != "VALID":
                continue
            product_id = str(source_row["product_id"])
            identity = identity_by_id.get(product_id)
            if identity is None or identity.is_quarantined:
                raise RelationIndexError(
                    f"relation source contains an unverified product ID: {product_id}"
                )
            entity_label = " ".join(str(source_row["entity_label"]).split())
            if not entity_label or len(entity_label) > 500:
                raise RelationIndexError("relation entity label is blank or too long")
            normalized_label = _normalize_label(entity_label)
            entity_id = _stable_id(
                "ent",
                {
                    "entity_kind": source.entity_kind.value,
                    "normalized_label": normalized_label,
                },
            )
            identity_payload = {
                "canonical_field": source.canonical_field,
                "entity_id": entity_id,
                "product_family": source.product_family.value,
                "product_id": product_id,
                "relation_type": source.relation_type.value,
            }
            relation_id = _stable_id("rel", identity_payload)
            if relation_id in seen:
                raise RelationIndexError(f"duplicate relation ID: {relation_id}")
            seen.add(relation_id)
            rows.append(
                _RelationRow(
                    relation_id=relation_id,
                    relation_type=source.relation_type,
                    entity_id=entity_id,
                    entity_kind=source.entity_kind,
                    entity_label=entity_label,
                    normalized_entity_label=normalized_label,
                    product_family=source.product_family,
                    product_id=product_id,
                    canonical_field=source.canonical_field,
                    source_dataset=source.product_family.value,
                    source_id=source_id,
                    source_row=int(source_row["source_row"]),
                    source_columns=tuple(definition.source.columns),
                    as_of=date.fromisoformat(str(source_row["static_as_of"])),
                    source_database_sha256=snapshot.database_sha256,
                )
            )
    rows.sort(
        key=lambda item: (
            item.relation_type.value,
            item.normalized_entity_label,
            item.product_family.value,
            item.product_id,
        )
    )
    if not rows:
        raise RelationIndexError("approved product databases produced no relation rows")
    return tuple(rows)


def _manifest(
    snapshots: Mapping[ProductFamily, VerifiedProductDatabase],
    rows: tuple[_RelationRow, ...],
    verifier: ProductDatabaseVerifier,
) -> RelationIndexManifest:
    registry = load_field_registry()
    relation_payload = [item.canonical_payload() for item in rows]
    bindings = tuple(
        RelationDatabaseBinding(
            product_family=family,
            source_id=registry.require_dataset(family.value).source_id,
            source_snapshot_date=snapshot.manifest.source_snapshot_date,
            source_file_sha256=snapshot.manifest.source_file_sha256,
            database_sha256=snapshot.database_sha256,
            searchable_rows=snapshot.manifest.searchable_rows,
        )
        for family, snapshot in sorted(snapshots.items(), key=lambda item: item[0].value)
    )
    return RelationIndexManifest(
        registry_schema_version=registry.schema_version,
        approval_manifest_sha256=verifier.approval_manifest_sha256,
        database_bindings=bindings,
        relation_types=tuple(
            sorted({item.relation_type for item in rows}, key=lambda item: item.value)
        ),
        relation_count=len(rows),
        relation_set_sha256=hashlib.sha256(_canonical_json_bytes(relation_payload)).hexdigest(),
    )


def _initialize_database(
    database_path: Path,
    manifest: RelationIndexManifest,
    rows: tuple[_RelationRow, ...],
) -> None:
    with sqlite3.connect(database_path) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        connection.executescript(
            """
            CREATE TABLE relation_metadata (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            ) WITHOUT ROWID;

            CREATE TABLE product_relations (
                relation_id TEXT PRIMARY KEY,
                relation_type TEXT NOT NULL,
                entity_id TEXT NOT NULL,
                entity_kind TEXT NOT NULL,
                entity_label TEXT NOT NULL,
                normalized_entity_label TEXT NOT NULL,
                product_family TEXT NOT NULL,
                product_id TEXT NOT NULL,
                canonical_field TEXT NOT NULL,
                source_dataset TEXT NOT NULL,
                source_id TEXT NOT NULL,
                source_row INTEGER NOT NULL CHECK (source_row >= 2),
                source_columns_json TEXT NOT NULL,
                as_of TEXT NOT NULL,
                source_database_sha256 TEXT NOT NULL,
                UNIQUE (
                    relation_type,
                    entity_id,
                    product_family,
                    product_id,
                    canonical_field
                )
            ) WITHOUT ROWID;

            CREATE VIRTUAL TABLE product_relations_fts USING fts5(
                relation_id UNINDEXED,
                entity_label,
                lexemes,
                tokenize = 'unicode61'
            );
            """
        )
        connection.execute(
            "INSERT INTO relation_metadata (key, value) VALUES ('manifest', ?)",
            (
                json.dumps(
                    manifest.model_dump(mode="json"),
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ),
            ),
        )
        for row in rows:
            connection.execute(
                """
                INSERT INTO product_relations (
                    relation_id, relation_type, entity_id, entity_kind,
                    entity_label, normalized_entity_label, product_family,
                    product_id, canonical_field, source_dataset, source_id,
                    source_row, source_columns_json, as_of, source_database_sha256
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    row.relation_id,
                    row.relation_type.value,
                    row.entity_id,
                    row.entity_kind.value,
                    row.entity_label,
                    row.normalized_entity_label,
                    row.product_family.value,
                    row.product_id,
                    row.canonical_field,
                    row.source_dataset,
                    row.source_id,
                    row.source_row,
                    json.dumps(list(row.source_columns), ensure_ascii=False),
                    row.as_of.isoformat(),
                    row.source_database_sha256,
                ),
            )
            connection.execute(
                """
                INSERT INTO product_relations_fts (relation_id, entity_label, lexemes)
                VALUES (?, ?, ?)
                """,
                (row.relation_id, row.entity_label, _lexemes(row.entity_label)),
            )
        relation_count = connection.execute("SELECT COUNT(*) FROM product_relations").fetchone()
        fts_count = connection.execute("SELECT COUNT(*) FROM product_relations_fts").fetchone()
        if (
            relation_count is None
            or int(relation_count[0]) != manifest.relation_count
            or fts_count is None
            or int(fts_count[0]) != manifest.relation_count
        ):
            raise RelationIndexError("built relation index failed integrity verification")
        connection.commit()
        connection.execute("VACUUM")
        integrity = connection.execute("PRAGMA quick_check").fetchone()
        if integrity is None or integrity[0] != "ok":
            raise RelationIndexError("built relation index failed integrity verification")


def _require_output_parent(output: Path) -> Path:
    if output.suffix not in {".sqlite", ".sqlite3", ".db"}:
        raise RelationIndexError("relation index must use a SQLite file suffix")
    parent = output.parent.resolve(strict=True)
    if not parent.is_dir() or output.name in {"", ".", ".."}:
        raise RelationIndexError("relation index output parent is invalid")
    return parent


def build_provided_relation_index(
    database_paths: Mapping[ProductFamily | str, str | Path],
    output_database: str | Path,
    *,
    verifier: ProductDatabaseVerifier | None = None,
) -> RelationIndexBuildReceipt:
    """Build a new immutable relation index from approved product fields only."""

    active_verifier = verifier or _default_product_database_verifier()
    snapshots = _verify_databases(database_paths, active_verifier)
    rows = _extract_relation_rows(snapshots)
    manifest = _manifest(snapshots, rows, active_verifier)
    output = Path(output_database)
    parent = _require_output_parent(output)
    target = parent / output.name
    if target.exists() or target.is_symlink():
        raise RelationIndexError("relation index output already exists")
    temporary = parent / f".{output.name}.{secrets.token_hex(8)}.tmp"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0)
    try:
        descriptor = os.open(temporary, flags, 0o600)
        os.close(descriptor)
        _initialize_database(temporary, manifest, rows)
        os.chmod(temporary, 0o444)
        size = temporary.stat().st_size
        digest = sha256_file(temporary)
        try:
            os.link(temporary, target, follow_symlinks=False)
        except OSError as error:
            raise RelationIndexError("relation index output cannot be installed") from error
        temporary.unlink()
        return RelationIndexBuildReceipt(
            database_sha256=digest,
            database_size_bytes=size,
            approval_manifest_sha256=manifest.approval_manifest_sha256,
            relation_set_sha256=manifest.relation_set_sha256,
            relation_count=manifest.relation_count,
        )
    except Exception:
        if temporary.exists() and not temporary.is_symlink():
            temporary.unlink()
        raise


def _load_relation_manifest(connection: sqlite3.Connection) -> RelationIndexManifest:
    row = connection.execute(
        "SELECT value FROM relation_metadata WHERE key = 'manifest'"
    ).fetchone()
    if row is None:
        raise RelationIndexError("relation index manifest is missing")
    try:
        return RelationIndexManifest.model_validate(json.loads(str(row[0])))
    except (json.JSONDecodeError, ValueError) as error:
        raise RelationIndexError("relation index manifest is invalid") from error


def _relation_index_version(path: Path) -> _RelationIndexVersion:
    stat = path.stat()
    if not path.is_file():
        raise RelationIndexError("relation index is unavailable")
    return _RelationIndexVersion(
        device=stat.st_dev,
        inode=stat.st_ino,
        size_bytes=stat.st_size,
        modified_ns=stat.st_mtime_ns,
        changed_ns=stat.st_ctime_ns,
    )


def _validate_relation_content(
    connection: sqlite3.Connection,
    manifest: RelationIndexManifest,
) -> None:
    registry = load_field_registry()
    bindings = {item.product_family: item for item in manifest.database_bindings}
    rows = connection.execute(
        """
        SELECT * FROM product_relations
        ORDER BY relation_type, normalized_entity_label, product_family, product_id
        """
    ).fetchall()
    if len(rows) != manifest.relation_count:
        raise RelationIndexError("relation index row count differs from its manifest")
    payload: list[dict[str, object]] = []
    expected_fts: list[tuple[str, str, str]] = []
    seen_relation_ids: set[str] = set()
    for row in rows:
        try:
            family = ProductFamily(str(row["product_family"]))
            relation_type = RelationType(str(row["relation_type"]))
            entity_kind = RelationEntityKind(str(row["entity_kind"]))
            canonical_field = str(row["canonical_field"])
            source = _RELATION_SOURCE_BY_FIELD[(family, canonical_field)]
            source_columns_payload = json.loads(str(row["source_columns_json"]))
            if not isinstance(source_columns_payload, list) or not all(
                isinstance(item, str) for item in source_columns_payload
            ):
                raise ValueError("source columns must be a string list")
            source_columns = tuple(source_columns_payload)
            as_of = date.fromisoformat(str(row["as_of"]))
            source_row_number = int(row["source_row"])
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
            raise RelationIndexError("relation index contains an invalid typed row") from error
        if (
            relation_type is not source.relation_type
            or entity_kind is not source.entity_kind
            or str(row["source_dataset"]) != family.value
            or str(row["source_id"]) != registry.require_dataset(family.value).source_id
            or source_columns
            != tuple(registry.require_field(canonical_field, [family.value]).source.columns)
        ):
            raise RelationIndexError("relation row differs from the server-owned field contract")
        binding = bindings.get(family)
        if binding is None or str(row["source_database_sha256"]) != binding.database_sha256:
            raise RelationIndexError("relation row database hash differs from its binding")
        entity_label = " ".join(str(row["entity_label"]).split())
        if not entity_label or len(entity_label) > 500 or source_row_number < 2:
            raise RelationIndexError("relation row contains invalid source evidence")
        normalized_label = _normalize_label(entity_label)
        if normalized_label != str(row["normalized_entity_label"]):
            raise RelationIndexError("relation entity normalization differs")
        expected_entity_id = _stable_id(
            "ent",
            {"entity_kind": entity_kind.value, "normalized_label": normalized_label},
        )
        if str(row["entity_id"]) != expected_entity_id:
            raise RelationIndexError("relation entity ID differs from canonical content")
        identity_payload = {
            "canonical_field": canonical_field,
            "entity_id": expected_entity_id,
            "product_family": family.value,
            "product_id": str(row["product_id"]),
            "relation_type": relation_type.value,
        }
        expected_relation_id = _stable_id("rel", identity_payload)
        if (
            str(row["relation_id"]) != expected_relation_id
            or expected_relation_id in seen_relation_ids
        ):
            raise RelationIndexError("relation ID differs or is duplicated")
        seen_relation_ids.add(expected_relation_id)
        logical_row = _RelationRow(
            relation_id=expected_relation_id,
            relation_type=relation_type,
            entity_id=expected_entity_id,
            entity_kind=entity_kind,
            entity_label=entity_label,
            normalized_entity_label=normalized_label,
            product_family=family,
            product_id=str(row["product_id"]),
            canonical_field=canonical_field,
            source_dataset=family.value,
            source_id=str(row["source_id"]),
            source_row=source_row_number,
            source_columns=source_columns,
            as_of=as_of,
            source_database_sha256=str(row["source_database_sha256"]),
        )
        payload.append(logical_row.canonical_payload())
        expected_fts.append((expected_relation_id, entity_label, _lexemes(entity_label)))
    relation_set_sha256 = hashlib.sha256(_canonical_json_bytes(payload)).hexdigest()
    if relation_set_sha256 != manifest.relation_set_sha256:
        raise RelationIndexError("relation set hash differs from its manifest")
    actual_relation_types = tuple(
        sorted(
            {RelationType(str(row["relation_type"])) for row in rows},
            key=lambda item: item.value,
        )
    )
    if actual_relation_types != manifest.relation_types:
        raise RelationIndexError("relation types differ from the index manifest")
    actual_fts = [
        (str(row[0]), str(row[1]), str(row[2]))
        for row in connection.execute(
            """
            SELECT relation_id, entity_label, lexemes
            FROM product_relations_fts
            ORDER BY relation_id, entity_label, lexemes
            """
        ).fetchall()
    ]
    if sorted(expected_fts) != actual_fts:
        raise RelationIndexError("relation FTS rows differ from canonical relations")


@lru_cache(maxsize=8)
def _verify_relation_index_cached(
    resolved_path: str,
    version: _RelationIndexVersion,
) -> tuple[RelationIndexManifest, str]:
    del version
    path = Path(resolved_path)
    sidecars = [Path(f"{path}{suffix}") for suffix in ("-wal", "-shm", "-journal")]
    if any(os.path.lexists(sidecar) for sidecar in sidecars):
        raise RelationIndexError("relation index has an unexpected SQLite sidecar")
    with connect_read_only(path) as connection:
        manifest = _load_relation_manifest(connection)
        integrity = connection.execute("PRAGMA quick_check").fetchone()
        if integrity is None or integrity[0] != "ok":
            raise RelationIndexError("relation index integrity check failed")
        _validate_relation_content(connection, manifest)
    return manifest, sha256_file(path)


def _identity_maps(
    manifest: RelationIndexManifest,
    database_paths: Mapping[ProductFamily | str, str | Path],
    verifier: ProductDatabaseVerifier,
) -> dict[ProductFamily, dict[str, ProductIdentityRecord]]:
    snapshots = _verify_databases(database_paths, verifier)
    expected = {item.product_family: item for item in manifest.database_bindings}
    registry = load_field_registry()
    if set(snapshots) != set(expected):
        raise RelationIndexError("relation search database families differ from index bindings")
    if verifier.approval_manifest_sha256 != manifest.approval_manifest_sha256:
        raise RelationIndexError("relation index approval manifest differs from runtime approval")
    identity_maps: dict[ProductFamily, dict[str, ProductIdentityRecord]] = {}
    for family, snapshot in snapshots.items():
        binding = expected[family]
        if (
            snapshot.database_sha256 != binding.database_sha256
            or snapshot.manifest.source_file_sha256 != binding.source_file_sha256
            or snapshot.manifest.source_snapshot_date != binding.source_snapshot_date
            or snapshot.manifest.searchable_rows != binding.searchable_rows
            or binding.source_id != registry.require_dataset(family.value).source_id
        ):
            raise RelationIndexError(
                f"{family.value} database differs from relation index binding"
            )
        identity_maps[family] = {item.product_id: item for item in snapshot.identities}
    return identity_maps


class SQLiteRelationIndex:
    """Read-only relation retrieval with official product identity re-verification."""

    def __init__(self, database_path: str | Path) -> None:
        self.database_path = Path(database_path)

    def _verified(
        self,
    ) -> tuple[Path, _RelationIndexVersion, RelationIndexManifest, str]:
        try:
            resolved = self.database_path.resolve(strict=True)
            version = _relation_index_version(resolved)
        except OSError as error:
            raise RelationIndexError("relation index is unavailable") from error
        manifest, digest = _verify_relation_index_cached(str(resolved), version)
        return resolved, version, manifest, digest

    def manifest(self) -> RelationIndexManifest:
        return self._verified()[2]

    def search(
        self,
        request: RelationSearchRequest,
        database_paths: Mapping[ProductFamily | str, str | Path],
        *,
        verifier: ProductDatabaseVerifier | None = None,
    ) -> RelationSearchResponse:
        active_verifier = verifier or _default_product_database_verifier()
        resolved, before, manifest, relation_index_sha256 = self._verified()
        with connect_read_only(resolved) as connection:
            identities = _identity_maps(manifest, database_paths, active_verifier)
            clauses = ["product_relations_fts MATCH ?"]
            parameters: list[str | int] = [_fts_query(request.query)]
            if request.product_families:
                placeholders = ", ".join("?" for _ in request.product_families)
                clauses.append(f"r.product_family IN ({placeholders})")
                parameters.extend(item.value for item in request.product_families)
            if request.relation_types:
                placeholders = ", ".join("?" for _ in request.relation_types)
                clauses.append(f"r.relation_type IN ({placeholders})")
                parameters.extend(item.value for item in request.relation_types)
            if request.as_of_on_or_before is not None:
                clauses.append("r.as_of <= ?")
                parameters.append(request.as_of_on_or_before.isoformat())
            sql = f"""
                SELECT
                    r.*,
                    bm25(product_relations_fts, 0.0, 1.0, 1.5) AS rank
                FROM product_relations_fts
                JOIN product_relations AS r
                    ON r.relation_id = product_relations_fts.relation_id
                WHERE {" AND ".join(clauses)}
                ORDER BY
                    ROUND(bm25(product_relations_fts, 0.0, 1.0, 1.5), 6) ASC,
                    r.relation_type ASC,
                    r.normalized_entity_label ASC,
                    r.product_family ASC,
                    r.product_id ASC
                LIMIT ?
            """
            parameters.append(request.top_k)
            rows = connection.execute(sql, parameters).fetchall()
        if _relation_index_version(resolved) != before:
            raise RelationIndexError("relation index changed during search")

        evidence: list[RelationEvidence] = []
        for row in rows:
            family = ProductFamily(str(row["product_family"]))
            identity = identities.get(family, {}).get(str(row["product_id"]))
            if identity is None or identity.is_quarantined:
                raise RelationIndexError(
                    f"relation result failed official product re-verification: {row['relation_id']}"
                )
            evidence.append(
                RelationEvidence(
                    evidence_id=f"relation:{row['relation_id']}",
                    relation_id=row["relation_id"],
                    relation_type=row["relation_type"],
                    entity_id=row["entity_id"],
                    entity_kind=row["entity_kind"],
                    entity_label=row["entity_label"],
                    product_family=family,
                    product_id=identity.product_id,
                    product_name=identity.product_name,
                    ticker=identity.ticker,
                    canonical_field=row["canonical_field"],
                    source_dataset=row["source_dataset"],
                    source_id=row["source_id"],
                    source_row=row["source_row"],
                    source_columns=tuple(json.loads(row["source_columns_json"])),
                    as_of=row["as_of"],
                    source_database_sha256=row["source_database_sha256"],
                    approval_manifest_sha256=manifest.approval_manifest_sha256,
                    relevance_score=round(max(0.0, -float(row["rank"])), 9),
                )
            )
        return RelationSearchResponse(
            status="found" if evidence else "not_found",
            query=request.query,
            relation_index_sha256=relation_index_sha256,
            evidence=tuple(evidence),
        )


__all__ = [
    "ApprovedProductDatabaseVerifier",
    "ProductDatabaseVerifier",
    "RelationDatabaseBinding",
    "RelationEntityKind",
    "RelationEvidence",
    "RelationIndexBuildReceipt",
    "RelationIndexError",
    "RelationIndexManifest",
    "RelationSearchRequest",
    "RelationSearchResponse",
    "RelationType",
    "SQLiteRelationIndex",
    "VerifiedProductDatabase",
    "build_provided_relation_index",
]
