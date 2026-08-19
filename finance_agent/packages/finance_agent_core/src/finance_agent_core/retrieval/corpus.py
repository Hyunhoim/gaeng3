from __future__ import annotations

import hashlib
import json
import os
import secrets
import sqlite3
import stat
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path, PurePosixPath
from typing import Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, StrictBool, field_validator, model_validator

from finance_agent_core.retrieval.models import DocumentInput, DocumentSourceKind
from finance_agent_core.retrieval.sqlite_fts import (
    SQLiteDocumentIndex,
    normalize_document_text,
    sha256_document_text,
)

_SHA256_PATTERN = r"^[0-9a-f]{64}$"
_ID_PATTERN = r"^[a-z0-9][a-z0-9._-]{2,127}$"
_REVIEWER_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._@:-]{2,127}$"
_LICENSE_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9.+_-]{1,63}$"
_MAX_MANIFEST_BYTES = 2 * 1024 * 1024
_MAX_DOCUMENT_BYTES = 16 * 1024 * 1024
_MAX_CORPUS_BYTES = 128 * 1024 * 1024
_READ_CHUNK_BYTES = 1024 * 1024

DocumentPurpose = Literal[
    "definition",
    "product_structure",
    "risk",
    "fee",
    "operation",
    "regulation",
]
DocumentLanguage = Literal["ko", "en"]
SnapshotMediaType = Literal["text/plain", "text/markdown"]
ReviewerRole = Literal["finance_domain", "data_rights"]


class CorpusApprovalError(RuntimeError):
    """Raised when an external document corpus cannot cross the approval boundary."""


class CorpusModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


def _require_https_url(value: str, field_name: str) -> str:
    parsed = urlsplit(value)
    if (
        value != value.strip()
        or parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
    ):
        raise ValueError(f"{field_name} must be an HTTPS URL without credentials")
    return value


