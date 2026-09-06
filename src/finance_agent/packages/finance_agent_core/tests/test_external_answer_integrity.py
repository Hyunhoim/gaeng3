from __future__ import annotations

import json
from datetime import date
from types import SimpleNamespace

from finance_agent_core.agent.backend_adapter import _finalize_with_official_hclx
from finance_agent_core.agent.external_knowledge_result_adapter import (
    external_knowledge_result_to_backend,
)
from finance_agent_core.agent.intent_analysis import IntentAnalysisDraft
from finance_agent_core.agent.knowledge_planning import CompositeKnowledgePlanCompiler
from finance_agent_core.agent.official_adapter import official_response_from_backend
from finance_agent_core.answering.final_generation import (
    FinalAnswerContext,
    FinalAnswerDraft,
    allowed_final_answer_leads,
)
from finance_agent_core.config.knowledge_coverage import KnowledgeCapability
from finance_agent_core.contracts.backend import BackendStatus
from finance_agent_core.contracts.external_knowledge import (
    ExternalKnowledgeOutcome,
    ExternalKnowledgeOutcomeStatus,
    KnowledgeIntentRequirement,
)
from finance_agent_core.contracts.queryplan import ProductFamily
from finance_agent_core.contracts.routing import InteractionIntent
from finance_agent_core.execution.external_knowledge_runtime import (
    ExternalKnowledgeRuntimeResult,
)
from finance_agent_core.execution.external_knowledge_verifier import OfficialProductFact
from finance_agent_core.retrieval.temporal_relations import (
    ExternalEvidenceReference,
    TemporalEntity,
    TemporalEntityKind,
    TemporalRelationRecord,
)

_SHA_A = "a" * 64
_SHA_B = "b" * 64
_AS_OF = date(2026, 8, 22)
_SOURCE_URI = "https://example.com/official-etf-page"


class _CapturingFinalProvider:
    provider_name = "hyperclova"
    model_name = "HCX-TEST"

    def __init__(self) -> None:
        self.context: FinalAnswerContext | None = None

    def generate_final_answer(self, context: FinalAnswerContext) -> FinalAnswerDraft:
        self.context = context
        return FinalAnswerDraft(
            lead=allowed_final_answer_leads(context)[0],
            answer_body_sha256=context.answer_body_sha256,
        )


def _evidence(evidence_id: str, *, locator: str, excerpt: str) -> ExternalEvidenceReference:
    return ExternalEvidenceReference(
        evidence_id=evidence_id,
        source_registry_id="official-product-source",
        document_id="official-shared-document",
        title="공식 ETF 상품 설명서",
        publisher="공식 운용사",
        source_uri=_SOURCE_URI,
        published_at=_AS_OF,
        as_of=_AS_OF,
        content_sha256=_SHA_A,
        locator=locator,
        excerpt=excerpt,
    )


def _relation(
    relation_id: str,
    *,
    capability: KnowledgeCapability,
    predicate: str,
    subject: TemporalEntity,
    object_: TemporalEntity,
    evidence: ExternalEvidenceReference,
) -> TemporalRelationRecord:
    return TemporalRelationRecord(
        relation_id=relation_id,
        capability=capability,
        predicate=predicate,
        subject=subject,
        object=object_,
        product_family=ProductFamily.DOMESTIC_ETP,
        product_id="SAME",
        observed_at=_AS_OF,
        evidence=(evidence,),
    )


