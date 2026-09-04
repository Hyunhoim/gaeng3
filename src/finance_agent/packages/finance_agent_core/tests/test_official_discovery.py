from __future__ import annotations

import hashlib
from datetime import date
from urllib.parse import urlsplit

from finance_agent_core.config.knowledge_coverage import KnowledgeCapability
from finance_agent_core.config.source_registry import (
    OfficialSourceEntry,
    OfficialSourceRegistry,
    load_official_source_registry,
)
from finance_agent_core.contracts.external_knowledge import KnowledgeRequirement
from finance_agent_core.contracts.queryplan import ProductFamily
from finance_agent_core.retrieval.official_acquisition import (
    OfficialEvidenceAcquisitionCoordinator,
    OfficialEvidenceAcquisitionStatus,
)
from finance_agent_core.retrieval.official_connector import (
    OfficialDiscoveryCandidate,
    OfficialDocumentLinkResolver,
    OfficialHttpResponse,
    OfficialSourceConnector,
    OfficialSourceSnapshot,
)
from finance_agent_core.retrieval.official_discovery import (
    BoundedOfficialDiscoveryProvider,
    OfficialWebSearchPage,
)
from finance_agent_core.retrieval.temporal_relations import (
    TemporalRelationStore,
    seal_temporal_relation_bundle,
)


def _registry(source_count: int) -> OfficialSourceRegistry:
    return OfficialSourceRegistry(
        schema_version="1.0",
        registry_id="test-official-discovery-ranking",
        evidence_cutoff=date(2026, 8, 24),
        entries=tuple(
            OfficialSourceEntry(
                source_id=f"issuer-{index:02d}",
                publisher=f"Issuer {index:02d}",
                authority="product_issuer",
                hostname=f"issuer-{index:02d}.example",
                path_prefixes=("/docs/",),
                connector_kind="html_or_pdf",
                capabilities=(KnowledgeCapability.PORTFOLIO_HOLDINGS,),
                product_families=("overseas_etp",),
                status="approved_for_live_evidence",
                publication_date_required=False,
            )
            for index in range(source_count)
        ),
    )


class _FakeSearchTransport:
    def __init__(
        self,
        items_by_hostname: dict[str, tuple[OfficialDiscoveryCandidate, ...]],
    ) -> None:
        self.items_by_hostname = items_by_hostname
        self.queries: list[str] = []

    def search(self, *, query: str, display: int, start: int) -> OfficialWebSearchPage:
        self.queries.append(query)
        site_term = next(item for item in query.split() if item.startswith("site:"))
        items = self.items_by_hostname[site_term.removeprefix("site:")]
        return OfficialWebSearchPage(
            total=len(items),
            start=start,
            display=min(display, len(items)),
            items=items,
        )


def test_later_relevant_source_leads_while_round_robin_preserves_source_diversity() -> None:
    registry = _registry(9)
    items_by_hostname = {
        entry.hostname: (
            OfficialDiscoveryCandidate(
                url=f"https://{entry.hostname}/docs/retail-etf-overview.pdf",
                title="Retail ETF overview",
            ),
        )
        for entry in registry.entries
    }
    last_hostname = registry.entries[-1].hostname
    items_by_hostname[last_hostname] = (
        OfficialDiscoveryCandidate(
            url=f"https://{last_hostname}/docs/retail-etf-overview.pdf",
            title="Retail ETF overview",
        ),
        OfficialDiscoveryCandidate(
            url=f"https://{last_hostname}/docs/ai-etf-portfolio.pdf",
            title="AI ETF portfolio report",
        ),
        OfficialDiscoveryCandidate(
            url=f"https://{last_hostname}/docs/ai-semiconductor-holdings.pdf",
            title="AI semiconductor ETF holdings",
        ),
    )
    provider = BoundedOfficialDiscoveryProvider(_FakeSearchTransport(items_by_hostname), registry)

    report = provider.discover_report(
        query="AI ETF",
        capability=KnowledgeCapability.PORTFOLIO_HOLDINGS,
        source_registry_ids=tuple(entry.source_id for entry in registry.entries),
        evidence_cutoff=registry.evidence_cutoff,
        top_k=8,
        max_queries=len(registry.entries),
    )

    assert {item.title for item in report.candidates[:2]} == {
        "AI ETF portfolio report",
        "AI semiconductor ETF holdings",
    }
    assert {urlsplit(candidate.url).hostname for candidate in report.candidates[:2]} == {
        last_hostname
    }
    first_round_hosts = {
        urlsplit(candidate.url).hostname
        for candidate in report.candidates[2 : 2 + len(registry.entries)]
    }
    assert first_round_hosts == {entry.hostname for entry in registry.entries}


