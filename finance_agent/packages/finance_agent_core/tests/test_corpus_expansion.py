from __future__ import annotations

from datetime import date

from finance_agent_core.config.knowledge_coverage import KnowledgeCapability
from finance_agent_core.config.pending_source_registry import (
    load_pending_intake_source_registry,
)
from finance_agent_core.config.source_registry import load_official_source_registry
from finance_agent_core.contracts.queryplan import ProductFamily
from finance_agent_core.retrieval.corpus_expansion import (
    CorpusExpansionCandidate,
    build_corpus_expansion_queries,
    discover_corpus_candidates,
    materialize_corpus_catalog,
    merge_pending_catalogs,
)
from finance_agent_core.retrieval.official_connector import (
    OfficialDiscoveryCandidate,
    OfficialHttpResponse,
    OfficialSourceConnector,
)
from finance_agent_core.retrieval.official_discovery import OfficialDiscoveryReport
from finance_agent_core.retrieval.pending_corpus import (
    PendingCorpusCatalog,
    PendingCorpusSource,
    collect_pending_corpus,
)
from finance_agent_core.storage.identity_cache import ProductIdentityRecord


def _record(
    family: ProductFamily,
    name: str,
    *,
    owner: str | None,
) -> ProductIdentityRecord:
    return ProductIdentityRecord(
        product_family=family.value,
        product_id=f"{family.value}:{name}",
        product_name=name,
        ticker=None,
        isin=None,
        short_name=None,
        source_owner=owner,
        public_offering=None,
        is_quarantined=False,
    )


def test_expansion_queries_use_registry_and_db_identity_without_entity_policy() -> None:
    registry = load_official_source_registry()
    records = {
        ProductFamily.DOMESTIC_ETP: (
            _record(ProductFamily.DOMESTIC_ETP, "KODEX 테스트 ETF", owner="삼성"),
            _record(ProductFamily.DOMESTIC_ETP, "다른 브랜드 ETF", owner="다른 운용사"),
        )
    }

    queries = build_corpus_expansion_queries(
        registry,
        records,
        max_products_per_source=10,
        max_queries=500,
    )

    samsung = [item for item in queries if item.source_registry_id.startswith("samsungfund-")]
    assert samsung
    assert any("KODEX 테스트 ETF" in item.query for item in samsung)
    assert all("다른 브랜드 ETF" not in item.query for item in samsung)
    assert {item.capability for item in samsung} == {
        KnowledgeCapability.PORTFOLIO_HOLDINGS,
        KnowledgeCapability.PRODUCT_RISK_FACTORS,
        KnowledgeCapability.SECTOR_THEME_HISTORY,
    }


def test_pending_intake_registry_cannot_change_production_source_authority() -> None:
    production = load_official_source_registry()
    intake = load_pending_intake_source_registry()

    assert {entry.status for entry in production.entries} == {"approved_for_live_evidence"}
    assert tuple(entry.source_id for entry in production.entries) == tuple(
        entry.source_id for entry in intake.entries if entry.status == "approved_for_live_evidence"
    )
    pending = tuple(
        entry for entry in intake.entries if entry.status == "approved_for_snapshot_intake"
    )
    assert len(pending) == 10
    assert {entry.product_families for entry in pending} == {(ProductFamily.OVERSEAS_ETP,)}
    assert intake.canonical_sha256 != production.canonical_sha256


def test_expansion_global_budget_is_round_robin_across_sources() -> None:
    registry = load_official_source_registry()

    queries = build_corpus_expansion_queries(
        registry,
        {},
        max_products_per_source=0,
        max_queries=len(registry.entries),
    )

    assert len({item.source_registry_id for item in queries}) == len(registry.entries)


def test_expansion_source_budget_is_round_robin_across_capabilities() -> None:
    registry = load_official_source_registry()
    queries = build_corpus_expansion_queries(
        registry,
        {},
        max_products_per_source=0,
        max_queries=500,
    )

    samsung = [item for item in queries if item.source_registry_id == "samsungfund-kodex-product"]
    assert {item.capability for item in samsung[:3]} == {
        KnowledgeCapability.PORTFOLIO_HOLDINGS,
        KnowledgeCapability.PRODUCT_RISK_FACTORS,
        KnowledgeCapability.SECTOR_THEME_HISTORY,
    }


