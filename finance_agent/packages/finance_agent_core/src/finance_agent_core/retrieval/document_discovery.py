from __future__ import annotations

import hashlib
import threading
from datetime import date
from pathlib import Path

from finance_agent_core.config.knowledge_coverage import KnowledgeCapability
from finance_agent_core.config.source_registry import (
    OfficialSourceRegistry,
    OfficialSourceRegistryError,
)
from finance_agent_core.contracts.external_knowledge import KnowledgeTimeRange
from finance_agent_core.retrieval.document_dense import (
    ApprovedDocumentDenseArtifact,
    ApprovedHybridDocumentIndex,
    DocumentCacheActivationReceipt,
    DocumentDenseArtifact,
    HybridDocumentIndex,
    document_cache_activation_receipt_bytes,
    fuse_document_hits_rrf,
    load_approved_document_dense_artifact,
    load_document_cache_activation_receipt,
)
from finance_agent_core.retrieval.official_connector import OfficialDiscoveryCandidate
from finance_agent_core.retrieval.official_discovery import (
    BoundedOfficialDiscoveryProvider,
    OfficialDiscoveryError,
    OfficialDiscoveryReport,
)
from finance_agent_core.retrieval.pending_corpus import (
    PendingCorpusManifest,
    pending_corpus_manifest_bytes,
)
from finance_agent_core.retrieval.schema_dense import EmbeddingProvider


class DocumentDiscoveryError(RuntimeError):
    """Raised when a candidate-only document cache crosses its trust boundary."""


_CAPABILITY_CACHE_QUERY_CONTEXT: dict[KnowledgeCapability, str] = {
    KnowledgeCapability.PORTFOLIO_HOLDINGS: (
        "편입 보유 구성종목 holdings portfolio official factsheet"
    ),
    KnowledgeCapability.CORPORATE_SUBSIDIARY: (
        "자회사 계열사 관계사 subsidiary corporate group official"
    ),
    KnowledgeCapability.SECTOR_THEME_HISTORY: (
        "테마 섹터 산업 관련 ETF 투자전략 theme sector official"
    ),
    KnowledgeCapability.FUND_STRUCTURE_STRATEGY_TREND: (
        "펀드 구조 투자전략 판매 운용 동향 official"
    ),
    KnowledgeCapability.PRODUCT_RISK_FACTORS: ("위험요인 투자위험 원금손실 유의사항 risk official"),
}


