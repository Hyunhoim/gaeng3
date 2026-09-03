from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from finance_agent_core.agent import RoutedFinanceAgent, execute_answer_request
from finance_agent_core.agent.external_knowledge_router import (
    ExternalKnowledgeRouteDisposition,
    ExternalKnowledgeRouter,
)
from finance_agent_core.agent.intent_analysis import IntentAnalysisDraft
from finance_agent_core.agent.knowledge_planning import CompositeKnowledgePlanCompiler
from finance_agent_core.agent.official_adapter import official_response_from_backend
from finance_agent_core.config.knowledge_coverage import (
    ApprovedKnowledgeCoverageManifest,
    KnowledgeCapability,
    KnowledgeCoverageEntry,
)
from finance_agent_core.config.source_registry import (
    OfficialSourceRegistryError,
    load_official_source_registry,
)
from finance_agent_core.contracts.backend import BackendAgentRequest, BackendStatus
from finance_agent_core.contracts.external_knowledge import KnowledgeIntentRequirement
from finance_agent_core.contracts.queryplan import ProductFamily
from finance_agent_core.contracts.routing import InteractionIntent
from finance_agent_core.execution.external_knowledge_executor import (
    ExternalKnowledgeExecutor,
)
from finance_agent_core.execution.external_knowledge_verifier import (
    ExternalKnowledgeEvidenceBundle,
    ExternalKnowledgeEvidenceVerifier,
    OfficialProductFact,
)
from finance_agent_core.retrieval.official_acquisition import (
    _MAX_FETCH_TIMEOUT_SECONDS,
    OfficialEvidenceAcquisitionCoordinator,
    OfficialEvidenceAcquisitionStatus,
)
from finance_agent_core.retrieval.official_connector import (
    BoundedOfficialDiscovery,
    OfficialConnectorError,
    OfficialDiscoveryCandidate,
    OfficialFetchRequest,
    OfficialHttpResponse,
    OfficialSourceConnector,
)
from finance_agent_core.retrieval.pending_corpus import (
    PendingCorpusCatalog,
    PendingCorpusError,
    PendingCorpusSource,
    collect_pending_corpus,
    load_pending_corpus_manifest,
)
from finance_agent_core.retrieval.temporal_relations import (
    ExternalEvidenceReference,
    TemporalEntity,
    TemporalEntityKind,
    TemporalRelationError,
    TemporalRelationRecord,
    TemporalRelationSearchRequest,
    TemporalRelationStore,
    seal_temporal_relation_bundle,
)

_SHA_A = "a" * 64
_SHA_B = "b" * 64
_SHA_C = "c" * 64


def _disabled_coverage() -> ApprovedKnowledgeCoverageManifest:
    return ApprovedKnowledgeCoverageManifest(
        schema_version="1.0",
        manifest_id="test-disabled-external-coverage",
        dataset_release_id="test-dataset",
        capabilities=tuple(
            KnowledgeCoverageEntry(
                capability=capability,
                status="not_configured",
                runtime_execution_enabled=False,
                allowed_source_kinds=("provided", "external_approved"),
            )
            for capability in KnowledgeCapability
        ),
    )


class _FixedIntentProvider:
    provider_name = "fixed-test-intent"
    model_name = "fixed-test-intent"

    def __init__(self, draft: IntentAnalysisDraft) -> None:
        self.draft = draft

    def analyze_intent(self, question: str, question_id: str) -> IntentAnalysisDraft:
        assert question
        assert question_id
        return self.draft


class _FakeTransport:
    def __init__(self, response: OfficialHttpResponse) -> None:
        self.response = response
        self.timeouts: list[float] = []

    def fetch(
        self,
        url: str,
        *,
        timeout_seconds: float,
        max_bytes: int,
    ) -> OfficialHttpResponse:
        assert url
        assert 0 < timeout_seconds <= 30
        assert max_bytes > 0
        self.timeouts.append(timeout_seconds)
        return self.response


