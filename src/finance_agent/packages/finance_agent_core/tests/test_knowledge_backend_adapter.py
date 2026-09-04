from __future__ import annotations

import json
from datetime import date

import pytest

from finance_agent_core.agent.knowledge_backend_adapter import (
    knowledge_result_to_backend,
    knowledge_route_control_to_backend,
)
from finance_agent_core.agent.knowledge_router import (
    KnowledgeRouteDecision,
    KnowledgeRouteDisposition,
)
from finance_agent_core.agent.knowledge_service import KnowledgeAgentResult
from finance_agent_core.agent.official_adapter import official_response_from_backend
from finance_agent_core.answering.claims import (
    ClaimVerification,
    KnowledgeAnswerComposition,
)
from finance_agent_core.contracts.backend import BackendAnswerMode, BackendStatus
from finance_agent_core.contracts.knowledge import (
    KnowledgePlanAuthorityGate,
    KnowledgeQueryPlan,
    RelationKnowledgeOperation,
)
from finance_agent_core.contracts.queryplan import ProductFamily
from finance_agent_core.retrieval.relations import (
    RelationEntityKind,
    RelationEvidence,
    RelationSearchResponse,
    RelationType,
)


def _plan() -> KnowledgeQueryPlan:
    return KnowledgeQueryPlan(
        question_id="relation-001",
        question="미래에셋이 운용하는 국내 ETF를 보여줘",
        operation=RelationKnowledgeOperation(
            query="미래에셋",
            relation_types=(RelationType.MANAGED_BY,),
            product_families=(ProductFamily.DOMESTIC_ETP,),
            top_k=3,
        ),
    )


def _composition(answer: str = "근거가 확인된 관계 답변") -> KnowledgeAnswerComposition:
    return KnowledgeAnswerComposition(
        mode="deterministic",
        answer=answer,
        provider_name=None,
        model_name=None,
        generation_latency_ms=0,
        draft=None,
        verification=ClaimVerification(
            passed=True,
            checks={"deterministic_evidence_compiler": True},
            violations=(),
        ),
    )


def _evidence() -> RelationEvidence:
    return RelationEvidence(
        evidence_id="relation-evidence-001",
        relation_id="relation-001",
        relation_type=RelationType.MANAGED_BY,
        entity_id="company:miraeasset",
        entity_kind=RelationEntityKind.COMPANY,
        entity_label="미래에셋자산운용",
        product_family=ProductFamily.DOMESTIC_ETP,
        product_id="KR70000D0009",
        product_name="테스트 ETF",
        ticker="0000D0",
        canonical_field="manager",
        source_dataset="domestic_etp",
        source_id="PREF01N001",
        source_row=20,
        source_columns=("MGR_NM",),
        as_of=date(2026, 7, 11),
        source_database_sha256="a" * 64,
        approval_manifest_sha256="b" * 64,
        relevance_score=1.0,
    )


def _result(*, found: bool = True) -> KnowledgeAgentResult:
    plan = _plan()
    evidence = (_evidence(),) if found else ()
    response = RelationSearchResponse(
        status="found" if found else "not_found",
        query=plan.operation.query,
        relation_index_sha256="c" * 64,
        evidence=evidence,
    )
    return KnowledgeAgentResult(
        status=response.status,
        plan=plan,
        authority=KnowledgePlanAuthorityGate().authorize(plan).receipt,
        release_contract_sha256="d" * 64,
        candidate_count=len(evidence),
        relation_response=response,
        answer=_composition(),
    )


def test_relation_knowledge_result_uses_existing_backend_and_official_contracts() -> None:
    response = knowledge_result_to_backend(_result())

    assert response.status is BackendStatus.SUCCESS
    assert response.answer_mode is BackendAnswerMode.DETERMINISTIC
    assert response.query_plan == _plan()
    assert response.products[0].fields[0].normalized_value == "미래에셋자산운용"
    assert response.citations[0].kind == "relation_field"
    assert response.citations[0].evidence_refs == ["KR70000D0009:manager"]
    assert response.as_of_dates == [date(2026, 7, 11)]

    official = official_response_from_backend(
        question_id="relation-001",
        question=_plan().question,
        response=response,
    )
    context = json.loads(official.retrieved_context)
    trace = json.loads(official.think_trace)
    assert context["evidence"]["products"][0]["fields"][0]["field"] == "manager"
    assert trace["execution_steps"] == [
        "intent_router",
        "knowledge_plan_validation",
        "relation_retrieval",
        "claim_verifier",
        "response_contract_validation",
    ]
    assert trace["filters"][0]["relation_type"] == "managed_by"


def test_not_found_knowledge_result_exposes_no_evidence() -> None:
    response = knowledge_result_to_backend(_result(found=False))

    assert response.status is BackendStatus.NOT_FOUND
    assert response.candidate_count == 0
    assert response.products == []
    assert response.citations == []
    assert response.as_of_dates == []


@pytest.mark.parametrize(
    ("disposition", "status"),
    [
        (KnowledgeRouteDisposition.CLARIFY, BackendStatus.CLARIFICATION),
        (KnowledgeRouteDisposition.UNSUPPORTED, BackendStatus.UNSUPPORTED),
    ],
)
def test_knowledge_control_route_uses_safe_backend_contract(
    disposition: KnowledgeRouteDisposition,
    status: BackendStatus,
) -> None:
    decision = KnowledgeRouteDecision(
        disposition=disposition,
        reason_code=(
            "ambiguous_product_family"
            if disposition is KnowledgeRouteDisposition.CLARIFY
            else "relation_family_unavailable"
        ),
        reason="테스트용 공개 사유",
    )

    response = knowledge_route_control_to_backend(decision, request_id="relation-control")

    assert response.status is status
    assert response.answer_mode is BackendAnswerMode.CONTROL
    assert response.query_plan is None
    assert response.products == []
    assert response.citations == []
    if disposition is KnowledgeRouteDisposition.CLARIFY:
        assert response.clarification is not None
        assert response.clarification.required_fields == ["product_family"]
    else:
        assert response.clarification is None


def test_knowledge_control_adapter_rejects_non_control_route() -> None:
    with pytest.raises(ValueError, match="only knowledge control"):
        knowledge_route_control_to_backend(
            KnowledgeRouteDecision(
                disposition=KnowledgeRouteDisposition.NOT_APPLICABLE,
                reason_code="not_relation_question",
                reason="기존 상품 경로",
            ),
            request_id="ordinary",
        )
