from __future__ import annotations

import json
from copy import deepcopy
from importlib.resources import files
from pathlib import Path

import pytest
from pydantic import ValidationError

from finance_agent_core.agent import RoutedFinanceAgent
from finance_agent_core.contracts.backend import (
    BackendAgentRequest,
    BackendAgentResponse,
    BackendStatus,
    backend_contract_schemas,
    routed_result_to_backend,
)
from finance_agent_core.domain import DatabaseManifest, NormalizedOverseasEtpRecord


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

    payload = _example("backend_document_response_v1.json")
    payload["documents"] = []
    with pytest.raises(
        ValidationError,
        match="requires product, aggregate, or document evidence",
    ):
        BackendAgentResponse.model_validate(payload)


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
