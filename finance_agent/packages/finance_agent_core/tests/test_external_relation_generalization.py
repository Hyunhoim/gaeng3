from __future__ import annotations

import hashlib
import sqlite3
from datetime import date

import pytest

from finance_agent_core.agent.intent_analysis import (
    IntentAnalysisDraft,
    has_external_knowledge_surface,
    sanitize_server_authorized_knowledge_draft,
)
from finance_agent_core.agent.knowledge_planning import CompositeKnowledgePlanCompiler
from finance_agent_core.config import QualityStatus
from finance_agent_core.config.knowledge_coverage import KnowledgeCapability
from finance_agent_core.config.source_registry import load_official_source_registry
from finance_agent_core.contracts.external_knowledge import (
    CompositeKnowledgeQueryPlan,
    KnowledgeIntentRequirement,
    KnowledgeMatchMode,
    KnowledgeProductSelection,
    KnowledgeRequirement,
    KnowledgeTimeRange,
    ProvidedProductCandidateUniverse,
)
from finance_agent_core.contracts.queryplan import ProductFamily, SortDirection
from finance_agent_core.contracts.routing import InteractionIntent
from finance_agent_core.execution.external_knowledge_executor import (
    ExternalKnowledgeExecutor,
)
from finance_agent_core.execution.external_knowledge_verifier import OfficialProductFact
from finance_agent_core.execution.official_product_facts import (
    SqliteOfficialProductFactProvider,
)
from finance_agent_core.retrieval.official_connector import OfficialSourceSnapshot
from finance_agent_core.retrieval.official_live_evidence import (
    VerifiedLiveRelationExtractor,
)
from finance_agent_core.retrieval.temporal_relations import (
    ExternalEvidenceReference,
    TemporalEntity,
    TemporalEntityKind,
    TemporalRelationRecord,
    TemporalRelationStore,
    seal_temporal_relation_bundle,
)

_SHA_A = "a" * 64
_SHA_B = "b" * 64
_AS_OF = date(2026, 8, 23)
_RANK_UNITS = {
    "aum": "source_currency_amount",
    "total_expense_ratio_pct": "pct_point",
    "one_day_return_pct": "pct_point",
    "one_month_return_pct": "pct_point",
    "three_month_return_pct": "pct_point",
    "one_year_return_pct": "pct_point",
    "daily_volume": "source_quantity",
    "daily_trading_value": "source_currency_amount",
    "twenty_day_volatility_pct": "pct_point",
}


def _empty_external_draft(
    *families: ProductFamily,
) -> IntentAnalysisDraft:
    return IntentAnalysisDraft(
        intent=InteractionIntent.SEARCH,
        product_families=list(families),
        evidence_spans=[],
        needs_clarification=False,
        knowledge_requirements=[],
    )


def test_named_product_risk_does_not_invent_a_theme_requirement() -> None:
    question = "KODEX 반도체 국내 ETF의 핵심 위험 요인을 공식 문서에서 확인해 주세요."

    sanitized = sanitize_server_authorized_knowledge_draft(
        question,
        _empty_external_draft(ProductFamily.DOMESTIC_ETP),
    )

    assert [item.capability for item in sanitized.knowledge_requirements] == [
        KnowledgeCapability.PRODUCT_RISK_FACTORS
    ]
    assert sanitized.knowledge_requirements[0].subject_span == "KODEX 반도체"


def test_nested_corporate_descendant_holding_is_one_dependency_chain() -> None:
    question = (
        "오로라그룹의 자회사 가운데 네뷸라소재를 편입한 국내 ETF의 "
        "위험요인을 알려줘."
    )

    sanitized = sanitize_server_authorized_knowledge_draft(
        question,
        _empty_external_draft(ProductFamily.DOMESTIC_ETP),
    )
    plan = CompositeKnowledgePlanCompiler().compile(
        question,
        "nested-corporate-holding",
        sanitized,
    )

    assert [item.capability for item in plan.requirements] == [
        KnowledgeCapability.CORPORATE_SUBSIDIARY,
        KnowledgeCapability.PORTFOLIO_HOLDINGS,
        KnowledgeCapability.PRODUCT_RISK_FACTORS,
    ]
    assert plan.requirements[0].subject == "오로라그룹"
    assert plan.requirements[1].subject == "네뷸라소재"
    assert plan.requirements[1].depends_on == ("need_1",)
    assert plan.requirements[2].depends_on == ("need_2",)


