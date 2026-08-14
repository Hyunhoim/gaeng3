from __future__ import annotations

from contextlib import nullcontext
from functools import partial
from time import perf_counter
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request, Response
from fastapi.responses import JSONResponse
from finance_agent_core.agent import (
    execute_answer_request,
    invalid_official_request_response,
    official_response_from_backend,
    official_timeout_response,
)
from finance_agent_core.contracts.backend import (
    BackendAgentRequest,
    BackendAgentResponse,
    BackendStatus,
)
from finance_agent_core.contracts.official import OfficialAnswerResponse
from finance_agent_core.observability import AuditOutcome, AuditStage, bind_request_audit

from app.answer_controls import (
    backend_overloaded_response,
    backend_timeout_response,
    official_overloaded_response,
)
from app.config import Settings
from app.dependencies import (
    AgentService,
    get_agent,
    get_settings,
    request_audit_recorder,
)
from app.http_audit import mark_request_audit_terminal, mark_response_serialization_start
from app.request_execution import (
    RequestExecutionTimeoutError,
    RequestOverloadedError,
    execute_bounded_request,
)

router = APIRouter(tags=["answer"])

_ERROR_RESPONSES = {
    status_code: {"model": BackendAgentResponse} for status_code in (422, 500, 502, 503, 504)
}


class OfficialJsonResponse(JSONResponse):
    """Return the exact UTF-8 JSON media type required by the evaluator."""

    media_type = "application/json; charset=utf-8"


@router.post(
    "/answer",
    response_model=BackendAgentResponse,
    responses=_ERROR_RESPONSES,
)
async def answer(
    request: BackendAgentRequest,
    response: Response,
    http_request: Request,
    service: Annotated[AgentService, Depends(get_agent)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> BackendAgentResponse:
    """Expose the framework-neutral adapter without changing its DTO semantics."""

    audit = request_audit_recorder(
        service,
        request=http_request,
        request_id=request.request_id,
        question=request.question,
    )
    audit_context = bind_request_audit(audit) if audit is not None else nullcontext()
    with audit_context:
        try:
            result = await execute_bounded_request(
                partial(execute_answer_request, service, request),
                timeout_seconds=settings.official_answer_timeout_seconds,
                max_inflight=settings.official_answer_max_inflight,
            )
        except RequestOverloadedError:
            mark_request_audit_terminal(
                http_request,
                outcome=AuditOutcome.BLOCKED,
                reason_code="admission_rejected",
            )
            response.status_code = 503
            return backend_overloaded_response(request.request_id)
        except RequestExecutionTimeoutError:
            mark_request_audit_terminal(
                http_request,
                outcome=AuditOutcome.TIMED_OUT,
                reason_code="deadline_exceeded",
            )
            response.status_code = 504
            return backend_timeout_response(request.request_id)
    response.status_code = result.http_status_code
    adapter_timed_out = result.http_status_code == 504
    mark_request_audit_terminal(
        http_request,
        outcome=(
            AuditOutcome.TIMED_OUT
            if adapter_timed_out
            else (AuditOutcome.SUCCEEDED if result.http_status_code == 200 else AuditOutcome.FAILED)
        ),
        reason_code=(
            "deadline_exceeded"
            if adapter_timed_out
            else ("response_completed" if result.http_status_code == 200 else "adapter_failure")
        ),
    )
    if result.response.status in {BackendStatus.SUCCESS, BackendStatus.NOT_FOUND}:
        mark_response_serialization_start(http_request)
    return result.response


@router.get(
    "/answer",
    response_model=OfficialAnswerResponse,
    response_class=OfficialJsonResponse,
)
async def official_answer(
    http_request: Request,
    service: Annotated[AgentService, Depends(get_agent)],
    settings: Annotated[Settings, Depends(get_settings)],
    question_id: Annotated[str | None, Query()] = None,
    question: Annotated[str | None, Query()] = None,
) -> OfficialAnswerResponse:
    """Expose the briefing's five-string GET contract with fail-safe HTTP 200 semantics."""

    if (
        question_id is None
        or not question_id.strip()
        or len(question_id) > 128
        or question is None
        or not question.strip()
        or len(question) > 2000
    ):
        mark_request_audit_terminal(
            http_request,
            outcome=AuditOutcome.BLOCKED,
            reason_code="invalid_input",
        )
        return invalid_official_request_response(
            question_id=question_id,
            question=question,
        )
    request = BackendAgentRequest(request_id=question_id, question=question)
    audit = request_audit_recorder(
        service,
        request=http_request,
        request_id=question_id,
        question=question,
    )
    audit_context = bind_request_audit(audit) if audit is not None else nullcontext()
    with audit_context:
        try:
            result = await execute_bounded_request(
                partial(execute_answer_request, service, request),
                timeout_seconds=settings.official_answer_timeout_seconds,
                max_inflight=settings.official_answer_max_inflight,
            )
        except RequestOverloadedError:
            mark_request_audit_terminal(
                http_request,
                outcome=AuditOutcome.BLOCKED,
                reason_code="admission_rejected",
            )
            return official_overloaded_response(
                question_id=question_id,
                question=question,
            )
        except RequestExecutionTimeoutError:
            mark_request_audit_terminal(
                http_request,
                outcome=AuditOutcome.TIMED_OUT,
                reason_code="deadline_exceeded",
            )
            return official_timeout_response(
                question_id=question_id,
                question=question,
            )
    if result.http_status_code == 504:
        mark_request_audit_terminal(
            http_request,
            outcome=AuditOutcome.TIMED_OUT,
            reason_code="deadline_exceeded",
        )
        return official_timeout_response(
            question_id=question_id,
            question=question,
        )
    official_dto_started = perf_counter()
    response = official_response_from_backend(
        question_id=question_id,
        question=question,
        response=result.response,
    )
    if audit is not None and result.response.status in {
        BackendStatus.SUCCESS,
        BackendStatus.NOT_FOUND,
    }:
        audit.emit(
            stage=AuditStage.SERIALIZATION,
            outcome=AuditOutcome.SUCCEEDED,
            reason_code="official_dto_built",
            duration_ms=(perf_counter() - official_dto_started) * 1000,
            candidate_count=result.response.candidate_count or 0,
            result_count=len(result.response.products),
            evidence_count=len(
                {
                    evidence_ref
                    for citation in result.response.citations
                    for evidence_ref in citation.evidence_refs
                }
            ),
        )
    mark_request_audit_terminal(
        http_request,
        outcome=(AuditOutcome.SUCCEEDED if result.http_status_code == 200 else AuditOutcome.FAILED),
        reason_code=("response_completed" if result.http_status_code == 200 else "adapter_failure"),
    )
    if result.response.status in {BackendStatus.SUCCESS, BackendStatus.NOT_FOUND}:
        mark_response_serialization_start(http_request)
    return response
