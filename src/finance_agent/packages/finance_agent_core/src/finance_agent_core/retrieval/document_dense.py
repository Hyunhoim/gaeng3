from __future__ import annotations

import hashlib
import json
import math
import os
import re
import stat
from collections import Counter
from collections.abc import Mapping, Sequence
from datetime import date
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, StrictBool, field_validator, model_validator

from finance_agent_core.config.knowledge_coverage import KnowledgeCapability
from finance_agent_core.config.source_registry import (
    OfficialSourceRegistry,
    OfficialSourceRegistryError,
)
from finance_agent_core.retrieval.corpus import VerifiedApprovedCorpus
from finance_agent_core.retrieval.pending_corpus import (
    PendingCorpusSnapshot,
    load_pending_corpus_manifest,
)
from finance_agent_core.retrieval.schema_dense import (
    EmbeddingProvider,
    EmbeddingProviderMetadata,
)
from finance_agent_core.retrieval.sqlite_fts import chunk_document, normalize_document_text

_SHA256_PATTERN = r"^[0-9a-f]{64}$"
_MAX_DOCUMENT_DENSE_FILE_BYTES = 512 * 1024 * 1024
_TOKEN = re.compile(r"[0-9A-Za-z가-힣]+")
_KOREAN_QUERY_SUFFIXES = (
    "으로",
    "에서",
    "에게",
    "부터",
    "까지",
    "처럼",
    "보다",
    "은",
    "는",
    "이",
    "가",
    "을",
    "를",
    "의",
    "와",
    "과",
    "도",
)


class DocumentDenseError(RuntimeError):
    """Raised when an offline document-vector artifact fails its trust contract."""


class DocumentDenseModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


def _canonical_json_bytes(payload: object) -> bytes:
    return (
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
    ).encode("utf-8")


class DocumentDenseChunk(DocumentDenseModel):
    chunk_id: str = Field(pattern=r"^[a-z0-9][a-z0-9._-]{2,127}:[0-9]{4}$")
    document_id: str = Field(pattern=r"^[a-z0-9][a-z0-9._-]{2,127}$")
    ordinal: int = Field(ge=0)
    title: str = Field(min_length=1, max_length=500)
    text: str = Field(min_length=1, max_length=2000)
    text_sha256: str = Field(pattern=_SHA256_PATTERN)

    @model_validator(mode="after")
    def validate_identity(self) -> DocumentDenseChunk:
        if self.chunk_id != f"{self.document_id}:{self.ordinal:04d}":
            raise ValueError("document chunk identity differs")
        if hashlib.sha256(self.text.encode("utf-8")).hexdigest() != self.text_sha256:
            raise ValueError("document chunk text SHA-256 differs")
        return self


class DocumentDenseVector(DocumentDenseModel):
    chunk: DocumentDenseChunk
    vector: tuple[float, ...] = Field(min_length=8, max_length=65536)

    @field_validator("vector")
    @classmethod
    def validate_vector(cls, value: tuple[float, ...]) -> tuple[float, ...]:
        if any(not math.isfinite(item) for item in value):
            raise ValueError("document vector contains a non-finite value")
        norm = math.sqrt(sum(item * item for item in value))
        if not 0.999 <= norm <= 1.001:
            raise ValueError("document vector must be L2-normalized")
        return value


class DocumentDenseManifest(DocumentDenseModel):
    schema_version: Literal["1.0"] = "1.0"
    index_kind: Literal["document_dense_pending_review"] = "document_dense_pending_review"
    scope: Literal["offline_evaluation_only"] = "offline_evaluation_only"
    production_enabled: StrictBool = False
    activation_status: Literal["blocked_until_corpus_approval_and_calibration"] = (
        "blocked_until_corpus_approval_and_calibration"
    )
    source_corpus_status: Literal["not_authorized_for_retrieval_or_release"] = (
        "not_authorized_for_retrieval_or_release"
    )
    source_manifest_sha256: str = Field(pattern=_SHA256_PATTERN)
    source_text_manifest_sha256: str = Field(pattern=_SHA256_PATTERN)
    embedding_snapshot_manifest_sha256: str = Field(pattern=_SHA256_PATTERN)
    chunking_version: Literal["document-chunk-v1"] = "document-chunk-v1"
    max_chars: int = Field(ge=100, le=2000)
    overlap_chars: int = Field(ge=0, le=500)
    document_count: int = Field(gt=0, le=1000)
    chunk_count: int = Field(gt=0, le=100000)
    provider: EmbeddingProviderMetadata
    vector_payload_sha256: str = Field(pattern=_SHA256_PATTERN)

    @model_validator(mode="after")
    def require_offline_frozen_model(self) -> DocumentDenseManifest:
        if self.production_enabled:
            raise ValueError("pending Document Dense can never be production enabled")
        if self.provider.provider_kind != "frozen_model":
            raise ValueError("pending Document Dense evaluation requires a frozen model")
        if self.overlap_chars >= self.max_chars // 2:
            raise ValueError("document overlap must be less than half max_chars")
        return self


