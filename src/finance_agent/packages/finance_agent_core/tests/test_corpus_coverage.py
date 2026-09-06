from __future__ import annotations

import hashlib
from datetime import UTC, date, datetime
from types import SimpleNamespace

import pytest

from finance_agent_core.config.knowledge_coverage import (
    ApprovedKnowledgeCoverageManifest,
    KnowledgeCapability,
    KnowledgeCoverageEntry,
)
from finance_agent_core.config.source_registry import (
    OfficialSourceEntry,
    OfficialSourceRegistry,
    SourceAuthority,
    SourceConnectorKind,
)
from finance_agent_core.contracts.external_knowledge import KnowledgePredicate
from finance_agent_core.contracts.queryplan import ProductFamily
from finance_agent_core.retrieval import corpus_coverage as coverage_module
from finance_agent_core.retrieval import corpus_coverage_cli
from finance_agent_core.retrieval.corpus_coverage import (
    CoverageAuthority,
    DatasetCoverageReceipt,
    GapAction,
    PendingBindingMethod,
    RelationBundleInput,
    build_corpus_coverage_analysis,
    canonical_json_bytes,
    coverage_analysis_bytes,
    gap_collection_queue_bytes,
)
from finance_agent_core.retrieval.pending_corpus import (
    PendingCorpusManifest,
    PendingCorpusSnapshot,
)
from finance_agent_core.retrieval.temporal_relations import (
    ExternalEvidenceReference,
    TemporalEntity,
    TemporalEntityKind,
    TemporalRelationRecord,
    seal_temporal_relation_bundle,
    temporal_relation_bundle_bytes,
)
from finance_agent_core.storage.approval import DatasetApprovalError
from finance_agent_core.storage.identity_cache import ProductIdentityRecord

_CUTOFF = date(2026, 8, 24)
_CORPUS_SHA = "a" * 64
_INDEX_SHA = "b" * 64


def _registry() -> OfficialSourceRegistry:
    return OfficialSourceRegistry(
        schema_version="1.0",
        registry_id="coverage-test-registry",
        evidence_cutoff=_CUTOFF,
        entries=(
            OfficialSourceEntry(
                source_id="manager-official",
                publisher="운용사A",
                authority=SourceAuthority.ASSET_MANAGER,
                hostname="example.com",
                path_prefixes=("/docs",),
                connector_kind=SourceConnectorKind.HTML_OR_PDF,
                capabilities=(
                    KnowledgeCapability.CORPORATE_SUBSIDIARY,
                    KnowledgeCapability.PORTFOLIO_HOLDINGS,
                    KnowledgeCapability.PRODUCT_RISK_FACTORS,
                    KnowledgeCapability.SECTOR_THEME_HISTORY,
                ),
                selection_aliases=("운용사A",),
                product_families=(ProductFamily.DOMESTIC_ETP,),
                status="approved_for_snapshot_intake",
                publication_date_required=False,
            ),
        ),
    )


def _coverage() -> ApprovedKnowledgeCoverageManifest:
    enabled = {
        KnowledgeCapability.CORPORATE_SUBSIDIARY,
        KnowledgeCapability.PORTFOLIO_HOLDINGS,
    }
    return ApprovedKnowledgeCoverageManifest(
        schema_version="1.0",
        manifest_id="coverage-test-manifest",
        dataset_release_id="coverage-test-dataset",
        capabilities=tuple(
            KnowledgeCoverageEntry(
                capability=capability,
                status="approved" if capability in enabled else "not_configured",
                runtime_execution_enabled=capability in enabled,
                allowed_source_kinds=("provided", "external_approved"),
                corpus_sha256=_CORPUS_SHA if capability in enabled else None,
                index_sha256=_INDEX_SHA if capability in enabled else None,
            )
            for capability in KnowledgeCapability
        ),
    )


def _product(
    product_id: str,
    name: str,
    ticker: str,
    *,
    family: ProductFamily = ProductFamily.DOMESTIC_ETP,
    owner: str | None = "운용사A",
) -> ProductIdentityRecord:
    return ProductIdentityRecord(
        product_family=family.value,
        product_id=product_id,
        product_name=name,
        ticker=None if family is ProductFamily.FUND else ticker,
        isin=None if family is ProductFamily.FUND else product_id,
        short_name=name,
        public_offering=True if family is ProductFamily.FUND else None,
        is_quarantined=False,
        source_owner=owner,
    )


