from __future__ import annotations

import hashlib
import math
from datetime import date

import pytest

from finance_agent_core.config.knowledge_coverage import KnowledgeCapability
from finance_agent_core.config.source_registry import load_official_source_registry
from finance_agent_core.retrieval.document_dense import (
    DocumentDenseError,
    DocumentSearchHit,
    OfflineHybridDocumentIndex,
    build_pending_document_dense_artifact,
)
from finance_agent_core.retrieval.document_discovery import (
    CacheFirstOfficialDiscoveryProvider,
    DocumentDiscoveryError,
)
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
    rebind_pending_corpus_registry,
)
from finance_agent_core.retrieval.schema_dense import EmbeddingProviderMetadata

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
