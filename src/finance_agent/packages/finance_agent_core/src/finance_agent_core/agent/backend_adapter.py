from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict, model_validator

from finance_agent_core.agent.external_knowledge_router import (
    ExternalKnowledgeRouteDecision,
)
from finance_agent_core.agent.hybrid_query import HybridExecutionAborted
from finance_agent_core.agent.knowledge_router import (
    KnowledgeRouteDecision,
    KnowledgeRoutedExecutionError,
)
from finance_agent_core.agent.knowledge_service import KnowledgeServiceError
from finance_agent_core.agent.providers.hyperclova import (
    HyperClovaXAuthenticationError,
    HyperClovaXConfigurationError,
    HyperClovaXProviderError,
    HyperClovaXRateLimitError,
    HyperClovaXResponseError,
    HyperClovaXServiceError,
    HyperClovaXTimeoutError,
    HyperClovaXTransportError,
    hyperclova_failure_reason,
)
from finance_agent_core.agent.router import IntentRouter
from finance_agent_core.answering.final_generation import (
    FinalAnswerContext,
    build_clarification_guidance,
    compose_final_answer,
)
from finance_agent_core.contracts.backend import (
    BackendAgentRequest,
    BackendAgentResponse,
    BackendAnswerMode,
    BackendError,
    BackendErrorCode,
    BackendStatus,
    routed_result_to_backend,
)
from finance_agent_core.contracts.knowledge import (
    KnowledgePlanAuthorityError,
    RelationKnowledgeOperation,
)
from finance_agent_core.contracts.routing import (
    InteractionIntent,
    RouteDecision,
    RoutedExecutionError,
)
from finance_agent_core.deadline import (
    RequestDeadlineExceeded,
    current_request_deadline,
)
from finance_agent_core.execution.authority import (
    PlanAuthorityCode,
    PlanAuthorityError,
)
from finance_agent_core.observability import (
    AuditOutcome,
    AuditStage,
    MetricCounter,
    current_request_audit,
)
from finance_agent_core.release import AgentReleaseError
from finance_agent_core.retrieval.relations import RelationIndexError
from finance_agent_core.storage import DatasetApprovalError


class RoutedAnswerService(Protocol):
    router: IntentRouter

    def answer(self, question: str, request_id: str): ...

    def assert_public_response_boundary_current(self) -> None: ...


