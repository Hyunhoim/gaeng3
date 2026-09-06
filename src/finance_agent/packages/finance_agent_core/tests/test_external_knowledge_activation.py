from __future__ import annotations

import hashlib
from datetime import date

import pytest

from finance_agent_core.agent.external_knowledge_router import (
    ExternalKnowledgeRouteDisposition,
    ExternalKnowledgeRouter,
)
from finance_agent_core.agent.external_search_planning import (
    resolve_explicit_candidate_ids,
)
from finance_agent_core.agent.intent_analysis import (
    IntentAnalysisDraft,
    holding_document_top_n_scope,
    sanitize_server_authorized_knowledge_draft,
)
from finance_agent_core.agent.knowledge_planning import (
    CompositeKnowledgePlanCompiler,
    CompositeKnowledgePlanningError,
)
from finance_agent_core.agent.source_selection import SourceSelectionSignals
from finance_agent_core.config.knowledge_coverage import (
    ApprovedKnowledgeCoverageManifest,
    KnowledgeCapability,
    KnowledgeCoverageEntry,
)
from finance_agent_core.config.source_registry import load_official_source_registry
from finance_agent_core.contracts.external_knowledge import (
    KnowledgeIntentRequirement,
    KnowledgeTimeRange,
    ProvidedProductCandidate,
    ProvidedProductCandidateUniverse,
)
from finance_agent_core.contracts.queryplan import ProductFamily
from finance_agent_core.contracts.routing import InteractionIntent
from finance_agent_core.release import ExternalKnowledgeArtifactRelease
from finance_agent_core.retrieval.external_knowledge_release import (
    build_live_external_knowledge_bootstrap,
)
from finance_agent_core.retrieval.official_connector import OfficialSourceSnapshot
from finance_agent_core.retrieval.official_live_evidence import (
    VerifiedLiveRelationExtractor,
)
from finance_agent_core.storage.identity_cache import ProductIdentityRecord


def _intent(question: str, intent: InteractionIntent) -> IntentAnalysisDraft:
    return IntentAnalysisDraft(
        intent=intent,
        product_families=[ProductFamily.DOMESTIC_ETP],
        evidence_spans=[],
        needs_clarification=False,
        knowledge_requirements=[
            KnowledgeIntentRequirement(
                capability=KnowledgeCapability.PORTFOLIO_HOLDINGS,
                subject_span="에코프로비엠",
                relation_span="편입이 확인되는",
            )
        ],
    )


@pytest.mark.parametrize(
    "intent",
    [InteractionIntent.DETAIL, InteractionIntent.COMPARE],
)
def test_external_plan_preserves_detail_and_compare_intent(
    intent: InteractionIntent,
) -> None:
    question = "에코프로비엠 편입이 확인되는 국내 ETF를 비교해 주세요."

    plan = CompositeKnowledgePlanCompiler().compile(
        question,
        f"external-{intent.value}",
        _intent(question, intent),
    )

    assert plan.interaction_intent is intent


def test_external_plan_locks_requested_official_db_fields() -> None:
    question = "캠브리콘 편입 해외 ETF의 티커와 운용사, 투자지역, 최종 AUM과 위험등급을 알려줘."
    draft = IntentAnalysisDraft(
        intent=InteractionIntent.DETAIL,
        product_families=[ProductFamily.OVERSEAS_ETP],
        evidence_spans=[],
        needs_clarification=False,
        knowledge_requirements=[
            KnowledgeIntentRequirement(
                capability=KnowledgeCapability.PORTFOLIO_HOLDINGS,
                subject_span="캠브리콘",
                relation_span="편입",
            )
        ],
    )

    plan = CompositeKnowledgePlanCompiler().compile(
        question,
        "external-requested-fields",
        draft,
    )

    assert plan.product_selection is None
    assert plan.requested_product_fields == (
        "aum",
        "investment_region",
        "manager",
        "risk_level",
        "ticker",
    )


def test_server_drops_db_fields_and_repairs_only_explicit_theme_requirement() -> None:
    question = (
        "최근 6개월 사이 우주항공 테마로 출시·소개된 KODEX ETF의 위험등급과 최종 AUM을 알려줘."
    )
    draft = IntentAnalysisDraft(
        intent=InteractionIntent.SEARCH,
        product_families=[ProductFamily.DOMESTIC_ETP],
        evidence_spans=[],
        needs_clarification=False,
        knowledge_requirements=[
            KnowledgeIntentRequirement(
                capability=KnowledgeCapability.FUND_STRUCTURE_STRATEGY_TREND,
                subject_span="우주항공 테마",
                relation_span="출시·소개된",
            ),
            KnowledgeIntentRequirement(
                capability=KnowledgeCapability.PRODUCT_RISK_FACTORS,
                subject_span="KODEX ETF",
                relation_span="위험등급",
            ),
        ],
    )

    sanitized = sanitize_server_authorized_knowledge_draft(question, draft)

    assert len(sanitized.knowledge_requirements) == 1
    requirement = sanitized.knowledge_requirements[0]
    assert requirement.capability is KnowledgeCapability.SECTOR_THEME_HISTORY
    assert requirement.subject_span == "우주항공 테마"
    assert requirement.relation_span == "우주항공 테마"


@pytest.mark.parametrize(
    ("question", "capability", "subject", "relation"),
    [
        (
            "운용전략이 실물복제인 국내 ETF를 추적오차율이 낮은 순으로 보여 주세요.",
            KnowledgeCapability.FUND_STRUCTURE_STRATEGY_TREND,
            "운용전략",
            "운용전략",
        ),
        (
            "국내 ETF KR7429760002의 운용전략을 확인하고 싶어요.",
            KnowledgeCapability.FUND_STRUCTURE_STRATEGY_TREND,
            "운용전략",
            "운용전략",
        ),
        (
            "퇴직연금 편입 가능 채권을 5개 보여 주세요.",
            KnowledgeCapability.PORTFOLIO_HOLDINGS,
            "채권",
            "편입",
        ),
        (
            "연금 편입이 가능한 국내 ETF를 AUM 순으로 보여 주세요.",
            KnowledgeCapability.PORTFOLIO_HOLDINGS,
            "국내 ETF",
            "편입이 가능",
        ),
        (
            "KODEX 2차전지산업의 AUM을 알려 주세요.",
            KnowledgeCapability.SECTOR_THEME_HISTORY,
            "2차전지산업",
            "2차전지산업",
        ),
    ],
)
def test_provided_db_phrases_cannot_open_external_search_authority(
    question: str,
    capability: KnowledgeCapability,
    subject: str,
    relation: str,
) -> None:
    draft = IntentAnalysisDraft(
        intent=InteractionIntent.SEARCH,
        product_families=[ProductFamily.DOMESTIC_ETP],
        evidence_spans=[],
        needs_clarification=False,
        knowledge_requirements=[
            KnowledgeIntentRequirement(
                capability=capability,
                subject_span=subject,
                relation_span=relation,
            )
        ],
    )

    sanitized = sanitize_server_authorized_knowledge_draft(question, draft)

    assert sanitized.knowledge_requirements == []


