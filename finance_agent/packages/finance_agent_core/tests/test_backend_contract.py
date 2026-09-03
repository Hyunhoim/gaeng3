from __future__ import annotations

import json
from copy import deepcopy
from datetime import date
from importlib.resources import files
from pathlib import Path

import pytest
from pydantic import ValidationError

from finance_agent_core.agent import RoutedFinanceAgent
from finance_agent_core.config.knowledge_coverage import KnowledgeCapability
from finance_agent_core.contracts.backend import (
    BackendAgentRequest,
    BackendAgentResponse,
    BackendAnswerMode,
    BackendStatus,
    SourceCitation,
    backend_contract_schemas,
    routed_result_to_backend,
)
from finance_agent_core.contracts.external_knowledge import (
    CompositeKnowledgeQueryPlan,
    KnowledgePredicate,
    KnowledgeRequirement,
)
from finance_agent_core.contracts.queryplan import ProductFamily
from finance_agent_core.contracts.routing import InteractionIntent
from finance_agent_core.domain import DatabaseManifest, NormalizedOverseasEtpRecord
from finance_agent_core.observability import (
    AuditStage,
    BoundedAsyncAuditSink,
    InMemoryAuditSink,
    RequestAuditRecorder,
    bind_request_audit,
)
from finance_agent_core.retrieval import DocumentEvidence, DocumentSourceKind


def _example(name: str) -> dict[str, object]:
    resource = files("finance_agent_core.contracts.examples").joinpath(name)
    return json.loads(resource.read_text(encoding="utf-8"))


def test_backend_json_examples_validate_without_fastapi() -> None:
    request = BackendAgentRequest.model_validate(_example("backend_request_v1.json"))
    clarification = BackendAgentResponse.model_validate(
        _example("backend_clarification_response_v1.json")
    )
    error = BackendAgentResponse.model_validate(_example("backend_error_response_v1.json"))
    document = BackendAgentResponse.model_validate(_example("backend_document_response_v1.json"))
    aggregate = BackendAgentResponse.model_validate(_example("backend_aggregate_response_v1.json"))

    assert request.locale == "ko-KR"
    assert clarification.status is BackendStatus.CLARIFICATION
    assert clarification.clarification is not None
    assert error.status is BackendStatus.ERROR
    assert error.error is not None
    assert document.status is BackendStatus.SUCCESS
    assert document.documents[0].source_kind.value == "provided"
    assert aggregate.status is BackendStatus.SUCCESS
    assert aggregate.aggregates[0].function.value == "avg"


def test_backend_contract_forbids_extra_fields_and_invalid_state() -> None:
    payload = _example("backend_clarification_response_v1.json")
    payload["framework_internal"] = "must not leak"
    with pytest.raises(ValidationError, match="Extra inputs"):
        BackendAgentResponse.model_validate(payload)


def _verified_external_not_found_response() -> BackendAgentResponse:
    question = "Kimi 관련 해외 ETF를 찾아줘."
    plan = CompositeKnowledgeQueryPlan(
        question_id="backend-external-not-found-001",
        question=question,
        interaction_intent=InteractionIntent.SEARCH,
        evidence_cutoff=date(2026, 8, 24),
        requirements=(
            KnowledgeRequirement(
                requirement_id="need_1",
                capability=KnowledgeCapability.SECTOR_THEME_HISTORY,
                predicate=KnowledgePredicate.THEME_RELATED,
                subject="Kimi",
                subject_evidence_span="Kimi",
                relation_evidence_span="관련",
                product_families=(ProductFamily.OVERSEAS_ETP,),
                source_registry_ids=("moonshot-ai-official",),
            ),
        ),
        join_order=("need_1", "official_product_db"),
        execution_authorized=True,
    )
    document = DocumentEvidence(
        evidence_id="external-identity:kimi",
        document_id="moonshot-kimi",
        chunk_ordinal=0,
        title="Kimi official model notice",
        text="Kimi is an official Moonshot AI model name.",
        source_uri="https://forum.moonshot.ai/t/kimi/25",
        source_kind=DocumentSourceKind.EXTERNAL_APPROVED,
        as_of=date(2026, 8, 24),
        document_sha256="a" * 64,
        metadata={"publisher": "Moonshot AI"},
        relevance_score=1.0,
    )
    citation = SourceCitation(
        citation_id="external-identity:kimi",
        kind="relation_field",
        label=document.title,
        source_id="moonshot-ai-official",
        source_locator=document.source_uri,
        as_of=document.as_of,
        evidence_refs=[document.evidence_id],
    )
    return BackendAgentResponse(
        request_id=plan.question_id,
        status=BackendStatus.NOT_FOUND,
        intent=InteractionIntent.SEARCH,
        product_families=[ProductFamily.OVERSEAS_ETP],
        answer="찾으시는 상품은 제공 데이터 내에 존재하지 않습니다.",
        query_plan=plan,
        candidate_count=0,
        products=[],
        comparisons=[],
        aggregates=[],
        documents=[document],
        citations=[citation],
        as_of_dates=[document.as_of],
        warnings=[],
        answer_mode=BackendAnswerMode.DETERMINISTIC,
        fallback_used=False,
        provider_model=None,
        clarification=None,
        error=None,
        source_manifest=None,
    )