class AnswerAdapterResult(BaseModel):
    """Framework-neutral result consumed by a future FastAPI /answer route."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    http_status_code: Literal[200, 500, 502, 503, 504]
    response: BackendAgentResponse

    @model_validator(mode="after")
    def validate_http_and_body_status(self) -> AnswerAdapterResult:
        is_error = self.response.status is BackendStatus.ERROR
        if is_error == (self.http_status_code == 200):
            raise ValueError("HTTP status and Backend error status differ")
        return self


def _finalize_with_official_hclx(
    service: RoutedAnswerService,
    question: str,
    response: BackendAgentResponse,
) -> BackendAgentResponse:
    provider = getattr(service, "official_answer_provider", None)
    required = bool(getattr(service, "require_official_hclx", False))
    if provider is None:
        if required:
            raise HyperClovaXConfigurationError(
                "official response requires HyperCLOVA X answer generation"
            )
        return response
    # Runtime-checkable Protocol is intentionally avoided; check the small
    # approved seam without invoking arbitrary provider methods.
    if not callable(getattr(provider, "generate_final_answer", None)):
        raise HyperClovaXConfigurationError("invalid official answer provider")
    if response.status is BackendStatus.ERROR:
        return response
    clarification_guidance = None
    if response.status is BackendStatus.CLARIFICATION:
        if response.clarification is None:
            raise HyperClovaXConfigurationError("clarification response is missing server guidance")
        clarification_guidance = build_clarification_guidance(
            question=question,
            reason_code=response.clarification.code,
            required_fields=response.clarification.required_fields,
            product_families=[family.value for family in response.product_families],
            existing_options=response.clarification.options,
        )
    context = FinalAnswerContext(
        question=question,
        status=response.status.value,
        intent=response.intent.value,
        product_families=[family.value for family in response.product_families],
        candidate_count=response.candidate_count,
        evidence_count=(
            sum(len(product.fields) for product in response.products)
            + sum(len(comparison.cells) for comparison in response.comparisons)
            + len(response.aggregates)
            + len(response.documents)
        ),
        citation_count=len(response.citations),
        as_of_dates=[value.isoformat() for value in response.as_of_dates],
        deterministic_answer=response.answer,
        clarification_guidance=clarification_guidance,
    )
    audit = current_request_audit()
    if audit is not None:
        try:
            audit.sink.metrics.increment(MetricCounter.HCLX_CALLS)
        except Exception:
            pass
    try:
        composition = compose_final_answer(context, provider)
    except Exception as error:
        if audit is not None:
            reason = hyperclova_failure_reason(error)
            audit.emit(
                stage=AuditStage.HCLX,
                outcome=(AuditOutcome.TIMED_OUT if reason == "timed_out" else AuditOutcome.FAILED),
                reason_code=f"final_generation_{reason}",
                duration_ms=0,
            )
        raise
    if audit is not None:
        # Documents are evidence for the returned products, not additional
        # product results. Counting them as results can make result_count exceed
        # candidate_count on verified external-knowledge responses and causes
        # the fail-closed AuditEvent contract to reject the terminal HCLX events.
        returned_product_count = len(response.products)
        audit_candidate_count = response.candidate_count
        if audit_candidate_count is None and response.independent_sections is not None:
            # The public hybrid contract deliberately has no cross-section
            # candidate count.  AuditEvent still requires its internal bound
            # to cover every returned product, so use the sum of the two
            # already verified section counts without exposing it in the DTO.
            audit_candidate_count = sum(
                section.candidate_count or 0 for section in response.independent_sections.sections
            )
        audit_candidate_count = audit_candidate_count or 0
        audit.emit(
            stage=AuditStage.HCLX,
            outcome=AuditOutcome.SUCCEEDED,
            reason_code="final_generation_completed",
            duration_ms=composition.generation_latency_ms,
            candidate_count=audit_candidate_count,
            result_count=returned_product_count,
            evidence_count=context.evidence_count,
        )
        audit.emit(
            stage=AuditStage.VERIFIER,
            outcome=AuditOutcome.SUCCEEDED,
            reason_code="final_generation_verified",
            duration_ms=0,
            candidate_count=audit_candidate_count,
            result_count=returned_product_count,
            evidence_count=context.evidence_count,
        )
    updates: dict[str, object] = {"answer": composition.answer}
    if composition.clarification_question is not None:
        assert response.clarification is not None
        assert clarification_guidance is not None
        updates["clarification"] = response.clarification.model_copy(
            update={
                "message": composition.clarification_question,
                "required_fields": clarification_guidance.missing_slots,
                "options": clarification_guidance.allowed_options,
            }
        )
    if response.status in {
        BackendStatus.SUCCESS,
        BackendStatus.NOT_FOUND,
        BackendStatus.CANNOT_VERIFY,
    }:
        updates.update(
            answer_mode=BackendAnswerMode.LLM_GROUNDED,
            fallback_used=False,
            provider_model=composition.model_name,
        )
    return BackendAgentResponse.model_validate({**response.model_dump(mode="python"), **updates})


def _is_safety_terminal_result(result: object) -> bool:
    """Return whether the server stopped a request at SafetyEnvelope.

    Safety controls are deliberately deterministic.  In particular, blocked
    user text is sent to neither the Intent provider nor the final-answer
    provider, even when official HCLX is mandatory for allowed evaluation
    queries.
    """

    decision = getattr(result, "decision", None)
    reason_code = getattr(decision, "reason_code", "")
    disposition = getattr(decision, "disposition", None)
    return (
        isinstance(reason_code, str)
        and reason_code.startswith("safety_")
        and getattr(disposition, "value", None) in {"clarify", "unsupported"}
    )


def _assert_final_public_boundary_current(service: RoutedAnswerService) -> None:
    """Discard a generated answer if its release or approved DB changed."""

    boundary = getattr(service, "assert_public_response_boundary_current", None)
    if callable(boundary):
        boundary()


@dataclass(frozen=True, slots=True)
class _ErrorMapping:
    http_status_code: Literal[500, 502, 503, 504]
    code: BackendErrorCode
    message: str
    retryable: bool


_PROVIDER_UNAVAILABLE = "현재 AI provider를 사용할 수 없습니다. 잠시 후 다시 시도해 주세요."
_PROVIDER_TIMEOUT = "AI provider 응답 시간이 초과되었습니다. 잠시 후 다시 시도해 주세요."
_PROVIDER_INVALID_RESPONSE = (
    "AI provider 응답을 안전하게 검증하지 못했습니다. 잠시 후 다시 시도해 주세요."
)
_DATASET_UNAVAILABLE = "금융상품 데이터에 접근할 수 없습니다. 잠시 후 다시 시도해 주세요."
_REQUEST_TIMEOUT = "요청 처리 시간이 초과되었습니다. 잠시 후 다시 시도해 주세요."
_RELEASE_UNAVAILABLE = "승인된 Agent 배포 상태를 확인할 수 없습니다. 잠시 후 다시 시도해 주세요."
_INTERNAL_ERROR = "요청 처리 중 내부 오류가 발생했습니다."


def _map_error(error: Exception) -> _ErrorMapping:
    if isinstance(error, RequestDeadlineExceeded):
        return _ErrorMapping(
            http_status_code=504,
            code=BackendErrorCode.PROVIDER_UNAVAILABLE,
            message=_REQUEST_TIMEOUT,
            retryable=True,
        )
    if isinstance(error, AgentReleaseError):
        return _ErrorMapping(
            http_status_code=503,
            code=BackendErrorCode.INTERNAL_ERROR,
            message=_RELEASE_UNAVAILABLE,
            retryable=True,
        )
    if isinstance(error, KnowledgePlanAuthorityError):
        return _ErrorMapping(
            http_status_code=500,
            code=BackendErrorCode.INTERNAL_ERROR,
            message=_INTERNAL_ERROR,
            retryable=False,
        )
    if isinstance(error, PlanAuthorityError):
        if error.code is PlanAuthorityCode.DEADLINE_EXCEEDED:
            return _ErrorMapping(
                http_status_code=504,
                code=BackendErrorCode.PROVIDER_UNAVAILABLE,
                message=_REQUEST_TIMEOUT,
                retryable=True,
            )
        if error.code is PlanAuthorityCode.RELEASE_MISMATCH:
            return _ErrorMapping(
                http_status_code=503,
                code=BackendErrorCode.INTERNAL_ERROR,
                message=_RELEASE_UNAVAILABLE,
                retryable=True,
            )
        if error.code in {
            PlanAuthorityCode.DATASET_NOT_CONFIGURED,
            PlanAuthorityCode.DATASET_MISMATCH,
            PlanAuthorityCode.STALE_AUTHORITY_CONTEXT,
        }:
            return _ErrorMapping(
                http_status_code=503,
                code=BackendErrorCode.DATASET_UNAVAILABLE,
                message=_DATASET_UNAVAILABLE,
                retryable=True,
            )
    deadline = current_request_deadline()
    if (
        isinstance(error, sqlite3.OperationalError)
        and "interrupted" in str(error).casefold()
        and deadline is not None
        and deadline.should_stop()
    ):
        return _ErrorMapping(
            http_status_code=504,
            code=BackendErrorCode.PROVIDER_UNAVAILABLE,
            message=_REQUEST_TIMEOUT,
            retryable=True,
        )
    if isinstance(
        error,
        (HyperClovaXConfigurationError, HyperClovaXAuthenticationError),
    ):
        return _ErrorMapping(
            http_status_code=503,
            code=BackendErrorCode.PROVIDER_UNAVAILABLE,
            message=_PROVIDER_UNAVAILABLE,
            retryable=False,
        )
    if isinstance(error, HyperClovaXTimeoutError):
        return _ErrorMapping(
            http_status_code=504,
            code=BackendErrorCode.PROVIDER_UNAVAILABLE,
            message=_PROVIDER_TIMEOUT,
            retryable=True,
        )
    if isinstance(error, HyperClovaXResponseError):
        return _ErrorMapping(
            http_status_code=502,
            code=BackendErrorCode.PROVIDER_UNAVAILABLE,
            message=_PROVIDER_INVALID_RESPONSE,
            retryable=True,
        )
    if isinstance(
        error,
        (
            HyperClovaXRateLimitError,
            HyperClovaXServiceError,
            HyperClovaXTransportError,
            HyperClovaXProviderError,
        ),
    ):
        return _ErrorMapping(
            http_status_code=503,
            code=BackendErrorCode.PROVIDER_UNAVAILABLE,
            message=_PROVIDER_UNAVAILABLE,
            retryable=True,
        )
    if isinstance(
        error,
        (
            DatasetApprovalError,
            KnowledgeServiceError,
            RelationIndexError,
            sqlite3.Error,
            OSError,
        ),
    ):
        return _ErrorMapping(
            http_status_code=503,
            code=BackendErrorCode.DATASET_UNAVAILABLE,
            message=_DATASET_UNAVAILABLE,
            retryable=True,
        )
    return _ErrorMapping(
        http_status_code=500,
        code=BackendErrorCode.INTERNAL_ERROR,
        message=_INTERNAL_ERROR,
        retryable=False,
    )


def _error_response(
    request: BackendAgentRequest,
    decision: RouteDecision | KnowledgeRouteDecision | ExternalKnowledgeRouteDecision | None,
    mapping: _ErrorMapping,
) -> BackendAgentResponse:
    if isinstance(decision, RouteDecision):
        intent = decision.draft.intent
        product_families = decision.draft.product_families
    elif isinstance(decision, KnowledgeRouteDecision) and decision.plan is not None:
        operation = decision.plan.operation
        intent = (
            InteractionIntent.SEARCH
            if isinstance(operation, RelationKnowledgeOperation)
            else InteractionIntent.EXPLAIN
        )
        product_families = (
            list(operation.product_families)
            if isinstance(operation, RelationKnowledgeOperation)
            else []
        )
    elif isinstance(decision, ExternalKnowledgeRouteDecision) and decision.plan is not None:
        intent = decision.plan.interaction_intent
        product_families = sorted(
            {
                family
                for requirement in decision.plan.requirements
                for family in requirement.product_families
            },
            key=lambda family: family.value,
        )
    else:
        intent = InteractionIntent.UNSUPPORTED
        product_families = []
    return BackendAgentResponse(
        request_id=request.request_id,
        status=BackendStatus.ERROR,
        intent=intent,
        product_families=product_families,
        answer=mapping.message,
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
        clarification=None,
        error=BackendError(
            code=mapping.code,
            message=mapping.message,
            retryable=mapping.retryable,
        ),
        source_manifest=None,
    )


def execute_answer_request(
    service: RoutedAnswerService,
    request: BackendAgentRequest,
) -> AnswerAdapterResult:
    """Run one validated request and normalize safe HTTP/body semantics."""

    answer_public_atomically = getattr(service, "_answer_public_atomically", None)
    if callable(answer_public_atomically):
        try:
            public_result = answer_public_atomically(request.question, request.request_id)
            from finance_agent_core.agent.external_knowledge_backend_adapter import (
                external_knowledge_route_to_backend,
            )
            from finance_agent_core.agent.external_knowledge_result_adapter import (
                external_knowledge_result_to_backend,
            )
            from finance_agent_core.agent.external_knowledge_router import (
                ExternalKnowledgeRouteDecision,
            )
            from finance_agent_core.agent.independent_hybrid_result_adapter import (
                independent_hybrid_result_to_backend,
            )
            from finance_agent_core.agent.knowledge_backend_adapter import (
                knowledge_result_to_backend,
                knowledge_route_control_to_backend,
            )
            from finance_agent_core.agent.knowledge_service import KnowledgeAgentResult
            from finance_agent_core.agent.routed_service import (
                ExactIdentityNotFoundResult,
                IndependentHybridAgentResult,
                RoutedAgentResult,
            )
            from finance_agent_core.execution.external_knowledge_runtime import (
                ExternalKnowledgeRuntimeResult,
            )

            if type(public_result) is RoutedAgentResult:
                response = routed_result_to_backend(public_result)
            elif type(public_result) is IndependentHybridAgentResult:
                response = independent_hybrid_result_to_backend(public_result)
            elif type(public_result) is ExactIdentityNotFoundResult:
                response = BackendAgentResponse(
                    request_id=public_result.request_id,
                    status=BackendStatus.NOT_FOUND,
                    intent=InteractionIntent.DETAIL,
                    product_families=public_result.checked_families,
                    answer=public_result.answer,
                    query_plan=None,
                    candidate_count=0,
                    products=[],
                    comparisons=[],
                    aggregates=[],
                    documents=[],
                    citations=[],
                    as_of_dates=[],
                    warnings=[],
                    answer_mode=BackendAnswerMode.DETERMINISTIC,
                    fallback_used=False,
                    provider_model=None,
                    clarification=None,
                    error=None,
                    source_manifest=None,
                    family_searches=[],
                    source_manifests=[],
                )
            elif type(public_result) is KnowledgeAgentResult:
                response = knowledge_result_to_backend(public_result)
            elif type(public_result) is KnowledgeRouteDecision:
                response = knowledge_route_control_to_backend(
                    public_result,
                    request_id=request.request_id,
                )
            elif type(public_result) is ExternalKnowledgeRouteDecision:
                response = external_knowledge_route_to_backend(
                    public_result,
                    request_id=request.request_id,
                )
            elif type(public_result) is ExternalKnowledgeRuntimeResult:
                response = external_knowledge_result_to_backend(public_result)
            else:
                raise TypeError("public Agent returned an unsupported result contract")
            if not _is_safety_terminal_result(public_result):
                try:
                    response = _finalize_with_official_hclx(
                        service,
                        request.question,
                        response,
                    )
                except Exception:
                    # A release/DB trust change that happens during the provider
                    # call outranks a malformed provider draft.  Re-check the
                    # immutable boundary before exposing the provider error so
                    # stale generated text is always discarded as 503.
                    _assert_final_public_boundary_current(service)
                    raise
            adapter_result = AnswerAdapterResult(http_status_code=200, response=response)
            # This must be the last operation before returning the public DTO.
            # It closes the final-generation race for both release bindings and
            # approved DB artifacts.
            _assert_final_public_boundary_current(service)
            return adapter_result
        except Exception as error:  # noqa: BLE001 - outer application boundary
            trusted_decision = None
            actual_error = error
            if isinstance(error, RoutedExecutionError):
                trusted_decision = error.decision
                actual_error = error.cause
            elif isinstance(error, KnowledgeRoutedExecutionError):
                trusted_decision = error.decision
                actual_error = error.cause
            elif isinstance(error, HybridExecutionAborted) and error.cause is not None:
                # Preserve the existing timeout/provider/dataset HTTP mapping.
                # The wrapper means only that atomic publication was aborted;
                # it must not erase the verified infrastructure cause.
                actual_error = error.cause
            mapping = _map_error(actual_error)
            return AnswerAdapterResult(
                http_status_code=mapping.http_status_code,
                response=_error_response(request, trusted_decision, mapping),
            )

    answer_atomically = getattr(service, "_answer_atomically", None)
    if callable(answer_atomically):
        try:
            routed = answer_atomically(request.question, request.request_id)
            response = routed_result_to_backend(routed)
            response = _finalize_with_official_hclx(service, request.question, response)
            return AnswerAdapterResult(http_status_code=200, response=response)
        except Exception as error:  # noqa: BLE001 - outer application boundary
            trusted_decision = None
            actual_error = error
            if isinstance(error, RoutedExecutionError):
                trusted_decision = error.decision
                actual_error = error.cause
            mapping = _map_error(actual_error)
            return AnswerAdapterResult(
                http_status_code=mapping.http_status_code,
                response=_error_response(request, trusted_decision, mapping),
            )

    try:
        decision = service.router.route(request.question, request.request_id)
    except Exception as error:  # noqa: BLE001 - normalize the outer routing boundary
        mapping = _map_error(error)
        return AnswerAdapterResult(
            http_status_code=mapping.http_status_code,
            response=_error_response(request, None, mapping),
        )
    try:
        routed = service.answer(request.question, request.request_id)
        response = routed_result_to_backend(routed)
        response = _finalize_with_official_hclx(service, request.question, response)
        return AnswerAdapterResult(http_status_code=200, response=response)
    except Exception as error:  # noqa: BLE001 - this is the outer application boundary
        mapping = _map_error(error)
        return AnswerAdapterResult(
            http_status_code=mapping.http_status_code,
            response=_error_response(request, decision, mapping),
        )
