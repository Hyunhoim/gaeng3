from __future__ import annotations

import pytest

from finance_agent_core.agent.intent_analysis import (
    IntentAnalysisDraft,
    IntentExecutionClause,
    SemanticFrameDirection,
    SemanticFrameOperation,
    SemanticFrameOperator,
    SemanticFrameStatus,
    has_server_authorized_knowledge_requirements,
    knowledge_capability_signal_matches,
    sanitize_server_authorized_knowledge_draft,
)
from finance_agent_core.agent.knowledge_planning import (
    CompositeKnowledgePlanCompiler,
    CompositeKnowledgePlanningError,
)
from finance_agent_core.config.knowledge_coverage import KnowledgeCapability
from finance_agent_core.contracts.external_knowledge import KnowledgeIntentRequirement
from finance_agent_core.contracts.queryplan import ProductFamily
from finance_agent_core.contracts.routing import InteractionIntent

_ETP_FAMILIES = (ProductFamily.DOMESTIC_ETP, ProductFamily.OVERSEAS_ETP)


def _external_frame_draft(
    *,
    subject_span: str,
    relation_span: str,
    requirement_capability: KnowledgeCapability,
    frame_capability: KnowledgeCapability | None = None,
) -> IntentAnalysisDraft:
    frame_capability = frame_capability or requirement_capability
    return IntentAnalysisDraft(
        intent=InteractionIntent.SEARCH,
        product_families=list(_ETP_FAMILIES),
        evidence_spans=[],
        needs_clarification=False,
        knowledge_requirements=[
            KnowledgeIntentRequirement(
                capability=requirement_capability,
                subject_span=subject_span,
                relation_span=relation_span,
            )
        ],
        execution_clauses=[
            IntentExecutionClause(
                sequence=1,
                role="relation",
                text_span=relation_span,
                product_families=list(_ETP_FAMILIES),
                semantic_status=SemanticFrameStatus.RESOLVED,
                field_id="__none__",
                operation=SemanticFrameOperation.EXTERNAL_RELATION,
                operator=SemanticFrameOperator.NONE,
                direction=SemanticFrameDirection.NONE,
                subject_span=subject_span,
                knowledge_capability=frame_capability.value,
            )
        ],
    )


def _assert_external_planning_is_closed(
    question: str,
    draft: IntentAnalysisDraft,
) -> None:
    sanitized = sanitize_server_authorized_knowledge_draft(question, draft)

    assert sanitized.knowledge_requirements == []
    assert not has_server_authorized_knowledge_requirements(question, sanitized)
    with pytest.raises(CompositeKnowledgePlanningError):
        CompositeKnowledgePlanCompiler().compile(
            question,
            "closed-external-authority",
            sanitized,
        )


def test_literal_closed_hclx_frame_bridges_novel_relation_into_bounded_plan() -> None:
    question = "양자컴퓨팅과 맥이 닿는 ETF를 알려줘."
    subject_span = "양자컴퓨팅"
    relation_span = "양자컴퓨팅과 맥이 닿는"
    capability = KnowledgeCapability.SECTOR_THEME_HISTORY
    draft = _external_frame_draft(
        subject_span=subject_span,
        relation_span=relation_span,
        requirement_capability=capability,
    )

    # Prove that admission comes from the closed HCLX frame, not from the
    # server's existing lexical/regex vocabulary.
    assert not knowledge_capability_signal_matches(capability, relation_span)

    sanitized = sanitize_server_authorized_knowledge_draft(question, draft)

    assert len(sanitized.knowledge_requirements) == 1
    sanitized_requirement = sanitized.knowledge_requirements[0]
    assert sanitized_requirement.capability is capability
    assert sanitized_requirement.subject_span == subject_span
    assert sanitized_requirement.relation_span == relation_span
    assert has_server_authorized_knowledge_requirements(question, sanitized)
    plan = CompositeKnowledgePlanCompiler().compile(
        question,
        "novel-hclx-relation",
        sanitized,
    )
    assert len(plan.requirements) == 1
    assert plan.requirements[0].capability is capability
    assert plan.requirements[0].subject == subject_span
    assert plan.requirements[0].subject_evidence_span == subject_span
    assert plan.requirements[0].relation_evidence_span == relation_span
    assert set(plan.requirements[0].product_families) == set(_ETP_FAMILIES)


def test_negated_relation_cannot_acquire_external_search_from_hclx_frame() -> None:
    question = "양자컴퓨팅과 관련되지 않은 ETF를 알려줘."
    draft = _external_frame_draft(
        subject_span="양자컴퓨팅",
        relation_span="양자컴퓨팅과 관련되지 않은",
        requirement_capability=KnowledgeCapability.SECTOR_THEME_HISTORY,
    )

    sanitized = sanitize_server_authorized_knowledge_draft(question, draft)

    assert sanitized.intent is InteractionIntent.CLARIFY
    assert sanitized.needs_clarification
    assert sanitized.execution_clauses == []
    assert sanitized.knowledge_requirements == []
    assert not has_server_authorized_knowledge_requirements(question, sanitized)
    with pytest.raises(CompositeKnowledgePlanningError):
        CompositeKnowledgePlanCompiler().compile(
            question,
            "negated-external-relation",
            sanitized,
        )


def test_non_literal_hclx_subject_cannot_acquire_external_search() -> None:
    question = "양자컴퓨팅과 맥이 닿는 ETF를 알려줘."
    draft = _external_frame_draft(
        subject_span="양자컴퓨터",
        relation_span="양자컴퓨팅과 맥이 닿는",
        requirement_capability=KnowledgeCapability.SECTOR_THEME_HISTORY,
    )

    _assert_external_planning_is_closed(question, draft)


def test_plain_db_ranking_cannot_be_relabelled_as_external_relation() -> None:
    question = "위험등급이 낮은 국내 ETF 3개를 알려줘."
    draft = _external_frame_draft(
        subject_span="위험등급",
        relation_span="위험등급이 낮은",
        requirement_capability=KnowledgeCapability.PRODUCT_RISK_FACTORS,
    )

    _assert_external_planning_is_closed(question, draft)


def test_mismatched_frame_capability_cannot_bridge_novel_relation() -> None:
    question = "양자컴퓨팅과 맥이 닿는 ETF를 알려줘."
    relation_span = "양자컴퓨팅과 맥이 닿는"
    draft = _external_frame_draft(
        subject_span="양자컴퓨팅",
        relation_span=relation_span,
        requirement_capability=KnowledgeCapability.SECTOR_THEME_HISTORY,
        frame_capability=KnowledgeCapability.PORTFOLIO_HOLDINGS,
    )

    assert not knowledge_capability_signal_matches(
        KnowledgeCapability.SECTOR_THEME_HISTORY,
        relation_span,
    )
    _assert_external_planning_is_closed(question, draft)
