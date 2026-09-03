from __future__ import annotations

import hashlib
import math
from datetime import UTC, date, datetime
from types import SimpleNamespace

import pytest

from finance_agent_core.config.knowledge_coverage import KnowledgeCapability
from finance_agent_core.config.source_registry import load_official_source_registry
from finance_agent_core.contracts.external_knowledge import KnowledgeTimeRange
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
    ApprovedHybridDocumentIndex,
    DocumentDenseError,
    DocumentSearchHit,
    OfflineHybridDocumentIndex,
    build_approved_document_dense_artifact,
    build_document_cache_activation_receipt,
    build_pending_document_dense_artifact,
    write_approved_document_dense_artifact,
    write_document_cache_activation_receipt,
)
from finance_agent_core.retrieval.document_discovery import (
    CacheFirstOfficialDiscoveryProvider,
    DocumentDiscoveryError,
    build_release_pinned_cache_first_discovery,
)
from finance_agent_core.retrieval.external_knowledge_release import (
    build_cache_first_external_knowledge_artifact,
)
from finance_agent_core.retrieval.external_knowledge_release_cli import main as release_cli_main
from finance_agent_core.retrieval.official_connector import (
    OfficialDiscoveryCandidate,
    OfficialHttpResponse,
    OfficialSourceConnector,
)
from finance_agent_core.retrieval.official_discovery import OfficialDiscoveryReport
from finance_agent_core.retrieval.pending_corpus import (
    PendingCorpusCatalog,
    PendingCorpusError,
    PendingCorpusSource,
    collect_pending_corpus,
    load_pending_corpus_manifest,
    merge_pending_corpus_roots,
    rebind_pending_corpus_registry,
)
from finance_agent_core.retrieval.schema_dense import EmbeddingProviderMetadata
from finance_agent_core.retrieval.temporal_relations import (
    seal_temporal_relation_bundle,
    temporal_relation_bundle_bytes,
)

_SNAPSHOT_SHA = "9" * 64


class _Transport:
    def fetch(self, url: str, *, timeout_seconds: float, max_bytes: int) -> OfficialHttpResponse:
        assert timeout_seconds > 0
        body = (
            "<html><head><script>secret()</script></head><body>"
            "<h1>국민성장펀드</h1><p>정책금융과 민간자금이 함께 참여하는 투자구조</p>"
            "</body></html>"
        ).encode()
        assert len(body) <= max_bytes
        return OfficialHttpResponse(
            status_code=200,
            final_url=url,
            content_type="text/html",
            body=body,
        )


class _PdfExtractor:
    def extract(self, content: bytes) -> str:
        raise AssertionError(f"unexpected PDF: {len(content)}")


class _FrozenHashProvider:
    def __init__(self, model_revision: str = "1" * 40) -> None:
        self._metadata = EmbeddingProviderMetadata(
            provider_kind="frozen_model",
            provider_id="frozen-test-provider",
            model_id="test/frozen-document-model",
            model_revision=model_revision,
            license_id="apache-2.0",
            dimension=8,
            pooling="cls",
        )

    @property
    def metadata(self) -> EmbeddingProviderMetadata:
        return self._metadata

    @property
    def artifact_gate_evidence(self):
        return SimpleNamespace(snapshot_file_manifest_sha256=_SNAPSHOT_SHA)

    @staticmethod
    def _vector(text: str) -> list[float]:
        digest = hashlib.sha256(text.encode()).digest()[:8]
        vector = [float(item + 1) for item in digest]
        norm = math.sqrt(sum(item * item for item in vector))
        return [item / norm for item in vector]

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return [self._vector(text) for text in texts]

    def embed_query(self, text: str) -> list[float]:
        return self._vector(text)


class _FallbackDiscovery:
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