@pytest.mark.parametrize(
    "mutation",
    ["non_composite_plan", "unapproved_document", "non_relation_citation", "unbound_citation"],
)
def test_external_not_found_requires_plan_and_verified_evidence_together(
    mutation: str,
) -> None:
    valid = _verified_external_not_found_response()
    payload = valid.model_dump(mode="python")
    if mutation == "non_composite_plan":
        ordinary_plan = deepcopy(_example("backend_aggregate_response_v1.json")["query_plan"])
        ordinary_plan["question_id"] = valid.request_id
        payload["query_plan"] = ordinary_plan
    elif mutation == "unapproved_document":
        payload["documents"] = [
            valid.documents[0].model_copy(
                update={"source_kind": DocumentSourceKind.PROVIDED}
            )
        ]
    elif mutation == "non_relation_citation":
        payload["citations"] = [
            valid.citations[0].model_copy(update={"kind": "product_field"})
        ]
    elif mutation == "unbound_citation":
        payload["citations"] = [
            valid.citations[0].model_copy(update={"evidence_refs": ["different-evidence"]})
        ]
    else:  # pragma: no cover - exhaustive test table
        raise AssertionError(mutation)

    with pytest.raises(
        ValidationError,
        match="only external-knowledge not_found may contain source evidence",
    ):
        BackendAgentResponse.model_validate(payload)

    payload = _example("backend_document_response_v1.json")
    payload["documents"] = []
    with pytest.raises(
        ValidationError,
        match="requires product, aggregate, or document evidence",
    ):
        BackendAgentResponse.model_validate(payload)


def test_internal_plan_authority_never_leaks_into_backend_dto(
    sample_database: tuple[Path, list[NormalizedOverseasEtpRecord], DatabaseManifest],
) -> None:
    path, _, _ = sample_database
    routed = RoutedFinanceAgent({"overseas_etp": path}).answer(
        "총보수율이 낮은 해외 ETF 3개를 보여줘",
        "backend-authority-private-001",
    )
    payload = routed_result_to_backend(routed).model_dump(mode="json")
    forbidden = {
        "validated_plan",
        "validation_receipt",
        "receipt",
        "authority_seal",
        "canonical_plan_json",
    }

    def collect_keys(value: object) -> set[str]:
        if isinstance(value, dict):
            return set(value) | {key for child in value.values() for key in collect_keys(child)}
        if isinstance(value, list):
            return {key for child in value for key in collect_keys(child)}
        return set()

    assert forbidden.isdisjoint(collect_keys(payload))


@pytest.mark.parametrize("status", ["clarification", "unsupported"])
def test_backend_control_contract_rejects_partial_query_plan(status: str) -> None:
    control = BackendAgentResponse.model_validate(
        _example("backend_clarification_response_v1.json")
    ).model_dump(mode="json")
    if status == "unsupported":
        control.update(
            {
                "status": "unsupported",
                "intent": "unsupported",
                "clarification": None,
            }
        )
    plan = _example("backend_aggregate_response_v1.json")["query_plan"]
    assert isinstance(plan, dict)
    plan = deepcopy(plan)
    plan["question_id"] = control["request_id"]
    control["query_plan"] = plan

    expected = (
        "clarification response cannot contain executed state"
        if status == "clarification"
        else "unsupported response cannot contain control or executed state"
    )
    with pytest.raises(ValidationError, match=expected):
        BackendAgentResponse.model_validate(control)


@pytest.mark.parametrize(
    "field",
    ["citations", "as_of_dates", "warnings", "source_manifest"],
)
def test_clarification_contract_rejects_all_executed_evidence_fields(field: str) -> None:
    control = BackendAgentResponse.model_validate(
        _example("backend_clarification_response_v1.json")
    ).model_dump(mode="json")
    executed = _example("backend_aggregate_response_v1.json")
    control[field] = deepcopy(executed[field])

    with pytest.raises(
        ValidationError,
        match="clarification response cannot contain executed state",
    ):
        BackendAgentResponse.model_validate(control)


def test_backend_control_contract_requires_control_answer_mode() -> None:
    control = BackendAgentResponse.model_validate(
        _example("backend_clarification_response_v1.json")
    ).model_dump(mode="json")
    control["answer_mode"] = "deterministic"

    with pytest.raises(
        ValidationError,
        match="clarification response requires control mode without fallback",
    ):
        BackendAgentResponse.model_validate(control)