def _vector_payload_sha256(records: tuple[DocumentDenseVector, ...]) -> str:
    return hashlib.sha256(
        _canonical_json_bytes([record.model_dump(mode="json") for record in records])
    ).hexdigest()


class DocumentDenseArtifact(DocumentDenseModel):
    manifest: DocumentDenseManifest
    records: tuple[DocumentDenseVector, ...] = Field(min_length=1, max_length=100000)

    @model_validator(mode="after")
    def validate_artifact(self) -> DocumentDenseArtifact:
        chunk_ids = [item.chunk.chunk_id for item in self.records]
        if chunk_ids != sorted(chunk_ids) or len(chunk_ids) != len(set(chunk_ids)):
            raise ValueError("Document Dense records must be unique and sorted")
        if len(self.records) != self.manifest.chunk_count:
            raise ValueError("Document Dense chunk count differs")
        document_ids = {item.chunk.document_id for item in self.records}
        if len(document_ids) != self.manifest.document_count:
            raise ValueError("Document Dense document count differs")
        if any(len(item.vector) != self.manifest.provider.dimension for item in self.records):
            raise ValueError("Document Dense vector dimension differs")
        if _vector_payload_sha256(self.records) != self.manifest.vector_payload_sha256:
            raise ValueError("Document Dense vector payload SHA-256 differs")
        return self

    @property
    def canonical_sha256(self) -> str:
        return hashlib.sha256(document_dense_artifact_bytes(self)).hexdigest()


class ApprovedDocumentDenseManifest(DocumentDenseModel):
    """Production-candidate index built only from a verified approved corpus."""

    schema_version: Literal["1.0"] = "1.0"
    index_kind: Literal["document_dense_approved_corpus"] = (
        "document_dense_approved_corpus"
    )
    scope: Literal["production_candidate"] = "production_candidate"
    production_enabled: StrictBool = True
    activation_status: Literal["approved_cache_candidate_only"] = (
        "approved_cache_candidate_only"
    )
    source_corpus_status: Literal["approved_for_bm25"] = "approved_for_bm25"
    source_manifest_sha256: str = Field(pattern=_SHA256_PATTERN)
    source_text_manifest_sha256: str = Field(pattern=_SHA256_PATTERN)
    embedding_snapshot_manifest_sha256: str = Field(pattern=_SHA256_PATTERN)
    chunking_version: Literal["document-chunk-v1"] = "document-chunk-v1"
    max_chars: int = Field(ge=100, le=2000)
    overlap_chars: int = Field(ge=0, le=500)
    document_count: int = Field(gt=0, le=1000)
    chunk_count: int = Field(gt=0, le=100000)
    provider: EmbeddingProviderMetadata
    vector_payload_sha256: str = Field(pattern=_SHA256_PATTERN)

    @model_validator(mode="after")
    def require_approved_frozen_model(self) -> ApprovedDocumentDenseManifest:
        if not self.production_enabled:
            raise ValueError("approved Document Dense must be production enabled")
        if self.provider.provider_kind != "frozen_model":
            raise ValueError("approved Document Dense requires a frozen model")
        if self.overlap_chars >= self.max_chars // 2:
            raise ValueError("document overlap must be less than half max_chars")
        return self


class ApprovedDocumentDenseArtifact(DocumentDenseModel):
    manifest: ApprovedDocumentDenseManifest
    records: tuple[DocumentDenseVector, ...] = Field(min_length=1, max_length=100000)

    @model_validator(mode="after")
    def validate_artifact(self) -> ApprovedDocumentDenseArtifact:
        chunk_ids = [item.chunk.chunk_id for item in self.records]
        if chunk_ids != sorted(chunk_ids) or len(chunk_ids) != len(set(chunk_ids)):
            raise ValueError("approved Document Dense records must be unique and sorted")
        if len(self.records) != self.manifest.chunk_count:
            raise ValueError("approved Document Dense chunk count differs")
        if len({item.chunk.document_id for item in self.records}) != self.manifest.document_count:
            raise ValueError("approved Document Dense document count differs")
        if any(len(item.vector) != self.manifest.provider.dimension for item in self.records):
            raise ValueError("approved Document Dense vector dimension differs")
        if _vector_payload_sha256(self.records) != self.manifest.vector_payload_sha256:
            raise ValueError("approved Document Dense vector payload SHA-256 differs")
        return self

    @property
    def canonical_sha256(self) -> str:
        return hashlib.sha256(approved_document_dense_artifact_bytes(self)).hexdigest()


