from __future__ import annotations

import hashlib
import json
from urllib.parse import parse_qsl, urlparse

import pytest

from finance_agent_core.evaluation.briefing_examples import load_briefing_example_suite
from finance_agent_core.evaluation.official_acceptance import (
    FROZEN_BRIEFING_SUITE_SHA256,
    FROZEN_SOURCE_ARTIFACT_SHA256,
    OfficialAcceptanceCase,
    OfficialAcceptanceCaseKind,
    OfficialAcceptanceRunner,
    build_official_acceptance_cases,
    evaluate_official_acceptance_case,
    sha256_file,
)


def _official_body(case: OfficialAcceptanceCase) -> dict[str, str]:
    if case.expected_control_code is not None:
        status = "error"
        intent = "unsupported"
        no_execution = True
    elif case.require_no_execution:
        status = (
            "clarification"
            if "clarification" in case.expected_trace_statuses
            else case.expected_trace_statuses[0]
        )
        intent = (
            "clarify"
            if "clarify" in case.expected_intents
            else case.expected_intents[0]
            if case.expected_intents
            else "unsupported"
        )
        no_execution = True
    else:
        status = "success"
        intent = "search"
        no_execution = False
    families = [family.value for family in case.expected_families]
    if not families and not no_execution:
        families = ["bond"]
    citations: list[dict[str, object]] = []
    evidence: dict[str, object] | None = None
    returned_products = 0
    if not no_execution:
        citations = [
            {
                "citation_id": "product:bond-test:product_name",
                "kind": "product_field",
                "evidence_refs": ["bond-test:product_name"],
            }
        ]
        evidence = {"products": [{"product_id": "bond-test"}]}
        returned_products = 1
    context: dict[str, object] = {"citations": citations}
    if evidence is not None:
        context["evidence"] = evidence
    trace: dict[str, object] = {
        "status": status,
        "intent": intent,
        "product_families": families,
        "candidate_count": None if no_execution else 1,
        "returned_evidence": {
            "products": returned_products,
            "comparisons": 0,
            "aggregates": 0,
            "documents": 0,
            "citations": len(citations),
        },
        "answer_mode": "control" if no_execution else "deterministic",
        "fallback_used": False,
    }
    if case.expected_control_code is not None:
        trace["control_code"] = case.expected_control_code
    return {
        "question_id": case.expected_question_id,
        "question": case.expected_question,
        "retrieved_context": json.dumps(context, ensure_ascii=False),
        "think_trace": json.dumps(trace, ensure_ascii=False),
        "answer": "검증된 계약에 따른 응답입니다.",
    }


def _healthy_body() -> dict[str, object]:
    families = ["bond", "domestic_etp", "overseas_etp", "fund"]
    return {
        "status": "ok",
        "configured_product_families": families,
        "ready_product_families": families,
        "missing_product_families": [],
        "unavailable_product_families": [],
        "fund_execution_policy": "locked",
    }


def test_acceptance_cases_pin_source_and_reuse_all_public_examples() -> None:
    loaded = load_briefing_example_suite()
    cases = build_official_acceptance_cases(loaded)

    assert loaded.sha256 == FROZEN_BRIEFING_SUITE_SHA256
    assert loaded.suite.source.sha256 == FROZEN_SOURCE_ARTIFACT_SHA256
    assert len(cases) == 16
    assert [case.expected_question for case in cases[:8]] == [
        example.question for example in loaded.suite.cases
    ]
    assert sum(case.kind is OfficialAcceptanceCaseKind.PUBLIC_EXAMPLE for case in cases) == 8
    assert sum(case.kind is OfficialAcceptanceCaseKind.TRANSPORT_EDGE for case in cases) == 8


