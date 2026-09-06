from __future__ import annotations

from datetime import date

import pytest

from finance_agent_core.agent.intent_analysis import (
    IntentAnalysisDraft,
    has_server_authorized_knowledge_requirements,
    sanitize_server_authorized_knowledge_draft,
)
from finance_agent_core.agent.knowledge_planning import (
    CompositeKnowledgePlanCompiler,
    CompositeKnowledgePlanningError,
)
from finance_agent_core.config.knowledge_coverage import KnowledgeCapability
from finance_agent_core.contracts.external_knowledge import (
    KnowledgeIntentRequirement,
    KnowledgeMatchMode,
    KnowledgeTimeRange,
    ProvidedProductCandidate,
    ProvidedProductCandidateUniverse,
)
from finance_agent_core.contracts.queryplan import ProductFamily, SortDirection
from finance_agent_core.contracts.routing import InteractionIntent
from finance_agent_core.execution.external_knowledge_runtime import (
    ExternalKnowledgeRuntime,
)
from finance_agent_core.retrieval.official_acquisition import (
    _snapshot_product_evidence_date,
)


def _draft(*families: ProductFamily) -> IntentAnalysisDraft:
    return IntentAnalysisDraft(
        intent=InteractionIntent.SEARCH,
        product_families=list(families),
        evidence_spans=[],
        needs_clarification=False,
    )


def _plan(
    question: str,
    *,
    product_mentions: tuple[str, ...] = (),
):
    families = (ProductFamily.DOMESTIC_ETP, ProductFamily.OVERSEAS_ETP)
    sanitized = sanitize_server_authorized_knowledge_draft(
        question,
        _draft(*families),
        fallback_families=families,
        product_mentions=product_mentions,
    )
    return CompositeKnowledgePlanCompiler().compile(
        question,
        "external-span-generalization",
        sanitized,
    )


@pytest.mark.parametrize(
    ("entity_connector", "quantifier", "expected_mode"),
    [
        ("와", "모두", KnowledgeMatchMode.ALL),
        (" 또는 ", "중 하나라도", KnowledgeMatchMode.ANY),
    ],
)
def test_named_candidate_list_never_leaks_into_constituent_set(
    entity_connector: str,
    quantifier: str,
    expected_mode: KnowledgeMatchMode,
) -> None:
    question = (
        "ALFA(2025-03-31), KODEX 미래인프라(2026-03-31)의 각 기준일 "
        f"상위 12개 종목 중 청람전자{entity_connector}북극소재 {quantifier} "
        "담은 국내외 ETF를 알려줘."
    )

    plan = _plan(
        question,
        product_mentions=("ALFA", "KODEX 미래인프라"),
    )
    holdings = [
        item
        for item in plan.requirements
        if item.capability is KnowledgeCapability.PORTFOLIO_HOLDINGS
    ]

    assert [item.subject for item in holdings] == ["청람전자", "북극소재"]
    assert plan.holding_match_mode is expected_mode


def test_company_name_ending_like_a_korean_particle_is_preserved() -> None:
    question = (
        "공식 편입표에서 비에이치아이, 새봄아이 두 종목을 모두 넣은 "
        "국내 ETF를 알려줘."
    )

    plan = _plan(question)
    subjects = [
        item.subject
        for item in plan.requirements
        if item.capability is KnowledgeCapability.PORTFOLIO_HOLDINGS
    ]

    assert subjects == ["비에이치아이", "새봄아이"]


def test_profile_bridge_keeps_the_actual_company_as_holding_subject() -> None:
    question = "별빛전자를 실제 구성종목으로 담은 국내 ETF를 찾아줘."

    plan = _plan(question)

    assert [
        (item.capability, item.subject, item.relation_evidence_span)
        for item in plan.requirements
    ] == [
        (
            KnowledgeCapability.PORTFOLIO_HOLDINGS,
            "별빛전자",
            "실제 구성종목으로 담은",
        )
    ]


