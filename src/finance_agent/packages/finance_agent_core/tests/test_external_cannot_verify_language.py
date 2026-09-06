from __future__ import annotations

from types import SimpleNamespace

from finance_agent_core.agent.backend_adapter import _finalize_with_official_hclx
from finance_agent_core.agent.external_control_language import (
    build_external_cannot_verify_narrative,
)
from finance_agent_core.agent.external_knowledge_result_adapter import (
    external_knowledge_result_to_backend,
)
from finance_agent_core.agent.intent_analysis import IntentAnalysisDraft
from finance_agent_core.agent.knowledge_planning import CompositeKnowledgePlanCompiler
from finance_agent_core.answering.final_generation import (
    FinalAnswerContext,
    FinalAnswerDraft,
    allowed_final_answer_leads,
)
from finance_agent_core.config.knowledge_coverage import KnowledgeCapability
from finance_agent_core.contracts.backend import BackendAnswerMode, BackendStatus
from finance_agent_core.contracts.external_knowledge import (
    ExternalKnowledgeOutcome,
    ExternalKnowledgeOutcomeStatus,
    KnowledgeIntentRequirement,
)
from finance_agent_core.contracts.queryplan import ProductFamily
from finance_agent_core.contracts.routing import InteractionIntent
from finance_agent_core.execution.external_knowledge_runtime import (
    ExternalKnowledgeRuntimeResult,
)


def _holding_plan():
    question = "캠브리콘이 편입된 중국 반도체 해외 ETF를 알려줘."
    return CompositeKnowledgePlanCompiler().compile(
        question,
        "cache-failure-language",
        IntentAnalysisDraft(
            intent=InteractionIntent.SEARCH,
            product_families=[ProductFamily.OVERSEAS_ETP],
            evidence_spans=[],
            needs_clarification=False,
            knowledge_requirements=[
                KnowledgeIntentRequirement(
                    capability=KnowledgeCapability.PORTFOLIO_HOLDINGS,
                    subject_span="캠브리콘",
                    relation_span="편입된",
                )
            ],
        ),
    )


class _CapturingProvider:
    provider_name = "hyperclova"
    model_name = "HCX-007"

    def __init__(self) -> None:
        self.context: FinalAnswerContext | None = None

    def generate_final_answer(self, context: FinalAnswerContext) -> FinalAnswerDraft:
        self.context = context
        return FinalAnswerDraft(
            lead=allowed_final_answer_leads(context)[0],
            answer_body_sha256=context.answer_body_sha256,
        )


def test_cache_only_miss_has_question_specific_safe_narrative() -> None:
    plan = _holding_plan()
    acquisition = SimpleNamespace(
        search_query_count=0,
        discovered_candidate_count=0,
        failure_codes=("search_not_exhaustive", "source_scope_incomplete"),
    )

    narrative = build_external_cannot_verify_narrative(
        plan,
        reason_codes=("live_exact_evidence_span_missing",),
        acquisitions=(acquisition,),
    )

    assert narrative.failure_kind == "cache_miss"
    assert "캠브리콘" in narrative.answer
    assert "구성종목·편입 관계" in narrative.answer
    assert "공식 문서 저장소(Cache)" in narrative.answer
    assert "확인할 수 없습니다" in narrative.answer
    assert "확인되지 않은 상품도 제시하지 않았습니다" in narrative.answer
    assert "source_scope_incomplete" not in narrative.answer


def test_explanation_cache_miss_does_not_claim_a_product_intersection_search() -> None:
    plan = _holding_plan().model_copy(
        update={"interaction_intent": InteractionIntent.EXPLAIN}
    )
    acquisition = SimpleNamespace(
        search_query_count=0,
        discovered_candidate_count=0,
        failure_codes=("search_not_exhaustive", "source_scope_incomplete"),
    )

    narrative = build_external_cannot_verify_narrative(
        plan,
        acquisitions=(acquisition,),
    )

    assert narrative.failure_kind == "cache_miss"
    assert "사전학습 지식으로 보완하지 않았으며" in narrative.answer
    assert "공식 근거 없는 설명도 생성하지 않았습니다" in narrative.answer
    assert "상품과의 교집합" not in narrative.answer


def test_cache_failure_body_reaches_hclx_without_changing_terminal_status() -> None:
    plan = _holding_plan()
    acquisition = SimpleNamespace(
        search_query_count=0,
        discovered_candidate_count=0,
        failure_codes=("search_not_exhaustive", "source_scope_incomplete"),
    )
    result = ExternalKnowledgeRuntimeResult.model_construct(
        outcome=ExternalKnowledgeOutcome(
            status=ExternalKnowledgeOutcomeStatus.CANNOT_VERIFY,
            search_complete=False,
            reason_codes=("live_exact_evidence_span_missing",),
        ),
        search_plan=SimpleNamespace(composite_plan=plan),
        candidate_universe=SimpleNamespace(),
        acquisitions=(acquisition,),
        execution=None,
        identity_evidence=(),
    )
    backend = external_knowledge_result_to_backend(result)
    provider = _CapturingProvider()
    service = SimpleNamespace(
        official_answer_provider=provider,
        require_official_hclx=True,
    )

    finalized = _finalize_with_official_hclx(service, plan.question, backend)

    assert backend.status is BackendStatus.CANNOT_VERIFY
    assert provider.context is not None
    assert provider.context.status == "cannot_verify"
    assert provider.context.deterministic_answer == backend.answer
    assert backend.answer in finalized.answer
    assert finalized.status is BackendStatus.CANNOT_VERIFY
    assert finalized.answer_mode is BackendAnswerMode.LLM_GROUNDED


def test_internal_failure_code_selects_message_but_is_never_exposed() -> None:
    narrative = build_external_cannot_verify_narrative(
        _holding_plan(),
        reason_codes=("external_search_budget_exhausted",),
    )

    assert narrative.failure_kind == "bounded_retrieval_incomplete"
    assert "제한된 공식 근거 확인 범위" in narrative.answer
    assert "external_search_budget_exhausted" not in narrative.answer
