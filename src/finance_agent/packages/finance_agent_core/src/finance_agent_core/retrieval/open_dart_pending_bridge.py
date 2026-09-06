from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from finance_agent_core.config.knowledge_coverage import KnowledgeCapability
from finance_agent_core.config.source_registry import (
    OfficialSourceRegistry,
    OfficialSourceRegistryError,
)
from finance_agent_core.retrieval.corpus import DocumentPurpose
from finance_agent_core.retrieval.open_dart import (
    OpenDartFilingWhitelist,
    OpenDartPendingCorpusManifest,
    open_dart_file_references,
    open_dart_manifest_bytes,
    select_open_dart_disclosures,
)
from finance_agent_core.retrieval.pending_corpus import (
    PendingCorpusManifest,
    PendingCorpusSnapshot,
    pending_corpus_manifest_bytes,
)

_SHA256_PATTERN = r"^[0-9a-f]{64}$"
_OPEN_DART_SOURCE_ID = "open-dart-disclosures"
_OPEN_DART_PUBLISHER = "금융감독원"
_MAX_MANIFEST_BYTES = 64 * 1024 * 1024
_MAX_FILE_BYTES = 64 * 1024 * 1024
_READ_CHUNK_BYTES = 1024 * 1024


class OpenDartPendingBridgeError(RuntimeError):
    """Raised when an Open DART snapshot cannot enter the human-review queue."""


class OpenDartPendingBridgeModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class OpenDartPendingBridgeReceipt(OpenDartPendingBridgeModel):
    schema_version: Literal["1.0"] = "1.0"
    receipt_kind: Literal["open_dart_to_external_pending_corpus"] = (
        "open_dart_to_external_pending_corpus"
    )
    status: Literal["not_authorized_for_retrieval_or_release"] = (
        "not_authorized_for_retrieval_or_release"
    )
    collection_id: str = Field(pattern=r"^[a-z0-9][a-z0-9._:-]{2,159}$")
    open_dart_manifest_sha256: str = Field(pattern=_SHA256_PATTERN)
    open_dart_file_manifest_sha256: str = Field(pattern=_SHA256_PATTERN)
    filing_policy_sha256: str = Field(pattern=_SHA256_PATTERN)
    source_registry_sha256: str = Field(pattern=_SHA256_PATTERN)
    pending_manifest_sha256: str = Field(pattern=_SHA256_PATTERN)
    document_count: int = Field(gt=0, le=1000)
    raw_file_manifest_sha256: str = Field(pattern=_SHA256_PATTERN)
    text_file_manifest_sha256: str = Field(pattern=_SHA256_PATTERN)

    @model_validator(mode="after")
    def preserve_pending_boundary(self) -> OpenDartPendingBridgeReceipt:
        if self.status != "not_authorized_for_retrieval_or_release":  # pragma: no cover
            raise ValueError("Open DART bridge cannot grant retrieval authority")
        return self


@dataclass(frozen=True, slots=True)
class BridgedOpenDartPendingCorpus:
    manifest: PendingCorpusManifest
    manifest_sha256: str
    receipt: OpenDartPendingBridgeReceipt
    receipt_sha256: str


def _canonical_json_bytes(payload: object) -> bytes:
    return (
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
    ).encode("utf-8")