def _result() -> ExternalKnowledgeRuntimeResult:
    question = "오로라소재를 편입한 국내외 ETF와 위험요인을 알려줘."
    plan = CompositeKnowledgePlanCompiler().compile(
        question,
        "external-answer-integrity",
        IntentAnalysisDraft(
            intent=InteractionIntent.SEARCH,
            product_families=[
                ProductFamily.DOMESTIC_ETP,
                ProductFamily.OVERSEAS_ETP,
            ],
            evidence_spans=[],
            needs_clarification=False,
            knowledge_requirements=[
                KnowledgeIntentRequirement(
                    capability=KnowledgeCapability.PORTFOLIO_HOLDINGS,
                    subject_span="오로라소재",
                    relation_span="편입한",
                )
            ],
        ),
    )
    product = TemporalEntity(
        entity_id="product-domestic-same",
        kind=TemporalEntityKind.PRODUCT,
        canonical_name="국내 오로라 ETF",
    )
    holding_excerpt = "공식 구성종목 표에는 오로라소재가 편입 종목으로 기재되어 있습니다."
    risk_excerpt = "투자자는 시장가격 변동으로 원금 손실이 발생할 수 있습니다."
    holding = _relation(
        "relation-holding-same",
        capability=KnowledgeCapability.PORTFOLIO_HOLDINGS,
        predicate="holds_company",
        subject=TemporalEntity(
            entity_id="company-aurora-materials",
            kind=TemporalEntityKind.COMPANY,
            canonical_name="오로라소재",
        ),
        object_=product,
        evidence=_evidence(
            "evidence-holding-same",
            locator="구성종목 표 2행",
            excerpt=holding_excerpt,
        ),
    )
    risk = _relation(
        "relation-risk-same",
        capability=KnowledgeCapability.PRODUCT_RISK_FACTORS,
        predicate="has_risk_factor",
        subject=product,
        object_=TemporalEntity(
            entity_id="risk-market-price",
            kind=TemporalEntityKind.RISK_CLAIM,
            canonical_name="시장가격 변동 및 원금 손실 위험",
        ),
        evidence=_evidence(
            "evidence-risk-same",
            locator="투자위험 4쪽",
            excerpt=risk_excerpt,
        ),
    )
    # A repeated extractor result can have a new generated ID. It must not
    # duplicate the exact same relation/span in the answer or public context.
    duplicate_risk = risk.model_copy(
        update={
            "relation_id": "relation-risk-same-duplicate",
            "evidence": (
                _evidence(
                    "evidence-risk-same-duplicate",
                    locator="투자위험 4쪽",
                    excerpt=risk_excerpt,
                ),
            ),
        }
    )
    facts = (
        OfficialProductFact(
            evidence_id="private-domestic-fact-id",
            product_family=ProductFamily.DOMESTIC_ETP,
            product_id="SAME",
            product_name="국내 오로라 ETF",
            canonical_field="product_name",
            normalized_value="국내 오로라 ETF",
            as_of=_AS_OF,
            source_dataset="domestic_etp",
            source_id="PREF01N001",
            source_row=2,
            source_columns=("product_name",),
            unit="text",
            source_database_sha256=_SHA_A,
        ),
        OfficialProductFact(
            evidence_id="private-overseas-fact-id",
            product_family=ProductFamily.OVERSEAS_ETP,
            product_id="SAME",
            product_name="Overseas Aurora ETF",
            canonical_field="product_name",
            normalized_value="Overseas Aurora ETF",
            as_of=_AS_OF,
            source_dataset="overseas_etp",
            source_id="PREF02N001",
            source_row=2,
            source_columns=("product_name",),
            unit="text",
            source_database_sha256=_SHA_B,
        ),
    )
    return ExternalKnowledgeRuntimeResult.model_construct(
        outcome=ExternalKnowledgeOutcome(
            status=ExternalKnowledgeOutcomeStatus.SUCCESS,
            search_complete=True,
            verified_product_keys=(
                (ProductFamily.DOMESTIC_ETP, "SAME"),
                (ProductFamily.OVERSEAS_ETP, "SAME"),
            ),
            verified_product_ids=("SAME", "SAME"),
            reason_codes=("verified_external_db_intersection",),
        ),
        search_plan=SimpleNamespace(composite_plan=plan),
        candidate_universe=SimpleNamespace(),
        acquisitions=(),
        execution=SimpleNamespace(
            evidence=SimpleNamespace(
                product_facts=facts,
                relations=(holding, risk, duplicate_risk),
                requirement_relations={
                    "need_1": (
                        "relation-holding-same",
                        "relation-risk-same",
                        "relation-risk-same-duplicate",
                    ),
                },
            )
        ),
        identity_evidence=(),
    )


def test_external_success_preserves_relation_meaning_and_distinct_exact_spans() -> None:
    backend = external_knowledge_result_to_backend(_result())

    assert backend.status is BackendStatus.SUCCESS
    assert "국내 오로라 ETF" in backend.answer
    assert "시장가격 변동 및 원금 손실 위험" in backend.answer
    assert "공식 구성종목 표에는 오로라소재가 편입 종목으로 기재되어 있습니다." in (backend.answer)
    assert backend.answer.count("투자자는 시장가격 변동으로 원금 손실이 발생할 수 있습니다.") == 1
    # Two distinct excerpts in one document remain; only the exact duplicate
    # risk extraction is removed.
    assert [document.evidence_id for document in backend.documents] == [
        "evidence-holding-same",
        "evidence-risk-same",
    ]


