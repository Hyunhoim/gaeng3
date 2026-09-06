from __future__ import annotations

import hashlib
from datetime import date

from finance_agent_core.agent.external_search_planning import (
    resolve_explicit_candidate_ids,
)
from finance_agent_core.agent.intent_analysis import (
    IntentAnalysisDraft,
    sanitize_server_authorized_knowledge_draft,
)
from finance_agent_core.agent.knowledge_planning import CompositeKnowledgePlanCompiler
from finance_agent_core.config.knowledge_coverage import KnowledgeCapability
from finance_agent_core.config.source_registry import (
    OfficialSourceRegistryError,
    load_official_source_registry,
)
from finance_agent_core.contracts.external_knowledge import (
    KnowledgeIntentRequirement,
    KnowledgeRequirement,
    ProvidedProductCandidate,
    ProvidedProductCandidateUniverse,
)
from finance_agent_core.contracts.queryplan import ProductFamily
from finance_agent_core.contracts.routing import InteractionIntent
from finance_agent_core.retrieval.official_acquisition import (
    _expand_globalx_product_roots,
)
from finance_agent_core.retrieval.official_connector import (
    OfficialConnectorError,
    OfficialDiscoveryCandidate,
    OfficialDocumentLinkResolver,
    OfficialFetchRequest,
    OfficialHttpResponse,
    OfficialNavigationSnapshot,
    OfficialSourceConnector,
    OfficialSourceSnapshot,
)
from finance_agent_core.retrieval.official_live_evidence import (
    VerifiedLiveRelationExtractor,
)
from finance_agent_core.storage.identity_cache import ProductIdentityRecord

_AS_OF = date(2026, 8, 24)
_SHA_A = "a" * 64
_SHA_B = "b" * 64


def test_tiger_legacy_pdf_scope_is_minimal_and_fail_closed() -> None:
    registry = load_official_source_registry()
    approved = "https://www.tigeretf.com/upload/etf/20250708095910009803.pdf"

    assert (
        registry.authorize_url(approved, KnowledgeCapability.PORTFOLIO_HOLDINGS).source_id
        == "miraeasset-tiger-legacy-pdf"
    )
    assert (
        registry.authorize_url(approved, KnowledgeCapability.PRODUCT_RISK_FACTORS).source_id
        == "miraeasset-tiger-legacy-pdf"
    )

    for rejected_url in (
        "https://www.tigeretf.com/notice/official.html",
        "https://tigeretf.com/upload/etf/20250708095910009803.pdf",
        "https://www.tigeretf.com.evil.example/upload/etf/report.pdf",
        "https://www.tigeretf.com/upload/fund/report.pdf",
    ):
        try:
            registry.authorize_url(
                rejected_url,
                KnowledgeCapability.PORTFOLIO_HOLDINGS,
            )
        except OfficialSourceRegistryError:
            pass
        else:  # pragma: no cover - makes a policy widening immediately visible
            raise AssertionError(f"out-of-scope TIGER URL was authorized: {rejected_url}")

    try:
        registry.authorize_url(approved, KnowledgeCapability.SECTOR_THEME_HISTORY)
    except OfficialSourceRegistryError:
        pass
    else:  # pragma: no cover
        raise AssertionError("legacy TIGER PDF scope gained an unapproved capability")


class _LegacyTigerRedirectTransport:
    def __init__(self, *, target_host: str = "investments.miraeasset.com") -> None:
        self.calls: list[str] = []
        self.target_host = target_host

    def fetch(self, url: str, *, timeout_seconds: float, max_bytes: int):
        del timeout_seconds, max_bytes
        self.calls.append(url)
        suffix = "/tigeretf/upload/etf/20250708095910009803.pdf"
        if url.startswith("https://www.tigeretf.com/"):
            return OfficialHttpResponse(
                status_code=301,
                final_url=f"http://{self.target_host}{suffix}",
                content_type="text/html",
                body=b"",
            )
        return OfficialHttpResponse(
            status_code=200,
            final_url=url,
            content_type="application/pdf",
            body=b"%PDF-1.7 synthetic official fixture",
        )


