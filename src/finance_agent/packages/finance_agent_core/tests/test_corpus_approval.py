from __future__ import annotations

import json
import os
from datetime import UTC, date, datetime
from pathlib import Path

import pytest
from pydantic import ValidationError

from finance_agent_core.retrieval import (
    CorpusApprovalError,
    CorpusDocumentSpec,
    CorpusLicenseApproval,
    CorpusReview,
    DocumentSearchRequest,
    ExternalCorpusIntakeSpec,
    SQLiteDocumentIndex,
    approved_corpus_manifest_bytes,
    build_approved_corpus_index,
    load_external_corpus_intake_spec,
    seal_approved_corpus_manifest,
    verify_approved_corpus,
    write_new_read_only_file,
)

COLLECTED_AT = datetime(2026, 8, 18, 3, 0, tzinfo=UTC)
REVIEWED_AT = datetime(2026, 8, 19, 3, 0, tzinfo=UTC)


def _license(**overrides: object) -> CorpusLicenseApproval:
    payload: dict[str, object] = {
        "license_id": "custom-approved-v1",
        "license_uri": "https://example.com/terms",
        "storage_allowed": True,
        "retrieval_allowed": True,
        "competition_use_allowed": True,
        "deployment_bundle_allowed": True,
        "attribution_text": "Example Financial Authority",
    }
    payload.update(overrides)
    return CorpusLicenseApproval.model_validate(payload)


def _document(
    *,
    document_id: str = "risk-terms-v1",
    relative_path: str = "terms/risk.md",
) -> CorpusDocumentSpec:
    return CorpusDocumentSpec(
        document_id=document_id,
        relative_path=relative_path,
        title="금융상품 위험등급 설명",
        publisher="Example Financial Authority",
        source_uri="https://example.com/finance/risk",
        collected_at_utc=COLLECTED_AT,
        as_of=date(2026, 8, 1),
        language="ko",
        media_type="text/markdown" if relative_path.endswith(".md") else "text/plain",
        purposes=("risk",),
        license=_license(),
    )


def _reviews(*, same_reviewer: bool = False) -> tuple[CorpusReview, CorpusReview]:
    return (
        CorpusReview(
            reviewer_role="data_rights",
            reviewer_id="rights-reviewer",
            reviewed_at_utc=REVIEWED_AT,
            note="저장·검색·평가·배포 사용 범위를 확인함",
        ),
        CorpusReview(
            reviewer_role="finance_domain",
            reviewer_id="rights-reviewer" if same_reviewer else "finance-reviewer",
            reviewed_at_utc=REVIEWED_AT,
            note="금융상품 설명 근거로 사용할 범위를 확인함",
        ),
    )


def _spec(*, documents: tuple[CorpusDocumentSpec, ...] | None = None) -> ExternalCorpusIntakeSpec:
    return ExternalCorpusIntakeSpec(
        corpus_id="external-finance-docs-v1",
        reviews=_reviews(),
        documents=documents or (_document(),),
    )


