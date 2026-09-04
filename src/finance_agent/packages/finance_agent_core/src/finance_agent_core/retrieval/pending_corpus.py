from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
from datetime import UTC, date, datetime
from html.parser import HTMLParser
from importlib.resources import files
from pathlib import Path
from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from finance_agent_core.config.knowledge_coverage import KnowledgeCapability
from finance_agent_core.config.source_registry import (
    OfficialSourceRegistry,
    OfficialSourceRegistryError,
)
from finance_agent_core.retrieval.corpus import DocumentLanguage, DocumentPurpose
from finance_agent_core.retrieval.official_connector import (
    OfficialConnectorError,
    OfficialDiscoveryCandidate,
    OfficialFetchRequest,
    OfficialSourceConnector,
)
from finance_agent_core.retrieval.sqlite_fts import normalize_document_text

_SHA256_PATTERN = r"^[0-9a-f]{64}$"
_DOCUMENT_ID_PATTERN = r"^[a-z0-9][a-z0-9._-]{2,127}$"
_MAX_TEXT_BYTES = 16 * 1024 * 1024


class PendingCorpusError(RuntimeError):
    """Raised when an unapproved official snapshot cannot be staged safely."""


class PendingCorpusModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class PendingCorpusSource(PendingCorpusModel):
    document_id: str = Field(pattern=_DOCUMENT_ID_PATTERN)
    title: str = Field(min_length=1, max_length=500)
    source_uri: str = Field(min_length=8, max_length=2000)
    capabilities: tuple[KnowledgeCapability, ...] = Field(min_length=1, max_length=5)
    published_at: date | None = None
    as_of: date
    language: DocumentLanguage
    purposes: tuple[DocumentPurpose, ...] = Field(min_length=1, max_length=10)

    @model_validator(mode="after")
    def validate_source(self) -> PendingCorpusSource:
        capabilities = [item.value for item in self.capabilities]
        if capabilities != sorted(capabilities) or len(capabilities) != len(set(capabilities)):
            raise ValueError("pending source capabilities must be unique and sorted")
        if self.purposes != tuple(sorted(self.purposes)):
            raise ValueError("pending source purposes must be sorted")
        if len(self.purposes) != len(set(self.purposes)):
            raise ValueError("pending source purposes must be unique")
        if self.published_at is not None and self.as_of > self.published_at:
            raise ValueError("pending source as_of cannot follow publication date")
        return self


class PendingCorpusCatalog(PendingCorpusModel):
    schema_version: Literal["1.0"] = "1.0"
    catalog_id: str = Field(pattern=_DOCUMENT_ID_PATTERN)
    evidence_cutoff: date
    documents: tuple[PendingCorpusSource, ...] = Field(min_length=1, max_length=1000)

    @model_validator(mode="after")
    def validate_catalog(self) -> PendingCorpusCatalog:
        document_ids = [item.document_id for item in self.documents]
        if document_ids != sorted(document_ids) or len(document_ids) != len(set(document_ids)):
            raise ValueError("pending catalog documents must be unique and sorted")
        if any(item.as_of > self.evidence_cutoff for item in self.documents):
            raise ValueError("pending catalog source exceeds the evidence cutoff")
        return self


