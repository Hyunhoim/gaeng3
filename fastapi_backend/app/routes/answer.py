from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Response
from finance_agent_core.agent import execute_answer_request
from finance_agent_core.contracts.backend import BackendAgentRequest, BackendAgentResponse

from app.dependencies import AgentService, get_agent

router = APIRouter(tags=["answer"])

_ERROR_RESPONSES = {
    status_code: {"model": BackendAgentResponse} for status_code in (422, 500, 502, 503, 504)
}


@router.post(
    "/answer",
    response_model=BackendAgentResponse,
    responses=_ERROR_RESPONSES,
)
def answer(
    request: BackendAgentRequest,
    response: Response,
    service: Annotated[AgentService, Depends(get_agent)],
) -> BackendAgentResponse:
    """Expose the framework-neutral adapter without changing its DTO semantics."""

    result = execute_answer_request(service, request)
    response.status_code = result.http_status_code
    return result.response
