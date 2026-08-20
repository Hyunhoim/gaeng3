from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import stat
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Any

from finance_agent_core.audit.registry import DATASET_BY_NAME, resolve_inputs
from finance_agent_core.config import load_field_registry
from finance_agent_core.contracts.queryplan import ProductFamily
from finance_agent_core.domain import DatabaseManifest
from finance_agent_core.release import (
    AgentReleaseError,
    RelationRetrievalArtifactRelease,
    load_relation_retrieval_artifact_release,
    relation_retrieval_artifact_file_bytes,
)
from finance_agent_core.retrieval.relations import (
    ApprovedProductDatabaseVerifier,
    ProductDatabaseVerifier,
    RelationIndexError,
    RelationIndexManifest,
    SQLiteRelationIndex,
    build_provided_relation_index,
)
from finance_agent_core.storage.approval import (
    ApprovedDatasetManifest,
    DatasetApprovalError,
    load_approved_dataset_manifest,
    require_approved_database,
    require_approved_source_files,
)
from finance_agent_core.storage.bond import build_bond_database
from finance_agent_core.storage.domestic_etp import build_domestic_etp_database
from finance_agent_core.storage.public_fund import build_public_fund_database
from finance_agent_core.storage.sqlite import (
    build_overseas_etp_database,
    connect_read_only,
    load_manifest,
)

