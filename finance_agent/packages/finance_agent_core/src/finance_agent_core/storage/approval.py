from __future__ import annotations

import hashlib
import json
import os
from datetime import date
from functools import lru_cache
from importlib.resources import files
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from finance_agent_core.config import load_field_registry
from finance_agent_core.domain import DatabaseManifest
from finance_agent_core.storage.sqlite import connect_read_only, load_manifest

DatasetName = Literal["bond", "domestic_etp", "overseas_etp", "fund"]
_DATASETS = frozenset({"bond", "domestic_etp", "overseas_etp", "fund"})
_SHA256_PATTERN = r"^[0-9a-f]{64}$"


class ApprovalModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ApprovedDataset(ApprovalModel):
    source_id: str = Field(min_length=5, max_length=32)
    manifest_schema_version: Literal["1.0", "1.1"]
    data_file_size_bytes: int = Field(gt=0)
    data_file_sha256: str = Field(pattern=_SHA256_PATTERN)
    schema_file_size_bytes: int = Field(gt=0)
    schema_file_sha256: str = Field(pattern=_SHA256_PATTERN)
    database_file_size_bytes: int = Field(gt=0)
    database_sha256: str = Field(pattern=_SHA256_PATTERN)
    source_snapshot_date: date
    total_rows: int = Field(gt=0)
    searchable_rows: int = Field(ge=0)
    quarantined_rows: int = Field(ge=0)
    logical_product_rows: int | None = Field(default=None, gt=0)
    attribute_rows: int | None = Field(default=None, ge=0)
    scope_excluded_rows: int | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def validate_counts(self) -> ApprovedDataset:
        if self.manifest_schema_version == "1.0":
            if any(
                value is not None
                for value in (
                    self.logical_product_rows,
                    self.attribute_rows,
                    self.scope_excluded_rows,
                )
            ):
                raise ValueError("schema 1.0 approval cannot contain fund-only counts")
            if self.searchable_rows + self.quarantined_rows != self.total_rows:
                raise ValueError("searchable and quarantined rows must equal total rows")
            return self
        if (
            self.logical_product_rows is None
            or self.attribute_rows is None
            or self.scope_excluded_rows is None
        ):
            raise ValueError("schema 1.1 approval requires all fund counts")
        if self.attribute_rows + self.quarantined_rows != self.total_rows:
            raise ValueError("fund attributes and quarantined rows must equal total rows")
        if self.searchable_rows + self.scope_excluded_rows != self.logical_product_rows:
            raise ValueError("fund searchable and excluded rows must equal logical products")
        return self


