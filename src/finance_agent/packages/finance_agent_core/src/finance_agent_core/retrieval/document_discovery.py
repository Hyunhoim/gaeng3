from __future__ import annotations

import hashlib
import os
import re
import stat
import threading
import unicodedata
from datetime import date
from pathlib import Path

from finance_agent_core.config.knowledge_coverage import (
    ApprovedKnowledgeCoverageManifest,
    KnowledgeCapability,
)
from finance_agent_core.config.source_registry import (
    OfficialSourceRegistry,
    OfficialSourceRegistryError,
)
from finance_agent_core.contracts.external_knowledge import KnowledgeTimeRange
from finance_agent_core.retrieval.corpus import (
    VerifiedApprovedCorpus,
    verify_approved_corpus,
)
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
from finance_agent_core.retrieval.official_connector import (
    OfficialDiscoveryCandidate,
    OfficialSourceSnapshot,
)
from finance_agent_core.retrieval.official_discovery import (
    BoundedOfficialDiscoveryProvider,
    DiscoveryFailureCode,
    OfficialDiscoveryError,
    OfficialDiscoveryReport,
)
from finance_agent_core.retrieval.pending_corpus import (
    PendingCorpusManifest,
    pending_corpus_manifest_bytes,
)
from finance_agent_core.retrieval.schema_dense import EmbeddingProvider
from finance_agent_core.retrieval.sqlite_fts import normalize_document_text


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

_GENERIC_DISCOVERY_TARGET_KEYS = frozenset(
    {
        "상품",
        "종목",
        "기업",
        "회사",
        "펀드",
        "채권",
        "테마",
        "섹터",
        "산업",
        "etf",
        "etn",
        "etp",
        "fund",
        "bond",
    }
)
_TARGET_RIGHT_BOUNDARY = (
    r"(?=$|[^0-9a-z가-힣]|(?:에게서|에서는|으로는|에게|에서|부터|까지|"
    r"으로|이나|과의|와의|은|는|이|가|을|를|의|와|과|도|에|로)"
    r"(?=$|[^0-9a-z가-힣]))"
)


def _target_key(value: str) -> str:
    return "".join(
        character
        for character in unicodedata.normalize("NFKC", value).casefold()
        if character.isalnum()
    )


def _bounded_target_groups(
    groups: tuple[tuple[str, ...], ...],
) -> tuple[tuple[str, ...], ...]:
    """Validate typed aliases without turning them into retrieval authority."""

    if len(groups) > 20:
        raise ValueError("document discovery target groups exceed the bounded limit")
    normalized_groups: list[tuple[str, ...]] = []
    seen_groups: set[tuple[str, ...]] = set()
    for group in groups:
        values = tuple(dict.fromkeys(value.strip() for value in group if value.strip()))
        if not values or len(values) > 21 or any(len(value) > 500 for value in values):
            raise ValueError("document discovery target aliases are invalid or unbounded")
        key = tuple(
            sorted(
                {
                    target
                    for value in values
                    if len(target := _target_key(value)) >= 2
                    and target not in _GENERIC_DISCOVERY_TARGET_KEYS
                }
            )
        )
        if not key:
            raise ValueError("document discovery target lacks a distinctive identity")
        if key in seen_groups:
            continue
        seen_groups.add(key)
        normalized_groups.append(values)
    return tuple(normalized_groups)


def _merge_candidate_groups(
    groups: tuple[tuple[OfficialDiscoveryCandidate, ...], ...],
    *,
    limit: int,
) -> tuple[OfficialDiscoveryCandidate, ...]:
    """Interleave targets so one verbose document cannot starve another."""

    merged: list[OfficialDiscoveryCandidate] = []
    seen_urls: set[str] = set()
    depth = 0
    while len(merged) < limit and any(depth < len(group) for group in groups):
        for group in groups:
            if depth >= len(group):
                continue
            candidate = group[depth]
            if candidate.url in seen_urls:
                continue
            seen_urls.add(candidate.url)
            merged.append(candidate)
            if len(merged) == limit:
                break
        depth += 1
    return tuple(merged)


