from __future__ import annotations

from datetime import date
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from finance_agent_core.agent import execute_answer_request
from finance_agent_core.agent.backend_adapter import AnswerAdapterResult
from finance_agent_core.agent.compound_query import (
    CompoundFamilyPlan,
    CompoundMetric,
    TypedCompoundPlan,
)
from finance_agent_core.agent.hybrid_query import HybridExecutionAborted
from finance_agent_core.agent.independent_hybrid_result_adapter import (
    independent_hybrid_result_to_backend,
)
from finance_agent_core.agent.providers.hyperclova import HyperClovaXTimeoutError
from finance_agent_core.agent.routed_service import IndependentHybridAgentResult
from finance_agent_core.answering.final_generation import FinalAnswerDraft
from finance_agent_core.answering.independent_sections import IndependentSectionStatus
from finance_agent_core.config import QualityStatus
from finance_agent_core.config.knowledge_coverage import KnowledgeCapability
from finance_agent_core.contracts.backend import (
    BackendAgentRequest,
    BackendAgentResponse,
    BackendAnswerMode,
    BackendStatus,
    SourceCitation,
)
from finance_agent_core.contracts.external_knowledge import (
    CompositeKnowledgeQueryPlan,
    KnowledgePredicate,
    KnowledgeRequirement,
)
from finance_agent_core.contracts.independent_hybrid import IndependentHybridPlan
from finance_agent_core.contracts.queryplan import ProductFamily, SortDirection
from finance_agent_core.contracts.routing import InteractionIntent
from finance_agent_core.domain import DatabaseManifest, FieldEvidence, ProductEvidence

REQUEST_ID = "hybrid-backend-001"
QUESTION = "외부근거 별도비교 국내채권"
AS_OF = date(2026, 8, 24)


def _plans() -> tuple[
    IndependentHybridPlan,
    CompositeKnowledgeQueryPlan,
    TypedCompoundPlan,
]:
    external_question = "외부근거"
    database_question = "국내채권"
    external = CompositeKnowledgeQueryPlan(
        question_id=REQUEST_ID,
        question=external_question,
        interaction_intent=InteractionIntent.SEARCH,
        evidence_cutoff=AS_OF,
        requirements=(
            KnowledgeRequirement(
                requirement_id="need_1",
                capability=KnowledgeCapability.PORTFOLIO_HOLDINGS,
                predicate=KnowledgePredicate.HOLDS_COMPANY,
                subject="외부",
                subject_evidence_span="외부",
                relation_evidence_span="근거",
                product_families=(ProductFamily.DOMESTIC_ETP,),
                source_registry_ids=("official-source",),
            ),
        ),
        join_order=("need_1", "official_product_db"),
        execution_authorized=True,
    )
    database = TypedCompoundPlan(
        question_id=REQUEST_ID,
        question=database_question,
        interaction_intent=InteractionIntent.SEARCH,
        kind="two_stage",
        family_plans=(
            CompoundFamilyPlan(
                product_family=ProductFamily.BOND,
                filters=(),
                metric=CompoundMetric(kind="field", source_fields=("issue_amount",)),
                direction=SortDirection.DESC,
                limit=2,
                shortlist_metric=CompoundMetric(
                    kind="field",
                    source_fields=("duration_years",),
                ),
                shortlist_limit=5,
            ),
        ),
    )
    policy_start = QUESTION.index(" 별도비교")
    database_start = QUESTION.index("국내채권")
    parent = IndependentHybridPlan.from_subplans(
        question_id=REQUEST_ID,
        question=QUESTION,
        external_source_ranges=((0, policy_start),),
        external_plan=external,
        database_source_ranges=((database_start, len(QUESTION)),),
        database_plan=database,
        combination_policy_ranges=((policy_start, database_start),),
    )
    return parent, external, database


def _manifest() -> DatabaseManifest:
    return DatabaseManifest(
        dataset="bond",
        registry_schema_version="test-v1",
        source_file_name="bond.xlsx",
        source_file_sha256="d" * 64,
        source_file_size_bytes=100,
        source_snapshot_date=AS_OF,
        total_rows=2,
        searchable_rows=2,
        quarantined_rows=0,
    )


def _product(family: ProductFamily, product_id: str) -> ProductEvidence:
    field_name = "aum" if family is ProductFamily.DOMESTIC_ETP else "issue_amount"
    return ProductEvidence(
        product_id=product_id,
        product_name=f"{family.value}-{product_id}",
        ticker=product_id,
        fields=[
            FieldEvidence(
                canonical_field=field_name,
                source_dataset=family.value,
                source_id=f"source-{family.value}",
                source_key={"product_id": product_id},
                source_row=2,
                source_columns=[field_name],
                raw_values={field_name: "100"},
                normalized_value="100",
                unit="KRW",
                as_of=AS_OF,
                quality=QualityStatus.VALID,
                quality_reason=None,
            )
        ],
    )