class ApprovedDocumentCacheSource(DocumentDenseModel):
    document_id: str = Field(pattern=r"^[a-z0-9][a-z0-9._-]{2,127}$")
    title: str = Field(min_length=1, max_length=500)
    source_registry_id: str = Field(pattern=r"^[a-z0-9][a-z0-9._-]{2,79}$")
    source_uri: str = Field(min_length=8, max_length=2000)
    capabilities: tuple[KnowledgeCapability, ...] = Field(min_length=1, max_length=5)
    published_at: date | None = None
    as_of: date
    content_sha256: str = Field(pattern=_SHA256_PATTERN)

    @model_validator(mode="after")
    def validate_source(self) -> ApprovedDocumentCacheSource:
        values = [item.value for item in self.capabilities]
        if values != sorted(values) or len(values) != len(set(values)):
            raise ValueError("approved cache capabilities must be unique and sorted")
        if self.published_at is not None and self.as_of > self.published_at:
            raise ValueError("approved cache as_of cannot follow publication date")
        return self


class DocumentCacheActivationReceipt(DocumentDenseModel):
    """Explicit release boundary between approved corpus artifacts and Runtime."""

    schema_version: Literal["1.0"] = "1.0"
    receipt_kind: Literal["document_cache_activation"] = "document_cache_activation"
    status: Literal["approved_cache_candidate_only"] = "approved_cache_candidate_only"
    source_registry_sha256: str = Field(pattern=_SHA256_PATTERN)
    evidence_cutoff: date
    approved_corpus_manifest_sha256: str = Field(pattern=_SHA256_PATTERN)
    approved_corpus_file_manifest_sha256: str = Field(pattern=_SHA256_PATTERN)
    document_dense_artifact_sha256: str = Field(pattern=_SHA256_PATTERN)
    document_dense_vector_payload_sha256: str = Field(pattern=_SHA256_PATTERN)
    document_bm25_sha256: str = Field(pattern=_SHA256_PATTERN)
    embedding_snapshot_manifest_sha256: str = Field(pattern=_SHA256_PATTERN)
    provider: EmbeddingProviderMetadata
    calibration_report_sha256: str = Field(pattern=_SHA256_PATTERN)
    minimum_dense_score: float = Field(ge=-1, le=1)
    minimum_dense_margin: float = Field(ge=0, le=2)
    top_k: int = Field(ge=1, le=20)
    documents: tuple[ApprovedDocumentCacheSource, ...] = Field(
        min_length=1,
        max_length=1000,
    )

    @model_validator(mode="after")
    def validate_receipt(self) -> DocumentCacheActivationReceipt:
        document_ids = [item.document_id for item in self.documents]
        if document_ids != sorted(document_ids) or len(document_ids) != len(set(document_ids)):
            raise ValueError("approved cache documents must be unique and sorted")
        if any(item.as_of > self.evidence_cutoff for item in self.documents):
            raise ValueError("approved cache document exceeds the evidence cutoff")
        if self.provider.provider_kind != "frozen_model":
            raise ValueError("approved cache requires a frozen embedding provider")
        return self

    @property
    def canonical_sha256(self) -> str:
        return hashlib.sha256(document_cache_activation_receipt_bytes(self)).hexdigest()


class DocumentSearchHit(DocumentDenseModel):
    chunk_id: str
    document_id: str
    title: str
    text: str
    score: float
    rank: int = Field(gt=0)
    channel: Literal["bm25", "dense", "hybrid_rrf"]


