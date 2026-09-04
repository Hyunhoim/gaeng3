from __future__ import annotations

from datetime import date

import pytest

from finance_agent_core.agent.intent_analysis import (
    IntentAnalysisDraft,
    has_server_authorized_knowledge_requirements,
    sanitize_server_authorized_knowledge_draft,
)
from finance_agent_core.agent.knowledge_planning import CompositeKnowledgePlanCompiler
from finance_agent_core.config.knowledge_coverage import KnowledgeCapability
from finance_agent_core.contracts.external_knowledge import (
    KnowledgeMatchMode,
    KnowledgeTimeRange,
    ProvidedProductCandidate,
    ProvidedProductCandidateUniverse,
)
from finance_agent_core.contracts.queryplan import ProductFamily
from finance_agent_core.contracts.routing import InteractionIntent
from finance_agent_core.execution.external_knowledge_runtime import ExternalKnowledgeRuntime
from finance_agent_core.retrieval.official_acquisition import _snapshot_product_evidence_date


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


def test_from_to_month_range_is_normalized_against_the_signed_cutoff() -> None:
    question = "2026년 1월부터 8월까지 반도체 산업과 연관된 ETF를 알려줘."

    plan = _plan(question)

    assert plan.requirements[0].time_range == KnowledgeTimeRange(
        start=date(2026, 1, 1),
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