class PendingCorpusSnapshot(PendingCorpusModel):
    document_id: str = Field(pattern=_DOCUMENT_ID_PATTERN)
    title: str = Field(min_length=1, max_length=500)
    publisher: str = Field(min_length=1, max_length=300)
    source_registry_id: str = Field(pattern=r"^[a-z0-9][a-z0-9._-]{2,79}$")
    source_uri: str = Field(min_length=8, max_length=2000)
    capabilities: tuple[KnowledgeCapability, ...] = Field(min_length=1, max_length=5)
    collected_at_utc: datetime
    published_at: date | None = None
    as_of: date
    language: DocumentLanguage
    purposes: tuple[DocumentPurpose, ...] = Field(min_length=1, max_length=10)
    raw_relative_path: str = Field(min_length=1, max_length=4096)
    raw_media_type: Literal["text/html", "application/pdf"]
    raw_size_bytes: int = Field(gt=0, le=16 * 1024 * 1024)
    raw_sha256: str = Field(pattern=_SHA256_PATTERN)
    text_relative_path: str = Field(min_length=1, max_length=4096)
    text_size_bytes: int = Field(gt=0, le=_MAX_TEXT_BYTES)
    text_sha256: str = Field(pattern=_SHA256_PATTERN)
    rights_status: Literal["pending_independent_review"] = "pending_independent_review"
    finance_review_status: Literal["pending_independent_review"] = "pending_independent_review"

    @field_validator("collected_at_utc")
    @classmethod
    def require_utc(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() != UTC.utcoffset(value):
            raise ValueError("collection timestamp must use UTC")
        return value


class PendingCorpusFailure(PendingCorpusModel):
    document_id: str = Field(pattern=_DOCUMENT_ID_PATTERN)
    source_uri: str = Field(min_length=8, max_length=2000)
    stage: Literal["fetch", "text_extraction", "deduplication"]
    reason_code: Literal[
        "official_fetch_failed",
        "text_extraction_failed",
        "duplicate_official_snapshot",
    ]


class PendingCorpusManifest(PendingCorpusModel):
    schema_version: Literal["1.0"] = "1.0"
    manifest_kind: Literal["external_corpus_pending_review"] = "external_corpus_pending_review"
    status: Literal["not_authorized_for_retrieval_or_release"] = (
        "not_authorized_for_retrieval_or_release"
    )
    catalog_id: str = Field(pattern=_DOCUMENT_ID_PATTERN)
    source_registry_sha256: str = Field(pattern=_SHA256_PATTERN)
    evidence_cutoff: date
    documents: tuple[PendingCorpusSnapshot, ...] = Field(min_length=1, max_length=1000)
    document_count: int = Field(gt=0, le=1000)
    failures: tuple[PendingCorpusFailure, ...] = Field(default=(), max_length=1000)
    requested_document_count: int = Field(gt=0, le=1000)
    raw_file_manifest_sha256: str = Field(pattern=_SHA256_PATTERN)
    text_file_manifest_sha256: str = Field(pattern=_SHA256_PATTERN)

    @model_validator(mode="after")
    def validate_manifest(self) -> PendingCorpusManifest:
        document_ids = [item.document_id for item in self.documents]
        if document_ids != sorted(document_ids) or len(document_ids) != len(set(document_ids)):
            raise ValueError("pending snapshots must be unique and sorted")
        if self.document_count != len(self.documents):
            raise ValueError("pending snapshot document count differs")
        failure_ids = [item.document_id for item in self.failures]
        if failure_ids != sorted(failure_ids) or len(failure_ids) != len(set(failure_ids)):
            raise ValueError("pending failures must be unique and sorted")
        if set(document_ids).intersection(failure_ids):
            raise ValueError("a pending document cannot be collected and failed")
        if self.requested_document_count != len(document_ids) + len(failure_ids):
            raise ValueError("pending requested document count differs")
        if self.raw_file_manifest_sha256 != _file_manifest_sha256(
            self.documents,
            kind="raw",
        ):
            raise ValueError("pending raw snapshot manifest SHA-256 differs")
        if self.text_file_manifest_sha256 != _file_manifest_sha256(
            self.documents,
            kind="text",
        ):
            raise ValueError("pending text snapshot manifest SHA-256 differs")
        return self

    @property
    def canonical_sha256(self) -> str:
        return hashlib.sha256(pending_corpus_manifest_bytes(self)).hexdigest()


class PdfTextExtractor(Protocol):
    def extract(self, content: bytes) -> str: ...


class _VisibleTextParser(HTMLParser):
    _IGNORED = frozenset({"script", "style", "noscript", "svg"})
    _BLOCKS = frozenset(
        {
            "article",
            "br",
            "dd",
            "div",
            "dl",
            "dt",
            "footer",
            "h1",
            "h2",
            "h3",
            "h4",
            "header",
            "li",
            "main",
            "nav",
            "p",
            "section",
            "table",
            "td",
            "th",
            "tr",
        }
    )

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._ignored_depth = 0
        self._parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        del attrs
        normalized = tag.casefold()
        if normalized in self._IGNORED:
            self._ignored_depth += 1
        elif not self._ignored_depth and normalized in self._BLOCKS:
            self._parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        normalized = tag.casefold()
        if normalized in self._IGNORED and self._ignored_depth:
            self._ignored_depth -= 1
        elif not self._ignored_depth and normalized in self._BLOCKS:
            self._parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self._ignored_depth and data.strip():
            self._parts.append(data)

    def text(self) -> str:
        lines = []
        for line in "".join(self._parts).splitlines():
            normalized = " ".join(line.split())
            if normalized:
                lines.append(normalized)
        return normalize_document_text("\n".join(lines))


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


def pending_corpus_manifest_bytes(manifest: PendingCorpusManifest) -> bytes:
    return _canonical_json_bytes(manifest.model_dump(mode="json"))


def _file_manifest_sha256(
    documents: tuple[PendingCorpusSnapshot, ...],
    *,
    kind: Literal["raw", "text"],
) -> str:
    payload = [
        {
            "document_id": item.document_id,
            "relative_path": (item.raw_relative_path if kind == "raw" else item.text_relative_path),
            "sha256": item.raw_sha256 if kind == "raw" else item.text_sha256,
            "size_bytes": item.raw_size_bytes if kind == "raw" else item.text_size_bytes,
        }
        for item in documents
    ]
    return hashlib.sha256(_canonical_json_bytes(payload)).hexdigest()


def load_pending_corpus_catalog() -> PendingCorpusCatalog:
    resource = files("finance_agent_core.config").joinpath("external_knowledge_source_catalog.json")
    return PendingCorpusCatalog.model_validate_json(resource.read_bytes())


def load_pending_corpus_manifest(path: str | Path) -> PendingCorpusManifest:
    try:
        return PendingCorpusManifest.model_validate_json(Path(path).read_bytes())
    except (OSError, ValueError) as error:
        raise PendingCorpusError("pending corpus manifest is invalid") from error


def rebind_pending_corpus_registry(
    manifest: PendingCorpusManifest,
    registry: OfficialSourceRegistry,
) -> PendingCorpusManifest:
    """Rebind unchanged snapshots after a Source Registry metadata-only revision.

    This never approves a pending corpus and never changes source bytes, dates,
    capabilities or document identities. Every URL/capability must still map to
    the same source ID and publisher under the new registry; otherwise a fresh
    collection and independent review are required.
    """

    if manifest.evidence_cutoff != registry.evidence_cutoff:
        raise PendingCorpusError("pending corpus and Source Registry cutoffs differ")
    for snapshot in manifest.documents:
        try:
            entries = tuple(
                registry.authorize_url(snapshot.source_uri, capability)
                for capability in snapshot.capabilities
            )
        except OfficialSourceRegistryError as error:
            raise PendingCorpusError("pending corpus source is no longer authorized") from error
        if (
            len({entry.source_id for entry in entries}) != 1
            or entries[0].source_id != snapshot.source_registry_id
            or entries[0].publisher != snapshot.publisher
        ):
            raise PendingCorpusError("pending corpus source authority changed")
    return manifest.model_copy(update={"source_registry_sha256": registry.canonical_sha256})


def _verified_snapshot_bytes(
    root: Path,
    relative_path: str,
    *,
    size_bytes: int,
    sha256: str,
) -> bytes:
    try:
        resolved_root = root.resolve(strict=True)
        resolved = (resolved_root / relative_path).resolve(strict=True)
        resolved.relative_to(resolved_root)
    except (OSError, ValueError) as error:
        raise PendingCorpusError("pending snapshot path escaped its corpus root") from error
    if resolved.is_symlink() or not resolved.is_file():
        raise PendingCorpusError("pending snapshot must be a regular file")
    content = resolved.read_bytes()
    if len(content) != size_bytes or hashlib.sha256(content).hexdigest() != sha256:
        raise PendingCorpusError("pending snapshot bytes differ from the manifest")
    return content


def merge_pending_corpus_roots(
    output_root: str | Path,
    source_roots: tuple[str | Path, ...],
    *,
    registry: OfficialSourceRegistry,
) -> PendingCorpusManifest:
    """Merge verified pending corpora without granting retrieval authority.

    Every source manifest is rebound to the current Source Registry only after
    its URL, capability, source ID and publisher remain identical. Raw and text
    bytes are re-hashed before being copied into one atomic output tree.
    """

    if len(source_roots) < 2:
        raise PendingCorpusError("at least two pending corpus roots are required")
    roots = tuple(Path(value) for value in source_roots)
    manifests = tuple(
        rebind_pending_corpus_registry(
            load_pending_corpus_manifest(root / "pending-manifest.json"),
            registry,
        )
        for root in roots
    )
    if any(item.evidence_cutoff != registry.evidence_cutoff for item in manifests):
        raise PendingCorpusError("pending corpus evidence cutoffs differ")

    target = Path(output_root)
    parent = target.parent.resolve(strict=True)
    if target.exists() or target.is_symlink():
        raise PendingCorpusError("merged pending corpus output already exists")
    staging = Path(tempfile.mkdtemp(prefix=f".{target.name}.", dir=parent))
    completed = False
    try:
        documents_by_id: dict[str, PendingCorpusSnapshot] = {}
        document_id_by_raw_sha256: dict[str, str] = {}
        failures_by_id: dict[str, PendingCorpusFailure] = {}
        for root, manifest in zip(roots, manifests, strict=True):
            for failure in manifest.failures:
                failures_by_id.setdefault(failure.document_id, failure)
            for snapshot in manifest.documents:
                if snapshot.document_id in documents_by_id:
                    previous = documents_by_id[snapshot.document_id]
                    if previous.raw_sha256 != snapshot.raw_sha256:
                        raise PendingCorpusError("duplicate document ID carries different bytes")
                    continue
                raw = _verified_snapshot_bytes(
                    root,
                    snapshot.raw_relative_path,
                    size_bytes=snapshot.raw_size_bytes,
                    sha256=snapshot.raw_sha256,
                )
                text = _verified_snapshot_bytes(
                    root,
                    snapshot.text_relative_path,
                    size_bytes=snapshot.text_size_bytes,
                    sha256=snapshot.text_sha256,
                )
                duplicate_id = document_id_by_raw_sha256.get(snapshot.raw_sha256)
                if duplicate_id is not None:
                    previous = documents_by_id[duplicate_id]
                    if previous.text_sha256 != snapshot.text_sha256:
                        raise PendingCorpusError(
                            "identical official bytes produced different extracted text"
                        )
                    documents_by_id[duplicate_id] = previous.model_copy(
                        update={
                            "capabilities": tuple(
                                sorted(
                                    {*previous.capabilities, *snapshot.capabilities},
                                    key=lambda item: item.value,
                                )
                            ),
                            "purposes": tuple(
                                sorted({*previous.purposes, *snapshot.purposes})
                            ),
                        }
                    )
                    failures_by_id[snapshot.document_id] = PendingCorpusFailure(
                        document_id=snapshot.document_id,
                        source_uri=snapshot.source_uri,
                        stage="deduplication",
                        reason_code="duplicate_official_snapshot",
                    )
                    continue
                raw_suffix = ".pdf" if snapshot.raw_media_type == "application/pdf" else ".html"
                raw_relative_path = f"raw/{snapshot.document_id}{raw_suffix}"
                text_relative_path = f"snapshots/{snapshot.document_id}.txt"
                _write_file(staging / raw_relative_path, raw)
                _write_file(staging / text_relative_path, text)
                documents_by_id[snapshot.document_id] = snapshot.model_copy(
                    update={
                        "raw_relative_path": raw_relative_path,
                        "text_relative_path": text_relative_path,
                    }
                )
                document_id_by_raw_sha256[snapshot.raw_sha256] = snapshot.document_id

        for document_id in documents_by_id:
            failures_by_id.pop(document_id, None)
        documents = tuple(documents_by_id[key] for key in sorted(documents_by_id))
        failures = tuple(failures_by_id[key] for key in sorted(failures_by_id))
        digest = hashlib.sha256(
            "\n".join(item.canonical_sha256 for item in manifests).encode("ascii")
        ).hexdigest()[:16]
        manifest = PendingCorpusManifest(
            catalog_id=f"merged-pending-corpus-{digest}",
            source_registry_sha256=registry.canonical_sha256,
            evidence_cutoff=registry.evidence_cutoff,
            documents=documents,
            document_count=len(documents),
            failures=failures,
            requested_document_count=len(documents) + len(failures),
            raw_file_manifest_sha256=_file_manifest_sha256(documents, kind="raw"),
            text_file_manifest_sha256=_file_manifest_sha256(documents, kind="text"),
        )
        _write_file(staging / "pending-manifest.json", pending_corpus_manifest_bytes(manifest))
        os.replace(staging, target)
        completed = True
        return manifest
    finally:
        if not completed:
            shutil.rmtree(staging, ignore_errors=True)


def _extract_text(
    media_type: str,
    content: bytes,
    pdf_text_extractor: PdfTextExtractor,
) -> str:
    if media_type.casefold().split(";", maxsplit=1)[0] == "application/pdf":
        text = pdf_text_extractor.extract(content)
    else:
        try:
            decoded = content.decode("utf-8")
        except UnicodeDecodeError:
            decoded = content.decode("utf-8", errors="replace")
        parser = _VisibleTextParser()
        parser.feed(decoded)
        parser.close()
        text = parser.text()
    normalized = normalize_document_text(text)
    encoded = normalized.encode("utf-8")
    if not normalized or len(encoded) > _MAX_TEXT_BYTES:
        raise PendingCorpusError("official snapshot produced invalid extracted text")
    return normalized


def _write_file(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0)
    descriptor = os.open(path, flags, 0o600)
    try:
        view = memoryview(data)
        while view:
            view = view[os.write(descriptor, view) :]
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def collect_pending_corpus(
    output_root: str | Path,
    *,
    connector: OfficialSourceConnector,
    pdf_text_extractor: PdfTextExtractor,
    catalog: PendingCorpusCatalog | None = None,
) -> PendingCorpusManifest:
    active_catalog = catalog or load_pending_corpus_catalog()
    if active_catalog.evidence_cutoff != connector.registry.evidence_cutoff:
        raise PendingCorpusError("source catalog and registry evidence cutoffs differ")
    target = Path(output_root)
    parent = target.parent.resolve(strict=True)
    if target.exists() or target.is_symlink():
        raise PendingCorpusError("pending corpus output already exists")
    staging = Path(tempfile.mkdtemp(prefix=f".{target.name}.", dir=parent))
    completed = False
    try:
        collected_at = datetime.now(UTC)
        snapshots: list[PendingCorpusSnapshot] = []
        failures: list[PendingCorpusFailure] = []
        snapshot_index_by_raw_sha256: dict[str, int] = {}
        for source in active_catalog.documents:
            registry_entries = [
                connector.registry.authorize_url(source.source_uri, capability)
                for capability in source.capabilities
            ]
            if len({item.source_id for item in registry_entries}) != 1:
                raise PendingCorpusError("source capabilities cross registry entries")
            registry_entry = registry_entries[0]
            try:
                snapshot = connector.fetch(
                    OfficialDiscoveryCandidate(
                        url=source.source_uri,
                        title=source.title,
                        published_at=source.published_at,
                        as_of=source.as_of,
                    ),
                    OfficialFetchRequest(
                        url=source.source_uri,
                        capability=source.capabilities[0],
                        published_at=source.published_at,
                        as_of=source.as_of,
                        timeout_seconds=30,
                        max_bytes=16 * 1024 * 1024,
                    ),
                )
            except OfficialConnectorError:
                failures.append(
                    PendingCorpusFailure(
                        document_id=source.document_id,
                        source_uri=source.source_uri,
                        stage="fetch",
                        reason_code="official_fetch_failed",
                    )
                )
                continue
            media_type = snapshot.content_type.casefold().split(";", maxsplit=1)[0]
            raw_suffix = ".pdf" if media_type == "application/pdf" else ".html"
            raw_relative_path = f"raw/{source.document_id}{raw_suffix}"
            text_relative_path = f"snapshots/{source.document_id}.txt"
            try:
                text = _extract_text(media_type, snapshot.content, pdf_text_extractor)
            except PendingCorpusError:
                failures.append(
                    PendingCorpusFailure(
                        document_id=source.document_id,
                        source_uri=source.source_uri,
                        stage="text_extraction",
                        reason_code="text_extraction_failed",
                    )
                )
                continue
            text_bytes = (text + "\n").encode("utf-8")
            raw_sha256 = hashlib.sha256(snapshot.content).hexdigest()
            text_sha256 = hashlib.sha256(text_bytes).hexdigest()
            pending_snapshot = PendingCorpusSnapshot(
                document_id=source.document_id,
                title=source.title,
                publisher=registry_entry.publisher,
                source_registry_id=registry_entry.source_id,
                source_uri=snapshot.source_uri,
                capabilities=source.capabilities,
                collected_at_utc=collected_at,
                published_at=source.published_at,
                as_of=source.as_of,
                language=source.language,
                purposes=source.purposes,
                raw_relative_path=raw_relative_path,
                raw_media_type=media_type,
                raw_size_bytes=len(snapshot.content),
                raw_sha256=raw_sha256,
                text_relative_path=text_relative_path,
                text_size_bytes=len(text_bytes),
                text_sha256=text_sha256,
            )
            duplicate_index = snapshot_index_by_raw_sha256.get(raw_sha256)
            if duplicate_index is not None:
                previous = snapshots[duplicate_index]
                if previous.text_sha256 != text_sha256:
                    raise PendingCorpusError("identical official bytes produced different text")
                snapshots[duplicate_index] = previous.model_copy(
                    update={
                        "capabilities": tuple(
                            sorted(
                                {*previous.capabilities, *pending_snapshot.capabilities},
                                key=lambda item: item.value,
                            )
                        ),
                        "purposes": tuple(sorted({*previous.purposes, *pending_snapshot.purposes})),
                    }
                )
                failures.append(
                    PendingCorpusFailure(
                        document_id=source.document_id,
                        source_uri=source.source_uri,
                        stage="deduplication",
                        reason_code="duplicate_official_snapshot",
                    )
                )
                continue
            _write_file(staging / raw_relative_path, snapshot.content)
            _write_file(staging / text_relative_path, text_bytes)
            snapshot_index_by_raw_sha256[raw_sha256] = len(snapshots)
            snapshots.append(pending_snapshot)
        documents = tuple(snapshots)
        manifest = PendingCorpusManifest(
            catalog_id=active_catalog.catalog_id,
            source_registry_sha256=connector.registry.canonical_sha256,
            evidence_cutoff=active_catalog.evidence_cutoff,
            documents=documents,
            document_count=len(documents),
            failures=tuple(failures),
            requested_document_count=len(active_catalog.documents),
            raw_file_manifest_sha256=_file_manifest_sha256(documents, kind="raw"),
            text_file_manifest_sha256=_file_manifest_sha256(documents, kind="text"),
        )
        _write_file(staging / "pending-manifest.json", pending_corpus_manifest_bytes(manifest))
        os.replace(staging, target)
        completed = True
        return manifest
    finally:
        if not completed:
            shutil.rmtree(staging, ignore_errors=True)


__all__ = [
    "PdfTextExtractor",
    "PendingCorpusCatalog",
    "PendingCorpusError",
    "PendingCorpusFailure",
    "PendingCorpusManifest",
    "PendingCorpusSnapshot",
    "PendingCorpusSource",
    "collect_pending_corpus",
    "load_pending_corpus_catalog",
    "load_pending_corpus_manifest",
    "merge_pending_corpus_roots",
    "pending_corpus_manifest_bytes",
    "rebind_pending_corpus_registry",
]
