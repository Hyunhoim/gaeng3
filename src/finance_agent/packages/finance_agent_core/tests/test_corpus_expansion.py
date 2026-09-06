from __future__ import annotations

import json
import os
from datetime import date

import pytest

from finance_agent_core.config.knowledge_coverage import KnowledgeCapability
from finance_agent_core.config.pending_source_registry import (
    load_pending_intake_source_registry,
)
from finance_agent_core.config.source_registry import load_official_source_registry
from finance_agent_core.contracts.queryplan import ProductFamily
from finance_agent_core.retrieval import pending_corpus as pending_corpus_module
from finance_agent_core.retrieval.corpus_coverage import (
    CorpusCoverageError,
    GapAction,
    GapCollectionItem,
    GapCollectionQueue,
    PendingBindingMethod,
    PendingDocumentReviewItem,
)
from finance_agent_core.retrieval.corpus_expansion import (
    CorpusExpansionCandidate,
    build_corpus_expansion_queries,
    build_gap_corpus_expansion_plan,
    build_gap_corpus_expansion_queries,
    discover_corpus_candidates,
    exclude_existing_corpus_candidates,
    materialize_corpus_catalog,
    merge_pending_catalogs,
    offline_snapshot_discovery_registry,
)
from finance_agent_core.retrieval.corpus_expansion_cli import (
    _returned_action_counts,
    _selected_targets_from_queries,
    _write_exclusive_bundle,
)
from finance_agent_core.retrieval.official_connector import (
    OfficialDiscoveryCandidate,
    OfficialFetchRequest,
    OfficialHttpResponse,
    OfficialSourceConnector,
    OfficialUrlExcludedError,
)
from finance_agent_core.retrieval.official_discovery import (
    OfficialDiscoveryReport,
    canonical_official_candidate_url,
)
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
    pending_source_ids = {entry.source_id for entry in pending}
    assert pending_source_ids == {
        "globalx-etf-static-official",
        "invesco-etf-official",
    }
    assert {entry.product_families for entry in pending} == {(ProductFamily.OVERSEAS_ETP,)}
    production_source_ids = {entry.source_id for entry in production.entries}
    assert pending_source_ids.isdisjoint(production_source_ids)
    assert {
        "first-trust-etf-official",
        "globalx-etf-official",
        "ishares-etf-official",
        "proshares-etf-official",
        "state-street-etf-official",
        "vaneck-etf-official",
        "vanguard-etf-official",
        "wisdomtree-etf-official",
    }.issubset(production_source_ids)
    assert "open-dart-disclosures" in production_source_ids
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


def _gap_queue(*items: GapCollectionItem) -> GapCollectionQueue:
    return GapCollectionQueue(
        max_gap_items=20,
        total_gap_count=len(items),
        returned_gap_count=len(items),
        truncated=False,
        action_counts={
            action.value: sum(item.action is action for item in items) for action in GapAction
        },
        document_review_items=(),
        items=items,
        network_collection_task_ids=tuple(
            item.task_id for item in items if item.action is GapAction.COLLECT_OFFICIAL_SNAPSHOT
        ),
    )


def _gap_item(
    *,
    owner: str = "Global X",
    query: str = '"Global X 우주항공 ETF" 구성종목 보유종목 포트폴리오',
    source_registry_ids: tuple[str, ...] = ("globalx-etf-static-official",),
) -> GapCollectionItem:
    return GapCollectionItem(
        task_id="gap-1111111111111111",
        product_family=ProductFamily.OVERSEAS_ETP,
        product_id="GX-SPACE",
        product_name="Global X 우주항공 ETF",
        source_owner=owner,
        capability=KnowledgeCapability.PORTFOLIO_HOLDINGS,
        action=GapAction.COLLECT_OFFICIAL_SNAPSHOT,
        reason_codes=("authorized_source_no_document",),
        source_registry_ids=source_registry_ids,
        queries=(query,),
    )


def _gap_record(
    product_id: str,
    product_name: str,
    *,
    owner: str = "Global X",
    family: ProductFamily = ProductFamily.OVERSEAS_ETP,
) -> ProductIdentityRecord:
    return ProductIdentityRecord(
        product_family=family.value,
        product_id=product_id,
        product_name=product_name,
        ticker=None,
        isin=None,
        short_name=None,
        public_offering=None,
        is_quarantined=False,
        source_owner=owner,
    )