def test_nested_corporate_selection_extracts_only_the_named_descendant() -> None:
    question = "오로라그룹의 자회사 가운데 네뷸라소재를 편입한 국내 ETF를 알려줘."
    sanitized = sanitize_server_authorized_knowledge_draft(
        question,
        _empty_external_draft(ProductFamily.DOMESTIC_ETP),
    )
    requirement = CompositeKnowledgePlanCompiler().compile(
        question,
        "named-corporate-descendant",
        sanitized,
    ).requirements[0]
    registry = load_official_source_registry()
    source_uri = "https://www.ecopro.co.kr/sub010101/view?id=synthetic"
    body = "오로라그룹의 자회사는 네뷸라소재입니다.".encode()
    snapshot = OfficialSourceSnapshot(
        source=registry.authorize_url(
            source_uri,
            KnowledgeCapability.CORPORATE_SUBSIDIARY,
        ),
        capability=KnowledgeCapability.CORPORATE_SUBSIDIARY,
        source_uri=source_uri,
        title="Synthetic official corporate profile",
        content_type="text/plain",
        content=body,
        content_sha256=hashlib.sha256(body).hexdigest(),
        published_at=_AS_OF,
        as_of=_AS_OF,
    )
    universe = ProvidedProductCandidateUniverse(
        candidates=(),
        database_sha256s={},
        candidate_filter_sha256=_SHA_B,
    )

    relations = VerifiedLiveRelationExtractor().extract(snapshot, requirement, universe)

    assert len(relations) == 1
    assert relations[0].subject.canonical_name == "오로라그룹"
    assert relations[0].object.canonical_name == "네뷸라소재"


def test_fund_structure_strategy_sales_schedule_and_trend_are_all_preserved() -> None:
    question = (
        "은하성장펀드의 구조와 투자 전략, 판매 일정과 최근 운용 동향을 "
        "근거와 함께 정리해줘."
    )

    sanitized = sanitize_server_authorized_knowledge_draft(
        question,
        _empty_external_draft(ProductFamily.FUND),
    )
    plan = CompositeKnowledgePlanCompiler().compile(
        question,
        "fund-four-relations",
        sanitized,
    )

    assert [item.relation_evidence_span for item in plan.requirements] == [
        "구조",
        "투자 전략",
        "판매 일정",
        "운용 동향",
    ]
    assert all(
        item.capability is KnowledgeCapability.FUND_STRUCTURE_STRATEGY_TREND
        for item in plan.requirements
    )


def test_dated_theme_plus_holding_does_not_promote_theme_clause_to_company() -> None:
    question = (
        "최근 4개월 동안 우주항공 테마와 연결 이력이 있고 "
        "위성기업을 편입한 ETF를 찾아줘."
    )

    sanitized = sanitize_server_authorized_knowledge_draft(
        question,
        _empty_external_draft(
            ProductFamily.DOMESTIC_ETP,
            ProductFamily.OVERSEAS_ETP,
        ),
    )
    plan = CompositeKnowledgePlanCompiler().compile(
        question,
        "dated-theme-holding",
        sanitized,
    )

    assert [(item.capability, item.subject) for item in plan.requirements] == [
        (KnowledgeCapability.PORTFOLIO_HOLDINGS, "위성기업"),
        (KnowledgeCapability.SECTOR_THEME_HISTORY, "우주항공 테마"),
    ]
    assert all(
        item.time_range
        == KnowledgeTimeRange(start=date(2026, 4, 24), end=date(2026, 8, 24))
        for item in plan.requirements
    )


@pytest.mark.parametrize(
    ("surface", "expected_start"),
    [
        ("지난 180일 사이", date(2026, 2, 25)),
        ("최근 두 분기 동안", date(2026, 2, 24)),
        ("최근 반년 동안", date(2026, 2, 24)),
        ("올해 1월 이후", date(2026, 1, 1)),
    ],
)
def test_temporal_relation_language_is_normalized_against_signed_cutoff(
    surface: str,
    expected_start: date,
) -> None:
    question = f"{surface} 우주산업과 연결된 국내 ETF를 정리해줘."
    sanitized = sanitize_server_authorized_knowledge_draft(
        question,
        _empty_external_draft(ProductFamily.DOMESTIC_ETP),
    )

    plan = CompositeKnowledgePlanCompiler().compile(question, surface, sanitized)

    assert plan.requirements
    assert all(
        item.time_range
        == KnowledgeTimeRange(start=expected_start, end=date(2026, 8, 24))
        for item in plan.requirements
    )