def _dataset(family: ProductFamily, count: int) -> DatasetCoverageReceipt:
    return DatasetCoverageReceipt(
        product_family=family,
        database_sha256="c" * 64,
        source_file_sha256="d" * 64,
        source_snapshot_date=_CUTOFF,
        product_count=count,
    )


def _snapshot(
    document_id: str,
    title: str,
    text: str,
    *,
    as_of: date = _CUTOFF,
) -> PendingCorpusSnapshot:
    raw = b"<html>official</html>"
    encoded = text.encode()
    return PendingCorpusSnapshot(
        document_id=document_id,
        title=title,
        publisher="운용사A",
        source_registry_id="manager-official",
        source_uri=f"https://example.com/docs/{document_id}",
        capabilities=(KnowledgeCapability.PORTFOLIO_HOLDINGS,),
        collected_at_utc=datetime(2026, 8, 25, tzinfo=UTC),
        published_at=as_of,
        as_of=as_of,
        language="ko",
        purposes=("holdings",),
        raw_relative_path=f"raw/{document_id}.html",
        raw_media_type="text/html",
        raw_size_bytes=len(raw),
        raw_sha256=hashlib.sha256(raw).hexdigest(),
        text_relative_path=f"text/{document_id}.txt",
        text_size_bytes=len(encoded),
        text_sha256=hashlib.sha256(encoded).hexdigest(),
    )


def _manifest(
    registry: OfficialSourceRegistry,
    documents: tuple[PendingCorpusSnapshot, ...],
    *,
    cutoff: date = _CUTOFF,
) -> PendingCorpusManifest:
    def file_hash(kind: str) -> str:
        payload = [
            {
                "document_id": item.document_id,
                "relative_path": (
                    item.raw_relative_path if kind == "raw" else item.text_relative_path
                ),
                "sha256": item.raw_sha256 if kind == "raw" else item.text_sha256,
                "size_bytes": item.raw_size_bytes if kind == "raw" else item.text_size_bytes,
            }
            for item in documents
        ]
        return hashlib.sha256(canonical_json_bytes(payload)).hexdigest()

    return PendingCorpusManifest(
        catalog_id="coverage-test-catalog",
        source_registry_sha256=registry.canonical_sha256,
        evidence_cutoff=cutoff,
        documents=documents,
        document_count=len(documents),
        requested_document_count=len(documents),
        raw_file_manifest_sha256=file_hash("raw"),
        text_file_manifest_sha256=file_hash("text"),
    )


def _relation(
    product_id: str | None,
    *,
    capability: KnowledgeCapability = KnowledgeCapability.PORTFOLIO_HOLDINGS,
    relation_id: str = "relation-holding",
) -> TemporalRelationRecord:
    evidence = ExternalEvidenceReference(
        evidence_id=f"evidence-{relation_id}",
        source_registry_id="manager-official",
        document_id=f"document-{relation_id}",
        title="공식 문서",
        publisher="운용사A",
        source_uri=f"https://example.com/docs/{relation_id}",
        published_at=_CUTOFF,
        as_of=_CUTOFF,
        content_sha256="e" * 64,
        locator="section 1",
        excerpt="공식 관계 근거",
    )
    predicate = (
        KnowledgePredicate.HOLDS_COMPANY
        if capability is KnowledgeCapability.PORTFOLIO_HOLDINGS
        else KnowledgePredicate.SUBSIDIARY_OF
    )
    return TemporalRelationRecord(
        relation_id=relation_id,
        capability=capability,
        predicate=predicate,
        subject=TemporalEntity(
            entity_id="product-alpha" if product_id else "company-parent",
            kind=TemporalEntityKind.PRODUCT if product_id else TemporalEntityKind.COMPANY,
            canonical_name="ALPHA ETF" if product_id else "모회사",
        ),
        object=TemporalEntity(
            entity_id="company-child",
            kind=TemporalEntityKind.COMPANY,
            canonical_name="자회사",
        ),
        product_family=ProductFamily.DOMESTIC_ETP if product_id else None,
        product_id=product_id,
        valid_from=_CUTOFF,
        observed_at=_CUTOFF,
        evidence=(evidence,),
    )


