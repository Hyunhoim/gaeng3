from __future__ import annotations

from typing import Any

from fastapi import Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from finance_agent_core.contracts.backend import (
    BackendAgentResponse,
    BackendAnswerMode,
    BackendError,
    BackendErrorCode,
    BackendStatus,
)
from finance_agent_core.contracts.routing import InteractionIntent
from finance_agent_core.observability import AuditOutcome

from app.http_audit import mark_request_audit_terminal

_INVALID_REQUEST_ID = "invalid-request"
_INVALID_REQUEST_MESSAGE = "요청 형식이 올바르지 않습니다. 입력값을 확인해 주세요."


def _safe_request_id(body: Any) -> str:
    """Keep a valid caller ID without reflecting malformed request content."""

    if not isinstance(body, dict):
        return _INVALID_REQUEST_ID
    request_id = body.get("request_id")
    if not isinstance(request_id, str):
        return _INVALID_REQUEST_ID
    stripped = request_id.strip()
    if not stripped or len(stripped) > 128:
        return _INVALID_REQUEST_ID
    return stripped


def _invalid_request_response(request_id: str) -> BackendAgentResponse:
    return BackendAgentResponse(
        request_id=request_id,
        status=BackendStatus.ERROR,
        intent=InteractionIntent.UNSUPPORTED,
        product_families=[],
        answer=_INVALID_REQUEST_MESSAGE,
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
            code=BackendErrorCode.INVALID_REQUEST,
            message=_INVALID_REQUEST_MESSAGE,
            retryable=False,
        ),
        source_manifest=None,
    )


async def request_validation_error_response(
    request: Request,
    error: RequestValidationError,
) -> JSONResponse:
    """Return the public Backend DTO without exposing validation internals."""

    mark_request_audit_terminal(
        request,
        outcome=AuditOutcome.BLOCKED,
        reason_code="invalid_input",
    )
    response = _invalid_request_response(_safe_request_id(error.body))
    return JSONResponse(status_code=422, content=response.model_dump(mode="json"))