def test_tiger_legacy_redirect_is_upgraded_before_fetch_and_reauthorized() -> None:
    original = "https://www.tigeretf.com/upload/etf/20250708095910009803.pdf"
    canonical = (
        "https://investments.miraeasset.com/tigeretf/upload/etf/"
        "20250708095910009803.pdf"
    )
    transport = _LegacyTigerRedirectTransport()
    connector = OfficialSourceConnector(transport, load_official_source_registry())

    snapshot = connector.fetch(
        OfficialDiscoveryCandidate(url=original, title="TIGER 공식 PDF"),
        OfficialFetchRequest(
            url=original,
            capability=KnowledgeCapability.PORTFOLIO_HOLDINGS,
            as_of=_AS_OF,
        ),
    )

    assert transport.calls == [original, canonical]
    assert snapshot.source_uri == canonical
    assert snapshot.source.source_id == "miraeasset-investments"


def test_tiger_legacy_redirect_never_fetches_an_unapproved_target() -> None:
    original = "https://www.tigeretf.com/upload/etf/20250708095910009803.pdf"
    transport = _LegacyTigerRedirectTransport(target_host="evil.example")
    connector = OfficialSourceConnector(transport, load_official_source_registry())

    try:
        connector.fetch(
            OfficialDiscoveryCandidate(url=original, title="TIGER 공식 PDF"),
            OfficialFetchRequest(
                url=original,
                capability=KnowledgeCapability.PORTFOLIO_HOLDINGS,
                as_of=_AS_OF,
            ),
        )
    except (OfficialSourceRegistryError, OfficialConnectorError):
        # 2026-09-06: connector가 Registry 범위 밖 URL을 자체 오류로 감싸 거부한다
        pass
    else:  # pragma: no cover
        raise AssertionError("unapproved redirect target was fetched")

    assert transport.calls == [original]


def test_globalx_factsheet_opens_only_the_exact_db_ticker_product_root() -> None:
    registry = load_official_source_registry()
    factsheet = OfficialDiscoveryCandidate(
        url="https://assets.globalxetfs.com/funds/documents/bug/Fact-Sheet_BUG.pdf",
        title="Global X Cybersecurity ETF Fact Sheet",
    )

    expanded = _expand_globalx_product_roots(
        (factsheet,),
        provided_product_surfaces=("BUG.O", "Global X Cybersecurity ETF"),
        requested_source_ids=frozenset(
            {"globalx-etf-factsheets", "globalx-etf-official"}
        ),
        capability=KnowledgeCapability.PORTFOLIO_HOLDINGS,
        registry=registry,
    )

    assert [(item.url, item.db_ticker) for item in expanded] == [
        ("https://www.globalxetfs.com/funds/bug", "BUG"),
        (factsheet.url, "BUG"),
    ]


def test_globalx_product_root_requires_db_and_registry_provenance() -> None:
    registry = load_official_source_registry()
    factsheet = OfficialDiscoveryCandidate(
        url="https://assets.globalxetfs.com/funds/documents/bug/Fact-Sheet_BUG.pdf",
        title="Global X Cybersecurity ETF Fact Sheet",
    )

    wrong_db = _expand_globalx_product_roots(
        (factsheet,),
        provided_product_surfaces=("CLOU.O",),
        requested_source_ids=frozenset(
            {"globalx-etf-factsheets", "globalx-etf-official"}
        ),
        capability=KnowledgeCapability.PORTFOLIO_HOLDINGS,
        registry=registry,
    )
    missing_target_scope = _expand_globalx_product_roots(
        (factsheet,),
        provided_product_surfaces=("BUG.O",),
        requested_source_ids=frozenset({"globalx-etf-factsheets"}),
        capability=KnowledgeCapability.PORTFOLIO_HOLDINGS,
        registry=registry,
    )
    non_factsheet = OfficialDiscoveryCandidate(
        url="https://assets.globalxetfs.com/funds/holdings/bug_full-holdings_20260901.csv",
        title="BUG holdings",
    )
    wrong_document = _expand_globalx_product_roots(
        (non_factsheet,),
        provided_product_surfaces=("BUG.O",),
        requested_source_ids=frozenset(
            {"globalx-etf-factsheets", "globalx-etf-official"}
        ),
        capability=KnowledgeCapability.PORTFOLIO_HOLDINGS,
        registry=registry,
    )

    assert wrong_db == (factsheet,)
    assert missing_target_scope == (factsheet.model_copy(update={"db_ticker": "BUG"}),)
    assert wrong_document == (non_factsheet,)