class _SameDocumentChunkIndex:
    def __init__(self, artifact) -> None:
        self.artifact = artifact

    def dense_search(self, _query: str, *, top_k: int):
        assert top_k >= 3
        return (
            DocumentSearchHit(
                chunk_id="national-growth-fund-structure:0000",
                document_id="national-growth-fund-structure",
                title="국민성장펀드 투자구조",
                text="첫 번째 조각",
                score=0.80,
                rank=1,
                channel="dense",
            ),
            DocumentSearchHit(
                chunk_id="national-growth-fund-structure:0001",
                document_id="national-growth-fund-structure",
                title="국민성장펀드 투자구조",
                text="두 번째 조각",
                score=0.79,
                rank=2,
                channel="dense",
            ),
            DocumentSearchHit(
                chunk_id="other-document:0000",
                document_id="other-document",
                title="다른 문서",
                text="다른 내용",
                score=0.60,
                rank=3,
                channel="dense",
            ),
        )

    def bm25_search(self, _query: str, *, top_k: int):
        assert top_k >= 1
        return ()


class _LexicalOnlyExpectedDocumentIndex:
    def __init__(self, artifact) -> None:
        self.artifact = artifact

    def dense_search(self, _query: str, *, top_k: int):
        assert top_k >= 1
        return (
            DocumentSearchHit(
                chunk_id="unrelated-document:0000",
                document_id="unrelated-document",
                title="무관 문서",
                text="무관 내용",
                score=0.99,
                rank=1,
                channel="dense",
            ),
        )

    def bm25_search(self, _query: str, *, top_k: int):
        assert top_k >= 1
        return (
            DocumentSearchHit(
                chunk_id="national-growth-fund-structure:0000",
                document_id="national-growth-fund-structure",
                title="국민성장펀드 투자구조",
                text="국민성장펀드 투자구조",
                score=8.0,
                rank=1,
                channel="bm25",
            ),
        )


class _CapabilityQueryCaptureIndex(_SameDocumentChunkIndex):
    def __init__(self, artifact) -> None:
        super().__init__(artifact)
        self.dense_queries: list[str] = []
        self.lexical_queries: list[str] = []

    def dense_search(self, query: str, *, top_k: int):
        self.dense_queries.append(query)
        return super().dense_search(query, top_k=top_k)

    def bm25_search(self, query: str, *, top_k: int):
        self.lexical_queries.append(query)
        return super().bm25_search(query, top_k=top_k)


def _pending_root(tmp_path):
    url = "https://www.fsc.go.kr/po010103/86950"
    catalog = PendingCorpusCatalog(
        catalog_id="document-dense-test-catalog",
        evidence_cutoff=date(2026, 8, 24),
        documents=(
            PendingCorpusSource(
                document_id="national-growth-fund-structure",
                title="국민성장펀드 투자구조",
                source_uri=url,
                capabilities=(KnowledgeCapability.FUND_STRUCTURE_STRATEGY_TREND,),
                published_at=date(2026, 5, 21),
                as_of=date(2026, 5, 21),
                language="ko",
                purposes=("product_structure",),
            ),
        ),
    )
    root = tmp_path / "pending"
    collect_pending_corpus(
        root,
        connector=OfficialSourceConnector(_Transport()),
        pdf_text_extractor=_PdfExtractor(),
        catalog=catalog,
    )
    return root