def _pending_snapshot_texts(
    manifest: PendingCorpusManifest,
    manifest_path: Path,
) -> dict[str, str]:
    """Load exact pending snapshots for an explicitly labelled lab run.

    Chunk text is never inverted into source evidence.  The pending manifest
    already seals the extracted UTF-8 files, so even laboratory evidence must
    be read from those files and checked for path escape, type, size and hash.
    """

    try:
        root = manifest_path.parent.resolve(strict=True)
    except OSError as error:
        raise DocumentDiscoveryError("pending document root is unavailable") from error
    loaded: dict[str, str] = {}
    for document in manifest.documents:
        candidate = manifest_path.parent / document.text_relative_path
        try:
            resolved = candidate.resolve(strict=True)
            resolved.relative_to(root)
        except (OSError, ValueError) as error:
            raise DocumentDiscoveryError("pending snapshot path escaped its root") from error
        if resolved.is_symlink():
            raise DocumentDiscoveryError("pending snapshot must not be a symbolic link")
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        descriptor: int | None = None
        try:
            descriptor = os.open(resolved, flags)
            before = os.fstat(descriptor)
            if not stat.S_ISREG(before.st_mode) or before.st_size != document.text_size_bytes:
                raise DocumentDiscoveryError("pending snapshot size or type differs")
            chunks: list[bytes] = []
            remaining = document.text_size_bytes
            while remaining:
                value = os.read(descriptor, min(1024 * 1024, remaining))
                if not value:
                    break
                chunks.append(value)
                remaining -= len(value)
            after = os.fstat(descriptor)
            if remaining or (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != (
                after.st_dev,
                after.st_ino,
                after.st_size,
                after.st_mtime_ns,
            ):
                raise DocumentDiscoveryError("pending snapshot changed while loading")
            content = b"".join(chunks)
        except DocumentDiscoveryError:
            raise
        except OSError as error:
            raise DocumentDiscoveryError("pending snapshot is unavailable") from error
        finally:
            if descriptor is not None:
                os.close(descriptor)
        if hashlib.sha256(content).hexdigest() != document.text_sha256:
            raise DocumentDiscoveryError("pending snapshot SHA-256 differs")
        try:
            loaded[document.document_id] = normalize_document_text(content.decode("utf-8"))
        except UnicodeDecodeError as error:
            raise DocumentDiscoveryError("pending snapshot is not UTF-8") from error
    return loaded


class CacheFirstOfficialDiscoveryProvider:
    """Find official documents in a release-bound cache before live search.

    A production-approved cache may supply its exact frozen text as evidence;
    an unapproved pending corpus remains URL-candidate-only unless an explicit
    laboratory switch is set.  Every document is still constrained by the
    Source Registry, capability, cutoff, and artifact hashes.  Low-confidence
    retrieval delegates to the bounded NAVER API HUB provider.
    """

    def __init__(
        self,
        *,
        index: HybridDocumentIndex,
        artifact: DocumentDenseArtifact | ApprovedDocumentDenseArtifact,
        corpus_manifest: PendingCorpusManifest | DocumentCacheActivationReceipt,
        corpus_manifest_path: str | Path,
        fallback: BoundedOfficialDiscoveryProvider | None,
        registry: OfficialSourceRegistry,
        approved_corpus: VerifiedApprovedCorpus | None = None,
        minimum_dense_score: float = 0.55,
        minimum_dense_margin: float = 0.03,
        allow_pending_snapshot_evidence: bool = False,
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
            if approved_corpus is not None:
                raise DocumentDiscoveryError("pending cache cannot bind an approved corpus")
            if not isinstance(artifact, DocumentDenseArtifact):
                raise DocumentDiscoveryError("pending corpus requires an offline index")
            if hashlib.sha256(raw_manifest).hexdigest() != artifact.manifest.source_manifest_sha256:
                raise DocumentDiscoveryError("document artifact source manifest SHA-256 differs")
            if raw_manifest != pending_corpus_manifest_bytes(corpus_manifest):
                raise DocumentDiscoveryError("document corpus manifest is not canonical")
            binding_sha256 = artifact.manifest.source_manifest_sha256
            cache_top_k = 20
        else:
            if approved_corpus is None:
                raise DocumentDiscoveryError(
                    "approved cache requires the exact verified corpus snapshots"
                )
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
                or approved_corpus.manifest_sha256
                != corpus_manifest.approved_corpus_manifest_sha256
                or approved_corpus.manifest.file_manifest_sha256
                != corpus_manifest.approved_corpus_file_manifest_sha256
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
        if fallback is not None and fallback.registry.canonical_sha256 != registry.canonical_sha256:
            raise DocumentDiscoveryError("document cache and live discovery registries differ")
        self.index = index
        self.artifact = artifact
        self.corpus_manifest = corpus_manifest
        self.fallback = fallback
        self.live_fallback_enabled = fallback is not None
        self.registry = registry
        self.minimum_dense_score = minimum_dense_score
        self.minimum_dense_margin = minimum_dense_margin
        self.activation_binding_sha256 = binding_sha256
        self.cache_top_k = cache_top_k
        self.production_approved = isinstance(
            corpus_manifest,
            DocumentCacheActivationReceipt,
        )
        if type(allow_pending_snapshot_evidence) is not bool:
            raise TypeError("pending snapshot evidence flag must be a boolean")
        # A release-approved cache is already bound to two independent corpus
        # reviews and the exact frozen chunks. Pending chunks may be used only
        # by an explicitly labelled lab regression; they can never silently
        # acquire production evidence authority.
        self._snapshot_evidence_enabled = (
            self.production_approved or allow_pending_snapshot_evidence
        )
        self._documents = {item.document_id: item for item in corpus_manifest.documents}
        if approved_corpus is not None:
            approved_by_id = {item.approval.document_id: item for item in approved_corpus.documents}
            if set(approved_by_id) != manifest_document_ids:
                raise DocumentDiscoveryError("approved cache and corpus document sets differ")
            for document_id, activated in self._documents.items():
                verified = approved_by_id[document_id]
                source = verified.approval
                if (
                    activated.title != source.title
                    or activated.publisher != source.publisher
                    or activated.source_registry_id != source.source_registry_id
                    or activated.source_uri != source.source_uri
                    or activated.capabilities != source.capabilities
                    or activated.published_at != source.published_at
                    or activated.as_of != source.as_of
                    or activated.content_sha256 != source.content_sha256
                    or activated.normalized_text_sha256 != source.normalized_text_sha256
                ):
                    raise DocumentDiscoveryError(
                        "approved cache source metadata differs from verified corpus"
                    )
            self._document_text = {
                document_id: normalize_document_text(verified.text)
                for document_id, verified in approved_by_id.items()
            }
        else:
            self._document_text = _pending_snapshot_texts(corpus_manifest, manifest_path)
        self._documents_by_url: dict[str, list[str]] = {}
        for document in corpus_manifest.documents:
            self._documents_by_url.setdefault(document.source_uri, []).append(document.document_id)
        self._stats_lock = threading.Lock()
        self.cache_queries = 0
        self.cache_hits = 0
        self.cache_evidence_hits = 0
        self.live_fallback_queries = 0

    def materialize_cached_candidate(
        self,
        candidate: OfficialDiscoveryCandidate,
        capability: KnowledgeCapability,
    ) -> OfficialSourceSnapshot | None:
        """Return SHA-bound cached official text without a fragile network refetch.

        Candidate selection still uses BM25+Dense and Source Registry scope.
        This method only removes a second download of the *same* already frozen
        official document. It does not turn search snippets into evidence.
        """

        if not self._snapshot_evidence_enabled:
            return None
        for document_id in self._documents_by_url.get(candidate.url, ()):
            document = self._documents[document_id]
            if capability not in document.capabilities:
                continue
            try:
                source = self.registry.authorize_url(candidate.url, capability)
            except OfficialSourceRegistryError:
                continue
            if source.source_id != document.source_registry_id:
                continue
            text = self._document_text.get(document_id)
            if not text:
                continue
            content = text.encode("utf-8")
            expected_normalized_sha256 = getattr(
                document,
                "normalized_text_sha256",
                hashlib.sha256(content).hexdigest(),
            )
            if hashlib.sha256(content).hexdigest() != expected_normalized_sha256:
                raise DocumentDiscoveryError("cached evidence normalized SHA-256 differs")
            with self._stats_lock:
                self.cache_evidence_hits += 1
            return OfficialSourceSnapshot(
                source=source,
                capability=capability,
                source_uri=document.source_uri,
                title=document.title,
                content_type="text/plain",
                content=content,
                content_sha256=expected_normalized_sha256,
                published_at=document.published_at,
                as_of=document.as_of,
                db_ticker=candidate.db_ticker,
                approved_document_id=document.document_id,
            )
        return None

    def _document_matches_target(
        self,
        document_id: str,
        target_terms: tuple[str, ...],
    ) -> bool:
        """Require literal DB/entity identity before a typed Dense hit is used.

        Dense remains useful for ranking official documents, but it cannot by
        itself claim that a document covers a named product or entity.  At
        least one distinctive alias must occur in the frozen title, URL, or
        text.  This is deliberately stricter than the general semantic path.
        """

        document = self._documents[document_id]
        surface = unicodedata.normalize(
            "NFKC",
            "\n".join(
                (document.title, document.source_uri, self._document_text.get(document_id, ""))
            ),
        ).casefold()
        terms = tuple(
            value.strip()
            for value in target_terms
            if len(key := _target_key(value)) >= 2 and key not in _GENERIC_DISCOVERY_TARGET_KEYS
        )
        for term in terms:
            escaped = re.escape(unicodedata.normalize("NFKC", term).casefold())
            escaped = escaped.replace(r"\ ", r"\s*")
            if (
                re.search(
                    rf"(?<![0-9a-z가-힣]){escaped}{_TARGET_RIGHT_BOUNDARY}",
                    surface,
                    re.IGNORECASE,
                )
                is not None
            ):
                return True
        return False

    def _cache_candidates(
        self,
        *,
        query: str,
        capability: KnowledgeCapability,
        source_registry_ids: tuple[str, ...],
        evidence_cutoff: date,
        top_k: int,
        time_range: KnowledgeTimeRange | None = None,
        required_target_terms: tuple[str, ...] = (),
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
        target_supported_documents = (
            {
                hit.document_id
                for hit in eligible
                if self._document_matches_target(hit.document_id, required_target_terms)
            }
            if required_target_terms
            else set()
        )
        if required_target_terms and not target_supported_documents:
            # A semantically close official document is not a cache hit for an
            # explicitly named product/entity.  Delegate that target to the
            # official live provider instead of widening the match.
            return ()
        if target_supported_documents:
            eligible.sort(key=lambda hit: hit.document_id not in target_supported_documents)
        top_document_id = eligible[0].document_id
        top_dense_hit = dense_by_document.get(top_document_id)
        top_has_lexical_support = (
            top_document_id in positive_lexical_documents
            or top_document_id in target_supported_documents
        )
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
            if required_target_terms and hit.document_id not in target_supported_documents:
                continue
            # Dense is allowed to bridge paraphrases only for the strongest
            # result. Lower-ranked documents require a lexical BM25 signal,
            # except when an explicit DB/entity target occurs literally in the
            # frozen document. That exception cannot admit an unrelated Dense
            # neighbour because it is bound to the caller's typed target group.
            if (
                ordered_ids
                and hit.document_id not in positive_lexical_documents
                and hit.document_id not in target_supported_documents
            ):
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
        entity_term_groups: tuple[tuple[str, ...], ...] = (),
        product_term_groups: tuple[tuple[str, ...], ...] = (),
        relation_terms: tuple[str, ...] = (),
        time_range: KnowledgeTimeRange | None = None,
        product_surfaces: tuple[str, ...] = (),
    ) -> OfficialDiscoveryReport:
        if evidence_cutoff != self.registry.evidence_cutoff:
            raise OfficialDiscoveryError("document discovery evidence cutoff differs")
        if not 1 <= top_k <= 20:
            raise ValueError("top_k must be between 1 and 20")
        entity_term_groups = _bounded_target_groups(entity_term_groups)
        product_term_groups = _bounded_target_groups(product_term_groups)
        typed_targets = tuple(("entity", group) for group in entity_term_groups) + tuple(
            ("product", group) for group in product_term_groups
        )
        with self._stats_lock:
            self.cache_queries += 1
        if not typed_targets:
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
                product_surfaces=product_surfaces,
            )

        cached_groups: list[tuple[OfficialDiscoveryCandidate, ...]] = []
        uncovered_targets: list[tuple[str, tuple[str, ...]]] = []
        for target_kind, target_group in typed_targets:
            # A product-document target remains paired with the relation entity;
            # an entity target stands alone so unrelated product aliases cannot
            # make it appear covered. Limit the query surface while retaining
            # every alias for the literal coverage check below.
            context_terms = (
                (*entity_terms[:4], *target_group[:4], *relation_terms[:2])
                if target_kind == "product"
                else (*target_group[:4], *relation_terms[:2])
            )
            target_query = " ".join(dict.fromkeys(value for value in context_terms if value))
            target_candidates = self._cache_candidates(
                query=target_query or query,
                capability=capability,
                source_registry_ids=source_registry_ids,
                evidence_cutoff=evidence_cutoff,
                top_k=top_k,
                time_range=time_range,
                required_target_terms=target_group,
            )
            if target_candidates:
                cached_groups.append(target_candidates)
            else:
                uncovered_targets.append((target_kind, target_group))

        cached = _merge_candidate_groups(tuple(cached_groups), limit=top_k)
        if cached:
            with self._stats_lock:
                self.cache_hits += 1
        if not uncovered_targets:
            return OfficialDiscoveryReport(
                candidates=cached,
                queried_source_registry_ids=(),
                exhaustive=False,
                failure_codes=(),
            )

        # Spend the bounded live-search budget only on targets not literally
        # covered by the cache. One partial cache hit can no longer suppress
        # discovery for the remaining named products/entities.
        total_query_budget = (
            max_queries
            if max_queries is not None
            else min(40, max(len(uncovered_targets), len(set(source_registry_ids))))
        )
        remaining_query_budget = total_query_budget
        live_groups: list[tuple[OfficialDiscoveryCandidate, ...]] = []
        queried_source_ids: set[str] = set()
        failure_codes: set[DiscoveryFailureCode] = set()
        search_query_count = 0
        for target_index, (target_kind, target_group) in enumerate(uncovered_targets):
            targets_left = len(uncovered_targets) - target_index
            if remaining_query_budget <= 0:
                failure_codes.add(DiscoveryFailureCode.SOURCE_SCOPE_INCOMPLETE)
                break
            target_budget = max(1, remaining_query_budget // targets_left)
            target_entity_terms = target_group if target_kind == "entity" else entity_terms
            target_product_terms = target_group if target_kind == "product" else ()
            target_query_terms = tuple(
                dict.fromkeys(
                    (
                        *target_entity_terms[:4],
                        *target_product_terms[:4],
                        *relation_terms[:2],
                    )
                )
            )
            live_report = self.discover_live_report(
                query=" ".join(target_query_terms) or query,
                capability=capability,
                source_registry_ids=source_registry_ids,
                evidence_cutoff=evidence_cutoff,
                top_k=max(1, min(top_k, 3)),
                max_queries=target_budget,
                entity_terms=target_entity_terms,
                product_terms=target_product_terms,
                relation_terms=relation_terms,
                time_range=time_range,
                product_surfaces=product_surfaces,
            )
            live_groups.append(live_report.candidates)
            queried_source_ids.update(live_report.queried_source_registry_ids)
            failure_codes.update(live_report.failure_codes)
            search_query_count += live_report.search_query_count
            remaining_query_budget -= live_report.search_query_count

        combined = _merge_candidate_groups(
            tuple((*cached_groups, *live_groups)),
            limit=top_k,
        )
        return OfficialDiscoveryReport(
            candidates=combined,
            queried_source_registry_ids=tuple(sorted(queried_source_ids)),
            search_query_count=search_query_count,
            exhaustive=False,
            failure_codes=tuple(sorted(failure_codes, key=lambda item: item.value)),
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
        entity_term_groups: tuple[tuple[str, ...], ...] = (),
        product_term_groups: tuple[tuple[str, ...], ...] = (),
        relation_terms: tuple[str, ...] = (),
        time_range: KnowledgeTimeRange | None = None,
        product_surfaces: tuple[str, ...] = (),
    ) -> OfficialDiscoveryReport:
        """Bypass a stale/false-positive cache candidate exactly once.

        The acquisition layer may call this only after fetched cached bytes
        fail exact relation extraction.  Search snippets still remain
        untrusted and every returned URL is constrained by the same Registry.
        """

        if self.fallback is None:
            return OfficialDiscoveryReport(
                candidates=(),
                queried_source_registry_ids=(),
                search_query_count=0,
                exhaustive=False,
                failure_codes=(DiscoveryFailureCode.SOURCE_SCOPE_INCOMPLETE,),
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
            entity_terms=entity_terms,
            product_terms=product_terms,
            entity_term_groups=entity_term_groups,
            product_term_groups=product_term_groups,
            relation_terms=relation_terms,
            time_range=time_range,
            product_surfaces=product_surfaces,
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
    fallback: BoundedOfficialDiscoveryProvider | None,
    registry: OfficialSourceRegistry,
    approved_corpus: VerifiedApprovedCorpus | None = None,
    minimum_dense_score: float,
    minimum_dense_margin: float,
    allow_pending_snapshot_evidence: bool = False,
) -> CacheFirstOfficialDiscoveryProvider:
    """Single constructor used by both the offline runner and release Runtime."""

    return CacheFirstOfficialDiscoveryProvider(
        index=index,
        artifact=artifact,
        corpus_manifest=corpus_manifest,
        corpus_manifest_path=corpus_manifest_path,
        fallback=fallback,
        registry=registry,
        approved_corpus=approved_corpus,
        minimum_dense_score=minimum_dense_score,
        minimum_dense_margin=minimum_dense_margin,
        allow_pending_snapshot_evidence=allow_pending_snapshot_evidence,
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
    fallback: BoundedOfficialDiscoveryProvider | None,
    registry: OfficialSourceRegistry,
    approved_corpus_manifest_path: str | Path,
    approved_corpus_root: str | Path,
    cache_only_coverage_manifest: ApprovedKnowledgeCoverageManifest | None = None,
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
    approved_corpus = verify_approved_corpus(
        approved_corpus_manifest_path,
        approved_corpus_root,
    )
    evidence = getattr(embedding, "artifact_gate_evidence", None)
    if (
        receipt.source_registry_sha256 != registry.canonical_sha256
        or receipt.evidence_cutoff != registry.evidence_cutoff
        or receipt.approved_corpus_manifest_sha256 != expected_corpus_manifest_sha256
        or approved_corpus.manifest_sha256 != expected_corpus_manifest_sha256
        or receipt.approved_corpus_file_manifest_sha256
        != approved_corpus.manifest.file_manifest_sha256
        or receipt.document_dense_artifact_sha256 != expected_document_dense_sha256
        or receipt.document_bm25_sha256 != expected_document_bm25_sha256
        or receipt.provider != embedding.metadata
        or receipt.provider.model_revision != expected_embedding_model_revision
        or getattr(evidence, "manifest_file_sha256", None)
        != receipt.embedding_snapshot_manifest_sha256
    ):
        raise DocumentDiscoveryError("Document Dense cache differs from the signed release")
    if fallback is None:
        if cache_only_coverage_manifest is None:
            raise DocumentDiscoveryError(
                "cache-only Document Dense requires an approved coverage binding"
            )
        try:
            cache_only_coverage_manifest.require_cache_only_binding(
                capabilities=(
                    capability
                    for document in receipt.documents
                    for capability in document.capabilities
                ),
                corpus_manifest_sha256=receipt.approved_corpus_manifest_sha256,
                document_dense_artifact_sha256=receipt.document_dense_artifact_sha256,
            )
        except ValueError as error:
            raise DocumentDiscoveryError(
                "cache-only Document Dense coverage differs from its activation"
            ) from error
    elif cache_only_coverage_manifest is not None:
        raise DocumentDiscoveryError(
            "live-fallback Document Dense cannot borrow cache-only coverage authority"
        )
    return build_cache_first_discovery_provider(
        index=ApprovedHybridDocumentIndex(artifact, embedding),
        artifact=artifact,
        corpus_manifest=receipt,
        corpus_manifest_path=activation_receipt_path,
        fallback=fallback,
        registry=registry,
        approved_corpus=approved_corpus,
        minimum_dense_score=receipt.minimum_dense_score,
        minimum_dense_margin=receipt.minimum_dense_margin,
    )


__all__ = [
    "CacheFirstOfficialDiscoveryProvider",
    "DocumentDiscoveryError",
    "build_cache_first_discovery_provider",
    "build_release_pinned_cache_first_discovery",
]