class _AlwaysFailTransport:
    def __init__(self) -> None:
        self.calls = 0

    def fetch(
        self,
        url: str,
        *,
        timeout_seconds: float,
        max_bytes: int,
    ) -> OfficialHttpResponse:
        assert url and timeout_seconds > 0 and max_bytes > 0
        self.calls += 1
        raise OfficialConnectorError("simulated official fetch failure")


class _EchoOfficialTransport:
    def fetch(
        self,
        url: str,
        *,
        timeout_seconds: float,
        max_bytes: int,
    ) -> OfficialHttpResponse:
        assert url and timeout_seconds > 0 and max_bytes > 0
        return OfficialHttpResponse(
            status_code=200,
            final_url=url,
            content_type="text/html",
            body=b"bounded official evidence",
        )


class _FakeDiscoveryProvider:
    def __init__(self, candidates: tuple[OfficialDiscoveryCandidate, ...]) -> None:
        self.candidates = candidates
        self.calls = 0

    def discover(
        self,
        *,
        query: str,
        capability: KnowledgeCapability,
        source_registry_ids: tuple[str, ...],
        evidence_cutoff: date,
        top_k: int,
    ) -> tuple[OfficialDiscoveryCandidate, ...]:
        assert query
        assert capability
        assert source_registry_ids
        assert evidence_cutoff == date(2026, 8, 24)
        assert 1 <= top_k <= 20
        self.calls += 1
        return self.candidates


class _UnusedPdfExtractor:
    def extract(self, content: bytes) -> str:
        raise AssertionError(f"unexpected PDF extraction: {len(content)} bytes")


def _intent(
    question: str,
    requirements: list[tuple[KnowledgeCapability, str, str]],
    families: list[ProductFamily],
    *,
    interaction_intent: InteractionIntent = InteractionIntent.SEARCH,
) -> IntentAnalysisDraft:
    return IntentAnalysisDraft(
        intent=interaction_intent,
        product_families=families,
        evidence_spans=[],
        needs_clarification=False,
        knowledge_requirements=[
            KnowledgeIntentRequirement(
                capability=capability,
                subject_span=subject,
                relation_span=relation,
            )
            for capability, subject, relation in requirements
        ],
    )


def _entity(entity_id: str, kind: TemporalEntityKind, name: str) -> TemporalEntity:
    return TemporalEntity(entity_id=entity_id, kind=kind, canonical_name=name)


def _evidence(
    evidence_id: str,
    source_registry_id: str,
    source_uri: str,
    publisher: str,
    *,
    content_sha256: str = _SHA_A,
    as_of: date = date(2026, 8, 22),
) -> ExternalEvidenceReference:
    return ExternalEvidenceReference(
        evidence_id=evidence_id,
        source_registry_id=source_registry_id,
        document_id=f"doc-{evidence_id}",
        title=f"공식 자료 {evidence_id}",
        publisher=publisher,
        source_uri=source_uri,
        published_at=as_of,
        as_of=as_of,
        content_sha256=content_sha256,
        locator="section 1",
        excerpt="공식 원문에서 확인된 관계 근거",
    )


def test_source_registry_allows_only_capability_scoped_official_urls() -> None:
    registry = load_official_source_registry()

    entry = registry.authorize_url(
        "https://engage.kraneshares.com/s/b264ceb1/kstr-presentation/",
        KnowledgeCapability.PORTFOLIO_HOLDINGS,
    )
    assert entry.source_id == "kraneshares-product"
    with pytest.raises(OfficialSourceRegistryError):
        registry.authorize_url(
            "https://example.com/kstr",
            KnowledgeCapability.PORTFOLIO_HOLDINGS,
        )
    with pytest.raises(OfficialSourceRegistryError):
        registry.authorize_url(
            "https://www.ecopro.co.kr/sub010101/view",
            KnowledgeCapability.PORTFOLIO_HOLDINGS,
        )
    with pytest.raises(OfficialSourceRegistryError):
        registry.authorize_url(
            "https://www.ecopro.co.kr/sub010101/unrelated",
            KnowledgeCapability.CORPORATE_SUBSIDIARY,
        )


