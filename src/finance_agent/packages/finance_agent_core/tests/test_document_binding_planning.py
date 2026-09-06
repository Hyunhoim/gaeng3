from __future__ import annotations

from datetime import date

from finance_agent_core.agent.external_search_planning import ExternalSearchPlanCompiler
from finance_agent_core.agent.intent_analysis import (
    IntentAnalysisDraft,
    sanitize_server_authorized_knowledge_draft,
)
from finance_agent_core.agent.knowledge_planning import CompositeKnowledgePlanCompiler
from finance_agent_core.config.knowledge_coverage import KnowledgeCapability
from finance_agent_core.config.source_registry import load_official_source_registry
from finance_agent_core.contracts.external_knowledge import (
    CompositeKnowledgeQueryPlan,
    KnowledgeIntentRequirement,
    KnowledgePredicate,
    KnowledgeRequirement,
    ProvidedProductCandidate,
    ProvidedProductCandidateUniverse,
)
from finance_agent_core.contracts.queryplan import ProductFamily
from finance_agent_core.contracts.routing import InteractionIntent


def test_multi_document_question_does_not_reuse_first_date_as_global_time() -> None:
    question = (
        "Global X 2025년 3월 말 팩트시트의 AIQ와 "
        "2025-06-30자 TIGER 미래기술 월간 팩트시트의 TIGER 미래기술에서 "
        "삼성전자를 담은 ETF를 찾아줘."
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
                subject_span="삼성전자",
                relation_span="담은",
            )
        ],
    )

    plan = CompositeKnowledgePlanCompiler().compile(question, "no-global-date", draft)

    assert plan.requirements[0].time_range is None


def test_cross_family_result_risk_is_bound_to_the_verified_selector() -> None:
    question = (
        "삼성전자를 담은 국내·해외 ETF를 추려 주세요. "
        "최종 후보별로 해당 운용사 자료의 손실 관련 유의점도 알려 주세요."
    )
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

    sanitized = sanitize_server_authorized_knowledge_draft(
        question,
        draft,
        fallback_families=(
            ProductFamily.DOMESTIC_ETP,
            ProductFamily.OVERSEAS_ETP,
        ),
    )
    plan = CompositeKnowledgePlanCompiler().compile(question, "dependent-risk", sanitized)

    assert tuple(item.capability for item in plan.requirements) == (
        KnowledgeCapability.PORTFOLIO_HOLDINGS,
        KnowledgeCapability.PRODUCT_RISK_FACTORS,
    )
    assert plan.requirements[1].subject == "최종 후보별로"
    assert plan.requirements[1].depends_on == ("need_1",)
    assert plan.requirements[1].product_families == (
        ProductFamily.DOMESTIC_ETP,
        ProductFamily.OVERSEAS_ETP,
    )


def test_multi_document_dates_are_bound_to_exact_provided_products_before_receipt() -> None:
    question = (
        "Global X 2025년 3월 말 팩트시트의 AIQ / "
        "KODEX 2026년 2분기 연금투자 가이드북"
        "(구성종목 기준 2026년 3월 31일)의 KODEX 반도체(091160) / "
        "2025-06-30자 TIGER 반도체TOP10 월간 팩트시트의 "
        "TIGER 반도체TOP10(396500)에 나온 상위 10개 종목 중 "
        "삼성전자를 담은 ETF를 찾아줘."
    )
    registry = load_official_source_registry()
    source_ids = tuple(
        sorted(
            entry.source_id
            for entry in registry.entries
            if KnowledgeCapability.PORTFOLIO_HOLDINGS in entry.capabilities
        )
    )
    original = CompositeKnowledgeQueryPlan(
        question_id="multi-document-binding",
        question=question,
        interaction_intent=InteractionIntent.SEARCH,
        evidence_cutoff=date(2026, 8, 24),
        requirements=(
            KnowledgeRequirement(
                requirement_id="need_1",
                capability=KnowledgeCapability.PORTFOLIO_HOLDINGS,
                predicate=KnowledgePredicate.HOLDS_COMPANY,
                subject="삼성전자",
                subject_evidence_span="삼성전자",
                relation_evidence_span="담은",
                product_families=(
                    ProductFamily.DOMESTIC_ETP,
                    ProductFamily.OVERSEAS_ETP,
                ),
                source_registry_ids=source_ids,
            ),
        ),
        join_order=("need_1", "official_product_db"),
        execution_authorized=True,
    )
    universe = ProvidedProductCandidateUniverse(
        candidates=(
            ProvidedProductCandidate(
                product_family=ProductFamily.DOMESTIC_ETP,
                product_id="A091160",
                product_name="KODEX 반도체",
                aliases=("091160", "A091160"),
            ),
            ProvidedProductCandidate(
                product_family=ProductFamily.DOMESTIC_ETP,
                product_id="A396500",
                product_name="TIGER 반도체TOP10",
                aliases=("396500", "A396500"),
            ),
            ProvidedProductCandidate(
                product_family=ProductFamily.DOMESTIC_ETP,
                product_id="A999999",
                product_name="TIGER 반도체",
            ),
            ProvidedProductCandidate(
                product_family=ProductFamily.OVERSEAS_ETP,
                product_id="AIQ.O",
                product_name="Global X Artificial Intelligence & Technology ETF",
                aliases=("AIQ.O",),
            ),
        ),
        database_sha256s={
            ProductFamily.DOMESTIC_ETP: "a" * 64,
            ProductFamily.OVERSEAS_ETP: "b" * 64,
        },
        candidate_filter_sha256="c" * 64,
        explicit_identity_bound=True,
    )

    search_plan = ExternalSearchPlanCompiler(registry).compile(original, universe)
    finalized = search_plan.composite_plan
    bindings = finalized.requirements[0].document_bindings

    assert tuple((item.product_family, item.product_id) for item in bindings) == (
        (ProductFamily.OVERSEAS_ETP, "AIQ.O"),
        (ProductFamily.DOMESTIC_ETP, "A091160"),
        (ProductFamily.DOMESTIC_ETP, "A396500"),
    )
    assert tuple((item.time_range.start, item.time_range.end) for item in bindings) == (
        (date(2025, 3, 31), date(2025, 3, 31)),
        (date(2026, 3, 31), date(2026, 3, 31)),
        (date(2025, 6, 30), date(2025, 6, 30)),
    )
    assert finalized.requirements[0].time_range is None
    assert search_plan.execution_receipt is not None
    assert finalized != original