def _require_utc(value: datetime, field_name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise ValueError(f"{field_name} must use UTC")
    return value


def _canonical_json_bytes(payload: object) -> bytes:
    return (
        json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    ).encode("utf-8")


def _canonical_sha256(payload: object) -> str:
    return hashlib.sha256(_canonical_json_bytes(payload)).hexdigest()


class CorpusLicenseApproval(CorpusModel):
    license_id: str = Field(pattern=_LICENSE_PATTERN)
    license_uri: str = Field(min_length=8, max_length=2000)
    storage_allowed: StrictBool
    retrieval_allowed: StrictBool
    competition_use_allowed: StrictBool
    deployment_bundle_allowed: StrictBool
    attribution_text: str = Field(min_length=3, max_length=1000)

    @field_validator("license_uri")
    @classmethod
    def validate_license_uri(cls, value: str) -> str:
        return _require_https_url(value, "license_uri")

    @field_validator("attribution_text")
    @classmethod
    def reject_blank_attribution(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("attribution_text cannot be blank")
        return value

    @model_validator(mode="after")
    def require_all_usage_rights(self) -> CorpusLicenseApproval:
        if not all(
            (
                self.storage_allowed,
                self.retrieval_allowed,
                self.competition_use_allowed,
                self.deployment_bundle_allowed,
            )
        ):
            raise ValueError("external corpus requires every declared usage right")
        return self


class CorpusReview(CorpusModel):
    reviewer_role: ReviewerRole
    reviewer_id: str = Field(pattern=_REVIEWER_PATTERN)
    decision: Literal["approved"] = "approved"
    reviewed_at_utc: datetime
    note: str = Field(min_length=3, max_length=1000)

    @field_validator("reviewed_at_utc")
    @classmethod
    def validate_reviewed_at_utc(cls, value: datetime) -> datetime:
        return _require_utc(value, "reviewed_at_utc")

    @field_validator("note")
    @classmethod
    def reject_blank_note(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("review note cannot be blank")
        return value


class CorpusDocumentSpec(CorpusModel):
    document_id: str = Field(pattern=_ID_PATTERN)
    relative_path: str = Field(min_length=1, max_length=4096)
    title: str = Field(min_length=1, max_length=500)
    publisher: str = Field(min_length=1, max_length=300)
    source_uri: str = Field(min_length=8, max_length=2000)
    source_kind: Literal["external_approved"] = "external_approved"
    collected_at_utc: datetime
    as_of: date
    language: DocumentLanguage
    media_type: SnapshotMediaType
    purposes: tuple[DocumentPurpose, ...] = Field(min_length=1, max_length=6)
    license: CorpusLicenseApproval

    @field_validator("relative_path")
    @classmethod
    def validate_relative_path(cls, value: str) -> str:
        if "\\" in value or "\x00" in value:
            raise ValueError("corpus path must use safe POSIX separators")
        path = PurePosixPath(value)
        parts = value.split("/")
        if (
            path.is_absolute()
            or value.startswith("/")
            or value.endswith("/")
            or any(part in {"", ".", ".."} for part in parts)
            or path.as_posix() != value
            or path.suffix.casefold() not in {".txt", ".md"}
        ):
            raise ValueError("corpus path must be canonical, relative, and text-only")
        return value

    @field_validator("source_uri")
    @classmethod
    def validate_source_uri(cls, value: str) -> str:
        return _require_https_url(value, "source_uri")

    @field_validator("title", "publisher")
    @classmethod
    def reject_blank_labels(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("document title and publisher cannot be blank")
        return value

    @field_validator("collected_at_utc")
    @classmethod
    def validate_collected_at_utc(cls, value: datetime) -> datetime:
        return _require_utc(value, "collected_at_utc")

    @field_validator("purposes")
    @classmethod
    def validate_purposes(
        cls,
        value: tuple[DocumentPurpose, ...],
    ) -> tuple[DocumentPurpose, ...]:
        if tuple(sorted(value)) != value or len(set(value)) != len(value):
            raise ValueError("document purposes must be unique and sorted")
        return value

    @model_validator(mode="after")
    def validate_collection_date(self) -> CorpusDocumentSpec:
        if self.as_of > self.collected_at_utc.date():
            raise ValueError("document as_of cannot be later than collection date")
        expected_media_type = (
            "text/markdown" if self.relative_path.endswith(".md") else "text/plain"
        )
        if self.media_type != expected_media_type:
            raise ValueError("document media_type differs from its file suffix")
        return self


class ApprovedCorpusDocument(CorpusDocumentSpec):
    content_size_bytes: int = Field(gt=0, le=_MAX_DOCUMENT_BYTES)
    content_sha256: str = Field(pattern=_SHA256_PATTERN)
    normalized_text_sha256: str = Field(pattern=_SHA256_PATTERN)


def _validate_document_order(documents: tuple[CorpusDocumentSpec, ...]) -> None:
    document_ids = [item.document_id for item in documents]
    paths = [item.relative_path for item in documents]
    if document_ids != sorted(document_ids) or len(set(document_ids)) != len(document_ids):
        raise ValueError("corpus documents must have unique, sorted document IDs")
    if len(set(paths)) != len(paths):
        raise ValueError("corpus documents must have unique snapshot paths")


def _validate_reviews(
    reviews: tuple[CorpusReview, ...],
    documents: tuple[CorpusDocumentSpec, ...],
) -> None:
    roles = [item.reviewer_role for item in reviews]
    reviewers = [item.reviewer_id for item in reviews]
    if roles != ["data_rights", "finance_domain"]:
        raise ValueError("reviews must contain sorted data_rights and finance_domain approvals")
    if len(set(reviewers)) != len(reviewers):
        raise ValueError("corpus approvals require two independent reviewer IDs")
    latest_collection = max(item.collected_at_utc for item in documents)
    if any(item.reviewed_at_utc < latest_collection for item in reviews):
        raise ValueError("corpus review cannot predate document collection")


class ExternalCorpusIntakeSpec(CorpusModel):
    schema_version: Literal["1.0"] = "1.0"
    spec_kind: Literal["external_corpus_intake"] = "external_corpus_intake"
    corpus_id: str = Field(pattern=_ID_PATTERN)
    status: Literal["reviewed_for_sealing"] = "reviewed_for_sealing"
    reviews: tuple[CorpusReview, ...] = Field(min_length=2, max_length=2)
    documents: tuple[CorpusDocumentSpec, ...] = Field(min_length=1, max_length=1000)

    @model_validator(mode="after")
    def validate_intake(self) -> ExternalCorpusIntakeSpec:
        _validate_document_order(self.documents)
        _validate_reviews(self.reviews, self.documents)
        return self


def _document_file_manifest_sha256(
    documents: tuple[ApprovedCorpusDocument, ...],
) -> str:
    payload = [
        {
            "content_sha256": item.content_sha256,
            "content_size_bytes": item.content_size_bytes,
            "document_id": item.document_id,
            "normalized_text_sha256": item.normalized_text_sha256,
            "relative_path": item.relative_path,
        }
        for item in documents
    ]
    return _canonical_sha256(payload)


class ApprovedCorpusManifest(CorpusModel):
    schema_version: Literal["1.0"] = "1.0"
    manifest_kind: Literal["external_approved_corpus"] = "external_approved_corpus"
    corpus_id: str = Field(pattern=_ID_PATTERN)
    status: Literal["approved_for_bm25"] = "approved_for_bm25"
    reviews: tuple[CorpusReview, ...] = Field(min_length=2, max_length=2)
    documents: tuple[ApprovedCorpusDocument, ...] = Field(min_length=1, max_length=1000)
    document_count: int = Field(ge=1, le=1000)
    total_content_size_bytes: int = Field(gt=0, le=_MAX_CORPUS_BYTES)
    file_manifest_sha256: str = Field(pattern=_SHA256_PATTERN)

    @model_validator(mode="after")
    def validate_manifest(self) -> ApprovedCorpusManifest:
        _validate_document_order(self.documents)
        _validate_reviews(self.reviews, self.documents)
        if self.document_count != len(self.documents):
            raise ValueError("corpus document_count differs")
        if self.total_content_size_bytes != sum(
            item.content_size_bytes for item in self.documents
        ):
            raise ValueError("corpus total_content_size_bytes differs")
        if self.file_manifest_sha256 != _document_file_manifest_sha256(self.documents):
            raise ValueError("corpus file manifest SHA-256 differs")
        return self

    @property
    def canonical_sha256(self) -> str:
        return hashlib.sha256(approved_corpus_manifest_bytes(self)).hexdigest()


class CorpusVerificationReceipt(CorpusModel):
    schema_version: Literal["1.0"] = "1.0"
    receipt_kind: Literal["external_corpus_verification"] = "external_corpus_verification"
    status: Literal["verified_not_release_activated"] = "verified_not_release_activated"
    corpus_id: str = Field(pattern=_ID_PATTERN)
    manifest_sha256: str = Field(pattern=_SHA256_PATTERN)
    file_manifest_sha256: str = Field(pattern=_SHA256_PATTERN)
    document_count: int = Field(ge=1)
    total_content_size_bytes: int = Field(gt=0)


class CorpusIndexBuildReceipt(CorpusModel):
    schema_version: Literal["1.0"] = "1.0"
    receipt_kind: Literal["external_corpus_bm25_index"] = "external_corpus_bm25_index"
    status: Literal["verified_index_not_release_activated"] = (
        "verified_index_not_release_activated"
    )
    corpus_id: str = Field(pattern=_ID_PATTERN)
    manifest_sha256: str = Field(pattern=_SHA256_PATTERN)
    file_manifest_sha256: str = Field(pattern=_SHA256_PATTERN)
    database_sha256: str = Field(pattern=_SHA256_PATTERN)
    database_size_bytes: int = Field(gt=0)
    document_count: int = Field(ge=1)
    chunk_count: int = Field(ge=1)


@dataclass(frozen=True)
class VerifiedCorpusDocument:
    approval: ApprovedCorpusDocument
    text: str


@dataclass(frozen=True)
class VerifiedApprovedCorpus:
    manifest: ApprovedCorpusManifest
    manifest_sha256: str
    documents: tuple[VerifiedCorpusDocument, ...]

    @property
    def receipt(self) -> CorpusVerificationReceipt:
        return CorpusVerificationReceipt(
            corpus_id=self.manifest.corpus_id,
            manifest_sha256=self.manifest_sha256,
            file_manifest_sha256=self.manifest.file_manifest_sha256,
            document_count=self.manifest.document_count,
            total_content_size_bytes=self.manifest.total_content_size_bytes,
        )


def approved_corpus_manifest_bytes(manifest: ApprovedCorpusManifest) -> bytes:
    return _canonical_json_bytes(manifest.model_dump(mode="json"))


def corpus_receipt_bytes(receipt: CorpusVerificationReceipt | CorpusIndexBuildReceipt) -> bytes:
    return _canonical_json_bytes(receipt.model_dump(mode="json"))


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
    except (json.JSONDecodeError, UnicodeError) as error:
        raise CorpusApprovalError(f"{label} is not valid JSON") from error
    if not isinstance(payload, dict):
        raise CorpusApprovalError(f"{label} must be a JSON object")
    return payload


def _read_regular_file(
    path: Path,
    *,
    label: str,
    max_bytes: int,
    expected_size: int | None = None,
) -> bytes:
    if path.is_symlink():
        raise CorpusApprovalError(f"{label} must not be a symbolic link")
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as error:
        raise CorpusApprovalError(f"{label} is unavailable") from error
    try:
        before = os.fstat(descriptor)
        identity_before = (
            before.st_dev,
            before.st_ino,
            before.st_size,
            before.st_mtime_ns,
            before.st_ctime_ns,
        )
        if not stat.S_ISREG(before.st_mode):
            raise CorpusApprovalError(f"{label} must be a regular file")
        if before.st_size <= 0 or before.st_size > max_bytes:
            raise CorpusApprovalError(f"{label} size is outside the allowed range")
        if expected_size is not None and before.st_size != expected_size:
            raise CorpusApprovalError(f"{label} size differs from the approved manifest")
        chunks: list[bytes] = []
        total = 0
        while chunk := os.read(descriptor, _READ_CHUNK_BYTES):
            total += len(chunk)
            if total > max_bytes:
                raise CorpusApprovalError(f"{label} exceeds the allowed size")
            chunks.append(chunk)
        after = os.fstat(descriptor)
        identity_after = (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
            after.st_ctime_ns,
        )
        if identity_after != identity_before or total != before.st_size:
            raise CorpusApprovalError(f"{label} changed while it was being read")
        return b"".join(chunks)
    finally:
        os.close(descriptor)


def _resolve_corpus_root(path: str | Path) -> Path:
    root = Path(path)
    if root.is_symlink():
        raise CorpusApprovalError("corpus root must not be a symbolic link")
    try:
        resolved = root.resolve(strict=True)
    except OSError as error:
        raise CorpusApprovalError("corpus root is unavailable") from error
    if not resolved.is_dir():
        raise CorpusApprovalError("corpus root must be a directory")
    return resolved


def _resolve_snapshot(root: Path, relative_path: str) -> Path:
    current = root
    for part in relative_path.split("/"):
        current = current / part
        if current.is_symlink():
            raise CorpusApprovalError("corpus snapshot path must not contain symbolic links")
    try:
        resolved = current.resolve(strict=True)
    except OSError as error:
        raise CorpusApprovalError("corpus snapshot is unavailable") from error
    if resolved.parent != root and root not in resolved.parents:
        raise CorpusApprovalError("corpus snapshot escaped the approved root")
    return resolved


def _decode_snapshot(data: bytes, label: str) -> str:
    if data.startswith(b"\xef\xbb\xbf"):
        raise CorpusApprovalError(f"{label} must not contain a UTF-8 BOM")
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as error:
        raise CorpusApprovalError(f"{label} must use UTF-8") from error
    if "\x00" in text or not text.strip():
        raise CorpusApprovalError(f"{label} must contain non-blank text without NUL bytes")
    return text


def load_external_corpus_intake_spec(path: str | Path) -> ExternalCorpusIntakeSpec:
    data = _read_regular_file(
        Path(path),
        label="external corpus intake spec",
        max_bytes=_MAX_MANIFEST_BYTES,
    )
    try:
        return ExternalCorpusIntakeSpec.model_validate(
            _strict_json_object(data, "external corpus intake spec")
        )
    except ValueError as error:
        raise CorpusApprovalError("external corpus intake spec violates the schema") from error


def seal_approved_corpus_manifest(
    spec: ExternalCorpusIntakeSpec,
    corpus_root: str | Path,
) -> ApprovedCorpusManifest:
    root = _resolve_corpus_root(corpus_root)
    approved: list[ApprovedCorpusDocument] = []
    total_size = 0
    for item in spec.documents:
        path = _resolve_snapshot(root, item.relative_path)
        data = _read_regular_file(
            path,
            label=f"corpus document {item.document_id}",
            max_bytes=_MAX_DOCUMENT_BYTES,
        )
        total_size += len(data)
        if total_size > _MAX_CORPUS_BYTES:
            raise CorpusApprovalError("external corpus exceeds the total size limit")
        text = _decode_snapshot(data, f"corpus document {item.document_id}")
        normalized = normalize_document_text(text)
        approved.append(
            ApprovedCorpusDocument(
                **item.model_dump(mode="python"),
                content_size_bytes=len(data),
                content_sha256=hashlib.sha256(data).hexdigest(),
                normalized_text_sha256=sha256_document_text(normalized),
            )
        )
    documents = tuple(approved)
    return ApprovedCorpusManifest(
        corpus_id=spec.corpus_id,
        reviews=spec.reviews,
        documents=documents,
        document_count=len(documents),
        total_content_size_bytes=total_size,
        file_manifest_sha256=_document_file_manifest_sha256(documents),
    )


def load_approved_corpus_manifest(path: str | Path) -> ApprovedCorpusManifest:
    data = _read_regular_file(
        Path(path),
        label="approved corpus manifest",
        max_bytes=_MAX_MANIFEST_BYTES,
    )
    try:
        manifest = ApprovedCorpusManifest.model_validate(
            _strict_json_object(data, "approved corpus manifest")
        )
    except ValueError as error:
        raise CorpusApprovalError("approved corpus manifest violates the schema") from error
    if data != approved_corpus_manifest_bytes(manifest):
        raise CorpusApprovalError("approved corpus manifest is not in canonical file form")
    return manifest


def verify_approved_corpus(
    manifest_path: str | Path,
    corpus_root: str | Path,
) -> VerifiedApprovedCorpus:
    manifest_file = Path(manifest_path)
    manifest_data = _read_regular_file(
        manifest_file,
        label="approved corpus manifest",
        max_bytes=_MAX_MANIFEST_BYTES,
    )
    try:
        manifest = ApprovedCorpusManifest.model_validate(
            _strict_json_object(manifest_data, "approved corpus manifest")
        )
    except ValueError as error:
        raise CorpusApprovalError("approved corpus manifest violates the schema") from error
    if manifest_data != approved_corpus_manifest_bytes(manifest):
        raise CorpusApprovalError("approved corpus manifest is not in canonical file form")

    root = _resolve_corpus_root(corpus_root)
    verified: list[VerifiedCorpusDocument] = []
    for item in manifest.documents:
        path = _resolve_snapshot(root, item.relative_path)
        data = _read_regular_file(
            path,
            label=f"corpus document {item.document_id}",
            max_bytes=_MAX_DOCUMENT_BYTES,
            expected_size=item.content_size_bytes,
        )
        if hashlib.sha256(data).hexdigest() != item.content_sha256:
            raise CorpusApprovalError(
                f"corpus document {item.document_id} differs from its approved SHA-256"
            )
        text = _decode_snapshot(data, f"corpus document {item.document_id}")
        normalized = normalize_document_text(text)
        if sha256_document_text(normalized) != item.normalized_text_sha256:
            raise CorpusApprovalError(
                f"corpus document {item.document_id} normalized text differs"
            )
        verified.append(VerifiedCorpusDocument(approval=item, text=text))
    return VerifiedApprovedCorpus(
        manifest=manifest,
        manifest_sha256=hashlib.sha256(manifest_data).hexdigest(),
        documents=tuple(verified),
    )


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(_READ_CHUNK_BYTES), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _require_output_parent(path: Path) -> Path:
    if path.name in {"", ".", ".."}:
        raise CorpusApprovalError("output path must name a regular file")
    parent = path.parent if path.parent != Path("") else Path(".")
    if parent.is_symlink():
        raise CorpusApprovalError("output parent must not be a symbolic link")
    try:
        resolved = parent.resolve(strict=True)
    except OSError as error:
        raise CorpusApprovalError("output parent is unavailable") from error
    if not resolved.is_dir():
        raise CorpusApprovalError("output parent must be a directory")
    return resolved


def write_new_read_only_file(path: str | Path, data: bytes) -> str:
    output = Path(path)
    parent = _require_output_parent(output)
    target = parent / output.name
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
        descriptor = os.open(target, flags, 0o444)
    except OSError as error:
        raise CorpusApprovalError("output already exists or cannot be created") from error
    try:
        view = memoryview(data)
        while view:
            written = os.write(descriptor, view)
            view = view[written:]
        os.fsync(descriptor)
        completed = True
    finally:
        if descriptor is not None:
            os.close(descriptor)
        if not completed and target.exists() and not target.is_symlink():
            target.unlink()
    try:
        parent_descriptor = os.open(
            parent,
            os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_CLOEXEC", 0),
        )
        try:
            os.fsync(parent_descriptor)
        finally:
            os.close(parent_descriptor)
    except OSError as error:
        raise CorpusApprovalError("cannot durably record the output") from error
    return hashlib.sha256(data).hexdigest()


def build_approved_corpus_index(
    verified: VerifiedApprovedCorpus,
    output_database: str | Path,
) -> CorpusIndexBuildReceipt:
    output = Path(output_database)
    parent = _require_output_parent(output)
    target = parent / output.name
    if target.exists() or target.is_symlink():
        raise CorpusApprovalError("corpus index output already exists")
    temporary = parent / f".{output.name}.{secrets.token_hex(8)}.tmp"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0)
    try:
        descriptor = os.open(temporary, flags, 0o600)
        os.close(descriptor)
        index = SQLiteDocumentIndex(temporary)
        index.initialize()
        chunk_count = 0
        for item in verified.documents:
            approval = item.approval
            result = index.ingest(
                DocumentInput(
                    document_id=approval.document_id,
                    title=approval.title,
                    text=item.text,
                    source_uri=approval.source_uri,
                    source_kind=DocumentSourceKind.EXTERNAL_APPROVED,
                    as_of=approval.as_of,
                    metadata={
                        "corpus_id": verified.manifest.corpus_id,
                        "language": approval.language,
                        "license_id": approval.license.license_id,
                        "publisher": approval.publisher,
                    },
                )
            )
            if result.document_sha256 != approval.normalized_text_sha256:
                raise CorpusApprovalError("index document hash differs from corpus approval")
            chunk_count += result.chunk_count
        with sqlite3.connect(temporary) as connection:
            document_row = connection.execute("SELECT COUNT(*) FROM documents").fetchone()
            chunk_row = connection.execute("SELECT COUNT(*) FROM document_chunks").fetchone()
            integrity = connection.execute("PRAGMA quick_check").fetchone()
            connection.execute("VACUUM")
        if (
            document_row is None
            or int(document_row[0]) != verified.manifest.document_count
            or chunk_row is None
            or int(chunk_row[0]) != chunk_count
            or integrity is None
            or integrity[0] != "ok"
        ):
            raise CorpusApprovalError("built corpus index failed integrity verification")
        os.chmod(temporary, 0o444)
        database_size = temporary.stat().st_size
        database_sha256 = _sha256_file(temporary)
        try:
            os.link(temporary, target, follow_symlinks=False)
        except OSError as error:
            raise CorpusApprovalError("corpus index output cannot be installed") from error
        temporary.unlink()
        return CorpusIndexBuildReceipt(
            corpus_id=verified.manifest.corpus_id,
            manifest_sha256=verified.manifest_sha256,
            file_manifest_sha256=verified.manifest.file_manifest_sha256,
            database_sha256=database_sha256,
            database_size_bytes=database_size,
            document_count=verified.manifest.document_count,
            chunk_count=chunk_count,
        )
    except Exception:
        if temporary.exists() and not temporary.is_symlink():
            temporary.unlink()
        raise


__all__ = [
    "ApprovedCorpusDocument",
    "ApprovedCorpusManifest",
    "CorpusApprovalError",
    "CorpusIndexBuildReceipt",
    "CorpusLicenseApproval",
    "CorpusReview",
    "CorpusVerificationReceipt",
    "CorpusDocumentSpec",
    "ExternalCorpusIntakeSpec",
    "VerifiedApprovedCorpus",
    "approved_corpus_manifest_bytes",
    "build_approved_corpus_index",
    "corpus_receipt_bytes",
    "load_approved_corpus_manifest",
    "load_external_corpus_intake_spec",
    "seal_approved_corpus_manifest",
    "verify_approved_corpus",
    "write_new_read_only_file",
]