def test_corporate_holding_theme_and_risk_compound_keeps_every_requirement() -> None:
    question = (
        "네뷸라홀딩스의 자회사를 편입하고 로봇 테마와 연결된 ETF 중 "
        "순자산이 큰 상품의 위험요인을 알려줘."
    )

    sanitized = sanitize_server_authorized_knowledge_draft(
        question,
        _empty_external_draft(
            ProductFamily.DOMESTIC_ETP,
            ProductFamily.OVERSEAS_ETP,
        ),
    )
    plan = CompositeKnowledgePlanCompiler().compile(
        question,
        "corporate-theme-compound",
        sanitized,
    )

    assert [item.capability for item in plan.requirements] == [
        KnowledgeCapability.CORPORATE_SUBSIDIARY,
        KnowledgeCapability.PORTFOLIO_HOLDINGS,
        KnowledgeCapability.SECTOR_THEME_HISTORY,
        KnowledgeCapability.PRODUCT_RISK_FACTORS,
    ]
    assert plan.requirements[1].depends_on == ("need_1",)
    assert plan.requirements[3].depends_on == ("need_2", "need_3")
    assert plan.product_selection == KnowledgeProductSelection(
        rank_field="aum",
        direction=SortDirection.DESC,
        limit=1,
    )


@pytest.mark.parametrize(
    ("question", "expected"),
    [
        (
            "오로라그룹 산하 기업이 포함된 친환경 테마 ETF 중 위험요인을 알려줘.",
            [
                (KnowledgeCapability.CORPORATE_SUBSIDIARY, "오로라그룹"),
                (KnowledgeCapability.PORTFOLIO_HOLDINGS, "오로라그룹"),
                (KnowledgeCapability.SECTOR_THEME_HISTORY, "친환경 테마"),
                (KnowledgeCapability.PRODUCT_RISK_FACTORS, "ETF"),
            ],
        ),
        (
            "네뷸라홀딩스 소속 자회사를 보유한 AI 섹터 ETF의 위험요인을 알려줘.",
            [
                (KnowledgeCapability.CORPORATE_SUBSIDIARY, "네뷸라홀딩스"),
                (KnowledgeCapability.PORTFOLIO_HOLDINGS, "네뷸라홀딩스"),
                (KnowledgeCapability.SECTOR_THEME_HISTORY, "AI 섹터"),
                (KnowledgeCapability.PRODUCT_RISK_FACTORS, "ETF"),
            ],
        ),
        (
            "최근 6개월 우주 산업과 연관된 ETF 중 위성기업이 포함된 상품을 알려줘.",
            [
                (KnowledgeCapability.PORTFOLIO_HOLDINGS, "위성기업"),
                (KnowledgeCapability.SECTOR_THEME_HISTORY, "우주 산업"),
            ],
        ),
    ],
)
def test_external_relation_paraphrases_compile_without_entity_specific_policy(
    question: str,
    expected: list[tuple[KnowledgeCapability, str]],
) -> None:
    sanitized = sanitize_server_authorized_knowledge_draft(
        question,
        _empty_external_draft(
            ProductFamily.DOMESTIC_ETP,
            ProductFamily.OVERSEAS_ETP,
        ),
    )

    assert [(item.capability, item.subject_span) for item in sanitized.knowledge_requirements] == (
        expected
    )


def test_fund_relation_paraphrases_preserve_each_requested_dimension() -> None:
    question = "은하성장펀드의 모집 현황과 출시 일정, 운용 전략 동향을 알려줘."

    sanitized = sanitize_server_authorized_knowledge_draft(
        question,
        _empty_external_draft(ProductFamily.FUND),
    )

    assert [item.relation_span for item in sanitized.knowledge_requirements] == [
        "모집 현황",
        "출시 일정",
        "운용 전략 동향",
    ]


@pytest.mark.parametrize(
    "question",
    [
        "네뷸라소재가 편입된 ETF를 알려줘.",
        "네뷸라소재를 보유하는 ETF를 알려줘.",
        "네뷸라소재가 들어 있는 ETF를 알려줘.",
        "네뷸라소재를 구성종목으로 둔 ETF를 알려줘.",
        "포트폴리오에 네뷸라소재가 포함된 ETF를 알려줘.",
    ],
)
def test_holding_paraphrases_keep_only_the_literal_company_subject(question: str) -> None:
    sanitized = sanitize_server_authorized_knowledge_draft(
        question,
        _empty_external_draft(
            ProductFamily.DOMESTIC_ETP,
            ProductFamily.OVERSEAS_ETP,
        ),
    )

    assert [
        (item.capability, item.subject_span)
        for item in sanitized.knowledge_requirements
    ] == [(KnowledgeCapability.PORTFOLIO_HOLDINGS, "네뷸라소재")]


