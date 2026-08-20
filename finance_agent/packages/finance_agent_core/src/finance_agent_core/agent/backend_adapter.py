from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict, model_validator

from finance_agent_core.agent.knowledge_router import (
    KnowledgeRouteDecision,
    KnowledgeRoutedExecutionError,
)
from finance_agent_core.agent.knowledge_service import KnowledgeServiceError
from finance_agent_core.agent.providers import (
    HyperClovaXAuthenticationError,
    HyperClovaXConfigurationError,
    HyperClovaXProviderError,
    HyperClovaXRateLimitError,
    HyperClovaXResponseError,
    HyperClovaXServiceError,
    HyperClovaXTimeoutError,
    HyperClovaXTransportError,
)
from finance_agent_core.agent.router import IntentRouter
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
from finance_agent_core.release import AgentReleaseError
from finance_agent_core.retrieval.relations import RelationIndexError
from finance_agent_core.storage import DatasetApprovalError


class RoutedAnswerService(Protocol):
    router: IntentRouter

    def answer(self, question: str, request_id: str): ...


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
    decision: RouteDecision | KnowledgeRouteDecision | None,
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
            from finance_agent_core.agent.knowledge_backend_adapter import (
                knowledge_result_to_backend,
                knowledge_route_control_to_backend,
            )
            from finance_agent_core.agent.knowledge_service import KnowledgeAgentResult
            from finance_agent_core.agent.routed_service import RoutedAgentResult

            if type(public_result) is RoutedAgentResult:
                response = routed_result_to_backend(public_result)
            elif type(public_result) is KnowledgeAgentResult:
                response = knowledge_result_to_backend(public_result)
            elif type(public_result) is KnowledgeRouteDecision:
                response = knowledge_route_control_to_backend(
                    public_result,
                    request_id=request.request_id,
                )
            else:
                raise TypeError("public Agent returned an unsupported result contract")
            return AnswerAdapterResult(http_status_code=200, response=response)
        except Exception as error:  # noqa: BLE001 - outer application boundary
            trusted_decision = None
            actual_error = error
            if isinstance(error, RoutedExecutionError):
                trusted_decision = error.decision
                actual_error = error.cause
            elif isinstance(error, KnowledgeRoutedExecutionError):
                trusted_decision = error.decision
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
        return AnswerAdapterResult(http_status_code=200, response=response)
    except Exception as error:  # noqa: BLE001 - this is the outer application boundary
        mapping = _map_error(error)
        return AnswerAdapterResult(
            http_status_code=mapping.http_status_code,
            response=_error_response(request, decision, mapping),
        )