class _Discovery:
    def __init__(self) -> None:
        self.calls = 0

    def discover_report(self, **_kwargs) -> OfficialDiscoveryReport:
        self.calls += 1
        return OfficialDiscoveryReport(
            candidates=(
                OfficialDiscoveryCandidate(
                    url="https://www.fsc.go.kr/po010103/86950",
                    title="공식 정책 자료",
                    published_at=date(2026, 5, 21),
                ),
            ),
            queried_source_registry_ids=("fsc-policy",),
            exhaustive=False,
        )


def test_discovery_deduplicates_url_and_merges_capabilities() -> None:
    registry = load_official_source_registry()
    planned = tuple(
        build_corpus_expansion_queries(
            registry,
            {},
            max_products_per_source=0,
            max_queries=500,
        )
    )
    fsc = tuple(item for item in planned if item.source_registry_id == "fsc-policy")
    provider = _Discovery()

    candidates, failures = discover_corpus_candidates(
        provider,  # type: ignore[arg-type]
        registry,
        fsc,
        request_interval_seconds=0,
    )

    assert failures == 0
    assert provider.calls == len(fsc)
    assert len(candidates) == 1
    assert candidates[0].capabilities == (KnowledgeCapability.FUND_STRUCTURE_STRATEGY_TREND,)


class _ManyCandidateDiscovery:
    def discover_report(self, **_kwargs) -> OfficialDiscoveryReport:
        return OfficialDiscoveryReport(
            candidates=tuple(
                OfficialDiscoveryCandidate(
                    url=f"https://www.fsc.go.kr/po010103/{86950 + index}",
                    title=f"공식 정책 자료 {index}",
                    published_at=date(2026, 5, 21),
                )
                for index in range(10)
            ),
            queried_source_registry_ids=("fsc-policy",),
            exhaustive=False,
        )


def test_discovery_enforces_per_query_candidate_budget() -> None:
    registry = load_official_source_registry()
    planned = tuple(
        item
        for item in build_corpus_expansion_queries(
            registry,
            {},
            max_products_per_source=0,
            max_queries=500,
        )
        if item.source_registry_id == "fsc-policy"
    )[:1]

    candidates, failures = discover_corpus_candidates(
        _ManyCandidateDiscovery(),  # type: ignore[arg-type]
        registry,
        planned,
        top_k=3,
        request_interval_seconds=0,
    )

    assert failures == 0
    assert len(candidates) == 3


class _OfficialTransport:
    def fetch(self, url: str, *, timeout_seconds: float, max_bytes: int) -> OfficialHttpResponse:
        assert timeout_seconds > 0
        body = (
            "<html><head><meta name='date' content='2026-05-21'></head>"
            "<body><h1>공식 펀드 구조</h1><p>2026-05-21 발표</p></body></html>"
        ).encode()
        assert len(body) <= max_bytes
        return OfficialHttpResponse(
            status_code=200,
            final_url=url,
            content_type="text/html",
            body=body,
        )


def test_materialized_catalog_keeps_pending_dated_official_documents_only() -> None:
    registry = load_official_source_registry()
    candidate = CorpusExpansionCandidate(
        source_registry_id="fsc-policy",
        url="https://www.fsc.go.kr/po010103/86950",
        title="공식 펀드 구조",
        published_at=date(2026, 5, 21),
        capabilities=(KnowledgeCapability.FUND_STRUCTURE_STRATEGY_TREND,),
    )

    catalog, stats = materialize_corpus_catalog(
        (candidate,),
        connector=OfficialSourceConnector(_OfficialTransport(), registry),
        registry=registry,
        fetch_interval_seconds=0,
    )

    assert catalog.evidence_cutoff == date(2026, 8, 24)
    assert len(catalog.documents) == 1
    assert catalog.documents[0].published_at == date(2026, 5, 21)
    assert catalog.documents[0].as_of == date(2026, 5, 21)
    assert catalog.documents[0].purposes == ("operation", "product_structure", "trend")
    assert stats.catalog_document_count == 1
    assert stats.fetch_failure_count == 0


