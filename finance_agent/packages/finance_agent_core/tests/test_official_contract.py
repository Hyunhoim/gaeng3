from __future__ import annotations

import json
from importlib.resources import files

from finance_agent_core.agent import (
    invalid_official_request_response,
    official_response_from_backend,
)
from finance_agent_core.contracts.backend import BackendAgentResponse
from finance_agent_core.contracts.official import OfficialAnswerResponse


def _backend_example(name: str) -> BackendAgentResponse:
    resource = files("finance_agent_core.contracts.examples").joinpath(name)
    return BackendAgentResponse.model_validate_json(resource.read_bytes())


def test_official_response_projects_grounded_context_and_structured_trace() -> None:
    response = official_response_from_backend(
        question_id="Q-001",
        question="원화 국내채권의 발행잔액 합계를 알려줘",
        response=_backend_example("backend_aggregate_response_v1.json"),
    )

    OfficialAnswerResponse.model_validate_json(response.model_dump_json())
    assert all(isinstance(value, str) for value in response.model_dump().values())
    context = json.loads(response.retrieved_context)
    trace = json.loads(response.think_trace)
    assert context["citations"]
    assert trace["trace_type"] == "structured_execution_summary_not_hidden_reasoning"
    assert trace["execution_steps"][-1] == "response_contract_validation"
    assert "/home/" not in response.model_dump_json()


def test_invalid_official_request_keeps_fixed_five_string_contract() -> None:
    response = invalid_official_request_response(question_id=" ", question=None)

    assert set(response.model_dump()) == {
        "question_id",
        "question",
        "retrieved_context",
        "think_trace",
        "answer",
    }
    assert response.question_id == "invalid-question-id"
    assert json.loads(response.think_trace)["control_code"] == "invalid_request"