def _relation_input(*relations: TemporalRelationRecord) -> RelationBundleInput:
    bundle = seal_temporal_relation_bundle(
        tuple(relations), corpus_manifest_sha256=_CORPUS_SHA, registry=_registry()
    )
    payload = temporal_relation_bundle_bytes(bundle)
    return RelationBundleInput(
        source_id="test-relations",
        file_sha256=hashlib.sha256(payload).hexdigest(),
        bundle=bundle,
    )


def test_pending_bindings_are_candidates_and_gap_actions_do_not_grant_coverage() -> None:
    registry = _registry()
    products = (
        _product("KR7000000001", "ALPHA 반도체", "100001"),
        _product("KR7000000002", "ALPHA 반도체 플러스", "100002"),
        _product("KR7000000003", "BETA 우주항공", "100003"),
    )
    exact = _snapshot("pending-exact", "KR7000000001 공식 구성종목", "ALPHA 반도체")
    guide_text = "ALPHA 반도체\nALPHA 반도체 플러스\n두 상품 비교 가이드"
    guide = _snapshot("pending-guide", "두 상품 공식 가이드", guide_text)
    pending = _manifest(registry, (exact, guide))

    analysis, queue = build_corpus_coverage_analysis(
        {ProductFamily.DOMESTIC_ETP: products},
        datasets=(_dataset(ProductFamily.DOMESTIC_ETP, 3),),
        registry=registry,
        coverage=_coverage(),
        pending_manifest=pending,
        pending_texts={exact.document_id: "ALPHA 반도체", guide.document_id: guide_text},
        max_gap_items=20,
    )

    methods = {item.document_id: item.binding_method for item in analysis.bindings}
    assert methods["pending-exact"] is PendingBindingMethod.EXACT_PRODUCT_ID
    assert methods["pending-guide"] is PendingBindingMethod.AMBIGUOUS
    row = next(
        item
        for item in analysis.product_matrix
        if item.capability is KnowledgeCapability.PORTFOLIO_HOLDINGS
    )
    assert row.active_covered_products == 0
    assert row.pending_review_products == 1
    actions = {(item.product_id, item.action) for item in queue.items}
    manual_actions = {(item.product_id, item.action) for item in queue.manual_review_items}
    assert ("KR7000000001", GapAction.REVIEW_EXISTING_DOCUMENT) in actions
    assert ("KR7000000002", GapAction.MANUAL_ENTITY_REVIEW) in manual_actions
    collect = next(item for item in queue.items if item.product_id == "KR7000000003")
    assert collect.action is GapAction.COLLECT_OFFICIAL_SNAPSHOT
    assert collect.source_registry_ids == ("manager-official",)
    assert "BETA 우주항공" in collect.queries[0]
    assert queue.network_collection_task_ids == tuple(
        item.task_id for item in queue.items if item.action is GapAction.COLLECT_OFFICIAL_SNAPSHOT
    )


def test_only_current_verified_relation_counts_active_and_outside_db_is_warned() -> None:
    product = _product("KR7000000001", "ALPHA 반도체", "100001")
    relation_input = _relation_input(
        _relation("KR7000000001"),
        _relation("KR7999999999", relation_id="relation-outside"),
        _relation(
            None,
            capability=KnowledgeCapability.CORPORATE_SUBSIDIARY,
            relation_id="relation-entity",
        ),
    )
    analysis, _ = build_corpus_coverage_analysis(
        {ProductFamily.DOMESTIC_ETP: (product,)},
        datasets=(_dataset(ProductFamily.DOMESTIC_ETP, 1),),
        registry=_registry(),
        coverage=_coverage(),
        active_relations=(relation_input,),
    )

    row = next(
        item
        for item in analysis.product_matrix
        if item.capability is KnowledgeCapability.PORTFOLIO_HOLDINGS
    )
    assert row.active_covered_products == 1
    assert all(item.authority is CoverageAuthority.ACTIVE_VERIFIED for item in analysis.bindings)
    assert any(item.code == "relation_product_outside_provided_db" for item in analysis.warnings)
    assert {item.canonical_name for item in analysis.entity_matrix} == {"모회사", "자회사"}