def test_discovery_snippets_are_candidates_and_fetch_rechecks_the_final_url() -> None:
    url = "https://www.fsc.go.kr/po010103/86950"
    candidate = OfficialDiscoveryCandidate(url=url, title="국민성장펀드 구조")
    discovery = BoundedOfficialDiscovery()
    authorized = discovery.authorize_candidates(
        (
            OfficialDiscoveryCandidate(url="https://example.com/a", title="비공식"),
            candidate,
        ),
        KnowledgeCapability.FUND_STRUCTURE_STRATEGY_TREND,
    )
    assert authorized == (candidate,)

    connector = OfficialSourceConnector(
        _FakeTransport(
            OfficialHttpResponse(
                status_code=200,
                final_url=url,
                content_type="text/html",
                body=b"official snapshot",
            )
        )
    )
    snapshot = connector.fetch(
        candidate,
        OfficialFetchRequest(
            url=url,
            capability=KnowledgeCapability.FUND_STRUCTURE_STRATEGY_TREND,
            published_at=date(2026, 5, 21),
            as_of=date(2026, 5, 21),
        ),
    )
    assert snapshot.source.source_id == "fsc-policy"
    assert snapshot.content_sha256

    redirecting = OfficialSourceConnector(
        _FakeTransport(
            OfficialHttpResponse(
                status_code=200,
                final_url="https://example.com/stolen",
                content_type="text/html",
                body=b"not official",
            )
        )
    )
    with pytest.raises((OfficialConnectorError, OfficialSourceRegistryError)):
        redirecting.fetch(
            candidate,
            OfficialFetchRequest(
                url=url,
                capability=KnowledgeCapability.FUND_STRUCTURE_STRATEGY_TREND,
                published_at=date(2026, 5, 21),
            ),
        )


def test_pending_corpus_collection_is_hashed_and_never_marked_approved(
    tmp_path: Path,
) -> None:
    url = "https://www.fsc.go.kr/po010103/86950"
    catalog = PendingCorpusCatalog(
        catalog_id="pending-test-catalog",
        evidence_cutoff=date(2026, 8, 24),
        documents=(
            PendingCorpusSource(
                document_id="fsc-growth-fund-test",
                title="국민성장펀드 구조",
                source_uri=url,
                capabilities=(KnowledgeCapability.FUND_STRUCTURE_STRATEGY_TREND,),
                published_at=date(2026, 5, 21),
                as_of=date(2026, 5, 21),
                language="ko",
                purposes=("product_structure", "regulation"),
            ),
        ),
    )
    output = tmp_path / "pending-corpus"
    manifest = collect_pending_corpus(
        output,
        connector=OfficialSourceConnector(
            _FakeTransport(
                OfficialHttpResponse(
                    status_code=200,
                    final_url=url,
                    content_type="text/html",
                    body=(
                        b"<html><body><h1>Growth Fund</h1>"
                        b"<script>secret()</script><p>Official structure</p></body></html>"
                    ),
                )
            )
        ),
        pdf_text_extractor=_UnusedPdfExtractor(),
        catalog=catalog,
    )

    assert manifest.status == "not_authorized_for_retrieval_or_release"
    assert manifest.documents[0].rights_status == "pending_independent_review"
    assert manifest.documents[0].finance_review_status == "pending_independent_review"
    text = (output / manifest.documents[0].text_relative_path).read_text("utf-8")
    assert "Official structure" in text
    assert "secret()" not in text
    loaded = load_pending_corpus_manifest(output / "pending-manifest.json")
    assert loaded == manifest
    assert loaded.canonical_sha256 == manifest.canonical_sha256
    with pytest.raises(PendingCorpusError):
        collect_pending_corpus(
            output,
            connector=OfficialSourceConnector(
                _FakeTransport(
                    OfficialHttpResponse(
                        status_code=200,
                        final_url=url,
                        content_type="text/html",
                        body=b"different",
                    )
                )
            ),
            pdf_text_extractor=_UnusedPdfExtractor(),
            catalog=catalog,
        )


