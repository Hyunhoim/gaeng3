from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from pathlib import Path

from finance_agent_core.retrieval.models import (
    DocumentEvidence,
    DocumentIngestionResult,
    DocumentInput,
    DocumentSearchRequest,
    DocumentSearchResponse,
)

_QUERY_TOKEN = re.compile(r"[0-9A-Za-z가-힣]+")
_KOREAN_SUFFIXES = (
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


class DocumentConflictError(ValueError):
    """Raised when an existing document ID is reused for different content."""


def normalize_document_text(text: str) -> str:
    """Return the canonical text representation used for hashing and indexing."""

    return "\n".join(line.rstrip() for line in text.replace("\r\n", "\n").splitlines()).strip()


def sha256_document_text(text: str) -> str:
    """Hash canonical document text rather than transport-specific line endings."""

    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _split_long_text(text: str, max_chars: int, overlap_chars: int) -> list[str]:
    if len(text) <= max_chars:
        return [text]
    chunks: list[str] = []
    start = 0
    while start < len(text):
        end = min(start + max_chars, len(text))
        if end < len(text):
            boundary = max(
                text.rfind(". ", start, end),
                text.rfind("다. ", start, end),
                text.rfind("\n", start, end),
                text.rfind(" ", start, end),
            )
            if boundary > start + max_chars // 2:
                end = boundary + 1
        chunk = text[start:end].strip()
        if chunk:
            chunks.append(chunk)
        if end >= len(text):
            break
        start = max(end - overlap_chars, start + 1)
    return chunks


def chunk_document(
    text: str,
    *,
    max_chars: int = 800,
    overlap_chars: int = 80,
) -> list[str]:
    if max_chars < 100:
        raise ValueError("max_chars must be at least 100")
    if overlap_chars < 0 or overlap_chars >= max_chars // 2:
        raise ValueError("overlap_chars must be non-negative and less than half max_chars")
    normalized = normalize_document_text(text)
    paragraphs = [
        paragraph.strip() for paragraph in re.split(r"\n\s*\n", normalized) if paragraph.strip()
    ]
    chunks: list[str] = []
    current = ""
    for paragraph in paragraphs:
        candidate = paragraph if not current else f"{current}\n\n{paragraph}"
        if len(candidate) <= max_chars:
            current = candidate
            continue
        if current:
            chunks.extend(_split_long_text(current, max_chars, overlap_chars))
            current = ""
        if len(paragraph) > max_chars:
            chunks.extend(_split_long_text(paragraph, max_chars, overlap_chars))
        else:
            current = paragraph
    if current:
        chunks.extend(_split_long_text(current, max_chars, overlap_chars))
    if not chunks:
        raise ValueError("document produced no searchable chunks")
    return chunks


def _fts_query(query: str) -> str:
    tokens = _QUERY_TOKEN.findall(query)
    if not tokens:
        raise ValueError("query contains no searchable tokens")
    unique = list(dict.fromkeys(token.casefold() for token in tokens))
    return " OR ".join(f'"{token}"' for token in unique)


def _lexical_expansion(text: str) -> str:
    expanded: list[str] = []
    for token in _QUERY_TOKEN.findall(text):
        expanded.append(token)
        for suffix in _KOREAN_SUFFIXES:
            if token.endswith(suffix) and len(token) > len(suffix) + 1:
                expanded.append(token[: -len(suffix)])
                break
    return " ".join(dict.fromkeys(expanded))


class SQLiteDocumentIndex:
    """Caller-fed BM25 index. It intentionally contains no crawler or downloader."""

    def __init__(self, database_path: str | Path) -> None:
        self.database_path = Path(database_path)

    def initialize(self) -> None:
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self.database_path) as connection:
            connection.execute("PRAGMA foreign_keys = ON")
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS documents (
                    document_id TEXT PRIMARY KEY,
                    title TEXT NOT NULL,
                    source_uri TEXT NOT NULL,
                    source_kind TEXT NOT NULL
                        CHECK (source_kind IN ('provided', 'external_approved')),
                    as_of TEXT NOT NULL,
                    document_sha256 TEXT NOT NULL,
                    metadata_json TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS document_chunks (
                    chunk_id TEXT PRIMARY KEY,
                    document_id TEXT NOT NULL REFERENCES documents(document_id),
                    ordinal INTEGER NOT NULL CHECK (ordinal >= 0),
                    text TEXT NOT NULL,
                    UNIQUE (document_id, ordinal)
                );
                CREATE VIRTUAL TABLE IF NOT EXISTS document_chunks_fts USING fts5(
                    chunk_id UNINDEXED,
                    document_id UNINDEXED,
                    title,
                    text,
                    lexemes,
                    tokenize = 'unicode61'
                );
                """
            )

    def ingest(
        self,
        document: DocumentInput,
        *,
        max_chars: int = 800,
        overlap_chars: int = 80,
    ) -> DocumentIngestionResult:
        normalized = normalize_document_text(document.text)
        document_sha256 = sha256_document_text(normalized)
        chunks = chunk_document(
            normalized,
            max_chars=max_chars,
            overlap_chars=overlap_chars,
        )
        metadata_json = json.dumps(
            document.metadata,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        with sqlite3.connect(self.database_path) as connection:
            connection.execute("PRAGMA foreign_keys = ON")
            existing = connection.execute(
                """
                SELECT document_sha256, title, source_uri, source_kind, as_of, metadata_json
                FROM documents
                WHERE document_id = ?
                """,
                (document.document_id,),
            ).fetchone()
            identity = (
                document_sha256,
                document.title,
                document.source_uri,
                document.source_kind.value,
                document.as_of.isoformat(),
                metadata_json,
            )
            if existing is not None:
                if tuple(existing) != identity:
                    raise DocumentConflictError(
                        f"document_id already exists with different content: {document.document_id}"
                    )
                chunk_count = connection.execute(
                    "SELECT COUNT(*) FROM document_chunks WHERE document_id = ?",
                    (document.document_id,),
                ).fetchone()[0]
                return DocumentIngestionResult(
                    document_id=document.document_id,
                    document_sha256=document_sha256,
                    chunk_count=int(chunk_count),
                    inserted=False,
                )
            connection.execute(
                """
                INSERT INTO documents (
                    document_id, title, source_uri, source_kind, as_of,
                    document_sha256, metadata_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    document.document_id,
                    document.title,
                    document.source_uri,
                    document.source_kind.value,
                    document.as_of.isoformat(),
                    document_sha256,
                    metadata_json,
                ),
            )
            for ordinal, text in enumerate(chunks):
                chunk_id = f"{document.document_id}:{ordinal:04d}"
                connection.execute(
                    """
                    INSERT INTO document_chunks (chunk_id, document_id, ordinal, text)
                    VALUES (?, ?, ?, ?)
                    """,
                    (chunk_id, document.document_id, ordinal, text),
                )
                connection.execute(
                    """
                    INSERT INTO document_chunks_fts (
                        chunk_id, document_id, title, text, lexemes
                    ) VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        chunk_id,
                        document.document_id,
                        document.title,
                        text,
                        _lexical_expansion(f"{document.title} {text}"),
                    ),
                )
        return DocumentIngestionResult(
            document_id=document.document_id,
            document_sha256=document_sha256,
            chunk_count=len(chunks),
            inserted=True,
        )

    def search(self, request: DocumentSearchRequest) -> DocumentSearchResponse:
        clauses = ["document_chunks_fts MATCH ?"]
        parameters: list[str | int] = [_fts_query(request.query)]
        filters = request.filters
        if filters.source_kinds:
            placeholders = ", ".join("?" for _ in filters.source_kinds)
            clauses.append(f"d.source_kind IN ({placeholders})")
            parameters.extend(item.value for item in filters.source_kinds)
        if filters.document_ids:
            placeholders = ", ".join("?" for _ in filters.document_ids)
            clauses.append(f"d.document_id IN ({placeholders})")
            parameters.extend(filters.document_ids)
        if filters.as_of_on_or_before is not None:
            clauses.append("d.as_of <= ?")
            parameters.append(filters.as_of_on_or_before.isoformat())
        for key, value in sorted(filters.metadata_equals.items()):
            clauses.append("json_extract(d.metadata_json, ?) = ?")
            parameters.extend((f"$.{key}", value))
        sql = f"""
            SELECT
                c.chunk_id,
                c.document_id,
                c.ordinal,
                c.text,
                d.title,
                d.source_uri,
                d.source_kind,
                d.as_of,
                d.document_sha256,
                d.metadata_json,
                bm25(document_chunks_fts, 0.0, 0.0, 1.0, 2.0, 1.5) AS rank
            FROM document_chunks_fts
            JOIN document_chunks AS c ON c.chunk_id = document_chunks_fts.chunk_id
            JOIN documents AS d ON d.document_id = c.document_id
            WHERE {" AND ".join(clauses)}
            ORDER BY
                ROUND(
                    bm25(document_chunks_fts, 0.0, 0.0, 1.0, 2.0, 1.5),
                    6
                ) ASC,
                CASE d.source_kind WHEN 'provided' THEN 0 ELSE 1 END ASC,
                d.document_id ASC,
                c.ordinal ASC
            LIMIT ?
        """
        parameters.append(request.top_k)
        with sqlite3.connect(self.database_path) as connection:
            connection.row_factory = sqlite3.Row
            rows = connection.execute(sql, parameters).fetchall()
        evidence = [
            DocumentEvidence(
                evidence_id=row["chunk_id"],
                document_id=row["document_id"],
                chunk_ordinal=row["ordinal"],
                title=row["title"],
                text=row["text"],
                source_uri=row["source_uri"],
                source_kind=row["source_kind"],
                as_of=row["as_of"],
                document_sha256=row["document_sha256"],
                metadata=json.loads(row["metadata_json"]),
                relevance_score=round(max(0.0, -float(row["rank"])), 9),
            )
            for row in rows
        ]
        return DocumentSearchResponse(
            status="found" if evidence else "not_found",
            query=request.query,
            evidence=evidence,
        )
