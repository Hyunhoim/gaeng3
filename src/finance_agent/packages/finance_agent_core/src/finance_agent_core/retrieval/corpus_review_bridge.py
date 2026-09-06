from __future__ import annotations

import hashlib
import json
import os
import stat
from datetime import date
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from finance_agent_core.config.knowledge_coverage import KnowledgeCapability
from finance_agent_core.config.source_registry import (
    OfficialSourceRegistry,
    OfficialSourceRegistryError,
)
from finance_agent_core.retrieval.corpus import (
    CorpusApprovalError,
    CorpusDocumentSpec,
    CorpusLicenseApproval,
    CorpusReview,
    ExternalCorpusIntakeSpec,
)
from finance_agent_core.retrieval.pending_corpus import (
    PendingCorpusError,
    PendingCorpusManifest,
    PendingCorpusSnapshot,
    pending_corpus_manifest_bytes,
    rebind_pending_corpus_registry,
)

_SHA256_PATTERN = r"^[0-9a-f]{64}$"
_ID_PATTERN = r"^[a-z0-9][a-z0-9._-]{2,127}$"
_MAX_MANIFEST_BYTES = 2 * 1024 * 1024
_MAX_TEXT_SNAPSHOT_BYTES = 16 * 1024 * 1024
_MAX_RAW_SNAPSHOT_BYTES = 32 * 1024 * 1024
_READ_CHUNK_BYTES = 1024 * 1024


class CorpusReviewBridgeModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


def _canonical_json_bytes(payload: object) -> bytes:
    return (
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
    ).encode("utf-8")


