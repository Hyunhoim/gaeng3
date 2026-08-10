from __future__ import annotations

from functools import partial
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Response
from finance_agent_core.agent import (
    execute_answer_request,
    invalid_official_request_response,
    official_response_from_backend,
    official_timeout_response,
)
from finance_agent_core.contracts.backend import BackendAgentRequest, BackendAgentResponse
from finance_agent_core.contracts.official import OfficialAnswerResponse

from app.answer_controls import (
    backend_overloaded_response,
    backend_timeout_response,
    official_overloaded_response,
)
from app.config import Settings
from app.dependencies import AgentService, get_agent, get_settings
from app.request_execution import (
    RequestExecutionTimeoutError,
    RequestOverloadedError,
    execute_bounded_request,
)

router = APIRouter(tags=["answer"])

_ERROR_RESPONSES = {
    status_code: {"model": BackendAgentResponse} for status_code in (422, 500, 502, 503, 504)
}


@router.post(
    "/answer",
    response_model=BackendAgentResponse,
    responses=_ERROR_RESPONSES,
)
async def answer(
    request: BackendAgentRequest,
    response: Response,
    service: Annotated[AgentService, Depends(get_agent)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> BackendAgentResponse:
    """Expose the framework-neutral adapter without changing its DTO semantics."""

    try:
        result = await execute_bounded_request(
            partial(execute_answer_request, service, request),
            timeout_seconds=settings.official_answer_timeout_seconds,
            max_inflight=settings.official_answer_max_inflight,
        )
    except RequestOverloadedError:
        response.status_code = 503
        return backend_overloaded_response(request.request_id)
    except RequestExecutionTimeoutError:
        response.status_code = 504
        return backend_timeout_response(request.request_id)
    response.status_code = result.http_status_code
    return result.response


@router.get(
    "/answer",
    response_model=OfficialAnswerResponse,
)
async def official_answer(
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
        return invalid_official_request_response(
            question_id=question_id,
            question=question,
        )
    request = BackendAgentRequest(request_id=question_id, question=question)
    try:
        result = await execute_bounded_request(
            partial(execute_answer_request, service, request),
            timeout_seconds=settings.official_answer_timeout_seconds,
            max_inflight=settings.official_answer_max_inflight,
        )
    except RequestOverloadedError:
        return official_overloaded_response(
            question_id=question_id,
            question=question,
        )
    except RequestExecutionTimeoutError:
        return official_timeout_response(
            question_id=question_id,
            question=question,
        )
    if result.http_status_code == 504:
        return official_timeout_response(
            question_id=question_id,
            question=question,
        )
    return official_response_from_backend(
        question_id=question_id,
        question=question,
        response=result.response,
    )