def test_catalog_merge_prefers_curated_seed_metadata_and_unions_capabilities() -> None:
    registry = load_official_source_registry()
    url = "https://engage.kraneshares.com/s/b264ceb1/kstr-presentation/"
    generated = PendingCorpusCatalog(
        catalog_id="generated-catalog",
        evidence_cutoff=date(2026, 8, 24),
        documents=(
            PendingCorpusSource(
                document_id="generated-document",
                title="search title",
                source_uri=url,
                capabilities=(KnowledgeCapability.PORTFOLIO_HOLDINGS,),
                as_of=date(2026, 6, 30),
                language="en",
                purposes=("holdings",),
            ),
        ),
    )
    curated = PendingCorpusCatalog(
        catalog_id="curated-catalog",
        evidence_cutoff=date(2026, 8, 24),
        documents=(
            PendingCorpusSource(
                document_id="curated-kstr-presentation",
                title="KSTR official presentation",
                source_uri=url,
                capabilities=(KnowledgeCapability.PRODUCT_RISK_FACTORS,),
                published_at=date(2026, 6, 30),
                as_of=date(2026, 6, 30),
                language="en",
                purposes=("risk",),
            ),
        ),
    )

    merged = merge_pending_catalogs(registry, (generated, curated))

    assert len(merged.documents) == 1
    assert merged.documents[0].document_id == "curated-kstr-presentation"
    assert merged.documents[0].published_at == date(2026, 6, 30)
    assert merged.documents[0].capabilities == (
        KnowledgeCapability.PORTFOLIO_HOLDINGS,
        KnowledgeCapability.PRODUCT_RISK_FACTORS,
    )


class _SamePdfTransport:
    def fetch(self, url: str, *, timeout_seconds: float, max_bytes: int) -> OfficialHttpResponse:
        body = b"%PDF-1.4 same official bytes"
        return OfficialHttpResponse(
            status_code=200,
            final_url=url,
            content_type="application/pdf",
            body=body,
            metadata_as_of=date(2026, 6, 30),
        )


class _SamePdfExtractor:
    def extract(self, _content: bytes) -> str:
        return "same official text"


def test_pending_collection_deduplicates_identical_official_bytes(tmp_path) -> None:
    registry = load_official_source_registry()
    catalog = PendingCorpusCatalog(
        catalog_id="deduplication-catalog",
        evidence_cutoff=date(2026, 8, 24),
        documents=tuple(
            PendingCorpusSource(
                document_id=f"duplicate-document-{index}",
                title=f"duplicate {index}",
                source_uri=f"https://engage.kraneshares.com/s/{value}",
                capabilities=(capability,),
                as_of=date(2026, 6, 30),
                language="en",
                purposes=(purpose,),
            )
            for index, (value, capability, purpose) in enumerate(
                (
                    (
                        "11111111",
                        KnowledgeCapability.PORTFOLIO_HOLDINGS,
                        "holdings",
                    ),
                    (
                        "22222222",
                        KnowledgeCapability.PRODUCT_RISK_FACTORS,
                        "risk",
                    ),
                ),
                start=1,
            )
        ),
    )

    manifest = collect_pending_corpus(
        tmp_path / "pending",
        connector=OfficialSourceConnector(_SamePdfTransport(), registry),
        pdf_text_extractor=_SamePdfExtractor(),
        catalog=catalog,
    )

    assert manifest.document_count == 1
    assert manifest.requested_document_count == 2
    assert len(manifest.failures) == 1
    assert manifest.failures[0].reason_code == "duplicate_official_snapshot"
    assert manifest.documents[0].capabilities == (
        KnowledgeCapability.PORTFOLIO_HOLDINGS,
        KnowledgeCapability.PRODUCT_RISK_FACTORS,
    )
