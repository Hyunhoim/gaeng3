from __future__ import annotations

import hashlib
import math
from datetime import UTC, date, datetime
from pathlib import Path
from types import SimpleNamespace

from fastapi.testclient import TestClient
from finance_agent_core.agent import RoutedFinanceAgent
from finance_agent_core.config.knowledge_coverage import KnowledgeCapability
from finance_agent_core.config.source_registry import load_official_source_registry
from finance_agent_core.retrieval.corpus import (
    CorpusDocumentSpec,
    CorpusLicenseApproval,
    CorpusReview,
    ExternalCorpusIntakeSpec,
    approved_corpus_manifest_bytes,
    seal_approved_corpus_manifest,
    verify_approved_corpus,
)
from finance_agent_core.retrieval.document_dense import (
    build_approved_document_dense_artifact,
    build_document_cache_activation_receipt,
    write_approved_document_dense_artifact,
    write_document_cache_activation_receipt,
)
from finance_agent_core.retrieval.document_discovery import (
    CacheFirstOfficialDiscoveryProvider,
    build_release_pinned_cache_first_discovery,
)
from finance_agent_core.retrieval.official_connector import OfficialDiscoveryCandidate
from finance_agent_core.retrieval.official_discovery import OfficialDiscoveryReport
from finance_agent_core.retrieval.schema_dense import EmbeddingProviderMetadata

from app.config import Settings
from app.main import create_app

_KURE_REVISION = "d14c8a9423946e268a0c9952fecf3a7aabd73bd9"
_SNAPSHOT_SHA256 = "9" * 64