def test_acceptance_runner_uses_sequential_get_and_scores_contract() -> None:
    loaded = load_briefing_example_suite()
    cases = build_official_acceptance_cases(loaded)
    requested_case_ids: list[str] = []

    def requester(url: str, timeout: float):
        assert timeout == 60.0
        parsed = urlparse(url)
        if parsed.path == "/health":
            body = _healthy_body()
            return 200, body, len(json.dumps(body).encode()), 1.0, "application/json"
        case = cases[len(requested_case_ids)]
        assert parse_qsl(parsed.query, keep_blank_values=True) == list(case.query)
        requested_case_ids.append(case.id)
        body = _official_body(case)
        return (
            200,
            body,
            len(json.dumps(body, ensure_ascii=False).encode()),
            10.0,
            "application/json; charset=utf-8",
        )

    report = OfficialAcceptanceRunner(
        loaded_suite=loaded,
        base_url="http://127.0.0.1:18002/",
        implementation_commit="a" * 40,
        runtime_image_reference="gaeng3-backend:acceptance-test",
        observed_source_artifact_sha256=FROZEN_SOURCE_ARTIFACT_SHA256,
        requester=requester,
    ).run(generated_at_utc="2026-08-19T00:00:00+00:00")

    assert requested_case_ids == [case.id for case in cases]
    assert [result.request_index for result in report.cases] == list(range(1, 17))
    assert report.request_contract == "unauthenticated_sequential_get"
    assert report.implementation_commit == "a" * 40
    assert report.runtime_image_reference == "gaeng3-backend:acceptance-test"
    assert report.summary.total == 16
    assert report.summary.contract_passed == 16
    assert report.summary.public_examples_passed == 8
    assert report.summary.public_unanswerable_safely_handled == 3
    assert report.summary.transport_edge_passed == 8
    assert report.summary.no_execution_passed == report.summary.no_execution_total
    assert report.summary.perfect


def test_acceptance_requires_verified_source_for_full_perfect() -> None:
    loaded = load_briefing_example_suite()
    cases = build_official_acceptance_cases(loaded)
    index = 0

    def requester(url: str, timeout: float):
        nonlocal index
        parsed = urlparse(url)
        if parsed.path == "/health":
            body = _healthy_body()
            return 200, body, 100, 1.0, "application/json"
        case = cases[index]
        index += 1
        body = _official_body(case)
        return 200, body, 100, 1.0, "application/json; charset=utf-8"

    report = OfficialAcceptanceRunner(
        loaded_suite=loaded,
        base_url="http://127.0.0.1:18002",
        implementation_commit="a" * 40,
        runtime_image_reference=None,
        observed_source_artifact_sha256=None,
        requester=requester,
    ).run(generated_at_utc="2026-08-19T00:00:00+00:00")

    assert report.summary.api_perfect
    assert not report.summary.perfect
    assert report.source_artifact.status == "not_provided"


def test_acceptance_rejects_source_artifact_hash_mismatch() -> None:
    with pytest.raises(ValueError, match="observed source artifact SHA-256 differs"):
        OfficialAcceptanceRunner(
            loaded_suite=load_briefing_example_suite(),
            base_url="http://127.0.0.1:18002",
            implementation_commit="a" * 40,
            runtime_image_reference=None,
            observed_source_artifact_sha256="0" * 64,
        )


def test_acceptance_rejects_unsafe_execution_for_unanswerable_example() -> None:
    case = next(
        case
        for case in build_official_acceptance_cases(load_briefing_example_suite())
        if case.answerability == "unanswerable"
    )
    body = _official_body(case)
    context = {
        "citations": [{"citation_id": "unsafe"}],
        "evidence": {"products": [{"product_id": "unsafe"}]},
    }
    trace = json.loads(body["think_trace"])
    trace.update(
        {
            "status": "success",
            "candidate_count": 1,
            "returned_evidence": {
                "products": 1,
                "comparisons": 0,
                "aggregates": 0,
                "documents": 0,
                "citations": 1,
            },
        }
    )
    body["retrieved_context"] = json.dumps(context)
    body["think_trace"] = json.dumps(trace)

    result = evaluate_official_acceptance_case(
        case,
        request_index=1,
        http_status=200,
        body=body,
        response_bytes=100,
        latency_ms=1.0,
        content_type="application/json; charset=utf-8",
    )

    assert result.contract_passed
    assert not result.safety_passed
    assert not result.passed
    assert "safe_control_status" in result.violations
    assert "returned_evidence_empty" in result.violations


def test_sha256_file_streams_source_bytes(tmp_path) -> None:
    source = tmp_path / "source.jpeg"
    source.write_bytes(b"official-source")

    assert sha256_file(source) == hashlib.sha256(b"official-source").hexdigest()