def fuse_document_hits_rrf(
    lexical: Sequence[DocumentSearchHit],
    dense: Sequence[DocumentSearchHit],
    *,
    top_k: int,
) -> tuple[DocumentSearchHit, ...]:
    """Fuse chunk retrieval at document level so one long PDF cannot crowd out peers."""

    if not 1 <= top_k <= 50:
        raise ValueError("top_k must be between 1 and 50")
    scores: dict[str, float] = {}
    representatives: dict[str, DocumentSearchHit] = {}
    representative_keys: dict[str, tuple[int, int, str]] = {}
    for channel_priority, hits in enumerate((lexical, dense)):
        seen: set[str] = set()
        document_rank = 0
        for hit in hits:
            if hit.document_id in seen:
                continue
            seen.add(hit.document_id)
            document_rank += 1
            # At document level a large RRF constant lets two weak channel
            # appearances outrank an exact top-1 hit from either channel. A
            # smaller constant preserves strong semantic or exact lexical
            # anchors while still rewarding agreement between both channels.
            scores[hit.document_id] = scores.get(hit.document_id, 0.0) + 1 / (
                10 + document_rank
            )
            key = (document_rank, channel_priority, hit.chunk_id)
            if key < representative_keys.get(hit.document_id, (10**9, 10**9, "")):
                representatives[hit.document_id] = hit
                representative_keys[hit.document_id] = key
    ordered = sorted(scores.items(), key=lambda item: (-item[1], item[0]))[:top_k]
    return tuple(
        DocumentSearchHit(
            chunk_id=representatives[document_id].chunk_id,
            document_id=document_id,
            title=representatives[document_id].title,
            text=representatives[document_id].text,
            score=score,
            rank=rank,
            channel="hybrid_rrf",
        )
        for rank, (document_id, score) in enumerate(ordered, start=1)
    )


def document_dense_artifact_bytes(artifact: DocumentDenseArtifact) -> bytes:
    return _canonical_json_bytes(artifact.model_dump(mode="json"))


def approved_document_dense_artifact_bytes(
    artifact: ApprovedDocumentDenseArtifact,
) -> bytes:
    return _canonical_json_bytes(artifact.model_dump(mode="json"))


def document_cache_activation_receipt_bytes(
    receipt: DocumentCacheActivationReceipt,
) -> bytes:
    return _canonical_json_bytes(receipt.model_dump(mode="json"))


def _safe_snapshot_text(root: Path, snapshot: PendingCorpusSnapshot) -> str:
    candidate = root / snapshot.text_relative_path
    try:
        resolved = candidate.resolve(strict=True)
        resolved.relative_to(root.resolve(strict=True))
    except (OSError, ValueError) as error:
        raise DocumentDenseError("pending snapshot path escaped its corpus root") from error
    if resolved.is_symlink() or not resolved.is_file():
        raise DocumentDenseError("pending snapshot must be a regular file")
    content = resolved.read_bytes()
    if len(content) != snapshot.text_size_bytes:
        raise DocumentDenseError("pending snapshot size differs")
    if hashlib.sha256(content).hexdigest() != snapshot.text_sha256:
        raise DocumentDenseError("pending snapshot SHA-256 differs")
    try:
        return normalize_document_text(content.decode("utf-8"))
    except UnicodeDecodeError as error:
        raise DocumentDenseError("pending snapshot is not UTF-8 text") from error


def build_pending_document_dense_artifact(
    corpus_root: str | Path,
    *,
    provider: EmbeddingProvider,
    embedding_snapshot_manifest_sha256: str,
    max_chars: int = 800,
    overlap_chars: int = 80,
) -> DocumentDenseArtifact:
    root = Path(corpus_root)
    manifest_path = root / "pending-manifest.json"
    source = load_pending_corpus_manifest(manifest_path)
    source_manifest_sha256 = hashlib.sha256(manifest_path.read_bytes()).hexdigest()
    chunks: list[DocumentDenseChunk] = []
    embedding_texts: list[str] = []
    for document in source.documents:
        text = _safe_snapshot_text(root, document)
        for ordinal, value in enumerate(
            chunk_document(text, max_chars=max_chars, overlap_chars=overlap_chars)
        ):
            chunk = DocumentDenseChunk(
                chunk_id=f"{document.document_id}:{ordinal:04d}",
                document_id=document.document_id,
                ordinal=ordinal,
                title=document.title,
                text=value,
                text_sha256=hashlib.sha256(value.encode("utf-8")).hexdigest(),
            )
            chunks.append(chunk)
            embedding_texts.append(f"제목: {document.title}\n본문: {value}")
    vectors = provider.embed_documents(embedding_texts)
    if len(vectors) != len(chunks):
        raise DocumentDenseError("embedding provider returned a different vector count")
    records = tuple(
        DocumentDenseVector(chunk=chunk, vector=tuple(vector))
        for chunk, vector in zip(chunks, vectors, strict=True)
    )
    dense_manifest = DocumentDenseManifest(
        source_manifest_sha256=source_manifest_sha256,
        source_text_manifest_sha256=source.text_file_manifest_sha256,
        embedding_snapshot_manifest_sha256=embedding_snapshot_manifest_sha256,
        max_chars=max_chars,
        overlap_chars=overlap_chars,
        document_count=source.document_count,
        chunk_count=len(records),
        provider=provider.metadata,
        vector_payload_sha256=_vector_payload_sha256(records),
    )
    return DocumentDenseArtifact(manifest=dense_manifest, records=records)


