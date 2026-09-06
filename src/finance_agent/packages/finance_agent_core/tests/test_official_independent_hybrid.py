from __future__ import annotations

import json
from datetime import date
from types import SimpleNamespace

import pytest

from finance_agent_core.agent.compound_query import (
    CompoundFamilyPlan,
    CompoundMetric,
    TypedCompoundPlan,
)
from finance_agent_core.agent.official_adapter import official_response_from_backend
from finance_agent_core.answering.independent_sections import (
    INDEPENDENT_RANKING_NOTICE,
    IndependentAnswerSection,
    IndependentSectionCitation,
    IndependentSectionId,
    IndependentSectionRanking,
    IndependentSectionStatus,
    build_independent_section_bundle,
    namespace_section_reference,
)
from finance_agent_core.config import QualityStatus
from finance_agent_core.config.knowledge_coverage import KnowledgeCapability
from finance_agent_core.contracts.backend import BackendAnswerMode, BackendStatus, SourceCitation
from finance_agent_core.contracts.external_knowledge import (
    CompositeKnowledgeQueryPlan,
    KnowledgePredicate,
    KnowledgeRequirement,
)
from finance_agent_core.contracts.independent_hybrid import IndependentHybridPlan
from finance_agent_core.contracts.queryplan import ProductFamily, SortDirection
from finance_agent_core.contracts.routing import InteractionIntent
from finance_agent_core.domain import FieldEvidence, ProductEvidence
from finance_agent_core.retrieval import DocumentEvidence, DocumentSourceKind

EXTERNAL = IndependentSectionId.OFFICIAL_EXTERNAL_EVIDENCE
DATABASE = IndependentSectionId.PROVIDED_DATABASE_COMPOUND
AS_OF = date(2026, 8, 24)


def _source_range(question: str, text: str) -> tuple[int, int]:
    start = question.index(text)
    return start, start + len(text)


def _hybrid_plan() -> IndependentHybridPlan:
    external_text = "오로라소재 편입 ETF를 공식 문서로 확인해줘"
    database_text = "국내채권은 매수수익률 높은 순으로 1개 보여줘"
    policy_text = "두 결과는 별도 비교군이고 통합 순위가 아니야"
    question = f"{external_text}. {database_text}. {policy_text}."
    request_id = "official-hybrid-001"
    external_plan = CompositeKnowledgeQueryPlan(
        question_id=request_id,
        question=external_text,
        interaction_intent=InteractionIntent.SEARCH,
        evidence_cutoff=AS_OF,
        requirements=(
            KnowledgeRequirement(
                requirement_id="need_1",
                capability=KnowledgeCapability.PORTFOLIO_HOLDINGS,
                predicate=KnowledgePredicate.HOLDS_COMPANY,
                subject="오로라소재",
                subject_evidence_span="오로라소재",
                relation_evidence_span="편입",
                product_families=(ProductFamily.DOMESTIC_ETP,),
                source_registry_ids=("official_asset_manager",),
            ),
        ),
        join_order=("need_1", "official_product_db"),
    )
    database_plan = TypedCompoundPlan(
        question_id=request_id,
        question=database_text,
        interaction_intent=InteractionIntent.SEARCH,
        kind="bounded",
        family_plans=(
            CompoundFamilyPlan(
                product_family=ProductFamily.BOND,
                filters=(),
                metric=CompoundMetric("field", ("buy_yield",)),
                direction=SortDirection.DESC,
                limit=1,
            ),
        ),
    )
    return IndependentHybridPlan.from_subplans(
        question_id=request_id,
        question=question,
        external_source_ranges=(_source_range(question, external_text),),
        external_plan=external_plan,
        database_source_ranges=(_source_range(question, database_text),),
        database_plan=database_plan,
        combination_policy_ranges=(_source_range(question, policy_text),),
    )


def _field(
    *,
    field: str,
    value: str,
    family: ProductFamily,
    row: int,
) -> FieldEvidence:
    return FieldEvidence(
        canonical_field=field,
        source_dataset=family.value,
        source_id=f"source-{family.value}",
        source_key={"product_family": family.value},
        source_row=row,
        source_columns=[field],
        raw_values={field: value},
        normalized_value=value,
        unit="text" if field == "product_name" else "pct",
        as_of=AS_OF,
        quality=QualityStatus.VALID,
        quality_reason=None,
    )


