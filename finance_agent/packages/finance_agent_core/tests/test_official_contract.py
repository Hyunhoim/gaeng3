from __future__ import annotations

import json
from importlib.resources import files

from finance_agent_core.agent import (
    invalid_official_request_response,
    official_response_from_backend,
    official_timeout_response,
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
    assert context["evidence"]["aggregates"] == [
        {
            "as_of_end": "2026-06-14",
            "as_of_start": "2026-06-14",
            "evidence_ref": "aggregate_1_1_avg_total_expense_ratio_pct",
            "field": "total_expense_ratio_pct",
            "function": "avg",
            "group_values": {},
            "label": "총보수율",
            "missing_count": 1,
            "row_count": 10,
            "source_snapshot_date": "2026-07-11",
            "unit": "pct_point",
            "valid_count": 9,
            "value": "0.138888888889",
        }
    ]
    assert context["truncation"] == {
        "aggregates": False,
        "citations": False,
        "comparisons": False,
        "documents": False,
        "products": False,
    }
    assert trace["trace_type"] == "structured_execution_summary_not_hidden_reasoning"
    assert trace["execution_steps"][-1] == "response_contract_validation"
    assert "/home/" not in response.model_dump_json()


def test_official_response_includes_bounded_document_evidence() -> None:
    response = official_response_from_backend(
        question_id="Q-DOC-001",
        question="위험등급이 무엇인지 알려줘",
        response=_backend_example("backend_document_response_v1.json"),
    )

    context = json.loads(response.retrieved_context)
    document = context["evidence"]["documents"][0]
    assert document == {
        "as_of": "2026-07-11",
        "chunk_ordinal": 0,
        "document_id": "provided-terms",
        "document_sha256": "a" * 64,
        "evidence_ref": "provided-terms:0000",
        "source_kind": "provided",
        "source_uri": "approved://provided-terms",
        "text": "위험등급은 제공 데이터에서 상품별 위험 분류를 나타냅니다.",
        "text_truncated": False,
        "title": "금융상품 용어집",
    }
    assert context["citations"][0]["evidence_refs"] == ["provided-terms:0000"]


def test_official_response_truncates_long_document_text() -> None:
    backend = _backend_example("backend_document_response_v1.json")
    document = backend.documents[0].model_copy(update={"text": "가" * 2_001})
    backend = backend.model_copy(update={"documents": [document]})

    response = official_response_from_backend(
        question_id="Q-DOC-LONG",
        question="긴 문서 근거를 알려줘",
        response=backend,
    )

    evidence = json.loads(response.retrieved_context)["evidence"]["documents"][0]
    assert len(evidence["text"]) == 2_000
    assert evidence["text_truncated"] is True


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


def test_official_timeout_response_has_no_evidence_and_fixed_control_code() -> None:
    response = official_timeout_response(
        question_id="Q-TIMEOUT",
        question="처리가 오래 걸리는 평가 질문",
    )

    assert response.question_id == "Q-TIMEOUT"
    assert json.loads(response.retrieved_context)["citations"] == []
    assert json.loads(response.think_trace)["control_code"] == "request_timeout"
    assert "시간이 초과" in response.answer