def open_dart_pending_bridge_receipt_bytes(receipt: OpenDartPendingBridgeReceipt) -> bytes:
    return _canonical_json_bytes(receipt.model_dump(mode="json"))


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _read_regular_file(
    path: Path,
    *,
    label: str,
    max_bytes: int,
    expected_size: int | None = None,
    expected_sha256: str | None = None,
) -> bytes:
    if path.is_symlink():
        raise OpenDartPendingBridgeError(f"{label} must not be a symbolic link")
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as error:
        raise OpenDartPendingBridgeError(f"{label} is unavailable") from error
    try:
        before = os.fstat(descriptor)
        identity = (
            before.st_dev,
            before.st_ino,
            before.st_size,
            before.st_mtime_ns,
            before.st_ctime_ns,
        )
        if not stat.S_ISREG(before.st_mode) or not 0 < before.st_size <= max_bytes:
            raise OpenDartPendingBridgeError(f"{label} is not a bounded regular file")
        if expected_size is not None and before.st_size != expected_size:
            raise OpenDartPendingBridgeError(f"{label} size differs from the Open DART manifest")
        chunks: list[bytes] = []
        total = 0
        while chunk := os.read(descriptor, _READ_CHUNK_BYTES):
            total += len(chunk)
            if total > max_bytes:
                raise OpenDartPendingBridgeError(f"{label} exceeds the size limit")
            chunks.append(chunk)
        after = os.fstat(descriptor)
        if identity != (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
            after.st_ctime_ns,
        ):
            raise OpenDartPendingBridgeError(f"{label} changed while being read")
        data = b"".join(chunks)
    finally:
        os.close(descriptor)
    if expected_sha256 is not None and _sha256(data) != expected_sha256:
        raise OpenDartPendingBridgeError(f"{label} SHA-256 differs from the Open DART manifest")
    return data


def _safe_root(path: str | Path, *, label: str) -> Path:
    target = Path(path)
    if target.is_symlink():
        raise OpenDartPendingBridgeError(f"{label} must not be a symbolic link")
    try:
        resolved = target.resolve(strict=True)
    except OSError as error:
        raise OpenDartPendingBridgeError(f"{label} is unavailable") from error
    if not resolved.is_dir():
        raise OpenDartPendingBridgeError(f"{label} must be a directory")
    return resolved


def _resolve_source_file(root: Path, relative_path: str) -> Path:
    current = root
    for part in relative_path.split("/"):
        if part in {"", ".", ".."}:
            raise OpenDartPendingBridgeError("Open DART file path is not canonical")
        current = current / part
        if current.is_symlink():
            raise OpenDartPendingBridgeError("Open DART file path contains a symbolic link")
    try:
        resolved = current.resolve(strict=True)
        resolved.relative_to(root)
    except (OSError, ValueError) as error:
        raise OpenDartPendingBridgeError("Open DART file escaped its corpus root") from error
    return resolved