def _response() -> SimpleNamespace:
    plan = _hybrid_plan()
    external_product = ProductEvidence(
        product_id="ETF-001",
        product_name="오로라 ETF",
        ticker="A000001",
        fields=[
            _field(
                field="product_name",
                value="오로라 ETF",
                family=ProductFamily.DOMESTIC_ETP,
                row=1,
            )
        ],
    )
    database_product = ProductEvidence(
        product_id="BOND-001",
        product_name="오로라 회사채",
        ticker=None,
        fields=[
            _field(
                field="buy_yield_pct",
                value="4.20",
                family=ProductFamily.BOND,
                row=2,
            )
        ],
    )
    document = DocumentEvidence(
        evidence_id="official-doc:0001",
        document_id="official-doc",
        chunk_ordinal=0,
        title="오로라 ETF 공식 구성종목",
        text="오로라 ETF는 오로라소재를 편입합니다.",
        source_uri="https://official.example/etf",
        source_kind=DocumentSourceKind.EXTERNAL_APPROVED,
        as_of=AS_OF,
        document_sha256="d" * 64,
        metadata={},
        relevance_score=1.0,
    )
    external_product_ref = namespace_section_reference(
        EXTERNAL,
        "provided:domestic_etp:ETF-001:product_name",
    )
    external_document_ref = namespace_section_reference(EXTERNAL, document.evidence_id)
    database_product_ref = namespace_section_reference(
        DATABASE,
        "BOND-001:buy_yield_pct",
    )
    external_product_citation_id = namespace_section_reference(
        EXTERNAL,
        "product:ETF-001:product_name",
    )
    external_document_citation_id = namespace_section_reference(
        EXTERNAL,
        "document:official-doc:0001",
    )
    database_product_citation_id = namespace_section_reference(
        DATABASE,
        "product:BOND-001:buy_yield_pct",
    )
    external_section = IndependentAnswerSection(
        section_id=EXTERNAL,
        status=IndependentSectionStatus.SUCCESS,
        plan_kind="composite_external_knowledge",
        plan_sha256=plan.external_section.subplan.plan_sha256,
        candidate_count=1,
        product_ids=("domestic_etp:ETF-001",),
        evidence_refs=(external_product_ref, external_document_ref),
        citation_ids=(external_product_citation_id, external_document_citation_id),
        deterministic_answer="공식 문서와 제공 DB의 교집합 결과는 오로라 ETF입니다.",
    )
    database_section = IndependentAnswerSection(
        section_id=DATABASE,
        status=IndependentSectionStatus.SUCCESS,
        plan_kind="typed_compound_bounded",
        plan_sha256=plan.database_section.subplan.plan_sha256,
        candidate_count=7,
        product_ids=("bond:BOND-001",),
        evidence_refs=(database_product_ref,),
        citation_ids=(database_product_citation_id,),
        ranking=IndependentSectionRanking(
            field_id="buy_yield_pct",
            direction="desc",
            limit=1,
            ordered_product_ids=("bond:BOND-001",),
            value_source="provided_product_database",
        ),
        deterministic_answer="채권 비교군의 매수수익률 1위는 오로라 회사채입니다.",
    )
    section_citations = (
        IndependentSectionCitation(
            section_id=EXTERNAL,
            citation_id=external_product_citation_id,
            evidence_refs=(external_product_ref,),
        ),
        IndependentSectionCitation(
            section_id=EXTERNAL,
            citation_id=external_document_citation_id,
            evidence_refs=(external_document_ref,),
        ),
        IndependentSectionCitation(
            section_id=DATABASE,
            citation_id=database_product_citation_id,
            evidence_refs=(database_product_ref,),
        ),
    )
    bundle = build_independent_section_bundle(
        external_section=external_section,
        database_section=database_section,
        citations=section_citations,
    )
    citations = [
        SourceCitation(
            citation_id=item.citation_id,
            kind=("document_chunk" if "document:" in item.citation_id else "product_field"),
            label="검증 근거",
            source_id="approved-source",
            source_locator="approved locator",
            as_of=AS_OF,
            evidence_refs=list(item.evidence_refs),
        )
        for item in section_citations
    ]
    return SimpleNamespace(
        request_id=plan.question_id,
        status=BackendStatus.SUCCESS,
        intent=InteractionIntent.SEARCH,
        product_families=[ProductFamily.DOMESTIC_ETP, ProductFamily.BOND],
        answer=bundle.deterministic_answer,
        query_plan=plan,
        independent_sections=bundle,
        candidate_count=None,
        products=[external_product, database_product],
        comparisons=[],
        aggregates=[],
        documents=[document],
        citations=citations,
        as_of_dates=[AS_OF],
        warnings=[],
        answer_mode=BackendAnswerMode.LLM_GROUNDED,
        fallback_used=False,
        provider_model="HCX-007",
        clarification=None,
        error=None,
    )


