import pytest

from scripts.smoke import (
    OFFICIAL_CASES,
    RELATION_OFFICIAL_CASES,
    SmokeCase,
    official_smoke_cases,
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


def test_validate_health_checks_relation_retrieval_readiness() -> None:
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
        "relation_retrieval_status": "ready",
    }

    assert (
        validate_health(
            200,
            body,
            expected_relation_retrieval_status="ready",
        )
        == []
    )
    assert "relation retrieval status differs" in validate_health(
        200,
        body,
        expected_relation_retrieval_status="disabled",
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


def test_smoke_cases_cover_relation_execution_and_disabled_control() -> None:
    ready = next(
        case
        for case in smoke_cases("locked", "ready")
        if case.case_id == "docker-smoke-relation-issued-by-001"
    )
    disabled = next(
        case
        for case in smoke_cases("locked", "disabled")
        if case.case_id == "docker-smoke-relation-issued-by-001"
    )

    assert ready.expected_status == "success"
    assert ready.expected_query_plan_kind == "relation_search"
    assert ready.expected_citation_kind == "relation_field"
    assert ready.uses_answer_provider is False
    assert disabled.expected_status == "unsupported"
    assert disabled.expected_intent == "unsupported"
    assert disabled.expected_families == ()

    with pytest.raises(ValueError, match="unsupported relation retrieval status"):
        smoke_cases("locked", "degraded")


def test_validate_answer_checks_relation_plan_and_evidence_kind() -> None:
    case = next(
        case
        for case in smoke_cases("locked", "ready")
        if case.case_id == "docker-smoke-relation-issued-by-001"
    )
    body = {
        "request_id": case.case_id,
        "status": "success",
        "intent": "search",
        "product_families": ["bond"],
        "answer_mode": "deterministic",
        "fallback_used": False,
        "provider_model": None,
        "query_plan": {"operation": {"kind": "relation_search"}},
        "products": [{"product_id": "BOND-001"}] * 3,
        "citations": [{"kind": "relation_field"}] * 3,
        "candidate_count": 3,
        "as_of_dates": ["2026-07-11"],
        "source_manifest": None,
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
    body["citations"] = [{"kind": "product_field"}] * 3
    assert "citation kind differs" in validate_answer(case, 200, body)


def test_official_smoke_keeps_public_fund_locked() -> None:
    case = next(item for item in OFFICIAL_CASES if item.case_id == "official-fund-locked")

    assert case.expected_trace_status == "unsupported"
    assert case.expected_intent == "unsupported"
    assert case.expected_families == ("fund",)
    assert case.expected_empty_context is True


def test_official_smoke_requires_verified_evidence_for_success() -> None:
    case = next(item for item in OFFICIAL_CASES if item.case_id == "official-valid")

    assert case.expected_evidence is True


def test_official_smoke_adds_full_relation_matrix_only_for_active_release() -> None:
    ready = official_smoke_cases("ready")
    disabled = official_smoke_cases("disabled")

    assert ready == OFFICIAL_CASES + RELATION_OFFICIAL_CASES
    assert disabled == OFFICIAL_CASES
    assert {case.case_id for case in RELATION_OFFICIAL_CASES} == {
        "official-relation-exact",
        "official-relation-partial-not-found",
        "official-relation-clarify",
        "official-relation-unsupported",
    }
    exact = next(case for case in ready if case.case_id == "official-relation-exact")
    assert exact.expected_execution_step == "relation_retrieval"
    assert exact.expected_citation_kind == "relation_field"
    assert exact.expected_evidence is True
    assert exact.expected_relation_type == "issued_by"
    assert exact.expected_relation_query == "한국주택금융공사"
    assert exact.expected_relation_top_k == 3
    assert exact.expected_product_count == 3
    assert exact.expected_relation_field == "issuer"
    assert exact.expected_relation_value == "한국주택금융공사"
    not_found = next(
        case for case in ready if case.case_id == "official-relation-partial-not-found"
    )
    assert not_found.expected_candidate_count == 0
    assert not_found.expected_product_count == 0
    for control_id in ("official-relation-clarify", "official-relation-unsupported"):
        control = next(case for case in ready if case.case_id == control_id)
        assert control.require_safe_control_response is True
        assert control.forbidden_execution_steps == (
            "relation_retrieval",
            "sql",
            "deterministic_oracle",
        )
    clarify = next(case for case in ready if case.case_id == "official-relation-clarify")
    unsupported = next(case for case in ready if case.case_id == "official-relation-unsupported")
    assert clarify.expected_control_code == "ambiguous_product_family"
    assert unsupported.expected_control_code is None

    with pytest.raises(ValueError, match="unsupported relation retrieval status"):
        official_smoke_cases("degraded")


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
    content_type_errors = validate_official_answer(
        200,
        body,
        content_type="application/json",
        question_id="Q-001",
        question="평가 질문",
    )
    assert any(error.startswith("official Content-Type differs") for error in content_type_errors)


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


def test_validate_official_answer_allows_typed_relation_control_code() -> None:
    body = {
        "question_id": "Q-RELATION-CLARIFY",
        "question": "미래에셋이 운용하는 ETF를 보여줘.",
        "retrieved_context": '{"citations":[],"reason":"조건 확인 필요"}',
        "think_trace": (
            '{"status":"clarification","intent":"clarify",'
            '"control_code":"ambiguous_product_family"}'
        ),
        "answer": "상품군을 명시해 주세요.",
    }

    assert (
        validate_official_answer(
            200,
            body,
            question_id="Q-RELATION-CLARIFY",
            question="미래에셋이 운용하는 ETF를 보여줘.",
            expected_control_code="ambiguous_product_family",
            expected_trace_status="clarification",
            expected_intent="clarify",
            expected_empty_context=True,
        )
        == []
    )


def test_validate_official_answer_checks_locked_fund_trace() -> None:
    body = {
        "question_id": "Q-FUND",
        "question": "공모펀드를 보여줘",
        "retrieved_context": '{"citations":[],"reason":"지원하지 않음"}',
        "think_trace": (
            '{"status":"unsupported","intent":"unsupported","product_families":["fund"]}'
        ),
        "answer": "현재 공모펀드 실행은 지원하지 않습니다.",
    }

    assert (
        validate_official_answer(
            200,
            body,
            question_id="Q-FUND",
            question="공모펀드를 보여줘",
            expected_trace_status="unsupported",
            expected_intent="unsupported",
            expected_families=("fund",),
            expected_empty_context=True,
        )
        == []
    )


def test_validate_official_answer_checks_verified_evidence() -> None:
    body = {
        "question_id": "Q-001",
        "question": "매수 가능한 채권을 보여줘",
        "retrieved_context": (
            '{"evidence":{"products":[{"product_id":"BOND-001"}]},'
            '"citations":[{"citation_id":"citation-001"}]}'
        ),
        "think_trace": '{"status":"success","intent":"search"}',
        "answer": "검증된 상품을 안내합니다.",
    }

    assert (
        validate_official_answer(
            200,
            body,
            question_id="Q-001",
            question="매수 가능한 채권을 보여줘",
            expected_evidence=True,
        )
        == []
    )


def test_validate_official_answer_checks_relation_execution_and_citation_kind() -> None:
    body = {
        "question_id": "Q-RELATION",
        "question": "한국주택금융공사가 발행한 국내채권을 보여줘",
        "retrieved_context": (
            '{"evidence":{"products":['
            '{"product_id":"BOND-001","fields":['
            '{"field":"issuer","value":"한국주택금융공사"}]},'
            '{"product_id":"BOND-002","fields":['
            '{"field":"issuer","value":"한국주택금융공사"}]},'
            '{"product_id":"BOND-003","fields":['
            '{"field":"issuer","value":"한국주택금융공사"}]}]},'
            '"citations":[{"kind":"relation_field"}]}'
        ),
        "think_trace": (
            '{"status":"success","intent":"search","product_families":["bond"],'
            '"execution_steps":["intent_router","relation_retrieval"],'
            '"filters":[{"relation_type":"issued_by",'
            '"query":"한국주택금융공사","top_k":3}],"candidate_count":3}'
        ),
        "answer": "검증된 관계 상품을 안내합니다.",
    }

    assert (
        validate_official_answer(
            200,
            body,
            question_id="Q-RELATION",
            question="한국주택금융공사가 발행한 국내채권을 보여줘",
            expected_trace_status="success",
            expected_intent="search",
            expected_families=("bond",),
            expected_evidence=True,
            expected_execution_step="relation_retrieval",
            expected_citation_kind="relation_field",
            expected_relation_type="issued_by",
            expected_relation_query="한국주택금융공사",
            expected_relation_top_k=3,
            expected_product_count=3,
            expected_relation_field="issuer",
            expected_relation_value="한국주택금융공사",
            expected_candidate_count=3,
        )
        == []
    )
    body["retrieved_context"] = (
        '{"evidence":{"products":[{"product_id":"BOND-001","fields":['
        '{"field":"issuer","value":"한국주택금융공사"}]}]},'
        '"citations":[{"kind":"product_field"}]}'
    )
    assert "official citation kind differs" in validate_official_answer(
        200,
        body,
        question_id="Q-RELATION",
        question="한국주택금융공사가 발행한 국내채권을 보여줘",
        expected_execution_step="relation_retrieval",
        expected_citation_kind="relation_field",
    )


def test_validate_official_answer_proves_not_found_and_control_non_execution() -> None:
    not_found = {
        "question_id": "Q-RELATION-NOT-FOUND",
        "question": "한국주택금융이 발행한 국내채권 3개 보여줘.",
        "retrieved_context": '{"citations":[],"reason":"조건에 맞는 근거를 찾지 못함"}',
        "think_trace": (
            '{"status":"not_found","intent":"search","product_families":["bond"],'
            '"execution_steps":["intent_router","knowledge_plan_validation",'
            '"relation_retrieval","claim_verifier","response_contract_validation"],'
            '"filters":[{"relation_type":"issued_by","query":"한국주택금융",'
            '"top_k":3}],"candidate_count":0}'
        ),
        "answer": "승인된 제공 데이터 관계에서 조건에 맞는 상품을 찾지 못했습니다.",
    }
    assert (
        validate_official_answer(
            200,
            not_found,
            question_id="Q-RELATION-NOT-FOUND",
            question="한국주택금융이 발행한 국내채권 3개 보여줘.",
            expected_trace_status="not_found",
            expected_intent="search",
            expected_families=("bond",),
            expected_empty_context=True,
            expected_execution_step="relation_retrieval",
            expected_relation_type="issued_by",
            expected_relation_query="한국주택금융",
            expected_relation_top_k=3,
            expected_product_count=0,
            expected_candidate_count=0,
        )
        == []
    )

    control = {
        "question_id": "Q-RELATION-CONTROL",
        "question": "미래에셋이 운용하는 ETF를 보여줘.",
        "retrieved_context": '{"citations":[],"reason":"조건 확인 필요"}',
        "think_trace": (
            '{"status":"clarification","intent":"clarify",'
            '"control_code":"ambiguous_product_family",'
            '"execution_steps":["intent_router","safe_control_response"]}'
        ),
        "answer": "상품군을 명시해 주세요.",
    }
    assert (
        validate_official_answer(
            200,
            control,
            question_id="Q-RELATION-CONTROL",
            question="미래에셋이 운용하는 ETF를 보여줘.",
            expected_control_code="ambiguous_product_family",
            expected_trace_status="clarification",
            expected_intent="clarify",
            expected_empty_context=True,
            require_safe_control_response=True,
            forbidden_execution_steps=("relation_retrieval", "sql", "deterministic_oracle"),
        )
        == []
    )
    control["think_trace"] = (
        '{"status":"clarification","intent":"clarify",'
        '"control_code":"ambiguous_product_family",'
        '"execution_steps":["intent_router","relation_retrieval"]}'
    )
    errors = validate_official_answer(
        200,
        control,
        question_id="Q-RELATION-CONTROL",
        question="미래에셋이 운용하는 ETF를 보여줘.",
        require_safe_control_response=True,
        forbidden_execution_steps=("relation_retrieval", "sql", "deterministic_oracle"),
    )
    assert "official safe control step is missing" in errors
    assert "official control response contains a forbidden execution step" in errors


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