def test_public_api_classifies_incomplete_external_search_as_cannot_verify() -> None:
    question = "캠브리콘이 편입된 중국 반도체 해외 ETF를 알려줘."
    draft = _intent(
        question,
        [(KnowledgeCapability.PORTFOLIO_HOLDINGS, "캠브리콘", "편입된")],
        [ProductFamily.OVERSEAS_ETP],
    )
    agent = RoutedFinanceAgent(
        {},
        intent_analysis_provider=_FixedIntentProvider(draft),
        external_knowledge_router=ExternalKnowledgeRouter(coverage=_disabled_coverage()),
    )

    result = execute_answer_request(
        agent,
        BackendAgentRequest(request_id="external-public-001", question=question),
    )

    assert result.http_status_code == 200
    assert result.response.status is BackendStatus.CANNOT_VERIFY
    assert result.response.query_plan is not None
    assert result.response.query_plan.plan_kind == "composite_external_knowledge"
    assert not result.response.query_plan.execution_authorized
    assert result.response.products == []
    assert result.response.documents == []
    assert result.response.citations == []
    assert "일부 조건을 무시한 상품 검색은 수행하지 않았습니다" in result.response.answer

    official = official_response_from_backend(
        question_id="external-public-001",
        question=question,
        response=result.response,
    )
    assert "official_evidence_authority_check" in official.think_trace
    assert "safe_cannot_verify_response" in official.think_trace


@pytest.mark.parametrize(
    ("question", "requirements", "families", "intent", "expected"),
    [
        (
            "캠브리콘이 편입된 중국 반도체 해외 ETF를 알려줘.",
            [(KnowledgeCapability.PORTFOLIO_HOLDINGS, "캠브리콘", "편입된")],
            [ProductFamily.OVERSEAS_ETP],
            InteractionIntent.SEARCH,
            [KnowledgeCapability.PORTFOLIO_HOLDINGS],
        ),
        (
            "최근 6개월 동안 우주항공 테마와 연결 이력이 있는 국내 ETF를 정리해줘.",
            [
                (
                    KnowledgeCapability.SECTOR_THEME_HISTORY,
                    "우주항공",
                    "테마와 연결 이력",
                )
            ],
            [ProductFamily.DOMESTIC_ETP],
            InteractionIntent.SEARCH,
            [KnowledgeCapability.SECTOR_THEME_HISTORY],
        ),
        (
            "에코프로의 자회사를 편입한 국내 ETF 중 순자산이 큰 상품의 위험요인을 알려줘.",
            [
                (KnowledgeCapability.CORPORATE_SUBSIDIARY, "에코프로", "자회사"),
                (KnowledgeCapability.PORTFOLIO_HOLDINGS, "에코프로", "편입한"),
                (KnowledgeCapability.PRODUCT_RISK_FACTORS, "상품", "위험요인"),
            ],
            [ProductFamily.DOMESTIC_ETP],
            InteractionIntent.SEARCH,
            [
                KnowledgeCapability.CORPORATE_SUBSIDIARY,
                KnowledgeCapability.PORTFOLIO_HOLDINGS,
                KnowledgeCapability.PRODUCT_RISK_FACTORS,
            ],
        ),
        (
            "국민성장펀드의 구조와 투자 전략 및 운용 동향을 알려줘.",
            [
                (
                    KnowledgeCapability.FUND_STRUCTURE_STRATEGY_TREND,
                    "국민성장펀드",
                    "구조",
                )
            ],
            [ProductFamily.FUND],
            InteractionIntent.EXPLAIN,
            [KnowledgeCapability.FUND_STRUCTURE_STRATEGY_TREND],
        ),
    ],
)
def test_public_example_types_compile_to_non_executable_composite_plans(
    question: str,
    requirements: list[tuple[KnowledgeCapability, str, str]],
    families: list[ProductFamily],
    intent: InteractionIntent,
    expected: list[KnowledgeCapability],
) -> None:
    plan = CompositeKnowledgePlanCompiler().compile(
        question,
        "public-example-variant",
        _intent(question, requirements, families, interaction_intent=intent),
    )

    assert [item.capability for item in plan.requirements] == expected
    assert plan.join_order[-1] == "official_product_db"
    assert plan.execution_authorized is False
    if KnowledgeCapability.SECTOR_THEME_HISTORY in expected:
        assert plan.requirements[0].time_range is not None
        assert plan.requirements[0].time_range.start == date(2026, 2, 24)
    if KnowledgeCapability.PRODUCT_RISK_FACTORS in expected:
        assert plan.product_selection is not None
        assert plan.product_selection.rank_field == "aum"
        assert plan.requirements[1].depends_on == ("need_1",)
        assert plan.requirements[2].depends_on == ("need_2",)