def _seed_registry() -> OfficialSourceRegistry:
    return OfficialSourceRegistry(
        schema_version="1.0",
        registry_id="test-subject-aware-seeds",
        evidence_cutoff=date(2026, 8, 24),
        entries=(
            OfficialSourceEntry(
                source_id="alpha-official",
                publisher="Alpha Manager",
                authority="asset_manager",
                hostname="alpha.example",
                path_prefixes=("/docs/",),
                connector_kind="html",
                capabilities=(KnowledgeCapability.PORTFOLIO_HOLDINGS,),
                selection_aliases=("ALPHA",),
                discovery_urls=("https://alpha.example/docs/catalog",),
                product_families=("domestic_etp",),
                status="approved_for_live_evidence",
                publication_date_required=False,
            ),
            OfficialSourceEntry(
                source_id="beta-official",
                publisher="Beta Manager",
                authority="asset_manager",
                hostname="beta.example",
                path_prefixes=("/docs/",),
                connector_kind="html",
                capabilities=(KnowledgeCapability.PORTFOLIO_HOLDINGS,),
                selection_aliases=("BETA",),
                discovery_urls=("https://beta.example/docs/catalog",),
                product_families=("domestic_etp",),
                status="approved_for_live_evidence",
                publication_date_required=False,
            ),
        ),
    )


def test_discovery_seeds_require_subject_relevance_for_multi_publisher_search() -> None:
    registry = _seed_registry()
    transport = _FakeSearchTransport({entry.hostname: () for entry in registry.entries})
    provider = BoundedOfficialDiscoveryProvider(transport, registry)
    unrelated = provider.discover_report(
        query="Zythera holdings",
        capability=KnowledgeCapability.PORTFOLIO_HOLDINGS,
        source_registry_ids=tuple(entry.source_id for entry in registry.entries),
        evidence_cutoff=registry.evidence_cutoff,
        top_k=2,
    )
    alpha = provider.discover_report(
        query="ALPHA holdings",
        capability=KnowledgeCapability.PORTFOLIO_HOLDINGS,
        source_registry_ids=tuple(entry.source_id for entry in registry.entries),
        evidence_cutoff=registry.evidence_cutoff,
        top_k=2,
    )

    assert unrelated.candidates == ()
    assert [item.url for item in alpha.candidates] == ["https://alpha.example/docs/catalog"]
    assert transport.queries[:2] == [
        "site:alpha.example Zythera holdings",
        "site:beta.example Zythera holdings",
    ]


def test_kind_registry_admits_only_the_approved_official_disclosure_viewer() -> None:
    registry = load_official_source_registry()
    source = registry.authorize_url(
        (
            "https://kind.krx.co.kr/common/disclsviewer.do?"
            "method=searchInitInfo&acptNo=20260429000124"
        ),
        KnowledgeCapability.CORPORATE_SUBSIDIARY,
    )

    assert source.source_id == "kind-corporate-disclosures"


def test_single_targeted_source_retains_its_approved_navigation_seed() -> None:
    registry = _seed_registry()
    beta = registry.require_source_id("beta-official")
    provider = BoundedOfficialDiscoveryProvider(
        _FakeSearchTransport({beta.hostname: ()}),
        registry,
    )

    report = provider.discover_report(
        query="Zythera holdings",
        capability=KnowledgeCapability.PORTFOLIO_HOLDINGS,
        source_registry_ids=(beta.source_id,),
        evidence_cutoff=registry.evidence_cutoff,
        top_k=1,
    )

    assert [item.url for item in report.candidates] == ["https://beta.example/docs/catalog"]