def test_gap_expansion_uses_only_exact_db_bound_network_tasks() -> None:
    registry = load_pending_intake_source_registry()
    collect = _gap_item()
    review = GapCollectionItem(
        task_id="gap-2222222222222222",
        product_family=ProductFamily.OVERSEAS_ETP,
        product_id="GX-OLD",
        product_name="Global X 기존 ETF",
        source_owner="Global X",
        capability=KnowledgeCapability.PORTFOLIO_HOLDINGS,
        action=GapAction.REVIEW_EXISTING_DOCUMENT,
        reason_codes=("authority_not_active",),
        source_registry_ids=("globalx-etf-official",),
    )
    records = {
        ProductFamily.OVERSEAS_ETP: (
            _gap_record("GX-SPACE", "Global X 우주항공 ETF"),
            _gap_record("GX-OLD", "Global X 기존 ETF"),
        )
    }

    planned = build_gap_corpus_expansion_queries(
        _gap_queue(collect, review),
        registry,
        records,
    )

    assert len(planned) == 1
    assert planned[0].task_id == collect.task_id
    assert planned[0].product_id == "GX-SPACE"
    assert planned[0].query == collect.queries[0]


def test_gap_expansion_rejects_db_identity_or_query_drift() -> None:
    registry = load_pending_intake_source_registry()
    record = _gap_record("GX-SPACE", "Global X 우주항공 ETF")
    records = {ProductFamily.OVERSEAS_ETP: (record,)}

    with pytest.raises(CorpusCoverageError, match="identity differs"):
        build_gap_corpus_expansion_queries(
            _gap_queue(_gap_item(owner="다른 운용사")),
            registry,
            records,
        )
    with pytest.raises(CorpusCoverageError, match="exact quoted product name"):
        build_gap_corpus_expansion_queries(
            _gap_queue(_gap_item(query="우주항공 ETF 구성종목")),
            registry,
            records,
        )
    for query in (
        '-"Global X 우주항공 ETF" 구성종목 보유종목 포트폴리오',
        '"Global X 우주항공 ETF" 구성종목 보유종목 포트폴리오 OR "다른 ETF"',
        "＂Global X 우주항공 ETF＂ 구성종목 보유종목 포트폴리오",
    ):
        with pytest.raises(CorpusCoverageError, match="exact quoted product name"):
            build_gap_corpus_expansion_queries(
                _gap_queue(_gap_item(query=query)),
                registry,
                records,
            )


def test_gap_expansion_rejects_owner_or_unscoped_family_source() -> None:
    registry = load_pending_intake_source_registry()
    records = {ProductFamily.OVERSEAS_ETP: (_gap_record("GX-SPACE", "Global X 우주항공 ETF"),)}

    with pytest.raises(CorpusCoverageError, match="source owner differs"):
        build_gap_corpus_expansion_queries(
            _gap_queue(
                _gap_item(source_registry_ids=("invesco-etf-official",)),
            ),
            registry,
            records,
        )
    unscoped = GapCollectionItem(
        task_id="gap-3333333333333333",
        product_family=ProductFamily.OVERSEAS_ETP,
        product_id="GX-SPACE",
        product_name="Global X 우주항공 ETF",
        source_owner="Global X",
        capability=KnowledgeCapability.PRODUCT_RISK_FACTORS,
        action=GapAction.COLLECT_OFFICIAL_SNAPSHOT,
        reason_codes=("authorized_source_no_document",),
        source_registry_ids=("open-dart-disclosures",),
        queries=('"Global X 우주항공 ETF" 투자설명서 위험요인 상품 위험',),
    )
    with pytest.raises(CorpusCoverageError, match="family is outside"):
        build_gap_corpus_expansion_queries(_gap_queue(unscoped), registry, records)


def test_gap_expansion_accepts_registry_brand_root_for_legal_owner_name() -> None:
    registry = load_pending_intake_source_registry()
    product_name = "Quadratic Deflation ETF"
    owner = "Krane Funds Advisors LLC"
    item = GapCollectionItem(
        task_id="gap-4444444444444444",
        product_family=ProductFamily.OVERSEAS_ETP,
        product_id="AMX:BNDD.K",
        product_name=product_name,
        source_owner=owner,
        capability=KnowledgeCapability.PORTFOLIO_HOLDINGS,
        action=GapAction.COLLECT_OFFICIAL_SNAPSHOT,
        reason_codes=("authorized_source_no_document",),
        source_registry_ids=("kraneshares-main-product", "kraneshares-product"),
        queries=(f'"{product_name}" 구성종목 보유종목 포트폴리오',),
    )
    records = {ProductFamily.OVERSEAS_ETP: (_gap_record("AMX:BNDD.K", product_name, owner=owner),)}

    planned = build_gap_corpus_expansion_queries(_gap_queue(item), registry, records)

    assert {query.source_registry_id for query in planned} == {
        "kraneshares-main-product",
        "kraneshares-product",
    }


