from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Any

from finance_agent_core.audit.registry import DATASET_BY_NAME, resolve_inputs
from finance_agent_core.config import load_field_registry
from finance_agent_core.domain import DatabaseManifest
from finance_agent_core.storage.bond import build_bond_database
from finance_agent_core.storage.domestic_etp import build_domestic_etp_database
from finance_agent_core.storage.public_fund import build_public_fund_database
from finance_agent_core.storage.sqlite import (
    build_overseas_etp_database,
    connect_read_only,
    load_manifest,
)

PREPARATION_CONTRACT_VERSION = "1"
STATE_FILE_NAME = ".finance-data-state.json"
DATASETS = ("bond", "domestic_etp", "overseas_etp", "fund")

type DatabaseBuilder = Callable[[str | Path, str | Path], DatabaseManifest]

BUILDERS: dict[str, DatabaseBuilder] = {
    "bond": build_bond_database,
    "domestic_etp": build_domestic_etp_database,
    "overseas_etp": build_overseas_etp_database,
    "fund": build_public_fund_database,
}

PRODUCT_TABLES = {
    "bond": "bond_products",
    "domestic_etp": "domestic_etp_products",
    "overseas_etp": "overseas_etp_products",
    "fund": "fund_products",
}


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _load_state(path: Path) -> dict[str, Any] | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def _write_state(path: Path, payload: dict[str, Any]) -> None:
    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
        delete=False,
    ) as temporary:
        json.dump(payload, temporary, ensure_ascii=False, indent=2, sort_keys=True)
        temporary.write("\n")
        temporary_path = Path(temporary.name)
    temporary_path.replace(path)


def _load_sidecar(path: Path) -> DatabaseManifest:
    sidecar = path.with_suffix(f"{path.suffix}.manifest.json")
    return DatabaseManifest.model_validate_json(sidecar.read_text(encoding="utf-8"))


def validate_prepared_database(
    path: Path,
    *,
    dataset: str,
    source_path: Path,
    source_sha256: str,
    registry_schema_version: str,
) -> tuple[bool, str, DatabaseManifest | None]:
    """Validate identity, provenance and row counts without trusting a sidecar alone."""

    try:
        with connect_read_only(path) as connection:
            manifest = load_manifest(connection)
            if manifest.dataset != dataset:
                return False, "dataset_mismatch", manifest
            if manifest.registry_schema_version != registry_schema_version:
                return False, "registry_version_changed", manifest
            if manifest.source_file_name != source_path.name:
                return False, "source_file_changed", manifest
            if manifest.source_file_size_bytes != source_path.stat().st_size:
                return False, "source_size_changed", manifest
            if manifest.source_file_sha256 != source_sha256:
                return False, "source_hash_changed", manifest

            integrity = connection.execute("PRAGMA quick_check").fetchone()
            if integrity is None or integrity[0] != "ok":
                return False, "sqlite_quick_check_failed", manifest

            product_count = connection.execute(
                f"SELECT COUNT(*) FROM {PRODUCT_TABLES[dataset]}"
            ).fetchone()[0]
            expected_products = (
                manifest.logical_product_rows if dataset == "fund" else manifest.total_rows
            )
            if product_count != expected_products:
                return False, "product_count_mismatch", manifest

            if dataset == "fund":
                attribute_count = connection.execute(
                    "SELECT COUNT(*) FROM fund_attributes"
                ).fetchone()[0]
                quarantine_count = connection.execute(
                    "SELECT COUNT(*) FROM fund_quarantine"
                ).fetchone()[0]
                if attribute_count != manifest.attribute_rows:
                    return False, "fund_attribute_count_mismatch", manifest
                if quarantine_count != manifest.quarantined_rows:
                    return False, "fund_quarantine_count_mismatch", manifest

        sidecar_manifest = _load_sidecar(path)
        if sidecar_manifest != manifest:
            return False, "manifest_sidecar_mismatch", manifest
    except (FileNotFoundError, OSError, sqlite3.Error, ValueError):
        return False, "database_unavailable", None
    return True, "ready", manifest


