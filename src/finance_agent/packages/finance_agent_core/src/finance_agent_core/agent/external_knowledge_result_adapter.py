from __future__ import annotations

from decimal import Decimal

from finance_agent_core.agent.external_control_language import (
    build_external_cannot_verify_narrative,
)
from finance_agent_core.config import QualityStatus
from finance_agent_core.config.knowledge_coverage import KnowledgeCapability
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
from finance_agent_core.execution.external_knowledge_verifier import OfficialProductFact
from finance_agent_core.retrieval import DocumentEvidence, DocumentSourceKind
from finance_agent_core.retrieval.temporal_relations import (
    ExternalEvidenceReference,
    TemporalRelationRecord,
)


def _relation_product_key(
    relation: TemporalRelationRecord,
) -> tuple[ProductFamily, str] | None:
    if relation.product_family is None or relation.product_id is None:
        return None
    return relation.product_family, relation.product_id


def _evidence_content_key(evidence: ExternalEvidenceReference) -> tuple[object, ...]:
    """Identify one exact official span independently of its generated ID."""

    return (
        evidence.document_id,
        evidence.source_uri,
        evidence.content_sha256,
        evidence.locator,
        evidence.excerpt,
        evidence.published_at,
        evidence.as_of,
    )


def _relation_content_key(relation: TemporalRelationRecord) -> tuple[object, ...]:
    """Identify one verified semantic edge independently of relation IDs."""

    return (
        relation.capability,
        relation.predicate,
        relation.subject.kind,
        relation.subject.entity_id,
        relation.subject.canonical_name,
        relation.object.kind,
        relation.object.entity_id,
        relation.object.canonical_name,
        relation.product_family,
        relation.product_id,
        relation.valid_from,
        relation.valid_to,
        relation.observed_at,
    )


def _normalized_surface(value: str) -> str:
    return "".join(character.casefold() for character in value if character.isalnum())


def _representative_span_rank(
    relation: TemporalRelationRecord,
    evidence: ExternalEvidenceReference,
) -> tuple[int, int, str]:
    """Prefer the shortest exact span that states this verified relation.

    One official page is commonly split into overlapping retrieval windows.
    The verifier must retain and inspect all of them, but repeating every
    overlap in the public answer adds no evidence.  Entity coverage is ranked
    first so an identity-only cover never displaces the actual relation
    sentence; length is ranked second to keep HCX input and the GET payload
    bounded.  Dates remain part of the relation key, so evidence from different
    periods is never collapsed here.
    """

    normalized_excerpt = _normalized_surface(evidence.excerpt)
    # Product identity can be proved by a separate cover span and the signed
    # DB fact.  For the public representative, prefer the span that states the
    # non-product side of the relation (company, theme, risk or policy event),
    # then choose the shortest overlapping retrieval window.
    entity_coverage = sum(
        bool(surface and surface in normalized_excerpt)
        for entity in (relation.subject, relation.object)
        if entity.kind.value != "product"
        for surface in (_normalized_surface(entity.canonical_name),)
    )
    return (-entity_coverage, len(evidence.excerpt), evidence.evidence_id)


def _fund_requirement_public_relation_ids(
    result: ExternalKnowledgeRuntimeResult,
    *,
    selected_relation_ids: set[str],
) -> set[str]:
    """Choose one concise, current, already-verified fund passage per dimension.

    Fund policy pages can yield dozens of overlapping programme paragraphs for
    each DB sale class.  The full bundle remains available to the verifier, but
    one public passage per separately compiled requirement (structure,
    strategy, sales or trend) is sufficient evidence.  Selection uses only the
    requirement map and source dates; no product, issuer or evaluation phrase
    is hard-coded.
    """

    assert result.execution is not None
    bundle = result.execution.evidence
    relations_by_id = {relation.relation_id: relation for relation in bundle.relations}
    keep: set[str] = set()
    for requirement in result.search_plan.composite_plan.requirements:
        if requirement.capability is not KnowledgeCapability.FUND_STRUCTURE_STRATEGY_TREND:
            continue
        candidates: list[tuple[tuple[object, ...], str]] = []
        for relation_id in bundle.requirement_relations.get(requirement.requirement_id, ()):
            if relation_id not in selected_relation_ids:
                continue
            relation = relations_by_id.get(relation_id)
            if relation is None:
                continue
            for evidence in relation.evidence:
                evidence_date = evidence.published_at or evidence.as_of
                # Avoid a bare heading when a complete sentence/paragraph is
                # available, then prefer the newest evidence and shortest
                # overlapping window for that same verified dimension.
                rank = (
                    0 if len(evidence.excerpt) >= 80 else 1,
                    -evidence_date.toordinal(),
                    len(evidence.excerpt),
                    evidence.evidence_id,
                )
                candidates.append((rank, relation_id))
        if candidates:
            candidates.sort(key=lambda item: item[0])
            keep.add(candidates[0][1])
    return keep