def test_normal_external_question_enters_approved_live_execution() -> None:
    question = "캠브리콘을 편입한 중국 반도체 해외 ETF를 찾아줘."
    decision = ExternalKnowledgeRouter().route(
        question,
        "knowledge-search-candidate",
        _intent(
            question,
            [(KnowledgeCapability.PORTFOLIO_HOLDINGS, "캠브리콘", "편입한")],
            [ProductFamily.OVERSEAS_ETP],
        ),
    )

    assert decision.disposition is ExternalKnowledgeRouteDisposition.EXECUTE
    assert decision.reason_code == "external_knowledge_authorized"
    assert decision.plan is not None
    assert decision.plan.execution_authorized is True


def test_temporal_relation_store_requires_cutoff_and_exact_entity_aliases() -> None:
    relation = TemporalRelationRecord(
        relation_id="rel-kstr-cambricon",
        capability=KnowledgeCapability.PORTFOLIO_HOLDINGS,
        predicate="holds_company",
        subject=_entity("company-cambricon", TemporalEntityKind.COMPANY, "캠브리콘"),
        object=_entity("product-kstr", TemporalEntityKind.PRODUCT, "KSTR"),
        product_family=ProductFamily.OVERSEAS_ETP,
        product_id="KSTR",
        valid_from=date(2026, 6, 30),
        observed_at=date(2026, 6, 30),
        evidence=(
            _evidence(
                "ev-kstr",
                "kraneshares-product",
                "https://engage.kraneshares.com/s/b264ceb1/kstr-presentation/",
                "KraneShares",
                as_of=date(2026, 6, 30),
            ),
        ),
    )
    bundle = seal_temporal_relation_bundle(
        (relation,),
        corpus_manifest_sha256=_SHA_B,
    )
    store = TemporalRelationStore(bundle)

    assert [
        item.product_id
        for item in store.search(
            TemporalRelationSearchRequest(
                capability=KnowledgeCapability.PORTFOLIO_HOLDINGS,
                subject="캠브리콘",
                product_families=(ProductFamily.OVERSEAS_ETP,),
            )
        )
    ] == ["KSTR"]
    assert (
        store.search(
            TemporalRelationSearchRequest(
                capability=KnowledgeCapability.PORTFOLIO_HOLDINGS,
                subject="임의의 기업",
            )
        )
        == ()
    )

    late = relation.model_copy(
        update={
            "relation_id": "rel-too-late",
            "observed_at": date(2026, 8, 25),
            "evidence": (
                relation.evidence[0].model_copy(
                    update={
                        "evidence_id": "ev-too-late",
                        "document_id": "doc-too-late",
                        "published_at": date(2026, 8, 25),
                        "as_of": date(2026, 8, 25),
                    }
                ),
            ),
        }
    )
    with pytest.raises(TemporalRelationError):
        seal_temporal_relation_bundle((late,), corpus_manifest_sha256=_SHA_B)


