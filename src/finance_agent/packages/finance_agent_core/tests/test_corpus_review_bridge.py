from __future__ import annotations

import hashlib
import json
import math
from datetime import date, timedelta
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from finance_agent_core.config.knowledge_coverage import KnowledgeCapability
from finance_agent_core.config.source_registry import load_official_source_registry
from finance_agent_core.retrieval.corpus import (
    CorpusApprovalError,
    CorpusLicenseApproval,
    CorpusReview,
    approved_corpus_manifest_bytes,
    seal_approved_corpus_manifest,
    verify_approved_corpus,
)
from finance_agent_core.retrieval.corpus_cli import main as corpus_cli_main
from finance_agent_core.retrieval.corpus_review_bridge import (
    CorpusDocumentHumanApproval,
    ExternalCorpusHumanApproval,
    bridge_pending_corpus_to_reviewed_intake,
    build_external_corpus_review_worksheet,
)
from finance_agent_core.retrieval.document_dense import (
    DocumentDenseError,
    build_approved_document_dense_artifact,
    build_document_cache_activation_receipt,
)
from finance_agent_core.retrieval.official_connector import (
    OfficialHttpResponse,
    OfficialSourceConnector,
)
from finance_agent_core.retrieval.pending_corpus import (
    PendingCorpusCatalog,
    PendingCorpusSource,
    collect_pending_corpus,
    load_pending_corpus_manifest,
    pending_corpus_manifest_bytes,
)
from finance_agent_core.retrieval.schema_dense import EmbeddingProviderMetadata

_EMBEDDING_SNAPSHOT_SHA256 = "9" * 64


class _Transport:
    def fetch(self, url: str, *, timeout_seconds: float, max_bytes: int) -> OfficialHttpResponse:
        assert timeout_seconds > 0
        content = b"<html><body>official fund structure evidence</body></html>"
        assert len(content) <= max_bytes
        return OfficialHttpResponse(
            status_code=200,
            final_url=url,
            content_type="text/html",
            body=content,
        )


class _PdfExtractor:
    def extract(self, content: bytes) -> str:
        raise AssertionError(f"unexpected PDF input: {len(content)}")


class _Embedding:
    metadata = EmbeddingProviderMetadata(
        provider_kind="frozen_model",
        provider_id="corpus-review-bridge-test",
        model_id="nlpai-lab/KURE-v1",
        model_revision="d14c8a9423946e268a0c9952fecf3a7aabd73bd9",
        license_id="cc-by-nc-sa-4.0",
        dimension=8,
        pooling="cls",
    )
    artifact_gate_evidence = SimpleNamespace(
        snapshot_file_manifest_sha256=_EMBEDDING_SNAPSHOT_SHA256,
    )

    @staticmethod
    def _vector(text: str) -> list[float]:
        values = [float(value + 1) for value in hashlib.sha256(text.encode()).digest()[:8]]
        norm = math.sqrt(sum(value * value for value in values))
        return [value / norm for value in values]

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return [self._vector(text) for text in texts]

    def embed_query(self, text: str) -> list[float]:
        return self._vector(text)


def _pending_root(tmp_path):
    registry = load_official_source_registry()
    root = tmp_path / "pending"
    collect_pending_corpus(
        root,
        connector=OfficialSourceConnector(_Transport(), registry),
        pdf_text_extractor=_PdfExtractor(),
        catalog=PendingCorpusCatalog(
            catalog_id="corpus-review-bridge-catalog",
            evidence_cutoff=registry.evidence_cutoff,
            documents=(
                PendingCorpusSource(
                    document_id="national-growth-fund-review",
                    title="국민성장펀드 투자구조",
                    source_uri="https://www.fsc.go.kr/po010103/86950",
                    capabilities=(KnowledgeCapability.FUND_STRUCTURE_STRATEGY_TREND,),
                    published_at=date(2026, 5, 21),
                    as_of=date(2026, 5, 20),
                    language="ko",
                    purposes=("product_structure", "trend"),
                ),
            ),
        ),
    )
    return root, registry


def _license() -> CorpusLicenseApproval:
    return CorpusLicenseApproval(
        license_id="official-open-use",
        license_uri="https://www.fsc.go.kr/",
        storage_allowed=True,
        retrieval_allowed=True,
        competition_use_allowed=True,
        deployment_bundle_allowed=True,
        attribution_text="금융위원회 공식 자료",
    )


