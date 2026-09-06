from __future__ import annotations

import json
from typing import Any

from finance_agent_core.answering.independent_sections import (
    IndependentSectionBundle,
    IndependentSectionId,
    namespace_section_reference,
)
from finance_agent_core.contracts.backend import BackendAgentResponse, BackendStatus
from finance_agent_core.contracts.external_knowledge import CompositeKnowledgeQueryPlan
from finance_agent_core.contracts.independent_hybrid import IndependentHybridPlan
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


def _hybrid_plan_summary(plan: IndependentHybridPlan) -> dict[str, Any]:
    """Expose immutable authority metadata, never hidden reasoning or raw prompts."""

    def ranges_payload(ranges: tuple[Any, ...]) -> list[dict[str, Any]]:
        return [
            {
                "source_start": item.source_start,
                "source_end": item.source_end,
                "source_text_sha256": item.source_text_sha256,
            }
            for item in ranges
        ]

    return {
        "plan_kind": plan.plan_kind,
        "combination_policy": plan.combination_policy,
        "plan_sha256": plan.canonical_sha256,
        "question_sha256": plan.question_sha256,
        "combination_policy_ranges": ranges_payload(plan.combination_policy_ranges),
        "sections": [
            {
                "section_kind": section.section_kind,
                "source_ranges": ranges_payload(section.source_ranges),
                "subplan_kind": section.subplan.subplan_kind,
                "subplan_question_sha256": section.subplan.question_sha256,
                "subplan_sha256": section.subplan.plan_sha256,
            }
            for section in (plan.external_section, plan.database_section)
        ],
    }


def _independent_sections_payload(
    bundle: IndependentSectionBundle,
    *,
    visible_evidence_refs: set[str],
    visible_citation_ids: set[str],
    visible_product_ids: dict[IndependentSectionId, tuple[str, ...]],
) -> dict[str, Any]:
    """Project the signed boundary and an exact, bounded public evidence view."""

    section_payloads: list[dict[str, Any]] = []
    for section in bundle.sections:
        section_evidence_refs = tuple(
            item for item in section.evidence_refs if item in visible_evidence_refs
        )
        section_citation_ids = tuple(
            item for item in section.citation_ids if item in visible_citation_ids
        )
        public_product_ids = visible_product_ids.get(section.section_id, ())
        ranking: dict[str, Any] | None = None
        if section.ranking is not None:
            public_product_id_set = set(public_product_ids)
            public_order = tuple(
                item
                for item in section.ranking.ordered_product_ids
                if item in public_product_id_set
            )
            ranking = {
                "field_id": section.ranking.field_id,
                "direction": section.ranking.direction,
                "limit": section.ranking.limit,
                "value_source": section.ranking.value_source,
                "ordered_product_ids": public_order,
                "ordered_product_count": len(section.ranking.ordered_product_ids),
                "ordered_products_truncated": (
                    len(public_order) != len(section.ranking.ordered_product_ids)
                ),
            }
        section_payloads.append(
            {
                "section_id": section.section_id.value,
                "status": section.status.value,
                "plan_kind": section.plan_kind,
                "plan_sha256": section.plan_sha256,
                "candidate_count": section.candidate_count,
                "product_ids": public_product_ids,
                "product_count": len(section.product_ids),
                "products_truncated": len(public_product_ids) != len(section.product_ids),
                "evidence_refs": section_evidence_refs,
                "evidence_count": len(section.evidence_refs),
                "evidence_truncated": len(section_evidence_refs) != len(section.evidence_refs),
                "citation_ids": section_citation_ids,
                "citation_count": len(section.citation_ids),
                "citations_truncated": len(section_citation_ids) != len(section.citation_ids),
                "ranking": ranking,
            }
        )
    return {
        "schema_version": bundle.schema_version,
        "combination_policy": bundle.combination_policy,
        "ranking_notice": bundle.ranking_notice,
        "section_manifest_sha256": bundle.section_manifest_sha256,
        "answer_body_sha256": bundle.answer_body_sha256,
        "sections": section_payloads,
        "citations": [
            item.model_dump(mode="json")
            for item in bundle.citations
            if item.citation_id in visible_citation_ids
        ],
    }