def _strict_json_object(data: bytes, label: str) -> dict[str, object]:
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as error:
        raise CorpusApprovalError(f"{label} must use UTF-8") from error

    def reject_duplicates(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in pairs:
            if key in result:
                raise CorpusApprovalError(f"{label} contains a duplicate JSON key")
            result[key] = value
        return result

    try:
        payload = json.loads(text, object_pairs_hook=reject_duplicates)
    except CorpusApprovalError:
        raise
    except json.JSONDecodeError as error:
        raise CorpusApprovalError(f"{label} is not valid JSON") from error
    if not isinstance(payload, dict):
        raise CorpusApprovalError(f"{label} must be a JSON object")
    return payload


def _read_regular_file(path: Path, *, label: str, max_bytes: int) -> bytes:
    if path.is_symlink():
        raise CorpusApprovalError(f"{label} must not be a symbolic link")
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as error:
        raise CorpusApprovalError(f"{label} is unavailable") from error
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
            raise CorpusApprovalError(f"{label} is not a bounded regular file")
        chunks: list[bytes] = []
        total = 0
        while chunk := os.read(descriptor, _READ_CHUNK_BYTES):
            total += len(chunk)
            if total > max_bytes:
                raise CorpusApprovalError(f"{label} exceeds the size limit")
            chunks.append(chunk)
        after = os.fstat(descriptor)
        if (
            identity
            != (
                after.st_dev,
                after.st_ino,
                after.st_size,
                after.st_mtime_ns,
                after.st_ctime_ns,
            )
            or total != before.st_size
        ):
            raise CorpusApprovalError(f"{label} changed while being read")
        return b"".join(chunks)
    finally:
        os.close(descriptor)


def _resolve_snapshot(root: Path, relative_path: str) -> Path:
    current = root
    for part in relative_path.split("/"):
        if part in {"", ".", ".."}:
            raise CorpusApprovalError("pending snapshot path is not canonical")
        current = current / part
        if current.is_symlink():
            raise CorpusApprovalError("pending snapshot path contains a symbolic link")
    try:
        resolved = current.resolve(strict=True)
    except OSError as error:
        raise CorpusApprovalError("pending snapshot is unavailable") from error
    if resolved.parent != root and root not in resolved.parents:
        raise CorpusApprovalError("pending snapshot escaped its corpus root")
    return resolved


def _snapshot_sha256(snapshot: PendingCorpusSnapshot) -> str:
    return hashlib.sha256(_canonical_json_bytes(snapshot.model_dump(mode="json"))).hexdigest()


class CorpusReviewWorksheetDocument(CorpusReviewBridgeModel):
    document_id: str = Field(pattern=_ID_PATTERN)
    pending_snapshot_sha256: str = Field(pattern=_SHA256_PATTERN)
    title: str = Field(min_length=1, max_length=500)
    publisher: str = Field(min_length=1, max_length=300)
    source_registry_id: str = Field(pattern=r"^[a-z0-9][a-z0-9._-]{2,79}$")
    source_uri: str = Field(min_length=8, max_length=2000)
    capabilities: tuple[KnowledgeCapability, ...] = Field(min_length=1, max_length=5)
    published_at: date | None
    as_of: date
    text_relative_path: str = Field(min_length=1, max_length=4096)
    text_size_bytes: int = Field(gt=0, le=_MAX_TEXT_SNAPSHOT_BYTES)
    text_sha256: str = Field(pattern=_SHA256_PATTERN)
    rights_status: Literal["pending_independent_review"]
    finance_review_status: Literal["pending_independent_review"]


class ExternalCorpusReviewWorksheet(CorpusReviewBridgeModel):
    schema_version: Literal["1.0"] = "1.0"
    worksheet_kind: Literal["external_corpus_human_review"] = "external_corpus_human_review"
    status: Literal["requires_external_reviews_and_rights"] = "requires_external_reviews_and_rights"
    pending_manifest_sha256: str = Field(pattern=_SHA256_PATTERN)
    pending_source_registry_sha256: str = Field(pattern=_SHA256_PATTERN)
    source_registry_sha256: str = Field(pattern=_SHA256_PATTERN)
    evidence_cutoff: date
    required_reviewer_roles: tuple[Literal["data_rights"], Literal["finance_domain"]] = (
        "data_rights",
        "finance_domain",
    )
    documents: tuple[CorpusReviewWorksheetDocument, ...] = Field(
        min_length=1,
        max_length=1000,
    )


class CorpusDocumentHumanApproval(CorpusReviewBridgeModel):
    document_id: str = Field(pattern=_ID_PATTERN)
    pending_snapshot_sha256: str = Field(pattern=_SHA256_PATTERN)
    decision: Literal["approved"] = "approved"
    license: CorpusLicenseApproval


class ExternalCorpusHumanApproval(CorpusReviewBridgeModel):
    schema_version: Literal["1.0"] = "1.0"
    approval_kind: Literal["external_corpus_human_approval"] = "external_corpus_human_approval"
    status: Literal["human_approvals_supplied"] = "human_approvals_supplied"
    pending_manifest_sha256: str = Field(pattern=_SHA256_PATTERN)
    pending_source_registry_sha256: str = Field(pattern=_SHA256_PATTERN)
    source_registry_sha256: str = Field(pattern=_SHA256_PATTERN)
    evidence_cutoff: date
    corpus_id: str = Field(pattern=_ID_PATTERN)
    reviews: tuple[CorpusReview, ...] = Field(min_length=2, max_length=2)
    documents: tuple[CorpusDocumentHumanApproval, ...] = Field(
        min_length=1,
        max_length=1000,
    )

    @model_validator(mode="after")
    def validate_external_approvals(self) -> ExternalCorpusHumanApproval:
        roles = [item.reviewer_role for item in self.reviews]
        reviewers = [item.reviewer_id for item in self.reviews]
        if roles != ["data_rights", "finance_domain"]:
            raise ValueError("external reviews must contain both required roles in order")
        if len(set(reviewers)) != 2:
            raise ValueError("external reviews require independent reviewer IDs")
        document_ids = [item.document_id for item in self.documents]
        if document_ids != sorted(document_ids) or len(document_ids) != len(set(document_ids)):
            raise ValueError("human-approved documents must be unique and sorted")
        return self


def corpus_review_worksheet_bytes(worksheet: ExternalCorpusReviewWorksheet) -> bytes:
    return _canonical_json_bytes(worksheet.model_dump(mode="json"))


def external_corpus_intake_spec_bytes(spec: ExternalCorpusIntakeSpec) -> bytes:
    return _canonical_json_bytes(spec.model_dump(mode="json"))


def load_external_corpus_human_approval(
    path: str | Path,
) -> ExternalCorpusHumanApproval:
    data = _read_regular_file(
        Path(path),
        label="external corpus human approval",
        max_bytes=_MAX_MANIFEST_BYTES,
    )
    try:
        return ExternalCorpusHumanApproval.model_validate(
            _strict_json_object(data, "external corpus human approval")
        )
    except ValueError as error:
        raise CorpusApprovalError("external corpus human approval violates the schema") from error


def _verified_pending_manifest(
    corpus_root: str | Path,
) -> tuple[Path, PendingCorpusManifest, str]:
    root = Path(corpus_root)
    if root.is_symlink():
        raise CorpusApprovalError("pending corpus root must not be a symbolic link")
    try:
        root = root.resolve(strict=True)
    except OSError as error:
        raise CorpusApprovalError("pending corpus root is unavailable") from error
    if not root.is_dir():
        raise CorpusApprovalError("pending corpus root must be a directory")
    manifest_data = _read_regular_file(
        root / "pending-manifest.json",
        label="pending corpus manifest",
        max_bytes=_MAX_MANIFEST_BYTES,
    )
    try:
        manifest = PendingCorpusManifest.model_validate(
            _strict_json_object(manifest_data, "pending corpus manifest")
        )
    except ValueError as error:
        raise CorpusApprovalError("pending corpus manifest violates the schema") from error
    if manifest_data != pending_corpus_manifest_bytes(manifest):
        raise CorpusApprovalError("pending corpus manifest is not in canonical file form")
    for snapshot in manifest.documents:
        for kind, relative_path, expected_size, expected_sha256 in (
            (
                "raw",
                snapshot.raw_relative_path,
                snapshot.raw_size_bytes,
                snapshot.raw_sha256,
            ),
            (
                "text",
                snapshot.text_relative_path,
                snapshot.text_size_bytes,
                snapshot.text_sha256,
            ),
        ):
            data = _read_regular_file(
                _resolve_snapshot(root, relative_path),
                label=f"pending {kind} snapshot {snapshot.document_id}",
                max_bytes=(
                    _MAX_RAW_SNAPSHOT_BYTES if kind == "raw" else _MAX_TEXT_SNAPSHOT_BYTES
                ),
            )
            if len(data) != expected_size or hashlib.sha256(data).hexdigest() != expected_sha256:
                raise CorpusApprovalError(
                    f"pending {kind} snapshot {snapshot.document_id} differs from its manifest"
                )
    return root, manifest, hashlib.sha256(manifest_data).hexdigest()


def _validate_source_metadata(
    snapshot: PendingCorpusSnapshot,
    *,
    registry: OfficialSourceRegistry,
) -> None:
    try:
        declared = registry.require_source_id(snapshot.source_registry_id)
        authorized = tuple(
            registry.authorize_url(snapshot.source_uri, capability)
            for capability in snapshot.capabilities
        )
    except OfficialSourceRegistryError as error:
        raise CorpusApprovalError(
            "pending document is outside its declared Source Registry authority"
        ) from error
    if (
        len({item.source_id for item in authorized}) != 1
        or authorized[0].source_id != snapshot.source_registry_id
        or declared.publisher != snapshot.publisher
    ):
        raise CorpusApprovalError("pending document source metadata differs from the registry")
    if declared.publication_date_required and snapshot.published_at is None:
        raise CorpusApprovalError("pending document lacks a required publication date")
    if snapshot.as_of > registry.evidence_cutoff or (
        snapshot.published_at is not None and snapshot.published_at > registry.evidence_cutoff
    ):
        raise CorpusApprovalError("pending document metadata exceeds the evidence cutoff")


def build_external_corpus_review_worksheet(
    corpus_root: str | Path,
    *,
    registry: OfficialSourceRegistry,
) -> ExternalCorpusReviewWorksheet:
    _, manifest, manifest_sha256 = _verified_pending_manifest(corpus_root)
    try:
        rebind_pending_corpus_registry(manifest, registry)
    except PendingCorpusError as error:
        raise CorpusApprovalError(
            "pending corpus cannot be rebound to the current Source Registry"
        ) from error
    documents = []
    for snapshot in manifest.documents:
        _validate_source_metadata(snapshot, registry=registry)
        documents.append(
            CorpusReviewWorksheetDocument(
                document_id=snapshot.document_id,
                pending_snapshot_sha256=_snapshot_sha256(snapshot),
                title=snapshot.title,
                publisher=snapshot.publisher,
                source_registry_id=snapshot.source_registry_id,
                source_uri=snapshot.source_uri,
                capabilities=snapshot.capabilities,
                published_at=snapshot.published_at,
                as_of=snapshot.as_of,
                text_relative_path=snapshot.text_relative_path,
                text_size_bytes=snapshot.text_size_bytes,
                text_sha256=snapshot.text_sha256,
                rights_status=snapshot.rights_status,
                finance_review_status=snapshot.finance_review_status,
            )
        )
    return ExternalCorpusReviewWorksheet(
        pending_manifest_sha256=manifest_sha256,
        pending_source_registry_sha256=manifest.source_registry_sha256,
        source_registry_sha256=registry.canonical_sha256,
        evidence_cutoff=manifest.evidence_cutoff,
        documents=tuple(documents),
    )


def bridge_pending_corpus_to_reviewed_intake(
    corpus_root: str | Path,
    *,
    approval: ExternalCorpusHumanApproval,
    registry: OfficialSourceRegistry,
) -> ExternalCorpusIntakeSpec:
    _, manifest, manifest_sha256 = _verified_pending_manifest(corpus_root)
    if approval.pending_manifest_sha256 != manifest_sha256:
        raise CorpusApprovalError("human approval targets a different pending manifest")
    if approval.pending_source_registry_sha256 != manifest.source_registry_sha256:
        raise CorpusApprovalError("human approval targets a different pending Source Registry")
    try:
        rebind_pending_corpus_registry(manifest, registry)
    except PendingCorpusError as error:
        raise CorpusApprovalError(
            "pending corpus cannot be rebound to the current Source Registry"
        ) from error
    if approval.source_registry_sha256 != registry.canonical_sha256:
        raise CorpusApprovalError("human approval Source Registry SHA-256 differs")
    if (
        manifest.evidence_cutoff != registry.evidence_cutoff
        or approval.evidence_cutoff != registry.evidence_cutoff
    ):
        raise CorpusApprovalError("human approval evidence cutoff differs")
    snapshots = {item.document_id: item for item in manifest.documents}
    documents: list[CorpusDocumentSpec] = []
    for document_approval in approval.documents:
        snapshot = snapshots.get(document_approval.document_id)
        if snapshot is None:
            raise CorpusApprovalError("human approval contains an unknown document ID")
        if document_approval.pending_snapshot_sha256 != _snapshot_sha256(snapshot):
            raise CorpusApprovalError("human approval targets different document metadata")
        _validate_source_metadata(snapshot, registry=registry)
        documents.append(
            CorpusDocumentSpec(
                document_id=snapshot.document_id,
                relative_path=snapshot.text_relative_path,
                title=snapshot.title,
                publisher=snapshot.publisher,
                source_registry_id=snapshot.source_registry_id,
                source_uri=snapshot.source_uri,
                capabilities=snapshot.capabilities,
                collected_at_utc=snapshot.collected_at_utc,
                published_at=snapshot.published_at,
                as_of=snapshot.as_of,
                language=snapshot.language,
                media_type="text/plain",
                purposes=snapshot.purposes,
                license=document_approval.license,
            )
        )
    try:
        return ExternalCorpusIntakeSpec(
            corpus_id=approval.corpus_id,
            reviews=approval.reviews,
            documents=tuple(documents),
        )
    except ValueError as error:
        raise CorpusApprovalError(
            "reviewed corpus intake violates the approval contract"
        ) from error


__all__ = [
    "CorpusDocumentHumanApproval",
    "CorpusReviewWorksheetDocument",
    "ExternalCorpusHumanApproval",
    "ExternalCorpusReviewWorksheet",
    "bridge_pending_corpus_to_reviewed_intake",
    "build_external_corpus_review_worksheet",
    "corpus_review_worksheet_bytes",
    "external_corpus_intake_spec_bytes",
    "load_external_corpus_human_approval",
]