def test_server_recovers_explicit_fund_structure_from_hclx_unsupported() -> None:
    question = (
        "미래에셋국민성장혼합자산-사모재간접C의 제공 상품 데이터와 함께, "
        "국민참여형 국민성장펀드의 구조와 2026년 5~6월 판매 동향을 정리해 주세요."
    )
    draft = IntentAnalysisDraft(
        intent=InteractionIntent.UNSUPPORTED,
        product_families=[],
        evidence_spans=[],
        needs_clarification=False,
        knowledge_requirements=[],
    )

    sanitized = sanitize_server_authorized_knowledge_draft(
        question,
        draft,
        fallback_families=(ProductFamily.FUND,),
        product_mentions=("미래에셋국민성장혼합자산-사모재간접C",),
    )

    assert sanitized.intent is InteractionIntent.SEARCH
    assert sanitized.product_families == [ProductFamily.FUND]
    assert [item.capability for item in sanitized.knowledge_requirements] == [
        KnowledgeCapability.FUND_STRUCTURE_STRATEGY_TREND,
        KnowledgeCapability.FUND_STRUCTURE_STRATEGY_TREND
    ]
    assert [item.relation_span for item in sanitized.knowledge_requirements] == [
        "구조",
        "판매 동향",
    ]


def test_server_lexical_family_scope_overrides_hclx_external_guess() -> None:
    question = "캠브리콘이 편입된 중국 반도체 해외 ETF를 알려줘."
    draft = IntentAnalysisDraft(
        intent=InteractionIntent.SEARCH,
        product_families=[ProductFamily.DOMESTIC_ETP],
        evidence_spans=[],
        needs_clarification=False,
        knowledge_requirements=[],
    )

    sanitized = sanitize_server_authorized_knowledge_draft(
        question,
        draft,
        fallback_families=(ProductFamily.OVERSEAS_ETP,),
    )

    assert sanitized.product_families == [ProductFamily.OVERSEAS_ETP]
    assert [item.capability for item in sanitized.knowledge_requirements] == [
        KnowledgeCapability.PORTFOLIO_HOLDINGS
    ]


def test_compiler_drops_only_atomic_holding_relation_restatement() -> None:
    question = "캠브리콘이 편입된 중국 반도체 해외 ETF를 알려줘."
    draft = IntentAnalysisDraft(
        intent=InteractionIntent.SEARCH,
        product_families=[ProductFamily.OVERSEAS_ETP],
        evidence_spans=[],
        needs_clarification=False,
        knowledge_requirements=[
            KnowledgeIntentRequirement(
                capability=KnowledgeCapability.PORTFOLIO_HOLDINGS,
                subject_span="캠브리콘",
                relation_span="편입된",
            ),
            KnowledgeIntentRequirement(
                capability=KnowledgeCapability.PORTFOLIO_HOLDINGS,
                subject_span="캠브리콘이 편입된",
                relation_span="캠브리콘이 편입된",
            ),
        ],
    )

    plan = CompositeKnowledgePlanCompiler().compile(question, "holding-restatement", draft)

    assert [item.subject_evidence_span for item in plan.requirements] == ["캠브리콘"]


def test_compiler_keeps_distinct_company_whose_name_contains_another() -> None:
    question = "한국전력과 한국전력기술이 편입된 해외 ETF를 알려줘."
    draft = IntentAnalysisDraft(
        intent=InteractionIntent.SEARCH,
        product_families=[ProductFamily.OVERSEAS_ETP],
        evidence_spans=[],
        needs_clarification=False,
        knowledge_requirements=[
            KnowledgeIntentRequirement(
                capability=KnowledgeCapability.PORTFOLIO_HOLDINGS,
                subject_span="한국전력",
                relation_span="편입된",
            ),
            KnowledgeIntentRequirement(
                capability=KnowledgeCapability.PORTFOLIO_HOLDINGS,
                subject_span="한국전력기술",
                relation_span="편입된",
            ),
        ],
    )

    plan = CompositeKnowledgePlanCompiler().compile(question, "holding-distinct-company", draft)

    assert [item.subject_evidence_span for item in plan.requirements] == [
        "한국전력",
        "한국전력기술",
    ]


def test_bare_etf_external_scope_keeps_both_provided_etp_families() -> None:
    question = "최근 6개월 동안 우주항공 테마와 연결 이력이 있는 ETF를 정리해줘."
    draft = IntentAnalysisDraft(
        intent=InteractionIntent.SEARCH,
        product_families=[ProductFamily.OVERSEAS_ETP],
        evidence_spans=[],
        needs_clarification=False,
        knowledge_requirements=[],
    )

    # The lexical Router currently uses domestic ETP as the default for a bare
    # ``ETF``.  External evidence must still preserve the generic two-market
    # scope instead of inheriting that implementation default.
    sanitized = sanitize_server_authorized_knowledge_draft(
        question,
        draft,
        fallback_families=(ProductFamily.DOMESTIC_ETP,),
    )

    assert sanitized.product_families == [
        ProductFamily.DOMESTIC_ETP,
        ProductFamily.OVERSEAS_ETP,
    ]
    assert [item.capability for item in sanitized.knowledge_requirements] == [
        KnowledgeCapability.SECTOR_THEME_HISTORY
    ]


