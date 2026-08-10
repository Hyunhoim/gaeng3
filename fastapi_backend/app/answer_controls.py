from __future__ import annotations

import json

from finance_agent_core.contracts.backend import (
    BackendAgentResponse,
    BackendAnswerMode,
    BackendError,
    BackendErrorCode,
    BackendStatus,
)
from finance_agent_core.contracts.official import OfficialAnswerResponse
from finance_agent_core.contracts.routing import InteractionIntent

_OVERLOADED_MESSAGE = "현재 요청이 많아 처리할 수 없습니다. 잠시 후 다시 시도해 주세요."
_TIMEOUT_MESSAGE = "요청 처리 시간이 초과되었습니다. 잠시 후 다시 시도해 주세요."


def _backend_error_response(
    *,
    request_id: str,
    message: str,
) -> BackendAgentResponse:
    return BackendAgentResponse(
        request_id=request_id,
        status=BackendStatus.ERROR,
        intent=InteractionIntent.UNSUPPORTED,
        product_families=[],
        answer=message,
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
            code=BackendErrorCode.PROVIDER_UNAVAILABLE,
            message=message,
            retryable=True,
        ),
        source_manifest=None,
    )


def backend_overloaded_response(request_id: str) -> BackendAgentResponse:
    return _backend_error_response(request_id=request_id, message=_OVERLOADED_MESSAGE)


def backend_timeout_response(request_id: str) -> BackendAgentResponse:
    return _backend_error_response(request_id=request_id, message=_TIMEOUT_MESSAGE)


def _json_text(payload: dict[str, object]) -> str:
    return json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def official_overloaded_response(
    *,
    question_id: str,
    question: str,
) -> OfficialAnswerResponse:
    """Return the fixed five-string contract without starting Agent work."""

    return OfficialAnswerResponse(
        question_id=question_id,
        question=question,
        retrieved_context=_json_text(
            {"citations": [], "reason": "동시 요청 제한으로 검색을 실행하지 않음"}
        ),
        think_trace=_json_text(
            {
                "trace_type": "structured_execution_summary_not_hidden_reasoning",
                "status": "error",
                "execution_steps": ["admission_control", "safe_control_response"],
                "control_code": "request_overloaded",
            }
        ),
        answer=_OVERLOADED_MESSAGE,
    )