def test_official_hybrid_context_keeps_exact_sectioned_evidence_authority() -> None:
    backend = _response()

    official = official_response_from_backend(
        question_id=backend.request_id,
        question=backend.query_plan.question,
        response=backend,  # type: ignore[arg-type]
    )

    context = json.loads(official.retrieved_context)
    trace = json.loads(official.think_trace)
    evidence_refs = {
        field["evidence_ref"]
        for product in context["evidence"]["products"]
        for field in product["fields"]
    } | {item["evidence_ref"] for item in context["evidence"]["documents"]}
    cited_refs = {
        evidence_ref
        for citation in context["citations"]
        for evidence_ref in citation["evidence_refs"]
    }
    section_refs = {
        evidence_ref
        for section in context["independent_sections"]["sections"]
        for evidence_ref in section["evidence_refs"]
    }

    assert evidence_refs == cited_refs == section_refs
    assert any(item.startswith(f"{EXTERNAL.value}::provided:") for item in evidence_refs)
    assert any(item.startswith(f"{DATABASE.value}::BOND-001:") for item in evidence_refs)
    assert context["independent_sections"]["ranking_notice"] == INDEPENDENT_RANKING_NOTICE
    assert context["independent_hybrid_plan"]["plan_sha256"] == backend.query_plan.canonical_sha256
    assert "canonical_plan_json" not in official.retrieved_context
    assert trace["combination_policy"] == "independent_sections_no_cross_ranking"
    assert "no_cross_section_ranking_validation" in trace["execution_steps"]
    assert "grounded_answer_verifier" in trace["execution_steps"]
    assert trace["candidate_count"] is None


def test_official_hybrid_context_rejects_dangling_or_cross_section_citation() -> None:
    backend = _response()
    changed = backend.citations[0].model_copy(
        update={
            "evidence_refs": [
                namespace_section_reference(DATABASE, "ETF-001:product_name")
            ]
        }
    )
    backend.citations[0] = changed

    with pytest.raises(ValueError, match="hybrid section and public citations differ"):
        official_response_from_backend(
            question_id=backend.request_id,
            question=backend.query_plan.question,
            response=backend,  # type: ignore[arg-type]
        )