def _citation(product: ProductEvidence) -> SourceCitation:
    field = product.fields[0]
    evidence_ref = (
        f"provided:domestic_etp:{product.product_id}:{field.canonical_field}"
        if field.source_dataset == ProductFamily.DOMESTIC_ETP.value
        else f"{product.product_id}:{field.canonical_field}"
    )
    return SourceCitation(
        citation_id=f"product:{evidence_ref}",
        kind="product_field",
        label=f"{product.product_name} · {field.canonical_field}",
        source_id=field.source_id,
        source_locator=f"{field.source_dataset} row {field.source_row}",
        as_of=field.as_of,
        evidence_refs=[evidence_ref],
    )


def _child_responses(
    *,
    external_status: BackendStatus = BackendStatus.SUCCESS,
    database_status: BackendStatus = BackendStatus.SUCCESS,
) -> tuple[BackendAgentResponse, BackendAgentResponse]:
    _, external_plan, _ = _plans()
    external_product = _product(ProductFamily.DOMESTIC_ETP, "ETF-001")
    database_product = _product(ProductFamily.BOND, "BOND-001")
    external_success = external_status is BackendStatus.SUCCESS
    database_success = database_status is BackendStatus.SUCCESS
    external = BackendAgentResponse(
        request_id=REQUEST_ID,
        status=external_status,
        intent=InteractionIntent.SEARCH,
        product_families=[ProductFamily.DOMESTIC_ETP],
        answer=("검증된 외부 교집합 결과" if external_success else "외부 교집합 0건"),
        query_plan=external_plan,
        candidate_count=1 if external_success else 0,
        products=[external_product] if external_success else [],
        comparisons=[],
        aggregates=[],
        documents=[],
        citations=[_citation(external_product)] if external_success else [],
        as_of_dates=[AS_OF] if external_success else [],
        warnings=[],
        answer_mode=BackendAnswerMode.DETERMINISTIC,
        fallback_used=False,
        provider_model=None,
        clarification=None,
        error=None,
        source_manifest=None,
    )
    database = BackendAgentResponse(
        request_id=REQUEST_ID,
        status=database_status,
        intent=InteractionIntent.SEARCH,
        product_families=[ProductFamily.BOND],
        answer=("검증된 독립 DB 계산 결과" if database_success else "DB 조건 결과 0건"),
        query_plan=None,
        candidate_count=2 if database_success else 0,
        products=[database_product] if database_success else [],
        comparisons=[],
        aggregates=[],
        documents=[],
        citations=[_citation(database_product)] if database_success else [],
        as_of_dates=[AS_OF] if database_success else [],
        warnings=[],
        answer_mode=BackendAnswerMode.DETERMINISTIC,
        fallback_used=False,
        provider_model=None,
        clarification=None,
        error=None,
        source_manifest=_manifest(),
    )
    return external, database


def _result() -> SimpleNamespace:
    parent, _, database_plan = _plans()
    return SimpleNamespace(
        request_id=REQUEST_ID,
        plan=parent,
        external_result=object(),
        database_result=object(),
        database_compound_plan=database_plan,
    )


def test_hybrid_adapter_publishes_atomic_namespaced_sections(monkeypatch) -> None:
    external, database = _child_responses()
    monkeypatch.setattr(
        "finance_agent_core.agent.independent_hybrid_result_adapter."
        "external_knowledge_result_to_backend",
        lambda _result: external,
    )
    monkeypatch.setattr(
        "finance_agent_core.agent.independent_hybrid_result_adapter.routed_result_to_backend",
        lambda _result: database,
    )

    response = independent_hybrid_result_to_backend(_result())

    assert response.status is BackendStatus.SUCCESS
    assert response.candidate_count is None
    assert response.independent_sections is not None
    assert "통합 순위가 아닙니다" in response.answer
    assert [item.product_ids for item in response.independent_sections.sections] == [
        ("domestic_etp:ETF-001",),
        ("bond:BOND-001",),
    ]
    assert all("::" in item.citation_id for item in response.citations)
    assert all("::" in ref for item in response.citations for ref in item.evidence_refs)
    assert response.family_searches == []
    assert response.source_manifests == []


def test_hybrid_adapter_keeps_two_verified_empty_sections_complete(monkeypatch) -> None:
    external, database = _child_responses(
        external_status=BackendStatus.NOT_FOUND,
        database_status=BackendStatus.NOT_FOUND,
    )
    monkeypatch.setattr(
        "finance_agent_core.agent.independent_hybrid_result_adapter."
        "external_knowledge_result_to_backend",
        lambda _result: external,
    )
    monkeypatch.setattr(
        "finance_agent_core.agent.independent_hybrid_result_adapter.routed_result_to_backend",
        lambda _result: database,
    )

    response = independent_hybrid_result_to_backend(_result())

    assert response.status is BackendStatus.NOT_FOUND
    assert response.candidate_count is None
    assert response.products == []
    assert response.documents == []
    assert response.independent_sections is not None
    assert [item.status for item in response.independent_sections.sections] == [
        IndependentSectionStatus.NOT_FOUND,
        IndependentSectionStatus.NOT_FOUND,
    ]
    assert response.citations == []


