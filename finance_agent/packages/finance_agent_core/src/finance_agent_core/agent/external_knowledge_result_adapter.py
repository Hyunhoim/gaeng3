from __future__ import annotations

from decimal import Decimal

from finance_agent_core.config import QualityStatus
from finance_agent_core.contracts.backend import (
    BackendAgentResponse,
    BackendAnswerMode,
    BackendStatus,
    SourceCitation,
)
from finance_agent_core.contracts.external_knowledge import ExternalKnowledgeOutcomeStatus
from finance_agent_core.contracts.queryplan import ProductFamily
from finance_agent_core.domain import FieldEvidence, ProductEvidence
from finance_agent_core.execution.external_knowledge_runtime import (
    ExternalKnowledgeRuntimeResult,
)
from finance_agent_core.retrieval import DocumentEvidence, DocumentSourceKind


def _backend_evidence_scalar(value: float | int | str) -> str | int:
    """Preserve non-integral DB numbers in the DTO's exact string boundary."""

    if isinstance(value, float):
        return str(Decimal(str(value)))
    return value


def _success_answer(result: ExternalKnowledgeRuntimeResult) -> str:
    assert result.execution is not None
    facts = result.execution.evidence.product_facts
    names = list(dict.fromkeys(item.product_name for item in facts))
    rendered = ", ".join(names)
    lines = [
        f"제공 데이터에 존재하는 상품과 승인된 공식 외부 근거의 교집합을 확인했습니다: {rendered}"
    ]
    for relation in result.execution.evidence.relations:
        for evidence in relation.evidence:
            lines.append(
                f"- 외부 근거: {evidence.publisher} · {evidence.title} "
                f"(기준일 {evidence.as_of.isoformat()}, {evidence.source_uri}, "
                f"{evidence.locator})"
            )
    for fact in facts:
        lines.append(
            f"- 제공 DB 근거: {fact.product_name} · {fact.canonical_field}="
            f"{fact.normalized_value} {fact.unit} "
            f"({fact.source_id} 원본 행 {fact.source_row}, "
            f"기준일 {fact.as_of.isoformat()})"
        )
    return "\n".join(dict.fromkeys(lines))


