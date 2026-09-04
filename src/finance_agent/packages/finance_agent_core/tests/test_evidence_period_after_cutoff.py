"""A question dated after the evidence cutoff must clarify, never crash.

Blind set A question 35 (``2026년 9월 1일 기준 SPY에서 엔비디아 편입 비중``)
produced a reversed ``KnowledgeTimeRange`` and an HTTP 500.  The planner now
names the situation, the external router keeps that reason code, and the
clarification guidance asks for a period inside the approved range.
"""

from __future__ import annotations

from datetime import date

import pytest

from finance_agent_core.agent.external_knowledge_router import (
    ExternalKnowledgeRouteDisposition,
    ExternalKnowledgeRouter,
)
from finance_agent_core.agent.intent_analysis import IntentAnalysisDraft, InteractionIntent
from finance_agent_core.agent.knowledge_planning import (
    CompositeKnowledgePlanningError,
    EvidencePeriodAfterCutoffError,
    _knowledge_time_range,
)
from finance_agent_core.answering.final_generation import build_clarification_guidance

_CUTOFF = date(2026, 8, 24)


def test_period_starting_after_the_cutoff_is_named_not_reversed() -> None:
    with pytest.raises(EvidencePeriodAfterCutoffError) as raised:
        _knowledge_time_range("2026년 9월 1일 기준 SPY에서 엔비디아 편입 비중을 알려줘", _CUTOFF)

    assert isinstance(raised.value, CompositeKnowledgePlanningError)
    assert raised.value.reason_code == "evidence_period_after_cutoff"


def test_period_ending_after_the_cutoff_is_still_clamped() -> None:
    clamped = _knowledge_time_range("2026년 8월 기준 SPY의 엔비디아 편입 비중을 알려줘", _CUTOFF)

    assert clamped is not None
    assert clamped.start == date(2026, 8, 1)
    assert clamped.end == _CUTOFF


def test_external_router_keeps_the_specific_reason_code() -> None:
    class _Compiler:
        def compile(self, question, question_id, intent, *, source_selection_signals=None):
            raise EvidencePeriodAfterCutoffError("requested evidence period starts after cutoff")

    class _Coverage:
        def require(self, capability):  # pragma: no cover - never reached
            raise AssertionError("coverage must not be consulted")

    intent = IntentAnalysisDraft(
        intent=InteractionIntent.SEARCH,
        product_families=[],
        evidence_spans=[],
        needs_clarification=False,
        knowledge_requirements=[
            {
                "capability": "portfolio_holdings",
                "subject_span": "SPY",
                "relation_span": "편입 비중",
            }
        ],
    )
    router = ExternalKnowledgeRouter(
        compiler=_Compiler(),  # type: ignore[arg-type]
        coverage=_Coverage(),  # type: ignore[arg-type]
    )

    decision = router.route("2026년 9월 1일 기준 SPY에서 엔비디아 편입 비중", "q-35", intent)

    assert decision.disposition is ExternalKnowledgeRouteDisposition.CLARIFY
    assert decision.reason_code == "evidence_period_after_cutoff"


def test_guidance_asks_for_a_period_inside_the_approved_range() -> None:
    guidance = build_clarification_guidance(
        question="2026년 9월 1일 기준 SPY에서 엔비디아 편입 비중이 정확히 얼마였는지 알려줘.",
        reason_code="evidence_period_after_cutoff",
        required_fields=[],
        product_families=["overseas_etp"],
    )

    assert guidance.missing_slots == ["evidence_period"]
    assert guidance.required_terms == ["기준일"]
    assert guidance.fallback_question.endswith("?")
    assert "마감일" in guidance.fallback_question
