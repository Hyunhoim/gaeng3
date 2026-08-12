import pytest

from scripts.smoke import (
    SmokeCase,
    smoke_cases,
    validate_answer,
    validate_health,
    validate_official_answer,
)


def test_validate_health_accepts_ready_four_family_service() -> None:
    assert (
        validate_health(
            200,
            {
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
            },
        )
        == []
    )


def test_validate_health_checks_declared_fund_execution_policy() -> None:
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

    assert (
        validate_health(
            200,
            body,
            expected_fund_execution_policy="locked",
        )
        == []
    )
    assert "fund execution policy differs" in validate_health(
        200,
        body,
        expected_fund_execution_policy="public_fund_v1_approved",
    )


def test_smoke_cases_switch_only_fund_expectation_for_approved_policy() -> None:
    locked = smoke_cases("locked")
    approved = smoke_cases("public_fund_v1_approved")
    locked_fund = next(case for case in locked if "fund" in case.case_id)
    approved_fund = next(case for case in approved if "fund" in case.case_id)

    assert locked_fund.expected_status == "unsupported"
    assert locked_fund.expected_intent == "unsupported"
    assert locked_fund.expected_clarification_code is None
    assert approved_fund.case_id == "docker-smoke-fund-approved-001"
    assert approved_fund.expected_status == "success"
    assert approved_fund.expected_product_count == 5
    assert approved_fund.expected_dataset == "fund"
    assert approved_fund.expected_clarification_code is None

    with pytest.raises(ValueError, match="unsupported fund execution policy"):
        smoke_cases("unsafe")


def test_validate_answer_checks_success_evidence() -> None:
    case = SmokeCase(
        case_id="smoke-success",
        question="해외 ETF를 보여줘.",
        expected_http_status=200,
        expected_status="success",
        expected_intent="search",
        expected_families=("overseas_etp",),
        expected_answer_mode="deterministic",
        expected_product_count=1,
        expected_dataset="overseas_etp",
    )
    body = {
        "request_id": "smoke-success",
        "status": "success",
        "intent": "search",
        "product_families": ["overseas_etp"],
        "answer_mode": "deterministic",
        "fallback_used": False,
        "provider_model": None,
        "products": [{"product_id": "ONE"}],
        "citations": [{"citation_id": "ONE:name"}],
        "candidate_count": 2,
        "as_of_dates": ["2026-07-11"],
        "source_manifest": {"dataset": "overseas_etp"},
        "clarification": None,
        "error": None,
    }

    assert validate_answer(case, 200, body) == []
    body["citations"] = []
    assert "citations are missing" in validate_answer(case, 200, body)


def test_validate_answer_accepts_grounded_local_model() -> None:
    case = SmokeCase(
        case_id="smoke-qwen",
        question="해외 ETF를 보여줘.",
        expected_http_status=200,
        expected_status="success",
        expected_intent="search",
        expected_families=("overseas_etp",),
        expected_product_count=1,
        expected_dataset="overseas_etp",
    )
    body = {
        "request_id": "smoke-qwen",
        "status": "success",
        "intent": "search",
        "product_families": ["overseas_etp"],
        "answer_mode": "llm_grounded",
        "fallback_used": False,
        "provider_model": "qwen3-local-test",
        "products": [{"product_id": "ONE"}],
        "citations": [{"citation_id": "ONE:name"}],
        "candidate_count": 2,
        "as_of_dates": ["2026-07-11"],
        "source_manifest": {"dataset": "overseas_etp"},
        "clarification": None,
        "error": None,
    }

    assert (
        validate_answer(
            case,
            200,
            body,
            success_answer_mode="llm_grounded",
            provider_model="qwen3-local-test",
        )
        == []
    )

    body["answer_mode"] = "deterministic_fallback"
    body["fallback_used"] = True
    assert (
        validate_answer(
            case,
            200,
            body,
            success_answer_mode="deterministic_fallback",
            provider_model="qwen3-local-test",
        )
        == []
    )


def test_validate_answer_accepts_locked_fund_control() -> None:
    case = SmokeCase(
        case_id="smoke-fund",
        question="공모펀드를 보여줘.",
        expected_http_status=200,
        expected_status="clarification",
        expected_intent="search",
        expected_families=("fund",),
        expected_clarification_code="capability_executable",
    )
    body = {
        "request_id": "smoke-fund",
        "status": "clarification",
        "intent": "search",
        "product_families": ["fund"],
        "answer_mode": "control",
        "fallback_used": False,
        "provider_model": None,
        "products": [],
        "citations": [],
        "candidate_count": None,
        "clarification": {"code": "capability_executable"},
        "error": None,
    }

    assert validate_answer(case, 200, body) == []


def test_validate_official_answer_requires_five_json_string_fields() -> None:
    body = {
        "question_id": "Q-001",
        "question": "평가 질문",
        "retrieved_context": '{"citations":[]}',
        "think_trace": '{"status":"not_found"}',
        "answer": "확인할 수 없습니다.",
    }

    assert (
        validate_official_answer(
            200,
            body,
            question_id="Q-001",
            question="평가 질문",
        )
        == []
    )
    body["think_trace"] = "not-json"
    assert "official think_trace is not valid JSON text" in validate_official_answer(
        200,
        body,
        question_id="Q-001",
        question="평가 질문",
    )


def test_validate_official_answer_checks_safe_control_code() -> None:
    body = {
        "question_id": "invalid-question-id",
        "question": "",
        "retrieved_context": '{"citations":[]}',
        "think_trace": '{"status":"error","control_code":"invalid_request"}',
        "answer": "요청 형식을 확인해 주세요.",
    }

    assert (
        validate_official_answer(
            200,
            body,
            question_id="invalid-question-id",
            question="",
            expected_control_code="invalid_request",
        )
        == []
    )
    body["think_trace"] = '{"status":"error","control_code":"wrong"}'
    assert "official control code differs" in validate_official_answer(
        200,
        body,
        question_id="invalid-question-id",
        question="",
        expected_control_code="invalid_request",
    )


def test_validate_official_answer_rejects_reflected_forbidden_fragment() -> None:
    fragment = "<script>alert(1)</script>"
    body = {
        "question_id": "Q-SAFE",
        "question": f"ETF를 알려줘 {fragment}",
        "retrieved_context": '{"citations":[]}',
        "think_trace": '{"status":"unsupported"}',
        "answer": f"지원하지 않습니다. {fragment}",
    }

    errors = validate_official_answer(
        200,
        body,
        question_id="Q-SAFE",
        question=f"ETF를 알려줘 {fragment}",
        forbidden_output_fragments=(fragment,),
    )

    assert "official answer reflected a forbidden input fragment" in errors
