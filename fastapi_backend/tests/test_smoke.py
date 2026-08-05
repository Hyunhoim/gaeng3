from scripts.smoke import SmokeCase, validate_answer, validate_health


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
