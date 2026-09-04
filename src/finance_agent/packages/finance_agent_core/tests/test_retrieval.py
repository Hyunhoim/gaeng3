from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from finance_agent_core.retrieval import (
    DocumentConflictError,
    DocumentFilters,
    DocumentInput,
    DocumentSearchRequest,
    SQLiteDocumentIndex,
    chunk_document,
)


def _document(
    document_id: str,
    *,
    source_kind: str,
    text: str = "위험등급은 제공 데이터의 상품별 위험 분류를 뜻합니다.",
    as_of: date = date(2026, 7, 11),
    category: str = "glossary",
) -> DocumentInput:
    return DocumentInput(
        document_id=document_id,
        title=f"문서 {document_id}",
        text=text,
        source_uri=f"approved://{document_id}",
        source_kind=source_kind,
        as_of=as_of,
        metadata={"category": category},
    )


def test_document_index_is_idempotent_and_rejects_conflicting_overwrite(
    tmp_path: Path,
) -> None:
    index = SQLiteDocumentIndex(tmp_path / "documents.sqlite3")
    index.initialize()
    document = _document("provided-terms", source_kind="provided")

    first = index.ingest(document, max_chars=120, overlap_chars=20)
    second = index.ingest(document, max_chars=120, overlap_chars=20)

    assert first.inserted
    assert not second.inserted
    assert first.document_sha256 == second.document_sha256
    assert first.chunk_count == second.chunk_count
    with pytest.raises(DocumentConflictError, match="different content"):
        index.ingest(
            document.model_copy(update={"text": "같은 ID에 다른 문서"}),
            max_chars=120,
            overlap_chars=20,
        )


def test_bm25_search_returns_field_level_document_evidence_and_provided_priority(
    tmp_path: Path,
) -> None:
    index = SQLiteDocumentIndex(tmp_path / "documents.sqlite3")
    index.initialize()
    index.ingest(_document("z-provided", source_kind="provided"))
    index.ingest(_document("a-external", source_kind="external_approved"))

    result = index.search(
        DocumentSearchRequest(
            query="위험등급 제공 데이터",
            top_k=2,
        )
    )

    assert result.status == "found"
    assert [item.document_id for item in result.evidence] == [
        "z-provided",
        "a-external",
    ]
    evidence = result.evidence[0]
    assert evidence.source_uri == "approved://z-provided"
    assert evidence.source_kind.value == "provided"
    assert evidence.as_of == date(2026, 7, 11)
    assert evidence.document_sha256
    assert evidence.metadata == {"category": "glossary"}
    assert "위험등급" in evidence.text


def test_document_search_filters_metadata_source_and_date(tmp_path: Path) -> None:
    index = SQLiteDocumentIndex(tmp_path / "documents.sqlite3")
    index.initialize()
    index.ingest(
        _document(
            "provided-old",
            source_kind="provided",
            as_of=date(2026, 6, 1),
            category="terms",
        )
    )
    index.ingest(
        _document(
            "external-new",
            source_kind="external_approved",
            as_of=date(2026, 7, 20),
            category="terms",
        )
    )
    index.ingest(
        _document(
            "provided-other",
            source_kind="provided",
            as_of=date(2026, 6, 1),
            category="policy",
        )
    )

    result = index.search(
        DocumentSearchRequest(
            query="위험등급",
            filters=DocumentFilters(
                source_kinds=["provided"],
                as_of_on_or_before=date(2026, 7, 1),
                metadata_equals={"category": "terms"},
            ),
        )
    )

    assert result.status == "found"
    assert [item.document_id for item in result.evidence] == ["provided-old"]


def test_document_search_returns_explicit_not_found_and_escapes_query(
    tmp_path: Path,
) -> None:
    index = SQLiteDocumentIndex(tmp_path / "documents.sqlite3")
    index.initialize()
    index.ingest(_document("provided-terms", source_kind="provided"))

    not_found = index.search(DocumentSearchRequest(query="만기수익률"))
    injection = index.search(DocumentSearchRequest(query='" OR DROP TABLE documents --'))
    still_found = index.search(DocumentSearchRequest(query="위험등급"))

    assert not_found.status == "not_found"
    assert not_found.evidence == []
    assert injection.status in {"found", "not_found"}
    assert still_found.status == "found"


def test_chunking_is_deterministic_and_bounded() -> None:
    text = "\n\n".join(f"{index}번째 문단 " + ("금융상품 설명 " * 20) for index in range(8))

    first = chunk_document(text, max_chars=180, overlap_chars=30)
    second = chunk_document(text, max_chars=180, overlap_chars=30)

    assert first == second
    assert len(first) > 1
    assert all(0 < len(chunk) <= 180 for chunk in first)
