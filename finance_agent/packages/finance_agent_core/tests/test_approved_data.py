from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from datetime import date
from pathlib import Path

import pytest

from finance_agent_core.domain import DatabaseManifest
from finance_agent_core.storage import approval as approval_module
from finance_agent_core.storage.approval import (
    ApprovedDatasetManifest,
    DatasetApprovalError,
    load_approved_dataset_manifest,
    require_approved_database,
    require_approved_database_paths,
    require_approved_source_files,
)


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _release_with(
    release: ApprovedDatasetManifest,
    dataset: str,
    **updates: object,
) -> ApprovedDatasetManifest:
    item = release.datasets[dataset].model_copy(update=updates)
    return release.model_copy(update={"datasets": {**release.datasets, dataset: item}})


def _write_manifest_database(path: Path, manifest: DatabaseManifest) -> None:
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        connection.executemany(
            "INSERT INTO metadata (key, value) VALUES (?, ?)",
            [
                (key, json.dumps(value, ensure_ascii=False))
                for key, value in manifest.model_dump(mode="json").items()
            ],
        )


class _BondAliasKey:
    value = "bond"


def test_packaged_approval_is_complete_and_bound_to_registry() -> None:
    release = load_approved_dataset_manifest()

    assert release.status == "official_competition_data_approved"
    assert release.registry_schema_version == "1.3"
    assert set(release.datasets) == {"bond", "domestic_etp", "overseas_etp", "fund"}
    assert len(release.canonical_sha256) == 64
    assert release.datasets["fund"].logical_product_rows == 11138


def test_source_approval_checks_both_data_and_schema_workbooks(tmp_path: Path) -> None:
    release = load_approved_dataset_manifest()
    data_payload = b"approved-data"
    schema_payload = b"approved-schema"
    data_path = tmp_path / "PRBD01N001_fixture_datarows.xlsx"
    schema_path = tmp_path / "PRBD01N001_fixture_schema.xlsx"
    data_path.write_bytes(data_payload)
    schema_path.write_bytes(schema_payload)
    release = _release_with(
        release,
        "bond",
        data_file_size_bytes=len(data_payload),
        data_file_sha256=_sha256(data_payload),
        schema_file_size_bytes=len(schema_payload),
        schema_file_sha256=_sha256(schema_payload),
    )

    require_approved_source_files("bond", data_path, schema_path, approval=release)

    schema_path.write_bytes(b"tampered-schema")
    with pytest.raises(DatasetApprovalError, match="schema workbook"):
        require_approved_source_files("bond", data_path, schema_path, approval=release)


def test_runtime_database_requires_manifest_and_exact_release_hash(tmp_path: Path) -> None:
    release = load_approved_dataset_manifest()
    source_sha256 = "a" * 64
    manifest = DatabaseManifest(
        schema_version="1.0",
        dataset="bond",
        registry_schema_version=release.registry_schema_version,
        source_file_name="approved.xlsx",
        source_file_sha256=source_sha256,
        source_file_size_bytes=1,
        source_snapshot_date=date(2026, 7, 11),
        total_rows=1,
        searchable_rows=1,
        quarantined_rows=0,
    )
    database_path = tmp_path / "bond.sqlite3"
    _write_manifest_database(database_path, manifest)
    database_payload = database_path.read_bytes()
    release = _release_with(
        release,
        "bond",
        data_file_sha256=source_sha256,
        data_file_size_bytes=1,
        database_file_size_bytes=len(database_payload),
        database_sha256=_sha256(database_payload),
        source_snapshot_date=date(2026, 7, 11),
        total_rows=1,
        searchable_rows=1,
        quarantined_rows=0,
    )

    approved = require_approved_database("bond", database_path, approval=release)
    assert approved == manifest

    wal_path = Path(f"{database_path}-wal")
    wal_path.write_bytes(b"unapproved-sidecar")
    with pytest.raises(DatasetApprovalError, match="unapproved SQLite sidecar"):
        require_approved_database("bond", database_path, approval=release)
    wal_path.unlink()

    with database_path.open("ab") as stream:
        stream.write(b"tampered")
    with pytest.raises(DatasetApprovalError, match="size differs"):
        require_approved_database("bond", database_path, approval=release)


def test_runtime_database_rejects_forged_manifest_even_without_file_hash(tmp_path: Path) -> None:
    release = load_approved_dataset_manifest()
    database_path = tmp_path / "bond.sqlite3"
    manifest = DatabaseManifest(
        dataset="bond",
        registry_schema_version=release.registry_schema_version,
        source_file_name="unapproved.xlsx",
        source_file_sha256="b" * 64,
        source_file_size_bytes=1,
        source_snapshot_date=date(2026, 7, 11),
        total_rows=release.datasets["bond"].total_rows,
        searchable_rows=release.datasets["bond"].searchable_rows,
        quarantined_rows=release.datasets["bond"].quarantined_rows,
    )
    _write_manifest_database(database_path, manifest)

    with pytest.raises(DatasetApprovalError, match="source_file_sha256"):
        require_approved_database(
            "bond",
            database_path,
            approval=release,
            verify_database_hash=False,
        )


