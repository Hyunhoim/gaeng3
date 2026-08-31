from __future__ import annotations

import hashlib
import re
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date

from finance_agent_core.agent.source_selection import normalize_selection_surface
from finance_agent_core.config.knowledge_coverage import KnowledgeCapability
from finance_agent_core.config.source_registry import OfficialSourceRegistry
from finance_agent_core.contracts.queryplan import ProductFamily
from finance_agent_core.retrieval.official_connector import (
    OfficialConnectorError,
    OfficialDiscoveryCandidate,
    OfficialDocumentLinkResolver,
    OfficialFetchRequest,
    OfficialSourceConnector,
    OfficialSourceSnapshot,
)
from finance_agent_core.retrieval.official_discovery import (
    BoundedOfficialDiscoveryProvider,
)
from finance_agent_core.retrieval.pending_corpus import (
    PendingCorpusCatalog,
    PendingCorpusSource,
)
from finance_agent_core.storage.identity_cache import ProductIdentityRecord

_CAPABILITY_QUERY_TERMS = {
    KnowledgeCapability.CORPORATE_SUBSIDIARY: "자회사 계열사 기업관계",
    KnowledgeCapability.FUND_STRUCTURE_STRATEGY_TREND: "펀드 구조 투자전략 운용 동향",
    KnowledgeCapability.PORTFOLIO_HOLDINGS: "구성종목 보유종목 포트폴리오",
    KnowledgeCapability.PRODUCT_RISK_FACTORS: "투자설명서 위험요인 상품 위험",
    KnowledgeCapability.SECTOR_THEME_HISTORY: "섹터 테마 관련 동향",
}
_PURPOSES_BY_CAPABILITY = {
    KnowledgeCapability.CORPORATE_SUBSIDIARY: ("corporate_relation",),
    KnowledgeCapability.FUND_STRUCTURE_STRATEGY_TREND: (
        "operation",
        "product_structure",
        "trend",
    ),
    KnowledgeCapability.PORTFOLIO_HOLDINGS: ("holdings",),
    KnowledgeCapability.PRODUCT_RISK_FACTORS: ("product_structure", "risk"),
    KnowledgeCapability.SECTOR_THEME_HISTORY: ("theme", "trend"),
}
_HANGUL = re.compile(r"[\uac00-\ud7a3]")


@dataclass(frozen=True, slots=True)
class CorpusExpansionQuery:
    source_registry_id: str
    capability: KnowledgeCapability
    query: str


@dataclass(frozen=True, slots=True)
class CorpusExpansionCandidate:
    source_registry_id: str
    url: str
    title: str
    published_at: date | None
    capabilities: tuple[KnowledgeCapability, ...]


@dataclass(frozen=True, slots=True)
class CorpusExpansionStats:
    query_count: int
    discovery_failure_count: int
    discovered_url_count: int
    fetched_url_count: int
    fetch_failure_count: int
    navigation_only_count: int
    catalog_document_count: int


def _record_matches_source(
    record: ProductIdentityRecord,
    *,
    aliases: tuple[str, ...],
    families: tuple[ProductFamily, ...],
) -> bool:
    if record.is_quarantined:
        return False
    try:
        family = ProductFamily(record.product_family)
    except ValueError:
        return False
    if families and family not in families:
        return False
    owner = normalize_selection_surface(record.source_owner or "")
    identities = tuple(
        normalize_selection_surface(value)
        for value in (record.product_name, record.short_name or "")
        if value
    )
    for raw_alias in aliases:
        alias = normalize_selection_surface(raw_alias)
        if not alias:
            continue
        if owner and (owner == alias or alias in owner or owner in alias):
            return True
        # Product-name matching is reserved for distinctive brand aliases.
        # Short owner aliases such as KB or DB must match the DB owner exactly.
        if len(alias) >= 3 and any(alias in identity for identity in identities):
            return True
    return False