def _write_intake_spec(path: Path, spec: ExternalCorpusIntakeSpec) -> None:
    path.write_text(
        json.dumps(spec.model_dump(mode="json"), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def _sealed_corpus(tmp_path: Path) -> tuple[Path, Path]:
    corpus_root = tmp_path / "corpus"
    document_path = corpus_root / "terms" / "risk.md"
    document_path.parent.mkdir(parents=True)
    document_path.write_text(
        "# 위험등급\n\n금융상품 위험등급은 손실 가능성을 비교하기 위한 분류입니다.\n",
        encoding="utf-8",
    )
    intake_path = tmp_path / "intake.json"
    _write_intake_spec(intake_path, _spec())
    manifest = seal_approved_corpus_manifest(
        load_external_corpus_intake_spec(intake_path),
        corpus_root,
    )
    manifest_path = tmp_path / "approved-corpus.json"
    write_new_read_only_file(manifest_path, approved_corpus_manifest_bytes(manifest))
    return corpus_root, manifest_path


def test_seal_verify_build_and_search_approved_corpus(tmp_path: Path) -> None:
    corpus_root, manifest_path = _sealed_corpus(tmp_path)

    verified = verify_approved_corpus(manifest_path, corpus_root)
    assert verified.receipt.status == "verified_not_release_activated"
    assert verified.receipt.document_count == 1

    database_path = tmp_path / "approved-corpus.sqlite3"
    receipt = build_approved_corpus_index(verified, database_path)

    assert receipt.status == "verified_index_not_release_activated"
    assert receipt.document_count == 1
    assert receipt.chunk_count == 1
    assert len(receipt.database_sha256) == 64
    assert database_path.stat().st_mode & 0o222 == 0

    result = SQLiteDocumentIndex(database_path).search(
        DocumentSearchRequest(query="위험등급 손실 가능성")
    )
    assert result.status == "found"
    assert result.evidence[0].document_id == "risk-terms-v1"
    assert result.evidence[0].source_kind.value == "external_approved"
    assert result.evidence[0].metadata["corpus_id"] == "external-finance-docs-v1"


@pytest.mark.parametrize(
    "permission",
    [
        "storage_allowed",
        "retrieval_allowed",
        "competition_use_allowed",
        "deployment_bundle_allowed",
    ],
)
def test_license_requires_every_declared_usage_right(permission: str) -> None:
    with pytest.raises(ValidationError, match="every declared usage right"):
        _license(**{permission: False})


def test_corpus_requires_independent_finance_and_rights_reviewers() -> None:
    with pytest.raises(ValidationError, match="independent reviewer IDs"):
        ExternalCorpusIntakeSpec(
            corpus_id="external-finance-docs-v1",
            reviews=_reviews(same_reviewer=True),
            documents=(_document(),),
        )


@pytest.mark.parametrize(
    ("relative_path", "message"),
    [
        ("../escape.md", "canonical, relative, and text-only"),
        ("terms\\risk.md", "safe POSIX separators"),
        ("terms/risk.pdf", "canonical, relative, and text-only"),
    ],
)
def test_document_snapshot_path_is_restricted(relative_path: str, message: str) -> None:
    with pytest.raises(ValidationError, match=message):
        _document(relative_path=relative_path)


def test_tampered_snapshot_is_rejected_before_indexing(tmp_path: Path) -> None:
    corpus_root, manifest_path = _sealed_corpus(tmp_path)
    document_path = corpus_root / "terms" / "risk.md"
    original = document_path.read_bytes()
    document_path.write_bytes(original.replace(b"#", b"!", 1))

    with pytest.raises(CorpusApprovalError, match="approved SHA-256"):
        verify_approved_corpus(manifest_path, corpus_root)


def test_noncanonical_approved_manifest_is_rejected(tmp_path: Path) -> None:
    corpus_root, manifest_path = _sealed_corpus(tmp_path)
    os.chmod(manifest_path, 0o644)
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    with pytest.raises(CorpusApprovalError, match="canonical file form"):
        verify_approved_corpus(manifest_path, corpus_root)


def test_duplicate_manifest_json_key_is_rejected(tmp_path: Path) -> None:
    corpus_root, manifest_path = _sealed_corpus(tmp_path)
    os.chmod(manifest_path, 0o644)
    text = manifest_path.read_text(encoding="utf-8")
    manifest_path.write_text(
        text.replace('"document_count":1', '"document_count":1,"document_count":1', 1),
        encoding="utf-8",
    )

    with pytest.raises(CorpusApprovalError, match="duplicate JSON key"):
        verify_approved_corpus(manifest_path, corpus_root)


def test_snapshot_symlink_is_rejected(tmp_path: Path) -> None:
    corpus_root = tmp_path / "corpus"
    target = tmp_path / "outside.md"
    target.write_text("외부 파일", encoding="utf-8")
    snapshot = corpus_root / "terms" / "risk.md"
    snapshot.parent.mkdir(parents=True)
    snapshot.symlink_to(target)
    intake_path = tmp_path / "intake.json"
    _write_intake_spec(intake_path, _spec())

    with pytest.raises(CorpusApprovalError, match="symbolic links"):
        seal_approved_corpus_manifest(
            load_external_corpus_intake_spec(intake_path),
            corpus_root,
        )


def test_existing_outputs_are_never_overwritten(tmp_path: Path) -> None:
    output = tmp_path / "existing.json"
    output.write_text("keep", encoding="utf-8")

    with pytest.raises(CorpusApprovalError, match="already exists"):
        write_new_read_only_file(output, b"replacement")

    assert output.read_text(encoding="utf-8") == "keep"


def test_existing_index_is_never_overwritten(tmp_path: Path) -> None:
    corpus_root, manifest_path = _sealed_corpus(tmp_path)
    verified = verify_approved_corpus(manifest_path, corpus_root)
    output = tmp_path / "existing.sqlite3"
    output.write_bytes(b"keep")

    with pytest.raises(CorpusApprovalError, match="already exists"):
        build_approved_corpus_index(verified, output)

    assert output.read_bytes() == b"keep"


def test_http_source_and_license_urls_are_rejected() -> None:
    with pytest.raises(ValidationError, match="HTTPS URL"):
        _license(license_uri="http://example.com/terms")

    payload = _document().model_dump(mode="python")
    payload["source_uri"] = "http://example.com/finance/risk"
    with pytest.raises(ValidationError, match="HTTPS URL"):
        CorpusDocumentSpec.model_validate(payload)


def test_utf8_bom_is_rejected(tmp_path: Path) -> None:
    corpus_root = tmp_path / "corpus"
    document_path = corpus_root / "terms" / "risk.md"
    document_path.parent.mkdir(parents=True)
    document_path.write_bytes(b"\xef\xbb\xbf# risk")
    intake_path = tmp_path / "intake.json"
    _write_intake_spec(intake_path, _spec())

    with pytest.raises(CorpusApprovalError, match="UTF-8 BOM"):
        seal_approved_corpus_manifest(
            load_external_corpus_intake_spec(intake_path),
            corpus_root,
        )


def test_corpus_documents_must_be_sorted_and_unique() -> None:
    first = _document(document_id="z-document", relative_path="terms/z.md")
    second = _document(document_id="a-document", relative_path="terms/a.md")

    with pytest.raises(ValidationError, match="unique, sorted document IDs"):
        _spec(documents=(first, second))


def test_review_cannot_predate_collection() -> None:
    stale_review = CorpusReview(
        reviewer_role="data_rights",
        reviewer_id="rights-reviewer",
        reviewed_at_utc=datetime(2026, 8, 17, tzinfo=UTC),
        note="문서 수집 이전의 잘못된 검토 기록",
    )
    with pytest.raises(ValidationError, match="cannot predate"):
        ExternalCorpusIntakeSpec(
            corpus_id="external-finance-docs-v1",
            reviews=(stale_review, _reviews()[1]),
            documents=(_document(),),
        )