def _globalx_navigation_snapshot(
    *,
    ticker: str | None,
    exposed_ticker: str = "bug",
) -> OfficialNavigationSnapshot:
    registry = load_official_source_registry()
    url = "https://www.globalxetfs.com/funds/bug"
    body = (
        '<html><a href="https://assets.globalxetfs.com/funds/holdings/'
        f'{exposed_ticker}_full-holdings_20260901.csv">Full holdings</a></html>'
    ).encode()
    return OfficialNavigationSnapshot(
        source=registry.authorize_url(url, KnowledgeCapability.PORTFOLIO_HOLDINGS),
        capability=KnowledgeCapability.PORTFOLIO_HOLDINGS,
        source_uri=url,
        title="BUG official product",
        content_type="text/html",
        content=body,
        content_sha256=hashlib.sha256(body).hexdigest(),
        db_ticker=ticker,
    )


def test_globalx_product_page_rewrites_only_exposed_same_ticker_holdings_date() -> None:
    registry = load_official_source_registry()
    snapshot = _globalx_navigation_snapshot(ticker="BUG")

    linked = OfficialDocumentLinkResolver(registry).resolve(
        snapshot,
        evidence_as_of=date(2026, 3, 31),
    )

    assert [(item.url, item.as_of, item.db_ticker) for item in linked] == [
        (
            "https://assets.globalxetfs.com/funds/holdings/"
            "bug_full-holdings_20260331.csv",
            date(2026, 3, 31),
            "BUG",
        )
    ]


def test_globalx_historical_holdings_rewrite_fails_closed_without_same_ticker() -> None:
    registry = load_official_source_registry()
    requested = "bug_full-holdings_20260331.csv"

    no_db_ticker = OfficialDocumentLinkResolver(registry).resolve(
        _globalx_navigation_snapshot(ticker=None),
        evidence_as_of=date(2026, 3, 31),
    )
    wrong_link_ticker = OfficialDocumentLinkResolver(registry).resolve(
        _globalx_navigation_snapshot(ticker="BUG", exposed_ticker="clou"),
        evidence_as_of=date(2026, 3, 31),
    )

    assert all(requested not in item.url for item in no_db_ticker)
    assert all(requested not in item.url for item in wrong_link_ticker)
    assert all(item.db_ticker is None for item in (*no_db_ticker, *wrong_link_ticker))


def _draft(*families: ProductFamily) -> IntentAnalysisDraft:
    return IntentAnalysisDraft(
        intent=InteractionIntent.SEARCH,
        product_families=list(families),
        evidence_spans=[],
        needs_clarification=False,
        knowledge_requirements=[],
    )


def _risk_requirement(question: str, subject: str):
    draft = IntentAnalysisDraft(
        intent=InteractionIntent.DETAIL,
        product_families=[ProductFamily.DOMESTIC_ETP],
        evidence_spans=[],
        needs_clarification=False,
        knowledge_requirements=[
            KnowledgeIntentRequirement(
                capability=KnowledgeCapability.PRODUCT_RISK_FACTORS,
                subject_span=subject,
                relation_span="위험요인",
            )
        ],
    )
    return (
        CompositeKnowledgePlanCompiler().compile(question, "risk-hardening", draft).requirements[0]
    )


def _risk_snapshot(body: str) -> OfficialSourceSnapshot:
    encoded = body.encode()
    url = "https://www.samsungfund.com/etf/product/view.do?id=GENERIC1"
    registry = load_official_source_registry()
    return OfficialSourceSnapshot(
        source=registry.authorize_url(url, KnowledgeCapability.PRODUCT_RISK_FACTORS),
        capability=KnowledgeCapability.PRODUCT_RISK_FACTORS,
        source_uri=url,
        title="공식 투자위험 설명",
        content_type="text/html",
        content=encoded,
        content_sha256=hashlib.sha256(encoded).hexdigest(),
        published_at=None,
        as_of=_AS_OF,
    )


def test_korean_or_list_and_composition_phrase_extract_literal_holding_entities() -> None:
    questions = {
        "Apple이나 Microsoft 중 하나 이상을 담은 해외 ETF를 찾아줘.": {
            "Apple",
            "Microsoft",
        },
        "NVIDIA나 AMD 중 하나라도 보유한 해외 ETF를 보여줘.": {"NVIDIA", "AMD"},
        "TSMC가 구성종목에 들어 있는 해외 ETF를 찾아줘.": {"TSMC"},
    }

    for question, expected_subjects in questions.items():
        sanitized = sanitize_server_authorized_knowledge_draft(
            question,
            _draft(ProductFamily.OVERSEAS_ETP),
        )
        subjects = {
            item.subject_span
            for item in sanitized.knowledge_requirements
            if item.capability is KnowledgeCapability.PORTFOLIO_HOLDINGS
        }
        assert subjects == expected_subjects


