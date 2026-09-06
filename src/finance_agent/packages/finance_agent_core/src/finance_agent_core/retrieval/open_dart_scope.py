from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import stat
import tempfile
import unicodedata
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from finance_agent_core.contracts.queryplan import ProductFamily
from finance_agent_core.retrieval.open_dart import OpenDartEntitySeed
from finance_agent_core.retrieval.relations import (
    RelationEntityKind,
    RelationIndexManifest,
    RelationType,
)

_SHA256_PATTERN = r"^[0-9a-f]{64}$"
_MAX_RELATION_DATABASE_BYTES = 128 * 1024 * 1024
_MAX_OPEN_DART_ENTITIES = 500
_COLLECTION_SCOPE_RELATION_TYPES = frozenset(
    {
        RelationType.ISSUED_BY,
        RelationType.MANAGED_BY,
    }
)
_AUTOMATIC_RELATION_TYPES = frozenset({RelationType.ISSUED_BY})
_REQUIRED_RELATION_COLUMNS = frozenset(
    {
        "relation_id",
        "relation_type",
        "entity_id",
        "entity_kind",
        "entity_label",
        "normalized_entity_label",
        "product_family",
        "product_id",
        "source_dataset",
    }
)


class OpenDartScopeError(RuntimeError):
    """Raised when a provided-relation DB cannot define a safe collection scope."""


class _ScopeModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class OpenDartProductFamilyCoverage(_ScopeModel):
    product_family: ProductFamily
    relation_count: int = Field(ge=1)
    product_count: int = Field(ge=1)
    product_id_set_sha256: str = Field(pattern=_SHA256_PATTERN)


class OpenDartScopedEntity(_ScopeModel):
    """One DB-derived candidate; this is not an OpenDART identity approval."""

    entity_id: str = Field(pattern=r"^odscope-[0-9a-f]{32}$")
    legal_name: str = Field(min_length=1, max_length=300)
    normalized_name: str = Field(min_length=1, max_length=300)
    source_relation_entity_id: str = Field(min_length=1, max_length=160)
    relation_types: tuple[Literal["issued_by"], ...] = Field(min_length=1)
    family_coverage: tuple[OpenDartProductFamilyCoverage, ...] = Field(min_length=1)
    relation_count: int = Field(ge=1)
    product_count: int = Field(ge=1)
    product_id_set_sha256: str = Field(pattern=_SHA256_PATTERN)

    @model_validator(mode="after")
    def validate_canonical_content(self) -> OpenDartScopedEntity:
        relation_types = tuple(sorted(set(self.relation_types)))
        if self.relation_types != relation_types:
            raise ValueError("OpenDART scope relation types must be unique and sorted")
        families = tuple(item.product_family.value for item in self.family_coverage)
        if families != tuple(sorted(set(families))):
            raise ValueError("OpenDART scope family coverage must be unique and sorted")
        if self.relation_count != sum(item.relation_count for item in self.family_coverage):
            raise ValueError("OpenDART scope relation count differs from family coverage")
        if self.product_count < max(item.product_count for item in self.family_coverage):
            raise ValueError("OpenDART scope product count is smaller than family coverage")
        return self


