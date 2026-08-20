from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path

import pytest

from finance_agent_core.config import load_field_registry
from finance_agent_core.contracts.queryplan import ProductFamily
from finance_agent_core.domain import DatabaseManifest
from finance_agent_core.release import load_relation_retrieval_artifact_release
from finance_agent_core.retrieval.relations import (
    RelationIndexError,
    VerifiedProductDatabase,
)
from finance_agent_core.storage.approval import sha256_file
from finance_agent_core.storage.identity_cache import load_product_identities
from finance_agent_core.storage.prepare import (
    RELATION_ARTIFACT_FILE_NAME,
    RELATION_ARTIFACT_SHA256_FILE_NAME,
    RELATION_DATASETS,
    RELATION_INDEX_FILE_NAME,
    prepare_relation_retrieval_artifacts,
)


class SyntheticProductDatabaseVerifier:
    def __init__(self) -> None:
        self.calls: list[tuple[ProductFamily, Path]] = []

    @property
    def approval_manifest_sha256(self) -> str:
        return "f" * 64

    def verify(
        self,
        product_family: ProductFamily,
        path: str | Path,
    ) -> VerifiedProductDatabase:
        resolved = Path(path).resolve(strict=True)
        manifest, identities = load_product_identities(resolved)
        if manifest.dataset != product_family.value:
            raise RelationIndexError("synthetic verifier family mismatch")
        self.calls.append((product_family, resolved))
        return VerifiedProductDatabase(
            product_family=product_family,
            path=resolved,
            manifest=manifest,
            database_sha256=sha256_file(resolved),
            identities=identities,
        )


def _write_metadata(
    connection: sqlite3.Connection,
    manifest: DatabaseManifest,
) -> None:
    connection.execute("CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
    connection.executemany(
        "INSERT INTO metadata (key, value) VALUES (?, ?)",
        [
            (key, json.dumps(value, ensure_ascii=False))
            for key, value in manifest.model_dump(mode="json").items()
        ],
    )


def _write_relation_source_database(output_dir: Path, dataset: str) -> None:
    path = output_dir / f"{dataset}.sqlite3"
    manifest = DatabaseManifest(
        dataset=dataset,  # type: ignore[arg-type]
        registry_schema_version=load_field_registry().schema_version,
        source_file_name=f"synthetic-{dataset}.xlsx",
        source_file_sha256=dataset.encode().hex().ljust(64, "0")[:64],
        source_file_size_bytes=100,
        source_snapshot_date="2026-07-11",
        total_rows=1,
        searchable_rows=1,
        quarantined_rows=0,
    )
    with sqlite3.connect(path) as connection:
        _write_metadata(connection, manifest)
        if dataset == "bond":
            connection.execute(
                """
                CREATE TABLE bond_products (
                    product_family TEXT, product_id TEXT, product_name TEXT,
                    ticker TEXT, short_name TEXT, is_quarantined INTEGER,
                    source_row INTEGER, static_as_of TEXT, issuer TEXT,
                    issuer_quality TEXT
                )
                """
            )
            connection.execute(
                "INSERT INTO bond_products VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    dataset,
                    "bond-1",
                    "Synthetic Bond",
                    "BOND1",
                    "Bond",
                    0,
                    2,
                    "2026-07-11",
                    "Synthetic Issuer",
                    "VALID",
                ),
            )
        elif dataset == "domestic_etp":
            connection.execute(
                """
                CREATE TABLE domestic_etp_products (
                    product_family TEXT, product_id TEXT, product_name TEXT,
                    ticker TEXT, isin TEXT, short_name TEXT, is_quarantined INTEGER,
                    source_row INTEGER, static_as_of TEXT, manager TEXT,
                    base_index TEXT, base_index_quality TEXT, asset_type TEXT,
                    investment_region TEXT
                )
                """
            )
            connection.execute(
                "INSERT INTO domestic_etp_products VALUES "
                "(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    dataset,
                    "domestic-etp-1",
                    "Synthetic Domestic ETF",
                    "D001",
                    "KR0000000001",
                    "Domestic",
                    0,
                    2,
                    "2026-07-11",
                    "Synthetic Manager",
                    "Synthetic Index",
                    "VALID",
                    "Equity",
                    "Korea",
                ),
            )
        else:
            connection.execute(
                """
                CREATE TABLE overseas_etp_products (
                    product_family TEXT, product_id TEXT, product_name TEXT,
                    ticker TEXT, isin TEXT, is_quarantined INTEGER,
                    source_row INTEGER, static_as_of TEXT, asset_type TEXT,
                    investment_region TEXT
                )
                """
            )
            connection.execute(
                "INSERT INTO overseas_etp_products VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    dataset,
                    "overseas-etp-1",
                    "Synthetic Overseas ETF",
                    "O001",
                    "US0000000001",
                    0,
                    2,
                    "2026-07-11",
                    "Equity",
                    "United States",
                ),
            )
        connection.commit()


