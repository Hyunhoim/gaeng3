from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Callable
from pathlib import Path

import pytest

from finance_agent_core.audit.registry import DATASET_BY_NAME, resolve_inputs
from finance_agent_core.config import load_field_registry
from finance_agent_core.domain import DatabaseManifest
from finance_agent_core.storage import prepare
from finance_agent_core.storage.approval import (
    DatasetApprovalError,
    load_approved_dataset_manifest,
)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write_fake_database(
    data_dir: str | Path,
    output_path: str | Path,
    dataset: str,
) -> DatabaseManifest:
    source_dir = Path(data_dir)
    destination = Path(output_path)
    source, _ = resolve_inputs(source_dir, DATASET_BY_NAME[dataset])
    registry = load_field_registry()
    values: dict[str, object] = {
        "dataset": dataset,
        "registry_schema_version": registry.schema_version,
        "source_file_name": source.name,
        "source_file_sha256": _sha256(source),
        "source_file_size_bytes": source.stat().st_size,
        "source_snapshot_date": registry.datasets[dataset].snapshot_date,
        "total_rows": 1,
        "searchable_rows": 1,
        "quarantined_rows": 0,
    }
    if dataset == "fund":
        values.update(
            schema_version="1.1",
            logical_product_rows=1,
            attribute_rows=1,
            scope_excluded_rows=0,
        )
    manifest = DatabaseManifest.model_validate(values)
    destination.unlink(missing_ok=True)
    with sqlite3.connect(destination) as connection:
        connection.execute("CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        connection.executemany(
            "INSERT INTO metadata (key, value) VALUES (?, ?)",
            [
                (key, json.dumps(value, ensure_ascii=False))
                for key, value in manifest.model_dump(mode="json").items()
            ],
        )
        connection.execute(f"CREATE TABLE {prepare.PRODUCT_TABLES[dataset]} (id INTEGER)")
        connection.execute(f"INSERT INTO {prepare.PRODUCT_TABLES[dataset]} VALUES (1)")
        if dataset == "fund":
            connection.execute("CREATE TABLE fund_attributes (id INTEGER)")
            connection.execute("INSERT INTO fund_attributes VALUES (1)")
            connection.execute("CREATE TABLE fund_quarantine (id INTEGER)")
    destination.with_suffix(".sqlite3.manifest.json").write_text(
        manifest.model_dump_json(indent=2) + "\n",
        encoding="utf-8",
    )
    return manifest


def _raw_directory(tmp_path: Path) -> Path:
    raw = tmp_path / "raw"
    raw.mkdir()
    for dataset in prepare.DATASETS:
        spec = DATASET_BY_NAME[dataset]
        (raw / f"{spec.prefix}_fixture_datarows.xlsx").write_bytes(f"{dataset}-source-v1".encode())
        (raw / f"{spec.prefix}_fixture_schema.xlsx").write_bytes(b"schema")
    return raw


def _fake_builders(calls: list[str]) -> dict[str, Callable[..., DatabaseManifest]]:
    builders: dict[str, Callable[..., DatabaseManifest]] = {}
    for dataset in prepare.DATASETS:

        def builder(
            data_dir: str | Path,
            output_path: str | Path,
            *,
            selected: str = dataset,
        ) -> DatabaseManifest:
            calls.append(selected)
            return _write_fake_database(data_dir, output_path, selected)

        builders[dataset] = builder
    return builders


def test_prepare_builds_once_and_reuses_verified_databases(
    tmp_path: Path,
    monkeypatch,
) -> None:
    raw = _raw_directory(tmp_path)
    output = tmp_path / "normalized"
    calls: list[str] = []
    monkeypatch.setattr(prepare, "BUILDERS", _fake_builders(calls))

    first = prepare.prepare_databases(raw, output)
    second = prepare.prepare_databases(raw, output)

    assert calls == list(prepare.DATASETS)
    assert {item["action"] for item in first["datasets"].values()} == {"built"}
    assert {item["action"] for item in second["datasets"].values()} == {"reused"}
    assert (output / prepare.STATE_FILE_NAME).stat().st_mode & 0o777 == 0o600
    assert output.stat().st_mode & 0o777 == 0o700


