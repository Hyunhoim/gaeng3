from __future__ import annotations

from collections.abc import Iterable

from finance_agent_core.agent.external_knowledge_result_adapter import (
    external_knowledge_result_to_backend,
)
from finance_agent_core.agent.routed_service import IndependentHybridAgentResult
from finance_agent_core.answering.independent_sections import (
    IndependentAnswerSection,
    IndependentSectionCitation,
    IndependentSectionId,
    IndependentSectionRanking,
    IndependentSectionStatus,
    build_independent_section_bundle,
    namespace_section_reference,
)
from finance_agent_core.contracts.backend import (
    BackendAgentResponse,
    BackendAnswerMode,
    BackendStatus,
    SourceCitation,
    routed_result_to_backend,
)
from finance_agent_core.contracts.external_knowledge import CompositeKnowledgeQueryPlan
from finance_agent_core.contracts.queryplan import ProductFamily
from finance_agent_core.domain import ProductEvidence

_EXTERNAL_SECTION = IndependentSectionId.OFFICIAL_EXTERNAL_EVIDENCE
_DATABASE_SECTION = IndependentSectionId.PROVIDED_DATABASE_COMPOUND
_FAMILY_VALUES = frozenset(item.value for item in ProductFamily)


def _family_qualified_product_id(product: ProductEvidence) -> str:
    """Derive one unambiguous public identity from field-level provenance."""

    families = {
        family
        for field in product.fields
        for family in (
            field.source_key.get("product_family"),
            field.source_dataset,
        )
        if family in _FAMILY_VALUES
    }
    if len(families) != 1:
        raise ValueError("hybrid product evidence must bind exactly one product family")
    return f"{families.pop()}:{product.product_id}"


def _namespace_citations(
    section_id: IndependentSectionId,
    citations: Iterable[SourceCitation],
) -> tuple[tuple[SourceCitation, ...], tuple[IndependentSectionCitation, ...]]:
    public: list[SourceCitation] = []
    sectioned: list[IndependentSectionCitation] = []
    for citation in citations:
        citation_id = namespace_section_reference(section_id, citation.citation_id)
        evidence_refs = tuple(
            namespace_section_reference(section_id, evidence_ref)
            for evidence_ref in citation.evidence_refs
        )
        public.append(
            SourceCitation.model_validate(
                {
                    **citation.model_dump(mode="python"),
                    "citation_id": citation_id,
                    "evidence_refs": list(evidence_refs),
                }
            )
        )
        sectioned.append(
            IndependentSectionCitation(
                section_id=section_id,
                citation_id=citation_id,
                evidence_refs=evidence_refs,
            )
        )
    return tuple(public), tuple(sectioned)


def _complete_section_citations(
    section_id: IndependentSectionId,
    response: BackendAgentResponse,
) -> tuple[tuple[SourceCitation, ...], tuple[IndependentSectionCitation, ...]]:
    citations: tuple[SourceCitation, ...] = tuple(response.citations)
    if not citations and response.status is BackendStatus.SUCCESS:
        raise ValueError("complete hybrid section requires evidence-bound citations")
    return _namespace_citations(section_id, citations)


def _section_status(response: BackendAgentResponse) -> IndependentSectionStatus:
    if response.status is BackendStatus.SUCCESS:
        return IndependentSectionStatus.SUCCESS
    if response.status is BackendStatus.NOT_FOUND:
        return IndependentSectionStatus.NOT_FOUND
    raise ValueError("hybrid response cannot publish an incomplete child section")


def _external_ranking(
    plan: CompositeKnowledgeQueryPlan,
    product_ids: tuple[str, ...],
) -> IndependentSectionRanking | None:
    if not product_ids:
        return None
    if plan.product_selection is not None:
        selection = plan.product_selection
        return IndependentSectionRanking(
            field_id=selection.rank_field,
            direction=selection.direction.value,
            limit=selection.limit,
            ordered_product_ids=product_ids,
            value_source="provided_product_database",
        )
    if plan.evidence_selection is not None:
        selection = plan.evidence_selection
        return IndependentSectionRanking(
            field_id=selection.order_by,
            direction=selection.direction.value,
            limit=selection.limit,
            ordered_product_ids=product_ids,
            value_source="verified_official_evidence",
        )
    return None