def test_gap_expansion_rejects_late_product_token_as_owner_alias() -> None:
    registry = load_pending_intake_source_registry()
    product_name = "삼성KODEX단기채권PLUS증권상장지수투자신탁[채권]"
    item = GapCollectionItem(
        task_id="gap-5555555555555555",
        product_family=ProductFamily.DOMESTIC_ETP,
        product_id="KR7214980005",
        product_name=product_name,
        source_owner=product_name,
        capability=KnowledgeCapability.PORTFOLIO_HOLDINGS,
        action=GapAction.COLLECT_OFFICIAL_SNAPSHOT,
        reason_codes=("authorized_source_no_document",),
        source_registry_ids=("plus-etf-official",),
        queries=(f'"{product_name}" 구성종목 보유종목 포트폴리오',),
    )
    records = {
        ProductFamily.DOMESTIC_ETP: (
            _gap_record(
                "KR7214980005",
                product_name,
                owner=product_name,
                family=ProductFamily.DOMESTIC_ETP,
            ),
        )
    }

    with pytest.raises(CorpusCoverageError, match="source owner differs"):
        build_gap_corpus_expansion_queries(_gap_queue(item), registry, records)


def test_gap_query_limit_reports_only_the_executed_source_query_pairs() -> None:
    registry = load_pending_intake_source_registry()
    records = {ProductFamily.OVERSEAS_ETP: (_gap_record("GX-SPACE", "Global X 우주항공 ETF"),)}
    plan = build_gap_corpus_expansion_plan(
        _gap_queue(
            _gap_item(
                source_registry_ids=(
                    "globalx-etf-factsheets",
                    "globalx-etf-official",
                )
            )
        ),
        registry,
        records,
        max_queries=1,
    )

    assert plan.total_query_count == 2
    assert plan.truncated is True
    assert len(plan.queries) == 1
    selected = _selected_targets_from_queries(plan.queries)
    assert selected[0]["source_registry_ids"] == ["globalx-etf-factsheets"]
    assert selected[0]["queries"] == [plan.queries[0].query]
    assert selected[0]["query_targets"] == [
        {
            "source_registry_id": "globalx-etf-factsheets",
            "query": plan.queries[0].query,
        }
    ]


def test_gap_returned_action_counts_include_all_queue_collections() -> None:
    collect = _gap_item()
    document_review = PendingDocumentReviewItem(
        task_id="gap-document-6666666666666666",
        document_id="pending-document",
        source_registry_id="globalx-etf-official",
        publisher="Global X",
        capabilities=(KnowledgeCapability.PORTFOLIO_HOLDINGS,),
        action=GapAction.REVIEW_EXISTING_DOCUMENT,
        binding_method=PendingBindingMethod.UNBOUND,
        reason_codes=("unbound_pending_document",),
    )
    manual_review = GapCollectionItem(
        task_id="gap-7777777777777777",
        product_family=ProductFamily.OVERSEAS_ETP,
        product_id="GX-MANUAL",
        product_name="Global X manual review ETF",
        source_owner="Global X",
        capability=KnowledgeCapability.PORTFOLIO_HOLDINGS,
        action=GapAction.MANUAL_ENTITY_REVIEW,
        reason_codes=("ambiguous_pending_document",),
    )
    queue = GapCollectionQueue(
        max_gap_items=3,
        total_gap_count=3,
        returned_gap_count=3,
        truncated=False,
        action_counts={
            GapAction.COLLECT_OFFICIAL_SNAPSHOT.value: 1,
            GapAction.REVIEW_EXISTING_DOCUMENT.value: 1,
            GapAction.MANUAL_ENTITY_REVIEW.value: 1,
        },
        document_review_items=(document_review,),
        items=(collect,),
        manual_review_items=(manual_review,),
        network_collection_task_ids=(collect.task_id,),
    )

    assert _returned_action_counts(queue) == queue.action_counts