def test_hybrid_contract_rejects_cross_section_count_and_plan_hash_change(
    monkeypatch,
) -> None:
    external, database = _child_responses()
    monkeypatch.setattr(
        "finance_agent_core.agent.independent_hybrid_result_adapter."
        "external_knowledge_result_to_backend",
        lambda _result: external,
    )
    monkeypatch.setattr(
        "finance_agent_core.agent.independent_hybrid_result_adapter.routed_result_to_backend",
        lambda _result: database,
    )
    response = independent_hybrid_result_to_backend(_result())
    payload = response.model_dump(mode="python")
    payload["candidate_count"] = 3
    with pytest.raises(ValidationError, match="cross-section count"):
        BackendAgentResponse.model_validate(payload)

    payload = response.model_dump(mode="python")
    payload["independent_sections"]["sections"][0]["plan_sha256"] = "f" * 64
    with pytest.raises(ValidationError, match="section plan hashes changed"):
        BackendAgentResponse.model_validate(payload)


def test_hybrid_adapter_rejects_incomplete_child_without_partial_output(monkeypatch) -> None:
    external, database = _child_responses()
    incomplete = external.model_copy(
        update={
            "status": BackendStatus.CANNOT_VERIFY,
            "candidate_count": None,
            "products": [],
            "citations": [],
            "as_of_dates": [],
            "answer_mode": BackendAnswerMode.CONTROL,
        }
    )
    monkeypatch.setattr(
        "finance_agent_core.agent.independent_hybrid_result_adapter."
        "external_knowledge_result_to_backend",
        lambda _result: incomplete,
    )
    monkeypatch.setattr(
        "finance_agent_core.agent.independent_hybrid_result_adapter.routed_result_to_backend",
        lambda _result: database,
    )

    with pytest.raises(ValueError, match="incomplete child section"):
        independent_hybrid_result_to_backend(_result())


def test_answer_adapter_unwraps_hybrid_timeout_cause() -> None:
    class HybridTimeoutService:
        def _answer_public_atomically(self, question: str, request_id: str):
            raise HybridExecutionAborted(
                "hybrid_external_execution_failed",
                cause=HyperClovaXTimeoutError("private provider detail"),
            )

        def assert_public_response_boundary_current(self) -> None:
            return None

    result: AnswerAdapterResult = execute_answer_request(
        HybridTimeoutService(),
        BackendAgentRequest(request_id=REQUEST_ID, question=QUESTION),
    )

    assert result.http_status_code == 504
    assert result.response.status is BackendStatus.ERROR
    assert "private provider detail" not in result.model_dump_json()


def test_answer_adapter_routes_hybrid_and_hclx_cannot_change_section_body(
    monkeypatch,
) -> None:
    external, database = _child_responses()
    monkeypatch.setattr(
        "finance_agent_core.agent.independent_hybrid_result_adapter."
        "external_knowledge_result_to_backend",
        lambda _result: external,
    )
    monkeypatch.setattr(
        "finance_agent_core.agent.independent_hybrid_result_adapter.routed_result_to_backend",
        lambda _result: database,
    )
    combined = independent_hybrid_result_to_backend(_result())
    parent, _, database_plan = _plans()
    routed = IndependentHybridAgentResult.model_construct(
        request_id=REQUEST_ID,
        plan=parent,
        external_result=object(),
        database_result=object(),
        database_compound_plan=database_plan,
    )

    class Provider:
        provider_name = "naver-clova-studio"
        model_name = "HCX-007"

        def generate_final_answer(self, context):
            return FinalAnswerDraft(
                lead="검증된 상품 데이터와 근거를 바탕으로 조회 결과를 안내합니다.",
                answer_body_sha256=context.answer_body_sha256,
            )

    class HybridService:
        official_answer_provider = Provider()
        require_official_hclx = True

        def _answer_public_atomically(self, question: str, request_id: str):
            return routed

        def assert_public_response_boundary_current(self) -> None:
            return None

    monkeypatch.setattr(
        "finance_agent_core.agent.independent_hybrid_result_adapter."
        "independent_hybrid_result_to_backend",
        lambda _result: combined,
    )

    result = execute_answer_request(
        HybridService(),
        BackendAgentRequest(request_id=REQUEST_ID, question=QUESTION),
    )

    assert result.http_status_code == 200
    assert result.response.answer_mode is BackendAnswerMode.LLM_GROUNDED
    assert result.response.independent_sections is not None
    assert result.response.answer.endswith(
        result.response.independent_sections.deterministic_answer
    )