def test_holding_put_phrase_survives_sanitizer_and_plan_validation() -> None:
    question = "Crowdstrike Ho-a, Trend Micro Inc 두 종목을 모두 넣은 ETF를 찾아줘."
    sanitized = sanitize_server_authorized_knowledge_draft(
        question,
        _draft(ProductFamily.OVERSEAS_ETP),
    )

    plan = CompositeKnowledgePlanCompiler().compile(
        question,
        "holding-put-phrase",
        sanitized,
    )

    holding_subjects = {
        item.subject
        for item in plan.requirements
        if item.capability is KnowledgeCapability.PORTFOLIO_HOLDINGS
    }
    assert holding_subjects == {"Crowdstrike Ho-a", "Trend Micro Inc"}


def test_dated_holding_table_boundary_extracts_only_requested_entities() -> None:
    questions = (
        "TIGER 반도체TOP10(2025-06-30)의 상위 10개 편입표에 "
        "동진쎄미켐, Cameco 두 종목을 모두 담은 국내 ETF가 있는지 알려줘",
        "두 상품의 상위 10개 종목을 기준으로 LS, 제룡전기 두 종목을 "
        "모두 편입한 ETF를 찾아줘",
        # ``Solutions`` contains the ETF brand surface ``SOL``.  The brand
        # guard must not erase a legitimate company from an AND holding list.
        "FINX, SNSR 두 상품의 공식 편입표에서 Paypal Holdings Inc, "
        "Skyworks Solutions Inc 두 종목을 모두 담은 해외 ETF를 찾아줘",
        "FINX, HERO 두 상품의 공식 편입표에서 Global Payments Inc, "
        "Block Inc 두 종목을 모두 함께 보유한 해외 ETF를 찾아줘",
    )

    for question in questions:
        sanitized = sanitize_server_authorized_knowledge_draft(
            question,
            _draft(ProductFamily.DOMESTIC_ETP),
        )
        subjects = {
            item.subject_span
            for item in sanitized.knowledge_requirements
            if item.capability is KnowledgeCapability.PORTFOLIO_HOLDINGS
        }
        assert len(subjects) == 2


def test_company_name_containing_product_brand_keeps_all_holding_semantics() -> None:
    question = (
        "FINX, SNSR 두 상품의 공식 편입표에서 Paypal Holdings Inc, "
        "Skyworks Solutions Inc 두 종목을 모두 담은 해외 ETF를 찾아줘"
    )
    sanitized = sanitize_server_authorized_knowledge_draft(
        question,
        _draft(ProductFamily.OVERSEAS_ETP),
    )

    plan = CompositeKnowledgePlanCompiler().compile(
        question,
        "holding-product-brand-substring",
        sanitized,
    )

    assert {
        item.subject
        for item in plan.requirements
        if item.capability is KnowledgeCapability.PORTFOLIO_HOLDINGS
    } == {"Paypal Holdings Inc", "Skyworks Solutions Inc"}
    assert plan.holding_match_mode.value == "all"


def test_entered_holding_phrase_passes_compiler_capability_check() -> None:
    question = "Oklo 또는 Antofagasta Plc 중 하나를 들어간 ETF를 찾아줘"
    sanitized = sanitize_server_authorized_knowledge_draft(
        question,
        _draft(ProductFamily.OVERSEAS_ETP),
    )

    plan = CompositeKnowledgePlanCompiler().compile(
        question,
        "holding-entered-phrase",
        sanitized,
    )

    assert {
        item.subject
        for item in plan.requirements
        if item.capability is KnowledgeCapability.PORTFOLIO_HOLDINGS
    } == {"Oklo", "Antofagasta Plc"}


def test_holding_grammar_without_a_literal_entity_stays_fail_closed() -> None:
    cases = (
        (
            "중 하나 이상을 담은 해외 ETF를 찾아줘.",
            KnowledgeIntentRequirement(
                capability=KnowledgeCapability.PORTFOLIO_HOLDINGS,
                subject_span="이상",
                relation_span="담은",
            ),
        ),
        (
            "구성종목에 들어 있는 해외 ETF를 찾아줘.",
            KnowledgeIntentRequirement(
                capability=KnowledgeCapability.PORTFOLIO_HOLDINGS,
                subject_span="구성종목에",
                relation_span="들어 있는",
            ),
        ),
    )
    for question, proposed_requirement in cases:
        draft = _draft(ProductFamily.OVERSEAS_ETP).model_copy(
            update={"knowledge_requirements": [proposed_requirement]}
        )
        sanitized = sanitize_server_authorized_knowledge_draft(
            question,
            draft,
        )
        assert not any(
            item.capability is KnowledgeCapability.PORTFOLIO_HOLDINGS
            for item in sanitized.knowledge_requirements
        )