class OpenDartProvidedRelationScope(_ScopeModel):
    """Collection-only OpenDART scope derived from a verified provided relation index."""

    schema_version: Literal["1.0"] = "1.0"
    artifact_kind: Literal["open_dart_provided_relation_collection_scope"] = (
        "open_dart_provided_relation_collection_scope"
    )
    status: Literal["collection_candidates_only_not_authorized_for_runtime_or_release"] = (
        "collection_candidates_only_not_authorized_for_runtime_or_release"
    )
    source_relation_index_sha256: str = Field(pattern=_SHA256_PATTERN)
    source_relation_manifest_sha256: str = Field(pattern=_SHA256_PATTERN)
    source_relation_set_sha256: str = Field(pattern=_SHA256_PATTERN)
    source_relation_count: int = Field(ge=1)
    eligible_relation_count: int = Field(ge=1)
    eligible_entity_count: int = Field(ge=1)
    eligible_product_count: int = Field(ge=1)
    managed_by_review_required_count: int = Field(ge=0)
    max_entities: int = Field(ge=1, le=_MAX_OPEN_DART_ENTITIES)
    truncated: bool
    selected_relation_count: int = Field(ge=1)
    selected_product_count: int = Field(ge=1)
    entities: tuple[OpenDartScopedEntity, ...] = Field(min_length=1)
    seeds: tuple[OpenDartEntitySeed, ...] = Field(min_length=1)
    scope_set_sha256: str = Field(pattern=_SHA256_PATTERN)

    @model_validator(mode="after")
    def validate_scope(self) -> OpenDartProvidedRelationScope:
        expected_entities = tuple(sorted(self.entities, key=_entity_selection_key))
        if self.entities != expected_entities:
            raise ValueError("OpenDART scope entities do not use the canonical selection order")
        if len(self.entities) > self.max_entities:
            raise ValueError("OpenDART scope exceeds its entity bound")
        if len({item.entity_id for item in self.entities}) != len(self.entities):
            raise ValueError("OpenDART scope contains duplicate entity IDs")
        if self.selected_relation_count != sum(item.relation_count for item in self.entities):
            raise ValueError("OpenDART selected relation count differs from its entities")
        if self.truncated != (self.eligible_entity_count > len(self.entities)):
            raise ValueError("OpenDART scope truncation marker differs from eligible entities")
        expected_seeds = tuple(
            sorted(
                (
                    OpenDartEntitySeed(
                        entity_id=item.entity_id,
                        legal_name=item.legal_name,
                        reviewed_aliases=(),
                        seed_source="provided_db",
                    )
                    for item in self.entities
                ),
                key=lambda item: item.entity_id,
            )
        )
        if self.seeds != expected_seeds:
            raise ValueError("OpenDART seeds differ from the selected provided-DB scope")
        expected_scope_sha256 = _sha256_json(
            [item.model_dump(mode="json") for item in self.entities]
        )
        if self.scope_set_sha256 != expected_scope_sha256:
            raise ValueError("OpenDART scope-set SHA-256 differs from canonical entities")
        return self

    @property
    def canonical_sha256(self) -> str:
        return _sha256_json(self.model_dump(mode="json"))


def _canonical_json_bytes(payload: object) -> bytes:
    return (
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
    ).encode("utf-8")


def _sha256_json(payload: object) -> str:
    return hashlib.sha256(_canonical_json_bytes(payload)).hexdigest()


def _normalized_label(value: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", value).split()).casefold()


def _canonical_label(value: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", value).split())


def _stable_scope_entity_id(normalized_name: str) -> str:
    digest = _sha256_json(
        {
            "entity_kind": RelationEntityKind.COMPANY.value,
            "normalized_name": normalized_name,
            "scope": "provided_relation_open_dart_collection",
        }
    )
    return f"odscope-{digest[:32]}"


def _identity(metadata: os.stat_result) -> tuple[int, int, int, int, int]:
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_size,
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
    )


def _read_race_safe(path: Path) -> tuple[bytes, str]:
    if path.is_symlink():
        raise OpenDartScopeError("provided relation index must not be a symbolic link")
    sidecars = tuple(Path(f"{path}{suffix}") for suffix in ("-wal", "-shm", "-journal"))
    if any(os.path.lexists(item) for item in sidecars):
        raise OpenDartScopeError("provided relation index has an unexpected SQLite sidecar")
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as error:
        raise OpenDartScopeError("provided relation index is unavailable") from error
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode):
            raise OpenDartScopeError("provided relation index must be a regular file")
        if not 0 < before.st_size <= _MAX_RELATION_DATABASE_BYTES:
            raise OpenDartScopeError("provided relation index size is outside the bound")
        chunks: list[bytes] = []
        remaining = before.st_size
        while remaining:
            chunk = os.read(descriptor, min(1024 * 1024, remaining))
            if not chunk:
                raise OpenDartScopeError("provided relation index ended before its bound size")
            chunks.append(chunk)
            remaining -= len(chunk)
        if os.read(descriptor, 1):
            raise OpenDartScopeError("provided relation index grew while it was read")
        after = os.fstat(descriptor)
        try:
            path_after = os.stat(path, follow_symlinks=False)
        except OSError as error:
            raise OpenDartScopeError("provided relation index changed while it was read") from error
        if _identity(before) != _identity(after) or _identity(after) != _identity(path_after):
            raise OpenDartScopeError("provided relation index changed while it was read")
        if any(os.path.lexists(item) for item in sidecars):
            raise OpenDartScopeError("provided relation index gained a SQLite sidecar")
        payload = b"".join(chunks)
        return payload, hashlib.sha256(payload).hexdigest()
    finally:
        os.close(descriptor)


