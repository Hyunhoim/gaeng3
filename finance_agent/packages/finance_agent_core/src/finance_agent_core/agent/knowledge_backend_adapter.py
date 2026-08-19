from __future__ import annotations

from time import perf_counter

from finance_agent_core.agent.knowledge_service import KnowledgeAgentResult
from finance_agent_core.answering.claims import KnowledgeAnswerComposition
from finance_agent_core.config import QualityStatus
from finance_agent_core.contracts.backend import (
    BackendAgentResponse,
    BackendAnswerMode,
    BackendStatus,
    SourceCitation,
)
from finance_agent_core.contracts.knowledge import RelationKnowledgeOperation
from finance_agent_core.contracts.routing import InteractionIntent
from finance_agent_core.domain import FieldEvidence, ProductEvidence
from finance_agent_core.observability import (
    AuditOutcome,
    AuditStage,
    current_request_audit,
)
from finance_agent_core.retrieval.models import DocumentEvidence
from finance_agent_core.retrieval.relations import RelationEvidence

_RELATION_WARNING = (
    "관계는 주최 측 제공 데이터의 표기를 그대로 조회한 결과이며 투자 추천이나 "
    "인과관계를 뜻하지 않습니다."
)


def _answer_mode(composition: KnowledgeAnswerComposition) -> BackendAnswerMode:
    return {
        "deterministic": BackendAnswerMode.DETERMINISTIC,
        "structured_grounded": BackendAnswerMode.LLM_GROUNDED,
        "deterministic_fallback": BackendAnswerMode.DETERMINISTIC_FALLBACK,
    }[composition.mode]


def _relation_product(evidence: RelationEvidence) -> ProductEvidence:
    return ProductEvidence(
        product_id=evidence.product_id,
        product_name=evidence.product_name,
        ticker=evidence.ticker,
        fields=[
            FieldEvidence(
                canonical_field=evidence.canonical_field,
                source_dataset=evidence.source_dataset,
                source_id=evidence.source_id,
                source_key={
                    "product_id": evidence.product_id,
                    "relation_id": evidence.relation_id,
                    "entity_id": evidence.entity_id,
                },
                source_row=evidence.source_row,
                source_columns=list(evidence.source_columns),
                raw_values={evidence.canonical_field: evidence.entity_label},
                normalized_value=evidence.entity_label,
                unit="text",
                as_of=evidence.as_of,
                quality=QualityStatus.VALID,
                quality_reason=None,
            )
        ],
    )


def _relation_citation(evidence: RelationEvidence) -> SourceCitation:
    return SourceCitation(
        citation_id=f"relation:{evidence.evidence_id}",
        kind="relation_field",
        label=f"{evidence.product_name} · {evidence.canonical_field}",
        source_id=evidence.source_id,
        source_locator=(
            f"{evidence.source_dataset} row {evidence.source_row} · "
            f"{'/'.join(evidence.source_columns)} · relation {evidence.relation_id}"
        ),
        as_of=evidence.as_of,
        evidence_refs=[f"{evidence.product_id}:{evidence.canonical_field}"],
    )


def _document_citation(evidence: DocumentEvidence) -> SourceCitation:
    return SourceCitation(
        citation_id=f"document:{evidence.evidence_id}",
        kind="document_chunk",
        label=f"{evidence.title} · 문서 근거",
        source_id=evidence.document_id,
        source_locator=f"{evidence.source_uri} · chunk {evidence.chunk_ordinal}",
        as_of=evidence.as_of,
        evidence_refs=[evidence.evidence_id],
    )


def knowledge_result_to_backend(result: KnowledgeAgentResult) -> BackendAgentResponse:
    """Project verified knowledge evidence onto the existing Backend v1 DTO."""

    if type(result) is not KnowledgeAgentResult:
        raise TypeError("result must be a KnowledgeAgentResult")
    started = perf_counter()
    relation_evidence = (
        list(result.relation_response.evidence) if result.relation_response is not None else []
    )
    document_evidence = (
        list(result.document_response.evidence) if result.document_response is not None else []
    )
    products = [_relation_product(item) for item in relation_evidence]
    citations = [
        *(_relation_citation(item) for item in relation_evidence),
        *(_document_citation(item) for item in document_evidence),
    ]
    answer_mode = _answer_mode(result.answer)
    operation = result.plan.operation
    response = BackendAgentResponse(
        request_id=result.plan.question_id,
        status=(BackendStatus.SUCCESS if result.status == "found" else BackendStatus.NOT_FOUND),
        intent=(
            InteractionIntent.SEARCH
            if isinstance(operation, RelationKnowledgeOperation)
            else InteractionIntent.EXPLAIN
        ),
        product_families=(
            list(operation.product_families)
            if isinstance(operation, RelationKnowledgeOperation)
            else []
        ),
        answer=result.answer.answer,
        query_plan=result.plan,
        candidate_count=result.candidate_count,
        products=products,
        comparisons=[],
        aggregates=[],
        documents=document_evidence,
        citations=citations,
        as_of_dates=sorted(
            {
                *(item.as_of for item in relation_evidence),
                *(item.as_of for item in document_evidence),
            }
        ),
        warnings=(
            [_RELATION_WARNING]
            if relation_evidence and isinstance(operation, RelationKnowledgeOperation)
            else []
        ),
        answer_mode=answer_mode,
        fallback_used=answer_mode is BackendAnswerMode.DETERMINISTIC_FALLBACK,
        provider_model=result.answer.model_name,
        clarification=None,
        error=None,
        source_manifest=None,
        family_searches=[],
        source_manifests=[],
    )
    audit = current_request_audit()
    if audit is not None:
        audit.emit(
            stage=AuditStage.SERIALIZATION,
            outcome=AuditOutcome.SUCCEEDED,
            reason_code="knowledge_backend_dto_built",
            duration_ms=(perf_counter() - started) * 1000,
            candidate_count=result.candidate_count,
            result_count=len(products) + len(document_evidence),
            evidence_count=len(citations),
        )
    return response


__all__ = ["knowledge_result_to_backend"]