class ApprovedDatasetManifest(ApprovalModel):
    schema_version: Literal["1.0"]
    release_id: str = Field(pattern=r"^[a-z0-9][a-z0-9._-]{7,127}$")
    status: Literal["official_competition_data_approved"]
    registry_schema_version: str = Field(min_length=1, max_length=32)
    datasets: dict[DatasetName, ApprovedDataset]

    @model_validator(mode="after")
    def validate_complete_release(self) -> ApprovedDatasetManifest:
        if set(self.datasets) != _DATASETS:
            raise ValueError("approval manifest must contain exactly the four product datasets")
        if self.datasets["fund"].manifest_schema_version != "1.1":
            raise ValueError("fund approval must use database manifest schema 1.1")
        if any(
            item.manifest_schema_version != "1.0"
            for name, item in self.datasets.items()
            if name != "fund"
        ):
            raise ValueError("non-fund approvals must use database manifest schema 1.0")
        return self

    @property
    def canonical_sha256(self) -> str:
        payload = json.dumps(
            self.model_dump(mode="json"),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(payload).hexdigest()


class DatasetApprovalError(RuntimeError):
    """Raised when a source workbook or normalized DB is not the approved release."""


@lru_cache(maxsize=1)
def load_approved_dataset_manifest() -> ApprovedDatasetManifest:
    resource = files("finance_agent_core.config").joinpath("approved_dataset_manifest.json")
    manifest = ApprovedDatasetManifest.model_validate_json(resource.read_text(encoding="utf-8"))
    registry = load_field_registry()
    if manifest.registry_schema_version != registry.schema_version:
        raise DatasetApprovalError(
            "approved dataset registry version differs from the packaged field registry"
        )
    for dataset, approved in manifest.datasets.items():
        definition = registry.datasets[dataset]
        if approved.source_id != definition.source_id:
            raise DatasetApprovalError(f"approved source id differs for {dataset}")
        if approved.source_snapshot_date != definition.snapshot_date:
            raise DatasetApprovalError(f"approved snapshot date differs for {dataset}")
        if approved.total_rows != definition.row_count:
            raise DatasetApprovalError(f"approved row count differs for {dataset}")
        if approved.quarantined_rows != definition.quarantined_rows:
            raise DatasetApprovalError(f"approved quarantine count differs for {dataset}")
        if approved.logical_product_rows != definition.logical_row_count:
            raise DatasetApprovalError(f"approved logical row count differs for {dataset}")
    return manifest


def _sha256_uncached(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def sha256_file(path: str | Path) -> str:
    """Hash the current bytes without trusting mutable filesystem metadata.

    Size and mtime are useful cache hints, but an in-place replacement can
    preserve both.  Approval is a security boundary, so every explicit hash
    check reads the file again.  The higher-level packaged DB validator still
    caches a completed approval result for an unchanged read-only deployment.
    """

    resolved = Path(path).resolve(strict=True)
    return _sha256_uncached(resolved)


def require_approved_source_files(
    dataset: DatasetName,
    data_path: str | Path,
    schema_path: str | Path,
    *,
    approval: ApprovedDatasetManifest | None = None,
) -> None:
    approved = (approval or load_approved_dataset_manifest()).datasets[dataset]
    data = Path(data_path)
    schema = Path(schema_path)
    checks = {
        "data workbook size": data.stat().st_size == approved.data_file_size_bytes,
        "data workbook SHA-256": sha256_file(data) == approved.data_file_sha256,
        "schema workbook size": schema.stat().st_size == approved.schema_file_size_bytes,
        "schema workbook SHA-256": sha256_file(schema) == approved.schema_file_sha256,
    }
    failed = [name for name, passed in checks.items() if not passed]
    if failed:
        raise DatasetApprovalError(
            f"{dataset} source is not the approved competition release: {', '.join(failed)}"
        )


def _manifest_differences(
    manifest: DatabaseManifest,
    approved: ApprovedDataset,
    dataset: DatasetName,
    registry_schema_version: str,
) -> list[str]:
    expected = {
        "dataset": dataset,
        "schema_version": approved.manifest_schema_version,
        "registry_schema_version": registry_schema_version,
        "source_file_sha256": approved.data_file_sha256,
        "source_file_size_bytes": approved.data_file_size_bytes,
        "source_snapshot_date": approved.source_snapshot_date,
        "total_rows": approved.total_rows,
        "searchable_rows": approved.searchable_rows,
        "quarantined_rows": approved.quarantined_rows,
        "logical_product_rows": approved.logical_product_rows,
        "attribute_rows": approved.attribute_rows,
        "scope_excluded_rows": approved.scope_excluded_rows,
    }
    return [name for name, value in expected.items() if getattr(manifest, name) != value]


def _reject_database_sidecars(path: Path) -> None:
    """Reject auxiliary files outside the pinned immutable DB artifact."""

    sidecars = [Path(f"{path}{suffix}") for suffix in ("-wal", "-shm", "-journal")]
    if any(os.path.lexists(sidecar) for sidecar in sidecars):
        raise DatasetApprovalError(f"{path.name} has an unapproved SQLite sidecar")


def _require_approved_database_uncached(
    dataset: DatasetName,
    database_path: Path,
    *,
    approval: ApprovedDatasetManifest,
    verify_database_hash: bool = True,
) -> DatabaseManifest:
    approved = approval.datasets[dataset]
    path = database_path
    _reject_database_sidecars(path)
    try:
        with connect_read_only(path) as connection:
            manifest = load_manifest(connection)
            integrity = connection.execute("PRAGMA quick_check").fetchone()
    except Exception as error:  # noqa: BLE001 - normalize all untrusted DB failures
        raise DatasetApprovalError(f"{dataset} approved database is unavailable") from error
    differences = _manifest_differences(
        manifest,
        approved,
        dataset,
        approval.registry_schema_version,
    )
    if differences:
        raise DatasetApprovalError(
            f"{dataset} database manifest differs from approval: {', '.join(differences)}"
        )
    if integrity is None or integrity[0] != "ok":
        raise DatasetApprovalError(f"{dataset} database integrity check failed")
    if verify_database_hash:
        try:
            size = path.stat().st_size
            digest = sha256_file(path)
        except OSError as error:
            raise DatasetApprovalError(f"{dataset} approved database became unavailable") from error
        if size != approved.database_file_size_bytes:
            raise DatasetApprovalError(f"{dataset} database size differs from approval")
        if digest != approved.database_sha256:
            raise DatasetApprovalError(f"{dataset} database SHA-256 differs from approval")
    return manifest


@lru_cache(maxsize=16)
def _require_packaged_database_cached(
    dataset: DatasetName,
    resolved_path: str,
    size: int,
    modified_ns: int,
    changed_ns: int,
    device: int,
    inode: int,
    verify_database_hash: bool,
) -> DatabaseManifest:
    del size, modified_ns, changed_ns, device, inode
    return _require_approved_database_uncached(
        dataset,
        Path(resolved_path),
        approval=load_approved_dataset_manifest(),
        verify_database_hash=verify_database_hash,
    )


def require_approved_database(
    dataset: DatasetName,
    database_path: str | Path,
    *,
    approval: ApprovedDatasetManifest | None = None,
    verify_database_hash: bool = True,
) -> DatabaseManifest:
    path = Path(database_path)
    if approval is not None:
        try:
            path = path.resolve(strict=True)
        except OSError as error:
            raise DatasetApprovalError(f"{dataset} approved database is unavailable") from error
        return _require_approved_database_uncached(
            dataset,
            path,
            approval=approval,
            verify_database_hash=verify_database_hash,
        )
    try:
        resolved = path.resolve(strict=True)
        stat = resolved.stat()
    except OSError as error:
        raise DatasetApprovalError(f"{dataset} approved database is unavailable") from error
    _reject_database_sidecars(resolved)
    return _require_packaged_database_cached(
        dataset,
        str(resolved),
        stat.st_size,
        stat.st_mtime_ns,
        stat.st_ctime_ns,
        stat.st_dev,
        stat.st_ino,
        verify_database_hash,
    )


def require_approved_database_paths(
    database_paths: dict[object, str | Path],
    *,
    approval: ApprovedDatasetManifest | None = None,
) -> None:
    normalized_items = [
        ((key.value if hasattr(key, "value") else str(key)), value)
        for key, value in database_paths.items()
    ]
    normalized = dict(normalized_items)
    missing = _DATASETS - set(normalized)
    unexpected = set(normalized) - _DATASETS
    duplicate = len(normalized) != len(normalized_items)
    if missing or unexpected or duplicate:
        details: list[str] = []
        if missing:
            details.append(f"missing={','.join(sorted(missing))}")
        if unexpected:
            details.append(f"unexpected={','.join(sorted(unexpected))}")
        if duplicate:
            details.append("duplicate=normalized_dataset_key")
        raise DatasetApprovalError(
            "approved deployment must contain exactly four product datasets: " + "; ".join(details)
        )
    for dataset in sorted(_DATASETS):
        require_approved_database(
            dataset,  # type: ignore[arg-type]
            normalized[dataset],
            approval=approval,
        )