def test_cache_miss_fetches_only_official_snapshot_into_pending_intake() -> None:
    cached_relation = TemporalRelationRecord(
        relation_id="rel-kstr-cambricon-cache",
        capability=KnowledgeCapability.PORTFOLIO_HOLDINGS,
        predicate="holds_company",
        subject=_entity("company-cambricon", TemporalEntityKind.COMPANY, "캠브리콘"),
        object=_entity("product-kstr", TemporalEntityKind.PRODUCT, "KSTR"),
        product_family=ProductFamily.OVERSEAS_ETP,
        product_id="KSTR",
        observed_at=date(2026, 6, 30),
        evidence=(
            _evidence(
                "ev-kstr-cache",
                "kraneshares-product",
                "https://engage.kraneshares.com/s/b264ceb1/kstr-presentation/",
                "KraneShares",
                as_of=date(2026, 6, 30),
            ),
        ),
    )
    store = TemporalRelationStore(
        seal_temporal_relation_bundle(
            (cached_relation,),
            corpus_manifest_sha256=_SHA_B,
        )
    )
    source_url = "https://www.fsc.go.kr/po010103/86950"
    discovery = _FakeDiscoveryProvider(
        (
            OfficialDiscoveryCandidate(
                url="https://example.com/untrusted",
                title="비공식 검색 결과",
                published_at=date(2026, 5, 21),
            ),
            OfficialDiscoveryCandidate(
                url=source_url,
                title="국민성장펀드 구조",
                published_at=date(2026, 5, 21),
                as_of=date(2026, 5, 21),
            ),
        )
    )
    transport = _FakeTransport(
        OfficialHttpResponse(
            status_code=200,
            final_url=source_url,
            content_type="text/html",
            body=b"official snapshot pending review",
        )
    )
    coordinator = OfficialEvidenceAcquisitionCoordinator(
        relation_store=store,
        discovery_provider=discovery,
        connector=OfficialSourceConnector(transport),
    )
    question = "국민성장펀드의 구조와 투자 전략 및 운용 동향을 알려줘."
    plan = CompositeKnowledgePlanCompiler().compile(
        question,
        "fund-knowledge-cache-miss",
        _intent(
            question,
            [
                (
                    KnowledgeCapability.FUND_STRUCTURE_STRATEGY_TREND,
                    "국민성장펀드",
                    "구조와 투자 전략 및 운용 동향",
                )
            ],
            [ProductFamily.FUND],
            interaction_intent=InteractionIntent.EXPLAIN,
        ),
    )

    result = coordinator.acquire(plan.requirements[0])

    assert result.status is OfficialEvidenceAcquisitionStatus.LIVE_EVIDENCE_CANDIDATE
    assert result.relations == ()
    assert len(result.snapshots) == 1
    assert result.snapshots[0].source_uri == source_url
    assert result.rejected_candidate_count == 1
    # One unresponsive host must not spend the acquisition deadline before the
    # other candidates are tried. Approved registry hosts answered in 0.16s
    # median and 2.23s worst case when probed on 2026-09-01.
    assert transport.timeouts
    assert _MAX_FETCH_TIMEOUT_SECONDS <= 10.0
    assert max(transport.timeouts) <= 10.0
    assert discovery.calls == 1


def test_failed_official_fetches_consume_the_bounded_attempt_budget() -> None:
    store = TemporalRelationStore(seal_temporal_relation_bundle((), corpus_manifest_sha256=_SHA_B))
    candidates = tuple(
        OfficialDiscoveryCandidate(
            url=f"https://www.fsc.go.kr/po010103/{identifier}",
            title=f"공식 자료 {identifier}",
            published_at=date(2026, 5, 21),
            as_of=date(2026, 5, 21),
        )
        for identifier in ("86950", "87009")
    )
    transport = _AlwaysFailTransport()
    coordinator = OfficialEvidenceAcquisitionCoordinator(
        relation_store=store,
        discovery_provider=_FakeDiscoveryProvider(candidates),
        connector=OfficialSourceConnector(transport),
    )
    question = "국민성장펀드의 구조와 투자 전략 및 운용 동향을 알려줘."
    requirement = (
        CompositeKnowledgePlanCompiler()
        .compile(
            question,
            "bounded-failed-fetches",
            _intent(
                question,
                [
                    (
                        KnowledgeCapability.FUND_STRUCTURE_STRATEGY_TREND,
                        "국민성장펀드",
                        "구조와 투자 전략 및 운용 동향",
                    )
                ],
                [ProductFamily.FUND],
                interaction_intent=InteractionIntent.EXPLAIN,
            ),
        )
        .requirements[0]
    )

    result = coordinator.acquire(requirement, top_k=2, max_fetches=2)

    assert result.status is OfficialEvidenceAcquisitionStatus.CANNOT_VERIFY
    assert result.fetch_count == 2
    assert transport.calls == 2
    assert result.snapshots == ()