class CacheFirstOfficialDiscoveryProvider:
    """Use Document Dense only to propose official URLs, then fall back to live search.

    The pending corpus and its chunks never become answer evidence.  Every URL
    returned here is authorized by the Source Registry and is fetched again by
    ``OfficialSourceConnector`` before an exact evidence span can be admitted.
    A low-confidence cache lookup is treated as a miss and delegates to the
    bounded NAVER API HUB provider.
    """

    def __init__(
        self,
        *,
        index: HybridDocumentIndex,
        artifact: DocumentDenseArtifact | ApprovedDocumentDenseArtifact,
        corpus_manifest: PendingCorpusManifest | DocumentCacheActivationReceipt,
        corpus_manifest_path: str | Path,
        fallback: BoundedOfficialDiscoveryProvider,
        registry: OfficialSourceRegistry,
        minimum_dense_score: float = 0.55,
        minimum_dense_margin: float = 0.03,
    ) -> None:
        if not -1 <= minimum_dense_score <= 1:
            raise ValueError("document cache score threshold is out of bounds")
        if not 0 <= minimum_dense_margin <= 2:
            raise ValueError("document cache margin threshold is out of bounds")
        if index.artifact is not artifact:
            raise DocumentDiscoveryError("document index and artifact identities differ")
        manifest_path = Path(corpus_manifest_path)
        try:
            raw_manifest = manifest_path.read_bytes()
        except OSError as error:
            raise DocumentDiscoveryError("document corpus manifest is unavailable") from error
        if isinstance(corpus_manifest, PendingCorpusManifest):
            if not isinstance(artifact, DocumentDenseArtifact):
                raise DocumentDiscoveryError("pending corpus requires an offline index")
            if hashlib.sha256(raw_manifest).hexdigest() != artifact.manifest.source_manifest_sha256:
                raise DocumentDiscoveryError("document artifact source manifest SHA-256 differs")
            if raw_manifest != pending_corpus_manifest_bytes(corpus_manifest):
                raise DocumentDiscoveryError("document corpus manifest is not canonical")
            binding_sha256 = artifact.manifest.source_manifest_sha256
            cache_top_k = 20
        else:
            if not isinstance(artifact, ApprovedDocumentDenseArtifact) or not isinstance(
                index,
                ApprovedHybridDocumentIndex,
            ):
                raise DocumentDiscoveryError("approved cache requires a production index")
            if raw_manifest != document_cache_activation_receipt_bytes(corpus_manifest):
                raise DocumentDiscoveryError("document activation receipt is not canonical")
            if (
                corpus_manifest.document_dense_artifact_sha256 != artifact.canonical_sha256
                or corpus_manifest.document_dense_vector_payload_sha256
                != artifact.manifest.vector_payload_sha256
                or corpus_manifest.approved_corpus_manifest_sha256
                != artifact.manifest.source_manifest_sha256
                or corpus_manifest.approved_corpus_file_manifest_sha256
                != artifact.manifest.source_text_manifest_sha256
                or corpus_manifest.embedding_snapshot_manifest_sha256
                != artifact.manifest.embedding_snapshot_manifest_sha256
                or corpus_manifest.provider != artifact.manifest.provider
            ):
                raise DocumentDiscoveryError("approved cache artifacts differ from activation")
            if (
                minimum_dense_score != corpus_manifest.minimum_dense_score
                or minimum_dense_margin != corpus_manifest.minimum_dense_margin
            ):
                raise DocumentDiscoveryError("document cache policy differs from activation")
            binding_sha256 = hashlib.sha256(raw_manifest).hexdigest()
            cache_top_k = corpus_manifest.top_k
        if corpus_manifest.source_registry_sha256 != registry.canonical_sha256:
            raise DocumentDiscoveryError("document corpus and Source Registry differ")
        if corpus_manifest.evidence_cutoff != registry.evidence_cutoff:
            raise DocumentDiscoveryError("document corpus evidence cutoff differs")
        artifact_document_ids = {item.chunk.document_id for item in artifact.records}
        manifest_document_ids = {item.document_id for item in corpus_manifest.documents}
        if artifact_document_ids != manifest_document_ids:
            raise DocumentDiscoveryError("document artifact and corpus document sets differ")
        if fallback.registry.canonical_sha256 != registry.canonical_sha256:
            raise DocumentDiscoveryError("document cache and live discovery registries differ")
        self.index = index
        self.artifact = artifact
        self.corpus_manifest = corpus_manifest
        self.fallback = fallback
        self.registry = registry
        self.minimum_dense_score = minimum_dense_score
        self.minimum_dense_margin = minimum_dense_margin
        self.activation_binding_sha256 = binding_sha256
        self.cache_top_k = cache_top_k
        self.production_approved = isinstance(
            corpus_manifest,
            DocumentCacheActivationReceipt,
        )
        self._documents = {item.document_id: item for item in corpus_manifest.documents}
        self._stats_lock = threading.Lock()
        self.cache_queries = 0
        self.cache_hits = 0
        self.live_fallback_queries = 0

    def _cache_candidates(
        self,
        *,
        query: str,
        capability: KnowledgeCapability,
        source_registry_ids: tuple[str, ...],
        evidence_cutoff: date,
        top_k: int,
        time_range: KnowledgeTimeRange | None = None,
    ) -> tuple[OfficialDiscoveryCandidate, ...]:
        top_k = min(top_k, self.cache_top_k)
        candidate_limit = min(50, max(20, top_k * 8))
        # Keep live search narrow, but give the offline cache a typed relation
        # context.  This bridges unseen user paraphrases such as "무엇을 담고
        # 있나" without adding entity-specific answer rules. Capability and
        # eligible official sources are already server-authorized before this
        # candidate-only retrieval step.
        cache_query = " ".join((query.strip(), _CAPABILITY_CACHE_QUERY_CONTEXT[capability]))
        dense_chunks = self.index.dense_search(cache_query, top_k=candidate_limit)
        # BM25 must retain entity precision.  Adding generic capability words
        # here gives every holdings/risk document a positive lexical score and
        # spends the bounded fetch budget on unrelated issuers.  Dense alone
        # receives the paraphrase bridge; lower-ranked candidates still need a
        # literal signal from the original entity-bearing query.
        lexical_chunks = self.index.bm25_search(query.strip(), top_k=candidate_limit)
        dense = []
        seen_dense_documents: set[str] = set()
        for hit in dense_chunks:
            if hit.document_id in seen_dense_documents:
                continue
            dense.append(hit)
            seen_dense_documents.add(hit.document_id)
        dense_by_document = {item.document_id: item for item in dense}
        positive_lexical_documents = {item.document_id for item in lexical_chunks if item.score > 0}
        hybrid = fuse_document_hits_rrf(
            lexical_chunks,
            dense_chunks,
            top_k=min(50, len({item.document_id for item in (*lexical_chunks, *dense_chunks)})),
        )
        allowed_sources = set(source_registry_ids)
        eligible = []
        for hit in hybrid:
            document = self._documents.get(hit.document_id)
            if document is None or (
                capability not in document.capabilities
                or document.source_registry_id not in allowed_sources
                or document.as_of > evidence_cutoff
            ):
                continue
            relation_date = document.published_at or document.as_of
            if time_range is not None and (
                relation_date > time_range.end
                or (time_range.start is not None and relation_date < time_range.start)
            ):
                # Runtime relations use this same publication/as-of date as
                # their valid interval.  Treat an out-of-window cached URL as
                # a cache miss so bounded live discovery can find a dated
                # document instead of failing after an avoidable refetch.
                continue
            try:
                source = self.registry.authorize_url(document.source_uri, capability)
            except OfficialSourceRegistryError:
                continue
            if source.source_id == document.source_registry_id:
                eligible.append(hit)
        if not eligible:
            return ()
        top_document_id = eligible[0].document_id
        top_dense_hit = dense_by_document.get(top_document_id)
        top_has_lexical_support = top_document_id in positive_lexical_documents
        if not top_has_lexical_support and (
            top_dense_hit is None or top_dense_hit.score < self.minimum_dense_score
        ):
            return ()
        # Confidence is a document-level decision. Comparing adjacent chunks
        # from the same document made a clearly correct document look
        # ambiguous and incorrectly forced a live-search fallback.
        # Documents from the same official publisher are one authority group:
        # structure and trend pages for the same subject are complementary,
        # not competing interpretations. Registry entries can split one
        # publisher by path or hostname, so source_id alone is too narrow.
        # Compare against the next independent publisher/authority instead.
        top_source = self.registry.require_source_id(
            self._documents[top_document_id].source_registry_id
        )
        top_authority_group = (top_source.publisher.casefold(), top_source.authority.value)
        next_score = next(
            (
                dense_by_document[hit.document_id].score
                for hit in eligible[1:]
                if hit.document_id in dense_by_document
                if (
                    (
                        source := self.registry.require_source_id(
                            self._documents[hit.document_id].source_registry_id
                        )
                    ).publisher.casefold(),
                    source.authority.value,
                )
                != top_authority_group
            ),
            -1.0,
        )
        if (
            not top_has_lexical_support
            and top_dense_hit is not None
            and top_dense_hit.score - next_score < self.minimum_dense_margin
        ):
            return ()
        ordered_ids: list[str] = []
        for hit in eligible:
            if hit.document_id in ordered_ids:
                continue
            # Dense is allowed to bridge paraphrases only for the strongest
            # result.  Lower-ranked documents require a lexical BM25 signal.
            if ordered_ids and hit.document_id not in positive_lexical_documents:
                continue
            ordered_ids.append(hit.document_id)
        candidates: list[OfficialDiscoveryCandidate] = []
        seen_urls: set[str] = set()
        for document_id in ordered_ids:
            document = self._documents[document_id]
            if (
                capability not in document.capabilities
                or document.source_registry_id not in allowed_sources
                or document.as_of > evidence_cutoff
            ):
                continue
            try:
                source = self.registry.authorize_url(document.source_uri, capability)
            except OfficialSourceRegistryError:
                continue
            if source.source_id != document.source_registry_id or document.source_uri in seen_urls:
                continue
            candidates.append(
                OfficialDiscoveryCandidate(
                    url=document.source_uri,
                    title=document.title,
                    published_at=document.published_at,
                    as_of=document.as_of,
                )
            )
            seen_urls.add(document.source_uri)
            if len(candidates) >= top_k:
                break
        return tuple(candidates)

    def discover_report(
        self,
        *,
        query: str,
        capability: KnowledgeCapability,
        source_registry_ids: tuple[str, ...],
        evidence_cutoff: date,
        top_k: int,
        max_queries: int | None = None,
        entity_terms: tuple[str, ...] = (),
        product_terms: tuple[str, ...] = (),
        relation_terms: tuple[str, ...] = (),
        time_range: KnowledgeTimeRange | None = None,
    ) -> OfficialDiscoveryReport:
        if evidence_cutoff != self.registry.evidence_cutoff:
            raise OfficialDiscoveryError("document discovery evidence cutoff differs")
        if not 1 <= top_k <= 20:
            raise ValueError("top_k must be between 1 and 20")
        with self._stats_lock:
            self.cache_queries += 1
        cached = self._cache_candidates(
            query=query,
            capability=capability,
            source_registry_ids=source_registry_ids,
            evidence_cutoff=evidence_cutoff,
            top_k=top_k,
            time_range=time_range,
        )
        if cached:
            with self._stats_lock:
                self.cache_hits += 1
            return OfficialDiscoveryReport(
                candidates=cached,
                queried_source_registry_ids=(),
                exhaustive=False,
                failure_codes=(),
            )
        return self.discover_live_report(
            query=query,
            capability=capability,
            source_registry_ids=source_registry_ids,
            evidence_cutoff=evidence_cutoff,
            top_k=top_k,
            max_queries=max_queries,
            entity_terms=entity_terms,
            product_terms=product_terms,
            relation_terms=relation_terms,
            time_range=time_range,
        )

    def discover_live_report(
        self,
        *,
        query: str,
        capability: KnowledgeCapability,
        source_registry_ids: tuple[str, ...],
        evidence_cutoff: date,
        top_k: int,
        max_queries: int | None = None,
        entity_terms: tuple[str, ...] = (),
        product_terms: tuple[str, ...] = (),
        relation_terms: tuple[str, ...] = (),
        time_range: KnowledgeTimeRange | None = None,
    ) -> OfficialDiscoveryReport:
        """Bypass a stale/false-positive cache candidate exactly once.

        The acquisition layer may call this only after fetched cached bytes
        fail exact relation extraction.  Search snippets still remain
        untrusted and every returned URL is constrained by the same Registry.
        """

        with self._stats_lock:
            self.live_fallback_queries += 1
        report = self.fallback.discover_report(
            query=query,
            capability=capability,
            source_registry_ids=source_registry_ids,
            evidence_cutoff=evidence_cutoff,
            top_k=top_k,
            max_queries=max_queries,
            entity_terms=entity_terms,
            product_terms=product_terms,
            relation_terms=relation_terms,
            time_range=time_range,
        )
        return report.model_copy(
            update={
                "failure_codes": tuple(
                    sorted(
                        set(report.failure_codes),
                        key=lambda item: item.value,
                    )
                )
            }
        )

    def discover(
        self,
        *,
        query: str,
        capability: KnowledgeCapability,
        source_registry_ids: tuple[str, ...],
        evidence_cutoff: date,
        top_k: int,
    ) -> tuple[OfficialDiscoveryCandidate, ...]:
        return self.discover_report(
            query=query,
            capability=capability,
            source_registry_ids=source_registry_ids,
            evidence_cutoff=evidence_cutoff,
            top_k=top_k,
        ).candidates