PREPARATION_CONTRACT_VERSION = "2"
RELATION_PREPARATION_CONTRACT_VERSION = "1"
STATE_FILE_NAME = ".finance-data-state.json"
DATASETS = ("bond", "domestic_etp", "overseas_etp", "fund")
RELATION_DATASETS = ("bond", "domestic_etp", "overseas_etp")
RELATION_INDEX_FILE_NAME = "provided-relations.sqlite3"
RELATION_ARTIFACT_FILE_NAME = "relation-retrieval-artifact.json"
RELATION_ARTIFACT_SHA256_FILE_NAME = "relation-retrieval-artifact.sha256"

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


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(
        path,
        os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_CLOEXEC", 0),
    )
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _write_read_only_file(path: Path, data: bytes) -> str:
    flags = (
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    descriptor: int | None = None
    completed = False
    try:
        descriptor = os.open(path, flags, 0o400)
        view = memoryview(data)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                raise OSError("relation artifact write made no progress")
            view = view[written:]
        os.fsync(descriptor)
        os.fchmod(descriptor, 0o444)
        completed = True
    finally:
        if descriptor is not None:
            os.close(descriptor)
        if not completed and path.exists() and not path.is_symlink():
            path.unlink()
    return hashlib.sha256(data).hexdigest()


def _relation_database_paths(output_dir: Path) -> dict[ProductFamily, Path]:
    return {
        ProductFamily(dataset): output_dir / f"{dataset}.sqlite3" for dataset in RELATION_DATASETS
    }


def _require_immutable_relation_index(path: Path) -> None:
    current = path
    while True:
        if current.is_symlink():
            raise RelationIndexError("relation index path contains a symbolic link")
        if current.parent == current:
            break
        current = current.parent
    try:
        metadata = path.stat()
    except OSError as error:
        raise RelationIndexError("relation index is unavailable") from error
    if not stat.S_ISREG(metadata.st_mode):
        raise RelationIndexError("relation index must be a regular file")
    if metadata.st_nlink != 1:
        raise RelationIndexError("relation index must have one hard link")
    if metadata.st_mode & (stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH):
        raise RelationIndexError("relation index must be read-only")


def _artifact_sha256_file_bytes(artifact_file_sha256: str) -> bytes:
    if len(artifact_file_sha256) != 64 or any(
        character not in "0123456789abcdef" for character in artifact_file_sha256
    ):
        raise RelationIndexError("relation artifact SHA-256 is invalid")
    return f"{artifact_file_sha256}\n".encode("ascii")


def _read_immutable_artifact_sha256_file(path: Path) -> tuple[str, str]:
    current = path
    while True:
        if current.is_symlink():
            raise RelationIndexError("relation artifact SHA-256 path contains a symbolic link")
        if current.parent == current:
            break
        current = current.parent
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as error:
        raise RelationIndexError("relation artifact SHA-256 file is unavailable") from error
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
            raise RelationIndexError("relation artifact SHA-256 must be a single-linked file")
        if before.st_mode & (stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH):
            raise RelationIndexError("relation artifact SHA-256 file must be read-only")
        if before.st_size != 65:
            raise RelationIndexError("relation artifact SHA-256 file must contain 65 bytes")
        chunks: list[bytes] = []
        remaining = 66
        while remaining:
            chunk = os.read(descriptor, remaining)
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        data = b"".join(chunks)
        after = os.fstat(descriptor)
        identity_before = (
            before.st_dev,
            before.st_ino,
            before.st_size,
            before.st_mtime_ns,
            before.st_ctime_ns,
        )
        identity_after = (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
            after.st_ctime_ns,
        )
        if identity_before != identity_after or len(data) != 65:
            raise RelationIndexError("relation artifact SHA-256 file changed while being read")
    finally:
        os.close(descriptor)
    try:
        digest = data[:64].decode("ascii")
    except UnicodeDecodeError as error:
        raise RelationIndexError("relation artifact SHA-256 file is not ASCII") from error
    if data != _artifact_sha256_file_bytes(digest):
        raise RelationIndexError("relation artifact SHA-256 file is not canonical")
    return digest, hashlib.sha256(data).hexdigest()


def _relation_database_hashes(manifest: RelationIndexManifest) -> dict[str, str]:
    return {
        binding.product_family.value: binding.database_sha256
        for binding in manifest.database_bindings
    }


def _validate_relation_artifacts(
    output_dir: Path,
    state: dict[str, Any],
    *,
    verifier: ProductDatabaseVerifier,
    database_dir: Path | None = None,
) -> tuple[RelationRetrievalArtifactRelease, RelationIndexManifest, str]:
    if state.get("preparation_contract_version") != RELATION_PREPARATION_CONTRACT_VERSION:
        raise RelationIndexError("relation preparation contract differs")
    if state.get("index_file") != RELATION_INDEX_FILE_NAME:
        raise RelationIndexError("relation index file binding differs")
    if state.get("artifact_file") != RELATION_ARTIFACT_FILE_NAME:
        raise RelationIndexError("relation artifact file binding differs")
    if state.get("artifact_sha256_file") != RELATION_ARTIFACT_SHA256_FILE_NAME:
        raise RelationIndexError("relation artifact SHA-256 file binding differs")
    expected_index_sha256 = state.get("index_sha256")
    expected_artifact_sha256 = state.get("artifact_file_sha256")
    expected_artifact_sha256_file_sha256 = state.get("artifact_sha256_file_sha256")
    if (
        not isinstance(expected_index_sha256, str)
        or len(expected_index_sha256) != 64
        or any(character not in "0123456789abcdef" for character in expected_index_sha256)
    ):
        raise RelationIndexError("relation index state SHA-256 is invalid")
    if not isinstance(expected_artifact_sha256, str):
        raise RelationIndexError("relation artifact state SHA-256 is invalid")
    if not isinstance(expected_artifact_sha256_file_sha256, str):
        raise RelationIndexError("relation artifact SHA-256 file state is invalid")

    index_path = output_dir / RELATION_INDEX_FILE_NAME
    artifact_path = output_dir / RELATION_ARTIFACT_FILE_NAME
    artifact_sha256_path = output_dir / RELATION_ARTIFACT_SHA256_FILE_NAME
    _require_immutable_relation_index(index_path)
    anchored_artifact_sha256, artifact_sha256_file_sha256 = _read_immutable_artifact_sha256_file(
        artifact_sha256_path
    )
    if anchored_artifact_sha256 != expected_artifact_sha256:
        raise RelationIndexError("relation artifact SHA-256 anchor differs from prepared state")
    if artifact_sha256_file_sha256 != expected_artifact_sha256_file_sha256:
        raise RelationIndexError("relation artifact SHA-256 file differs from prepared state")
    artifact = load_relation_retrieval_artifact_release(
        artifact_path=artifact_path,
        expected_file_sha256=expected_artifact_sha256,
    )
    manifest, actual_index_sha256 = SQLiteRelationIndex(index_path).verify_runtime(
        _relation_database_paths(database_dir or output_dir),
        verifier=verifier,
    )
    if actual_index_sha256 != expected_index_sha256:
        raise RelationIndexError("relation index differs from prepared state")
    if artifact.index_sha256 != actual_index_sha256:
        raise RelationIndexError("relation artifact index binding differs")
    if artifact.approval_manifest_sha256 != verifier.approval_manifest_sha256:
        raise RelationIndexError("relation artifact approval binding differs")
    if artifact.approval_manifest_sha256 != manifest.approval_manifest_sha256:
        raise RelationIndexError("relation index approval binding differs")
    if artifact.relation_set_sha256 != manifest.relation_set_sha256:
        raise RelationIndexError("relation artifact set binding differs")
    if set(_relation_database_hashes(manifest)) != set(RELATION_DATASETS):
        raise RelationIndexError("relation index must bind exactly three product databases")
    if state.get("database_sha256") != _relation_database_hashes(manifest):
        raise RelationIndexError("relation database state bindings differ")
    return artifact, manifest, artifact_sha256_file_sha256


def _relation_state(
    *,
    action: str,
    artifact: RelationRetrievalArtifactRelease,
    artifact_file_sha256: str,
    artifact_sha256_file_sha256: str,
    manifest: RelationIndexManifest,
) -> dict[str, Any]:
    return {
        "action": action,
        "preparation_contract_version": RELATION_PREPARATION_CONTRACT_VERSION,
        "index_file": RELATION_INDEX_FILE_NAME,
        "index_sha256": artifact.index_sha256,
        "artifact_file": RELATION_ARTIFACT_FILE_NAME,
        "artifact_file_sha256": artifact_file_sha256,
        "artifact_sha256_file": RELATION_ARTIFACT_SHA256_FILE_NAME,
        "artifact_sha256_file_sha256": artifact_sha256_file_sha256,
        "approval_manifest_sha256": artifact.approval_manifest_sha256,
        "relation_set_sha256": artifact.relation_set_sha256,
        "relation_count": manifest.relation_count,
        "database_sha256": _relation_database_hashes(manifest),
    }


def prepare_relation_retrieval_artifacts(
    output_dir: str | Path,
    *,
    previous_state: dict[str, Any] | None,
    force: bool = False,
    verifier: ProductDatabaseVerifier | None = None,
) -> dict[str, Any]:
    """Build or reuse one canonical relation release bound to three product DBs."""

    destination = Path(output_dir).resolve(strict=True)
    active_verifier = verifier or ApprovedProductDatabaseVerifier()
    index_path = destination / RELATION_INDEX_FILE_NAME
    artifact_path = destination / RELATION_ARTIFACT_FILE_NAME
    artifact_sha256_path = destination / RELATION_ARTIFACT_SHA256_FILE_NAME

    if not force and previous_state is not None:
        try:
            artifact, manifest, artifact_sha256_file_sha256 = _validate_relation_artifacts(
                destination,
                previous_state,
                verifier=active_verifier,
            )
        except (AgentReleaseError, RelationIndexError, OSError, ValueError) as error:
            raise RuntimeError(
                "published relation retrieval artifacts failed closed validation"
            ) from error
        return _relation_state(
            action="reused",
            artifact=artifact,
            artifact_file_sha256=_sha256(artifact_path),
            artifact_sha256_file_sha256=artifact_sha256_file_sha256,
            manifest=manifest,
        )

    if not force and any(
        path.exists() or path.is_symlink()
        for path in (index_path, artifact_path, artifact_sha256_path)
    ):
        raise RuntimeError(
            "relation retrieval outputs exist without a validated prepared state; use --force"
        )

    staging_dir = Path(tempfile.mkdtemp(prefix=".relation-retrieval.", dir=destination))
    staged_index = staging_dir / RELATION_INDEX_FILE_NAME
    staged_artifact = staging_dir / RELATION_ARTIFACT_FILE_NAME
    staged_artifact_sha256 = staging_dir / RELATION_ARTIFACT_SHA256_FILE_NAME
    try:
        receipt = build_provided_relation_index(
            _relation_database_paths(destination),
            staged_index,
            verifier=active_verifier,
        )
        artifact = RelationRetrievalArtifactRelease(
            index_sha256=receipt.database_sha256,
            approval_manifest_sha256=receipt.approval_manifest_sha256,
            relation_set_sha256=receipt.relation_set_sha256,
        )
        artifact_file_sha256 = _write_read_only_file(
            staged_artifact,
            relation_retrieval_artifact_file_bytes(artifact),
        )
        artifact_sha256_file_sha256 = _write_read_only_file(
            staged_artifact_sha256,
            _artifact_sha256_file_bytes(artifact_file_sha256),
        )
        staged_state = _relation_state(
            action="built",
            artifact=artifact,
            artifact_file_sha256=artifact_file_sha256,
            artifact_sha256_file_sha256=artifact_sha256_file_sha256,
            manifest=SQLiteRelationIndex(staged_index).manifest(),
        )
        _validate_relation_artifacts(
            staging_dir,
            staged_state,
            verifier=active_verifier,
            database_dir=destination,
        )

        os.replace(staged_index, index_path)
        os.replace(staged_artifact, artifact_path)
        os.replace(staged_artifact_sha256, artifact_sha256_path)
        _fsync_directory(destination)
        _, installed_manifest, installed_sha256_file_sha256 = _validate_relation_artifacts(
            destination,
            staged_state,
            verifier=active_verifier,
        )
        return _relation_state(
            action="built",
            artifact=artifact,
            artifact_file_sha256=artifact_file_sha256,
            artifact_sha256_file_sha256=installed_sha256_file_sha256,
            manifest=installed_manifest,
        )
    finally:
        for staged in (staged_index, staged_artifact, staged_artifact_sha256):
            if staged.exists() and not staged.is_symlink():
                staged.unlink()
        staging_dir.rmdir()


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
    paths = [
        (output_dir, 0o700),
        (output_dir / STATE_FILE_NAME, 0o600),
        (output_dir / RELATION_INDEX_FILE_NAME, 0o444),
        (output_dir / RELATION_ARTIFACT_FILE_NAME, 0o444),
        (output_dir / RELATION_ARTIFACT_SHA256_FILE_NAME, 0o444),
    ]
    for dataset in DATASETS:
        database = output_dir / f"{dataset}.sqlite3"
        paths.extend(
            (
                (database, 0o600),
                (database.with_suffix(".sqlite3.manifest.json"), 0o600),
            )
        )

    if (owner_uid is None) != (owner_gid is None):
        raise ValueError("owner UID and GID must be provided together")
    if owner_uid is not None and (owner_uid < 0 or owner_gid is None or owner_gid < 0):
        raise ValueError("owner UID and GID cannot be negative")

    for path, mode in paths:
        if not path.exists():
            continue
        os.chmod(path, mode)
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
    approval: ApprovedDatasetManifest | None = None,
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
    if approval is not None and approval.registry_schema_version != registry_version:
        raise DatasetApprovalError(
            "approved dataset registry version differs from the active field registry"
        )
    previous_state = _load_state(destination_dir / STATE_FILE_NAME)
    contract_changed = (
        previous_state is None
        or previous_state.get("preparation_contract_version") != PREPARATION_CONTRACT_VERSION
    )
    results: dict[str, dict[str, Any]] = {}

    for dataset in DATASETS:
        source_path, schema_path = resolve_inputs(source_dir, DATASET_BY_NAME[dataset])
        if approval is not None:
            require_approved_source_files(
                dataset,  # type: ignore[arg-type]
                source_path,
                schema_path,
                approval=approval,
            )
        source_sha256 = _sha256(source_path)
        database_path = destination_dir / f"{dataset}.sqlite3"
        ready, reason, _ = validate_prepared_database(
            database_path,
            dataset=dataset,
            source_path=source_path,
            source_sha256=source_sha256,
            registry_schema_version=registry_version,
        )
        if ready and approval is not None:
            try:
                require_approved_database(
                    dataset,  # type: ignore[arg-type]
                    database_path,
                    approval=approval,
                )
            except DatasetApprovalError:
                ready = False
                reason = "database_approval_mismatch"
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
            if approval is not None:
                require_approved_database(
                    dataset,  # type: ignore[arg-type]
                    database_path,
                    approval=approval,
                )
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
    if approval is not None:
        state["approved_release_id"] = approval.release_id
        state["approved_manifest_sha256"] = approval.canonical_sha256
        previous_relation_state = (
            previous_state.get("relation_retrieval") if previous_state is not None else None
        )
        state["relation_retrieval"] = prepare_relation_retrieval_artifacts(
            destination_dir,
            previous_state=(
                previous_relation_state if isinstance(previous_relation_state, dict) else None
            ),
            force=force,
            verifier=ApprovedProductDatabaseVerifier(approval),
        )
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
    approval = load_approved_dataset_manifest()
    state = prepare_databases(
        arguments.data_dir,
        arguments.output_dir,
        force=arguments.force,
        owner_uid=arguments.owner_uid,
        owner_gid=arguments.owner_gid,
        approval=approval,
    )
    print(json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