@pytest.mark.parametrize(
    ("surface_with_particle", "expected"),
    (
        ("그중 삼성전자를", "삼성전자"),
        ("그 가운데 Apple를", "Apple"),
        ("이 중 에코프로를", "에코프로"),
        ("중국전력을", "중국전력"),
    ),
)
def test_holding_subject_removes_only_bounded_discourse_prefix(
    surface_with_particle: str,
    expected: str,
) -> None:
    question = f"공식 편입표에서 {surface_with_particle} 담은 국내 ETF를 찾아줘."

    plan = _plan(question)
    subjects = [
        item.subject
        for item in plan.requirements
        if item.capability is KnowledgeCapability.PORTFOLIO_HOLDINGS
    ]

    assert subjects == [expected]


def test_official_material_boundary_keeps_only_atomic_holding_entities() -> None:
    question = (
        "가장 최근의 공식 구성종목 자료를 기준으로 삼성전자와 SK하이닉스를 "
        "둘 다 담고 있는 국내 ETF만 찾아 순자산이 큰 순서로 5개 보여주세요. "
        "두 종목의 편입 비중과 구성종목 기준일도 알려주세요."
    )
    malformed_hcx = _draft(ProductFamily.DOMESTIC_ETP).model_copy(
        update={
            "knowledge_requirements": [
                KnowledgeIntentRequirement(
                    capability=KnowledgeCapability.PORTFOLIO_HOLDINGS,
                    subject_span="가장 최근의 공식 구성종목 자료를 기준으로 삼성전자",
                    relation_span="담고 있는",
                ),
                KnowledgeIntentRequirement(
                    capability=KnowledgeCapability.PORTFOLIO_HOLDINGS,
                    subject_span="SK하이닉스",
                    relation_span="담고 있는",
                ),
                KnowledgeIntentRequirement(
                    capability=KnowledgeCapability.PORTFOLIO_HOLDINGS,
                    subject_span="종목의",
                    relation_span="편입 비중",
                ),
                KnowledgeIntentRequirement(
                    capability=KnowledgeCapability.PORTFOLIO_HOLDINGS,
                    subject_span="삼성전자와 SK하이닉스",
                    relation_span="담고 있는",
                ),
            ]
        }
    )

    sanitized = sanitize_server_authorized_knowledge_draft(
        question,
        malformed_hcx,
        fallback_families=(ProductFamily.DOMESTIC_ETP,),
    )
    plan = CompositeKnowledgePlanCompiler().compile(
        question,
        "external-atomic-holdings",
        sanitized,
    )

    holdings = [
        item
        for item in plan.requirements
        if item.capability is KnowledgeCapability.PORTFOLIO_HOLDINGS
    ]
    assert [item.subject for item in holdings] == ["삼성전자", "SK하이닉스"]
    assert plan.holding_match_mode is KnowledgeMatchMode.ALL
    assert plan.product_selection is not None
    assert plan.product_selection.rank_field == "aum"
    assert plan.product_selection.limit == 5


def test_classified_theme_builds_dependent_risk_with_exact_day_window() -> None:
    question = (
        "2026년 2월 24일부터 2026년 8월 24일까지 운용사가 공개한 공식 자료에서 "
        "AI 반도체 테마로 소개된 국내 ETF를 찾아주세요. "
        "각 상품의 공식 위험요인도 함께 요약해 주세요."
    )

    plan = _plan(question)

    assert [item.capability for item in plan.requirements] == [
        KnowledgeCapability.SECTOR_THEME_HISTORY,
        KnowledgeCapability.PRODUCT_RISK_FACTORS,
    ]
    theme, risk = plan.requirements
    assert theme.subject == "AI 반도체 테마"
    assert theme.relation_evidence_span == "AI 반도체 테마로 소개된"
    assert theme.time_range == KnowledgeTimeRange(
        start=date(2026, 2, 24),
        end=date(2026, 8, 24),
    )
    assert risk.subject == "상품"
    assert risk.depends_on == (theme.requirement_id,)


def test_high_03_orders_only_by_the_bound_theme_evidence_publication_date() -> None:
    question = (
        "2026년 2월 24일부터 2026년 8월 24일까지 운용사가 공개한 공식 자료에서 "
        "AI 반도체 테마로 소개된 국내 ETF를 찾아주세요. "
        "테마 연결 근거의 발행일이 최신인 순서로 최대 5개를 보여주고, "
        "각 상품의 최신 공식 문서에 적힌 핵심 위험요인도 함께 요약해 주세요."
    )

    plan = _plan(question)

    assert plan.evidence_selection is not None
    assert plan.evidence_selection.order_by == "published_at"
    assert plan.evidence_selection.direction is SortDirection.DESC
    assert plan.evidence_selection.limit == 5
    assert plan.evidence_selection.source_requirement_id == "need_1"
    assert (
        plan.evidence_selection.selection_evidence_span
        == "테마 연결 근거의 발행일이 최신인 순서"
    )
    assert plan.requirements[0].capability is KnowledgeCapability.SECTOR_THEME_HISTORY
    assert plan.requirements[1].depends_on == ("need_1",)