def _selected_relations(
    result: ExternalKnowledgeRuntimeResult,
) -> tuple[TemporalRelationRecord, ...]:
    """Keep only verified relation paths that reach an exposed product.

    Execution may retain relations for every pre-ranking candidate.  Sending
    that whole bundle to HCX both inflates the payload and can describe a
    sibling product the final DB ranking did not select.  Start at the exact
    family-qualified output keys, then walk plan dependencies backwards.  A
    product-less corporate edge is admitted only when its child entity is used
    by a retained downstream holding edge; sharing the same parent is not
    enough, so unrelated subsidiaries stay out.
    """

    assert result.execution is not None
    bundle = result.execution.evidence
    selected_product_keys = set(result.outcome.verified_product_keys)
    relations_by_id = {relation.relation_id: relation for relation in bundle.relations}
    requirement_relations = bundle.requirement_relations
    referenced_relation_ids = {
        relation_id
        for relation_ids in requirement_relations.values()
        for relation_id in relation_ids
    }
    selected_relation_ids = {
        relation.relation_id
        for relation in bundle.relations
        if relation.relation_id in referenced_relation_ids
        and _relation_product_key(relation) in selected_product_keys
    }
    requirements_by_id = {
        requirement.requirement_id: requirement
        for requirement in result.search_plan.composite_plan.requirements
    }
    # Requirements are validated in dependency order. Reverse traversal means
    # every child path is fixed before deciding which parent edges explain it.
    for requirement in reversed(result.search_plan.composite_plan.requirements):
        child_relations = tuple(
            relations_by_id[relation_id]
            for relation_id in requirement_relations.get(requirement.requirement_id, ())
            if relation_id in selected_relation_ids and relation_id in relations_by_id
        )
        if not child_relations:
            continue
        child_entity_ids = {
            entity.entity_id
            for relation in child_relations
            for entity in (relation.subject, relation.object)
        }
        for dependency_id in requirement.depends_on:
            dependency = requirements_by_id.get(dependency_id)
            if dependency is None:
                continue
            for relation_id in requirement_relations.get(dependency_id, ()):
                relation = relations_by_id.get(relation_id)
                if relation is None:
                    continue
                product_key = _relation_product_key(relation)
                if product_key is not None:
                    if product_key in selected_product_keys:
                        selected_relation_ids.add(relation_id)
                    continue
                # Corporate relations are oriented parent -> subsidiary.  Match
                # only the subsidiary endpoint; matching the parent would pull
                # every sibling company into a one-product answer.
                if (
                    dependency.capability is KnowledgeCapability.CORPORATE_SUBSIDIARY
                    and relation.object.entity_id in child_entity_ids
                ):
                    selected_relation_ids.add(relation_id)
    selected_fund_relation_ids = _fund_requirement_public_relation_ids(
        result,
        selected_relation_ids=selected_relation_ids,
    )
    return tuple(
        relation
        for relation in bundle.relations
        if relation.relation_id in selected_relation_ids
        and (
            relation.capability is not KnowledgeCapability.FUND_STRUCTURE_STRATEGY_TREND
            or relation.relation_id in selected_fund_relation_ids
        )
    )


def _public_relation_spans(
    result: ExternalKnowledgeRuntimeResult,
) -> tuple[tuple[TemporalRelationRecord, ExternalEvidenceReference], ...]:
    """Expose one direct exact span for each verified relation and time.

    The execution verifier has already checked the complete immutable evidence
    bundle.  This public boundary removes overlapping retrieval windows for the
    same semantic edge, while keeping distinct relations and distinct dates.
    """

    evidence_id_keys: dict[str, tuple[object, ...]] = {}
    representatives: dict[
        tuple[object, ...], tuple[TemporalRelationRecord, ExternalEvidenceReference]
    ] = {}
    for relation in _selected_relations(result):
        relation_key = _relation_content_key(relation)
        for evidence in relation.evidence:
            evidence_key = _evidence_content_key(evidence)
            previous_key = evidence_id_keys.setdefault(evidence.evidence_id, evidence_key)
            if previous_key != evidence_key:
                raise ValueError("external evidence ID refers to different official spans")
            current = representatives.get(relation_key)
            if current is None or _representative_span_rank(
                relation, evidence
            ) < _representative_span_rank(*current):
                representatives[relation_key] = (relation, evidence)
    return tuple(representatives.values())


def _public_document_spans(
    relation_spans: tuple[tuple[TemporalRelationRecord, ExternalEvidenceReference], ...],
) -> tuple[ExternalEvidenceReference, ...]:
    """Return one public evidence object per exact span, not per document."""

    seen: set[tuple[object, ...]] = set()
    documents: list[ExternalEvidenceReference] = []
    for _, evidence in relation_spans:
        key = _evidence_content_key(evidence)
        if key in seen:
            continue
        seen.add(key)
        documents.append(evidence)
    return tuple(documents)