def build_approved_document_dense_artifact(
    corpus: VerifiedApprovedCorpus,
    *,
    provider: EmbeddingProvider,
    embedding_snapshot_manifest_sha256: str,
    max_chars: int = 800,
    overlap_chars: int = 80,
) -> ApprovedDocumentDenseArtifact:
    """Build vectors only after the corpus passed both independent reviews."""

    chunks: list[DocumentDenseChunk] = []
    embedding_texts: list[str] = []
    for verified in corpus.documents:
        document = verified.approval
        text = normalize_document_text(verified.text)
        for ordinal, value in enumerate(
            chunk_document(text, max_chars=max_chars, overlap_chars=overlap_chars)
        ):
            chunk = DocumentDenseChunk(
                chunk_id=f"{document.document_id}:{ordinal:04d}",
                document_id=document.document_id,
                ordinal=ordinal,
                title=document.title,
                text=value,
                text_sha256=hashlib.sha256(value.encode("utf-8")).hexdigest(),
            )
            chunks.append(chunk)
            embedding_texts.append(f"제목: {document.title}\n본문: {value}")
    vectors = provider.embed_documents(embedding_texts)
    if len(vectors) != len(chunks):
        raise DocumentDenseError("embedding provider returned a different vector count")
    records = tuple(
        DocumentDenseVector(chunk=chunk, vector=tuple(vector))
        for chunk, vector in zip(chunks, vectors, strict=True)
    )
    manifest = ApprovedDocumentDenseManifest(
        source_manifest_sha256=corpus.manifest_sha256,
        source_text_manifest_sha256=corpus.manifest.file_manifest_sha256,
        embedding_snapshot_manifest_sha256=embedding_snapshot_manifest_sha256,
        max_chars=max_chars,
        overlap_chars=overlap_chars,
        document_count=corpus.manifest.document_count,
        chunk_count=len(records),
        provider=provider.metadata,
        vector_payload_sha256=_vector_payload_sha256(records),
    )
    return ApprovedDocumentDenseArtifact(manifest=manifest, records=records)


_PURPOSE_CAPABILITY: dict[str, KnowledgeCapability] = {
    "corporate_relation": KnowledgeCapability.CORPORATE_SUBSIDIARY,
    "holdings": KnowledgeCapability.PORTFOLIO_HOLDINGS,
    "product_structure": KnowledgeCapability.FUND_STRUCTURE_STRATEGY_TREND,
    "risk": KnowledgeCapability.PRODUCT_RISK_FACTORS,
    "theme": KnowledgeCapability.SECTOR_THEME_HISTORY,
    "trend": KnowledgeCapability.FUND_STRUCTURE_STRATEGY_TREND,
}


def _approved_capabilities(
    *,
    document_id: str,
    purposes: Sequence[str],
    overrides: Mapping[str, Sequence[KnowledgeCapability]] | None,
) -> tuple[KnowledgeCapability, ...]:
    configured = None if overrides is None else overrides.get(document_id)
    values = (
        tuple(configured)
        if configured is not None
        else tuple(_PURPOSE_CAPABILITY[item] for item in purposes if item in _PURPOSE_CAPABILITY)
    )
    return tuple(sorted(set(values), key=lambda item: item.value))