def test_packaged_database_cache_invalidates_same_size_same_mtime_tamper(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    release = load_approved_dataset_manifest()
    source_sha256 = "c" * 64
    manifest = DatabaseManifest(
        schema_version="1.0",
        dataset="bond",
        registry_schema_version=release.registry_schema_version,
        source_file_name="approved.xlsx",
        source_file_sha256=source_sha256,
        source_file_size_bytes=1,
        source_snapshot_date=date(2026, 7, 11),
        total_rows=1,
        searchable_rows=1,
        quarantined_rows=0,
    )
    database_path = tmp_path / "bond.sqlite3"
    _write_manifest_database(database_path, manifest)
    original_stat = database_path.stat()
    original_payload = database_path.read_bytes()
    release = _release_with(
        release,
        "bond",
        data_file_sha256=source_sha256,
        data_file_size_bytes=1,
        database_file_size_bytes=len(original_payload),
        database_sha256=_sha256(original_payload),
        source_snapshot_date=date(2026, 7, 11),
        total_rows=1,
        searchable_rows=1,
        quarantined_rows=0,
    )
    monkeypatch.setattr(approval_module, "load_approved_dataset_manifest", lambda: release)
    approval_module._require_packaged_database_cached.cache_clear()

    assert require_approved_database("bond", database_path) == manifest

    sidecar = Path(f"{database_path}-shm")
    sidecar.write_bytes(b"cache-bypass-attempt")
    with pytest.raises(DatasetApprovalError, match="unapproved SQLite sidecar"):
        require_approved_database("bond", database_path)
    sidecar.unlink()

    tampered = original_payload.replace(b"approved.xlsx", b"tampered.xlsx")
    assert len(tampered) == len(original_payload)
    assert tampered != original_payload
    replacement_path = tmp_path / "replacement.sqlite3"
    replacement_path.write_bytes(tampered)
    os.utime(
        replacement_path,
        ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns),
    )
    replacement_path.replace(database_path)
    assert database_path.stat().st_ino != original_stat.st_ino

    with pytest.raises(DatasetApprovalError):
        require_approved_database("bond", database_path)
    approval_module._require_packaged_database_cached.cache_clear()


@pytest.mark.parametrize(
    "paths",
    [
        {
            "bond": Path("bond.sqlite3"),
            "domestic_etp": Path("domestic.sqlite3"),
            "overseas_etp": Path("overseas.sqlite3"),
        },
        {
            "bond": Path("bond.sqlite3"),
            "domestic_etp": Path("domestic.sqlite3"),
            "overseas_etp": Path("overseas.sqlite3"),
            "fund": Path("fund.sqlite3"),
            "crypto": Path("crypto.sqlite3"),
        },
        {
            _BondAliasKey(): Path("bond.sqlite3"),
            "bond": Path("second-bond.sqlite3"),
            "domestic_etp": Path("domestic.sqlite3"),
            "overseas_etp": Path("overseas.sqlite3"),
            "fund": Path("fund.sqlite3"),
        },
    ],
    ids=["missing", "unexpected", "normalized-duplicate"],
)
def test_approved_database_paths_require_exactly_four_unique_datasets(
    paths: dict[object, Path],
) -> None:
    with pytest.raises(DatasetApprovalError, match="exactly four"):
        require_approved_database_paths(paths)


def test_database_hash_race_is_normalized_to_approval_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    release = load_approved_dataset_manifest()
    manifest = DatabaseManifest(
        schema_version="1.0",
        dataset="bond",
        registry_schema_version=release.registry_schema_version,
        source_file_name="approved.xlsx",
        source_file_sha256="d" * 64,
        source_file_size_bytes=1,
        source_snapshot_date=date(2026, 7, 11),
        total_rows=1,
        searchable_rows=1,
        quarantined_rows=0,
    )
    database_path = tmp_path / "bond.sqlite3"
    _write_manifest_database(database_path, manifest)
    payload = database_path.read_bytes()
    release = _release_with(
        release,
        "bond",
        data_file_sha256="d" * 64,
        data_file_size_bytes=1,
        database_file_size_bytes=len(payload),
        database_sha256=_sha256(payload),
        source_snapshot_date=date(2026, 7, 11),
        total_rows=1,
        searchable_rows=1,
        quarantined_rows=0,
    )

    def disappear(_: str | Path) -> str:
        raise FileNotFoundError("simulated replacement race")

    monkeypatch.setattr(approval_module, "sha256_file", disappear)

    with pytest.raises(DatasetApprovalError, match="became unavailable"):
        require_approved_database("bond", database_path, approval=release)