def test_registry_mismatched_legacy_graph_is_candidate_never_active() -> None:
    product = _product("KR7000000001", "ALPHA 반도체", "100001")
    relation_input = _relation_input(_relation("KR7000000001"))
    mismatched = relation_input.bundle.model_copy(
        update={
            "manifest": relation_input.bundle.manifest.model_copy(
                update={"source_registry_sha256": "f" * 64}
            )
        }
    )
    legacy = relation_input.model_copy(update={"bundle": mismatched})
    analysis, queue = build_corpus_coverage_analysis(
        {ProductFamily.DOMESTIC_ETP: (product,)},
        datasets=(_dataset(ProductFamily.DOMESTIC_ETP, 1),),
        registry=_registry(),
        coverage=_coverage(),
        legacy_relations=(legacy,),
        max_gap_items=20,
    )

    row = next(
        item
        for item in analysis.product_matrix
        if item.capability is KnowledgeCapability.PORTFOLIO_HOLDINGS
    )
    assert row.active_covered_products == 0
    assert row.legacy_live_candidate_products == 1
    assert any(item.code == "legacy_registry_hash_mismatch" for item in analysis.warnings)
    gap = next(
        item
        for item in queue.items
        if item.product_id == product.product_id
        and item.capability is KnowledgeCapability.PORTFOLIO_HOLDINGS
    )
    assert gap.action is GapAction.REBUILD_OR_RESEAL_LEGACY_ARTIFACT


def test_cutoff_is_fail_closed_and_fund_owner_is_never_inferred() -> None:
    registry = _registry()
    product = _product("FUND-1", "새희망 공모펀드", "", family=ProductFamily.FUND)
    late = _snapshot(
        "pending-late",
        "FUND-1 공식 문서",
        "새희망 공모펀드",
        as_of=date(2026, 8, 25),
    )
    pending = _manifest(registry, (late,), cutoff=date(2026, 8, 25))
    analysis, queue = build_corpus_coverage_analysis(
        {ProductFamily.FUND: (product,)},
        datasets=(_dataset(ProductFamily.FUND, 1),),
        registry=registry,
        coverage=_coverage(),
        pending_manifest=pending,
        pending_texts={late.document_id: "새희망 공모펀드"},
        max_gap_items=2,
    )

    assert all(
        row.source_owner == "unknown_not_in_provided_schema" for row in analysis.product_matrix
    )
    assert analysis.bindings[0].binding_method is PendingBindingMethod.REJECTED_CUTOFF
    assert all(row.active_covered_products == 0 for row in analysis.product_matrix)
    assert queue.truncated
    assert all(item.action is GapAction.EXPAND_SOURCE_REGISTRY for item in queue.items)
    assert all("fund_manager_unavailable" in item.reason_codes for item in queue.items)
    assert queue.network_collection_task_ids == ()


def test_analysis_and_gap_outputs_are_deterministic() -> None:
    product = _product("KR7000000001", "ALPHA 반도체", "100001")
    arguments = dict(
        records_by_family={ProductFamily.DOMESTIC_ETP: (product,)},
        datasets=(_dataset(ProductFamily.DOMESTIC_ETP, 1),),
        registry=_registry(),
        coverage=_coverage(),
        max_gap_items=2,
    )
    first_analysis, first_queue = build_corpus_coverage_analysis(**arguments)
    second_analysis, second_queue = build_corpus_coverage_analysis(**arguments)

    assert coverage_analysis_bytes(first_analysis) == coverage_analysis_bytes(second_analysis)
    assert gap_collection_queue_bytes(first_queue) == gap_collection_queue_bytes(second_queue)
    assert first_queue.returned_gap_count == 2
    assert first_queue.truncated


def test_dataset_release_mismatch_is_explicit() -> None:
    product = _product("KR7000000001", "ALPHA 반도체", "100001")
    analysis, _ = build_corpus_coverage_analysis(
        {ProductFamily.DOMESTIC_ETP: (product,)},
        datasets=(_dataset(ProductFamily.DOMESTIC_ETP, 1),),
        registry=_registry(),
        coverage=_coverage(),
        dataset_release_id="coverage-test-dataset-v9",
        max_gap_items=1,
    )

    assert analysis.dataset_release_id == "coverage-test-dataset-v9"
    assert any(item.code == "coverage_dataset_release_mismatch" for item in analysis.warnings)