def test_server_builds_explicit_subsidiary_holding_and_risk_chain() -> None:
    question = (
        "2026년 7월 공식 구성종목에서 에코프로 계열사인 에코프로비엠 편입이 "
        "확인되는 KODEX 2차전지산업과 KODEX 2차전지핵심소재10 중, 최종 AUM이 "
        "더 큰 상품 하나의 위험등급과 핵심 위험요인을 설명해 주세요."
    )
    draft = IntentAnalysisDraft(
        intent=InteractionIntent.SEARCH,
        product_families=[ProductFamily.DOMESTIC_ETP],
        evidence_spans=[],
        needs_clarification=False,
        knowledge_requirements=[],
    )

    sanitized = sanitize_server_authorized_knowledge_draft(
        question,
        draft,
        product_mentions=("KODEX 2차전지산업과 KODEX 2차전지핵심소재10 중",),
    )
    plan = CompositeKnowledgePlanCompiler().compile(
        question,
        "external-multihop-chain",
        sanitized,
        source_selection_signals=SourceSelectionSignals(
            product_families=(ProductFamily.DOMESTIC_ETP,),
            db_confirmed_aliases=("KODEX",),
            matched_product_names=(
                "KODEX 2차전지산업",
                "KODEX 2차전지핵심소재10",
            ),
            matched_source_owners=("삼성자산운용",),
            explicit_identity_bound=True,
        ),
    )

    assert [item.capability for item in plan.requirements] == [
        KnowledgeCapability.CORPORATE_SUBSIDIARY,
        KnowledgeCapability.PORTFOLIO_HOLDINGS,
        KnowledgeCapability.PRODUCT_RISK_FACTORS,
    ]
    assert plan.requirements[1].depends_on == ("need_1",)
    assert plan.requirements[2].depends_on == ("need_2",)
    assert plan.requirements[0].source_registry_ids == (
        "ecopro-corporate",
        "ecopro-official-brochures",
        "kind-corporate-disclosures",
        "open-dart-disclosures",
    )
    assert plan.requirements[1].source_registry_ids == (
        "samsungfund-kodex-newsroom",
        "samsungfund-kodex-product",
        "samsungfund-kodex-product-mobile",
    )
    assert plan.requirements[2].source_registry_ids == (
        "samsungfund-kodex-newsroom",
        "samsungfund-kodex-product",
        "samsungfund-kodex-product-mobile",
    )
    assert plan.product_selection is not None
    assert plan.product_selection.rank_field == "aum"
    assert plan.product_selection.limit == 1
    assert plan.requested_product_fields == ("aum", "risk_level")
    assert plan.requirements[1].time_range == KnowledgeTimeRange(
        start=date(2026, 7, 1),
        end=date(2026, 7, 31),
    )


def test_source_selection_uses_registry_and_db_signals_without_entity_policy() -> None:
    question = "TIGER 반도체 ETF의 구성종목과 위험요인을 알려줘."
    draft = IntentAnalysisDraft(
        intent=InteractionIntent.DETAIL,
        product_families=[ProductFamily.DOMESTIC_ETP],
        evidence_spans=[],
        needs_clarification=False,
        knowledge_requirements=[
            KnowledgeIntentRequirement(
                capability=KnowledgeCapability.PORTFOLIO_HOLDINGS,
                subject_span="TIGER 반도체 ETF",
                relation_span="구성종목",
            )
        ],
    )

    plan = CompositeKnowledgePlanCompiler().compile(
        question,
        "generalized-source-selection",
        draft,
        source_selection_signals=SourceSelectionSignals(
            product_families=(ProductFamily.DOMESTIC_ETP,),
            db_confirmed_aliases=("TIGER",),
            matched_product_names=("TIGER 반도체",),
            matched_source_owners=("미래에셋자산운용",),
            explicit_identity_bound=True,
        ),
    )

    assert plan.requirements[0].source_registry_ids == (
        "miraeasset-investments",
        "miraeasset-investments-prod",
        "miraeasset-tiger-legacy-pdf",
    )


def test_source_selection_accepts_bounded_subject_prefix_of_registry_alias() -> None:
    question = "Moonshot 관련 해외 ETF를 찾아 주세요."
    draft = IntentAnalysisDraft(
        intent=InteractionIntent.SEARCH,
        product_families=[ProductFamily.OVERSEAS_ETP],
        evidence_spans=[],
        needs_clarification=False,
        knowledge_requirements=[
            KnowledgeIntentRequirement(
                capability=KnowledgeCapability.SECTOR_THEME_HISTORY,
                subject_span="Moonshot",
                relation_span="관련 해외 ETF",
            )
        ],
    )

    plan = CompositeKnowledgePlanCompiler().compile(
        question,
        "generalized-source-prefix",
        draft,
        source_selection_signals=SourceSelectionSignals(
            product_families=(ProductFamily.OVERSEAS_ETP,),
        ),
    )

    assert plan.requirements[0].source_registry_ids == ("moonshot-ai-official-forum",)


def test_composite_compiler_rejects_a_loose_relation_without_theme_context() -> None:
    question = "ALFA ETF와 연결 이력이 있는 상품을 찾아줘."
    draft = IntentAnalysisDraft(
        intent=InteractionIntent.SEARCH,
        product_families=[ProductFamily.DOMESTIC_ETP],
        evidence_spans=[],
        needs_clarification=False,
        knowledge_requirements=[
            KnowledgeIntentRequirement(
                capability=KnowledgeCapability.SECTOR_THEME_HISTORY,
                subject_span="ALFA ETF",
                relation_span="연결 이력이 있는",
            )
        ],
    )

    with pytest.raises(
        CompositeKnowledgePlanningError,
        match="requires a theme, sector, or industry cue",
    ):
        CompositeKnowledgePlanCompiler().compile(question, "theme-defense", draft)


def test_hclx_cannot_invent_subject_but_can_propose_bounded_cross_script_alias() -> None:
    question = (
        "2026년 6월 말 KraneShares 공식 보유내역에서 캠브리콘 편입이 확인되는 "
        "중국 반도체 해외 ETF를 찾아 주세요."
    )
    draft = IntentAnalysisDraft(
        intent=InteractionIntent.SEARCH,
        product_families=[ProductFamily.OVERSEAS_ETP],
        evidence_spans=[],
        needs_clarification=False,
        knowledge_requirements=[
            KnowledgeIntentRequirement(
                capability=KnowledgeCapability.PORTFOLIO_HOLDINGS,
                subject_span="공식",
                relation_span="보유",
            ),
            KnowledgeIntentRequirement(
                capability=KnowledgeCapability.PORTFOLIO_HOLDINGS,
                subject_span="캠브리콘",
                relation_span="편입",
                search_aliases=("Cambricon",),
            ),
        ],
    )

    sanitized = sanitize_server_authorized_knowledge_draft(question, draft)

    assert len(sanitized.knowledge_requirements) == 1
    assert sanitized.knowledge_requirements[0].subject_span == "캠브리콘"
    assert sanitized.knowledge_requirements[0].search_aliases == ("Cambricon",)


