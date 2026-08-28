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
from finance_agent_core.retrieval.document_dense import (
    DocumentDenseArtifact,
    OfflineHybridDocumentIndex,
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


class DocumentDiscoveryError(RuntimeError):
    """Raised when a candidate-only document cache crosses its trust boundary."""


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
        index: OfflineHybridDocumentIndex,
        artifact: DocumentDenseArtifact,
        corpus_manifest: PendingCorpusManifest,
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
        if hashlib.sha256(raw_manifest).hexdigest() != artifact.manifest.source_manifest_sha256:
            raise DocumentDiscoveryError("document artifact source manifest SHA-256 differs")
        if raw_manifest != pending_corpus_manifest_bytes(corpus_manifest):
            raise DocumentDiscoveryError("document corpus manifest is not canonical")
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
    ) -> tuple[OfficialDiscoveryCandidate, ...]:
        dense_chunks = self.index.dense_search(query, top_k=min(50, max(10, top_k * 4)))
        dense = []
        seen_dense_documents: set[str] = set()
        for hit in dense_chunks:
            if hit.document_id in seen_dense_documents:
                continue
            dense.append(hit)
            seen_dense_documents.add(hit.document_id)
        allowed_sources = set(source_registry_ids)
        eligible_dense = []
        for hit in dense:
            document = self._documents.get(hit.document_id)
            if document is None or (
                capability not in document.capabilities
                or document.source_registry_id not in allowed_sources
                or document.as_of > evidence_cutoff
            ):
                continue
            try:
                source = self.registry.authorize_url(document.source_uri, capability)
            except OfficialSourceRegistryError:
                continue
            if source.source_id == document.source_registry_id:
                eligible_dense.append(hit)
        if not eligible_dense or eligible_dense[0].score < self.minimum_dense_score:
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
            self._documents[eligible_dense[0].document_id].source_registry_id
        )
        top_authority_group = (top_source.publisher.casefold(), top_source.authority.value)
        next_score = next(
            (
                hit.score
                for hit in eligible_dense[1:]
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
        if eligible_dense[0].score - next_score < self.minimum_dense_margin:
            return ()
        bm25_ids = {
            item.document_id
            for item in self.index.bm25_search(query, top_k=min(10, max(2, top_k)))
            if item.score > 0
        }
        ordered_ids: list[str] = []
        for hit in eligible_dense:
            if hit.document_id in ordered_ids:
                continue
            # Dense is allowed to bridge paraphrases only for the strongest
            # result.  Lower-ranked documents require a lexical BM25 signal.
            if ordered_ids and hit.document_id not in bm25_ids:
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
        with self._stats_lock:
            self.live_fallback_queries += 1
        report = self.fallback.discover_report(
            query=query,
            capability=capability,
            source_registry_ids=source_registry_ids,
            evidence_cutoff=evidence_cutoff,
            top_k=top_k,
            max_queries=max_queries,
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


__all__ = [
    "CacheFirstOfficialDiscoveryProvider",
    "DocumentDiscoveryError",
]