def build_corpus_expansion_queries(
    registry: OfficialSourceRegistry,
    records_by_family: Mapping[ProductFamily, Sequence[ProductIdentityRecord]],
    *,
    max_products_per_source: int = 24,
    max_queries: int = 600,
) -> tuple[CorpusExpansionQuery, ...]:
    """Build capability queries from registry metadata and provided-DB identities.

    This planner deliberately contains no company, fund, or product names. New
    sources are supported by registry metadata and new products by the approved
    DB identity snapshots.
    """

    if not 0 <= max_products_per_source <= 200:
        raise ValueError("max_products_per_source must be between 0 and 200")
    if not 1 <= max_queries <= 5000:
        raise ValueError("max_queries must be between 1 and 5000")
    all_records = tuple(
        record
        for family in sorted(records_by_family, key=lambda item: item.value)
        for record in records_by_family[family]
    )
    buckets: list[list[CorpusExpansionQuery]] = []
    seen: set[tuple[str, str, str]] = set()
    for entry in registry.entries:
        capability_buckets: list[list[CorpusExpansionQuery]] = []
        aliases = (entry.publisher, *entry.selection_aliases)
        matched_names = sorted(
            {
                record.product_name.strip()
                for record in all_records
                if record.product_name.strip()
                and _record_matches_source(
                    record,
                    aliases=aliases,
                    families=entry.product_families,
                )
            },
            key=str.casefold,
        )[:max_products_per_source]
        for capability in entry.capabilities:
            capability_bucket: list[CorpusExpansionQuery] = []
            terms = _CAPABILITY_QUERY_TERMS[capability]
            values = (f"{entry.publisher} {terms}", *(f"{name} {terms}" for name in matched_names))
            for query in values:
                normalized = " ".join(query.split())
                key = (entry.source_id, capability.value, normalized.casefold())
                if key in seen:
                    continue
                capability_bucket.append(
                    CorpusExpansionQuery(
                        source_registry_id=entry.source_id,
                        capability=capability,
                        query=normalized,
                    )
                )
                seen.add(key)
            if capability_bucket:
                capability_buckets.append(capability_bucket)
        bucket: list[CorpusExpansionQuery] = []
        capability_depth = 0
        while any(
            capability_depth < len(capability_bucket) for capability_bucket in capability_buckets
        ):
            for capability_bucket in capability_buckets:
                if capability_depth < len(capability_bucket):
                    bucket.append(capability_bucket[capability_depth])
            capability_depth += 1
        if bucket:
            buckets.append(bucket)
    # Round-robin keeps a global query cap from being consumed by the first
    # high-cardinality manager in source-id order.
    planned: list[CorpusExpansionQuery] = []
    depth = 0
    while len(planned) < max_queries and any(depth < len(bucket) for bucket in buckets):
        for bucket in buckets:
            if depth < len(bucket):
                planned.append(bucket[depth])
                if len(planned) == max_queries:
                    break
        depth += 1
    return tuple(planned)


def discover_corpus_candidates(
    provider: BoundedOfficialDiscoveryProvider,
    registry: OfficialSourceRegistry,
    queries: Sequence[CorpusExpansionQuery],
    *,
    top_k: int = 5,
    request_interval_seconds: float = 0.05,
) -> tuple[tuple[CorpusExpansionCandidate, ...], int]:
    if not 1 <= top_k <= 20:
        raise ValueError("top_k must be between 1 and 20")
    if not 0 <= request_interval_seconds <= 5:
        raise ValueError("request_interval_seconds must be between 0 and 5")
    by_url: dict[str, CorpusExpansionCandidate] = {}
    failure_count = 0
    for index, planned in enumerate(queries):
        report = provider.discover_report(
            query=planned.query,
            capability=planned.capability,
            source_registry_ids=(planned.source_registry_id,),
            evidence_cutoff=registry.evidence_cutoff,
            top_k=top_k,
            max_queries=1,
        )
        if report.failure_codes:
            failure_count += 1
        # The live provider may interleave more authorized candidates than the
        # caller's intake budget. Corpus expansion keeps the per-query bound
        # explicit so a broad official domain cannot dominate collection.
        for candidate in report.candidates[:top_k]:
            source = registry.authorize_url(candidate.url, planned.capability)
            if source.source_id != planned.source_registry_id:
                continue
            if (
                candidate.published_at is not None
                and candidate.published_at > registry.evidence_cutoff
            ):
                continue
            previous = by_url.get(candidate.url)
            capabilities = tuple(
                sorted(
                    {
                        planned.capability,
                        *(() if previous is None else previous.capabilities),
                    },
                    key=lambda item: item.value,
                )
            )
            by_url[candidate.url] = CorpusExpansionCandidate(
                source_registry_id=planned.source_registry_id,
                url=candidate.url,
                title=candidate.title if previous is None else previous.title,
                published_at=(
                    candidate.published_at if previous is None else previous.published_at
                ),
                capabilities=capabilities,
            )
        if request_interval_seconds and index + 1 < len(queries):
            time.sleep(request_interval_seconds)
    return tuple(sorted(by_url.values(), key=lambda item: item.url)), failure_count