def build_document_cache_activation_receipt(
    corpus: VerifiedApprovedCorpus,
    artifact: ApprovedDocumentDenseArtifact,
    *,
    registry: OfficialSourceRegistry,
    calibration_report_sha256: str,
    minimum_dense_score: float,
    minimum_dense_margin: float,
    top_k: int = 5,
    capability_overrides: Mapping[str, Sequence[KnowledgeCapability]] | None = None,
) -> DocumentCacheActivationReceipt:
    """Seal the exact approved corpus, KURE index and admission policy for release."""

    if artifact.manifest.source_manifest_sha256 != corpus.manifest_sha256:
        raise DocumentDenseError("approved index and corpus manifests differ")
    if (
        artifact.manifest.source_text_manifest_sha256
        != corpus.manifest.file_manifest_sha256
    ):
        raise DocumentDenseError("approved index and corpus file manifests differ")
    artifact_document_ids = {record.chunk.document_id for record in artifact.records}
    if artifact_document_ids != {item.approval.document_id for item in corpus.documents}:
        raise DocumentDenseError("approved index and corpus document sets differ")
    documents: list[ApprovedDocumentCacheSource] = []
    for verified in corpus.documents:
        source = verified.approval
        capabilities = _approved_capabilities(
            document_id=source.document_id,
            purposes=source.purposes,
            overrides=capability_overrides,
        )
        if not capabilities:
            continue
        try:
            authorized = tuple(
                registry.authorize_url(source.source_uri, capability)
                for capability in capabilities
            )
        except OfficialSourceRegistryError as error:
            raise DocumentDenseError(
                "approved corpus URL is not authorized for its capabilities"
            ) from error
        if len({item.source_id for item in authorized}) != 1:
            raise DocumentDenseError("approved corpus URL crosses Source Registry authorities")
        documents.append(
            ApprovedDocumentCacheSource(
                document_id=source.document_id,
                title=source.title,
                source_registry_id=authorized[0].source_id,
                source_uri=source.source_uri,
                capabilities=capabilities,
                published_at=None,
                as_of=source.as_of,
                content_sha256=source.content_sha256,
            )
        )
    if not documents:
        raise DocumentDenseError("approved corpus contains no cache-search capability")
    return DocumentCacheActivationReceipt(
        source_registry_sha256=registry.canonical_sha256,
        evidence_cutoff=registry.evidence_cutoff,
        approved_corpus_manifest_sha256=corpus.manifest_sha256,
        approved_corpus_file_manifest_sha256=corpus.manifest.file_manifest_sha256,
        document_dense_artifact_sha256=artifact.canonical_sha256,
        document_dense_vector_payload_sha256=artifact.manifest.vector_payload_sha256,
        document_bm25_sha256=_lexical_payload_sha256(artifact.records),
        embedding_snapshot_manifest_sha256=(
            artifact.manifest.embedding_snapshot_manifest_sha256
        ),
        provider=artifact.manifest.provider,
        calibration_report_sha256=calibration_report_sha256,
        minimum_dense_score=minimum_dense_score,
        minimum_dense_margin=minimum_dense_margin,
        top_k=top_k,
        documents=tuple(sorted(documents, key=lambda item: item.document_id)),
    )


def write_document_dense_artifact(
    artifact: DocumentDenseArtifact,
    output_path: str | Path,
) -> None:
    target = Path(output_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        descriptor = target.open("xb")
    except FileExistsError as error:
        raise DocumentDenseError("Document Dense artifact already exists") from error
    with descriptor:
        descriptor.write(document_dense_artifact_bytes(artifact))
        descriptor.flush()


def _write_new_read_only_file(path: str | Path, data: bytes, *, label: str) -> str:
    target = Path(path)
    if not target.is_absolute() or not target.parent.is_dir() or target.is_symlink():
        raise DocumentDenseError(f"{label} output path must be safe and absolute")
    descriptor: int | None = None
    completed = False
    try:
        descriptor = os.open(
            target,
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0),
            0o444,
        )
        view = memoryview(data)
        while view:
            view = view[os.write(descriptor, view) :]
        os.fsync(descriptor)
        completed = True
    except OSError as error:
        raise DocumentDenseError(f"{label} cannot be created") from error
    finally:
        if descriptor is not None:
            os.close(descriptor)
        if not completed and target.exists() and not target.is_symlink():
            target.unlink()
    return hashlib.sha256(data).hexdigest()


def write_approved_document_dense_artifact(
    artifact: ApprovedDocumentDenseArtifact,
    output_path: str | Path,
) -> str:
    return _write_new_read_only_file(
        output_path,
        approved_document_dense_artifact_bytes(artifact),
        label="approved Document Dense artifact",
    )


def write_document_cache_activation_receipt(
    receipt: DocumentCacheActivationReceipt,
    output_path: str | Path,
) -> str:
    return _write_new_read_only_file(
        output_path,
        document_cache_activation_receipt_bytes(receipt),
        label="Document Dense activation receipt",
    )


def load_document_dense_artifact(path: str | Path) -> DocumentDenseArtifact:
    try:
        return DocumentDenseArtifact.model_validate_json(Path(path).read_bytes())
    except (OSError, ValueError) as error:
        raise DocumentDenseError("Document Dense artifact is invalid") from error