def test_exact_full_product_name_risk_does_not_open_embedded_theme_search() -> None:
    product_name = "브랜드 글로벌AI테마증권상장지수투자신탁(주식)"
    question = f"{product_name}의 위험요인을 공식 문서로 알려줘."

    draft = _draft(ProductFamily.DOMESTIC_ETP).model_copy(
        update={
            "knowledge_requirements": [
                KnowledgeIntentRequirement(
                    capability=KnowledgeCapability.SECTOR_THEME_HISTORY,
                    subject_span="글로벌AI테마",
                    relation_span="글로벌AI테마",
                )
            ]
        }
    )
    sanitized = sanitize_server_authorized_knowledge_draft(
        question,
        draft,
        product_mentions=(product_name,),
    )

    assert [item.capability for item in sanitized.knowledge_requirements] == [
        KnowledgeCapability.PRODUCT_RISK_FACTORS
    ]
    assert sanitized.knowledge_requirements[0].subject_span == product_name


def test_programme_core_binds_only_fund_sale_classes_not_lookalike_etps() -> None:
    sale_classes = (
        ProductIdentityRecord(
            product_family="fund",
            product_id="FUND-A",
            product_name="운용사새봄혁신혼합자산투자신탁 종류A",
            ticker=None,
            isin=None,
            short_name="운용사새봄혁신혼합자산A",
            public_offering=True,
            is_quarantined=False,
        ),
        ProductIdentityRecord(
            product_family="fund",
            product_id="FUND-AE",
            product_name="운용사새봄혁신혼합자산투자신탁 종류A-e",
            ticker=None,
            isin=None,
            short_name="운용사새봄혁신혼합자산Ae",
            public_offering=True,
            is_quarantined=False,
        ),
    )
    lookalike_etp = ProductIdentityRecord(
        product_family="domestic_etp",
        product_id="ETF-LOOKALIKE",
        product_name="새봄혁신테마 ETF",
        ticker="000001",
        isin="KR7000000001",
        short_name="새봄혁신테마",
        public_offering=None,
        is_quarantined=False,
    )

    resolved = resolve_explicit_candidate_ids(
        {
            ProductFamily.FUND: sale_classes,
            ProductFamily.DOMESTIC_ETP: (lookalike_etp,),
        },
        ("새봄혁신펀드",),
    )

    assert resolved == {ProductFamily.FUND: {"FUND-A", "FUND-AE"}}


def test_risk_evidence_excludes_accounting_loss_cost_and_rating_only_text() -> None:
    product_name = "KODEX 테스트산업"
    body = """
    <html><body>
      <h1>KODEX 테스트산업</h1>
      <p>목차 ........ 12</p>
      <p>회계상 평가손실 및 운용 비용은 재무제표에 인식됩니다.</p>
      <p>투자위험등급 2등급</p>
      <p>이 투자상품은 원금 손실이 발생할 수 있습니다.</p>
      <p>특정 산업 집중으로 가격 변동성 위험이 확대될 수 있습니다.</p>
    </body></html>
    """
    universe = ProvidedProductCandidateUniverse(
        candidates=(
            ProvidedProductCandidate(
                product_family=ProductFamily.DOMESTIC_ETP,
                product_id="KR7000000001",
                product_name=product_name,
                aliases=("000001",),
            ),
        ),
        database_sha256s={ProductFamily.DOMESTIC_ETP: _SHA_A},
        candidate_filter_sha256=_SHA_B,
        explicit_identity_bound=True,
    )

    relations = VerifiedLiveRelationExtractor().extract(
        _risk_snapshot(body),
        _risk_requirement(f"{product_name}의 위험요인을 알려줘.", product_name),
        universe,
    )

    assert 1 <= len(relations) <= 2
    excerpts = "\n".join(
        evidence.excerpt for item in relations for evidence in item.evidence
    )
    assert "원금 손실" in excerpts or "변동성 위험" in excerpts
    assert "평가손실" not in excerpts
    assert "운용 비용" not in excerpts
    assert "투자위험등급" not in excerpts