def test_query_token_substrings_do_not_promote_unrelated_pages() -> None:
    registry = _registry(2)
    first, second = registry.entries
    items_by_hostname = {
        first.hostname: (
            OfficialDiscoveryCandidate(
                url=f"https://{first.hostname}/docs/detail/retail-guide.pdf",
                title="Retail analytics guide",
            ),
        ),
        second.hostname: (
            OfficialDiscoveryCandidate(
                url=f"https://{second.hostname}/docs/list",
                title="AI",
            ),
        ),
    }
    provider = BoundedOfficialDiscoveryProvider(_FakeSearchTransport(items_by_hostname), registry)

    first_report = provider.discover_report(
        query="AI",
        capability=KnowledgeCapability.PORTFOLIO_HOLDINGS,
        source_registry_ids=(first.source_id, second.source_id),
        evidence_cutoff=registry.evidence_cutoff,
        top_k=2,
    )
    second_report = provider.discover_report(
        query="AI",
        capability=KnowledgeCapability.PORTFOLIO_HOLDINGS,
        source_registry_ids=(first.source_id, second.source_id),
        evidence_cutoff=registry.evidence_cutoff,
        top_k=2,
    )

    assert urlsplit(first_report.candidates[0].url).hostname == second.hostname
    assert first_report.candidates == second_report.candidates


def test_off_domain_site_result_is_removed_from_the_approved_report() -> None:
    registry = _registry(1)
    entry = registry.entries[0]
    provider = BoundedOfficialDiscoveryProvider(
        _FakeSearchTransport(
            {
                entry.hostname: (
                    OfficialDiscoveryCandidate(
                        url="https://unapproved.example/docs/ai-etf.pdf",
                        title="AI ETF portfolio report",
                    ),
                )
            }
        ),
        registry,
    )

    report = provider.discover_report(
        query="AI ETF",
        capability=KnowledgeCapability.PORTFOLIO_HOLDINGS,
        source_registry_ids=(entry.source_id,),
        evidence_cutoff=registry.evidence_cutoff,
        top_k=1,
    )

    assert report.candidates == ()
    assert report.queried_source_registry_ids == (entry.source_id,)


class _RecordingHttpTransport:
    def __init__(self) -> None:
        self.requested_urls: list[str] = []

    def fetch(self, url: str, *, timeout_seconds: float, max_bytes: int) -> OfficialHttpResponse:
        assert timeout_seconds > 0
        body = b"<html><body>Holdings as of August 20, 2026.</body></html>"
        assert len(body) <= max_bytes
        self.requested_urls.append(url)
        return OfficialHttpResponse(
            status_code=200,
            final_url=url,
            content_type="text/html",
            body=body,
        )


class _StaticDiscovery:
    def __init__(self, candidate: OfficialDiscoveryCandidate) -> None:
        self.candidate = candidate

    def discover(self, **kwargs):
        assert kwargs
        return (self.candidate,)


class _MappedNavigationTransport:
    def __init__(self, bodies: dict[str, bytes]) -> None:
        self.bodies = bodies
        self.requested_urls: list[str] = []

    def fetch(self, url: str, *, timeout_seconds: float, max_bytes: int) -> OfficialHttpResponse:
        assert timeout_seconds > 0
        body = self.bodies[url]
        assert len(body) <= max_bytes
        self.requested_urls.append(url)
        return OfficialHttpResponse(
            status_code=200,
            final_url=url,
            content_type="text/html",
            body=body,
        )


def _navigation_registry() -> OfficialSourceRegistry:
    return OfficialSourceRegistry(
        schema_version="1.0",
        registry_id="test-depth-two-navigation",
        evidence_cutoff=date(2026, 8, 24),
        entries=(
            OfficialSourceEntry(
                source_id="issuer-navigation",
                publisher="Issuer Navigation",
                authority="product_issuer",
                hostname="issuer-navigation.example",
                path_prefixes=("/docs/",),
                connector_kind="html",
                capabilities=(KnowledgeCapability.PORTFOLIO_HOLDINGS,),
                product_families=("overseas_etp",),
                status="approved_for_live_evidence",
                publication_date_required=False,
            ),
        ),
    )


def _empty_store(registry: OfficialSourceRegistry) -> TemporalRelationStore:
    return TemporalRelationStore(
        seal_temporal_relation_bundle(
            (),
            corpus_manifest_sha256="a" * 64,
            registry=registry,
        ),
        registry,
    )


def _navigation_requirement(source_id: str) -> KnowledgeRequirement:
    return KnowledgeRequirement(
        requirement_id="need_1",
        capability=KnowledgeCapability.PORTFOLIO_HOLDINGS,
        predicate="holds_company",
        subject="Zythera",
        subject_evidence_span="Zythera",
        relation_evidence_span="holds",
        product_families=(ProductFamily.OVERSEAS_ETP,),
        source_registry_ids=(source_id,),
    )


