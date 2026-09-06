from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from datetime import date
from pathlib import Path

import pytest

from finance_agent_core.contracts.queryplan import ProductFamily
from finance_agent_core.retrieval.open_dart_scope import (
    OpenDartProvidedRelationScope,
    OpenDartScopeError,
    build_open_dart_scope_from_provided_relations,
)
from finance_agent_core.retrieval.relations import (
    RelationDatabaseBinding,
    RelationIndexManifest,
    RelationType,
)

_HASH_A = "a" * 64
_HASH_B = "b" * 64
_HASH_C = "c" * 64
_HASH_D = "d" * 64


def _manifest() -> RelationIndexManifest:
    return RelationIndexManifest(
        registry_schema_version="test-v1",
        approval_manifest_sha256=_HASH_A,
        database_bindings=(
            RelationDatabaseBinding(
                product_family=ProductFamily.BOND,
                source_id="PRBD01N001",
                source_snapshot_date=date(2026, 8, 24),
                source_file_sha256=_HASH_B,
                database_sha256=_HASH_C,
                searchable_rows=3,
            ),
            RelationDatabaseBinding(
                product_family=ProductFamily.DOMESTIC_ETP,
                source_id="PREF01N001",
                source_snapshot_date=date(2026, 8, 24),
                source_file_sha256=_HASH_B,
                database_sha256=_HASH_D,
                searchable_rows=1,
            ),
        ),
        relation_types=(
            RelationType.ISSUED_BY,
            RelationType.MANAGED_BY,
            RelationType.TRACKS_INDEX,
        ),
        relation_count=5,
        relation_set_sha256=_HASH_D,
    )


def _database(path: Path) -> Path:
    rows = (
        (
            "r1",
            "issued_by",
            "source-alpha",
            "company",
            "알파 주식회사",
            "알파 주식회사",
            "bond",
            "B1",
            "bond",
        ),
        (
            "r2",
            "issued_by",
            "source-alpha",
            "company",
            "알파 주식회사",
            "알파 주식회사",
            "bond",
            "B2",
            "bond",
        ),
        (
            "r3",
            "managed_by",
            "source-beta",
            "company",
            "베타자산운용",
            "베타자산운용",
            "domestic_etp",
            "D1",
            "domestic_etp",
        ),
        (
            "r4",
            "tracks_index",
            "source-index",
            "index",
            "테스트 지수",
            "테스트 지수",
            "domestic_etp",
            "D1",
            "domestic_etp",
        ),
        (
            "r5",
            "issued_by",
            "source-gamma",
            "company",
            "감마 주식회사",
            "감마 주식회사",
            "bond",
            "B3",
            "bond",
        ),
    )
    with sqlite3.connect(path) as connection:
        connection.executescript(
            """
            CREATE TABLE relation_metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE product_relations (
                relation_id TEXT PRIMARY KEY,
                relation_type TEXT NOT NULL,
                entity_id TEXT NOT NULL,
                entity_kind TEXT NOT NULL,
                entity_label TEXT NOT NULL,
                normalized_entity_label TEXT NOT NULL,
                product_family TEXT NOT NULL,
                product_id TEXT NOT NULL,
                source_dataset TEXT NOT NULL
            );
            """
        )
        connection.execute(
            "INSERT INTO relation_metadata (key, value) VALUES ('manifest', ?)",
            (json.dumps(_manifest().model_dump(mode="json"), sort_keys=True),),
        )
        connection.executemany(
            "INSERT INTO product_relations VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            rows,
        )
    return path


def test_builds_deterministic_bounded_collection_only_scope(tmp_path: Path) -> None:
    database = _database(tmp_path / "provided-relations.sqlite3")

    scope = build_open_dart_scope_from_provided_relations(database, max_entities=1)
    repeated = build_open_dart_scope_from_provided_relations(database, max_entities=1)

    assert scope == repeated
    assert scope.status == "collection_candidates_only_not_authorized_for_runtime_or_release"
    assert scope.source_relation_index_sha256 == hashlib.sha256(database.read_bytes()).hexdigest()
    assert scope.source_relation_count == 5
    assert scope.eligible_relation_count == 3
    assert scope.eligible_entity_count == 2
    assert scope.eligible_product_count == 3
    assert scope.managed_by_review_required_count == 1
    assert scope.truncated is True
    assert len(scope.entities) == len(scope.seeds) == 1
    entity = scope.entities[0]
    assert entity.legal_name == "알파 주식회사"
    assert entity.product_count == entity.relation_count == 2
    assert entity.relation_types == ("issued_by",)
    assert entity.family_coverage[0].product_family is ProductFamily.BOND
    assert scope.seeds[0].entity_id == entity.entity_id
    assert scope.seeds[0].seed_source == "provided_db"
    assert scope.seeds[0].reviewed_aliases == ()
    assert all(entity.legal_name != "베타자산운용" for entity in scope.entities)
    assert all(seed.legal_name != "베타자산운용" for seed in scope.seeds)


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("index_kind", "external_product_relations"),
        ("status", "approved_for_runtime"),
        ("external_relation_count", 1),
    ),
)
def test_rejects_manifest_that_is_not_a_provided_only_index(
    tmp_path: Path,
    field: str,
    value: object,
) -> None:
    database = _database(tmp_path / "provided-relations.sqlite3")
    with sqlite3.connect(database) as connection:
        payload = json.loads(
            connection.execute(
                "SELECT value FROM relation_metadata WHERE key = 'manifest'"
            ).fetchone()[0]
        )
        payload[field] = value
        connection.execute(
            "UPDATE relation_metadata SET value = ? WHERE key = 'manifest'",
            (json.dumps(payload, sort_keys=True),),
        )

    with pytest.raises(OpenDartScopeError, match="manifest is invalid"):
        build_open_dart_scope_from_provided_relations(database)


def test_rejects_row_count_or_sqlite_sidecar_drift(tmp_path: Path) -> None:
    database = _database(tmp_path / "provided-relations.sqlite3")
    with sqlite3.connect(database) as connection:
        connection.execute("DELETE FROM product_relations WHERE relation_id = 'r4'")

    with pytest.raises(OpenDartScopeError, match="row count differs"):
        build_open_dart_scope_from_provided_relations(database)

    database = _database(tmp_path / "second-provided-relations.sqlite3")
    Path(f"{database}-wal").touch()
    with pytest.raises(OpenDartScopeError, match="SQLite sidecar"):
        build_open_dart_scope_from_provided_relations(database)


def test_rejects_symlink_and_tampered_normalized_company(tmp_path: Path) -> None:
    database = _database(tmp_path / "provided-relations.sqlite3")
    symlink = tmp_path / "relation-link.sqlite3"
    os.symlink(database, symlink)
    with pytest.raises(OpenDartScopeError, match="symbolic link"):
        build_open_dart_scope_from_provided_relations(symlink)

    with sqlite3.connect(database) as connection:
        connection.execute(
            "UPDATE product_relations SET normalized_entity_label = '다른 회사' "
            "WHERE relation_id = 'r1'"
        )
    with pytest.raises(OpenDartScopeError, match="label differs"):
        build_open_dart_scope_from_provided_relations(database)


def test_scope_model_cannot_be_promoted_by_status_mutation(tmp_path: Path) -> None:
    database = _database(tmp_path / "provided-relations.sqlite3")
    scope = build_open_dart_scope_from_provided_relations(database)
    payload = scope.model_dump(mode="json")
    payload["status"] = "approved_for_runtime"

    with pytest.raises(ValueError):
        OpenDartProvidedRelationScope.model_validate(payload)