def test_risk_statement_in_a_catalogue_does_not_spill_to_neighbour_product() -> None:
    alpha = ProvidedProductCandidate(
        product_family=ProductFamily.DOMESTIC_ETP,
        product_id="KR7000000001",
        product_name="ALPHA 테스트 ETF",
        aliases=("000001",),
    )
    beta = ProvidedProductCandidate(
        product_family=ProductFamily.DOMESTIC_ETP,
        product_id="KR7000000002",
        product_name="BETA 테스트 ETF",
        aliases=("000002",),
    )
    body = """
    <html><body>
      <p>ALPHA 테스트 ETF는 환율 변동 위험으로 손실이 발생할 수 있습니다.</p>
      <p>BETA 테스트 ETF의 운용 비용과 회계상 평가손실 내역입니다.</p>
    </body></html>
    """
    universe = ProvidedProductCandidateUniverse(
        candidates=(alpha, beta),
        database_sha256s={ProductFamily.DOMESTIC_ETP: _SHA_A},
        candidate_filter_sha256=_SHA_B,
    )

    relations = VerifiedLiveRelationExtractor().extract(
        _risk_snapshot(body),
        _risk_requirement("ALPHA 테스트 ETF의 위험요인을 알려줘.", "ALPHA 테스트 ETF"),
        universe,
    )

    assert [item.product_id for item in relations] == [alpha.product_id]
    assert "ALPHA 테스트 ETF" in relations[0].evidence[0].excerpt


def test_narrowed_dependency_universe_does_not_turn_catalogue_into_single_product() -> None:
    """A selected product key cannot erase the source's multi-product structure."""

    alpha = ProvidedProductCandidate(
        product_family=ProductFamily.DOMESTIC_ETP,
        product_id="KR7000000001",
        product_name="ALPHA 전력 ETF",
        aliases=("000001", "A000001"),
    )
    body = """
    <html><body>
      <p>종목코드 000001</p>
      <p>ALPHA 전력 ETF는 환율 변동 위험으로 손실이 발생할 수 있습니다.</p>
      <p>종목코드 000002</p>
      <p>BETA 우주 ETF는 레버리지 위험으로 큰 손실이 발생할 수 있습니다.</p>
    </body></html>
    """
    # This is the shape used by a dependent risk lookup after a prior holding
    # relation has selected exactly one provided-DB product.
    narrowed_universe = ProvidedProductCandidateUniverse(
        candidates=(alpha,),
        database_sha256s={ProductFamily.DOMESTIC_ETP: _SHA_A},
        candidate_filter_sha256=_SHA_B,
        explicit_identity_bound=True,
    )

    relations = VerifiedLiveRelationExtractor().extract(
        _risk_snapshot(body),
        _risk_requirement("ALPHA 전력 ETF의 위험요인을 알려줘.", "ALPHA 전력 ETF"),
        narrowed_universe,
    )

    assert relations
    excerpts = "\n".join(
        evidence.excerpt for relation in relations for evidence in relation.evidence
    )
    assert "ALPHA 전력 ETF" in excerpts
    assert "BETA 우주 ETF" not in excerpts


def test_catalogue_scope_accepts_new_alphanumeric_domestic_code_without_sibling_risk() -> None:
    product = ProvidedProductCandidate(
        product_family=ProductFamily.DOMESTIC_ETP,
        product_id="KR7000098F01",
        product_name="ALPHA 차세대 ETF",
        aliases=("0098F0", "A0098F0"),
    )
    body = """
    <html><body>
      <p>종목코드 0098F0</p>
      <p>ALPHA 차세대 ETF</p>
      <p>이 금융상품은 시장가격 변동으로 원금 손실이 발생할 수 있습니다.</p>
      <p>종목코드 123456</p>
      <p>BETA 레버리지 ETF는 레버리지 위험으로 큰 손실이 발생할 수 있습니다.</p>
    </body></html>
    """
    universe = ProvidedProductCandidateUniverse(
        candidates=(product,),
        database_sha256s={ProductFamily.DOMESTIC_ETP: _SHA_A},
        candidate_filter_sha256=_SHA_B,
        explicit_identity_bound=True,
    )

    relations = VerifiedLiveRelationExtractor().extract(
        _risk_snapshot(body),
        _risk_requirement("ALPHA 차세대 ETF의 위험요인을 알려줘.", "ALPHA 차세대 ETF"),
        universe,
    )

    excerpts = "\n".join(
        evidence.excerpt for relation in relations for evidence in relation.evidence
    )
    assert "시장가격 변동" in excerpts
    assert "레버리지 위험" not in excerpts