def _read_immutable_file(
    path: str | Path,
    *,
    expected_sha256: str,
    label: str,
    max_bytes: int,
) -> bytes:
    if re.fullmatch(_SHA256_PATTERN, expected_sha256) is None:
        raise DocumentDenseError(f"{label} trust anchor is invalid")
    target = Path(path)
    if not target.is_absolute():
        raise DocumentDenseError(f"{label} path must be absolute")
    descriptor: int | None = None
    try:
        descriptor = os.open(
            target,
            os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
        )
        before = os.fstat(descriptor)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_nlink != 1
            or before.st_mode & (stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH)
            or before.st_size <= 0
            or before.st_size > max_bytes
        ):
            raise DocumentDenseError(f"{label} is not an immutable regular file")
        parts: list[bytes] = []
        remaining = before.st_size
        while remaining:
            chunk = os.read(descriptor, min(1024 * 1024, remaining))
            if not chunk:
                raise DocumentDenseError(f"{label} changed while loading")
            parts.append(chunk)
            remaining -= len(chunk)
        if os.read(descriptor, 1):
            raise DocumentDenseError(f"{label} changed while loading")
        after = os.fstat(descriptor)
        current = target.stat(follow_symlinks=False)

        def identity(item: os.stat_result) -> tuple[int, ...]:
            return (
                item.st_dev,
                item.st_ino,
                item.st_mode,
                item.st_uid,
                item.st_nlink,
                item.st_size,
                item.st_mtime_ns,
                item.st_ctime_ns,
            )

        if identity(before) != identity(after) or identity(after) != identity(current):
            raise DocumentDenseError(f"{label} changed while loading")
        data = b"".join(parts)
    except DocumentDenseError:
        raise
    except OSError:
        raise DocumentDenseError(f"{label} is unavailable") from None
    finally:
        if descriptor is not None:
            os.close(descriptor)
    if hashlib.sha256(data).hexdigest() != expected_sha256:
        raise DocumentDenseError(f"{label} differs from its trusted SHA-256")
    return data


def load_approved_document_dense_artifact(
    path: str | Path,
    *,
    expected_file_sha256: str,
) -> ApprovedDocumentDenseArtifact:
    data = _read_immutable_file(
        path,
        expected_sha256=expected_file_sha256,
        label="approved Document Dense artifact",
        max_bytes=_MAX_DOCUMENT_DENSE_FILE_BYTES,
    )
    try:
        artifact = ApprovedDocumentDenseArtifact.model_validate_json(data)
    except ValueError:
        raise DocumentDenseError("approved Document Dense artifact is invalid") from None
    if data != approved_document_dense_artifact_bytes(artifact):
        raise DocumentDenseError("approved Document Dense artifact is not canonical JSON")
    return artifact


def load_document_cache_activation_receipt(
    path: str | Path,
    *,
    expected_file_sha256: str,
) -> DocumentCacheActivationReceipt:
    data = _read_immutable_file(
        path,
        expected_sha256=expected_file_sha256,
        label="Document Dense activation receipt",
        max_bytes=2 * 1024 * 1024,
    )
    try:
        receipt = DocumentCacheActivationReceipt.model_validate_json(data)
    except ValueError:
        raise DocumentDenseError("Document Dense activation receipt is invalid") from None
    if data != document_cache_activation_receipt_bytes(receipt):
        raise DocumentDenseError("Document Dense activation receipt is not canonical JSON")
    return receipt


def _tokens(text: str) -> tuple[str, ...]:
    tokens: list[str] = []
    for item in _TOKEN.findall(text):
        normalized = item.casefold()
        tokens.append(normalized)
        for suffix in _KOREAN_QUERY_SUFFIXES:
            if normalized.endswith(suffix) and len(normalized) > len(suffix) + 1:
                tokens.append(normalized[: -len(suffix)])
                break
    return tuple(tokens)


def _lexical_payload_sha256(records: Sequence[DocumentDenseVector]) -> str:
    return hashlib.sha256(
        _canonical_json_bytes(
            [
                {
                    "chunk_id": item.chunk.chunk_id,
                    "tokens": _tokens(f"{item.chunk.title}\n{item.chunk.text}"),
                }
                for item in records
            ]
        )
    ).hexdigest()


