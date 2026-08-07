from __future__ import annotations

import json
from urllib.parse import parse_qs, urlparse

from finance_agent_core.evaluation.official_mock import (
    OfficialMockCase,
    load_official_mock_suite,
)
from finance_agent_core.evaluation.official_mock_http import (
    OfficialMockHttpRunner,
    evaluate_official_http_case,
    validate_health,
)


def _official_body(
    case: OfficialMockCase,
    *,
    profile: str = "local_test",
) -> dict[str, str]:
    expected = case.expectation
    citations: list[dict[str, object]] = []
    for product_id in expected.product_ids:
        citations.append(
            {
                "citation_id": f"product:{product_id}:product_name",
                "kind": "product_field",
                "evidence_refs": [f"{product_id}:product_name"],
            }
        )
    for field in expected.comparison_fields:
        citations.append(
            {
                "citation_id": f"comparison:{field}",
                "kind": "comparison_field",
                "evidence_refs": [f"{product_id}:{field}" for product_id in expected.product_ids],
            }
        )
    for index, function in enumerate(expected.aggregate_functions, start=1):
        citations.append(
            {
                "citation_id": f"aggregate:aggregate_1_{index}_{function}_field",
                "kind": "aggregate_field",
                "evidence_refs": [f"aggregate_1_{index}_{function}_field"],
            }
        )
    if expected.backend_status.value == "not_found":
        answer_mode = "deterministic"
    elif expected.backend_status.value != "success":
        answer_mode = "control"
    elif profile == "local_test" and expected.llm_answer_eligible:
        answer_mode = "llm_grounded"
    else:
        answer_mode = "deterministic"
    trace = {
        "status": expected.backend_status.value,
        "intent": expected.interaction_intent.value,
        "product_families": [family.value for family in expected.product_families],
        "candidate_count": expected.candidate_count,
        "returned_evidence": {
            "products": len(expected.product_ids),
            "comparisons": len(expected.comparison_fields),
            "aggregates": len(expected.aggregate_functions),
            "documents": 0,
            "citations": len(citations),
        },
        "answer_mode": answer_mode,
        "fallback_used": False,
    }
    return {
        "question_id": case.id,
        "question": case.question,
        "retrieved_context": json.dumps({"citations": citations}, ensure_ascii=False),
        "think_trace": json.dumps(trace, ensure_ascii=False),
        "answer": "검증된 근거에 따른 답변입니다.",
    }


def test_validate_health_requires_all_four_ready_families() -> None:
    healthy = {
        "status": "ok",
        "configured_product_families": ["bond", "domestic_etp", "overseas_etp", "fund"],
        "ready_product_families": ["bond", "domestic_etp", "overseas_etp", "fund"],
        "missing_product_families": [],
        "unavailable_product_families": [],
        "fund_execution_policy": "locked",
    }

    assert validate_health(200, healthy) == []
    assert "ready_families_exact" in validate_health(
        200, {**healthy, "ready_product_families": ["bond"]}
    )


def test_official_http_case_checks_five_strings_and_semantics() -> None:
    case = load_official_mock_suite().suite.cases[0]
    body = _official_body(case)

    result = evaluate_official_http_case(
        case,
        http_status=200,
        body=body,
        response_bytes=len(json.dumps(body).encode()),
        latency_ms=123.0,
        backend_profile="local_test",
        response_budget_seconds=60.0,
    )

    assert result.passed
    assert result.official_contract_passed
    assert result.semantic_passed
    assert result.answer_mode == "llm_grounded"


def test_official_http_case_exposes_safe_fund_policy_lock_as_semantic_gap() -> None:
    case = next(
        case
        for case in load_official_mock_suite().suite.cases
        if case.coverage_family.value == "fund"
        and case.expectation.backend_status.value == "success"
    )
    body = _official_body(case)
    body["retrieved_context"] = json.dumps({"citations": [], "reason": "조건 확인 필요"})
    trace = json.loads(body["think_trace"])
    trace.update(
        {
            "status": "clarification",
            "candidate_count": None,
            "returned_evidence": {
                "products": 0,
                "comparisons": 0,
                "aggregates": 0,
                "documents": 0,
                "citations": 0,
            },
            "answer_mode": "control",
        }
    )
    body["think_trace"] = json.dumps(trace)

    result = evaluate_official_http_case(
        case,
        http_status=200,
        body=body,
        response_bytes=100,
        latency_ms=10.0,
        backend_profile="local_test",
        response_budget_seconds=60.0,
    )

    assert result.official_contract_passed
    assert not result.semantic_passed
    assert not result.passed
    assert "backend_status_exact" in result.violations
    assert "evidence_shape_exact" in result.violations


def test_official_http_runner_scores_frozen_suite_over_get_requests() -> None:
    loaded = load_official_mock_suite()
    cases = {case.id: case for case in loaded.suite.cases}

    def requester(url: str, timeout: float):
        assert timeout == 60.0
        parsed = urlparse(url)
        if parsed.path == "/health":
            body = {
                "status": "ok",
                "configured_product_families": [
                    "bond",
                    "domestic_etp",
                    "overseas_etp",
                    "fund",
                ],
                "ready_product_families": [
                    "bond",
                    "domestic_etp",
                    "overseas_etp",
                    "fund",
                ],
                "missing_product_families": [],
                "unavailable_product_families": [],
                "fund_execution_policy": "locked",
            }
            return 200, body, 200, 1.0
        parameters = parse_qs(parsed.query)
        case = cases[parameters["question_id"][0]]
        assert parameters["question"][0] == case.question
        body = _official_body(case)
        return 200, body, len(json.dumps(body).encode()), 100.0

    report = OfficialMockHttpRunner(
        loaded_suite=loaded,
        base_url="http://127.0.0.1:18002/",
        backend_profile="local_test",
        declared_model="qwen3-local-test",
        requester=requester,
    ).run()

    assert report.base_url == "http://127.0.0.1:18002"
    assert report.summary.total == 30
    assert report.summary.passed == 30
    assert report.summary.unanswerable_safely_handled == 5
    assert report.summary.official_contract_passed == 30
    assert report.summary.llm_answer_eligible == 17
    assert report.summary.llm_grounded == 17
    assert report.summary.perfect