def test_offline_snapshot_seam_and_existing_url_filter_are_collection_only() -> None:
    registry = load_pending_intake_source_registry()
    original = registry.require_source_id("globalx-etf-static-official")

    offline = offline_snapshot_discovery_registry(
        registry,
        source_registry_ids=("globalx-etf-static-official",),
    )

    assert original.status == "approved_for_snapshot_intake"
    assert registry.require_source_id(original.source_id).status == "approved_for_snapshot_intake"
    assert offline.require_source_id(original.source_id).status == "approved_for_live_evidence"
    invesco_status = offline.require_source_id("invesco-etf-official").status
    assert invesco_status == "approved_for_snapshot_intake"
    candidate = CorpusExpansionCandidate(
        source_registry_id="globalx-etf-static-official",
        url="https://www.globalxetfs.com/funds/example",
        title="example",
        published_at=date(2026, 8, 24),
        capabilities=(KnowledgeCapability.PORTFOLIO_HOLDINGS,),
    )
    kept, excluded = exclude_existing_corpus_candidates(
        (candidate,),
        existing_urls={f"{candidate.url}?utm_source=mail&utm_campaign=gap"},
    )
    assert kept == ()
    assert excluded == 1
    assert canonical_official_candidate_url("https://WWW.GLOBALXETFS.COM") == (
        "https://www.globalxetfs.com/"
    )


class _NavigationExistingUrlTransport:
    root_url = "https://www.globalxetfs.com/funds/example"
    existing_url = "https://www.globalxetfs.com/content/files/existing-holdings-20260801.pdf"
    new_url = "https://www.globalxetfs.com/content/files/new-holdings-20260801.pdf"

    def __init__(self) -> None:
        self.calls: list[str] = []

    def fetch(self, url: str, *, timeout_seconds: float, max_bytes: int) -> OfficialHttpResponse:
        self.calls.append(url)
        if url == self.root_url:
            body = (
                f'<a href="{self.existing_url}">existing holdings</a>'
                f'<a href="{self.new_url}">new holdings</a>'
            ).encode()
            return OfficialHttpResponse(
                status_code=200,
                final_url=url,
                content_type="text/html",
                body=body,
            )
        assert url == self.new_url
        body = b"%PDF-1.4 new official holdings"
        assert len(body) <= max_bytes
        assert timeout_seconds > 0
        return OfficialHttpResponse(
            status_code=200,
            final_url=url,
            content_type="application/pdf",
            body=body,
            metadata_as_of=date(2026, 8, 1),
        )


def test_navigation_resolution_does_not_refetch_a_normalized_existing_url() -> None:
    registry = load_official_source_registry()
    transport = _NavigationExistingUrlTransport()
    existing_keys = {canonical_official_candidate_url(f"{transport.existing_url}?utm_source=prior")}
    connector = OfficialSourceConnector(
        transport,
        registry,
        url_is_excluded=lambda url: canonical_official_candidate_url(url) in existing_keys,
    )
    candidate = CorpusExpansionCandidate(
        source_registry_id="globalx-etf-official",
        url=transport.root_url,
        title="Global X example",
        published_at=None,
        capabilities=(KnowledgeCapability.PORTFOLIO_HOLDINGS,),
    )

    catalog, stats = materialize_corpus_catalog(
        (candidate,),
        connector=connector,
        registry=registry,
        fetch_interval_seconds=0,
    )

    assert transport.calls == [transport.root_url, transport.new_url]
    assert [document.source_uri for document in catalog.documents] == [transport.new_url]
    assert stats.excluded_existing_url_count == 1
    assert stats.fetch_failure_count == 0


class _RedirectExistingUrlTransport:
    source_url = "https://www.tigeretf.com/upload/etf/legacy.pdf"
    existing_url = "https://investments.miraeasset.com/tigeretf/existing-holdings-20260801.pdf"

    def __init__(self) -> None:
        self.calls: list[str] = []

    def fetch(self, url: str, *, timeout_seconds: float, max_bytes: int) -> OfficialHttpResponse:
        self.calls.append(url)
        assert url == self.source_url
        return OfficialHttpResponse(
            status_code=302,
            final_url=self.existing_url,
            content_type="text/html",
            body=b"",
        )