def test_acquisition_follows_two_official_link_hops_and_stops_cycles() -> None:
    registry = _navigation_registry()
    root = "https://issuer-navigation.example/docs/root"
    child = "https://issuer-navigation.example/docs/child"
    evidence = "https://issuer-navigation.example/docs/evidence"
    child_body = b"".join(
        (
            b'<html><a href="/docs/root">cycle</a>',
            b'<a href="/docs/evidence">evidence</a></html>',
        )
    )
    transport = _MappedNavigationTransport(
        {
            root: b'<html><a href="/docs/child">child</a></html>',
            child: child_body,
            evidence: b"<html><body>Holdings as of August 20, 2026.</body></html>",
        }
    )
    coordinator = OfficialEvidenceAcquisitionCoordinator(
        relation_store=_empty_store(registry),
        discovery_provider=_StaticDiscovery(
            OfficialDiscoveryCandidate(url=root, title="official root")
        ),
        connector=OfficialSourceConnector(transport, registry),
    )

    result = coordinator.acquire(
        _navigation_requirement("issuer-navigation"),
        top_k=1,
        max_fetches=3,
    )

    assert result.status is OfficialEvidenceAcquisitionStatus.LIVE_EVIDENCE_CANDIDATE
    assert result.fetch_count == 3
    assert transport.requested_urls == [root, child, evidence]
    assert [item.source_uri for item in result.snapshots] == [evidence]


def test_acquisition_never_traverses_beyond_two_official_link_hops() -> None:
    registry = _navigation_registry()
    root = "https://issuer-navigation.example/docs/root"
    child = "https://issuer-navigation.example/docs/child"
    grandchild = "https://issuer-navigation.example/docs/grandchild"
    too_deep = "https://issuer-navigation.example/docs/too-deep"
    transport = _MappedNavigationTransport(
        {
            root: b'<html><a href="/docs/child">child</a></html>',
            child: b'<html><a href="/docs/grandchild">grandchild</a></html>',
            grandchild: b'<html><a href="/docs/too-deep">too deep</a></html>',
            too_deep: b"<html><body>Holdings as of August 20, 2026.</body></html>",
        }
    )
    coordinator = OfficialEvidenceAcquisitionCoordinator(
        relation_store=_empty_store(registry),
        discovery_provider=_StaticDiscovery(
            OfficialDiscoveryCandidate(url=root, title="official root")
        ),
        connector=OfficialSourceConnector(transport, registry),
    )

    result = coordinator.acquire(
        _navigation_requirement("issuer-navigation"),
        top_k=1,
        max_fetches=4,
    )

    assert result.status is OfficialEvidenceAcquisitionStatus.CANNOT_VERIFY
    assert result.fetch_count == 3
    assert transport.requested_urls == [root, child, grandchild]


def test_unknown_entity_from_late_issuer_enters_single_fetch_budget() -> None:
    registry = load_official_source_registry()
    entries = registry.entries_for_capability(
        KnowledgeCapability.PORTFOLIO_HOLDINGS,
    )
    source_ids = tuple(entry.source_id for entry in entries)
    assert len(source_ids) > 20
    assert entries[-1].source_id == "wisdomtree-etf-official"
    items_by_hostname: dict[str, tuple[OfficialDiscoveryCandidate, ...]] = {}
    for entry in entries:
        prefix = entry.path_prefixes[0]
        separator = "" if prefix.endswith("/") else "/"
        suffix = "unrelated.pdf" if entry.connector_kind == "pdf" else "unrelated"
        items_by_hostname[entry.hostname] = (
            OfficialDiscoveryCandidate(
                url=f"https://{entry.hostname}{prefix}{separator}{suffix}",
                title="Generic official product overview",
            ),
        )
    late_source = registry.require_source_id("wisdomtree-etf-official")
    relevant_url = "https://www.wisdomtree.com/investments/zythera-etf"
    items_by_hostname[late_source.hostname] = (
        OfficialDiscoveryCandidate(
            url=relevant_url,
            title="Zythera ETF official holdings",
        ),
    )
    discovery = BoundedOfficialDiscoveryProvider(
        _FakeSearchTransport(items_by_hostname),
        registry,
    )
    http_transport = _RecordingHttpTransport()
    relation_store = TemporalRelationStore(
        seal_temporal_relation_bundle(
            (),
            corpus_manifest_sha256="a" * 64,
            registry=registry,
        ),
        registry,
    )
    coordinator = OfficialEvidenceAcquisitionCoordinator(
        relation_store=relation_store,
        discovery_provider=discovery,
        connector=OfficialSourceConnector(http_transport, registry),
    )
    requirement = KnowledgeRequirement(
        requirement_id="need_1",
        capability=KnowledgeCapability.PORTFOLIO_HOLDINGS,
        predicate="holds_company",
        subject="Zythera",
        subject_evidence_span="Zythera",
        relation_evidence_span="holds",
        product_families=(ProductFamily.OVERSEAS_ETP,),
        source_registry_ids=source_ids,
    )

    result = coordinator.acquire(
        requirement,
        top_k=1,
        max_search_queries=len(source_ids),
        max_fetches=1,
    )

    assert result.status is OfficialEvidenceAcquisitionStatus.LIVE_EVIDENCE_CANDIDATE
    assert result.fetch_count == 1
    assert http_transport.requested_urls == [relevant_url]
    assert result.snapshots[0].source.source_id == "wisdomtree-etf-official"