@pytest.mark.parametrize(
    ("question", "order_by", "direction", "limit"),
    [
        (
            "삼성전자를 담은 국내 ETF를 공식 구성종목 자료의 게시일이 "
            "오래된 순서로 최대 3개 보여줘.",
            "published_at",
            SortDirection.ASC,
            3,
        ),
        (
            "최근 6개월 우주항공 테마와 연결된 국내 ETF를 "
            "공식 근거 기준일이 최신인 순서로 4개 알려줘.",
            "as_of",
            SortDirection.DESC,
            4,
        ),
    ],
)
def test_evidence_date_ordering_variants_compile_to_one_typed_source_requirement(
    question: str,
    order_by: str,
    direction: SortDirection,
    limit: int,
) -> None:
    plan = _plan(question)

    assert plan.evidence_selection is not None
    assert plan.evidence_selection.order_by == order_by
    assert plan.evidence_selection.direction is direction
    assert plan.evidence_selection.limit == limit
    source = next(
        item
        for item in plan.requirements
        if item.requirement_id == plan.evidence_selection.source_requirement_id
    )
    assert not source.depends_on


def test_generic_latest_product_word_does_not_become_evidence_ordering() -> None:
    plan = _plan("삼성전자를 담은 최신 국내 ETF 3개를 알려줘.")

    assert plan.evidence_selection is None


def test_db_metric_and_evidence_date_ordering_conflict_fails_closed() -> None:
    question = (
        "삼성전자를 담은 국내 ETF를 공식 구성종목 자료 발행일 최신순이면서 "
        "순자산이 큰 순서로 3개 알려줘."
    )

    with pytest.raises(
        CompositeKnowledgePlanningError,
        match="DB product ranking and evidence-date ordering conflict",
    ):
        _plan(question)


def test_bare_parent_subsidiary_chain_is_bound_without_company_suffix_false_positive() -> None:
    question = "청람그룹 자회사를 편입한 국내 ETF 중 순자산이 큰 상품의 위험요인 알려줘."

    plan = _plan(question)

    assert [item.capability for item in plan.requirements] == [
        KnowledgeCapability.CORPORATE_SUBSIDIARY,
        KnowledgeCapability.PORTFOLIO_HOLDINGS,
        KnowledgeCapability.PRODUCT_RISK_FACTORS,
    ]
    assert plan.requirements[0].subject == "청람그룹"
    assert plan.requirements[1].subject == "청람그룹"
    assert plan.requirements[1].depends_on == ("need_1",)
    assert plan.requirements[2].depends_on == ("need_2",)


def test_compound_risk_is_attached_to_relation_selected_products() -> None:
    question = (
        "ALFA, KODEX 미래인프라 두 상품의 공식 편입표에서 "
        "청람전자와 북극소재를 모두 담은 ETF 중 위험요인을 알려줘."
    )

    plan = _plan(
        question,
        product_mentions=("ALFA", "KODEX 미래인프라"),
    )
    risk = [
        item
        for item in plan.requirements
        if item.capability is KnowledgeCapability.PRODUCT_RISK_FACTORS
    ]

    assert len(risk) == 1
    assert risk[0].subject == "ETF"
    assert risk[0].depends_on == ("need_1", "need_2")


def test_bare_family_risk_in_next_sentence_binds_one_selector_scope() -> None:
    question = (
        "공식 가이드의 상위 10개에 청람전자, 북극소재 두 종목이 모두 있는지 "
        "확인해 ETF를 찾아줘. ETF 손실 유의점도 함께 설명해줘."
    )

    plan = _plan(question)
    holdings = [
        item
        for item in plan.requirements
        if item.capability is KnowledgeCapability.PORTFOLIO_HOLDINGS
    ]
    risk = next(
        item
        for item in plan.requirements
        if item.capability is KnowledgeCapability.PRODUCT_RISK_FACTORS
    )

    assert [item.subject for item in holdings] == ["청람전자", "북극소재"]
    assert risk.subject == "ETF"
    assert risk.depends_on == tuple(item.requirement_id for item in holdings)