def test_official_hybrid_context_truncates_large_sections_without_dangling_refs() -> None:
    backend = _response()
    external_fields = [
        _field(
            field=f"external_metric_{index:02d}",
            value=str(index),
            family=ProductFamily.DOMESTIC_ETP,
            row=1,
        )
        for index in range(21)
    ]
    external_product = backend.products[0].model_copy(update={"fields": external_fields})
    database_products = [
        ProductEvidence(
            product_id=f"BOND-{index:03d}",
            product_name=f"회사채 {index}",
            ticker=None,
            fields=[
                _field(
                    field="buy_yield_pct",
                    value=f"{index}.0",
                    family=ProductFamily.BOND,
                    row=index + 1,
                )
            ],
        )
        for index in range(1, 21)
    ]
    documents = [
        backend.documents[0].model_copy(
            update={
                "evidence_id": f"official-doc:{index:04d}",
                "chunk_ordinal": index,
                "text": f"공식 문서 근거 {index}",
            }
        )
        for index in range(11)
    ]

    def public_citation(
        section_id: IndependentSectionId,
        citation_id: str,
        evidence_ref: str,
    ) -> SourceCitation:
        return SourceCitation(
            citation_id=namespace_section_reference(section_id, citation_id),
            kind=("document_chunk" if citation_id.startswith("document:") else "product_field"),
            label="검증 근거",
            source_id="approved-source",
            source_locator="approved locator",
            as_of=AS_OF,
            evidence_refs=[namespace_section_reference(section_id, evidence_ref)],
        )

    external_citations = [
        public_citation(
            EXTERNAL,
            f"product:ETF-001:{field.canonical_field}",
            f"provided:domestic_etp:ETF-001:{field.canonical_field}",
        )
        for field in external_fields
    ] + [
        public_citation(EXTERNAL, f"document:{item.evidence_id}", item.evidence_id)
        for item in documents
    ]
    database_citations = [
        public_citation(
            DATABASE,
            f"product:{product.product_id}:buy_yield_pct",
            f"{product.product_id}:buy_yield_pct",
        )
        for product in database_products
    ]
    section_citations = tuple(
        IndependentSectionCitation(
            section_id=(EXTERNAL if citation in external_citations else DATABASE),
            citation_id=citation.citation_id,
            evidence_refs=tuple(citation.evidence_refs),
        )
        for citation in (*external_citations, *database_citations)
    )
    external_section_citations = section_citations[: len(external_citations)]
    database_section_citations = section_citations[len(external_citations) :]
    bundle = build_independent_section_bundle(
        external_section=IndependentAnswerSection(
            section_id=EXTERNAL,
            status=IndependentSectionStatus.SUCCESS,
            plan_kind="composite_external_knowledge",
            plan_sha256=backend.query_plan.external_section.subplan.plan_sha256,
            candidate_count=1,
            product_ids=("domestic_etp:ETF-001",),
            evidence_refs=tuple(
                ref for item in external_section_citations for ref in item.evidence_refs
            ),
            citation_ids=tuple(item.citation_id for item in external_section_citations),
            deterministic_answer="외부 공식 문서와 제공 DB의 검증 결과입니다.",
        ),
        database_section=IndependentAnswerSection(
            section_id=DATABASE,
            status=IndependentSectionStatus.SUCCESS,
            plan_kind="typed_compound_bounded",
            plan_sha256=backend.query_plan.database_section.subplan.plan_sha256,
            candidate_count=20,
            product_ids=tuple(f"bond:{item.product_id}" for item in database_products),
            evidence_refs=tuple(
                ref for item in database_section_citations for ref in item.evidence_refs
            ),
            citation_ids=tuple(item.citation_id for item in database_section_citations),
            deterministic_answer="제공 DB의 독립 채권 결과입니다.",
        ),
        citations=section_citations,
    )
    backend.products = [external_product, *database_products]
    backend.documents = documents
    backend.citations = [*external_citations, *database_citations]
    backend.independent_sections = bundle
    backend.answer = bundle.deterministic_answer

    official = official_response_from_backend(
        question_id=backend.request_id,
        question=backend.query_plan.question,
        response=backend,  # type: ignore[arg-type]
    )
    context = json.loads(official.retrieved_context)
    evidence_refs = {
        field["evidence_ref"]
        for product in context["evidence"]["products"]
        for field in product["fields"]
    } | {item["evidence_ref"] for item in context["evidence"]["documents"]}
    cited_refs = {
        ref for citation in context["citations"] for ref in citation["evidence_refs"]
    }
    section_refs = {
        ref
        for section in context["independent_sections"]["sections"]
        for ref in section["evidence_refs"]
    }

    assert len(context["evidence"]["products"]) == 20
    assert len(context["evidence"]["products"][0]["fields"]) == 20
    assert len(context["evidence"]["documents"]) == 10
    assert context["truncation"]["products"] is True
    assert context["truncation"]["documents"] is True
    assert context["evidence"]["products"][0]["fields_truncated"] is True
    assert evidence_refs == cited_refs == section_refs