def _document_id(source_id: str, url: str) -> str:
    return f"{source_id}-{hashlib.sha256(url.encode('utf-8')).hexdigest()[:16]}"


def _language(title: str, content: bytes) -> str:
    sample = f"{title} {content[:4096].decode('utf-8', errors='ignore')}"
    return "ko" if _HANGUL.search(sample) else "en"


def _pending_source(
    snapshot: OfficialSourceSnapshot,
    capabilities: tuple[KnowledgeCapability, ...],
) -> PendingCorpusSource:
    purposes = tuple(
        sorted(
            {
                purpose
                for capability in capabilities
                for purpose in _PURPOSES_BY_CAPABILITY[capability]
            }
        )
    )
    return PendingCorpusSource(
        document_id=_document_id(snapshot.source.source_id, snapshot.source_uri),
        title=snapshot.title,
        source_uri=snapshot.source_uri,
        capabilities=capabilities,
        published_at=snapshot.published_at,
        as_of=snapshot.as_of,
        language=_language(snapshot.title, snapshot.content),
        purposes=purposes,
    )


def materialize_corpus_catalog(
    candidates: Sequence[CorpusExpansionCandidate],
    *,
    connector: OfficialSourceConnector,
    registry: OfficialSourceRegistry,
    max_documents: int = 1000,
    fetch_interval_seconds: float = 0.05,
) -> tuple[PendingCorpusCatalog, CorpusExpansionStats]:
    """Fetch dated official originals and return an unapproved intake catalog."""

    if not 1 <= max_documents <= 1000:
        raise ValueError("max_documents must be between 1 and 1000")
    if not 0 <= fetch_interval_seconds <= 5:
        raise ValueError("fetch_interval_seconds must be between 0 and 5")
    resolver = OfficialDocumentLinkResolver(registry)
    collected: dict[str, PendingCorpusSource] = {}
    fetched_urls: set[str] = set()
    fetch_failures = 0
    navigation_only = 0

    def admit(
        snapshot: OfficialSourceSnapshot, capabilities: tuple[KnowledgeCapability, ...]
    ) -> None:
        source = _pending_source(snapshot, capabilities)
        previous = collected.get(source.source_uri)
        if previous is None:
            collected[source.source_uri] = source
            return
        merged_capabilities = tuple(
            sorted({*previous.capabilities, *source.capabilities}, key=lambda item: item.value)
        )
        merged_purposes = tuple(sorted({*previous.purposes, *source.purposes}))
        collected[source.source_uri] = previous.model_copy(
            update={"capabilities": merged_capabilities, "purposes": merged_purposes}
        )

    for candidate_index, candidate in enumerate(candidates):
        if len(collected) >= max_documents:
            break
        primary_capability = candidate.capabilities[0]
        discovered = OfficialDiscoveryCandidate(
            url=candidate.url,
            title=candidate.title,
            published_at=candidate.published_at,
        )
        try:
            snapshot = connector.fetch(
                discovered,
                OfficialFetchRequest(
                    url=candidate.url,
                    capability=primary_capability,
                    published_at=candidate.published_at,
                    timeout_seconds=20,
                    max_bytes=16 * 1024 * 1024,
                    allow_navigation_only=True,
                ),
            )
            fetched_urls.add(candidate.url)
        except OfficialConnectorError:
            fetch_failures += 1
            continue
        if isinstance(snapshot, OfficialSourceSnapshot):
            admit(snapshot, candidate.capabilities)
        else:
            navigation_only += 1
            linked = resolver.resolve(
                snapshot,
                max_links=8,
                evidence_as_of=registry.evidence_cutoff,
            )
            for linked_candidate in linked:
                if len(collected) >= max_documents:
                    break
                try:
                    linked_snapshot = connector.fetch(
                        linked_candidate,
                        OfficialFetchRequest(
                            url=linked_candidate.url,
                            capability=primary_capability,
                            published_at=linked_candidate.published_at,
                            as_of=linked_candidate.as_of,
                            timeout_seconds=20,
                            max_bytes=16 * 1024 * 1024,
                        ),
                    )
                    fetched_urls.add(linked_candidate.url)
                except OfficialConnectorError:
                    fetch_failures += 1
                    continue
                if isinstance(linked_snapshot, OfficialSourceSnapshot):
                    admit(linked_snapshot, candidate.capabilities)
        if fetch_interval_seconds and candidate_index + 1 < len(candidates):
            time.sleep(fetch_interval_seconds)

    documents = tuple(sorted(collected.values(), key=lambda item: item.document_id))
    if not documents:
        raise ValueError("official corpus discovery produced no dated documents")
    digest = hashlib.sha256(
        "\n".join(f"{item.document_id}\t{item.source_uri}" for item in documents).encode("utf-8")
    ).hexdigest()[:16]
    catalog = PendingCorpusCatalog(
        catalog_id=f"official-corpus-expanded-20260824-{digest}",
        evidence_cutoff=registry.evidence_cutoff,
        documents=documents,
    )
    return catalog, CorpusExpansionStats(
        query_count=0,
        discovery_failure_count=0,
        discovered_url_count=len(candidates),
        fetched_url_count=len(fetched_urls),
        fetch_failure_count=fetch_failures,
        navigation_only_count=navigation_only,
        catalog_document_count=len(documents),
    )