def _approved_corpus(tmp_path):
    root = tmp_path / "approved"
    (root / "documents").mkdir(parents=True)
    (root / "documents" / "fsc.txt").write_text(
        "국민성장펀드는 정책금융과 민간자금이 함께 참여하는 투자구조입니다.",
        encoding="utf-8",
    )
    collected = datetime(2026, 8, 20, tzinfo=UTC)
    spec = ExternalCorpusIntakeSpec(
        corpus_id="approved-document-cache-test",
        reviews=(
            CorpusReview(
                reviewer_role="data_rights",
                reviewer_id="rights-reviewer",
                reviewed_at_utc=datetime(2026, 8, 21, tzinfo=UTC),
                note="공식 공개 문서의 저장 및 검색 권리를 검토함",
            ),
            CorpusReview(
                reviewer_role="finance_domain",
                reviewer_id="finance-reviewer",
                reviewed_at_utc=datetime(2026, 8, 21, tzinfo=UTC),
                note="펀드 구조 근거와 기준일을 검토함",
            ),
        ),
        documents=(
            CorpusDocumentSpec(
                document_id="national-growth-fund-approved",
                relative_path="documents/fsc.txt",
                title="국민성장펀드 투자구조",
                publisher="금융위원회",
                source_uri="https://www.fsc.go.kr/po010103/86950",
                collected_at_utc=collected,
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
    )
    manifest = seal_approved_corpus_manifest(spec, root)
    manifest_path = root / "approved-manifest.json"
    manifest_path.write_bytes(approved_corpus_manifest_bytes(manifest))
    return verify_approved_corpus(manifest_path, root)


def test_pending_document_dense_is_hash_bound_and_offline_only(tmp_path) -> None:
    root = _pending_root(tmp_path)
    provider = _FrozenHashProvider()

    artifact = build_pending_document_dense_artifact(
        root,
        provider=provider,
        embedding_snapshot_manifest_sha256=_SNAPSHOT_SHA,
    )

    assert artifact.manifest.scope == "offline_evaluation_only"
    assert not artifact.manifest.production_enabled
    assert artifact.manifest.embedding_snapshot_manifest_sha256 == _SNAPSHOT_SHA
    assert artifact.manifest.document_count == 1
    assert artifact.manifest.chunk_count == len(artifact.records)


def test_pending_bm25_dense_and_hybrid_return_only_collected_chunks(tmp_path) -> None:
    root = _pending_root(tmp_path)
    provider = _FrozenHashProvider()
    artifact = build_pending_document_dense_artifact(
        root,
        provider=provider,
        embedding_snapshot_manifest_sha256=_SNAPSHOT_SHA,
    )
    index = OfflineHybridDocumentIndex(artifact, provider)

    assert index.bm25_search("국민성장펀드 투자구조", top_k=1)[0].document_id == (
        "national-growth-fund-structure"
    )
    assert index.dense_search("정책금융 민간자금", top_k=1)[0].channel == "dense"
    assert index.hybrid_search("국민성장펀드 구조", top_k=1)[0].channel == "hybrid_rrf"


def test_document_dense_rejects_snapshot_or_provider_substitution(tmp_path) -> None:
    root = _pending_root(tmp_path)
    provider = _FrozenHashProvider()
    artifact = build_pending_document_dense_artifact(
        root,
        provider=provider,
        embedding_snapshot_manifest_sha256=_SNAPSHOT_SHA,
    )
    snapshot = next((root / "snapshots").iterdir())
    snapshot.write_text("tampered", encoding="utf-8")

    with pytest.raises(DocumentDenseError, match="size differs|SHA-256 differs"):
        build_pending_document_dense_artifact(
            root,
            provider=provider,
            embedding_snapshot_manifest_sha256=_SNAPSHOT_SHA,
        )
    with pytest.raises(DocumentDenseError, match="query provider differs"):
        OfflineHybridDocumentIndex(artifact, _FrozenHashProvider("2" * 40))


def test_pending_manifest_registry_rebind_preserves_every_source_authority(tmp_path) -> None:
    root = _pending_root(tmp_path)
    registry = load_official_source_registry()
    manifest = load_pending_corpus_manifest(root / "pending-manifest.json")
    stale = manifest.model_copy(update={"source_registry_sha256": "0" * 64})

    rebound = rebind_pending_corpus_registry(stale, registry)

    assert rebound.source_registry_sha256 == registry.canonical_sha256
    assert rebound.documents == manifest.documents
    assert rebound.status == "not_authorized_for_retrieval_or_release"


def test_pending_manifest_registry_rebind_rejects_authority_change(tmp_path) -> None:
    root = _pending_root(tmp_path)
    registry = load_official_source_registry()
    manifest = load_pending_corpus_manifest(root / "pending-manifest.json")
    snapshot = manifest.documents[0].model_copy(update={"source_registry_id": "wrong-id"})
    changed = manifest.model_copy(update={"documents": (snapshot,)})

    with pytest.raises(PendingCorpusError, match="authority changed"):
        rebind_pending_corpus_registry(changed, registry)


def test_pending_corpus_merge_rehashes_and_deduplicates_without_approving(tmp_path) -> None:
    first = _pending_root(tmp_path)
    catalog = PendingCorpusCatalog(
        catalog_id="document-dense-second-catalog",
        evidence_cutoff=date(2026, 8, 24),
        documents=(
            PendingCorpusSource(
                document_id="national-growth-fund-structure-duplicate",
                title="국민성장펀드 투자구조 사본",
                source_uri="https://www.fsc.go.kr/po010103/86950",
                capabilities=(KnowledgeCapability.FUND_STRUCTURE_STRATEGY_TREND,),
                published_at=date(2026, 5, 21),
                as_of=date(2026, 5, 21),
                language="ko",
                purposes=("product_structure",),
            ),
        ),
    )
    second = tmp_path / "pending-second"
    collect_pending_corpus(
        second,
        connector=OfficialSourceConnector(_Transport()),
        pdf_text_extractor=_PdfExtractor(),
        catalog=catalog,
    )

    merged = merge_pending_corpus_roots(
        tmp_path / "pending-merged",
        (first, second),
        registry=load_official_source_registry(),
    )

    assert merged.status == "not_authorized_for_retrieval_or_release"
    assert merged.document_count == 1
    assert [item.reason_code for item in merged.failures] == ["duplicate_official_snapshot"]


def test_document_dense_cache_proposes_only_refetchable_official_urls(tmp_path) -> None:
    root = _pending_root(tmp_path)
    provider = _FrozenHashProvider()
    artifact = build_pending_document_dense_artifact(
        root,
        provider=provider,
        embedding_snapshot_manifest_sha256=_SNAPSHOT_SHA,
    )
    fallback = _FallbackDiscovery()
    discovery = CacheFirstOfficialDiscoveryProvider(
        index=OfflineHybridDocumentIndex(artifact, provider),
        artifact=artifact,
        corpus_manifest=load_pending_corpus_manifest(root / "pending-manifest.json"),
        corpus_manifest_path=root / "pending-manifest.json",
        fallback=fallback,  # type: ignore[arg-type]
        registry=fallback.registry,
        minimum_dense_score=-1.0,
        minimum_dense_margin=0.0,
    )

    report = discovery.discover_report(
        query="국민성장펀드 투자구조",
        capability=KnowledgeCapability.FUND_STRUCTURE_STRATEGY_TREND,
        source_registry_ids=("fsc-policy",),
        evidence_cutoff=date(2026, 8, 24),
        top_k=1,
    )

    assert [item.url for item in report.candidates] == ["https://www.fsc.go.kr/po010103/86950"]
    assert report.queried_source_registry_ids == ()
    assert fallback.calls == 0
    assert discovery.cache_queries == 1
    assert discovery.cache_hits == 1
    assert discovery.live_fallback_queries == 0


def test_document_dense_cache_adds_typed_capability_context_not_entity_rules(tmp_path) -> None:
    root = _pending_root(tmp_path)
    provider = _FrozenHashProvider()
    artifact = build_pending_document_dense_artifact(
        root,
        provider=provider,
        embedding_snapshot_manifest_sha256=_SNAPSHOT_SHA,
    )
    index = _CapabilityQueryCaptureIndex(artifact)
    fallback = _FallbackDiscovery()
    discovery = CacheFirstOfficialDiscoveryProvider(
        index=index,  # type: ignore[arg-type]
        artifact=artifact,
        corpus_manifest=load_pending_corpus_manifest(root / "pending-manifest.json"),
        corpus_manifest_path=root / "pending-manifest.json",
        fallback=fallback,  # type: ignore[arg-type]
        registry=fallback.registry,
        minimum_dense_score=-1.0,
        minimum_dense_margin=0.0,
    )

    discovery.discover_report(
        query="처음 보는 우주 산업 표현",
        capability=KnowledgeCapability.SECTOR_THEME_HISTORY,
        source_registry_ids=("fsc-policy",),
        evidence_cutoff=date(2026, 8, 24),
        top_k=1,
    )

    assert index.dense_queries == [
        "처음 보는 우주 산업 표현 테마 섹터 산업 관련 ETF 투자전략 theme sector official"
    ]
    assert index.lexical_queries == ["처음 보는 우주 산업 표현"]
    assert all(
        "에코프로" not in value and "국민성장" not in value
        for value in (*index.dense_queries, *index.lexical_queries)
    )


def test_document_dense_margin_compares_distinct_documents_not_adjacent_chunks(
    tmp_path,
) -> None:
    root = _pending_root(tmp_path)
    provider = _FrozenHashProvider()
    artifact = build_pending_document_dense_artifact(
        root,
        provider=provider,
        embedding_snapshot_manifest_sha256=_SNAPSHOT_SHA,
    )
    fallback = _FallbackDiscovery()
    discovery = CacheFirstOfficialDiscoveryProvider(
        index=_SameDocumentChunkIndex(artifact),  # type: ignore[arg-type]
        artifact=artifact,
        corpus_manifest=load_pending_corpus_manifest(root / "pending-manifest.json"),
        corpus_manifest_path=root / "pending-manifest.json",
        fallback=fallback,  # type: ignore[arg-type]
        registry=fallback.registry,
        minimum_dense_score=0.70,
        minimum_dense_margin=0.05,
    )

    report = discovery.discover_report(
        query="국민성장펀드 투자구조",
        capability=KnowledgeCapability.FUND_STRUCTURE_STRATEGY_TREND,
        source_registry_ids=("fsc-policy",),
        evidence_cutoff=date(2026, 8, 24),
        top_k=1,
    )

    assert [item.url for item in report.candidates] == ["https://www.fsc.go.kr/po010103/86950"]
    assert fallback.calls == 0


def test_document_dense_low_confidence_delegates_to_bounded_live_search(tmp_path) -> None:
    root = _pending_root(tmp_path)
    provider = _FrozenHashProvider()
    artifact = build_pending_document_dense_artifact(
        root,
        provider=provider,
        embedding_snapshot_manifest_sha256=_SNAPSHOT_SHA,
    )
    fallback = _FallbackDiscovery()
    discovery = CacheFirstOfficialDiscoveryProvider(
        index=OfflineHybridDocumentIndex(artifact, provider),
        artifact=artifact,
        corpus_manifest=load_pending_corpus_manifest(root / "pending-manifest.json"),
        corpus_manifest_path=root / "pending-manifest.json",
        fallback=fallback,  # type: ignore[arg-type]
        registry=fallback.registry,
        minimum_dense_score=1.0,
        minimum_dense_margin=2.0,
    )

    report = discovery.discover_report(
        query="처음 보는 관계 표현",
        capability=KnowledgeCapability.FUND_STRUCTURE_STRATEGY_TREND,
        source_registry_ids=("fsc-policy",),
        evidence_cutoff=date(2026, 8, 24),
        top_k=1,
    )

    assert report.queried_source_registry_ids == ("fsc-policy",)
    assert fallback.calls == 1
    assert discovery.cache_queries == 1
    assert discovery.cache_hits == 0
    assert discovery.live_fallback_queries == 1


def test_document_dense_out_of_window_candidate_delegates_to_live_search(tmp_path) -> None:
    """A semantically close but stale document must not suppress dated evidence."""

    root = _pending_root(tmp_path)
    provider = _FrozenHashProvider()
    artifact = build_pending_document_dense_artifact(
        root,
        provider=provider,
        embedding_snapshot_manifest_sha256=_SNAPSHOT_SHA,
    )
    fallback = _FallbackDiscovery()
    discovery = CacheFirstOfficialDiscoveryProvider(
        index=OfflineHybridDocumentIndex(artifact, provider),
        artifact=artifact,
        corpus_manifest=load_pending_corpus_manifest(root / "pending-manifest.json"),
        corpus_manifest_path=root / "pending-manifest.json",
        fallback=fallback,  # type: ignore[arg-type]
        registry=fallback.registry,
        minimum_dense_score=-1.0,
        minimum_dense_margin=0.0,
    )

    report = discovery.discover_report(
        query="국민성장펀드 최근 운용 동향",
        capability=KnowledgeCapability.FUND_STRUCTURE_STRATEGY_TREND,
        source_registry_ids=("fsc-policy",),
        evidence_cutoff=date(2026, 8, 24),
        top_k=1,
        time_range=KnowledgeTimeRange(
            start=date(2026, 8, 1),
            end=date(2026, 8, 24),
        ),
    )

    assert report.queried_source_registry_ids == ("fsc-policy",)
    assert fallback.calls == 1
    assert discovery.cache_hits == 0
    assert discovery.live_fallback_queries == 1


def test_document_dense_cache_keeps_an_exact_lexical_document_missing_from_dense(
    tmp_path,
) -> None:
    root = _pending_root(tmp_path)
    provider = _FrozenHashProvider()
    artifact = build_pending_document_dense_artifact(
        root,
        provider=provider,
        embedding_snapshot_manifest_sha256=_SNAPSHOT_SHA,
    )
    fallback = _FallbackDiscovery()
    discovery = CacheFirstOfficialDiscoveryProvider(
        index=_LexicalOnlyExpectedDocumentIndex(artifact),  # type: ignore[arg-type]
        artifact=artifact,
        corpus_manifest=load_pending_corpus_manifest(root / "pending-manifest.json"),
        corpus_manifest_path=root / "pending-manifest.json",
        fallback=fallback,  # type: ignore[arg-type]
        registry=fallback.registry,
        minimum_dense_score=1.0,
        minimum_dense_margin=2.0,
    )

    report = discovery.discover_report(
        query="국민성장펀드 투자구조",
        capability=KnowledgeCapability.FUND_STRUCTURE_STRATEGY_TREND,
        source_registry_ids=("fsc-policy",),
        evidence_cutoff=date(2026, 8, 24),
        top_k=1,
    )

    assert [item.url for item in report.candidates] == ["https://www.fsc.go.kr/po010103/86950"]
    assert fallback.calls == 0


def test_document_dense_cache_rejects_manifest_substitution(tmp_path) -> None:
    root = _pending_root(tmp_path)
    provider = _FrozenHashProvider()
    artifact = build_pending_document_dense_artifact(
        root,
        provider=provider,
        embedding_snapshot_manifest_sha256=_SNAPSHOT_SHA,
    )
    manifest_path = root / "pending-manifest.json"
    manifest = load_pending_corpus_manifest(manifest_path)
    manifest_path.write_bytes(manifest_path.read_bytes() + b"\n")
    fallback = _FallbackDiscovery()

    with pytest.raises(DocumentDiscoveryError, match="SHA-256 differs"):
        CacheFirstOfficialDiscoveryProvider(
            index=OfflineHybridDocumentIndex(artifact, provider),
            artifact=artifact,
            corpus_manifest=manifest,
            corpus_manifest_path=manifest_path,
            fallback=fallback,  # type: ignore[arg-type]
            registry=fallback.registry,
        )


def test_approved_document_cache_is_release_pinned_and_runtime_eligible(tmp_path) -> None:
    corpus = _approved_corpus(tmp_path)
    provider = _FrozenHashProvider("d14c8a9423946e268a0c9952fecf3a7aabd73bd9")
    registry = load_official_source_registry()
    artifact = build_approved_document_dense_artifact(
        corpus,
        provider=provider,
        embedding_snapshot_manifest_sha256=_SNAPSHOT_SHA,
    )
    receipt = build_document_cache_activation_receipt(
        corpus,
        artifact,
        registry=registry,
        calibration_report_sha256="8" * 64,
        minimum_dense_score=-1.0,
        minimum_dense_margin=0.0,
        top_k=5,
    )
    artifact_path = tmp_path / "approved-document-dense.json"
    receipt_path = tmp_path / "document-cache-activation.json"
    artifact_sha = write_approved_document_dense_artifact(artifact, artifact_path)
    receipt_sha = write_document_cache_activation_receipt(receipt, receipt_path)
    fallback = _FallbackDiscovery()

    discovery = build_release_pinned_cache_first_discovery(
        activation_receipt_path=receipt_path,
        expected_activation_receipt_sha256=receipt_sha,
        document_dense_artifact_path=artifact_path,
        expected_document_dense_sha256=artifact_sha,
        expected_corpus_manifest_sha256=corpus.manifest_sha256,
        expected_document_bm25_sha256=receipt.document_bm25_sha256,
        expected_embedding_model_revision=provider.metadata.model_revision,
        embedding=provider,
        fallback=fallback,  # type: ignore[arg-type]
        registry=registry,
    )

    assert isinstance(discovery.index, ApprovedHybridDocumentIndex)
    assert discovery.production_approved
    assert discovery.activation_binding_sha256 == receipt_sha
    relation_bundle = seal_temporal_relation_bundle(
        (),
        corpus_manifest_sha256=corpus.manifest_sha256,
        registry=registry,
    )
    release_artifact = build_cache_first_external_knowledge_artifact(
        activation=receipt,
        relation_bundle=relation_bundle,
    )
    assert release_artifact.document_activation_receipt_sha256 == receipt_sha
    assert release_artifact.document_dense_sha256 == artifact_sha
    assert release_artifact.document_bm25_sha256 == receipt.document_bm25_sha256
    report = discovery.discover_report(
        query="국민성장펀드 투자구조",
        capability=KnowledgeCapability.FUND_STRUCTURE_STRATEGY_TREND,
        source_registry_ids=("fsc-policy",),
        evidence_cutoff=date(2026, 8, 24),
        top_k=1,
    )
    assert [item.url for item in report.candidates] == ["https://www.fsc.go.kr/po010103/86950"]
    assert fallback.calls == 0


def test_cache_first_release_cli_requires_and_copies_one_approved_corpus_set(
    tmp_path,
    capsys,
) -> None:
    corpus = _approved_corpus(tmp_path)
    provider = _FrozenHashProvider("d14c8a9423946e268a0c9952fecf3a7aabd73bd9")
    registry = load_official_source_registry()
    dense = build_approved_document_dense_artifact(
        corpus,
        provider=provider,
        embedding_snapshot_manifest_sha256=_SNAPSHOT_SHA,
    )
    activation = build_document_cache_activation_receipt(
        corpus,
        dense,
        registry=registry,
        calibration_report_sha256="8" * 64,
        minimum_dense_score=-1.0,
        minimum_dense_margin=0.0,
        top_k=5,
    )
    relation_bundle = seal_temporal_relation_bundle(
        (),
        corpus_manifest_sha256=corpus.manifest_sha256,
        registry=registry,
    )
    dense_input = tmp_path / "approved-document-dense-input.json"
    activation_input = tmp_path / "document-cache-activation-input.json"
    relation_input = tmp_path / "approved-temporal-relations-input.json"
    dense_sha = write_approved_document_dense_artifact(dense, dense_input)
    activation_sha = write_document_cache_activation_receipt(
        activation,
        activation_input,
    )
    relation_input.write_bytes(temporal_relation_bundle_bytes(relation_bundle))
    relation_input.chmod(0o444)
    relation_sha = hashlib.sha256(relation_input.read_bytes()).hexdigest()
    external_output = tmp_path / "external-knowledge-artifact.json"
    relation_output = tmp_path / "external-temporal-relations.json"
    dense_output = tmp_path / "document-dense-approved.json"
    activation_output = tmp_path / "document-cache-activation.json"

    result = release_cli_main(
        [
            "--mode",
            "approved_corpus_cache_first",
            "--artifact",
            str(external_output),
            "--temporal-relations",
            str(relation_output),
            "--approved-corpus-manifest",
            str(tmp_path / "approved/approved-manifest.json"),
            "--approved-corpus-root",
            str(tmp_path / "approved"),
            "--approved-corpus-manifest-sha256",
            corpus.manifest_sha256,
            "--approved-document-dense",
            str(dense_input),
            "--approved-document-dense-sha256",
            dense_sha,
            "--document-dense-output",
            str(dense_output),
            "--document-cache-activation",
            str(activation_input),
            "--document-cache-activation-sha256",
            activation_sha,
            "--document-cache-activation-output",
            str(activation_output),
            "--approved-temporal-relations",
            str(relation_input),
            "--approved-temporal-relations-sha256",
            relation_sha,
        ]
    )

    assert result == 0
    assert dense_output.read_bytes() == dense_input.read_bytes()
    assert activation_output.read_bytes() == activation_input.read_bytes()
    assert relation_output.read_bytes() == relation_input.read_bytes()
    assert all(
        path.stat().st_mode & 0o777 == 0o444
        for path in (
            external_output,
            relation_output,
            dense_output,
            activation_output,
        )
    )
    output = capsys.readouterr().out
    assert f"external_document_dense_sha256={dense_sha}" in output
    assert f"external_document_cache_activation_sha256={activation_sha}" in output


def test_cache_first_release_cli_fails_before_output_on_corpus_hash_mismatch(
    tmp_path,
) -> None:
    corpus = _approved_corpus(tmp_path)
    provider = _FrozenHashProvider("d14c8a9423946e268a0c9952fecf3a7aabd73bd9")
    registry = load_official_source_registry()
    dense = build_approved_document_dense_artifact(
        corpus,
        provider=provider,
        embedding_snapshot_manifest_sha256=_SNAPSHOT_SHA,
    )
    activation = build_document_cache_activation_receipt(
        corpus,
        dense,
        registry=registry,
        calibration_report_sha256="8" * 64,
        minimum_dense_score=-1.0,
        minimum_dense_margin=0.0,
    )
    relation_bundle = seal_temporal_relation_bundle(
        (),
        corpus_manifest_sha256=corpus.manifest_sha256,
        registry=registry,
    )
    dense_input = tmp_path / "dense-input.json"
    activation_input = tmp_path / "activation-input.json"
    relation_input = tmp_path / "relations-input.json"
    dense_sha = write_approved_document_dense_artifact(dense, dense_input)
    activation_sha = write_document_cache_activation_receipt(
        activation,
        activation_input,
    )
    relation_input.write_bytes(temporal_relation_bundle_bytes(relation_bundle))
    relation_input.chmod(0o444)
    outputs = tuple(tmp_path / name for name in ("out-a", "out-r", "out-d", "out-c"))

    with pytest.raises(ValueError, match="approved corpus manifest differs"):
        release_cli_main(
            [
                "--mode",
                "approved_corpus_cache_first",
                "--artifact",
                str(outputs[0]),
                "--temporal-relations",
                str(outputs[1]),
                "--approved-corpus-manifest",
                str(tmp_path / "approved/approved-manifest.json"),
                "--approved-corpus-root",
                str(tmp_path / "approved"),
                "--approved-corpus-manifest-sha256",
                "0" * 64,
                "--approved-document-dense",
                str(dense_input),
                "--approved-document-dense-sha256",
                dense_sha,
                "--document-dense-output",
                str(outputs[2]),
                "--document-cache-activation",
                str(activation_input),
                "--document-cache-activation-sha256",
                activation_sha,
                "--document-cache-activation-output",
                str(outputs[3]),
                "--approved-temporal-relations",
                str(relation_input),
                "--approved-temporal-relations-sha256",
                hashlib.sha256(relation_input.read_bytes()).hexdigest(),
            ]
        )

    assert not any(path.exists() for path in outputs)


def test_release_pinned_document_cache_rejects_hash_or_pending_substitution(tmp_path) -> None:
    corpus = _approved_corpus(tmp_path)
    provider = _FrozenHashProvider("d14c8a9423946e268a0c9952fecf3a7aabd73bd9")
    registry = load_official_source_registry()
    artifact = build_approved_document_dense_artifact(
        corpus,
        provider=provider,
        embedding_snapshot_manifest_sha256=_SNAPSHOT_SHA,
    )
    receipt = build_document_cache_activation_receipt(
        corpus,
        artifact,
        registry=registry,
        calibration_report_sha256="8" * 64,
        minimum_dense_score=0.55,
        minimum_dense_margin=0.03,
    )
    artifact_path = tmp_path / "approved-document-dense.json"
    receipt_path = tmp_path / "document-cache-activation.json"
    artifact_sha = write_approved_document_dense_artifact(artifact, artifact_path)
    receipt_sha = write_document_cache_activation_receipt(receipt, receipt_path)

    with pytest.raises(DocumentDenseError, match="trusted SHA-256"):
        build_release_pinned_cache_first_discovery(
            activation_receipt_path=receipt_path,
            expected_activation_receipt_sha256="0" * 64,
            document_dense_artifact_path=artifact_path,
            expected_document_dense_sha256=artifact_sha,
            expected_corpus_manifest_sha256=corpus.manifest_sha256,
            expected_document_bm25_sha256=receipt.document_bm25_sha256,
            expected_embedding_model_revision=provider.metadata.model_revision,
            embedding=provider,
            fallback=_FallbackDiscovery(),  # type: ignore[arg-type]
            registry=registry,
        )
    assert receipt_sha == receipt.canonical_sha256