def external_knowledge_result_to_backend(
    result: ExternalKnowledgeRuntimeResult,
) -> BackendAgentResponse:
    """Expose only verified intersections; never expose discovery snippets."""

    plan = result.search_plan.composite_plan
    families = sorted(
        {family for requirement in plan.requirements for family in requirement.product_families},
        key=lambda item: item.value,
    )
    status = result.outcome.status
    if status is ExternalKnowledgeOutcomeStatus.CANNOT_VERIFY:
        return BackendAgentResponse(
            request_id=plan.question_id,
            status=BackendStatus.CANNOT_VERIFY,
            intent=plan.interaction_intent,
            product_families=families,
            answer=(
                "공식 외부 자료 검색 또는 근거 검증을 끝까지 완료하지 못해 제공 "
                "데이터 내 해당 상품의 존재 여부를 확인할 수 없습니다."
            ),
            query_plan=plan,
            candidate_count=None,
            products=[],
            comparisons=[],
            aggregates=[],
            documents=[],
            citations=[],
            as_of_dates=[],
            warnings=list(result.outcome.reason_codes),
            answer_mode=BackendAnswerMode.CONTROL,
            fallback_used=False,
            provider_model=None,
            clarification=None,
            error=None,
            source_manifest=None,
        )
    if status is ExternalKnowledgeOutcomeStatus.NOT_FOUND:
        documents = [
            DocumentEvidence(
                evidence_id=evidence.evidence_id,
                document_id=evidence.document_id,
                chunk_ordinal=0,
                title=evidence.title,
                text=evidence.excerpt,
                source_uri=evidence.source_uri,
                source_kind=DocumentSourceKind.EXTERNAL_APPROVED,
                as_of=evidence.as_of,
                document_sha256=evidence.content_sha256,
                metadata={
                    "publisher": evidence.publisher,
                    "source_registry_id": evidence.source_registry_id,
                    "locator": evidence.locator,
                    "evidence_scope": "external_entity_identity_only",
                },
                relevance_score=1.0,
            )
            for evidence in result.identity_evidence
        ]
        citations = [
            SourceCitation(
                citation_id=f"external-identity:{evidence.evidence_id}",
                kind="relation_field",
                label=evidence.title,
                source_id=evidence.source_registry_id,
                source_locator=f"{evidence.source_uri} · {evidence.locator}",
                as_of=evidence.as_of,
                evidence_refs=[evidence.evidence_id],
            )
            for evidence in result.identity_evidence
        ]
        answer_lines = ["찾으시는 상품은 제공 데이터 내에 존재하지 않습니다."]
        answer_lines.extend(
            f"- 외부 명칭 근거: {evidence.publisher} · {evidence.title} "
            f"(기준일 {evidence.as_of.isoformat()}, {evidence.source_uri})"
            for evidence in result.identity_evidence
        )
        return BackendAgentResponse(
            request_id=plan.question_id,
            status=BackendStatus.NOT_FOUND,
            intent=plan.interaction_intent,
            product_families=families,
            answer="\n".join(answer_lines),
            query_plan=plan,
            candidate_count=0,
            products=[],
            comparisons=[],
            aggregates=[],
            documents=documents,
            citations=citations,
            as_of_dates=sorted({item.as_of for item in documents}),
            warnings=[],
            answer_mode=BackendAnswerMode.DETERMINISTIC,
            fallback_used=False,
            provider_model=None,
            clarification=None,
            error=None,
            source_manifest=None,
        )
    if status is not ExternalKnowledgeOutcomeStatus.SUCCESS or result.execution is None:
        raise ValueError("external runtime adapter received an unsupported terminal state")

    facts = result.execution.evidence.product_facts
    # Product IDs are not globally unique across all four source tables.  Keep
    # the product family in the internal key so a cross-family result cannot
    # accidentally merge evidence from two distinct products that happen to
    # share the same identifier.
    fields_by_product: dict[tuple[ProductFamily, str], list[FieldEvidence]] = {}
    product_name_by_key: dict[tuple[ProductFamily, str], str] = {}
    ordered_product_keys: list[tuple[ProductFamily, str]] = []
    product_citations: list[SourceCitation] = []
    for fact in facts:
        product_key = (fact.product_family, fact.product_id)
        if product_key not in product_name_by_key:
            ordered_product_keys.append(product_key)
        field = FieldEvidence(
            canonical_field=fact.canonical_field,
            source_dataset=fact.source_dataset,
            source_id=fact.source_id,
            source_key={
                "product_family": fact.product_family.value,
                "product_id": fact.product_id,
            },
            source_row=fact.source_row,
            source_columns=list(fact.source_columns),
            raw_values={},
            normalized_value=_backend_evidence_scalar(fact.normalized_value),
            unit=fact.unit,
            as_of=fact.as_of,
            quality=fact.quality,
            quality_reason=(
                None
                if fact.quality is QualityStatus.VALID
                else "제공 데이터의 원천 품질 상태를 그대로 보존했습니다."
            ),
        )
        product_name_by_key[product_key] = fact.product_name
        fields_by_product.setdefault(product_key, []).append(field)
        product_citations.append(
            SourceCitation(
                citation_id=(
                    f"product:{fact.product_family.value}:{fact.product_id}:"
                    f"{fact.canonical_field}"
                ),
                kind="product_field",
                label=f"{fact.product_name} · {fact.canonical_field}",
                source_id=fact.source_id,
                source_locator=(
                    f"{fact.source_dataset} row {fact.source_row} · {'/'.join(fact.source_columns)}"
                ),
                as_of=fact.as_of,
                evidence_refs=[fact.evidence_id],
            )
        )

    documents: list[DocumentEvidence] = []
    relation_citations: list[SourceCitation] = []
    seen_evidence: set[str] = set()
    for relation in result.execution.evidence.relations:
        for evidence in relation.evidence:
            if evidence.evidence_id in seen_evidence:
                continue
            seen_evidence.add(evidence.evidence_id)
            documents.append(
                DocumentEvidence(
                    evidence_id=evidence.evidence_id,
                    document_id=evidence.document_id,
                    chunk_ordinal=0,
                    title=evidence.title,
                    text=evidence.excerpt,
                    source_uri=evidence.source_uri,
                    source_kind=DocumentSourceKind.EXTERNAL_APPROVED,
                    as_of=evidence.as_of,
                    document_sha256=evidence.content_sha256,
                    metadata={
                        "publisher": evidence.publisher,
                        "source_registry_id": evidence.source_registry_id,
                        "locator": evidence.locator,
                    },
                    relevance_score=1.0,
                )
            )
            relation_citations.append(
                SourceCitation(
                    citation_id=f"relation:{evidence.evidence_id}",
                    kind="relation_field",
                    label=evidence.title,
                    source_id=evidence.source_registry_id,
                    source_locator=f"{evidence.source_uri} · {evidence.locator}",
                    as_of=evidence.as_of,
                    evidence_refs=[evidence.evidence_id],
                )
            )
    products = [
        ProductEvidence(
            product_id=product_key[1],
            product_name=product_name_by_key[product_key],
            ticker=None,
            fields=fields_by_product[product_key],
        )
        for product_key in ordered_product_keys
        if product_key in result.outcome.verified_product_keys
        and product_key in fields_by_product
    ]
    return BackendAgentResponse(
        request_id=plan.question_id,
        status=BackendStatus.SUCCESS,
        intent=plan.interaction_intent,
        product_families=families,
        answer=_success_answer(result),
        query_plan=plan,
        candidate_count=len(products),
        products=products,
        comparisons=[],
        aggregates=[],
        documents=documents,
        citations=[*product_citations, *relation_citations],
        as_of_dates=sorted({*(item.as_of for item in facts), *(item.as_of for item in documents)}),
        warnings=[],
        answer_mode=BackendAnswerMode.DETERMINISTIC,
        fallback_used=False,
        provider_model=None,
        clarification=None,
        error=None,
        source_manifest=None,
    )


__all__ = ["external_knowledge_result_to_backend"]