def test_pending_name_binding_requires_source_corroboration_and_cover_stays_manual() -> None:
    registry = _registry()
    generic = _product("KR7000000001", "USD ESG", "ESG")
    foreign = _product(
        "KR7000000002",
        "KRANESHARES FOREIGN ETF",
        "KSTR",
        owner="타운용사",
    )
    own = _product("KR7000000003", "ALPHA 반도체", "100003")
    generic_doc = _snapshot("generic", "USD ESG 시장 보고서", "일반 시장 자료")
    foreign_doc = _snapshot(
        "foreign",
        "KRANESHARES FOREIGN ETF 안내",
        "타사 상품 자료",
    )
    cover_doc = _snapshot("cover-only", "시장 안내", "ALPHA 반도체")
    entity_doc = _snapshot("entity", "기업 관계", "모회사와 자회사").model_copy(
        update={"capabilities": (KnowledgeCapability.CORPORATE_SUBSIDIARY,)}
    )
    documents = tuple(
        sorted(
            (generic_doc, foreign_doc, cover_doc, entity_doc),
            key=lambda item: item.document_id,
        )
    )
    analysis, queue = build_corpus_coverage_analysis(
        {ProductFamily.DOMESTIC_ETP: (generic, foreign, own)},
        datasets=(_dataset(ProductFamily.DOMESTIC_ETP, 3),),
        registry=registry,
        coverage=_coverage(),
        pending_manifest=_manifest(registry, documents),
        pending_texts={
            "generic": "일반 시장 자료",
            "foreign": "타사 상품 자료",
            "cover-only": "ALPHA 반도체",
            "entity": "모회사와 자회사",
        },
        max_gap_items=20,
    )

    methods = {item.document_id: item.binding_method for item in analysis.bindings}
    assert methods["generic"] is PendingBindingMethod.UNBOUND
    assert methods["foreign"] is PendingBindingMethod.UNBOUND
    assert methods["cover-only"] is PendingBindingMethod.AMBIGUOUS
    assert methods["entity"] is PendingBindingMethod.ENTITY_ONLY
    assert analysis.entity_matrix == ()
    assert [item.document_id for item in analysis.pending_entity_documents] == ["entity"]
    assert any(item.product_id == own.product_id for item in queue.manual_review_items)
    assert {item.document_id for item in queue.document_review_items} == {
        "entity",
        "foreign",
        "generic",
    }
    assert (
        next(item for item in queue.document_review_items if item.document_id == "entity").action
        is GapAction.MANUAL_ENTITY_REVIEW
    )


def test_gap_queue_reuses_assets_before_balanced_new_work() -> None:
    registry = _registry()
    domestic = (
        _product("KR7000000001", "ALPHA 1", "100001"),
        _product("KR7000000002", "ALPHA 2", "100002"),
        _product("KR7000000003", "ALPHA 3", "100003"),
    )
    exact = _snapshot("pending-exact", "KR7000000001 공식 자료", "ALPHA 1")
    pending = _manifest(registry, (exact,))
    legacy_input = _relation_input(_relation("KR7000000002"))
    legacy = legacy_input.model_copy(
        update={
            "bundle": legacy_input.bundle.model_copy(
                update={
                    "manifest": legacy_input.bundle.manifest.model_copy(
                        update={"source_registry_sha256": "f" * 64}
                    )
                }
            )
        }
    )
    records = {
        ProductFamily.BOND: (_product("BOND-1", "채권 1", "", family=ProductFamily.BOND),),
        ProductFamily.DOMESTIC_ETP: domestic,
        ProductFamily.OVERSEAS_ETP: (
            _product("US-1", "OVERSEAS 1 ETF", "OVER", family=ProductFamily.OVERSEAS_ETP),
        ),
        ProductFamily.FUND: (_product("FUND-1", "공모펀드 1", "", family=ProductFamily.FUND),),
    }
    analysis, queue = build_corpus_coverage_analysis(
        records,
        datasets=tuple(_dataset(family, len(items)) for family, items in records.items()),
        registry=registry,
        coverage=_coverage(),
        pending_manifest=pending,
        pending_texts={exact.document_id: "ALPHA 1"},
        legacy_relations=(legacy,),
        max_gap_items=6,
    )

    assert analysis.product_matrix
    assert {item.action for item in queue.items[:2]} == {
        GapAction.REVIEW_EXISTING_DOCUMENT,
        GapAction.REBUILD_OR_RESEAL_LEGACY_ARTIFACT,
    }
    assert {item.product_family for item in queue.items[2:6]} == set(ProductFamily)
    assert {item.action for item in queue.items[2:6]} == {
        GapAction.COLLECT_OFFICIAL_SNAPSHOT,
        GapAction.EXPAND_SOURCE_REGISTRY,
    }
    assert any(item.source_registry_ids == ("manager-official",) for item in queue.items[2:6])