def test_connector_blocks_an_existing_redirect_target_before_the_second_fetch() -> None:
    registry = load_official_source_registry()
    transport = _RedirectExistingUrlTransport()
    existing_key = canonical_official_candidate_url(transport.existing_url)
    connector = OfficialSourceConnector(
        transport,
        registry,
        url_is_excluded=lambda url: canonical_official_candidate_url(url) == existing_key,
    )
    candidate = OfficialDiscoveryCandidate(
        url=transport.source_url,
        title="legacy TIGER document",
    )

    with pytest.raises(OfficialUrlExcludedError):
        connector.fetch(
            candidate,
            OfficialFetchRequest(
                url=transport.source_url,
                capability=KnowledgeCapability.PRODUCT_RISK_FACTORS,
            ),
        )

    assert transport.calls == [transport.source_url]


class _OutOfScopeRedirectThenSuccessTransport:
    redirecting_url = "https://www.globalxetfs.com/funds/out-of-scope-redirect"
    outside_url = "https://example.com/unapproved-holdings.pdf"
    valid_url = "https://www.globalxetfs.com/content/files/good-holdings-20260801.pdf"

    def __init__(self) -> None:
        self.calls: list[str] = []

    def fetch(self, url: str, *, timeout_seconds: float, max_bytes: int) -> OfficialHttpResponse:
        self.calls.append(url)
        if url == self.redirecting_url:
            return OfficialHttpResponse(
                status_code=302,
                final_url=self.outside_url,
                content_type="text/html",
                body=b"",
            )
        assert url == self.valid_url
        return OfficialHttpResponse(
            status_code=200,
            final_url=url,
            content_type="application/pdf",
            body=b"%PDF-1.4 approved document",
            metadata_as_of=date(2026, 8, 1),
        )


def test_out_of_scope_redirect_is_counted_as_one_fetch_failure_and_collection_continues() -> None:
    registry = load_official_source_registry()
    transport = _OutOfScopeRedirectThenSuccessTransport()
    connector = OfficialSourceConnector(transport, registry)
    candidates = tuple(
        CorpusExpansionCandidate(
            source_registry_id="globalx-etf-official",
            url=url,
            title=title,
            published_at=None,
            capabilities=(KnowledgeCapability.PORTFOLIO_HOLDINGS,),
        )
        for url, title in (
            (transport.redirecting_url, "out-of-scope redirect"),
            (transport.valid_url, "valid official document"),
        )
    )

    catalog, stats = materialize_corpus_catalog(
        candidates,
        connector=connector,
        registry=registry,
        fetch_interval_seconds=0,
    )

    assert transport.calls == [transport.redirecting_url, transport.valid_url]
    assert [document.source_uri for document in catalog.documents] == [transport.valid_url]
    assert stats.fetch_failure_count == 1
    assert stats.catalog_document_count == 1


def test_output_bundle_rolls_back_if_only_one_file_can_be_published(
    tmp_path,
    monkeypatch,
) -> None:
    catalog_path = tmp_path / "catalog.json"
    report_path = tmp_path / "report.json"
    original_link = os.link

    def fail_report_link(source, target, *, follow_symlinks=True):
        if target == report_path:
            raise OSError("injected second-output failure")
        return original_link(source, target, follow_symlinks=follow_symlinks)

    monkeypatch.setattr(os, "link", fail_report_link)
    with pytest.raises(OSError, match="second-output failure"):
        _write_exclusive_bundle(
            (
                (catalog_path, b"complete catalog"),
                (report_path, b"complete report"),
            )
        )
    assert not catalog_path.exists()
    assert not report_path.exists()

    monkeypatch.setattr(os, "link", original_link)
    _write_exclusive_bundle(
        (
            (catalog_path, b"complete catalog"),
            (report_path, b"complete report"),
        )
    )
    assert catalog_path.read_bytes() == b"complete catalog"
    assert report_path.read_bytes() == b"complete report"


def test_output_bundle_rolls_back_on_base_exception(tmp_path, monkeypatch) -> None:
    catalog_path = tmp_path / "catalog.json"
    report_path = tmp_path / "report.json"
    original_link = os.link

    def interrupt_report_link(source, target, *, follow_symlinks=True):
        if target == report_path:
            raise KeyboardInterrupt
        return original_link(source, target, follow_symlinks=follow_symlinks)

    monkeypatch.setattr(os, "link", interrupt_report_link)
    with pytest.raises(KeyboardInterrupt):
        _write_exclusive_bundle(
            (
                (catalog_path, b"complete catalog"),
                (report_path, b"complete report"),
            )
        )

    assert not catalog_path.exists()
    assert not report_path.exists()


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