@pytest.mark.parametrize(
    ("question", "subject"),
    [
        ("로봇과 연관된 ETF를 알려줘.", "로봇"),
        ("반도체 테마 노출 이력이 있는 ETF를 알려줘.", "반도체 테마"),
        ("우주 산업 관련 ETF를 알려줘.", "우주 산업"),
    ],
)
def test_theme_relation_paraphrases_keep_one_taxonomy_requirement(
    question: str,
    subject: str,
) -> None:
    sanitized = sanitize_server_authorized_knowledge_draft(
        question,
        _empty_external_draft(
            ProductFamily.DOMESTIC_ETP,
            ProductFamily.OVERSEAS_ETP,
        ),
    )

    assert [
        (item.capability, item.subject_span)
        for item in sanitized.knowledge_requirements
    ] == [(KnowledgeCapability.SECTOR_THEME_HISTORY, subject)]


def test_fund_strategy_paraphrases_do_not_drop_requested_relations() -> None:
    question = "은하펀드의 조성 방식과 운용 기조, 모집 일정과 향후 계획을 알려줘."

    sanitized = sanitize_server_authorized_knowledge_draft(
        question,
        _empty_external_draft(ProductFamily.FUND),
    )

    assert [item.relation_span for item in sanitized.knowledge_requirements] == [
        "조성 방식",
        "운용 기조",
        "모집 일정",
        "향후 계획",
    ]


def test_explicit_month_range_is_not_absorbed_into_fund_identity() -> None:
    question = "2026년 5~6월 은하성장펀드의 구조와 판매 현황, 향후 계획을 알려줘."

    sanitized = sanitize_server_authorized_knowledge_draft(
        question,
        _empty_external_draft(ProductFamily.FUND),
    )
    plan = CompositeKnowledgePlanCompiler().compile(
        question,
        "dated-fund-trend",
        sanitized,
    )

    assert {item.subject for item in plan.requirements} == {"은하성장펀드"}
    assert all(
        item.time_range
        == KnowledgeTimeRange(start=date(2026, 5, 1), end=date(2026, 6, 30))
        for item in plan.requirements
    )


def test_numeric_field_description_is_not_misread_as_portfolio_holding() -> None:
    question = "총보수에 운용 비용이 포함된 국내 ETF를 알려줘."

    sanitized = sanitize_server_authorized_knowledge_draft(
        question,
        _empty_external_draft(ProductFamily.DOMESTIC_ETP),
    )

    assert sanitized.knowledge_requirements == []


@pytest.mark.parametrize(
    "question",
    [
        "네뷸라소재가 들어 있는 ETF를 알려줘.",
        "로봇과 연관된 ETF를 알려줘.",
        "은하펀드의 모집 일정과 향후 계획을 알려줘.",
        "오로라그룹 산하 기업을 담은 ETF를 알려줘.",
    ],
)
def test_runtime_identity_preflight_uses_the_same_external_vocabulary(question: str) -> None:
    assert has_external_knowledge_surface(question)


def test_named_subsidiary_particle_is_not_part_of_the_entity() -> None:
    question = "오로라그룹의 자회사인 네뷸라소재를 담은 로봇 테마 ETF를 알려줘."

    sanitized = sanitize_server_authorized_knowledge_draft(
        question,
        _empty_external_draft(
            ProductFamily.DOMESTIC_ETP,
            ProductFamily.OVERSEAS_ETP,
        ),
    )
    plan = CompositeKnowledgePlanCompiler().compile(
        question,
        "named-subsidiary-particle",
        sanitized,
    )

    assert plan.requirements[0].relation_evidence_span.endswith("네뷸라소재")
    assert plan.requirements[1].subject == "네뷸라소재"
    assert plan.requirements[1].depends_on == ("need_1",)


