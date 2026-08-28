from __future__ import annotations

from time import perf_counter

from finance_agent_core.agent.external_knowledge_router import (
    ExternalKnowledgeRouteDecision,
    ExternalKnowledgeRouteDisposition,
)
from finance_agent_core.contracts.backend import (
    BackendAgentResponse,
    BackendAnswerMode,
    BackendClarification,
    BackendStatus,
)
from finance_agent_core.contracts.routing import InteractionIntent
from finance_agent_core.observability import (
    AuditOutcome,
    AuditStage,
    current_request_audit,
)

_EVIDENCE_CANNOT_VERIFY = (
    "현재 공식 외부 자료 검색과 근거 검증을 끝까지 완료하지 못해 제공 데이터 내 "
    "해당 상품의 존재 여부를 확인할 수 없습니다. 일부 조건을 무시한 상품 검색은 "
    "수행하지 않았습니다."
)


def external_knowledge_route_to_backend(
    decision: ExternalKnowledgeRouteDecision,
    *,
    request_id: str,
) -> BackendAgentResponse:
    """Project a non-executed external-knowledge route onto Backend v1.

    A KNOWLEDGE_SEARCH decision is a valid financial-product question, not an
    out-of-domain request. Until a complete verified evidence bundle is
    attached, it is CANNOT_VERIFY and carries no asserted evidence.
    """

    if type(decision) is not ExternalKnowledgeRouteDecision:
        raise TypeError("decision must be an ExternalKnowledgeRouteDecision")
    if not request_id.strip():
        raise ValueError("request_id cannot be blank")
    if decision.disposition in {
        ExternalKnowledgeRouteDisposition.NOT_APPLICABLE,
        ExternalKnowledgeRouteDisposition.EXECUTE,
    }:
        raise ValueError("only non-executable external knowledge routes can be projected")

    started = perf_counter()
    if decision.disposition is ExternalKnowledgeRouteDisposition.KNOWLEDGE_SEARCH:
        assert decision.plan is not None
        families = sorted(
            {
                family
                for requirement in decision.plan.requirements
                for family in requirement.product_families
            },
            key=lambda family: family.value,
        )
        response = BackendAgentResponse(
            request_id=request_id,
            status=BackendStatus.CANNOT_VERIFY,
            intent=decision.plan.interaction_intent,
            product_families=families,
            answer=_EVIDENCE_CANNOT_VERIFY,
            query_plan=decision.plan,
            candidate_count=None,
            products=[],
            comparisons=[],
            aggregates=[],
            documents=[],
            citations=[],
            as_of_dates=[],
            warnings=[],
            answer_mode=BackendAnswerMode.CONTROL,
            fallback_used=False,
            provider_model=None,
            clarification=None,
            error=None,
            source_manifest=None,
            family_searches=[],
            source_manifests=[],
        )
        audit_outcome = AuditOutcome.SUCCEEDED
        audit_reason = "external_knowledge_cannot_verify"
    else:
        clarify = decision.disposition is ExternalKnowledgeRouteDisposition.CLARIFY
        response = BackendAgentResponse(
            request_id=request_id,
            status=(BackendStatus.CLARIFICATION if clarify else BackendStatus.UNSUPPORTED),
            intent=(
                InteractionIntent.CLARIFY
                if clarify
                else InteractionIntent.UNSUPPORTED
            ),
            product_families=[],
            answer=(
                "정확한 공식 근거를 찾으려면 대상 상품·기업과 관계 조건을 더 "
                "구체적으로 알려주세요."
                if clarify
                else "제공 데이터와 허용된 공식 금융상품 근거 범위에서 처리할 수 없습니다."
            ),
            query_plan=None,
            candidate_count=None,
            products=[],
            comparisons=[],
            aggregates=[],
            documents=[],
            citations=[],
            as_of_dates=[],
            warnings=[],
            answer_mode=BackendAnswerMode.CONTROL,
            fallback_used=False,
            provider_model=None,
            clarification=(
                BackendClarification(
                    code=decision.reason_code,
                    message=decision.reason,
                    required_fields=["knowledge_subject", "knowledge_relation"],
                    options=[],
                )
                if clarify
                else None
            ),
            error=None,
            source_manifest=None,
            family_searches=[],
            source_manifests=[],
        )
        audit_outcome = AuditOutcome.CLARIFIED if clarify else AuditOutcome.UNSUPPORTED
        audit_reason = (
            "external_knowledge_clarified"
            if clarify
            else "external_knowledge_unsupported"
        )

    audit = current_request_audit()
    if audit is not None:
        audit.emit(
            stage=AuditStage.SERIALIZATION,
            outcome=audit_outcome,
            reason_code=audit_reason,
            duration_ms=(perf_counter() - started) * 1000,
            candidate_count=response.candidate_count or 0,
            result_count=0,
            evidence_count=0,
        )
    return response


__all__ = ["external_knowledge_route_to_backend"]
