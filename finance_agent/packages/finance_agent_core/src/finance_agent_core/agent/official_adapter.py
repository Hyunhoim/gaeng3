from __future__ import annotations

import json
from typing import Any

from finance_agent_core.contracts.backend import BackendAgentResponse, BackendStatus
from finance_agent_core.contracts.knowledge import (
    KnowledgeQueryPlan,
    RelationKnowledgeOperation,
)
from finance_agent_core.contracts.official import OfficialAnswerResponse

_INVALID_REQUEST_MESSAGE = "요청 형식이 올바르지 않습니다. question_id와 question을 확인해 주세요."
_TIMEOUT_MESSAGE = "요청 처리 시간이 초과되었습니다. 잠시 후 다시 시도해 주세요."
_MAX_CONTEXT_PRODUCTS = 20
_MAX_CONTEXT_FIELDS_PER_PRODUCT = 20
_MAX_CONTEXT_COMPARISONS = 40
_MAX_CONTEXT_AGGREGATES = 40
_MAX_CONTEXT_DOCUMENTS = 10
_MAX_CONTEXT_DOCUMENT_CHARS = 2_000
_MAX_CONTEXT_CITATIONS = 400


def _json_text(payload: dict[str, Any]) -> str:
    return json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _retrieved_context(response: BackendAgentResponse) -> str:
    if not (
        response.products
        or response.comparisons
        or response.aggregates
        or response.documents
        or response.citations
    ):
        reason = {
            BackendStatus.CLARIFICATION: "조건 확인 필요",
            BackendStatus.UNSUPPORTED: "현재 데이터·기능 범위에서 지원하지 않음",
            BackendStatus.NOT_FOUND: "조건에 맞는 근거를 찾지 못함",
            BackendStatus.ERROR: "안전하게 공개할 수 있는 근거 없음",
        }.get(response.status, "참조 근거 없음")
        return _json_text({"citations": [], "reason": reason})
    products = response.products[:_MAX_CONTEXT_PRODUCTS]
    comparisons = response.comparisons[:_MAX_CONTEXT_COMPARISONS]
    aggregates = response.aggregates[:_MAX_CONTEXT_AGGREGATES]
    documents = response.documents[:_MAX_CONTEXT_DOCUMENTS]
    citations = response.citations[:_MAX_CONTEXT_CITATIONS]
    return _json_text(
        {
            "schema_version": "1.0",
            "evidence": {
                "products": [
                    {
                        "product_id": product.product_id,
                        "product_name": product.product_name,
                        "ticker": product.ticker,
                        "fields": [
                            {
                                "evidence_ref": f"{product.product_id}:{field.canonical_field}",
                                "field": field.canonical_field,
                                "value": field.normalized_value,
                                "unit": field.unit,
                                "quality": field.quality.value,
                                "as_of": field.as_of.isoformat(),
                            }
                            for field in product.fields[:_MAX_CONTEXT_FIELDS_PER_PRODUCT]
                        ],
                        "field_count": len(product.fields),
                        "fields_truncated": (len(product.fields) > _MAX_CONTEXT_FIELDS_PER_PRODUCT),
                    }
                    for product in products
                ],
                "comparisons": [
                    {
                        "field": comparison.canonical_field,
                        "label": comparison.label,
                        "unit": comparison.unit,
                        "status": comparison.status,
                        "delta": comparison.delta,
                        "delta_basis": comparison.delta_basis,
                        "cells": [
                            {
                                "target_index": cell.target_index,
                                "product_id": cell.product_id,
                                "product_name": cell.product_name,
                                "value": cell.value,
                                "trading_currency": cell.trading_currency,
                                "as_of": (
                                    cell.as_of.isoformat() if cell.as_of is not None else None
                                ),
                                "evidence_ref": cell.evidence_ref,
                            }
                            for cell in comparison.cells
                        ],
                    }
                    for comparison in comparisons
                ],
                "aggregates": [
                    {
                        "evidence_ref": aggregate.evidence_id,
                        "function": aggregate.function.value,
                        "field": aggregate.field,
                        "label": aggregate.label,
                        "value": aggregate.value,
                        "unit": aggregate.unit,
                        "group_values": aggregate.group_values,
                        "row_count": aggregate.row_count,
                        "valid_count": aggregate.valid_count,
                        "missing_count": aggregate.missing_count,
                        "as_of_start": (
                            aggregate.as_of_start.isoformat()
                            if aggregate.as_of_start is not None
                            else None
                        ),
                        "as_of_end": (
                            aggregate.as_of_end.isoformat()
                            if aggregate.as_of_end is not None
                            else None
                        ),
                        "source_snapshot_date": aggregate.source_snapshot_date.isoformat(),
                    }
                    for aggregate in aggregates
                ],
                "documents": [
                    {
                        "evidence_ref": document.evidence_id,
                        "document_id": document.document_id,
                        "chunk_ordinal": document.chunk_ordinal,
                        "title": document.title,
                        "text": document.text[:_MAX_CONTEXT_DOCUMENT_CHARS],
                        "text_truncated": len(document.text) > _MAX_CONTEXT_DOCUMENT_CHARS,
                        "source_uri": document.source_uri,
                        "source_kind": document.source_kind.value,
                        "as_of": document.as_of.isoformat(),
                        "document_sha256": document.document_sha256,
                    }
                    for document in documents
                ],
            },
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
                for citation in citations
            ],
            "truncation": {
                "products": len(response.products) > len(products),
                "comparisons": len(response.comparisons) > len(comparisons),
                "aggregates": len(response.aggregates) > len(aggregates),
                "documents": len(response.documents) > len(documents),
                "citations": len(response.citations) > len(citations),
            },
        }
    )