def test_explicit_catalogue_compliance_notice_is_global_but_product_sections_are_not() -> None:
    product = ProvidedProductCandidate(
        product_family=ProductFamily.DOMESTIC_ETP,
        product_id="KR7000000021",
        product_name="ALPHA 전력 ETF",
        aliases=("000021", "A000021"),
    )
    body = """
    <html><body>
      <p>종목코드 000021</p><p>ALPHA 전력 ETF</p><p>전력 설비 기업에 투자합니다.</p>
      <p>종목코드 000022</p>
      <p>BETA 우주 ETF는 레버리지 위험으로 큰 손실이 발생할 수 있습니다.</p>
      <p>Compliance Notice</p>
      <p>금융투자상품은 자산가격 변동에 따라 투자원금의 손실이 발생할 수 있습니다.</p>
    </body></html>
    """
    universe = ProvidedProductCandidateUniverse(
        candidates=(product,),
        database_sha256s={ProductFamily.DOMESTIC_ETP: _SHA_A},
        candidate_filter_sha256=_SHA_B,
        explicit_identity_bound=True,
    )

    relations = VerifiedLiveRelationExtractor().extract(
        _risk_snapshot(body),
        _risk_requirement(
            "ALPHA 전력 ETF의 위험요인과 손실 유의점을 알려줘.",
            "ALPHA 전력 ETF",
        ),
        universe,
    )

    excerpts = "\n".join(
        evidence.excerpt for relation in relations for evidence in relation.evidence
    )
    assert "투자원금의 손실" in excerpts
    assert "BETA 우주 ETF" not in excerpts
    assert "레버리지 위험" not in excerpts


def test_single_product_risk_document_cannot_bind_to_another_requested_product() -> None:
    alpha = ProvidedProductCandidate(
        product_family=ProductFamily.DOMESTIC_ETP,
        product_id="KR7000000011",
        product_name="ALPHA 테스트 ETF",
        aliases=("000011",),
    )
    beta = ProvidedProductCandidate(
        product_family=ProductFamily.DOMESTIC_ETP,
        product_id="KR7000000012",
        product_name="BETA 테스트 ETF",
        aliases=("000012",),
    )
    universe = ProvidedProductCandidateUniverse(
        candidates=(alpha, beta),
        database_sha256s={ProductFamily.DOMESTIC_ETP: _SHA_A},
        candidate_filter_sha256=_SHA_B,
    )
    snapshots = (
        _risk_snapshot(
            "<h1>ALPHA 테스트 ETF</h1>"
            "<p>이 상품은 원금 손실 위험이 있습니다.</p>"
        ),
        _risk_snapshot(
            "<h1>BETA 테스트 ETF</h1>"
            "<p>이 상품은 환율 변동 위험이 있습니다.</p>"
        ),
    )

    relations = VerifiedLiveRelationExtractor().extract_many(
        snapshots,
        _risk_requirement("ALPHA 테스트 ETF의 위험요인을 알려줘.", "ALPHA 테스트 ETF"),
        universe,
    )

    assert {item.product_id for item in relations} == {alpha.product_id}


def test_official_english_factsheet_risk_is_exact_span_evidence() -> None:
    product = ProvidedProductCandidate(
        product_family=ProductFamily.DOMESTIC_ETP,
        product_id="KR7000000003",
        product_name="GAMMA Cybersecurity ETF",
        aliases=("GAMMA",),
    )
    body = """
    <html><body>
      <h1>GAMMA Cybersecurity ETF</h1>
      <p>Investing involves risk, including possible loss of principal.</p>
      <p>International investments may involve risk of capital loss from
      unfavorable fluctuation in currency values or political instability.</p>
    </body></html>
    """
    universe = ProvidedProductCandidateUniverse(
        candidates=(product,),
        database_sha256s={ProductFamily.DOMESTIC_ETP: _SHA_A},
        candidate_filter_sha256=_SHA_B,
        explicit_identity_bound=True,
    )

    relations = VerifiedLiveRelationExtractor().extract(
        _risk_snapshot(body),
        _risk_requirement(
            "GAMMA Cybersecurity ETF의 위험요인을 알려줘.",
            product.product_name,
        ),
        universe,
    )

    excerpts = "\n".join(
        evidence.excerpt for item in relations for evidence in item.evidence
    )
    assert "possible loss of principal" in excerpts
    assert "currency values" in excerpts