def _human_approval(root, registry) -> ExternalCorpusHumanApproval:
    worksheet = build_external_corpus_review_worksheet(root, registry=registry)
    manifest = load_pending_corpus_manifest(root / "pending-manifest.json")
    reviewed_at = max(item.collected_at_utc for item in manifest.documents) + timedelta(seconds=1)
    return ExternalCorpusHumanApproval(
        pending_manifest_sha256=worksheet.pending_manifest_sha256,
        pending_source_registry_sha256=worksheet.pending_source_registry_sha256,
        source_registry_sha256=worksheet.source_registry_sha256,
        evidence_cutoff=worksheet.evidence_cutoff,
        corpus_id="external-official-corpus-reviewed-v1",
        reviews=(
            CorpusReview(
                reviewer_role="data_rights",
                reviewer_id="rights-reviewer",
                reviewed_at_utc=reviewed_at,
                note="원문 저장·검색·평가·배포 권리를 독립 검토함",
            ),
            CorpusReview(
                reviewer_role="finance_domain",
                reviewer_id="finance-reviewer",
                reviewed_at_utc=reviewed_at,
                note="금융 근거·출처·발행일·기준일을 독립 검토함",
            ),
        ),
        documents=tuple(
            CorpusDocumentHumanApproval(
                document_id=item.document_id,
                pending_snapshot_sha256=item.pending_snapshot_sha256,
                license=_license(),
            )
            for item in worksheet.documents
        ),
    )


def test_pending_metadata_survives_review_seal_and_activation(tmp_path) -> None:
    root, registry = _pending_root(tmp_path)
    worksheet = build_external_corpus_review_worksheet(root, registry=registry)
    source = worksheet.documents[0]

    assert worksheet.status == "requires_external_reviews_and_rights"
    assert source.source_registry_id == "fsc-policy"
    assert source.capabilities == (KnowledgeCapability.FUND_STRUCTURE_STRATEGY_TREND,)
    assert source.published_at == date(2026, 5, 21)
    assert source.as_of == date(2026, 5, 20)

    intake = bridge_pending_corpus_to_reviewed_intake(
        root,
        approval=_human_approval(root, registry),
        registry=registry,
    )
    manifest = seal_approved_corpus_manifest(intake, root)
    manifest_path = root / "approved-manifest.json"
    manifest_path.write_bytes(approved_corpus_manifest_bytes(manifest))
    corpus = verify_approved_corpus(manifest_path, root)
    approved = corpus.manifest.documents[0]

    assert approved.source_registry_id == source.source_registry_id
    assert approved.capabilities == source.capabilities
    assert approved.published_at == source.published_at
    assert approved.as_of == source.as_of

    dense = build_approved_document_dense_artifact(
        corpus,
        provider=_Embedding(),
        embedding_snapshot_manifest_sha256=_EMBEDDING_SNAPSHOT_SHA256,
    )
    activation = build_document_cache_activation_receipt(
        corpus,
        dense,
        registry=registry,
        calibration_report_sha256="8" * 64,
        minimum_dense_score=0.55,
        minimum_dense_margin=0.03,
    )
    activated = activation.documents[0]
    assert activated.publisher == approved.publisher
    assert activated.source_registry_id == approved.source_registry_id
    assert activated.capabilities == approved.capabilities
    assert activated.published_at == approved.published_at
    assert activated.as_of == approved.as_of


def test_bridge_requires_external_reviews_rights_and_exact_pending_hash(tmp_path) -> None:
    root, registry = _pending_root(tmp_path)
    approval = _human_approval(root, registry)

    with pytest.raises(ValidationError):
        ExternalCorpusHumanApproval.model_validate(
            {
                "pending_manifest_sha256": approval.pending_manifest_sha256,
                "pending_source_registry_sha256": (approval.pending_source_registry_sha256),
                "source_registry_sha256": approval.source_registry_sha256,
                "evidence_cutoff": approval.evidence_cutoff,
                "corpus_id": approval.corpus_id,
                "reviews": [],
                "documents": [],
            }
        )

    changed = approval.model_copy(
        update={
            "documents": (
                approval.documents[0].model_copy(update={"pending_snapshot_sha256": "0" * 64}),
            )
        }
    )
    with pytest.raises(CorpusApprovalError, match="different document metadata"):
        bridge_pending_corpus_to_reviewed_intake(
            root,
            approval=changed,
            registry=registry,
        )


def test_required_publication_date_and_capability_cannot_be_bypassed(tmp_path) -> None:
    root, registry = _pending_root(tmp_path)
    manifest_path = root / "pending-manifest.json"
    manifest = load_pending_corpus_manifest(manifest_path)
    manifest_path.write_bytes(
        pending_corpus_manifest_bytes(
            manifest.model_copy(
                update={
                    "documents": (manifest.documents[0].model_copy(update={"published_at": None}),)
                }
            )
        )
    )

    with pytest.raises(CorpusApprovalError, match="required publication date"):
        build_external_corpus_review_worksheet(root, registry=registry)