def build_cache_first_discovery_provider(
    *,
    index: HybridDocumentIndex,
    artifact: DocumentDenseArtifact | ApprovedDocumentDenseArtifact,
    corpus_manifest: PendingCorpusManifest | DocumentCacheActivationReceipt,
    corpus_manifest_path: str | Path,
    fallback: BoundedOfficialDiscoveryProvider,
    registry: OfficialSourceRegistry,
    minimum_dense_score: float,
    minimum_dense_margin: float,
) -> CacheFirstOfficialDiscoveryProvider:
    """Single constructor used by both the offline runner and release Runtime."""

    return CacheFirstOfficialDiscoveryProvider(
        index=index,
        artifact=artifact,
        corpus_manifest=corpus_manifest,
        corpus_manifest_path=corpus_manifest_path,
        fallback=fallback,
        registry=registry,
        minimum_dense_score=minimum_dense_score,
        minimum_dense_margin=minimum_dense_margin,
    )


def build_release_pinned_cache_first_discovery(
    *,
    activation_receipt_path: str | Path,
    expected_activation_receipt_sha256: str,
    document_dense_artifact_path: str | Path,
    expected_document_dense_sha256: str,
    expected_corpus_manifest_sha256: str,
    expected_document_bm25_sha256: str,
    expected_embedding_model_revision: str,
    embedding: EmbeddingProvider,
    fallback: BoundedOfficialDiscoveryProvider,
    registry: OfficialSourceRegistry,
) -> CacheFirstOfficialDiscoveryProvider:
    """Load a cache only when every signed release binding agrees exactly."""

    receipt = load_document_cache_activation_receipt(
        activation_receipt_path,
        expected_file_sha256=expected_activation_receipt_sha256,
    )
    artifact = load_approved_document_dense_artifact(
        document_dense_artifact_path,
        expected_file_sha256=expected_document_dense_sha256,
    )
    evidence = getattr(embedding, "artifact_gate_evidence", None)
    if (
        receipt.source_registry_sha256 != registry.canonical_sha256
        or receipt.evidence_cutoff != registry.evidence_cutoff
        or receipt.approved_corpus_manifest_sha256 != expected_corpus_manifest_sha256
        or receipt.document_dense_artifact_sha256 != expected_document_dense_sha256
        or receipt.document_bm25_sha256 != expected_document_bm25_sha256
        or receipt.provider != embedding.metadata
        or receipt.provider.model_revision != expected_embedding_model_revision
        or getattr(evidence, "snapshot_file_manifest_sha256", None)
        != receipt.embedding_snapshot_manifest_sha256
    ):
        raise DocumentDiscoveryError("Document Dense cache differs from the signed release")
    return build_cache_first_discovery_provider(
        index=ApprovedHybridDocumentIndex(artifact, embedding),
        artifact=artifact,
        corpus_manifest=receipt,
        corpus_manifest_path=activation_receipt_path,
        fallback=fallback,
        registry=registry,
        minimum_dense_score=receipt.minimum_dense_score,
        minimum_dense_margin=receipt.minimum_dense_margin,
    )


__all__ = [
    "CacheFirstOfficialDiscoveryProvider",
    "DocumentDiscoveryError",
    "build_cache_first_discovery_provider",
    "build_release_pinned_cache_first_discovery",
]