def test_bare_family_risk_binds_across_db_only_enrichment_clause() -> None:
    question = (
        "공식 가이드의 상위 10개에 청람전자, 북극소재 두 종목이 모두 있는지 "
        "확인해 ETF를 찾아줘. 제공 DB에서는 추적오차율 0.5 이하를 적용해 "
        "순자산을 연결해줘. 자료 날짜와 ETF 손실 유의점도 설명해줘."
    )

    plan = _plan(question)
    holdings = [
        item
        for item in plan.requirements
        if item.capability is KnowledgeCapability.PORTFOLIO_HOLDINGS
    ]
    risk = next(
        item
        for item in plan.requirements
        if item.capability is KnowledgeCapability.PRODUCT_RISK_FACTORS
    )

    assert risk.depends_on == tuple(item.requirement_id for item in holdings)


def test_bare_family_risk_does_not_cross_two_independent_selector_scopes() -> None:
    question = (
        "공식 자료에서 청람전자를 담은 ETF와 북극소재 테마로 소개된 ETF를 "
        "각각 찾아줘. "
        "ETF 위험요인을 설명해줘."
    )

    plan = _plan(question)

    assert all(
        item.capability is not KnowledgeCapability.PRODUCT_RISK_FACTORS
        for item in plan.requirements
    )


def test_from_to_month_range_is_normalized_against_the_signed_cutoff() -> None:
    question = "2026년 1월부터 8월까지 반도체 산업과 연관된 ETF를 알려줘."

    plan = _plan(question)

    assert plan.requirements[0].time_range == KnowledgeTimeRange(
        start=date(2026, 1, 1),
        end=date(2026, 8, 24),
    )


@pytest.mark.parametrize(
    "surface",
    [
        "2026년 8월 24일에서 6개월을 거슬러 올라간 기간",
        "2026년 8월 24일을 기준으로 여섯 달 전부터",
    ],
)
def test_anchored_back_month_range_uses_the_named_day(surface: str) -> None:
    plan = _plan(f"{surface}에 발행된 공식 자료에서 청람전자를 담은 ETF를 알려줘.")

    assert plan.requirements[0].time_range == KnowledgeTimeRange(
        start=date(2026, 2, 24),
        end=date(2026, 8, 24),
    )


def test_explanatory_investment_risk_is_not_lost_after_relation_filter() -> None:
    question = (
        "지난 두 분기에 로봇 산업과 연결된 ETF 가운데 NVIDIA를 편입한 "
        "상품의 투자 위험을 설명해줘."
    )

    families = (ProductFamily.DOMESTIC_ETP, ProductFamily.OVERSEAS_ETP)
    sanitized = sanitize_server_authorized_knowledge_draft(
        question,
        _draft(*families),
        fallback_families=families,
    )
    assert has_server_authorized_knowledge_requirements(question, sanitized)
    plan = CompositeKnowledgePlanCompiler().compile(
        question,
        "external-investment-risk-generalization",
        sanitized,
    )

    assert [item.capability for item in plan.requirements] == [
        KnowledgeCapability.PORTFOLIO_HOLDINGS,
        KnowledgeCapability.SECTOR_THEME_HISTORY,
        KnowledgeCapability.PRODUCT_RISK_FACTORS,
    ]
    assert plan.requirements[-1].subject == "상품"
    assert plan.requirements[-1].depends_on == ("need_1", "need_2")