@pytest.mark.parametrize(
    ("surface", "field", "direction"),
    [
        ("AUM이 높은 순", "aum", SortDirection.DESC),
        ("총보수율이 낮은 순", "total_expense_ratio_pct", SortDirection.ASC),
        ("1일 수익률이 높은 순", "one_day_return_pct", SortDirection.DESC),
        ("1개월 수익률이 낮은 순", "one_month_return_pct", SortDirection.ASC),
        ("3개월 수익률이 높은 순", "three_month_return_pct", SortDirection.DESC),
        ("1년 수익률이 낮은 순", "one_year_return_pct", SortDirection.ASC),
        ("거래량이 많은 순", "daily_volume", SortDirection.DESC),
        ("거래대금이 적은 순", "daily_trading_value", SortDirection.ASC),
        ("20일 변동성이 낮은 순", "twenty_day_volatility_pct", SortDirection.ASC),
    ],
)
def test_external_ranking_compiler_supports_generalized_db_metrics(
    surface: str,
    field: str,
    direction: SortDirection,
) -> None:
    question = f"네뷸라소재를 보유한 국내 ETF를 {surface}으로 4개 보여줘."
    draft = IntentAnalysisDraft(
        intent=InteractionIntent.SEARCH,
        product_families=[ProductFamily.DOMESTIC_ETP],
        evidence_spans=[],
        needs_clarification=False,
        knowledge_requirements=[
            KnowledgeIntentRequirement(
                capability=KnowledgeCapability.PORTFOLIO_HOLDINGS,
                subject_span="네뷸라소재",
                relation_span="보유한",
            )
        ],
    )

    plan = CompositeKnowledgePlanCompiler().compile(question, f"rank-{field}", draft)

    assert plan.product_selection == KnowledgeProductSelection(
        rank_field=field,
        direction=direction,
        limit=4,
    )


@pytest.mark.parametrize(
    ("question", "mode"),
    [
        (
            "오로라소재와 네뷸라반도체를 모두 보유한 국내 ETF를 보여줘.",
            KnowledgeMatchMode.ALL,
        ),
        (
            "오로라소재 또는 네뷸라반도체 중 하나 이상을 보유한 국내 ETF를 보여줘.",
            KnowledgeMatchMode.ANY,
        ),
    ],
)
def test_multiple_holding_entities_compile_to_explicit_set_operation(
    question: str,
    mode: KnowledgeMatchMode,
) -> None:
    draft = IntentAnalysisDraft(
        intent=InteractionIntent.SEARCH,
        product_families=[ProductFamily.DOMESTIC_ETP],
        evidence_spans=[],
        needs_clarification=False,
        knowledge_requirements=[
            KnowledgeIntentRequirement(
                capability=KnowledgeCapability.PORTFOLIO_HOLDINGS,
                subject_span="오로라소재",
                relation_span="보유한",
            ),
            KnowledgeIntentRequirement(
                capability=KnowledgeCapability.PORTFOLIO_HOLDINGS,
                subject_span="네뷸라반도체",
                relation_span="보유한",
            ),
        ],
    )

    plan = CompositeKnowledgePlanCompiler().compile(question, f"holding-{mode.value}", draft)

    assert plan.holding_match_mode is mode


def _requirement(
    requirement_id: str,
    capability: KnowledgeCapability,
    subject: str,
    *,
    depends_on: tuple[str, ...] = (),
) -> KnowledgeRequirement:
    relation = "위험요인" if capability is KnowledgeCapability.PRODUCT_RISK_FACTORS else "보유한"
    predicate = (
        "has_risk_factor"
        if capability is KnowledgeCapability.PRODUCT_RISK_FACTORS
        else "holds_company"
    )
    return KnowledgeRequirement(
        requirement_id=requirement_id,
        capability=capability,
        predicate=predicate,
        subject=subject,
        subject_evidence_span=subject,
        relation_evidence_span=relation,
        product_families=(ProductFamily.DOMESTIC_ETP,),
        source_registry_ids=("samsungfund-kodex-product",),
        depends_on=depends_on,
    )


def _plan(
    mode: KnowledgeMatchMode,
    *,
    with_risk_ranking: bool = False,
) -> CompositeKnowledgeQueryPlan:
    requirements = [
        _requirement("need_1", KnowledgeCapability.PORTFOLIO_HOLDINGS, "오로라소재"),
        _requirement("need_2", KnowledgeCapability.PORTFOLIO_HOLDINGS, "네뷸라반도체"),
    ]
    question = "오로라소재 네뷸라반도체 보유한 국내 ETF"
    selection = None
    if with_risk_ranking:
        question += " 중 거래량이 높은 상품의 위험요인"
        requirements.append(
            _requirement(
                "need_3",
                KnowledgeCapability.PRODUCT_RISK_FACTORS,
                "상품",
                depends_on=("need_1", "need_2"),
            )
        )
        selection = KnowledgeProductSelection(
            rank_field="daily_volume",
            direction=SortDirection.DESC,
            limit=1,
        )
    return CompositeKnowledgeQueryPlan(
        question_id=f"set-{mode.value}-{with_risk_ranking}",
        question=question,
        interaction_intent=InteractionIntent.SEARCH,
        evidence_cutoff=date(2026, 8, 24),
        requirements=tuple(requirements),
        holding_match_mode=mode,
        product_selection=selection,
        join_order=tuple([item.requirement_id for item in requirements] + ["official_product_db"]),
    )