@contextmanager
def _materialized_connection(database_bytes: bytes) -> Iterator[sqlite3.Connection]:
    with tempfile.TemporaryDirectory(prefix="finance-open-dart-scope-") as directory:
        snapshot = Path(directory) / "provided-relations.sqlite3"
        descriptor = os.open(
            snapshot,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0),
            0o600,
        )
        try:
            view = memoryview(database_bytes)
            while view:
                written = os.write(descriptor, view)
                if written <= 0:  # pragma: no cover - defensive OS boundary
                    raise OpenDartScopeError("could not materialize relation index snapshot")
                view = view[written:]
        finally:
            os.close(descriptor)
        os.chmod(snapshot, 0o400)
        uri = f"file:{snapshot.as_posix()}?mode=ro&immutable=1"
        connection = sqlite3.connect(uri, uri=True)
        connection.row_factory = sqlite3.Row
        try:
            connection.execute("PRAGMA query_only = ON")
            yield connection
        finally:
            connection.close()


def _load_manifest(connection: sqlite3.Connection) -> RelationIndexManifest:
    try:
        row = connection.execute(
            "SELECT value FROM relation_metadata WHERE key = 'manifest'"
        ).fetchone()
    except sqlite3.DatabaseError as error:
        raise OpenDartScopeError("provided relation manifest is unavailable") from error
    if row is None:
        raise OpenDartScopeError("provided relation manifest is missing")
    try:
        payload = json.loads(str(row[0]))
        manifest = RelationIndexManifest.model_validate(payload)
    except (json.JSONDecodeError, ValueError) as error:
        raise OpenDartScopeError("provided relation manifest is invalid") from error
    if (
        manifest.index_kind != "provided_product_relations"
        or manifest.status != "verified_not_agent_activated"
        or manifest.external_relation_count != 0
    ):  # pragma: no cover - also enforced by the typed manifest
        raise OpenDartScopeError("provided relation manifest is not collection eligible")
    return manifest


def _validate_database(connection: sqlite3.Connection, manifest: RelationIndexManifest) -> None:
    try:
        integrity = tuple(str(row[0]) for row in connection.execute("PRAGMA integrity_check"))
        columns = {
            str(row[1]) for row in connection.execute("PRAGMA table_info(product_relations)")
        }
        relation_count = int(
            connection.execute("SELECT COUNT(*) FROM product_relations").fetchone()[0]
        )
        actual_relation_types = tuple(
            sorted(
                RelationType(str(row[0]))
                for row in connection.execute(
                    "SELECT DISTINCT relation_type FROM product_relations"
                )
            )
        )
        actual_families = {
            ProductFamily(str(row[0]))
            for row in connection.execute("SELECT DISTINCT product_family FROM product_relations")
        }
    except (sqlite3.DatabaseError, TypeError, ValueError) as error:
        raise OpenDartScopeError("provided relation index content is invalid") from error
    if integrity != ("ok",):
        raise OpenDartScopeError("provided relation index integrity check failed")
    if not _REQUIRED_RELATION_COLUMNS.issubset(columns):
        raise OpenDartScopeError("provided relation index schema is incomplete")
    if relation_count != manifest.relation_count:
        raise OpenDartScopeError("provided relation row count differs from its manifest")
    if actual_relation_types != tuple(sorted(manifest.relation_types)):
        raise OpenDartScopeError("provided relation types differ from its manifest")
    binding_families = {item.product_family for item in manifest.database_bindings}
    if not actual_families.issubset(binding_families):
        raise OpenDartScopeError("provided relation families differ from its manifest bindings")