@pytest.fixture
def relation_output(tmp_path: Path) -> tuple[Path, SyntheticProductDatabaseVerifier]:
    output = tmp_path / "normalized"
    output.mkdir()
    for dataset in RELATION_DATASETS:
        _write_relation_source_database(output, dataset)
    return output, SyntheticProductDatabaseVerifier()


def test_relation_prepare_builds_canonical_artifact_and_reuses_it(
    relation_output: tuple[Path, SyntheticProductDatabaseVerifier],
) -> None:
    output, verifier = relation_output

    first = prepare_relation_retrieval_artifacts(
        output,
        previous_state=None,
        verifier=verifier,
    )
    second = prepare_relation_retrieval_artifacts(
        output,
        previous_state=first,
        verifier=verifier,
    )

    assert first["action"] == "built"
    assert second == {**first, "action": "reused"}
    assert set(first["database_sha256"]) == set(RELATION_DATASETS)
    assert (output / RELATION_INDEX_FILE_NAME).stat().st_mode & 0o222 == 0
    assert (output / RELATION_ARTIFACT_FILE_NAME).stat().st_mode & 0o222 == 0
    anchor_path = output / RELATION_ARTIFACT_SHA256_FILE_NAME
    assert anchor_path.stat().st_mode & 0o222 == 0
    assert anchor_path.stat().st_size == 65
    assert anchor_path.read_bytes() == f"{first['artifact_file_sha256']}\n".encode()
    assert first["artifact_sha256_file"] == RELATION_ARTIFACT_SHA256_FILE_NAME
    assert first["artifact_sha256_file_sha256"] == sha256_file(anchor_path)
    artifact = load_relation_retrieval_artifact_release(
        artifact_path=(output / RELATION_ARTIFACT_FILE_NAME).resolve(),
        expected_file_sha256=first["artifact_file_sha256"],
    )
    assert artifact.index_sha256 == first["index_sha256"]
    assert not list(output.glob(".relation-retrieval.*"))


@pytest.mark.parametrize(
    "file_name",
    [
        RELATION_INDEX_FILE_NAME,
        RELATION_ARTIFACT_FILE_NAME,
        RELATION_ARTIFACT_SHA256_FILE_NAME,
    ],
)
def test_relation_prepare_fails_closed_when_published_artifact_becomes_writable(
    relation_output: tuple[Path, SyntheticProductDatabaseVerifier],
    file_name: str,
) -> None:
    output, verifier = relation_output
    state = prepare_relation_retrieval_artifacts(
        output,
        previous_state=None,
        verifier=verifier,
    )
    os.chmod(output / file_name, 0o644)

    with pytest.raises(RuntimeError, match="failed closed"):
        prepare_relation_retrieval_artifacts(
            output,
            previous_state=state,
            verifier=verifier,
        )


def test_relation_prepare_fails_closed_when_sha256_anchor_content_changes(
    relation_output: tuple[Path, SyntheticProductDatabaseVerifier],
) -> None:
    output, verifier = relation_output
    state = prepare_relation_retrieval_artifacts(
        output,
        previous_state=None,
        verifier=verifier,
    )
    anchor = output / RELATION_ARTIFACT_SHA256_FILE_NAME
    os.chmod(anchor, 0o644)
    anchor.write_bytes(("0" * 64 + "\n").encode())
    os.chmod(anchor, 0o444)

    with pytest.raises(RuntimeError, match="failed closed"):
        prepare_relation_retrieval_artifacts(
            output,
            previous_state=state,
            verifier=verifier,
        )


def test_relation_prepare_fails_closed_when_bound_product_database_changes(
    relation_output: tuple[Path, SyntheticProductDatabaseVerifier],
) -> None:
    output, verifier = relation_output
    state = prepare_relation_retrieval_artifacts(
        output,
        previous_state=None,
        verifier=verifier,
    )
    with sqlite3.connect(output / "bond.sqlite3") as connection:
        connection.execute("UPDATE bond_products SET product_name = 'Tampered Bond'")

    with pytest.raises(RuntimeError, match="failed closed"):
        prepare_relation_retrieval_artifacts(
            output,
            previous_state=state,
            verifier=verifier,
        )


def test_relation_prepare_force_replaces_a_complete_release_atomically(
    relation_output: tuple[Path, SyntheticProductDatabaseVerifier],
) -> None:
    output, verifier = relation_output
    first = prepare_relation_retrieval_artifacts(
        output,
        previous_state=None,
        verifier=verifier,
    )
    previous_inodes = {
        file_name: (output / file_name).stat().st_ino
        for file_name in (
            RELATION_INDEX_FILE_NAME,
            RELATION_ARTIFACT_FILE_NAME,
            RELATION_ARTIFACT_SHA256_FILE_NAME,
        )
    }

    rebuilt = prepare_relation_retrieval_artifacts(
        output,
        previous_state=first,
        force=True,
        verifier=verifier,
    )

    assert rebuilt == first
    assert all(
        (output / file_name).stat().st_ino != inode for file_name, inode in previous_inodes.items()
    )
    assert not list(output.glob(".relation-retrieval.*"))