def _evidence(identifier: str) -> ExternalEvidenceReference:
    return ExternalEvidenceReference(
        evidence_id=f"evidence-{identifier}",
        source_registry_id="samsungfund-kodex-product",
        document_id=f"document-{identifier}",
        title="Synthetic official product evidence",
        publisher="삼성자산운용",
        source_uri="https://www.samsungfund.com/etf/product/view.do?id=2ETFB1",
        published_at=_AS_OF,
        as_of=_AS_OF,
        content_sha256=_SHA_A,
        locator="holdings or risk section",
        excerpt="Synthetic names vary while the relation contract remains unchanged.",
    )


def _holding(identifier: str, company: str, product_id: str) -> TemporalRelationRecord:
    return TemporalRelationRecord(
        relation_id=f"relation-holding-{identifier}",
        capability=KnowledgeCapability.PORTFOLIO_HOLDINGS,
        predicate="holds_company",
        subject=TemporalEntity(
            entity_id=f"company-{identifier}",
            kind=TemporalEntityKind.COMPANY,
            canonical_name=company,
        ),
        object=TemporalEntity(
            entity_id=f"product-{product_id.casefold()}",
            kind=TemporalEntityKind.PRODUCT,
            canonical_name=f"합성상품 {product_id}",
        ),
        product_family=ProductFamily.DOMESTIC_ETP,
        product_id=product_id,
        observed_at=_AS_OF,
        evidence=(_evidence(f"holding-{identifier}"),),
    )


def _risk(identifier: str, product_id: str) -> TemporalRelationRecord:
    return TemporalRelationRecord(
        relation_id=f"relation-risk-{identifier}",
        capability=KnowledgeCapability.PRODUCT_RISK_FACTORS,
        predicate="has_risk_factor",
        subject=TemporalEntity(
            entity_id=f"product-{product_id.casefold()}",
            kind=TemporalEntityKind.PRODUCT,
            canonical_name=f"합성상품 {product_id}",
        ),
        object=TemporalEntity(
            entity_id=f"risk-{identifier}",
            kind=TemporalEntityKind.RISK_CLAIM,
            canonical_name=f"합성 위험 {identifier}",
        ),
        product_family=ProductFamily.DOMESTIC_ETP,
        product_id=product_id,
        observed_at=_AS_OF,
        evidence=(_evidence(f"risk-{identifier}"),),
    )


def _theme(identifier: str, theme: str, product_id: str) -> TemporalRelationRecord:
    return TemporalRelationRecord(
        relation_id=f"relation-theme-{identifier}",
        capability=KnowledgeCapability.SECTOR_THEME_HISTORY,
        predicate="theme_related",
        subject=TemporalEntity(
            entity_id=f"theme-{identifier}",
            kind=TemporalEntityKind.THEME,
            canonical_name=theme,
        ),
        object=TemporalEntity(
            entity_id=f"product-{product_id.casefold()}",
            kind=TemporalEntityKind.PRODUCT,
            canonical_name=f"합성상품 {product_id}",
        ),
        product_family=ProductFamily.DOMESTIC_ETP,
        product_id=product_id,
        observed_at=_AS_OF,
        evidence=(_evidence(f"theme-{identifier}"),),
    )


class _SyntheticFactProvider:
    def __init__(self, values: dict[str, float]) -> None:
        self.values = values
        self.calls: list[tuple[tuple[tuple[ProductFamily, str], ...], object]] = []

    def fetch(self, product_keys, selection, requested_fields):
        del requested_fields
        self.calls.append((product_keys, selection))
        ordered = list(product_keys)
        field = "product_name"
        unit = "text"
        if selection is not None:
            field = selection.rank_field
            unit = _RANK_UNITS[field]
            ordered.sort(
                key=lambda item: self.values[item[1]],
                reverse=selection.direction is SortDirection.DESC,
            )
            ordered = ordered[: selection.limit]
        facts = []
        for source_row, (family, product_id) in enumerate(ordered, start=2):
            value = self.values[product_id] if selection is not None else f"합성상품 {product_id}"
            facts.append(
                OfficialProductFact(
                    evidence_id=f"provided-{product_id.casefold()}-{field}",
                    product_family=family,
                    product_id=product_id,
                    product_name=f"합성상품 {product_id}",
                    canonical_field=field,
                    normalized_value=value,
                    as_of=_AS_OF,
                    source_dataset="domestic_etp",
                    source_id="PREF01N001",
                    source_row=source_row,
                    source_columns=(field,),
                    unit=unit,
                    trading_currency=("KRW" if unit == "source_currency_amount" else None),
                    source_database_sha256=_SHA_A,
                )
            )
        return tuple(facts)