def test_ranked_holding_position_compiles_only_with_typed_ordinal_scope() -> None:
    question = (
        "세 공식 팩트시트에서 상위 종목을 확인하고 Apple·삼성전자 중 하나 이상이 "
        "상위 10개에 들어 있는 ETF만 추려 주세요."
    )
    draft = IntentAnalysisDraft(
        intent=InteractionIntent.SEARCH,
        product_families=[
            ProductFamily.DOMESTIC_ETP,
            ProductFamily.OVERSEAS_ETP,
        ],
        evidence_spans=[],
        needs_clarification=False,
        knowledge_requirements=[
            KnowledgeIntentRequirement(
                capability=KnowledgeCapability.PORTFOLIO_HOLDINGS,
                subject_span="Apple·삼성전자",
                relation_span="상위 10개에 들어 있는",
            )
        ],
    )

    sanitized = sanitize_server_authorized_knowledge_draft(question, draft)

    assert [item.subject_span for item in sanitized.knowledge_requirements] == [
        "Apple",
        "삼성전자",
    ]
    plan = CompositeKnowledgePlanCompiler().compile(
        question,
        "ranked-holding-scope",
        sanitized,
    )
    assert all(
        requirement.document_scope is not None
        and requirement.document_scope.limit == 10
        and requirement.document_scope.selection_evidence_span in question
        for requirement in plan.requirements
    )


@pytest.mark.parametrize(
    "question",
    [
        "공식 팩트시트의 상위 10개 구성표를 대조해 Aurora Corp를 담은 ETF를 찾아줘.",
        "운용사 팩트시트 상위 10개 표를 모두 확인해서 Aurora Corp가 포함됐는지 알려줘.",
        "두 공식 가이드북의 상위 10개에서 공통 종목을 구해 ETF를 역으로 찾아줘.",
    ],
)
def test_document_top_n_parser_accepts_general_table_surfaces(question: str) -> None:
    assert holding_document_top_n_scope(question) is not None
    assert holding_document_top_n_scope(question)[0] == 10


@pytest.mark.parametrize(
    "question",
    [
        "공식 팩트시트에서 삼성전자를 보유한 ETF 중 AUM 상위 10개에 들어 있는 상품",
        "공식 문서와 대조한 ETF 가운데 1년 수익률 상위 10개에 포함된 상품",
    ],
)
def test_document_top_n_parser_rejects_product_ranking_surfaces(question: str) -> None:
    assert holding_document_top_n_scope(question) is None


def test_document_top_n_parser_accepts_named_membership_question() -> None:
    question = (
        "운용사가 공개한 2분기 가이드에서 상품 설명을 확인해 주세요. "
        "같은 자료의 상위 10개에 알파전자와 베타소재 두 종목이 모두 있는지 검증해 주세요."
    )

    scope = holding_document_top_n_scope(question)

    assert scope is not None
    assert scope[0] == 10
    assert scope[1] == "상위 10개에"

    draft = IntentAnalysisDraft(
        intent=InteractionIntent.SEARCH,
        product_families=[ProductFamily.DOMESTIC_ETP],
        evidence_spans=[],
        needs_clarification=False,
        knowledge_requirements=[],
    )
    sanitized = sanitize_server_authorized_knowledge_draft(question, draft)
    holdings = [
        item
        for item in sanitized.knowledge_requirements
        if item.capability is KnowledgeCapability.PORTFOLIO_HOLDINGS
    ]
    assert [item.subject_span for item in holdings] == ["알파전자", "베타소재"]
    assert len({item.relation_span for item in holdings}) == 1
    plan = CompositeKnowledgePlanCompiler().compile(
        question,
        "named-document-membership",
        sanitized,
    )
    assert all(
        item.document_scope is not None and item.document_scope.limit == 10
        for item in plan.requirements
        if item.capability is KnowledgeCapability.PORTFOLIO_HOLDINGS
    )


@pytest.mark.parametrize(
    "entity_list",
    [
        "알파전자, 베타소재",
        "알파전자，베타소재",
        "알파전자 및 베타소재",
        "알파전자와 베타소재",
    ],
)
def test_document_membership_list_separators_share_one_authority(
    entity_list: str,
) -> None:
    question = (
        "운용사 공식 가이드에서 상품 설명을 확인하고, 같은 자료의 "
        f"상위 10개에 {entity_list} 두 종목이 모두 있는지 검증해 주세요."
    )
    draft = IntentAnalysisDraft(
        intent=InteractionIntent.SEARCH,
        product_families=[ProductFamily.DOMESTIC_ETP],
        evidence_spans=[],
        needs_clarification=False,
        knowledge_requirements=[],
    )

    sanitized = sanitize_server_authorized_knowledge_draft(question, draft)
    holdings = [
        item
        for item in sanitized.knowledge_requirements
        if item.capability is KnowledgeCapability.PORTFOLIO_HOLDINGS
    ]

    assert [item.subject_span for item in holdings] == ["알파전자", "베타소재"]
    assert all(item.relation_span in question for item in holdings)


def test_named_membership_wording_does_not_reclassify_product_metric_ranking() -> None:
    question = (
        "공식 가이드와 대조한 국내 ETF 중 AUM 상위 10개에 "
        "알파 ETF와 베타 ETF 두 상품이 모두 있는지 알려 주세요."
    )

    assert holding_document_top_n_scope(question) is None
    draft = IntentAnalysisDraft(
        intent=InteractionIntent.SEARCH,
        product_families=[ProductFamily.DOMESTIC_ETP],
        evidence_spans=[],
        needs_clarification=False,
        knowledge_requirements=[],
    )
    sanitized = sanitize_server_authorized_knowledge_draft(question, draft)
    assert all(
        item.capability is not KnowledgeCapability.PORTFOLIO_HOLDINGS
        for item in sanitized.knowledge_requirements
    )


def test_dependent_risk_requirement_inherits_cross_family_relation_scope() -> None:
    question = (
        "공식 구성표에서 Aurora Corp가 들어 있는 국내·해외 ETF를 찾고 "
        "최종 후보별 손실 관련 유의점을 알려줘."
    )
    draft = IntentAnalysisDraft(
        intent=InteractionIntent.SEARCH,
        product_families=[ProductFamily.DOMESTIC_ETP, ProductFamily.OVERSEAS_ETP],
        evidence_spans=[],
        needs_clarification=False,
        knowledge_requirements=[
            KnowledgeIntentRequirement(
                capability=KnowledgeCapability.PORTFOLIO_HOLDINGS,
                subject_span="Aurora Corp",
                relation_span="들어 있는",
            ),
            KnowledgeIntentRequirement(
                capability=KnowledgeCapability.PRODUCT_RISK_FACTORS,
                subject_span="해외 ETF",
                relation_span="손실 관련 유의점",
            ),
        ],
    )

    plan = CompositeKnowledgePlanCompiler().compile(question, "risk-family-union", draft)
    risk = next(
        item
        for item in plan.requirements
        if item.capability is KnowledgeCapability.PRODUCT_RISK_FACTORS
    )
    assert set(risk.product_families) == {
        ProductFamily.DOMESTIC_ETP,
        ProductFamily.OVERSEAS_ETP,
    }