def _write_file(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(
        path,
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    try:
        view = memoryview(data)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:  # pragma: no cover - OS contract guard
                raise OSError("Open DART bridge write made no progress")
            view = view[written:]
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _file_manifest_sha256(
    documents: tuple[PendingCorpusSnapshot, ...],
    *,
    kind: Literal["raw", "text"],
) -> str:
    payload = [
        {
            "document_id": item.document_id,
            "relative_path": item.raw_relative_path if kind == "raw" else item.text_relative_path,
            "sha256": item.raw_sha256 if kind == "raw" else item.text_sha256,
            "size_bytes": item.raw_size_bytes if kind == "raw" else item.text_size_bytes,
        }
        for item in documents
    ]
    return _sha256(_canonical_json_bytes(payload))


def _purposes(capabilities: tuple[KnowledgeCapability, ...]) -> tuple[DocumentPurpose, ...]:
    by_capability: dict[KnowledgeCapability, tuple[DocumentPurpose, ...]] = {
        KnowledgeCapability.CORPORATE_SUBSIDIARY: ("corporate_relation",),
        KnowledgeCapability.FUND_STRUCTURE_STRATEGY_TREND: (
            "product_structure",
            "trend",
        ),
        KnowledgeCapability.PORTFOLIO_HOLDINGS: ("holdings",),
        KnowledgeCapability.PRODUCT_RISK_FACTORS: ("risk",),
        KnowledgeCapability.SECTOR_THEME_HISTORY: ("theme", "trend"),
    }
    return tuple(sorted({purpose for item in capabilities for purpose in by_capability[item]}))


def _document_title(corp_name: str, report_name: str) -> str:
    """Keep the actual filer visible without exceeding the corpus title contract."""

    prefix = f"{corp_name} — "
    return prefix + report_name[: 500 - len(prefix)]


def _load_and_verify_open_dart_root(
    root: Path,
    *,
    policy: OpenDartFilingWhitelist,
) -> tuple[OpenDartPendingCorpusManifest, bytes, dict[str, bytes]]:
    manifest_data = _read_regular_file(
        root / "open-dart-pending-manifest.json",
        label="Open DART pending manifest",
        max_bytes=_MAX_MANIFEST_BYTES,
    )
    try:
        manifest = OpenDartPendingCorpusManifest.model_validate_json(manifest_data)
    except ValueError as error:
        raise OpenDartPendingBridgeError("Open DART pending manifest is invalid") from error
    if manifest_data != open_dart_manifest_bytes(manifest):
        raise OpenDartPendingBridgeError("Open DART pending manifest is not canonical JSON")
    if manifest.filing_policy_sha256 != policy.canonical_sha256:
        raise OpenDartPendingBridgeError("Open DART filing policy SHA-256 differs")
    expected_selection = select_open_dart_disclosures(
        manifest.disclosure_lists,
        policy,
        skip_unresolved_correction_lineages=manifest.entity_scope is not None,
    )
    if manifest.selected_disclosures != expected_selection:
        raise OpenDartPendingBridgeError("Open DART disclosure selection differs from policy")
    selected_receipts = {item.disclosure.rcept_no for item in expected_selection}
    document_receipts = {item.rcept_no for item in manifest.documents}
    unavailable_receipts = set(manifest.unavailable_original_document_receipts)
    if (
        document_receipts.intersection(unavailable_receipts)
        or selected_receipts != document_receipts.union(unavailable_receipts)
    ):
        raise OpenDartPendingBridgeError(
            "Open DART collection is incomplete for the selected disclosures"
        )
    references = open_dart_file_references(manifest)
    paths = [item.relative_path for item in references]
    if len(paths) != len(set(paths)):
        raise OpenDartPendingBridgeError("Open DART manifest contains duplicate file paths")
    files_by_path = {
        reference.relative_path: _read_regular_file(
            _resolve_source_file(root, reference.relative_path),
            label=f"Open DART file {reference.relative_path}",
            max_bytes=_MAX_FILE_BYTES,
            expected_size=reference.size_bytes,
            expected_sha256=reference.sha256,
        )
        for reference in references
    }
    return manifest, manifest_data, files_by_path


def bridge_open_dart_pending_corpus(
    open_dart_root: str | Path,
    output_root: str | Path,
    *,
    registry: OfficialSourceRegistry,
    policy: OpenDartFilingWhitelist,
) -> BridgedOpenDartPendingCorpus:
    """Convert a verified Open DART snapshot into the existing review queue.

    This operation deliberately creates only a generic *pending* corpus.  It
    neither asserts document usage rights nor creates relation claims, BM25,
    Document Dense, or release authority.  Those remain behind the existing
    two-reviewer approval and activation contracts.  The registry publisher
    identifies the official disclosure repository operator, not an endorsement
    of a filing's contents.  The actual filer remains explicit in each title and
    in the hash-bound upstream manifest.
    """

    source_root = _safe_root(open_dart_root, label="Open DART corpus root")
    manifest, source_manifest_data, source_files = _load_and_verify_open_dart_root(
        source_root,
        policy=policy,
    )
    try:
        source = registry.require_source_id(_OPEN_DART_SOURCE_ID)
    except OfficialSourceRegistryError as error:
        raise OpenDartPendingBridgeError(
            "Open DART source is absent from the intake registry"
        ) from error
    if source.publisher != _OPEN_DART_PUBLISHER:
        raise OpenDartPendingBridgeError("Open DART publisher differs from the intake registry")

    snapshots: list[PendingCorpusSnapshot] = []
    for document in manifest.documents:
        try:
            authorities = tuple(
                registry.authorize_url(document.source_uri, capability)
                for capability in document.capabilities
            )
        except OfficialSourceRegistryError as error:
            raise OpenDartPendingBridgeError(
                "Open DART document exceeds intake source authority"
            ) from error
        if any(
            item.source_id != _OPEN_DART_SOURCE_ID or item.publisher != _OPEN_DART_PUBLISHER
            for item in authorities
        ):
            raise OpenDartPendingBridgeError("Open DART source identity differs")
        snapshots.append(
            PendingCorpusSnapshot(
                document_id=f"open-dart-{document.rcept_no}",
                title=_document_title(document.corp_name, document.report_nm),
                publisher=source.publisher,
                source_registry_id=source.source_id,
                source_uri=document.source_uri,
                capabilities=document.capabilities,
                collected_at_utc=document.collected_at_utc,
                published_at=document.rcept_dt,
                as_of=document.rcept_dt,
                language="ko",
                purposes=_purposes(document.capabilities),
                raw_relative_path=document.archive.relative_path,
                raw_media_type="application/zip",
                raw_size_bytes=document.archive.size_bytes,
                raw_sha256=document.archive.sha256,
                text_relative_path=document.normalized_snapshot.relative_path,
                text_size_bytes=document.normalized_snapshot.size_bytes,
                text_sha256=document.normalized_snapshot.sha256,
            )
        )
    documents = tuple(sorted(snapshots, key=lambda item: item.document_id))
    pending = PendingCorpusManifest(
        catalog_id=manifest.collection_id,
        source_registry_sha256=registry.canonical_sha256,
        evidence_cutoff=manifest.evidence_cutoff,
        documents=documents,
        document_count=len(documents),
        requested_document_count=len(documents),
        raw_file_manifest_sha256=_file_manifest_sha256(documents, kind="raw"),
        text_file_manifest_sha256=_file_manifest_sha256(documents, kind="text"),
    )
    pending_bytes = pending_corpus_manifest_bytes(pending)
    pending_sha256 = _sha256(pending_bytes)
    receipt = OpenDartPendingBridgeReceipt(
        collection_id=manifest.collection_id,
        open_dart_manifest_sha256=_sha256(source_manifest_data),
        open_dart_file_manifest_sha256=manifest.file_manifest_sha256,
        filing_policy_sha256=manifest.filing_policy_sha256,
        source_registry_sha256=registry.canonical_sha256,
        pending_manifest_sha256=pending_sha256,
        document_count=len(documents),
        raw_file_manifest_sha256=pending.raw_file_manifest_sha256,
        text_file_manifest_sha256=pending.text_file_manifest_sha256,
    )
    receipt_bytes = open_dart_pending_bridge_receipt_bytes(receipt)

    target = Path(output_root)
    if target.exists() or target.is_symlink():
        raise OpenDartPendingBridgeError("Open DART bridge output already exists")
    try:
        parent = target.parent.resolve(strict=True)
    except OSError as error:
        raise OpenDartPendingBridgeError("Open DART bridge output parent is unavailable") from error
    if not parent.is_dir() or parent.is_symlink():
        raise OpenDartPendingBridgeError("Open DART bridge output parent is unsafe")
    staging = Path(tempfile.mkdtemp(prefix=f".{target.name}.", dir=parent))
    completed = False
    try:
        for relative_path in sorted(source_files):
            _write_file(staging / relative_path, source_files[relative_path])
        _write_file(
            staging / "provenance/open-dart-pending-manifest.json",
            source_manifest_data,
        )
        _write_file(staging / "pending-manifest.json", pending_bytes)
        _write_file(staging / "open-dart-bridge-receipt.json", receipt_bytes)
        os.replace(staging, target)
        completed = True
    finally:
        if not completed:
            shutil.rmtree(staging, ignore_errors=True)
    return BridgedOpenDartPendingCorpus(
        manifest=pending,
        manifest_sha256=pending_sha256,
        receipt=receipt,
        receipt_sha256=_sha256(receipt_bytes),
    )


__all__ = [
    "BridgedOpenDartPendingCorpus",
    "OpenDartPendingBridgeError",
    "OpenDartPendingBridgeReceipt",
    "bridge_open_dart_pending_corpus",
    "open_dart_pending_bridge_receipt_bytes",
]