def test_acquisition_result_accepts_snapshots_up_to_its_fetch_budget() -> None:
    store = TemporalRelationStore(seal_temporal_relation_bundle((), corpus_manifest_sha256=_SHA_B))
    candidates = tuple(
        OfficialDiscoveryCandidate(
            url=f"https://www.fsc.go.kr/po010103/{identifier}",
            title=f"공식 자료 {identifier}",
            published_at=date(2026, 5, 21),
            as_of=date(2026, 5, 21),
        )
        for identifier in range(88001, 88010)
    )
    coordinator = OfficialEvidenceAcquisitionCoordinator(
        relation_store=store,
        discovery_provider=_FakeDiscoveryProvider(candidates),
        connector=OfficialSourceConnector(_EchoOfficialTransport()),
    )
    question = "국민성장펀드의 구조와 투자 전략 및 운용 동향을 알려줘."
    requirement = (
        CompositeKnowledgePlanCompiler()
        .compile(
            question,
            "bounded-nine-snapshots",
            _intent(
                question,
                [
                    (
                        KnowledgeCapability.FUND_STRUCTURE_STRATEGY_TREND,
                        "국민성장펀드",
                        "구조와 투자 전략 및 운용 동향",
                    )
                ],
                [ProductFamily.FUND],
                interaction_intent=InteractionIntent.EXPLAIN,
            ),
        )
        .requirements[0]
    )

    result = coordinator.acquire(requirement, top_k=9, max_fetches=9)

    assert result.status is OfficialEvidenceAcquisitionStatus.LIVE_EVIDENCE_CANDIDATE
    assert result.fetch_count == 9
    assert len(result.snapshots) == 9


def test_evidence_verifier_requires_complete_multihop_chain_and_provided_aum() -> None:
    question = "에코프로의 자회사를 편입한 국내 ETF 중 순자산이 큰 상품의 위험요인을 알려줘."
    plan = CompositeKnowledgePlanCompiler().compile(
        question,
        "ecopro-composite",
        _intent(
            question,
            [
                (KnowledgeCapability.CORPORATE_SUBSIDIARY, "에코프로", "자회사"),
                (KnowledgeCapability.PORTFOLIO_HOLDINGS, "에코프로", "편입한"),
                (KnowledgeCapability.PRODUCT_RISK_FACTORS, "상품", "위험요인"),
            ],
            [ProductFamily.DOMESTIC_ETP],
        ),
    )
    ecopro = _entity("company-ecopro", TemporalEntityKind.COMPANY, "에코프로")
    ecopro_bm = _entity("company-ecopro-bm", TemporalEntityKind.COMPANY, "에코프로비엠")
    product = _entity("product-kodex-battery", TemporalEntityKind.PRODUCT, "KODEX 2차전지")
    risk = _entity("risk-battery", TemporalEntityKind.RISK_CLAIM, "산업 집중 위험")
    relations = (
        TemporalRelationRecord(
            relation_id="rel-1-subsidiary",
            capability=KnowledgeCapability.CORPORATE_SUBSIDIARY,
            predicate="subsidiary_of",
            subject=ecopro,
            object=ecopro_bm,
            observed_at=date(2026, 8, 22),
            evidence=(
                _evidence(
                    "ev-1-subsidiary",
                    "ecopro-corporate",
                    "https://www.ecopro.co.kr/sub010101/view",
                    "에코프로",
                ).model_copy(update={"published_at": None}),
            ),
        ),
        TemporalRelationRecord(
            relation_id="rel-2-holding",
            capability=KnowledgeCapability.PORTFOLIO_HOLDINGS,
            predicate="holds_company",
            subject=ecopro_bm,
            object=product,
            product_family=ProductFamily.DOMESTIC_ETP,
            product_id="KR-ETF-001",
            observed_at=date(2026, 8, 22),
            evidence=(
                _evidence(
                    "ev-2-holding",
                    "samsungfund-kodex-product",
                    "https://www.samsungfund.com/etf/product/view.do?id=2ETFB1",
                    "삼성자산운용",
                ),
            ),
        ),
        TemporalRelationRecord(
            relation_id="rel-3-risk",
            capability=KnowledgeCapability.PRODUCT_RISK_FACTORS,
            predicate="has_risk_factor",
            subject=product,
            object=risk,
            product_family=ProductFamily.DOMESTIC_ETP,
            product_id="KR-ETF-001",
            observed_at=date(2026, 8, 22),
            evidence=(
                _evidence(
                    "ev-3-risk",
                    "samsungfund-kodex-product",
                    "https://www.samsungfund.com/etf/product/view.do?id=2ETFB1",
                    "삼성자산운용",
                    content_sha256=_SHA_C,
                ),
            ),
        ),
    )
    sealed = seal_temporal_relation_bundle(
        relations,
        corpus_manifest_sha256=_SHA_B,
    )
    bundle = ExternalKnowledgeEvidenceBundle(
        plan=plan,
        relations=sealed.relations,
        requirement_relations={
            "need_1": ("rel-1-subsidiary",),
            "need_2": ("rel-2-holding",),
            "need_3": ("rel-3-risk",),
        },
        product_facts=(
            OfficialProductFact(
                evidence_id="provided-aum-1",
                product_family=ProductFamily.DOMESTIC_ETP,
                product_id="KR-ETF-001",
                product_name="KODEX 2차전지",
                canonical_field="aum",
                normalized_value=123456789,
                as_of=date(2026, 8, 22),
                source_dataset="domestic_etp",
                source_id="PREF01N001",
                source_row=2,
                source_columns=("du_last_aum",),
                unit="source_currency_amount",
                trading_currency="KRW",
                source_database_sha256=_SHA_A,
            ),
        ),
    )
    verification = ExternalKnowledgeEvidenceVerifier().verify(
        bundle,
        approved_product_ids={(ProductFamily.DOMESTIC_ETP, "KR-ETF-001")},
    )
    assert verification.passed

    broken = bundle.model_copy(update={"requirement_relations": {"need_1": ("rel-1-subsidiary",)}})
    rejected = ExternalKnowledgeEvidenceVerifier().verify(
        broken,
        approved_product_ids={(ProductFamily.DOMESTIC_ETP, "KR-ETF-001")},
    )
    assert not rejected.passed
    assert not rejected.checks["requirement_coverage"]