def test_holdings_table_line_window_keeps_exact_product_and_entity_binding() -> None:
    product = ProvidedProductCandidate(
        product_family=ProductFamily.OVERSEAS_ETP,
        product_id="AURR.O",
        product_name="Aurora Solar ETF",
        aliases=("AURR",),
    )
    universe = ProvidedProductCandidateUniverse(
        candidates=(product,),
        database_sha256s={ProductFamily.OVERSEAS_ETP: _SHA_A},
        candidate_filter_sha256=_SHA_B,
        explicit_identity_bound=True,
    )
    requirement = KnowledgeRequirement(
        requirement_id="need_1",
        capability=KnowledgeCapability.PORTFOLIO_HOLDINGS,
        predicate="holds_company",
        subject="Zythera",
        subject_evidence_span="Zythera",
        relation_evidence_span="구성종목에 들어 있는",
        product_families=(ProductFamily.OVERSEAS_ETP,),
        source_registry_ids=("kraneshares-main-product",),
    )
    filler = "시장 정보와 운용 현황을 설명하는 공식 문서의 표 행입니다. " * 3
    body = f"""
    <html><body>
      <h1>Aurora Solar ETF</h1>
      <h2>Portfolio Holdings</h2>
      <p>{filler}</p><p>{filler}</p><p>{filler}</p>
      <p>{filler}</p><p>{filler}</p><p>{filler}</p>
      <p>Zythera Corporation</p>
    </body></html>
    """.encode()
    url = "https://kraneshares.com/etf/aurora-solar/"
    registry = load_official_source_registry()
    snapshot = OfficialSourceSnapshot(
        source=registry.authorize_url(url, KnowledgeCapability.PORTFOLIO_HOLDINGS),
        capability=KnowledgeCapability.PORTFOLIO_HOLDINGS,
        source_uri=url,
        title="Aurora Solar ETF official factsheet",
        content_type="text/html",
        content=body,
        content_sha256=hashlib.sha256(body).hexdigest(),
        published_at=None,
        as_of=_AS_OF,
    )

    relations = VerifiedLiveRelationExtractor().extract(snapshot, requirement, universe)

    assert {(item.product_id, item.object.canonical_name) for item in relations} == {
        ("AURR.O", "Zythera")
    }
    assert "Aurora Solar ETF" in relations[0].evidence[0].excerpt
    assert "Zythera Corporation" in relations[0].evidence[0].excerpt


def test_multi_fund_schedule_never_uses_search_title_as_holding_owner() -> None:
    """A search-result title is not ownership proof for a multi-fund report."""

    product = ProvidedProductCandidate(
        product_family=ProductFamily.OVERSEAS_ETP,
        product_id="AMX:KWEB.K",
        product_name="KraneShares CSI China Internet ETF",
        aliases=("KWEB", "KWEB.K"),
    )
    universe = ProvidedProductCandidateUniverse(
        candidates=(product,),
        database_sha256s={ProductFamily.OVERSEAS_ETP: _SHA_A},
        candidate_filter_sha256=_SHA_B,
    )
    requirement = KnowledgeRequirement(
        requirement_id="need_1",
        capability=KnowledgeCapability.PORTFOLIO_HOLDINGS,
        predicate="holds_company",
        subject="Cambricon",
        subject_evidence_span="캠브리콘",
        relation_evidence_span="편입된",
        product_families=(ProductFamily.OVERSEAS_ETP,),
        source_registry_ids=("kraneshares-main-product",),
    )
    filler = "unrelated portfolio accounting line\n" * 160
    body = f"""
    Schedule of Investments (Unaudited) June 30, 2024
    KraneShares CICC China 5G & Semiconductor Index ETF
    Portfolio Holdings
    {filler}
    Cambricon Technologies, Cl A  6,260  170,346
    Schedule of Investments (Unaudited) June 30, 2024
    KraneShares Electric Vehicles & Future Mobility ETF
    Portfolio Holdings
    Another Company  1,000  10,000
    """.encode()
    url = (
        "https://kraneshares.com/resources/compliance/"
        "2024_08_22_multi_fund_schedule.of.investments.pdf"
    )
    registry = load_official_source_registry()
    snapshot = OfficialSourceSnapshot(
        source=registry.authorize_url(url, KnowledgeCapability.PORTFOLIO_HOLDINGS),
        capability=KnowledgeCapability.PORTFOLIO_HOLDINGS,
        source_uri=url,
        title="KraneShares 90% KWEB Defined Outcome ETF | KBUF",
        content_type="text/plain",
        content=body,
        content_sha256=hashlib.sha256(body).hexdigest(),
        published_at=date(2024, 8, 22),
        as_of=date(2024, 6, 30),
    )

    relations = VerifiedLiveRelationExtractor().extract(snapshot, requirement, universe)

    assert relations == ()