def test_confirm_all_table_boundary_preserves_one_hyphenated_entity() -> None:
    question = (
        "두 공식 팩트시트의 상위 10개 표를 모두 확인해서 "
        "Northstar Holdings Inc-adr가 포함된 ETF가 있는지 찾아줘."
    )
    draft = IntentAnalysisDraft(
        intent=InteractionIntent.SEARCH,
        product_families=[ProductFamily.DOMESTIC_ETP, ProductFamily.OVERSEAS_ETP],
        evidence_spans=[],
        needs_clarification=False,
        knowledge_requirements=[],
    )

    sanitized = sanitize_server_authorized_knowledge_draft(question, draft)
    subjects = [
        item.subject_span
        for item in sanitized.knowledge_requirements
        if item.capability is KnowledgeCapability.PORTFOLIO_HOLDINGS
    ]
    assert subjects == ["Northstar Holdings Inc-adr"]


def test_server_parser_separates_source_products_from_target_holdings() -> None:
    question = (
        "ALFA, BETA 두 상품의 2025-03-31 기준 공식 상위 10개 편입 종목을 보면 "
        "Gamma Corp, Delta Inc 두 종목이 모두 들어 있는 ETF를 찾아줘."
    )
    draft = IntentAnalysisDraft(
        intent=InteractionIntent.SEARCH,
        product_families=[ProductFamily.OVERSEAS_ETP],
        evidence_spans=[],
        needs_clarification=False,
        knowledge_requirements=[],
    )

    sanitized = sanitize_server_authorized_knowledge_draft(
        question,
        draft,
        product_mentions=("ALFA", "BETA"),
    )

    assert [
        item.subject_span for item in sanitized.knowledge_requirements
        if item.capability is KnowledgeCapability.PORTFOLIO_HOLDINGS
    ] == ["Gamma Corp", "Delta Inc"]


def test_source_product_role_does_not_cross_a_sentence_boundary() -> None:
    question = (
        "ALFA ETF를 비교해줘. "
        "BETA 공식 편입표에서 ALFA를 담은 ETF를 찾아줘."
    )
    draft = IntentAnalysisDraft(
        intent=InteractionIntent.SEARCH,
        product_families=[ProductFamily.OVERSEAS_ETP],
        evidence_spans=[],
        needs_clarification=False,
        knowledge_requirements=[],
    )

    sanitized = sanitize_server_authorized_knowledge_draft(
        question,
        draft,
        product_mentions=("ALFA", "BETA"),
    )

    assert any(
        item.capability is KnowledgeCapability.PORTFOLIO_HOLDINGS
        and item.subject_span == "ALFA"
        for item in sanitized.knowledge_requirements
    )


@pytest.mark.parametrize(
    "question",
    [
        "새희망펀드가 어떤 형태로 설계되어 있는 상품인지 설명해줘.",
        "새희망펀드가 어떤 방식으로 조성된 상품인지 알려줘.",
    ],
)
def test_named_fund_structure_paraphrases_are_server_recovered(question: str) -> None:
    draft = IntentAnalysisDraft(
        intent=InteractionIntent.UNSUPPORTED,
        product_families=[],
        evidence_spans=[],
        needs_clarification=False,
        knowledge_requirements=[],
    )

    sanitized = sanitize_server_authorized_knowledge_draft(
        question,
        draft,
        fallback_families=(ProductFamily.FUND,),
        product_mentions=("새희망펀드",),
    )

    assert sanitized.intent is InteractionIntent.SEARCH
    assert [item.capability for item in sanitized.knowledge_requirements] == [
        KnowledgeCapability.FUND_STRUCTURE_STRATEGY_TREND
    ]
    plan = CompositeKnowledgePlanCompiler().compile(
        question,
        "fund-structure-paraphrase",
        sanitized,
    )
    assert [item.capability for item in plan.requirements] == [
        KnowledgeCapability.FUND_STRUCTURE_STRATEGY_TREND
    ]


def test_fund_structure_hcx_proposal_cannot_reclassify_an_etf_profile() -> None:
    question = "ALPHA 200 ETF는 어떤 방식으로 구성된 상품인지 알려줘."
    draft = IntentAnalysisDraft(
        intent=InteractionIntent.SEARCH,
        product_families=[ProductFamily.DOMESTIC_ETP],
        evidence_spans=[],
        needs_clarification=False,
        knowledge_requirements=[
            KnowledgeIntentRequirement(
                capability=KnowledgeCapability.FUND_STRUCTURE_STRATEGY_TREND,
                subject_span="ALPHA 200 ETF",
                relation_span="어떤 방식으로 구성된",
            )
        ],
    )

    sanitized = sanitize_server_authorized_knowledge_draft(
        question,
        draft,
        fallback_families=(ProductFamily.DOMESTIC_ETP,),
        product_mentions=("ALPHA 200 ETF",),
    )

    assert all(
        item.capability is not KnowledgeCapability.FUND_STRUCTURE_STRATEGY_TREND
        for item in sanitized.knowledge_requirements
    )


@pytest.mark.parametrize(
    ("question", "fallback", "expected"),
    [
        (
            "회사 A를 보유한 ETF와 공모펀드를 알려줘",
            (ProductFamily.DOMESTIC_ETP, ProductFamily.OVERSEAS_ETP, ProductFamily.FUND),
            {
                ProductFamily.DOMESTIC_ETP,
                ProductFamily.OVERSEAS_ETP,
                ProductFamily.FUND,
            },
        ),
        (
            "회사 A를 보유한 ETF와 채권을 알려줘",
            (ProductFamily.DOMESTIC_ETP, ProductFamily.BOND),
            {
                ProductFamily.BOND,
                ProductFamily.DOMESTIC_ETP,
                ProductFamily.OVERSEAS_ETP,
            },
        ),
        (
            "회사 A를 보유한 ETF를 알려줘",
            (ProductFamily.DOMESTIC_ETP,),
            {ProductFamily.DOMESTIC_ETP, ProductFamily.OVERSEAS_ETP},
        ),
        (
            "회사 A를 보유한 국내 ETF와 공모펀드를 알려줘",
            (ProductFamily.DOMESTIC_ETP, ProductFamily.FUND),
            {ProductFamily.DOMESTIC_ETP, ProductFamily.FUND},
        ),
    ],
)
def test_generic_etp_expansion_preserves_other_explicit_product_families(
    question: str,
    fallback: tuple[ProductFamily, ...],
    expected: set[ProductFamily],
) -> None:
    draft = IntentAnalysisDraft(
        intent=InteractionIntent.SEARCH,
        product_families=list(fallback),
        evidence_spans=[],
        needs_clarification=False,
        knowledge_requirements=[],
    )

    sanitized = sanitize_server_authorized_knowledge_draft(
        question,
        draft,
        fallback_families=fallback,
    )

    assert set(sanitized.product_families) == expected