def test_metadata_only_registry_revision_is_explicitly_recorded(tmp_path) -> None:
    root, registry = _pending_root(tmp_path)
    manifest_path = root / "pending-manifest.json"
    manifest = load_pending_corpus_manifest(manifest_path)
    stale_registry_sha256 = "0" * 64
    manifest_path.write_bytes(
        pending_corpus_manifest_bytes(
            manifest.model_copy(update={"source_registry_sha256": stale_registry_sha256})
        )
    )

    worksheet = build_external_corpus_review_worksheet(root, registry=registry)

    assert worksheet.pending_source_registry_sha256 == stale_registry_sha256
    assert worksheet.source_registry_sha256 == registry.canonical_sha256
    assert (
        worksheet.pending_manifest_sha256 == hashlib.sha256(manifest_path.read_bytes()).hexdigest()
    )


def test_activation_rejects_capability_override_after_human_approval(tmp_path) -> None:
    root, registry = _pending_root(tmp_path)
    intake = bridge_pending_corpus_to_reviewed_intake(
        root,
        approval=_human_approval(root, registry),
        registry=registry,
    )
    manifest = seal_approved_corpus_manifest(intake, root)
    path = root / "approved-manifest.json"
    path.write_bytes(approved_corpus_manifest_bytes(manifest))
    corpus = verify_approved_corpus(path, root)
    dense = build_approved_document_dense_artifact(
        corpus,
        provider=_Embedding(),
        embedding_snapshot_manifest_sha256=_EMBEDDING_SNAPSHOT_SHA256,
    )

    with pytest.raises(DocumentDenseError, match="cannot override"):
        build_document_cache_activation_receipt(
            corpus,
            dense,
            registry=registry,
            calibration_report_sha256="8" * 64,
            minimum_dense_score=0.55,
            minimum_dense_margin=0.03,
            capability_overrides={
                "national-growth-fund-review": (KnowledgeCapability.PRODUCT_RISK_FACTORS,)
            },
        )


def test_activation_rechecks_registry_required_publication_date(tmp_path) -> None:
    root, registry = _pending_root(tmp_path)
    intake = bridge_pending_corpus_to_reviewed_intake(
        root,
        approval=_human_approval(root, registry),
        registry=registry,
    )
    document = intake.documents[0].model_copy(update={"published_at": None})
    untrusted = intake.model_copy(update={"documents": (document,)})
    manifest = seal_approved_corpus_manifest(untrusted, root)
    path = root / "approved-missing-publication.json"
    path.write_bytes(approved_corpus_manifest_bytes(manifest))
    corpus = verify_approved_corpus(path, root)
    dense = build_approved_document_dense_artifact(
        corpus,
        provider=_Embedding(),
        embedding_snapshot_manifest_sha256=_EMBEDDING_SNAPSHOT_SHA256,
    )

    with pytest.raises(DocumentDenseError, match="required publication date"):
        build_document_cache_activation_receipt(
            corpus,
            dense,
            registry=registry,
            calibration_report_sha256="8" * 64,
            minimum_dense_score=0.55,
            minimum_dense_margin=0.03,
        )


def test_review_cli_emits_non_authoritative_worksheet_then_requires_approval_input(
    tmp_path,
) -> None:
    root, registry = _pending_root(tmp_path)
    worksheet_path = tmp_path / "review-worksheet.json"
    corpus_cli_main(
        [
            "prepare-review",
            "--pending-root",
            str(root),
            "--output",
            str(worksheet_path),
        ]
    )
    worksheet = json.loads(worksheet_path.read_text(encoding="utf-8"))
    assert worksheet["status"] == "requires_external_reviews_and_rights"
    assert "reviews" not in worksheet
    assert "license" not in worksheet["documents"][0]

    approval = _human_approval(root, registry)
    approval_path = tmp_path / "human-approval.json"
    approval_path.write_text(
        json.dumps(approval.model_dump(mode="json"), ensure_ascii=False),
        encoding="utf-8",
    )
    intake_path = tmp_path / "reviewed-intake.json"
    corpus_cli_main(
        [
            "bridge-pending",
            "--pending-root",
            str(root),
            "--approval-input",
            str(approval_path),
            "--output",
            str(intake_path),
        ]
    )
    intake = json.loads(intake_path.read_text(encoding="utf-8"))
    assert intake["schema_version"] == "1.1"
    assert intake["documents"][0]["source_registry_id"] == "fsc-policy"
    assert intake["documents"][0]["published_at"] == "2026-05-21"