def _family_coverage(
    rows: tuple[sqlite3.Row, ...],
) -> tuple[OpenDartProductFamilyCoverage, ...]:
    by_family: dict[ProductFamily, list[sqlite3.Row]] = {}
    for row in rows:
        family = ProductFamily(str(row["product_family"]))
        by_family.setdefault(family, []).append(row)
    coverage: list[OpenDartProductFamilyCoverage] = []
    for family in sorted(by_family, key=lambda item: item.value):
        family_rows = by_family[family]
        product_ids = tuple(sorted({str(row["product_id"]) for row in family_rows}))
        coverage.append(
            OpenDartProductFamilyCoverage(
                product_family=family,
                relation_count=len(family_rows),
                product_count=len(product_ids),
                product_id_set_sha256=_sha256_json(list(product_ids)),
            )
        )
    return tuple(coverage)


def _scoped_entity(rows: tuple[sqlite3.Row, ...]) -> OpenDartScopedEntity:
    normalized_names = {str(row["normalized_entity_label"]) for row in rows}
    source_entity_ids = {str(row["entity_id"]) for row in rows}
    if len(normalized_names) != 1 or len(source_entity_ids) != 1:
        raise OpenDartScopeError("provided relation entity identity is inconsistent")
    normalized_name = next(iter(normalized_names))
    labels = tuple(sorted({_canonical_label(str(row["entity_label"])) for row in rows}))
    labels_are_consistent = all(_normalized_label(item) == normalized_name for item in labels)
    if not normalized_name or not labels or not labels_are_consistent:
        raise OpenDartScopeError("provided relation entity label is inconsistent")
    product_ids = tuple(sorted({f"{row['product_family']}:{row['product_id']}" for row in rows}))
    relation_types = tuple(sorted({str(row["relation_type"]) for row in rows}))
    return OpenDartScopedEntity(
        entity_id=_stable_scope_entity_id(normalized_name),
        legal_name=labels[0],
        normalized_name=normalized_name,
        source_relation_entity_id=next(iter(source_entity_ids)),
        relation_types=relation_types,
        family_coverage=_family_coverage(rows),
        relation_count=len(rows),
        product_count=len(product_ids),
        product_id_set_sha256=_sha256_json(list(product_ids)),
    )


def _entity_selection_key(entity: OpenDartScopedEntity) -> tuple[int, int, str, str]:
    return (-entity.product_count, -entity.relation_count, entity.normalized_name, entity.entity_id)