def _public_product_fact_ref(fact: OfficialProductFact) -> str:
    """Use a family-qualified public ID because source product IDs can collide."""

    return f"provided:{fact.product_family.value}:{fact.product_id}:{fact.canonical_field}"


def _backend_evidence_scalar(value: float | int | str) -> str | int:
    """Preserve non-integral DB numbers in the DTO's exact string boundary."""

    if isinstance(value, float):
        return str(Decimal(str(value)))
    return value


def _success_answer(
    result: ExternalKnowledgeRuntimeResult,
    relation_spans: tuple[tuple[TemporalRelationRecord, ExternalEvidenceReference], ...],
) -> str:
    assert result.execution is not None
    facts = result.execution.evidence.product_facts
    names = list(dict.fromkeys(item.product_name for item in facts))
    rendered = ", ".join(names)
    lines = [
        f"제공 데이터에 존재하는 상품과 승인된 공식 외부 근거의 교집합을 확인했습니다: {rendered}"
    ]
    grouped_spans: list[tuple[ExternalEvidenceReference, list[TemporalRelationRecord]]] = []
    span_indexes: dict[tuple[object, ...], int] = {}
    for relation, evidence in relation_spans:
        evidence_key = _evidence_content_key(evidence)
        index = span_indexes.get(evidence_key)
        if index is None:
            span_indexes[evidence_key] = len(grouped_spans)
            grouped_spans.append((evidence, [relation]))
        elif _relation_content_key(relation) not in {
            _relation_content_key(item) for item in grouped_spans[index][1]
        }:
            grouped_spans[index][1].append(relation)
    for evidence, relations in grouped_spans:
        publication = (
            f"발행일 {evidence.published_at.isoformat()}, "
            if evidence.published_at is not None
            else ""
        )
        relation_text = "; ".join(
            f"{relation.subject.canonical_name} --{relation.predicate.value}→ "
            f"{relation.object.canonical_name}"
            for relation in relations
        )
        lines.append(
            f"- 검증된 관계: {relation_text}\n"
            f"  외부 근거 원문: {evidence.excerpt}\n"
            f"  출처: {evidence.publisher} · {evidence.title} "
            f"({publication}기준일 {evidence.as_of.isoformat()}, "
            f"{evidence.source_uri}, "
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
        narrative = build_external_cannot_verify_narrative(
            plan,
            reason_codes=result.outcome.reason_codes,
            acquisitions=result.acquisitions,
        )
        return BackendAgentResponse(
            request_id=plan.question_id,
            status=BackendStatus.CANNOT_VERIFY,
            intent=plan.interaction_intent,
            product_families=families,
            answer=narrative.answer,
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
    ticker_by_key: dict[tuple[ProductFamily, str], str] = {}
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
        if fact.canonical_field == "ticker":
            ticker_by_key[product_key] = str(fact.normalized_value)
        fields_by_product.setdefault(product_key, []).append(field)
        product_citations.append(
            SourceCitation(
                citation_id=(
                    f"product:{fact.product_family.value}:{fact.product_id}:{fact.canonical_field}"
                ),
                kind="product_field",
                label=f"{fact.product_name} · {fact.canonical_field}",
                source_id=fact.source_id,
                source_locator=(
                    f"{fact.source_dataset} row {fact.source_row} · {'/'.join(fact.source_columns)}"
                ),
                as_of=fact.as_of,
                evidence_refs=[_public_product_fact_ref(fact)],
            )
        )

    documents: list[DocumentEvidence] = []
    relation_citations: list[SourceCitation] = []
    relation_spans = _public_relation_spans(result)
    for evidence in _public_document_spans(relation_spans):
        metadata = {
            "publisher": evidence.publisher,
            "source_registry_id": evidence.source_registry_id,
            "locator": evidence.locator,
        }
        # DocumentEvidence keeps metadata values as non-null strings.  An
        # official product page can be valid evidence without publishing a
        # separate publication date; omit the optional key instead of turning
        # a verified SUCCESS result into a Pydantic/HTTP 500.
        if evidence.published_at is not None:
            metadata["published_at"] = evidence.published_at.isoformat()
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
                metadata=metadata,
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
            ticker=ticker_by_key.get(product_key),
            fields=fields_by_product[product_key],
        )
        for product_key in ordered_product_keys
        if product_key in result.outcome.verified_product_keys and product_key in fields_by_product
    ]
    return BackendAgentResponse(
        request_id=plan.question_id,
        status=BackendStatus.SUCCESS,
        intent=plan.interaction_intent,
        product_families=families,
        answer=_success_answer(result, relation_spans),
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