def test_related_etp_parser_does_not_promote_connective_as_theme_subject() -> None:
    question = "최근 6개월 동안 우주항공 테마와 연결 이력이 있는 관련 ETF를 정리해줘."
    draft = IntentAnalysisDraft(
        intent=InteractionIntent.SEARCH,
        product_families=[
            ProductFamily.DOMESTIC_ETP,
            ProductFamily.OVERSEAS_ETP,
        ],
        evidence_spans=[],
        needs_clarification=False,
        knowledge_requirements=[],
    )

    sanitized = sanitize_server_authorized_knowledge_draft(question, draft)

    assert [(item.capability, item.subject_span) for item in sanitized.knowledge_requirements] == [
        (KnowledgeCapability.SECTOR_THEME_HISTORY, "우주항공 테마")
    ]


def test_generic_related_investment_product_search_keeps_all_provided_families() -> None:
    question = "Kimi 관련 투자 상품 있어?"
    draft = IntentAnalysisDraft(
        intent=InteractionIntent.UNSUPPORTED,
        product_families=[],
        evidence_spans=["Kimi"],
        needs_clarification=False,
        knowledge_requirements=[],
    )

    sanitized = sanitize_server_authorized_knowledge_draft(question, draft)

    assert sanitized.intent is InteractionIntent.SEARCH
    assert sanitized.product_families == list(ProductFamily)
    assert [(item.capability, item.subject_span) for item in sanitized.knowledge_requirements] == [
        (KnowledgeCapability.SECTOR_THEME_HISTORY, "Kimi")
    ]
    plan = CompositeKnowledgePlanCompiler().compile(
        question,
        "generic-related-product",
        sanitized,
    )
    assert plan.requirements[0].subject == "Kimi"
    assert plan.requirements[0].predicate == "theme_related"


def test_hclx_cross_script_alias_rejects_generic_or_non_ascii_fact_text() -> None:
    question = "캠브리콘이 편입된 해외 ETF를 찾아 주세요."
    draft = IntentAnalysisDraft(
        intent=InteractionIntent.SEARCH,
        product_families=[ProductFamily.OVERSEAS_ETP],
        evidence_spans=[],
        needs_clarification=False,
        knowledge_requirements=[
            KnowledgeIntentRequirement(
                capability=KnowledgeCapability.PORTFOLIO_HOLDINGS,
                subject_span="캠브리콘",
                relation_span="편입된",
                search_aliases=("ETF", "다른 회사", "Cambricon Technologies"),
            )
        ],
    )

    sanitized = sanitize_server_authorized_knowledge_draft(question, draft)

    assert sanitized.knowledge_requirements[0].search_aliases == ("Cambricon Technologies",)


def test_live_official_coverage_authorizes_external_execution_without_fake_index() -> None:
    coverage = ApprovedKnowledgeCoverageManifest(
        schema_version="1.0",
        manifest_id="test-live-official",
        dataset_release_id="test-dataset",
        capabilities=tuple(
            KnowledgeCoverageEntry(
                capability=capability,
                status="approved_live_official",
                runtime_execution_enabled=True,
                allowed_source_kinds=("provided", "external_approved"),
            )
            for capability in KnowledgeCapability
        ),
    )
    question = "에코프로비엠 편입이 확인되는 국내 ETF를 비교해 주세요."

    decision = ExternalKnowledgeRouter(coverage=coverage).route(
        question,
        "external-live",
        _intent(question, InteractionIntent.COMPARE),
    )

    assert decision.disposition is ExternalKnowledgeRouteDisposition.EXECUTE
    assert decision.plan is not None and decision.plan.execution_authorized


def test_live_official_release_does_not_claim_bm25_or_document_dense() -> None:
    artifact = ExternalKnowledgeArtifactRelease(
        retrieval_mode="verified_live_official",
        source_registry_sha256="a" * 64,
        coverage_manifest_sha256="b" * 64,
        corpus_manifest_sha256="c" * 64,
        temporal_relation_sha256="d" * 64,
        verifier_contract_sha256="e" * 64,
        embedding_model_revision=None,
    )

    assert artifact.document_bm25_sha256 is None
    assert artifact.document_dense_sha256 is None

    with pytest.raises(ValueError):
        artifact.model_copy(
            update={"document_dense_sha256": "f" * 64},
        ).model_validate(
            artifact.model_copy(
                update={"document_dense_sha256": "f" * 64},
            ).model_dump(mode="python")
        )


def test_live_external_bootstrap_cannot_borrow_cache_only_release_authority() -> None:
    with pytest.raises(
        ValueError,
        match="every external capability must be approved for official live search",
    ):
        build_live_external_knowledge_bootstrap()


def test_explicit_product_identity_narrows_only_its_official_family() -> None:
    domestic = ProductIdentityRecord(
        product_family="domestic_etp",
        product_id="KR7123456789",
        product_name="KODEX 예시산업",
        ticker="123456",
        isin="KR7123456789",
        short_name="KODEX 예시산업",
        public_offering=None,
        is_quarantined=False,
    )
    overseas = ProductIdentityRecord(
        product_family="overseas_etp",
        product_id="EXAMPLE.O",
        product_name="Example Overseas ETF",
        ticker="EXAMPLE.O",
        isin="US0000000001",
        short_name=None,
        public_offering=None,
        is_quarantined=False,
    )

    resolved = resolve_explicit_candidate_ids(
        {
            ProductFamily.DOMESTIC_ETP: (domestic,),
            ProductFamily.OVERSEAS_ETP: (overseas,),
        },
        ("KODEX 예시산업", "에코프로비엠"),
    )

    assert resolved == {ProductFamily.DOMESTIC_ETP: {"KR7123456789"}}