def _execute(
    plan: CompositeKnowledgeQueryPlan,
    relations: tuple[TemporalRelationRecord, ...],
    provider: _SyntheticFactProvider,
):
    store = TemporalRelationStore(
        seal_temporal_relation_bundle(relations, corpus_manifest_sha256=_SHA_B)
    )
    return ExternalKnowledgeExecutor(
        relation_store=store,
        product_fact_provider=provider,
        approved_product_ids={
            (ProductFamily.DOMESTIC_ETP, "P1"),
            (ProductFamily.DOMESTIC_ETP, "P2"),
        },
    ).execute(plan)


def test_holding_all_intersects_and_any_unions_without_entity_policy() -> None:
    relations = (
        _holding("aurora-p1", "오로라소재", "P1"),
        _holding("aurora-p2", "오로라소재", "P2"),
        _holding("nebula-p1", "네뷸라반도체", "P1"),
    )

    all_execution = _execute(_plan(KnowledgeMatchMode.ALL), relations, _SyntheticFactProvider({}))
    any_execution = _execute(_plan(KnowledgeMatchMode.ANY), relations, _SyntheticFactProvider({}))

    assert [item.product_id for item in all_execution.evidence.product_facts] == ["P1"]
    assert [item.product_id for item in any_execution.evidence.product_facts] == ["P1", "P2"]


def test_dependent_risk_is_retrieved_only_for_ranked_holding_products() -> None:
    relations = (
        _holding("aurora-p1", "오로라소재", "P1"),
        _holding("nebula-p2", "네뷸라반도체", "P2"),
        _risk("p1", "P1"),
        _risk("p2", "P2"),
    )
    provider = _SyntheticFactProvider({"P1": 10.0, "P2": 20.0})

    execution = _execute(
        _plan(KnowledgeMatchMode.ANY, with_risk_ranking=True),
        relations,
        provider,
    )

    risk_relations = [
        item
        for item in execution.evidence.relations
        if item.capability is KnowledgeCapability.PRODUCT_RISK_FACTORS
    ]
    assert [(item.product_family, item.product_id) for item in risk_relations] == [
        (ProductFamily.DOMESTIC_ETP, "P2")
    ]
    assert execution.verification.checks["dependent_product_scope"]


def test_holding_and_theme_requirements_intersect_before_db_facts() -> None:
    plan = CompositeKnowledgeQueryPlan(
        question_id="holding-theme-intersection",
        question="오로라소재를 보유한 로봇 테마와 연결된 국내 ETF",
        interaction_intent=InteractionIntent.SEARCH,
        evidence_cutoff=date(2026, 8, 24),
        requirements=(
            _requirement(
                "need_1",
                KnowledgeCapability.PORTFOLIO_HOLDINGS,
                "오로라소재",
            ),
            KnowledgeRequirement(
                requirement_id="need_2",
                capability=KnowledgeCapability.SECTOR_THEME_HISTORY,
                predicate="theme_related",
                subject="로봇 테마",
                subject_evidence_span="로봇 테마",
                relation_evidence_span="로봇 테마와 연결된",
                product_families=(ProductFamily.DOMESTIC_ETP,),
                source_registry_ids=("samsungfund-kodex-product",),
            ),
        ),
        join_order=("need_1", "need_2", "official_product_db"),
    )
    relations = (
        _holding("aurora-p1", "오로라소재", "P1"),
        _holding("aurora-p2", "오로라소재", "P2"),
        _theme("robot-p1", "로봇 테마", "P1"),
    )

    execution = _execute(plan, relations, _SyntheticFactProvider({}))

    assert [item.product_id for item in execution.evidence.product_facts] == ["P1"]
    assert execution.verification.checks["relation_set_operation"]