def test_same_brand_name_across_product_families_requires_manual_review() -> None:
    entry = (
        _registry()
        .entries[0]
        .model_copy(
            update={
                "publisher": "미래에셋자산운용",
                "selection_aliases": ("TIGER", "미래에셋자산운용"),
                "product_families": (ProductFamily.DOMESTIC_ETP, ProductFamily.FUND),
            }
        )
    )
    registry = OfficialSourceRegistry(
        schema_version="1.0",
        registry_id="cross-family-registry",
        evidence_cutoff=_CUTOFF,
        entries=(entry,),
    )
    domestic = _product(
        "KR7000000001",
        "TIGER 성장",
        "100001",
        owner="미래에셋자산운용",
    )
    fund = _product(
        "FUND-1",
        "TIGER 성장",
        "",
        family=ProductFamily.FUND,
        owner=None,
    )
    document = _snapshot("same-name", "TIGER 성장 공식 자료", "상품 설명").model_copy(
        update={"publisher": "미래에셋자산운용"}
    )
    analysis, queue = build_corpus_coverage_analysis(
        {
            ProductFamily.DOMESTIC_ETP: (domestic,),
            ProductFamily.FUND: (fund,),
        },
        datasets=(
            _dataset(ProductFamily.DOMESTIC_ETP, 1),
            _dataset(ProductFamily.FUND, 1),
        ),
        registry=registry,
        coverage=_coverage(),
        pending_manifest=_manifest(registry, (document,)),
        pending_texts={document.document_id: "상품 설명"},
        max_gap_items=20,
    )

    binding = next(item for item in analysis.bindings if item.document_id == "same-name")
    assert binding.binding_method is PendingBindingMethod.AMBIGUOUS
    assert {item.product_family for item in binding.candidate_products} == {
        ProductFamily.DOMESTIC_ETP,
        ProductFamily.FUND,
    }
    assert {item.product_family for item in queue.manual_review_items} == {
        ProductFamily.DOMESTIC_ETP,
        ProductFamily.FUND,
    }


def test_output_writer_retries_short_writes(tmp_path, monkeypatch) -> None:
    target = tmp_path / "large.json"
    payload = b"0123456789" * 100
    real_write = coverage_module.os.write

    def short_write(descriptor: int, data: memoryview) -> int:
        return real_write(descriptor, data[:7])

    monkeypatch.setattr(coverage_module.os, "write", short_write)
    coverage_module._write_new_read_only(target, payload)

    assert target.read_bytes() == payload


def test_contaminated_owner_selects_one_prefix_brand_publisher_group() -> None:
    capabilities = (
        KnowledgeCapability.PORTFOLIO_HOLDINGS,
        KnowledgeCapability.PRODUCT_RISK_FACTORS,
        KnowledgeCapability.SECTOR_THEME_HISTORY,
    )
    plus = OfficialSourceEntry(
        source_id="plus-etf-official",
        publisher="한화자산운용",
        authority=SourceAuthority.ASSET_MANAGER,
        hostname="plus.example.com",
        path_prefixes=("/product",),
        connector_kind=SourceConnectorKind.HTML_OR_PDF,
        capabilities=capabilities,
        selection_aliases=("ARIRANG", "PLUS", "한화", "한화자산운용"),
        product_families=(ProductFamily.DOMESTIC_ETP,),
        status="approved_for_snapshot_intake",
        publication_date_required=False,
    )
    samsung = OfficialSourceEntry(
        source_id="samsung-kodex-official",
        publisher="삼성자산운용",
        authority=SourceAuthority.ASSET_MANAGER,
        hostname="samsung.example.com",
        path_prefixes=("/product",),
        connector_kind=SourceConnectorKind.HTML_OR_PDF,
        capabilities=capabilities,
        selection_aliases=("KODEX", "삼성자산운용"),
        product_families=(ProductFamily.DOMESTIC_ETP,),
        status="approved_for_snapshot_intake",
        publication_date_required=False,
    )
    registry = OfficialSourceRegistry(
        schema_version="1.0",
        registry_id="brand-prefix-registry",
        evidence_cutoff=_CUTOFF,
        entries=(plus, samsung),
    )
    names = (
        "삼성KODEX단기채권PLUS증권상장지수투자신탁[채권]",
        "삼성KODEX미국S&P500채권혼합PLUS증권상장지수투자신탁",
    )
    products = tuple(
        _product(product_id, name, ticker, owner=name)
        for product_id, name, ticker in zip(
            ("KR7117460006", "KR7214980005"),
            names,
            ("A117460", "A214980"),
            strict=True,
        )
    )
    _, queue = build_corpus_coverage_analysis(
        {ProductFamily.DOMESTIC_ETP: products},
        datasets=(_dataset(ProductFamily.DOMESTIC_ETP, 2),),
        registry=registry,
        coverage=_coverage(),
        max_gap_items=20,
    )

    assert queue.items
    assert all(item.source_registry_ids == ("samsung-kodex-official",) for item in queue.items)
    assert all("plus-etf-official" not in item.source_registry_ids for item in queue.items)