def test_external_success_emits_one_direct_span_per_same_dated_relation() -> None:
    result = _result()
    holding = result.execution.evidence.relations[0]
    indirect = _evidence(
        "evidence-holding-indirect",
        locator="겹치는 넓은 문서 조각",
        excerpt=(
            "국내 오로라 ETF의 공식 자료입니다. 이 문서에는 여러 설명이 있으며 "
            "구성종목 표에는 오로라소재가 편입 종목으로 기재되어 있습니다."
        ),
    )
    duplicate_relation = holding.model_copy(
        update={
            "relation_id": "relation-holding-overlap",
            "evidence": (indirect,),
        }
    )
    updated_relations = (*result.execution.evidence.relations, duplicate_relation)
    updated_requirement_relations = {
        "need_1": (
            *result.execution.evidence.requirement_relations["need_1"],
            duplicate_relation.relation_id,
        )
    }
    updated = result.model_copy(
        update={
            "execution": SimpleNamespace(
                evidence=SimpleNamespace(
                    product_facts=result.execution.evidence.product_facts,
                    relations=updated_relations,
                    requirement_relations=updated_requirement_relations,
                )
            )
        }
    )

    backend = external_knowledge_result_to_backend(updated)

    assert "겹치는 넓은 문서 조각" not in {
        document.metadata["locator"] for document in backend.documents
    }
    assert [document.evidence_id for document in backend.documents] == [
        "evidence-holding-same",
        "evidence-risk-same",
    ]


def test_external_relation_meaning_and_exact_excerpt_reach_hclx_grounded_input() -> None:
    backend = external_knowledge_result_to_backend(_result())
    provider = _CapturingFinalProvider()
    service = SimpleNamespace(
        official_answer_provider=provider,
        require_official_hclx=True,
    )

    finalized = _finalize_with_official_hclx(
        service,
        "오로라소재를 편입한 국내외 ETF와 위험요인을 알려줘.",
        backend,
    )

    assert provider.context is not None
    assert "시장가격 변동 및 원금 손실 위험" in provider.context.deterministic_answer
    assert (
        "투자자는 시장가격 변동으로 원금 손실이 발생할 수 있습니다."
        in provider.context.deterministic_answer
    )
    assert provider.context.deterministic_answer in finalized.answer


def test_external_official_context_refs_are_exact_and_family_qualified() -> None:
    backend = external_knowledge_result_to_backend(_result())
    official = official_response_from_backend(
        question_id="external-answer-integrity",
        question="오로라소재를 편입한 국내외 ETF와 위험요인을 알려줘.",
        response=backend,
    )

    context = json.loads(official.retrieved_context)
    product_refs = {
        field["evidence_ref"]
        for product in context["evidence"]["products"]
        for field in product["fields"]
    }
    document_refs = {document["evidence_ref"] for document in context["evidence"]["documents"]}
    cited_refs = {
        evidence_ref
        for citation in context["citations"]
        for evidence_ref in citation["evidence_refs"]
    }

    assert product_refs == {
        "provided:domestic_etp:SAME:product_name",
        "provided:overseas_etp:SAME:product_name",
    }
    assert document_refs == {"evidence-holding-same", "evidence-risk-same"}
    assert cited_refs == product_refs | document_refs
    assert [item["text"] for item in context["evidence"]["documents"]] == [
        "공식 구성종목 표에는 오로라소재가 편입 종목으로 기재되어 있습니다.",
        "투자자는 시장가격 변동으로 원금 손실이 발생할 수 있습니다.",
    ]