def _database_ranking(
    result: IndependentHybridAgentResult,
    product_ids: tuple[str, ...],
) -> IndependentSectionRanking | None:
    if not product_ids:
        return None
    plan = result.database_compound_plan
    family_plans = plan.family_plans
    # A named comparison, parallel family lists, or aggregate result has a
    # stable display order but not one shared ranking.  Publish ranking
    # authority only for plan kinds whose output is explicitly metric-ordered.
    if not family_plans or plan.kind not in {
        "anchor_relative",
        "derived_ranking",
        "field_relative_ranking",
        "global_ranking",
        "two_stage",
    }:
        return None
    first = family_plans[0]
    if any(
        item.metric != first.metric
        or item.direction is not first.direction
        or item.limit != first.limit
        for item in family_plans[1:]
    ):
        # Multiple independently ordered family lists are represented by their
        # exact product order, not falsely collapsed into one shared ranking.
        return None
    field_id = (
        first.metric.source_fields[0]
        if first.metric.kind == "field" and len(first.metric.source_fields) == 1
        else f"{first.metric.kind}({','.join(first.metric.source_fields)})"
    )
    if len(field_id) > 127 or len(product_ids) > first.limit:
        return None
    return IndependentSectionRanking(
        field_id=field_id,
        direction=first.direction.value,
        limit=first.limit,
        ordered_product_ids=product_ids,
        value_source="provided_product_database",
    )


def _section_evidence_refs(
    citations: tuple[IndependentSectionCitation, ...],
) -> tuple[str, ...]:
    return tuple(
        dict.fromkeys(
            evidence_ref for citation in citations for evidence_ref in citation.evidence_refs
        )
    )


def independent_hybrid_result_to_backend(
    result: IndependentHybridAgentResult,
) -> BackendAgentResponse:
    """Publish two verified results atomically without inventing a joint rank."""

    external = external_knowledge_result_to_backend(result.external_result)
    database = routed_result_to_backend(result.database_result)
    if external.request_id != result.request_id or database.request_id != result.request_id:
        raise ValueError("hybrid child response request ID changed")

    external_public_citations, external_section_citations = _complete_section_citations(
        _EXTERNAL_SECTION,
        external,
    )
    database_public_citations, database_section_citations = _complete_section_citations(
        _DATABASE_SECTION,
        database,
    )
    external_product_ids = tuple(
        _family_qualified_product_id(product) for product in external.products
    )
    database_product_ids = tuple(
        _family_qualified_product_id(product) for product in database.products
    )
    external_plan = external.query_plan
    if not isinstance(external_plan, CompositeKnowledgeQueryPlan):
        raise ValueError("hybrid external child lost its CompositeKnowledgeQueryPlan")

    external_section = IndependentAnswerSection(
        section_id=_EXTERNAL_SECTION,
        status=_section_status(external),
        plan_kind=external_plan.plan_kind,
        plan_sha256=result.plan.external_section.subplan.plan_sha256,
        candidate_count=external.candidate_count,
        product_ids=external_product_ids,
        evidence_refs=_section_evidence_refs(external_section_citations),
        citation_ids=tuple(item.citation_id for item in external_section_citations),
        ranking=(
            _external_ranking(external_plan, external_product_ids)
            if external.status is BackendStatus.SUCCESS
            else None
        ),
        deterministic_answer=external.answer,
    )
    database_section = IndependentAnswerSection(
        section_id=_DATABASE_SECTION,
        status=_section_status(database),
        plan_kind=f"typed_compound_{result.database_compound_plan.kind}",
        plan_sha256=result.plan.database_section.subplan.plan_sha256,
        candidate_count=database.candidate_count,
        product_ids=database_product_ids,
        evidence_refs=_section_evidence_refs(database_section_citations),
        citation_ids=tuple(item.citation_id for item in database_section_citations),
        ranking=(
            _database_ranking(result, database_product_ids)
            if database.status is BackendStatus.SUCCESS
            else None
        ),
        deterministic_answer=database.answer,
    )
    bundle = build_independent_section_bundle(
        external_section=external_section,
        database_section=database_section,
        citations=(*external_section_citations, *database_section_citations),
    )
    status = (
        BackendStatus.NOT_FOUND
        if external.status is BackendStatus.NOT_FOUND and database.status is BackendStatus.NOT_FOUND
        else BackendStatus.SUCCESS
    )
    product_families = list(dict.fromkeys((*external.product_families, *database.product_families)))
    citations = [*external_public_citations, *database_public_citations]
    return BackendAgentResponse(
        request_id=result.request_id,
        status=status,
        intent=result.database_compound_plan.interaction_intent,
        product_families=product_families,
        answer=bundle.deterministic_answer,
        query_plan=result.plan,
        candidate_count=None,
        products=[*external.products, *database.products],
        comparisons=list(database.comparisons),
        aggregates=list(database.aggregates),
        documents=[*external.documents, *database.documents],
        citations=citations,
        as_of_dates=sorted(
            {
                *external.as_of_dates,
                *database.as_of_dates,
                *(citation.as_of for citation in citations),
            }
        ),
        warnings=list(dict.fromkeys((*external.warnings, *database.warnings))),
        answer_mode=BackendAnswerMode.DETERMINISTIC,
        fallback_used=False,
        provider_model=None,
        clarification=None,
        error=None,
        source_manifest=None,
        family_searches=[],
        source_manifests=[],
        independent_sections=bundle,
    )


__all__ = ["independent_hybrid_result_to_backend"]