def _hybrid_product_owners(
    response: BackendAgentResponse,
    *,
    visible_product_count: int,
) -> tuple[tuple[IndependentSectionId, str, str], ...]:
    """Bind each public ProductEvidence to its section and product family."""

    bundle = response.independent_sections
    if bundle is None:
        return ()
    expected = tuple(
        (section.section_id, product_key)
        for section in bundle.sections
        for product_key in section.product_ids
    )
    if len(response.products) != len(expected):
        raise ValueError("hybrid products and section product IDs differ")
    owners: list[tuple[IndependentSectionId, str, str]] = []
    for product, (section_id, product_key) in zip(
        response.products,
        expected,
        strict=True,
    ):
        family, separator, product_id = product_key.partition(":")
        if not separator or not family or product_id != product.product_id:
            raise ValueError("hybrid section product key differs from public product evidence")
        owners.append((section_id, family, product_key))
    return tuple(owners[:visible_product_count])


def _retrieved_context(response: BackendAgentResponse) -> str:
    independent_sections = response.independent_sections
    if independent_sections is None and not (
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
            BackendStatus.CANNOT_VERIFY: "공식 외부 근거 탐색이 불완전해 확인할 수 없음",
            BackendStatus.ERROR: "안전하게 공개할 수 있는 근거 없음",
        }.get(response.status, "참조 근거 없음")
        return _json_text({"citations": [], "reason": reason})
    products = response.products[:_MAX_CONTEXT_PRODUCTS]
    comparisons = response.comparisons[:_MAX_CONTEXT_COMPARISONS]
    aggregates = response.aggregates[:_MAX_CONTEXT_AGGREGATES]
    documents = response.documents[:_MAX_CONTEXT_DOCUMENTS]
    is_external = isinstance(response.query_plan, CompositeKnowledgeQueryPlan)
    is_hybrid = isinstance(response.query_plan, IndependentHybridPlan)
    if is_hybrid != (independent_sections is not None):
        raise ValueError("hybrid plan and independent section bundle differ")
    if is_hybrid:
        assert independent_sections is not None
        complete_section_citations = {
            item.citation_id: item.evidence_refs for item in independent_sections.citations
        }
        complete_public_citations = {
            item.citation_id: tuple(item.evidence_refs) for item in response.citations
        }
        if complete_section_citations != complete_public_citations:
            raise ValueError("hybrid section and public citations differ")
    hybrid_product_owners = _hybrid_product_owners(
        response,
        visible_product_count=len(products),
    )

    def product_field_ref(product_index: int, product_id: str, field: Any) -> str:
        if is_hybrid:
            section_id, family, _ = hybrid_product_owners[product_index]
            raw_ref = (
                f"provided:{family}:{product_id}:{field.canonical_field}"
                if section_id is IndependentSectionId.OFFICIAL_EXTERNAL_EVIDENCE
                else f"{product_id}:{field.canonical_field}"
            )
            return namespace_section_reference(section_id, raw_ref)
        if is_external:
            family = field.source_key.get("product_family")
            if family:
                return f"provided:{family}:{product_id}:{field.canonical_field}"
        return f"{product_id}:{field.canonical_field}"

    def comparison_ref(evidence_ref: str) -> str:
        if not is_hybrid:
            return evidence_ref
        return namespace_section_reference(
            IndependentSectionId.PROVIDED_DATABASE_COMPOUND,
            evidence_ref,
        )

    def aggregate_ref(evidence_ref: str) -> str:
        if not is_hybrid:
            return evidence_ref
        return namespace_section_reference(
            IndependentSectionId.PROVIDED_DATABASE_COMPOUND,
            evidence_ref,
        )

    def document_ref(evidence_ref: str) -> str:
        if not is_hybrid:
            return evidence_ref
        return namespace_section_reference(
            IndependentSectionId.OFFICIAL_EXTERNAL_EVIDENCE,
            evidence_ref,
        )

    visible_evidence_refs = {
        product_field_ref(product_index, product.product_id, field)
        for product_index, product in enumerate(products)
        for field in product.fields[:_MAX_CONTEXT_FIELDS_PER_PRODUCT]
    }
    visible_evidence_refs.update(
        comparison_ref(cell.evidence_ref)
        for comparison in comparisons
        for cell in comparison.cells
        if cell.evidence_ref is not None
    )
    visible_evidence_refs.update(
        aggregate_ref(aggregate.evidence_id)
        for aggregate in aggregates
    )
    visible_evidence_refs.update(
        document_ref(document.evidence_id)
        for document in documents
    )
    # Public citations may never point at evidence removed by one of the
    # bounded context limits.  For external multi-family results this also
    # ensures that family-qualified product references survive unchanged.
    citations = [
        citation
        for citation in response.citations
        if set(citation.evidence_refs) <= visible_evidence_refs
    ][:_MAX_CONTEXT_CITATIONS]
    cited_evidence_refs = {
        evidence_ref for citation in citations for evidence_ref in citation.evidence_refs
    }
    published_products: list[tuple[int, Any, tuple[Any, ...]]] = []
    for product_index, product in enumerate(products):
        fields = tuple(product.fields[:_MAX_CONTEXT_FIELDS_PER_PRODUCT])
        if is_external or is_hybrid:
            fields = tuple(
                field
                for field in fields
                if product_field_ref(product_index, product.product_id, field)
                in cited_evidence_refs
            )
        if fields or not (is_external or is_hybrid):
            published_products.append((product_index, product, fields))
    if is_external or is_hybrid:
        published_comparisons = [
            comparison
            for comparison in comparisons
            if (
                refs := {
                    comparison_ref(cell.evidence_ref)
                    for cell in comparison.cells
                    if cell.evidence_ref is not None
                }
            )
            and refs <= cited_evidence_refs
        ]
        published_aggregates = [
            aggregate
            for aggregate in aggregates
            if aggregate_ref(aggregate.evidence_id) in cited_evidence_refs
        ]
        published_documents = [
            document
            for document in documents
            if document_ref(document.evidence_id) in cited_evidence_refs
        ]
        published_evidence_refs = {
            product_field_ref(product_index, product.product_id, field)
            for product_index, product, fields in published_products
            for field in fields
        }
        published_evidence_refs.update(
            comparison_ref(cell.evidence_ref)
            for comparison in published_comparisons
            for cell in comparison.cells
            if cell.evidence_ref is not None
        )
        published_evidence_refs.update(
            aggregate_ref(aggregate.evidence_id) for aggregate in published_aggregates
        )
        published_evidence_refs.update(
            document_ref(document.evidence_id) for document in published_documents
        )
        if published_evidence_refs != cited_evidence_refs:
            raise ValueError("public context evidence and citation references differ")
        visible_evidence_refs = published_evidence_refs
    else:
        published_comparisons = comparisons
        published_aggregates = aggregates
        published_documents = documents
    visible_hybrid_product_ids: dict[IndependentSectionId, tuple[str, ...]] = {}
    if is_hybrid:
        if independent_sections is None:
            raise ValueError("hybrid public context requires independent sections")
        section_evidence_refs = {
            evidence_ref
            for section in independent_sections.sections
            for evidence_ref in section.evidence_refs
        }
        if not visible_evidence_refs <= section_evidence_refs:
            raise ValueError("public evidence escaped the hybrid section authority")
        visible_citation_ids = {item.citation_id for item in citations}
        section_citation_map = {
            item.citation_id: item.evidence_refs
            for item in independent_sections.citations
            if item.citation_id in visible_citation_ids
        }
        public_citation_map = {
            item.citation_id: tuple(item.evidence_refs) for item in citations
        }
        if section_citation_map != public_citation_map:
            raise ValueError("hybrid section and public citations differ")
        visible_hybrid_product_ids = {
            section_id: tuple(
                hybrid_product_owners[product_index][2]
                for product_index, _product, _fields in published_products
                if hybrid_product_owners[product_index][0] == section_id
            )
            for section_id in IndependentSectionId
        }
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
                                "evidence_ref": product_field_ref(
                                    product_index,
                                    product.product_id,
                                    field,
                                ),
                                "field": field.canonical_field,
                                "value": field.normalized_value,
                                "unit": field.unit,
                                "quality": field.quality.value,
                                "as_of": field.as_of.isoformat(),
                            }
                            for field in fields
                        ],
                        "field_count": len(product.fields),
                        "fields_truncated": len(product.fields) > len(fields),
                    }
                    for product_index, product, fields in published_products
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
                                "evidence_ref": (
                                    namespace_section_reference(
                                        IndependentSectionId.PROVIDED_DATABASE_COMPOUND,
                                        cell.evidence_ref,
                                    )
                                    if is_hybrid and cell.evidence_ref is not None
                                    else cell.evidence_ref
                                ),
                            }
                            for cell in comparison.cells
                        ],
                    }
                    for comparison in published_comparisons
                ],
                "aggregates": [
                    {
                        "evidence_ref": (
                            namespace_section_reference(
                                IndependentSectionId.PROVIDED_DATABASE_COMPOUND,
                                aggregate.evidence_id,
                            )
                            if is_hybrid
                            else aggregate.evidence_id
                        ),
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
                    for aggregate in published_aggregates
                ],
                "documents": [
                    {
                        "evidence_ref": (
                            namespace_section_reference(
                                IndependentSectionId.OFFICIAL_EXTERNAL_EVIDENCE,
                                document.evidence_id,
                            )
                            if is_hybrid
                            else document.evidence_id
                        ),
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
                    for document in published_documents
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
            **(
                {
                    "independent_hybrid_plan": _hybrid_plan_summary(response.query_plan),
                    "independent_sections": _independent_sections_payload(
                        independent_sections,
                        visible_evidence_refs=visible_evidence_refs,
                        visible_citation_ids={item.citation_id for item in citations},
                        visible_product_ids=visible_hybrid_product_ids,
                    ),
                }
                if is_hybrid and independent_sections is not None
                else {}
            ),
            "truncation": {
                "products": len(response.products) > len(published_products),
                "comparisons": len(response.comparisons) > len(published_comparisons),
                "aggregates": len(response.aggregates) > len(published_aggregates),
                "documents": len(response.documents) > len(published_documents),
                "citations": len(response.citations) > len(citations),
            },
        }
    )


def _think_trace(response: BackendAgentResponse) -> str:
    plan = response.query_plan
    execution_steps = ["intent_router"]
    independent_sections = response.independent_sections
    if isinstance(plan, IndependentHybridPlan):
        if independent_sections is None:
            raise ValueError("hybrid execution trace requires independent sections")
        execution_steps.extend(
            [
                "independent_hybrid_plan_validation",
                "all_sections_authorized_before_execution",
                "official_external_evidence_retrieval",
                "provided_db_candidate_intersection_verification",
                "independent_database_compound_execution",
                "result_verifier",
                "atomic_section_completion_barrier",
                "no_cross_section_ranking_validation",
            ]
        )
        if response.answer_mode.value in {"llm_grounded", "deterministic_fallback"}:
            execution_steps.append("grounded_answer_verifier")
        execution_steps.append("response_contract_validation")
        filters = [
            {
                "section_id": section.section_id.value,
                "plan_kind": section.plan_kind,
                "plan_sha256": section.plan_sha256,
                "status": section.status.value,
                "candidate_count": section.candidate_count,
            }
            for section in independent_sections.sections
        ]
        ranking = [
            {
                "section_id": section.section_id.value,
                **section.ranking.model_dump(mode="json"),
            }
            for section in independent_sections.sections
            if section.ranking is not None
        ]
    elif isinstance(plan, CompositeKnowledgeQueryPlan):
        external_terminal = {
            BackendStatus.SUCCESS: "verified_external_db_intersection",
            BackendStatus.CANNOT_VERIFY: "safe_cannot_verify_response",
            BackendStatus.NOT_FOUND: "safe_not_found_response",
        }.get(response.status, "safe_control_response")
        execution_steps.extend(
            [
                "composite_knowledge_plan_validation",
                "provided_db_candidate_freeze",
                "official_evidence_authority_check",
                "temporal_relation_verifier",
                external_terminal,
                "response_contract_validation",
            ]
        )
        filters = [
            {
                "requirement_id": requirement.requirement_id,
                "capability": requirement.capability.value,
                "predicate": requirement.predicate.value,
                "subject_evidence_span": requirement.subject_evidence_span,
                "relation_evidence_span": requirement.relation_evidence_span,
                "product_families": [family.value for family in requirement.product_families],
            }
            for requirement in plan.requirements
        ]
        ranking = (
            [plan.product_selection.model_dump(mode="json")]
            if plan.product_selection is not None
            else []
        )
    elif isinstance(plan, KnowledgeQueryPlan):
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
            **(
                {
                    "combination_policy": independent_sections.combination_policy,
                    "ranking_notice": independent_sections.ranking_notice,
                    "section_manifest_sha256": (
                        independent_sections.section_manifest_sha256
                    ),
                    "sections": [
                        {
                            "section_id": section.section_id.value,
                            "status": section.status.value,
                            "candidate_count": section.candidate_count,
                            "returned_products": len(section.product_ids),
                            "evidence_count": len(section.evidence_refs),
                            "citation_count": len(section.citation_ids),
                        }
                        for section in independent_sections.sections
                    ],
                }
                if isinstance(plan, IndependentHybridPlan)
                and independent_sections is not None
                else {}
            ),
            **(
                {"constraint_expression": plan.constraint_expression.model_dump(mode="json")}
                if getattr(plan, "constraint_expression", None) is not None
                else {}
            ),
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