def test_routed_product_result_adapts_to_backend_contract(
    sample_database: tuple[Path, list[NormalizedOverseasEtpRecord], DatabaseManifest],
) -> None:
    path, _, _ = sample_database
    routed = RoutedFinanceAgent({"overseas_etp": path}).answer(
        "미국 채권형 해외 ETF 중 현재 거래 가능한 상품에서 "
        "총보수 0.20% 이하를 AUM 높은 순으로 3개 보여줘",
        "backend-adapter-001",
    )

    response = routed_result_to_backend(routed)

    assert response.status is BackendStatus.SUCCESS
    assert response.query_plan is not None
    assert response.candidate_count == 6
    assert len(response.products) == 3
    assert response.citations
    assert response.as_of_dates
    assert response.answer_mode.value == "deterministic"
    assert not response.fallback_used
    BackendAgentResponse.model_validate_json(response.model_dump_json())


def test_verified_document_intersection_with_unavailable_metric_remains_success(
    sample_database: tuple[Path, list[NormalizedOverseasEtpRecord], DatabaseManifest],
) -> None:
    path, _, _ = sample_database
    routed = RoutedFinanceAgent({"overseas_etp": path}).answer(
        "총보수율이 999% 이상인 해외 ETF 3개를 보여줘",
        "backend-verified-empty-metric-001",
    )
    assert routed.candidate_count == 0
    document = DocumentEvidence(
        evidence_id="official-relation:verified-empty-metric",
        document_id="official-relation",
        chunk_ordinal=0,
        title="공식 구성종목 자료",
        text="요청 상품과 기업의 관계를 확인한 공식 근거입니다.",
        source_uri="https://example.com/official-relation",
        source_kind=DocumentSourceKind.EXTERNAL_APPROVED,
        as_of="2026-08-24",
        document_sha256="a" * 64,
        metadata={"publisher": "official-publisher"},
        relevance_score=1.0,
    )
    routed = routed.model_copy(
        update={
            "documents": [document],
            "verified_document_intersection_count": 1,
        }
    )

    response = routed_result_to_backend(routed)

    assert response.status is BackendStatus.SUCCESS
    assert response.candidate_count == 0
    assert response.products == []
    assert response.documents == [document]
    BackendAgentResponse.model_validate_json(response.model_dump_json())


def test_executed_backend_projection_audits_citation_and_dto_cost_without_payload(
    sample_database: tuple[Path, list[NormalizedOverseasEtpRecord], DatabaseManifest],
) -> None:
    path, _, _ = sample_database
    routed = RoutedFinanceAgent({"overseas_etp": path}).answer(
        "미국 채권형 해외 ETF를 AUM 높은 순으로 3개 보여줘",
        "backend-serialization-audit-001",
    )
    downstream = InMemoryAuditSink()
    sink = BoundedAsyncAuditSink(downstream)
    recorder = RequestAuditRecorder(
        request_id="backend-serialization-audit-001",
        question="미국 채권형 해외 ETF를 AUM 높은 순으로 3개 보여줘",
        sink=sink,
    )

    with bind_request_audit(recorder):
        response = routed_result_to_backend(routed)
    assert sink.close(timeout_seconds=1) is True

    events = downstream.snapshot()
    assert [(event.stage, event.reason_code) for event in events] == [
        (AuditStage.SERIALIZATION, "citations_built"),
        (AuditStage.SERIALIZATION, "backend_dto_built"),
    ]
    assert events[0].evidence_count == len(
        {reference for citation in response.citations for reference in citation.evidence_refs}
    )
    assert events[1].result_count == len(response.products)
    assert all(event.duration_ms >= 0 for event in events)


def test_routed_clarification_adapts_without_execution_evidence() -> None:
    routed = RoutedFinanceAgent({}).answer(
        "해외 ETF 상세 정보를 알려줘",
        "backend-adapter-002",
    )

    response = routed_result_to_backend(routed)

    assert response.status is BackendStatus.CLARIFICATION
    assert response.clarification is not None
    assert response.clarification.required_fields == ["product_identity"]
    assert response.products == []
    assert response.candidate_count is None


def test_routed_aggregate_result_adapts_with_aggregate_citations(
    sample_database: tuple[Path, list[NormalizedOverseasEtpRecord], DatabaseManifest],
) -> None:
    path, _, _ = sample_database
    routed = RoutedFinanceAgent({"overseas_etp": path}).answer(
        "해외 ETF의 총보수율 평균을 집계해줘",
        "backend-adapter-aggregate-001",
    )

    response = routed_result_to_backend(routed)

    assert response.status is BackendStatus.SUCCESS
    assert response.products == []
    assert len(response.aggregates) == 1
    assert response.citations[0].kind == "aggregate_field"
    assert response.citations[0].evidence_refs == [response.aggregates[0].evidence_id]
    assert response.as_of_dates
    BackendAgentResponse.model_validate_json(response.model_dump_json())


def test_backend_contract_exports_request_and_response_json_schema() -> None:
    schemas = backend_contract_schemas()

    assert set(schemas) == {"request", "response"}
    assert schemas["request"]["additionalProperties"] is False
    assert schemas["response"]["additionalProperties"] is False
    serialized = json.dumps(schemas)
    assert "fastapi" not in serialized.casefold()