class _JsonThenMalformedTransport:
    malformed_url = "https://www.samsungfund.com/api/v1/kodex/product-pdf/malformed-json.do"
    valid_url = "https://www.samsungfund.com/api/v1/kodex/product-pdf/valid-json.do"
    valid_payload = {
        "holdings": (
            {
                "weight": 12.5,
                "name": "삼성전자",
            },
        ),
        "as_of": "2026-08-24",
    }
    valid_body = json.dumps(valid_payload, ensure_ascii=False).encode("utf-8")

    def fetch(self, url: str, *, timeout_seconds: float, max_bytes: int) -> OfficialHttpResponse:
        assert timeout_seconds > 0
        body = b'{"holdings": [' if url == self.malformed_url else self.valid_body
        assert len(body) <= max_bytes
        return OfficialHttpResponse(
            status_code=200,
            final_url=url,
            content_type="application/json ; charset=utf-8",
            body=body,
        )


def test_pending_collection_stages_canonical_json_and_isolates_malformed_json(tmp_path) -> None:
    registry = load_official_source_registry()
    transport = _JsonThenMalformedTransport()
    catalog = PendingCorpusCatalog(
        catalog_id="pending-json-catalog",
        evidence_cutoff=registry.evidence_cutoff,
        documents=(
            PendingCorpusSource(
                document_id="json-malformed-document",
                title="malformed official JSON",
                source_uri=transport.malformed_url,
                capabilities=(KnowledgeCapability.PORTFOLIO_HOLDINGS,),
                as_of=registry.evidence_cutoff,
                language="ko",
                purposes=("holdings",),
            ),
            PendingCorpusSource(
                document_id="json-valid-document",
                title="valid official JSON",
                source_uri=transport.valid_url,
                capabilities=(KnowledgeCapability.PORTFOLIO_HOLDINGS,),
                as_of=registry.evidence_cutoff,
                language="ko",
                purposes=("holdings",),
            ),
        ),
    )
    root = tmp_path / "pending-json"

    manifest = collect_pending_corpus(
        root,
        connector=OfficialSourceConnector(transport, registry),
        pdf_text_extractor=_SamePdfExtractor(),
        catalog=catalog,
    )

    assert manifest.document_count == 1
    assert manifest.requested_document_count == 2
    assert manifest.failures[0].document_id == "json-malformed-document"
    assert manifest.failures[0].stage == "text_extraction"
    assert manifest.failures[0].reason_code == "text_extraction_failed"
    snapshot = manifest.documents[0]
    assert snapshot.document_id == "json-valid-document"
    assert snapshot.raw_media_type == "application/json"
    assert snapshot.raw_relative_path == "raw/json-valid-document.json"
    assert (root / snapshot.raw_relative_path).read_bytes() == transport.valid_body
    assert (root / snapshot.text_relative_path).read_text(encoding="utf-8") == (
        json.dumps(
            transport.valid_payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    )
    assert not (root / "raw/json-malformed-document.json").exists()
    assert pending_corpus_module._extract_text(
        "application/json",
        b'{"second":2,"first":1}',
        _SamePdfExtractor(),
    ) == pending_corpus_module._extract_text(
        "application/json",
        b'{"first":1,"second":2}',
        _SamePdfExtractor(),
    )


def test_pending_json_text_extraction_rejects_empty_and_oversized_raw_bytes(monkeypatch) -> None:
    with pytest.raises(pending_corpus_module.PendingCorpusError, match="empty or oversized"):
        pending_corpus_module._extract_text("application/json", b"", _SamePdfExtractor())

    monkeypatch.setattr(pending_corpus_module, "_MAX_RAW_BYTES", 8)
    with pytest.raises(pending_corpus_module.PendingCorpusError, match="empty or oversized"):
        pending_corpus_module._extract_text(
            "application/json",
            b'{"value":1}',
            _SamePdfExtractor(),
        )

    monkeypatch.setattr(pending_corpus_module, "_MAX_RAW_BYTES", 1024)
    monkeypatch.setattr(pending_corpus_module, "_MAX_TEXT_BYTES", 8)
    with pytest.raises(pending_corpus_module.PendingCorpusError, match="invalid extracted text"):
        pending_corpus_module._extract_text(
            "application/json",
            b'{"value":1}',
            _SamePdfExtractor(),
        )