def merge_pending_catalogs(
    registry: OfficialSourceRegistry,
    catalogs: Sequence[PendingCorpusCatalog],
) -> PendingCorpusCatalog:
    """Union generated and packaged seeds without granting retrieval authority."""

    if not catalogs:
        raise ValueError("at least one pending catalog is required")
    by_url: dict[str, PendingCorpusSource] = {}
    for catalog in catalogs:
        if catalog.evidence_cutoff != registry.evidence_cutoff:
            raise ValueError("pending catalog evidence cutoff differs from Source Registry")
        for document in catalog.documents:
            source_ids = {
                registry.authorize_url(document.source_uri, capability).source_id
                for capability in document.capabilities
            }
            if len(source_ids) != 1:
                raise ValueError("pending document capabilities cross Source Registry entries")
            previous = by_url.get(document.source_uri)
            if previous is None:
                by_url[document.source_uri] = document
                continue
            if previous.as_of != document.as_of or (
                previous.published_at is not None
                and document.published_at is not None
                and previous.published_at != document.published_at
            ):
                raise ValueError("duplicate pending URL carries conflicting metadata")
            # Later catalogs are explicit curated seeds and may replace a
            # search-derived title/language or fill a missing publication date.
            by_url[document.source_uri] = document.model_copy(
                update={
                    "published_at": document.published_at or previous.published_at,
                    "capabilities": tuple(
                        sorted(
                            {*previous.capabilities, *document.capabilities},
                            key=lambda item: item.value,
                        )
                    ),
                    "purposes": tuple(sorted({*previous.purposes, *document.purposes})),
                }
            )
    documents = tuple(sorted(by_url.values(), key=lambda item: item.document_id))
    digest = hashlib.sha256(
        "\n".join(f"{item.document_id}\t{item.source_uri}" for item in documents).encode("utf-8")
    ).hexdigest()[:16]
    return PendingCorpusCatalog(
        catalog_id=f"official-corpus-expanded-20260824-{digest}",
        evidence_cutoff=registry.evidence_cutoff,
        documents=documents,
    )


__all__ = [
    "CorpusExpansionCandidate",
    "CorpusExpansionQuery",
    "CorpusExpansionStats",
    "build_corpus_expansion_queries",
    "discover_corpus_candidates",
    "materialize_corpus_catalog",
    "merge_pending_catalogs",
]