def test_typed_discovery_uses_exact_relation_query_before_broad_fallback() -> None:
    registry = _registry(2)
    transport = _FakeSearchTransport({entry.hostname: () for entry in registry.entries})
    provider = BoundedOfficialDiscoveryProvider(transport, registry)

    report = provider.discover_report(
        query="Zythera Aurora Growth ETF",
        capability=KnowledgeCapability.PORTFOLIO_HOLDINGS,
        source_registry_ids=tuple(entry.source_id for entry in registry.entries),
        evidence_cutoff=registry.evidence_cutoff,
        top_k=2,
        max_queries=6,
        entity_terms=("Zythera",),
        product_terms=("Aurora Growth ETF",),
        relation_terms=("holds",),
    )

    assert report.search_query_count == 6
    assert report.queried_source_registry_ids == tuple(
        entry.source_id for entry in registry.entries
    )
    assert all('"Zythera"' in query for query in transport.queries[:2])
    assert all('"Aurora Growth ETF"' in query for query in transport.queries[:2])
    assert all("holdings" in query for query in transport.queries[:2])
    assert all('"Zythera"' in query for query in transport.queries[2:4])
    assert all('"Aurora Growth ETF"' in query for query in transport.queries[2:4])
    assert all("factsheet portfolio" in query for query in transport.queries[2:4])
    assert transport.queries[4:] == [
        "site:issuer-00.example Zythera Aurora Growth ETF",
        "site:issuer-01.example Zythera Aurora Growth ETF",
    ]


def test_exact_query_deduplicates_product_name_and_keeps_db_ticker() -> None:
    registry = _registry(1)
    transport = _FakeSearchTransport({registry.entries[0].hostname: ()})
    provider = BoundedOfficialDiscoveryProvider(transport, registry)

    provider.discover_report(
        query="Aurora Solar ETF",
        capability=KnowledgeCapability.PORTFOLIO_HOLDINGS,
        source_registry_ids=(registry.entries[0].source_id,),
        evidence_cutoff=registry.evidence_cutoff,
        top_k=1,
        max_queries=1,
        entity_terms=("Aurora Solar ETF",),
        product_terms=("Aurora Solar ETF", "AURR.O"),
        relation_terms=("holdings",),
    )

    assert transport.queries[0].count('"Aurora Solar ETF"') == 1
    assert '"AURR.O"' in transport.queries[0]


