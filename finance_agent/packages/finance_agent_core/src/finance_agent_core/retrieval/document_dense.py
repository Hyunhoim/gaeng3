from __future__ import annotations

import hashlib
import json
import math
import re
from collections import Counter
from collections.abc import Sequence
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, StrictBool, field_validator, model_validator

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
_TOKEN = re.compile(r"[0-9A-Za-z가-힣]+")


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


class DocumentSearchHit(DocumentDenseModel):
    chunk_id: str
    document_id: str
    title: str
    text: str
    score: float
    rank: int = Field(gt=0)
    channel: Literal["bm25", "dense", "hybrid_rrf"]


def document_dense_artifact_bytes(artifact: DocumentDenseArtifact) -> bytes:
    return _canonical_json_bytes(artifact.model_dump(mode="json"))


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


def load_document_dense_artifact(path: str | Path) -> DocumentDenseArtifact:
    try:
        return DocumentDenseArtifact.model_validate_json(Path(path).read_bytes())
    except (OSError, ValueError) as error:
        raise DocumentDenseError("Document Dense artifact is invalid") from error


def _tokens(text: str) -> tuple[str, ...]:
    return tuple(item.casefold() for item in _TOKEN.findall(text))


class OfflineHybridDocumentIndex:
    """BM25 + KURE candidate search; deliberately unable to serve production answers."""

    def __init__(self, artifact: DocumentDenseArtifact, provider: EmbeddingProvider) -> None:
        if artifact.manifest.provider != provider.metadata:
            raise DocumentDenseError("query provider differs from the frozen index provider")
        self.artifact = artifact
        self.provider = provider
        self._document_tokens = tuple(_tokens(item.chunk.text) for item in artifact.records)
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
        candidate_count = min(max(top_k * 4, 20), 50, len(self.artifact.records))
        lexical = self.bm25_search(query, top_k=candidate_count)
        dense = self.dense_search(query, top_k=candidate_count)
        scores: dict[str, float] = {}
        for hits in (lexical, dense):
            for hit in hits:
                scores[hit.chunk_id] = scores.get(hit.chunk_id, 0.0) + 1 / (60 + hit.rank)
        by_id = {item.chunk.chunk_id: item for item in self.artifact.records}
        ordered = sorted(scores.items(), key=lambda item: (-item[1], item[0]))[:top_k]
        return tuple(
            DocumentSearchHit(
                chunk_id=chunk_id,
                document_id=by_id[chunk_id].chunk.document_id,
                title=by_id[chunk_id].chunk.title,
                text=by_id[chunk_id].chunk.text,
                score=score,
                rank=rank,
                channel="hybrid_rrf",
            )
            for rank, (chunk_id, score) in enumerate(ordered, start=1)
        )


__all__ = [
    "DocumentDenseArtifact",
    "DocumentDenseError",
    "DocumentDenseManifest",
    "DocumentSearchHit",
    "OfflineHybridDocumentIndex",
    "build_pending_document_dense_artifact",
    "document_dense_artifact_bytes",
    "load_document_dense_artifact",
    "write_document_dense_artifact",
]
