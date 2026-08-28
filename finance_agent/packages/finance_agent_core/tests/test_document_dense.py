from __future__ import annotations

import hashlib
import math
from datetime import date

import pytest

from finance_agent_core.config.knowledge_coverage import KnowledgeCapability
from finance_agent_core.retrieval.document_dense import (
    DocumentDenseError,
    OfflineHybridDocumentIndex,
    build_pending_document_dense_artifact,
)
from finance_agent_core.retrieval.official_connector import (
    OfficialHttpResponse,
    OfficialSourceConnector,
)
from finance_agent_core.retrieval.pending_corpus import (
    PendingCorpusCatalog,
    PendingCorpusSource,
    collect_pending_corpus,
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