def test_two_exact_short_names_are_resolved_inside_one_conjoined_surface() -> None:
    records = (
        ProductIdentityRecord(
            product_family="domestic_etp",
            product_id="KR7305720005",
            product_name="삼성 KODEX 2차전지산업증권상장지수투자신탁",
            ticker="305720",
            isin="KR7305720005",
            short_name="KODEX 2차전지산업",
            public_offering=None,
            is_quarantined=False,
        ),
        ProductIdentityRecord(
            product_family="domestic_etp",
            product_id="KR7461950008",
            product_name="삼성 KODEX 2차전지핵심소재10증권상장지수투자신탁",
            ticker="461950",
            isin="KR7461950008",
            short_name="KODEX 2차전지핵심소재10",
            public_offering=None,
            is_quarantined=False,
        ),
    )

    resolved = resolve_explicit_candidate_ids(
        {ProductFamily.DOMESTIC_ETP: records},
        ("KODEX 2차전지산업과 KODEX 2차전지핵심소재10 중",),
    )

    assert resolved == {ProductFamily.DOMESTIC_ETP: {"KR7305720005", "KR7461950008"}}


def test_generic_fund_surface_binds_only_db_names_with_the_same_long_core() -> None:
    records = (
        ProductIdentityRecord(
            product_family="fund",
            product_id="FUND-C",
            product_name="미래에셋국민참여형국민성장혼합자산투자신탁 종류C",
            ticker=None,
            isin=None,
            short_name="미래에셋국민성장혼합자산C",
            public_offering=True,
            is_quarantined=False,
        ),
        ProductIdentityRecord(
            product_family="fund",
            product_id="FUND-CE",
            product_name="미래에셋국민참여형국민성장혼합자산투자신탁 종류C-e",
            ticker=None,
            isin=None,
            short_name="미래에셋국민성장혼합자산Ce",
            public_offering=True,
            is_quarantined=False,
        ),
        ProductIdentityRecord(
            product_family="fund",
            product_id="OTHER",
            product_name="미래에셋다른성장혼합자산투자신탁",
            ticker=None,
            isin=None,
            short_name="미래에셋다른성장",
            public_offering=True,
            is_quarantined=False,
        ),
    )

    resolved = resolve_explicit_candidate_ids(
        {ProductFamily.FUND: records},
        ("국민성장펀드",),
    )

    assert resolved == {ProductFamily.FUND: {"FUND-C", "FUND-CE"}}


def test_exact_fund_short_name_outranks_a_broader_programme_core() -> None:
    records = (
        ProductIdentityRecord(
            product_family="fund",
            product_id="FUND-C",
            product_name="미래에셋국민참여형국민성장혼합자산투자신탁 종류C",
            ticker=None,
            isin=None,
            short_name="미래에셋국민성장혼합자산-사모재간접C",
            public_offering=True,
            is_quarantined=False,
        ),
        ProductIdentityRecord(
            product_family="fund",
            product_id="FUND-CE",
            product_name="미래에셋국민참여형국민성장혼합자산투자신탁 종류C-e",
            ticker=None,
            isin=None,
            short_name="미래에셋국민성장혼합자산-사모재간접Ce",
            public_offering=True,
            is_quarantined=False,
        ),
    )

    resolved = resolve_explicit_candidate_ids(
        {ProductFamily.FUND: records},
        ("미래에셋국민성장혼합자산-사모재간접C", "국민성장펀드"),
    )

    assert resolved == {ProductFamily.FUND: {"FUND-C"}}


def test_company_surface_does_not_narrow_products_by_reverse_substring() -> None:
    record = ProductIdentityRecord(
        product_family="domestic_etp",
        product_id="ETF-001",
        product_name="KODEX 에코프로밸류체인",
        ticker="000001",
        isin="KR7000000001",
        short_name="KODEX 에코프로밸류체인",
        public_offering=None,
        is_quarantined=False,
    )

    assert (
        resolve_explicit_candidate_ids(
            {ProductFamily.DOMESTIC_ETP: (record,)},
            ("에코프로",),
        )
        == {}
    )


def test_explicit_fund_core_can_bind_multiple_official_sale_classes() -> None:
    body = (
        "<html><body><article>국민성장펀드는 민간 자금과 정책 자금을 "
        "결합하는 구조이며 단계적으로 투자를 집행합니다.</article></body></html>"
    ).encode()
    registry = load_official_source_registry()
    source = registry.authorize_url(
        "https://www.fsc.go.kr/po010103/86950",
        KnowledgeCapability.FUND_STRUCTURE_STRATEGY_TREND,
    )
    snapshot = OfficialSourceSnapshot(
        source=source,
        capability=KnowledgeCapability.FUND_STRUCTURE_STRATEGY_TREND,
        source_uri="https://www.fsc.go.kr/po010103/86950",
        title="국민성장 프로젝트 구조 설명",
        content_type="text/html",
        content=body,
        content_sha256=hashlib.sha256(body).hexdigest(),
        published_at=None,
        as_of=date(2026, 8, 24),
    )
    requirement = (
        CompositeKnowledgePlanCompiler()
        .compile(
            "국민성장펀드의 구조와 투자 전략 동향을 알려줘.",
            "external-fund-policy",
            IntentAnalysisDraft(
                intent=InteractionIntent.DETAIL,
                product_families=[ProductFamily.FUND],
                evidence_spans=[],
                needs_clarification=False,
                knowledge_requirements=[
                    KnowledgeIntentRequirement(
                        capability=KnowledgeCapability.FUND_STRUCTURE_STRATEGY_TREND,
                        subject_span="국민성장펀드",
                        relation_span="구조와 투자 전략 동향",
                    )
                ],
            ),
        )
        .requirements[0]
    )
    universe = ProvidedProductCandidateUniverse(
        candidates=(
            ProvidedProductCandidate(
                product_family=ProductFamily.FUND,
                product_id="FUND-001",
                product_name="미래에셋국민성장혼합자산 일반형",
            ),
            ProvidedProductCandidate(
                product_family=ProductFamily.FUND,
                product_id="FUND-002",
                product_name="미래에셋국민성장혼합자산 서민형",
            ),
            ProvidedProductCandidate(
                product_family=ProductFamily.FUND,
                product_id="FUND-OTHER",
                product_name="미래에셋다른성장혼합자산 일반형",
            ),
        ),
        database_sha256s={ProductFamily.FUND: "a" * 64},
        candidate_filter_sha256="b" * 64,
        explicit_identity_bound=True,
    )

    relations = VerifiedLiveRelationExtractor().extract(snapshot, requirement, universe)

    assert {item.product_id for item in relations} == {"FUND-001", "FUND-002"}