def _secure_outputs(output_dir: Path, owner_uid: int | None, owner_gid: int | None) -> None:
    paths = [output_dir, output_dir / STATE_FILE_NAME]
    for dataset in DATASETS:
        database = output_dir / f"{dataset}.sqlite3"
        paths.extend((database, database.with_suffix(".sqlite3.manifest.json")))

    if (owner_uid is None) != (owner_gid is None):
        raise ValueError("owner UID and GID must be provided together")
    if owner_uid is not None and (owner_uid < 0 or owner_gid is None or owner_gid < 0):
        raise ValueError("owner UID and GID cannot be negative")

    for path in paths:
        if not path.exists():
            continue
        os.chmod(path, 0o700 if path.is_dir() else 0o600)
        if owner_uid is not None and owner_gid is not None:
            try:
                os.chown(path, owner_uid, owner_gid)
            except PermissionError as error:
                current = path.stat()
                if (current.st_uid, current.st_gid) != (owner_uid, owner_gid):
                    raise PermissionError(
                        f"cannot set output ownership for {path}; run as root or omit owner flags"
                    ) from error


def prepare_databases(
    data_dir: str | Path,
    output_dir: str | Path,
    *,
    force: bool = False,
    owner_uid: int | None = None,
    owner_gid: int | None = None,
) -> dict[str, Any]:
    """Build or reuse all four normalized databases, then publish one ready state."""

    source_dir = Path(data_dir)
    destination_dir = Path(output_dir)
    if not source_dir.is_dir():
        raise FileNotFoundError(f"raw financial-product directory does not exist: {source_dir}")
    if (
        destination_dir.resolve() == source_dir.resolve()
        or source_dir.resolve() in destination_dir.resolve().parents
    ):
        raise ValueError("normalized output directory cannot be inside the raw data directory")
    destination_dir.mkdir(parents=True, exist_ok=True)

    registry_version = load_field_registry().schema_version
    previous_state = _load_state(destination_dir / STATE_FILE_NAME)
    contract_changed = (
        previous_state is None
        or previous_state.get("preparation_contract_version") != PREPARATION_CONTRACT_VERSION
    )
    results: dict[str, dict[str, Any]] = {}

    for dataset in DATASETS:
        source_path, _ = resolve_inputs(source_dir, DATASET_BY_NAME[dataset])
        source_sha256 = _sha256(source_path)
        database_path = destination_dir / f"{dataset}.sqlite3"
        ready, reason, _ = validate_prepared_database(
            database_path,
            dataset=dataset,
            source_path=source_path,
            source_sha256=source_sha256,
            registry_schema_version=registry_version,
        )
        previous_datasets = previous_state.get("datasets", {}) if previous_state else {}
        previous_dataset = (
            previous_datasets.get(dataset, {}) if isinstance(previous_datasets, dict) else {}
        )
        expected_database_sha256 = (
            previous_dataset.get("database_sha256") if isinstance(previous_dataset, dict) else None
        )
        if (
            ready
            and not contract_changed
            and not force
            and (
                not isinstance(expected_database_sha256, str)
                or _sha256(database_path) != expected_database_sha256
            )
        ):
            ready = False
            reason = "database_hash_changed"
        rebuild = force or contract_changed or not ready
        if rebuild:
            manifest = BUILDERS[dataset](source_dir, database_path)
            ready, reason, manifest = validate_prepared_database(
                database_path,
                dataset=dataset,
                source_path=source_path,
                source_sha256=source_sha256,
                registry_schema_version=registry_version,
            )
            if not ready or manifest is None:
                raise RuntimeError(f"prepared {dataset} database failed validation: {reason}")
            action = "built"
        else:
            action = "reused"

        results[dataset] = {
            "action": action,
            "database": database_path.name,
            "database_sha256": _sha256(database_path),
            "source_file": source_path.name,
            "source_sha256": source_sha256,
        }

    state: dict[str, Any] = {
        "schema_version": "1.0",
        "preparation_contract_version": PREPARATION_CONTRACT_VERSION,
        "registry_schema_version": registry_version,
        "datasets": results,
    }
    _write_state(destination_dir / STATE_FILE_NAME, state)
    _secure_outputs(destination_dir, owner_uid, owner_gid)
    return state


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Prepare and verify all normalized financial-product databases."
    )
    parser.add_argument("--data-dir", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--owner-uid", type=int)
    parser.add_argument("--owner-gid", type=int)
    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = build_parser().parse_args(argv)
    state = prepare_databases(
        arguments.data_dir,
        arguments.output_dir,
        force=arguments.force,
        owner_uid=arguments.owner_uid,
        owner_gid=arguments.owner_gid,
    )
    print(json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