class _FakeKureEmbedding:
    metadata = EmbeddingProviderMetadata(
        provider_kind="frozen_model",
        provider_id="fake-kure-document-get-e2e",
        model_id="nlpai-lab/KURE-v1",
        model_revision=_KURE_REVISION,
        license_id="cc-by-nc-sa-4.0",
        dimension=8,
        pooling="cls",
    )
    artifact_gate_evidence = SimpleNamespace(
        manifest_file_sha256=_SNAPSHOT_SHA256,
        snapshot_file_manifest_sha256=_SNAPSHOT_SHA256,
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


class _FakeLiveFallback:
    def __init__(self) -> None:
        self.registry = load_official_source_registry()
        self.calls = 0

    def discover_report(self, **_kwargs) -> OfficialDiscoveryReport:
        self.calls += 1
        return OfficialDiscoveryReport(
            candidates=(
                OfficialDiscoveryCandidate(
                    url="https://www.fsc.go.kr/po010103/86950",
                    title="금융위원회 공식 검색 결과",
                    published_at=date(2026, 5, 21),
                    as_of=date(2026, 5, 21),
                ),
            ),
            queried_source_registry_ids=("fsc-policy",),
            exhaustive=False,
        )


class _DocumentCacheGetProbeService:
    """HTTP test seam that invokes the real approved cache before a safe control response."""

    def __init__(self, discovery: CacheFirstOfficialDiscoveryProvider) -> None:
        self._delegate = RoutedFinanceAgent({})
        self.router = self._delegate.router
        self.discovery = discovery
        self.last_report: OfficialDiscoveryReport | None = None

    def answer(self, question: str, request_id: str):
        self.last_report = self.discovery.discover_report(
            query=question,
            capability=KnowledgeCapability.FUND_STRUCTURE_STRATEGY_TREND,
            source_registry_ids=("fsc-policy",),
            evidence_cutoff=date(2026, 8, 24),
            top_k=1,
        )
        return self._delegate.answer("오늘 날씨를 알려줘", request_id)


def _approved_cache(tmp_path: Path):
    root = tmp_path / "approved"
    (root / "documents").mkdir(parents=True)
    (root / "documents/fsc.txt").write_text(
        "국민성장펀드는 정책금융과 민간자금이 함께 참여하는 투자구조입니다.",
        encoding="utf-8",
    )
    collected = datetime(2026, 8, 20, tzinfo=UTC)
    manifest = seal_approved_corpus_manifest(
        ExternalCorpusIntakeSpec(
            corpus_id="fastapi-document-cache-get-e2e",
            reviews=(
                CorpusReview(
                    reviewer_role="data_rights",
                    reviewer_id="rights-reviewer",
                    reviewed_at_utc=datetime(2026, 8, 21, tzinfo=UTC),
                    note="공식 공개 문서 저장 및 검색 권리 검토",
                ),
                CorpusReview(
                    reviewer_role="finance_domain",
                    reviewer_id="finance-reviewer",
                    reviewed_at_utc=datetime(2026, 8, 21, tzinfo=UTC),
                    note="금융상품 근거와 기준일 검토",
                ),
            ),
            documents=(
                CorpusDocumentSpec(
                    document_id="national-growth-fund-get-e2e",
                    relative_path="documents/fsc.txt",
                    title="국민성장펀드 투자구조",
                    publisher="금융위원회",
                    source_registry_id="fsc-policy",
                    source_uri="https://www.fsc.go.kr/po010103/86950",
                    capabilities=(KnowledgeCapability.FUND_STRUCTURE_STRATEGY_TREND,),
                    collected_at_utc=collected,
                    published_at=date(2026, 5, 21),
                    as_of=date(2026, 5, 21),
                    language="ko",
                    media_type="text/plain",
                    purposes=("product_structure",),
                    license=CorpusLicenseApproval(
                        license_id="official-open-use",
                        license_uri="https://www.fsc.go.kr/",
                        storage_allowed=True,
                        retrieval_allowed=True,
                        competition_use_allowed=True,
                        deployment_bundle_allowed=True,
                        attribution_text="금융위원회 공식 자료",
                    ),
                ),
            ),
        ),
        root,
    )
    manifest_path = root / "approved-manifest.json"
    manifest_path.write_bytes(approved_corpus_manifest_bytes(manifest))
    corpus = verify_approved_corpus(manifest_path, root)
    embedding = _FakeKureEmbedding()
    dense = build_approved_document_dense_artifact(
        corpus,
        provider=embedding,
        embedding_snapshot_manifest_sha256=_SNAPSHOT_SHA256,
    )
    registry = load_official_source_registry()
    activation = build_document_cache_activation_receipt(
        corpus,
        dense,
        registry=registry,
        calibration_report_sha256="8" * 64,
        minimum_dense_score=1.0,
        minimum_dense_margin=2.0,
        top_k=1,
    )
    dense_path = tmp_path / "document-dense-approved.json"
    activation_path = tmp_path / "document-cache-activation.json"
    dense_sha256 = write_approved_document_dense_artifact(dense, dense_path)
    activation_sha256 = write_document_cache_activation_receipt(
        activation,
        activation_path,
    )
    fallback = _FakeLiveFallback()
    discovery = build_release_pinned_cache_first_discovery(
        activation_receipt_path=activation_path,
        expected_activation_receipt_sha256=activation_sha256,
        document_dense_artifact_path=dense_path,
        expected_document_dense_sha256=dense_sha256,
        expected_corpus_manifest_sha256=corpus.manifest_sha256,
        expected_document_bm25_sha256=activation.document_bm25_sha256,
        expected_embedding_model_revision=_KURE_REVISION,
        embedding=embedding,
        fallback=fallback,  # type: ignore[arg-type]
        registry=registry,
        approved_corpus_manifest_path=manifest_path,
        approved_corpus_root=root,
    )
    return discovery, fallback


def test_fake_official_get_uses_approved_document_cache_hit(tmp_path: Path) -> None:
    discovery, fallback = _approved_cache(tmp_path)
    service = _DocumentCacheGetProbeService(discovery)
    application = create_app(settings=Settings(), agent=service)  # type: ignore[arg-type]

    with TestClient(application) as client:
        response = client.get(
            "/answer",
            params={
                "question_id": "DOC-CACHE-HIT",
                "question": "국민성장펀드 투자구조",
            },
        )

    assert response.status_code == 200
    assert discovery.cache_queries == 1
    assert discovery.cache_hits == 1
    assert discovery.live_fallback_queries == 0
    assert fallback.calls == 0
    assert service.last_report is not None
    assert [item.url for item in service.last_report.candidates] == [
        "https://www.fsc.go.kr/po010103/86950"
    ]


def test_fake_official_get_cache_miss_uses_bounded_live_fallback(tmp_path: Path) -> None:
    discovery, fallback = _approved_cache(tmp_path)
    service = _DocumentCacheGetProbeService(discovery)
    application = create_app(settings=Settings(), agent=service)  # type: ignore[arg-type]

    with TestClient(application) as client:
        response = client.get(
            "/answer",
            params={
                "question_id": "DOC-CACHE-MISS",
                "question": "처음 보는 완전히 다른 공식 동향 표현",
            },
        )

    assert response.status_code == 200
    assert discovery.cache_queries == 1
    assert discovery.cache_hits == 0
    assert discovery.live_fallback_queries == 1
    assert fallback.calls == 1