def test_one_site_query_covers_multiple_approved_paths_on_the_same_hostname() -> None:
    entries = tuple(
        OfficialSourceEntry(
            source_id=f"issuer-path-{index}",
            publisher="One Issuer",
            authority="product_issuer",
            hostname="one-issuer.example",
            path_prefixes=(prefix,),
            connector_kind="html_or_pdf",
            capabilities=(KnowledgeCapability.PORTFOLIO_HOLDINGS,),
            product_families=("overseas_etp",),
            status="approved_for_live_evidence",
            publication_date_required=False,
        )
        for index, prefix in enumerate(("/funds/", "/documents/"), start=1)
    )
    registry = OfficialSourceRegistry(
        schema_version="1.0",
        registry_id="test-shared-host-query-budget",
        evidence_cutoff=date(2026, 8, 24),
        entries=entries,
    )
    transport = _FakeSearchTransport({"one-issuer.example": ()})
    provider = BoundedOfficialDiscoveryProvider(transport, registry)

    report = provider.discover_report(
        query="Zythera holdings",
        capability=KnowledgeCapability.PORTFOLIO_HOLDINGS,
        source_registry_ids=tuple(item.source_id for item in entries),
        evidence_cutoff=registry.evidence_cutoff,
        top_k=1,
        max_queries=1,
        entity_terms=("Zythera",),
        relation_terms=("holdings",),
    )

    assert report.search_query_count == 1
    assert report.queried_source_registry_ids == tuple(item.source_id for item in entries)
    assert len(transport.queries) == 1


def test_search_summary_and_factsheet_cues_rank_exact_relation_candidate_first() -> None:
    registry = _registry(1)
    entry = registry.entries[0]
    relevant = OfficialDiscoveryCandidate(
        url=f"https://{entry.hostname}/docs/aurora-holdings.pdf",
        title="Aurora Growth ETF factsheet",
        summary="Aurora Growth ETF portfolio holdings include Zythera Corporation.",
    )
    unrelated = OfficialDiscoveryCandidate(
        url=f"https://{entry.hostname}/docs/generic-factsheet.pdf",
        title="Generic ETF factsheet",
        summary="General product overview.",
    )
    provider = BoundedOfficialDiscoveryProvider(
        _FakeSearchTransport({entry.hostname: (unrelated, relevant)}),
        registry,
    )

    report = provider.discover_report(
        query="Zythera Aurora Growth ETF",
        capability=KnowledgeCapability.PORTFOLIO_HOLDINGS,
        source_registry_ids=(entry.source_id,),
        evidence_cutoff=registry.evidence_cutoff,
        top_k=2,
        entity_terms=("Zythera",),
        product_terms=("Aurora Growth ETF",),
        relation_terms=("holdings",),
    )

    assert report.candidates[0] == relevant


def test_top_result_from_exact_entity_query_is_preserved_for_byte_verification() -> None:
    registry = _registry(1)
    entry = registry.entries[0]
    provider_ranked = OfficialDiscoveryCandidate(
        url=f"https://{entry.hostname}/docs/semiconductor-product",
        title="Semiconductor ETF | CHIP",
        # The provider does not echo Zythera in its snippet. The fetched bytes,
        # not this metadata, remain responsible for exact relation evidence.
        summary="Official product page.",
    )
    generic = OfficialDiscoveryCandidate(
        url=f"https://{entry.hostname}/docs/generic-holdings",
        title="Generic ETF holdings",
        summary="Portfolio holdings overview.",
    )
    provider = BoundedOfficialDiscoveryProvider(
        _FakeSearchTransport({entry.hostname: (provider_ranked, generic)}),
        registry,
    )

    report = provider.discover_report(
        query="Zythera holdings",
        capability=KnowledgeCapability.PORTFOLIO_HOLDINGS,
        source_registry_ids=(entry.source_id,),
        evidence_cutoff=registry.evidence_cutoff,
        top_k=2,
        max_queries=1,
        entity_terms=("Zythera",),
        relation_terms=("holdings",),
    )

    assert report.candidates[0] == provider_ranked


def test_acquisition_promotes_search_candidate_with_provided_db_identity() -> None:
    registry = _registry(1)
    entry = registry.entries[0]
    generic_url = f"https://{entry.hostname}/docs/generic"
    matched_url = f"https://{entry.hostname}/docs/chip"

    class RankedDiscovery:
        def discover(self, **_kwargs):
            return (
                OfficialDiscoveryCandidate(url=generic_url, title="Generic ETF holdings"),
                OfficialDiscoveryCandidate(
                    url=matched_url,
                    title="Aurora Semiconductor ETF | CHIP",
                ),
            )

    transport = _RecordingHttpTransport()
    coordinator = OfficialEvidenceAcquisitionCoordinator(
        relation_store=_empty_store(registry),
        discovery_provider=RankedDiscovery(),
        connector=OfficialSourceConnector(transport, registry),
    )

    result = coordinator.acquire(
        _navigation_requirement(entry.source_id),
        top_k=1,
        max_fetches=1,
        provided_product_surfaces=("Aurora Semiconductor ETF", "CHIP"),
    )

    assert result.status is OfficialEvidenceAcquisitionStatus.LIVE_EVIDENCE_CANDIDATE
    assert transport.requested_urls == [matched_url]