def build_open_dart_scope_from_provided_relations(
    relation_database: str | Path,
    *,
    max_entities: int = _MAX_OPEN_DART_ENTITIES,
) -> OpenDartProvidedRelationScope:
    """Derive bounded OpenDART collection candidates from the provided relation DB.

    The result grants neither document approval nor runtime/release authority. Entity
    names still need exact OpenDART corporation-code binding before any collection.
    """

    if not 1 <= max_entities <= _MAX_OPEN_DART_ENTITIES:
        raise OpenDartScopeError(
            f"OpenDART scope max_entities must be between 1 and {_MAX_OPEN_DART_ENTITIES}"
        )
    database_bytes, database_sha256 = _read_race_safe(Path(relation_database))
    with _materialized_connection(database_bytes) as connection:
        manifest = _load_manifest(connection)
        _validate_database(connection, manifest)
        placeholders = ", ".join("?" for _ in _COLLECTION_SCOPE_RELATION_TYPES)
        parameters = tuple(sorted(item.value for item in _COLLECTION_SCOPE_RELATION_TYPES))
        try:
            raw_rows = tuple(
                connection.execute(
                    f"""
                    SELECT
                        relation_id,
                        relation_type,
                        entity_id,
                        entity_kind,
                        entity_label,
                        normalized_entity_label,
                        product_family,
                        product_id,
                        source_dataset
                    FROM product_relations
                    WHERE entity_kind = ? AND relation_type IN ({placeholders})
                    ORDER BY normalized_entity_label, relation_type, product_family, product_id
                    """,
                    (RelationEntityKind.COMPANY.value, *parameters),
                )
            )
        except sqlite3.DatabaseError as error:
            raise OpenDartScopeError("provided company relations cannot be read") from error
    automatic_rows = tuple(
        row
        for row in raw_rows
        if RelationType(str(row["relation_type"])) in _AUTOMATIC_RELATION_TYPES
    )
    managed_by_review_required_count = sum(
        RelationType(str(row["relation_type"])) is RelationType.MANAGED_BY for row in raw_rows
    )
    if not automatic_rows:
        raise OpenDartScopeError("provided relation index contains no eligible companies")

    rows_by_name: dict[str, list[sqlite3.Row]] = {}
    for row in automatic_rows:
        if str(row["entity_kind"]) != RelationEntityKind.COMPANY.value:
            raise OpenDartScopeError("non-company relation crossed the OpenDART scope filter")
        relation_type = RelationType(str(row["relation_type"]))
        if relation_type not in _AUTOMATIC_RELATION_TYPES:
            raise OpenDartScopeError("unsupported relation crossed the OpenDART scope filter")
        if str(row["source_dataset"]) != str(row["product_family"]):
            raise OpenDartScopeError("provided relation source dataset differs from its family")
        normalized = str(row["normalized_entity_label"])
        if _normalized_label(str(row["entity_label"])) != normalized:
            raise OpenDartScopeError("provided relation label differs from its normalized form")
        rows_by_name.setdefault(normalized, []).append(row)

    eligible_entities = tuple(
        sorted(
            (_scoped_entity(tuple(rows)) for rows in rows_by_name.values()),
            key=_entity_selection_key,
        )
    )
    selected_entities = eligible_entities[:max_entities]
    seeds = tuple(
        sorted(
            (
                OpenDartEntitySeed(
                    entity_id=item.entity_id,
                    legal_name=item.legal_name,
                    reviewed_aliases=(),
                    seed_source="provided_db",
                )
                for item in selected_entities
            ),
            key=lambda item: item.entity_id,
        )
    )
    eligible_products = {f"{row['product_family']}:{row['product_id']}" for row in automatic_rows}
    selected_names = {item.normalized_name for item in selected_entities}
    selected_products = len(
        {
            f"{row['product_family']}:{row['product_id']}"
            for row in automatic_rows
            if str(row["normalized_entity_label"]) in selected_names
        }
    )
    manifest_payload = manifest.model_dump(mode="json")
    return OpenDartProvidedRelationScope(
        source_relation_index_sha256=database_sha256,
        source_relation_manifest_sha256=_sha256_json(manifest_payload),
        source_relation_set_sha256=manifest.relation_set_sha256,
        source_relation_count=manifest.relation_count,
        eligible_relation_count=len(automatic_rows),
        eligible_entity_count=len(eligible_entities),
        eligible_product_count=len(eligible_products),
        managed_by_review_required_count=managed_by_review_required_count,
        max_entities=max_entities,
        truncated=len(eligible_entities) > len(selected_entities),
        selected_relation_count=sum(item.relation_count for item in selected_entities),
        selected_product_count=selected_products,
        entities=selected_entities,
        seeds=seeds,
        scope_set_sha256=_sha256_json([item.model_dump(mode="json") for item in selected_entities]),
    )


__all__ = [
    "OpenDartProductFamilyCoverage",
    "OpenDartProvidedRelationScope",
    "OpenDartScopeError",
    "OpenDartScopedEntity",
    "build_open_dart_scope_from_provided_relations",
]