def test_external_answer_keeps_only_dependency_path_to_selected_product() -> None:
    question = "오로라그룹의 자회사를 편입한 국내 ETF의 위험요인을 알려줘."
    plan = CompositeKnowledgePlanCompiler().compile(
        question,
        "external-selected-path-only",
        IntentAnalysisDraft(
            intent=InteractionIntent.SEARCH,
            product_families=[ProductFamily.DOMESTIC_ETP],
            evidence_spans=[],
            needs_clarification=False,
            knowledge_requirements=[
                KnowledgeIntentRequirement(
                    capability=KnowledgeCapability.CORPORATE_SUBSIDIARY,
                    subject_span="오로라그룹",
                    relation_span="자회사",
                ),
                KnowledgeIntentRequirement(
                    capability=KnowledgeCapability.PORTFOLIO_HOLDINGS,
                    subject_span="오로라그룹",
                    relation_span="편입한",
                ),
                KnowledgeIntentRequirement(
                    capability=KnowledgeCapability.PRODUCT_RISK_FACTORS,
                    subject_span="ETF",
                    relation_span="위험요인",
                ),
            ],
        ),
    )
    requirement_ids = {
        requirement.capability: requirement.requirement_id for requirement in plan.requirements
    }
    parent = TemporalEntity(
        entity_id="company-aurora-group",
        kind=TemporalEntityKind.COMPANY,
        canonical_name="오로라그룹",
    )
    selected_child = TemporalEntity(
        entity_id="company-selected-child",
        kind=TemporalEntityKind.COMPANY,
        canonical_name="선택자회사",
    )
    unrelated_child = TemporalEntity(
        entity_id="company-unrelated-child",
        kind=TemporalEntityKind.COMPANY,
        canonical_name="무관자회사",
    )
    selected_product = TemporalEntity(
        entity_id="product-selected",
        kind=TemporalEntityKind.PRODUCT,
        canonical_name="선택 ETF",
    )
    unrelated_product = TemporalEntity(
        entity_id="product-unrelated",
        kind=TemporalEntityKind.PRODUCT,
        canonical_name="무관 ETF",
    )

    def relation(
        relation_id: str,
        capability: KnowledgeCapability,
        predicate: str,
        subject: TemporalEntity,
        object_: TemporalEntity,
        *,
        product_id: str | None,
    ) -> TemporalRelationRecord:
        return TemporalRelationRecord(
            relation_id=relation_id,
            capability=capability,
            predicate=predicate,
            subject=subject,
            object=object_,
            product_family=(ProductFamily.DOMESTIC_ETP if product_id is not None else None),
            product_id=product_id,
            observed_at=_AS_OF,
            evidence=(
                _evidence(
                    f"evidence-{relation_id}",
                    locator=f"{relation_id} 위치",
                    excerpt=(
                        f"{subject.canonical_name}에서 {object_.canonical_name}으로 "
                        "이어지는 공식 근거"
                    ),
                ),
            ),
        )

    selected_relations = (
        relation(
            "relation-selected-corporate",
            KnowledgeCapability.CORPORATE_SUBSIDIARY,
            "subsidiary_of",
            parent,
            selected_child,
            product_id=None,
        ),
        relation(
            "relation-selected-holding",
            KnowledgeCapability.PORTFOLIO_HOLDINGS,
            "holds_company",
            selected_child,
            selected_product,
            product_id="SELECTED",
        ),
        relation(
            "relation-selected-risk",
            KnowledgeCapability.PRODUCT_RISK_FACTORS,
            "has_risk_factor",
            selected_product,
            TemporalEntity(
                entity_id="risk-selected",
                kind=TemporalEntityKind.RISK_CLAIM,
                canonical_name="선택 상품 위험",
            ),
            product_id="SELECTED",
        ),
    )
    unrelated_relations = (
        relation(
            "relation-unrelated-corporate",
            KnowledgeCapability.CORPORATE_SUBSIDIARY,
            "subsidiary_of",
            parent,
            unrelated_child,
            product_id=None,
        ),
        relation(
            "relation-unrelated-holding",
            KnowledgeCapability.PORTFOLIO_HOLDINGS,
            "holds_company",
            unrelated_child,
            unrelated_product,
            product_id="UNRELATED",
        ),
        relation(
            "relation-unrelated-risk",
            KnowledgeCapability.PRODUCT_RISK_FACTORS,
            "has_risk_factor",
            unrelated_product,
            TemporalEntity(
                entity_id="risk-unrelated",
                kind=TemporalEntityKind.RISK_CLAIM,
                canonical_name="무관 상품 위험",
            ),
            product_id="UNRELATED",
        ),
    )
    relations = tuple(
        sorted((*selected_relations, *unrelated_relations), key=lambda item: item.relation_id)
    )
    facts = (
        OfficialProductFact(
            evidence_id="provided-selected-product-name",
            product_family=ProductFamily.DOMESTIC_ETP,
            product_id="SELECTED",
            product_name="선택 ETF",
            canonical_field="product_name",
            normalized_value="선택 ETF",
            as_of=_AS_OF,
            source_dataset="domestic_etp",
            source_id="PREF01N001",
            source_row=2,
            source_columns=("product_name",),
            unit="text",
            source_database_sha256=_SHA_A,
        ),
    )
    relations_by_capability = {
        capability: tuple(item.relation_id for item in relations if item.capability is capability)
        for capability in requirement_ids
    }
    result = ExternalKnowledgeRuntimeResult.model_construct(
        outcome=ExternalKnowledgeOutcome(
            status=ExternalKnowledgeOutcomeStatus.SUCCESS,
            search_complete=True,
            verified_product_keys=((ProductFamily.DOMESTIC_ETP, "SELECTED"),),
            verified_product_ids=("SELECTED",),
            reason_codes=("verified_external_db_intersection",),
        ),
        search_plan=SimpleNamespace(composite_plan=plan),
        candidate_universe=SimpleNamespace(),
        acquisitions=(),
        execution=SimpleNamespace(
            evidence=SimpleNamespace(
                product_facts=facts,
                relations=relations,
                requirement_relations={
                    requirement_id: relations_by_capability[capability]
                    for capability, requirement_id in requirement_ids.items()
                },
            )
        ),
        identity_evidence=(),
    )

    backend = external_knowledge_result_to_backend(result)

    assert "선택자회사" in backend.answer
    assert "선택 상품 위험" in backend.answer
    assert "무관자회사" not in backend.answer
    assert "무관 ETF" not in backend.answer
    assert "무관 상품 위험" not in backend.answer
    assert len(backend.documents) == 3
    assert len([item for item in backend.citations if item.kind == "relation_field"]) == 3