def _think_trace(response: BackendAgentResponse) -> str:
    plan = response.query_plan
    execution_steps = ["intent_router"]
    if isinstance(plan, KnowledgeQueryPlan):
        is_relation = isinstance(plan.operation, RelationKnowledgeOperation)
        execution_steps.extend(
            [
                "knowledge_plan_validation",
                "relation_retrieval" if is_relation else "document_retrieval",
                "claim_verifier",
                "response_contract_validation",
            ]
        )
        if is_relation:
            filters: list[dict[str, Any]] = [
                {
                    "relation_type": plan.operation.relation_types[0].value,
                    "query": plan.operation.query,
                    "top_k": plan.operation.top_k,
                }
            ]
        else:
            filters = [
                {
                    "source_kinds": [item.value for item in plan.operation.source_kinds],
                    "document_ids": list(plan.operation.document_ids),
                    "top_k": plan.operation.top_k,
                }
            ]
        ranking: list[dict[str, Any]] = []
    elif plan is not None:
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
        filters = [
            {
                "field": constraint.field,
                "operator": constraint.operator.value,
                "value": constraint.value,
                "unit": constraint.unit.value,
            }
            for constraint in plan.constraints
        ]
        ranking = [item.model_dump(mode="json") for item in plan.ranking]
    else:
        execution_steps.append("safe_control_response")
        filters = []
        ranking = []
    return _json_text(
        {
            "trace_type": "structured_execution_summary_not_hidden_reasoning",
            "status": response.status.value,
            "intent": response.intent.value,
            "product_families": [family.value for family in response.product_families],
            "execution_steps": execution_steps,
            "filters": filters,
            "ranking": ranking,
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


def official_timeout_response(
    *,
    question_id: str,
    question: str,
) -> OfficialAnswerResponse:
    """Return a fixed public response when the outer evaluation budget expires."""

    return OfficialAnswerResponse(
        question_id=question_id,
        question=question,
        retrieved_context=_json_text(
            {"citations": [], "reason": "시간 제한으로 근거 처리를 완료하지 못함"}
        ),
        think_trace=_json_text(
            {
                "trace_type": "structured_execution_summary_not_hidden_reasoning",
                "status": "error",
                "execution_steps": ["request_time_budget", "safe_control_response"],
                "control_code": "request_timeout",
            }
        ),
        answer=_TIMEOUT_MESSAGE,
    )