def test_krane_owner_alias_and_publisher_guarded_market_suffix_ticker() -> None:
    entry = OfficialSourceEntry(
        source_id="kraneshares-product",
        publisher="KraneShares",
        authority=SourceAuthority.PRODUCT_ISSUER,
        hostname="krane.example.com",
        path_prefixes=("/product",),
        connector_kind=SourceConnectorKind.HTML_OR_PDF,
        capabilities=(KnowledgeCapability.PORTFOLIO_HOLDINGS,),
        selection_aliases=("KraneShares", "KSTR"),
        product_families=(ProductFamily.OVERSEAS_ETP,),
        status="approved_for_snapshot_intake",
        publication_date_required=False,
    )
    registry = OfficialSourceRegistry(
        schema_version="1.0",
        registry_id="krane-owner-registry",
        evidence_cutoff=_CUTOFF,
        entries=(entry,),
    )
    product = _product(
        "KSTR.K",
        "KraneShares SSE STAR Market 50 Index ETF",
        "KSTR.K",
        family=ProductFamily.OVERSEAS_ETP,
        owner="Krane Funds Advisors LLC",
    )
    document = _snapshot("kstr", "KSTR holdings", "official holdings").model_copy(
        update={
            "publisher": "KraneShares",
            "source_registry_id": "kraneshares-product",
            "source_uri": "https://krane.example.com/product/kstr",
        }
    )
    analysis, queue = build_corpus_coverage_analysis(
        {ProductFamily.OVERSEAS_ETP: (product,)},
        datasets=(_dataset(ProductFamily.OVERSEAS_ETP, 1),),
        registry=registry,
        coverage=_coverage(),
        pending_manifest=_manifest(registry, (document,)),
        pending_texts={document.document_id: "official holdings"},
        max_gap_items=10,
    )

    binding = next(item for item in analysis.bindings if item.document_id == "kstr")
    assert binding.binding_method is PendingBindingMethod.UNIQUE_TICKER
    assert binding.candidate_products[0].product_id == "KSTR.K"
    assert any(item.source_registry_ids == ("kraneshares-product",) for item in queue.items)


def test_cli_checks_database_approval_before_loading_identities(monkeypatch) -> None:
    arguments = SimpleNamespace(
        bond_db="bond.db",
        domestic_etp_db="domestic.db",
        overseas_etp_db="overseas.db",
        fund_db="fund.db",
        source_registry=None,
        coverage_manifest=None,
        pending_manifest=None,
        pending_root=None,
        active_relation=[],
        legacy_relation=[],
        max_gap_items=1,
        output_dir="output",
    )
    monkeypatch.setattr(
        corpus_coverage_cli,
        "_parser",
        lambda: SimpleNamespace(parse_args=lambda: arguments),
    )
    monkeypatch.setattr(corpus_coverage_cli, "load_official_source_registry", _registry)
    monkeypatch.setattr(corpus_coverage_cli, "load_approved_knowledge_coverage_manifest", _coverage)
    monkeypatch.setattr(
        corpus_coverage_cli,
        "load_approved_dataset_manifest",
        lambda: SimpleNamespace(release_id="coverage-test-dataset"),
    )
    monkeypatch.setattr(
        corpus_coverage_cli,
        "require_approved_database_paths",
        lambda *args, **kwargs: (_ for _ in ()).throw(DatasetApprovalError("not approved")),
    )
    monkeypatch.setattr(
        corpus_coverage_cli,
        "load_product_identities",
        lambda path: pytest.fail("identity load ran before database approval"),
    )

    with pytest.raises(DatasetApprovalError, match="not approved"):
        corpus_coverage_cli.main()