def test_live_subsidiary_relation_requires_exact_parent_and_child_in_source() -> None:
    question = "에코프로 계열사인 에코프로비엠 편입 국내 ETF를 찾아줘."
    draft = IntentAnalysisDraft(
        intent=InteractionIntent.SEARCH,
        product_families=[ProductFamily.DOMESTIC_ETP],
        evidence_spans=[],
        needs_clarification=False,
        knowledge_requirements=[
            KnowledgeIntentRequirement(
                capability=KnowledgeCapability.CORPORATE_SUBSIDIARY,
                subject_span="에코프로",
                relation_span="계열사인",
            )
        ],
    )
    requirement = (
        CompositeKnowledgePlanCompiler()
        .compile(
            question,
            "external-subsidiary",
            draft,
        )
        .requirements[0]
    )
    body = (
        "<html><body><article>에코프로의 주요 계열사인 에코프로비엠은 "
        "양극소재 사업을 영위합니다.</article></body></html>"
    ).encode()
    registry = load_official_source_registry()
    source = registry.authorize_url(
        "https://www.ecopro.co.kr/sub010101/view",
        KnowledgeCapability.CORPORATE_SUBSIDIARY,
    )
    snapshot = OfficialSourceSnapshot(
        source=source,
        capability=KnowledgeCapability.CORPORATE_SUBSIDIARY,
        source_uri="https://www.ecopro.co.kr/sub010101/view",
        title="에코프로 주요 가족사",
        content_type="text/html",
        content=body,
        content_sha256=hashlib.sha256(body).hexdigest(),
        published_at=None,
        as_of=date(2026, 8, 24),
    )
    universe = ProvidedProductCandidateUniverse(
        candidates=(
            ProvidedProductCandidate(
                product_family=ProductFamily.DOMESTIC_ETP,
                product_id="KR7123456789",
                product_name="KODEX 예시산업",
            ),
        ),
        database_sha256s={ProductFamily.DOMESTIC_ETP: "a" * 64},
        candidate_filter_sha256="b" * 64,
    )

    relations = VerifiedLiveRelationExtractor().extract(
        snapshot,
        requirement,
        universe,
    )

    assert len(relations) == 1
    assert relations[0].subject.canonical_name == "에코프로"
    assert relations[0].object.canonical_name == "에코프로비엠"
    assert relations[0].product_id is None


def test_server_normalizes_particles_and_builds_parent_only_multihop_chain() -> None:
    question = "에코프로의 자회사를 편입한 ETF 중 순자산이 큰 상품의 위험요인을 알려줘."
    draft = IntentAnalysisDraft(
        intent=InteractionIntent.SEARCH,
        product_families=[ProductFamily.DOMESTIC_ETP],
        evidence_spans=[],
        needs_clarification=False,
        knowledge_requirements=[
            KnowledgeIntentRequirement(
                capability=KnowledgeCapability.CORPORATE_SUBSIDIARY,
                subject_span="자회사를",
                relation_span="자회사",
            ),
            KnowledgeIntentRequirement(
                capability=KnowledgeCapability.PORTFOLIO_HOLDINGS,
                subject_span="자회사를",
                relation_span="편입한",
            ),
        ],
    )

    sanitized = sanitize_server_authorized_knowledge_draft(question, draft)
    plan = CompositeKnowledgePlanCompiler().compile(
        question,
        "parent-only-chain",
        sanitized,
    )

    assert [item.capability for item in plan.requirements] == [
        KnowledgeCapability.CORPORATE_SUBSIDIARY,
        KnowledgeCapability.PORTFOLIO_HOLDINGS,
        KnowledgeCapability.PRODUCT_RISK_FACTORS,
    ]
    assert [item.subject for item in plan.requirements] == ["에코프로", "에코프로", "상품"]
    assert plan.requirements[1].depends_on == ("need_1",)
    assert plan.requirements[2].depends_on == ("need_2",)


def test_server_adds_only_literal_theme_base_as_document_search_alias() -> None:
    question = "최근 6개월 동안 우주항공 테마와 연결 이력이 있는 국내 ETF를 정리해줘."
    sanitized = sanitize_server_authorized_knowledge_draft(
        question,
        IntentAnalysisDraft(
            intent=InteractionIntent.SEARCH,
            product_families=[ProductFamily.DOMESTIC_ETP],
            evidence_spans=["우주항공 테마"],
            needs_clarification=False,
            knowledge_requirements=[],
        ),
    )

    requirement = next(
        item
        for item in sanitized.knowledge_requirements
        if item.capability is KnowledgeCapability.SECTOR_THEME_HISTORY
    )
    assert requirement.subject_span == "우주항공 테마"
    assert requirement.search_aliases == ("우주항공",)


def test_live_parent_only_subsidiary_extracts_exact_official_company_spans() -> None:
    question = "에코프로의 자회사를 편입한 국내 ETF를 알려줘."
    draft = sanitize_server_authorized_knowledge_draft(
        question,
        IntentAnalysisDraft(
            intent=InteractionIntent.SEARCH,
            product_families=[ProductFamily.DOMESTIC_ETP],
            evidence_spans=[],
            needs_clarification=False,
            knowledge_requirements=[],
        ),
    )
    requirement = (
        CompositeKnowledgePlanCompiler()
        .compile(
            question,
            "parent-only-extract",
            draft,
        )
        .requirements[0]
    )
    body = (
        "<html><body><article>2016년 에코프로비엠, 2021년 에코프로에이치엔을 "
        "각각 분할 설립하며 지주회사 체제로 전환한 에코프로입니다.</article></body></html>"
    ).encode()
    registry = load_official_source_registry()
    source = registry.authorize_url(
        "https://www.ecopro.co.kr/sub010101/view",
        KnowledgeCapability.CORPORATE_SUBSIDIARY,
    )
    snapshot = OfficialSourceSnapshot(
        source=source,
        capability=KnowledgeCapability.CORPORATE_SUBSIDIARY,
        source_uri="https://www.ecopro.co.kr/sub010101/view",
        title="에코프로 기업개요",
        content_type="text/html",
        content=body,
        content_sha256=hashlib.sha256(body).hexdigest(),
        published_at=None,
        as_of=date(2026, 8, 24),
    )
    universe = ProvidedProductCandidateUniverse(
        candidates=(
            ProvidedProductCandidate(
                product_family=ProductFamily.DOMESTIC_ETP,
                product_id="KR7123456789",
                product_name="KODEX 예시산업",
            ),
        ),
        database_sha256s={ProductFamily.DOMESTIC_ETP: "a" * 64},
        candidate_filter_sha256="b" * 64,
    )

    relations = VerifiedLiveRelationExtractor().extract(snapshot, requirement, universe)

    assert {item.object.canonical_name for item in relations} == {
        "에코프로비엠",
        "에코프로에이치엔",
    }