def test_prepare_rebuilds_only_the_dataset_whose_source_changed(
    tmp_path: Path,
    monkeypatch,
) -> None:
    raw = _raw_directory(tmp_path)
    output = tmp_path / "normalized"
    calls: list[str] = []
    monkeypatch.setattr(prepare, "BUILDERS", _fake_builders(calls))
    prepare.prepare_databases(raw, output)
    calls.clear()
    overseas_source, _ = resolve_inputs(raw, DATASET_BY_NAME["overseas_etp"])
    overseas_source.write_bytes(b"overseas-source-v2")

    state = prepare.prepare_databases(raw, output)

    assert calls == ["overseas_etp"]
    assert state["datasets"]["overseas_etp"]["action"] == "built"
    assert state["datasets"]["bond"]["action"] == "reused"


def test_prepare_rebuilds_a_database_with_a_tampered_sidecar(
    tmp_path: Path,
    monkeypatch,
) -> None:
    raw = _raw_directory(tmp_path)
    output = tmp_path / "normalized"
    calls: list[str] = []
    monkeypatch.setattr(prepare, "BUILDERS", _fake_builders(calls))
    prepare.prepare_databases(raw, output)
    calls.clear()
    (output / "bond.sqlite3.manifest.json").write_text("{}\n", encoding="utf-8")

    state = prepare.prepare_databases(raw, output)

    assert calls == ["bond"]
    assert state["datasets"]["bond"]["action"] == "built"


def test_prepare_rebuilds_a_valid_sqlite_file_whose_content_changed(
    tmp_path: Path,
    monkeypatch,
) -> None:
    raw = _raw_directory(tmp_path)
    output = tmp_path / "normalized"
    calls: list[str] = []
    monkeypatch.setattr(prepare, "BUILDERS", _fake_builders(calls))
    prepare.prepare_databases(raw, output)
    calls.clear()
    with sqlite3.connect(output / "domestic_etp.sqlite3") as connection:
        connection.execute("UPDATE domestic_etp_products SET id = 2")

    state = prepare.prepare_databases(raw, output)

    assert calls == ["domestic_etp"]
    assert state["datasets"]["domestic_etp"]["action"] == "built"


def test_prepare_rejects_output_inside_raw_data(tmp_path: Path) -> None:
    raw = _raw_directory(tmp_path)

    try:
        prepare.prepare_databases(raw, raw / "normalized")
    except ValueError as error:
        assert "cannot be inside" in str(error)
    else:
        raise AssertionError("raw data safety boundary was not enforced")


def test_prepare_rejects_nonapproved_sources_before_building(
    tmp_path: Path,
    monkeypatch,
) -> None:
    raw = _raw_directory(tmp_path)
    output = tmp_path / "normalized"
    calls: list[str] = []
    monkeypatch.setattr(prepare, "BUILDERS", _fake_builders(calls))

    with pytest.raises(DatasetApprovalError, match="approved competition release"):
        prepare.prepare_databases(
            raw,
            output,
            approval=load_approved_dataset_manifest(),
        )

    assert calls == []


def test_approved_prepare_publishes_relation_state_after_four_databases(
    tmp_path: Path,
    monkeypatch,
) -> None:
    raw = _raw_directory(tmp_path)
    output = tmp_path / "normalized"
    calls: list[str] = []
    relation_calls: list[tuple[Path, object]] = []
    monkeypatch.setattr(prepare, "BUILDERS", _fake_builders(calls))
    monkeypatch.setattr(prepare, "require_approved_source_files", lambda *args, **kwargs: None)
    monkeypatch.setattr(prepare, "require_approved_database", lambda *args, **kwargs: None)

    def fake_prepare_relations(
        output_dir: str | Path,
        *,
        previous_state,
        force: bool,
        verifier,
    ) -> dict[str, object]:
        assert all(
            (Path(output_dir) / f"{dataset}.sqlite3").exists() for dataset in prepare.DATASETS
        )
        assert previous_state is None
        assert force is False
        relation_calls.append((Path(output_dir), verifier))
        return {"action": "built", "index_sha256": "a" * 64}

    monkeypatch.setattr(
        prepare,
        "prepare_relation_retrieval_artifacts",
        fake_prepare_relations,
    )

    state = prepare.prepare_databases(
        raw,
        output,
        approval=load_approved_dataset_manifest(),
    )

    assert calls == list(prepare.DATASETS)
    assert len(relation_calls) == 1
    assert state["relation_retrieval"] == {
        "action": "built",
        "index_sha256": "a" * 64,
    }