_NUMERIC_COLUMNS = {
    "aum": ("aum_minor_units", 100),
    "total_expense_ratio_pct": ("total_expense_ratio_micro_pct", 1_000_000),
    "one_day_return_pct": ("one_day_return_micro_pct", 1_000_000),
    "one_month_return_pct": ("one_month_return_micro_pct", 1_000_000),
    "three_month_return_pct": ("three_month_return_micro_pct", 1_000_000),
    "one_year_return_pct": ("one_year_return_micro_pct", 1_000_000),
    "daily_volume": ("daily_volume_hundredth_units", 100),
    "daily_trading_value": ("daily_trading_value_minor_units", 100),
    "twenty_day_volatility_pct": ("twenty_day_volatility_eight_decimal_pct", 100_000_000),
}


def _synthetic_product_db(path) -> None:
    def quality_column(column: str) -> str:
        return (
            column.removesuffix("_minor_units")
            .removesuffix("_hundredth_units")
            .removesuffix("_eight_decimal_pct")
            .removesuffix("_micro_pct")
            + "_quality"
        )

    numeric_definitions = ", ".join(
        f"{column} INTEGER, {quality_column(column)} TEXT"
        for column, _ in _NUMERIC_COLUMNS.values()
    )
    connection = sqlite3.connect(path)
    try:
        connection.execute(
            f"""
            CREATE TABLE domestic_etp_products (
                product_id TEXT PRIMARY KEY,
                product_name TEXT NOT NULL,
                source_row INTEGER NOT NULL,
                is_quarantined INTEGER NOT NULL,
                trading_currency TEXT NOT NULL,
                static_as_of TEXT NOT NULL,
                dynamic_as_of TEXT NOT NULL,
                {numeric_definitions}
            )
            """
        )
        columns = [column for column, _ in _NUMERIC_COLUMNS.values()]
        quality_columns = [quality_column(column) for column in columns]
        insert_columns = [
            "product_id",
            "product_name",
            "source_row",
            "is_quarantined",
            "trading_currency",
            "static_as_of",
            "dynamic_as_of",
            *columns,
            *quality_columns,
        ]
        placeholders = ", ".join("?" for _ in insert_columns)
        for offset, (product_id, multiplier, quality) in enumerate(
            (
                ("LOW", 1, "VALID"),
                ("HIGH", 3, "PARTIAL"),
                ("ZERO", 0, "VALID"),
                ("MISSING", None, "VALID"),
                ("UNKNOWN", 5, "UNKNOWN"),
            ),
            start=2,
        ):
            values = [
                None if multiplier is None else multiplier * scale
                for _, scale in _NUMERIC_COLUMNS.values()
            ]
            insert_sql = (
                f"INSERT INTO domestic_etp_products ({', '.join(insert_columns)}) "
                f"VALUES ({placeholders})"
            )
            connection.execute(
                insert_sql,
                (
                    product_id,
                    f"Synthetic {product_id}",
                    offset,
                    0,
                    "KRW",
                    _AS_OF.isoformat(),
                    _AS_OF.isoformat(),
                    *values,
                    *([quality] * len(quality_columns)),
                ),
            )
        connection.commit()
    finally:
        connection.close()


@pytest.mark.parametrize("field", tuple(_NUMERIC_COLUMNS))
@pytest.mark.parametrize(
    ("direction", "expected"),
    [
        (SortDirection.ASC, ["LOW", "HIGH"]),
        (SortDirection.DESC, ["HIGH", "LOW"]),
    ],
)
def test_official_db_is_sole_numeric_rank_authority_and_excludes_zero_missing_quality(
    tmp_path,
    field: str,
    direction: SortDirection,
    expected: list[str],
) -> None:
    database = tmp_path / "synthetic-domestic.sqlite3"
    _synthetic_product_db(database)
    provider = SqliteOfficialProductFactProvider(
        {ProductFamily.DOMESTIC_ETP: database},
        verify_approved_databases=False,
    )
    keys = tuple(
        (ProductFamily.DOMESTIC_ETP, product_id)
        for product_id in ("HIGH", "LOW", "UNKNOWN", "ZERO", "MISSING")
    )

    facts = provider.fetch(
        keys,
        KnowledgeProductSelection(rank_field=field, direction=direction, limit=10),
    )

    assert [item.product_id for item in facts] == expected
    assert all(item.source_kind == "provided" and item.as_of == _AS_OF for item in facts)
    assert all(item.normalized_value in {1.0, 3.0} for item in facts)
    quality_by_id = {item.product_id: item.quality for item in facts}
    assert quality_by_id == {
        "LOW": QualityStatus.VALID,
        "HIGH": QualityStatus.PARTIAL,
    }