def test_acquisition_round_robins_sources_before_following_noisy_siblings() -> None:
    registry = _registry(2)
    first, second = registry.entries
    first_one = f"https://{first.hostname}/docs/one"
    first_two = f"https://{first.hostname}/docs/two"
    second_one = f"https://{second.hostname}/docs/one"

    class MultiDiscovery:
        def discover(self, **_kwargs):
            return (
                OfficialDiscoveryCandidate(url=first_one, title="first one"),
                OfficialDiscoveryCandidate(url=first_two, title="first two"),
                OfficialDiscoveryCandidate(url=second_one, title="second one"),
            )

    transport = _RecordingHttpTransport()
    coordinator = OfficialEvidenceAcquisitionCoordinator(
        relation_store=_empty_store(registry),
        discovery_provider=MultiDiscovery(),
        connector=OfficialSourceConnector(transport, registry),
    )

    result = coordinator.acquire(
        _navigation_requirement(first.source_id).model_copy(
            update={"source_registry_ids": (first.source_id, second.source_id)}
        ),
        top_k=2,
        max_fetches=2,
    )

    assert result.status is OfficialEvidenceAcquisitionStatus.LIVE_EVIDENCE_CANDIDATE
    assert transport.requested_urls == [first_one, second_one]


def test_corporate_official_source_precedes_regulator_for_subsidiary_evidence() -> None:
    registry = OfficialSourceRegistry(
        schema_version="1.0",
        registry_id="test-corporate-authority-priority",
        evidence_cutoff=date(2026, 8, 24),
        entries=(
            OfficialSourceEntry(
                source_id="aurora-corporate",
                publisher="Aurora Group",
                authority="corporate_official",
                hostname="aurora.example",
                path_prefixes=("/company/",),
                connector_kind="html_or_pdf",
                capabilities=(KnowledgeCapability.CORPORATE_SUBSIDIARY,),
                selection_aliases=("Aurora",),
                product_families=(),
                status="approved_for_live_evidence",
                publication_date_required=False,
            ),
            OfficialSourceEntry(
                source_id="market-regulator",
                publisher="Market Regulator",
                authority="regulator",
                hostname="regulator.example",
                path_prefixes=("/filing/",),
                connector_kind="html_or_pdf",
                capabilities=(KnowledgeCapability.CORPORATE_SUBSIDIARY,),
                product_families=(),
                status="approved_for_live_evidence",
                publication_date_required=False,
            ),
        ),
    )
    candidates = {
        "aurora.example": (
            OfficialDiscoveryCandidate(
                url="https://aurora.example/company/subsidiaries",
                title="Aurora Group subsidiaries",
            ),
        ),
        "regulator.example": (
            OfficialDiscoveryCandidate(
                url="https://regulator.example/filing/aurora",
                title="Aurora Group subsidiary filing",
            ),
        ),
    }
    provider = BoundedOfficialDiscoveryProvider(_FakeSearchTransport(candidates), registry)

    report = provider.discover_report(
        query="Aurora Group subsidiaries",
        capability=KnowledgeCapability.CORPORATE_SUBSIDIARY,
        source_registry_ids=("aurora-corporate", "market-regulator"),
        evidence_cutoff=registry.evidence_cutoff,
        top_k=2,
        entity_terms=("Aurora Group",),
        relation_terms=("subsidiaries",),
    )

    assert report.candidates[0].url == "https://aurora.example/company/subsidiaries"


