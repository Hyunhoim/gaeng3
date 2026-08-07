from __future__ import annotations

import json
from typing import Any

from finance_agent_core.contracts.backend import BackendAgentResponse, BackendStatus
from finance_agent_core.contracts.official import OfficialAnswerResponse

_INVALID_REQUEST_MESSAGE = "요청 형식이 올바르지 않습니다. question_id와 question을 확인해 주세요."


def _json_text(payload: dict[str, Any]) -> str:
    return json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _retrieved_context(response: BackendAgentResponse) -> str:
    if not response.citations:
        reason = {
            BackendStatus.CLARIFICATION: "조건 확인 필요",
            BackendStatus.UNSUPPORTED: "현재 데이터·기능 범위에서 지원하지 않음",
            BackendStatus.NOT_FOUND: "조건에 맞는 근거를 찾지 못함",
            BackendStatus.ERROR: "안전하게 공개할 수 있는 근거 없음",
        }.get(response.status, "참조 근거 없음")
        return _json_text({"citations": [], "reason": reason})
    return _json_text(
        {
            "citations": [
                {
                    "citation_id": citation.citation_id,
                    "kind": citation.kind,
                    "label": citation.label,
                    "source_id": citation.source_id,
                    "source_locator": citation.source_locator,
                    "as_of": citation.as_of.isoformat(),
                    "evidence_refs": citation.evidence_refs,
                }
                for citation in response.citations
            ]
        }
    )


def _think_trace(response: BackendAgentResponse) -> str:
    plan = response.query_plan
    execution_steps = ["intent_router"]
    if plan is not None:
        execution_steps.extend(
            [
                "query_plan_validation",
                "deterministic_oracle",
                "result_verifier",
                "field_evidence_builder",
            ]
        )
        if response.answer_mode.value in {"llm_grounded", "deterministic_fallback"}:
            execution_steps.append("answer_verifier")
        execution_steps.append("response_contract_validation")
    else:
        execution_steps.append("safe_control_response")
    return _json_text(
        {
            "trace_type": "structured_execution_summary_not_hidden_reasoning",
            "status": response.status.value,
            "intent": response.intent.value,
            "product_families": [family.value for family in response.product_families],
            "execution_steps": execution_steps,
            "filters": []
            if plan is None
            else [
                {
                    "field": constraint.field,
                    "operator": constraint.operator.value,
                    "value": constraint.value,
                    "unit": constraint.unit.value,
                }
                for constraint in plan.constraints
            ],
            "ranking": []
            if plan is None
            else [ranking.model_dump(mode="json") for ranking in plan.ranking],
            "candidate_count": response.candidate_count,
            "returned_evidence": {
                "products": len(response.products),
                "comparisons": len(response.comparisons),
                "aggregates": len(response.aggregates),
                "documents": len(response.documents),
                "citations": len(response.citations),
            },
            "as_of_dates": [value.isoformat() for value in response.as_of_dates],
            "answer_mode": response.answer_mode.value,
            "fallback_used": response.fallback_used,
            "control_code": (
                response.clarification.code
                if response.clarification is not None
                else response.error.code.value
                if response.error is not None
                else None
            ),
        }
    )


def official_response_from_backend(
    *,
    question_id: str,
    question: str,
    response: BackendAgentResponse,
) -> OfficialAnswerResponse:
    """Project a validated internal DTO onto the fixed public five-string contract."""

    return OfficialAnswerResponse(
        question_id=question_id,
        question=question,
        retrieved_context=_retrieved_context(response),
        think_trace=_think_trace(response),
        answer=response.answer,
    )


def invalid_official_request_response(
    *,
    question_id: str | None,
    question: str | None,
) -> OfficialAnswerResponse:
    """Keep the official schema and HTTP 200 even when required query values are invalid."""

    safe_question_id = (
        question_id
        if isinstance(question_id, str) and question_id.strip() and len(question_id) <= 128
        else "invalid-question-id"
    )
    safe_question = question[:2000] if isinstance(question, str) else ""
    return OfficialAnswerResponse(
        question_id=safe_question_id,
        question=safe_question,
        retrieved_context=_json_text(
            {"citations": [], "reason": "요청 형식 오류로 검색을 실행하지 않음"}
        ),
        think_trace=_json_text(
            {
                "trace_type": "structured_execution_summary_not_hidden_reasoning",
                "status": "error",
                "execution_steps": ["request_validation", "safe_control_response"],
                "control_code": "invalid_request",
            }
        ),
        answer=_INVALID_REQUEST_MESSAGE,
    )