class HybridDocumentIndex:
    """Shared deterministic BM25 + KURE index implementation."""

    def __init__(
        self,
        artifact: DocumentDenseArtifact | ApprovedDocumentDenseArtifact,
        provider: EmbeddingProvider,
    ) -> None:
        if artifact.manifest.provider != provider.metadata:
            raise DocumentDenseError("query provider differs from the frozen index provider")
        self.artifact = artifact
        self.provider = provider
        self._document_tokens = tuple(
            _tokens(f"{item.chunk.title}\n{item.chunk.text}") for item in artifact.records
        )
        self._document_frequency = Counter(
            token for tokens in self._document_tokens for token in set(tokens)
        )
        self._average_length = sum(map(len, self._document_tokens)) / len(self._document_tokens)

    def _hits(
        self,
        scores: Sequence[float],
        *,
        top_k: int,
        channel: Literal["bm25", "dense", "hybrid_rrf"],
    ) -> tuple[DocumentSearchHit, ...]:
        if not 1 <= top_k <= 50:
            raise ValueError("top_k must be between 1 and 50")
        ranked = sorted(
            enumerate(scores),
            key=lambda item: (-item[1], self.artifact.records[item[0]].chunk.chunk_id),
        )[:top_k]
        return tuple(
            DocumentSearchHit(
                chunk_id=self.artifact.records[index].chunk.chunk_id,
                document_id=self.artifact.records[index].chunk.document_id,
                title=self.artifact.records[index].chunk.title,
                text=self.artifact.records[index].chunk.text,
                score=float(score),
                rank=rank,
                channel=channel,
            )
            for rank, (index, score) in enumerate(ranked, start=1)
        )

    def bm25_search(self, query: str, *, top_k: int = 5) -> tuple[DocumentSearchHit, ...]:
        query_tokens = _tokens(query)
        if not query_tokens:
            raise ValueError("query contains no searchable tokens")
        total = len(self._document_tokens)
        scores: list[float] = []
        for tokens in self._document_tokens:
            counts = Counter(tokens)
            length = len(tokens)
            score = 0.0
            for token in set(query_tokens):
                frequency = counts[token]
                if not frequency:
                    continue
                document_frequency = self._document_frequency[token]
                inverse = math.log(
                    1 + (total - document_frequency + 0.5) / (document_frequency + 0.5)
                )
                denominator = frequency + 1.5 * (
                    1 - 0.75 + 0.75 * length / max(self._average_length, 1)
                )
                score += inverse * frequency * 2.5 / denominator
            scores.append(score)
        return self._hits(scores, top_k=top_k, channel="bm25")

    def dense_search(self, query: str, *, top_k: int = 5) -> tuple[DocumentSearchHit, ...]:
        vector = self.provider.embed_query(query)
        if len(vector) != self.artifact.manifest.provider.dimension:
            raise DocumentDenseError("query embedding dimension differs")
        norm = math.sqrt(sum(item * item for item in vector))
        if not 0.999 <= norm <= 1.001:
            raise DocumentDenseError("query embedding must be L2-normalized")
        scores = [
            sum(left * right for left, right in zip(vector, item.vector, strict=True))
            for item in self.artifact.records
        ]
        return self._hits(scores, top_k=top_k, channel="dense")

    def hybrid_search(self, query: str, *, top_k: int = 5) -> tuple[DocumentSearchHit, ...]:
        candidate_count = min(max(top_k * 8, 50), 50, len(self.artifact.records))
        lexical = self.bm25_search(query, top_k=candidate_count)
        dense = self.dense_search(query, top_k=candidate_count)
        return fuse_document_hits_rrf(lexical, dense, top_k=top_k)


class OfflineHybridDocumentIndex(HybridDocumentIndex):
    """Pending-corpus index which remains limited to offline evaluation."""

    def __init__(self, artifact: DocumentDenseArtifact, provider: EmbeddingProvider) -> None:
        if (
            artifact.manifest.production_enabled
            or artifact.manifest.scope != "offline_evaluation_only"
        ):
            raise DocumentDenseError("offline index requires a pending evaluation artifact")
        super().__init__(artifact, provider)


class ApprovedHybridDocumentIndex(HybridDocumentIndex):
    """Production-candidate index accepted only after explicit corpus activation."""

    def __init__(
        self,
        artifact: ApprovedDocumentDenseArtifact,
        provider: EmbeddingProvider,
    ) -> None:
        if (
            not artifact.manifest.production_enabled
            or artifact.manifest.scope != "production_candidate"
            or artifact.manifest.source_corpus_status != "approved_for_bm25"
        ):
            raise DocumentDenseError("production index requires an approved corpus artifact")
        super().__init__(artifact, provider)


__all__ = [
    "ApprovedDocumentCacheSource",
    "ApprovedDocumentDenseArtifact",
    "ApprovedDocumentDenseManifest",
    "ApprovedHybridDocumentIndex",
    "DocumentDenseArtifact",
    "DocumentDenseError",
    "DocumentDenseManifest",
    "DocumentCacheActivationReceipt",
    "DocumentSearchHit",
    "HybridDocumentIndex",
    "OfflineHybridDocumentIndex",
    "approved_document_dense_artifact_bytes",
    "build_approved_document_dense_artifact",
    "build_document_cache_activation_receipt",
    "build_pending_document_dense_artifact",
    "document_cache_activation_receipt_bytes",
    "document_dense_artifact_bytes",
    "fuse_document_hits_rrf",
    "load_approved_document_dense_artifact",
    "load_document_cache_activation_receipt",
    "load_document_dense_artifact",
    "write_document_dense_artifact",
    "write_approved_document_dense_artifact",
    "write_document_cache_activation_receipt",
]