def test_link_resolver_prioritizes_factsheet_anchor_text_for_opaque_pdf_url() -> None:
    registry = OfficialSourceRegistry(
        schema_version="1.0",
        registry_id="test-link-anchor-ranking",
        evidence_cutoff=date(2026, 8, 24),
        entries=(
            OfficialSourceEntry(
                source_id="issuer-documents",
                publisher="Issuer Documents",
                authority="product_issuer",
                hostname="issuer-documents.example",
                path_prefixes=("/docs/",),
                connector_kind="html_or_pdf",
                capabilities=(KnowledgeCapability.PORTFOLIO_HOLDINGS,),
                product_families=("overseas_etp",),
                status="approved_for_live_evidence",
                publication_date_required=False,
            ),
        ),
    )
    url = "https://issuer-documents.example/docs/product"
    body = (
        b'<a href="/docs/news.pdf">General news</a>'
        b'<a href="/docs/a1.pdf"><span>Portfolio holdings factsheet</span></a>'
    )
    snapshot = OfficialSourceSnapshot(
        source=registry.authorize_url(url, KnowledgeCapability.PORTFOLIO_HOLDINGS),
        capability=KnowledgeCapability.PORTFOLIO_HOLDINGS,
        source_uri=url,
        title="official product page",
        content_type="text/html",
        content=body,
        content_sha256=hashlib.sha256(body).hexdigest(),
        published_at=None,
        as_of=date(2026, 8, 24),
    )

    linked = OfficialDocumentLinkResolver(registry).resolve(snapshot, max_links=1)

    assert [item.url for item in linked] == ["https://issuer-documents.example/docs/a1.pdf"]


def test_link_resolver_discovers_registry_authorized_jsonld_data_download() -> None:
    registry = _navigation_registry()
    url = "https://issuer-navigation.example/docs/product"
    body = b"""<html><script type="application/ld+json">
      {"mainEntity":{"contentUrl":
      "https://issuer-navigation.example/docs/latest-holdings.csv",
      "encodingFormat":"text/csv"}}
    </script></html>"""
    snapshot = OfficialSourceSnapshot(
        source=registry.authorize_url(url, KnowledgeCapability.PORTFOLIO_HOLDINGS),
        capability=KnowledgeCapability.PORTFOLIO_HOLDINGS,
        source_uri=url,
        title="Aurora ETF",
        content_type="text/html",
        content=body,
        content_sha256=hashlib.sha256(body).hexdigest(),
        published_at=None,
        as_of=date(2026, 8, 24),
    )

    linked = OfficialDocumentLinkResolver(registry).resolve(snapshot)

    assert linked[0].url == ("https://issuer-navigation.example/docs/latest-holdings.csv")


def test_official_holdings_csv_is_admitted_with_source_authored_as_of_date() -> None:
    registry = _navigation_registry()
    url = "https://issuer-navigation.example/docs/aurora-holdings.csv"

    class CsvTransport:
        def fetch(self, requested_url: str, **_kwargs) -> OfficialHttpResponse:
            assert requested_url == url
            return OfficialHttpResponse(
                status_code=200,
                final_url=url,
                content_type="text/csv",
                body=(
                    b'Aurora ETF\nFund Holdings as of,"Aug 20, 2026"\n'
                    b"Ticker,Name\nZYTH,Zythera Corp\n"
                ),
            )

    coordinator = OfficialEvidenceAcquisitionCoordinator(
        relation_store=_empty_store(registry),
        discovery_provider=_StaticDiscovery(
            OfficialDiscoveryCandidate(url=url, title="Aurora ETF holdings")
        ),
        connector=OfficialSourceConnector(CsvTransport(), registry),
    )

    result = coordinator.acquire(
        _navigation_requirement("issuer-navigation"),
        top_k=1,
        max_fetches=1,
    )

    assert result.status is OfficialEvidenceAcquisitionStatus.LIVE_EVIDENCE_CANDIDATE
    assert result.snapshots[0].as_of == date(2026, 8, 20)


def test_mobile_kodex_product_page_resolves_to_approved_desktop_risk_pdf() -> None:
    registry = load_official_source_registry()
    url = "https://m.samsungfund.com/etf/product/view.do?id=2ETFZZ"
    body = b"<html><body>official mobile product</body></html>"
    snapshot = OfficialSourceSnapshot(
        source=registry.authorize_url(url, KnowledgeCapability.PRODUCT_RISK_FACTORS),
        capability=KnowledgeCapability.PRODUCT_RISK_FACTORS,
        source_uri=url,
        title="official mobile product",
        content_type="text/html",
        content=body,
        content_sha256=hashlib.sha256(body).hexdigest(),
        published_at=None,
        as_of=date(2026, 8, 24),
    )

    linked = OfficialDocumentLinkResolver(registry).resolve(snapshot)

    assert [item.url for item in linked] == [
        "https://www.samsungfund.com/upload/invest/2ETFZZ-A.pdf"
    ]