def test_external_knowledge_executor_joins_sealed_relation_and_verifier() -> None:
    question = "국민성장펀드의 구조를 알려줘"
    plan = CompositeKnowledgePlanCompiler().compile(
        question,
        "fund-structure-execution",
        _intent(
            question,
            [
                (
                    KnowledgeCapability.FUND_STRUCTURE_STRATEGY_TREND,
                    "국민성장펀드",
                    "구조",
                )
            ],
            [ProductFamily.FUND],
            interaction_intent=InteractionIntent.EXPLAIN,
        ),
    )
    relation = TemporalRelationRecord(
        relation_id="rel-fund-structure",
        capability=KnowledgeCapability.FUND_STRUCTURE_STRATEGY_TREND,
        predicate="describes_fund_structure_trend",
        subject=_entity("fund-national-growth", TemporalEntityKind.FUND, "국민성장펀드"),
        object=_entity(
            "event-investment-structure",
            TemporalEntityKind.POLICY_EVENT,
            "투자구조",
        ),
        observed_at=date(2026, 5, 21),
        evidence=(
            _evidence(
                "ev-fund-structure",
                "fsc-policy",
                "https://www.fsc.go.kr/po010103/86950",
                "금융위원회",
                as_of=date(2026, 5, 21),
            ),
        ),
    )
    store = TemporalRelationStore(
        seal_temporal_relation_bundle((relation,), corpus_manifest_sha256=_SHA_B)
    )

    class _NoProductFacts:
        def fetch(self, product_keys, selection, requested_fields):
            assert product_keys == ()
            assert selection is None
            assert requested_fields == ()
            return ()

    execution = ExternalKnowledgeExecutor(
        relation_store=store,
        product_fact_provider=_NoProductFacts(),
        approved_product_ids=set(),
    ).execute(plan)

    assert execution.verification.passed
    assert execution.evidence.requirement_relations == {"need_1": ("rel-fund-structure",)}