def test_distinct_product_dates_are_bound_only_to_fixed_db_identities() -> None:
    question = (
        "ALFA(2025-03-31), KODEX 미래인프라(2026-03-31)의 각 기준일 "
        "상위 12개 종목 중 청람전자와 북극소재를 모두 담은 국내외 ETF를 알려줘."
    )
    plan = _plan(
        question,
        product_mentions=("ALFA", "KODEX 미래인프라"),
    )
    universe = ProvidedProductCandidateUniverse(
        candidates=(
            ProvidedProductCandidate(
                product_family=ProductFamily.DOMESTIC_ETP,
                product_id="A123456",
                product_name="KODEX 미래인프라",
                aliases=("A123456",),
            ),
            ProvidedProductCandidate(
                product_family=ProductFamily.OVERSEAS_ETP,
                product_id="ALFA",
                product_name="Alpha Infrastructure ETF",
                aliases=("ALFA",),
            ),
        ),
        database_sha256s={
            ProductFamily.DOMESTIC_ETP: "a" * 64,
            ProductFamily.OVERSEAS_ETP: "b" * 64,
        },
        candidate_filter_sha256="c" * 64,
        explicit_identity_bound=True,
    )

    bindings = dict(
        ExternalKnowledgeRuntime._provided_product_evidence_dates(
            question,
            requirement=plan.requirements[0],
            universe=universe,
        )
    )

    assert bindings["alfa"] == date(2025, 3, 31)
    assert bindings["alphainfrastructureetf"] == date(2025, 3, 31)
    assert bindings["kodex미래인프라"] == date(2026, 3, 31)
    assert bindings["a123456"] == date(2026, 3, 31)


@pytest.mark.parametrize(
    "question",
    [
        "ALFA(2025년 3월 31일), KODEX 미래인프라(2026.3.31)의 각 기준일 편입표를 비교해줘.",
        "2025년 3월 31일 기준 ALFA와 2026/3/31 기준의 KODEX 미래인프라 구성종목을 비교해줘.",
    ],
)
def test_natural_korean_and_delimited_product_dates_bind_per_identity(
    question: str,
) -> None:
    requirement = _plan(
        "ALFA, KODEX 미래인프라의 공식 편입표에서 "
        "청람전자를 담은 ETF를 찾아줘.",
        product_mentions=("ALFA", "KODEX 미래인프라"),
    ).requirements[0]
    universe = ProvidedProductCandidateUniverse(
        candidates=(
            ProvidedProductCandidate(
                product_family=ProductFamily.DOMESTIC_ETP,
                product_id="A123456",
                product_name="KODEX 미래인프라",
                aliases=("A123456",),
            ),
            ProvidedProductCandidate(
                product_family=ProductFamily.OVERSEAS_ETP,
                product_id="ALFA",
                product_name="Alpha Infrastructure ETF",
                aliases=("ALFA",),
            ),
        ),
        database_sha256s={
            ProductFamily.DOMESTIC_ETP: "a" * 64,
            ProductFamily.OVERSEAS_ETP: "b" * 64,
        },
        candidate_filter_sha256="c" * 64,
        explicit_identity_bound=True,
    )

    bindings = dict(
        ExternalKnowledgeRuntime._provided_product_evidence_dates(
            question,
            requirement=requirement,
            universe=universe,
        )
    )

    assert bindings["alfa"] == date(2025, 3, 31)
    assert bindings["alphainfrastructureetf"] == date(2025, 3, 31)
    assert bindings["kodex미래인프라"] == date(2026, 3, 31)
    assert bindings["a123456"] == date(2026, 3, 31)


def test_historical_link_date_uses_one_matched_product_identity() -> None:
    bindings = (
        ("alfa", date(2025, 3, 31)),
        ("kodex미래인프라", date(2026, 3, 31)),
    )

    selected, matched = _snapshot_product_evidence_date(
        type(
            "Snapshot",
            (),
            {"db_ticker": None, "title": "KODEX 미래인프라 공식 상품 페이지"},
        )(),
        bindings,
    )
    ambiguous, ambiguous_matched = _snapshot_product_evidence_date(
        type(
            "Snapshot",
            (),
            {"db_ticker": None, "title": "ALFA 및 KODEX 미래인프라 비교"},
        )(),
        bindings,
    )

    assert (selected, matched) == (date(2026, 3, 31), True)
    assert (ambiguous, ambiguous_matched) == (None, True)


def test_historical_link_date_does_not_use_a_product_name_prefix() -> None:
    selected, matched = _snapshot_product_evidence_date(
        type(
            "Snapshot",
            (),
            {"db_ticker": None, "title": "KODEX 200TR 공식 상품 페이지"},
        )(),
        (
            ("kodex200", date(2025, 3, 31)),
            ("kodex200tr", date(2026, 3, 31)),
        ),
    )

    assert (selected, matched) == (date(2026, 3, 31), True)
