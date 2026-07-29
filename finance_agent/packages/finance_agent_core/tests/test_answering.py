from pathlib import Path

from finance_agent_core.agent import FinanceAgent
from finance_agent_core.agent.providers import (
    DomesticMockProvider,
    LocalTestSettings,
    domestic_vertical_slice_plan,
)
from finance_agent_core.answering import (
    AnswerVerifier,
    ExpectedGroundedAnswerProvider,
    GroundedAnswerDraft,
    LocalGroundedAnswerProvider,
    ProductAnswerDraft,
    build_grounded_answer_context,
    compose_grounded_answer,
)
from finance_agent_core.domain import DatabaseManifest, NormalizedDomesticEtpRecord
from finance_agent_core.execution import (
    ResultVerifier,
    SQLiteOracle,
    build_product_evidence,
)
from finance_agent_core.storage import connect_read_only, load_all_records


def _verified_domestic_context(
    database: Path,
):
    plan = domestic_vertical_slice_plan("answer-001")
    executed = SQLiteOracle(database).execute(plan)
    with connect_read_only(database) as connection:
        universe = load_all_records(connection)
    verified = ResultVerifier().verify(plan, executed, universe)
    products = build_product_evidence(plan, verified)
    context = build_grounded_answer_context(
        question="미국 주식형 국내 ETF를 수익률 순으로 보여줘",
        plan=plan,
        verified=verified,
        products=products,
    )
    return plan, verified, products, context


def test_grounded_answer_compiles_only_verified_values(
    domestic_sample_database: tuple[
        Path,
        list[NormalizedDomesticEtpRecord],
        DatabaseManifest,
    ],
) -> None:
    path, _, _ = domestic_sample_database
    response, composition = FinanceAgent(
        path,
        DomesticMockProvider(),
        answer_provider=ExpectedGroundedAnswerProvider(),
    ).answer_with_composition(
        "미국 주식형 국내 ETF 중 연금 거래 가능한 상품을 수익률 순으로 보여줘",
        "answer-agent-001",
    )

    assert composition is not None
    assert composition.mode == "llm_grounded"
    assert composition.verification.passed
    assert composition.draft is not None
    assert all(
        not any(character.isdigit() for character in product.explanation)
        for product in composition.draft.products
    )
    assert "1개월 수익률 60%" in response.answer
    assert "PREF01N001 원본 행 7" in response.answer
    assert "기준일 2026-06-15" in response.answer
    assert "적용 조건:" in response.answer


def test_answer_verifier_rejects_numeric_model_prose_and_falls_back(
    domestic_sample_database: tuple[
        Path,
        list[NormalizedDomesticEtpRecord],
        DatabaseManifest,
    ],
) -> None:
    path, _, _ = domestic_sample_database
    plan, verified, products, context = _verified_domestic_context(path)
    expected = ExpectedGroundedAnswerProvider().generate_grounded_answer(context)
    first = expected.products[0].model_copy(
        update={"explanation": "수익률이 60%라서 선택했습니다."}
    )
    tampered = expected.model_copy(update={"products": [first, *expected.products[1:]]})

    class TamperedProvider:
        provider_name = "tampered"
        model_name = "tampered-model"

        def generate_grounded_answer(self, _context):
            return tampered

    composition = compose_grounded_answer(
        question=context.question,
        plan=plan,
        verified=verified,
        products=products,
        provider=TamperedProvider(),
    )

    assert composition.mode == "deterministic_fallback"
    assert not composition.verification.passed
    assert not composition.verification.checks["prose_numbers_are_grounded"]
    assert composition.answer == context.deterministic_answer
    assert "60%라서" not in composition.answer


def test_answer_verifier_allows_bond_buy_field_names_but_rejects_advice(
    domestic_sample_database: tuple[
        Path,
        list[NormalizedDomesticEtpRecord],
        DatabaseManifest,
    ],
) -> None:
    path, _, _ = domestic_sample_database
    _, _, _, context = _verified_domestic_context(path)
    expected = ExpectedGroundedAnswerProvider().generate_grounded_answer(context)
    field_name_explanation = expected.products[0].model_copy(
        update={"explanation": "매수수익률과 매수 가능 수량이 검색 근거로 사용되었습니다."}
    )
    field_name_draft = expected.model_copy(
        update={"products": [field_name_explanation, *expected.products[1:]]}
    )

    field_name_verification = AnswerVerifier().verify(context, field_name_draft)

    assert field_name_verification.checks["prose_has_no_advice_or_forecast"]

    advice = expected.products[0].model_copy(update={"explanation": "이 상품을 지금 매수하세요."})
    advice_draft = expected.model_copy(update={"products": [advice, *expected.products[1:]]})

    advice_verification = AnswerVerifier().verify(context, advice_draft)

    assert not advice_verification.checks["prose_has_no_advice_or_forecast"]


def test_answer_provider_error_fails_closed(
    domestic_sample_database: tuple[
        Path,
        list[NormalizedDomesticEtpRecord],
        DatabaseManifest,
    ],
) -> None:
    path, _, _ = domestic_sample_database
    plan, verified, products, context = _verified_domestic_context(path)

    class BrokenProvider:
        provider_name = "broken"
        model_name = "broken-model"

        def generate_grounded_answer(self, _context):
            raise RuntimeError("generation failed")

    composition = compose_grounded_answer(
        question=context.question,
        plan=plan,
        verified=verified,
        products=products,
        provider=BrokenProvider(),
    )

    assert composition.mode == "deterministic_fallback"
    assert composition.answer == context.deterministic_answer
    assert composition.verification.violations == ["RuntimeError: generation failed"]


def test_local_grounded_provider_uses_structured_output(
    domestic_sample_database: tuple[
        Path,
        list[NormalizedDomesticEtpRecord],
        DatabaseManifest,
    ],
    monkeypatch,
) -> None:
    path, _, _ = domestic_sample_database
    _, _, _, context = _verified_domestic_context(path)
    settings = LocalTestSettings.from_environment(
        {
            "FINANCE_AGENT_LLM_MODE": "local_test",
            "ENABLE_NON_HCX_TEST_LLM": "1",
            "LLM_PROVIDER": "local_test",
            "LOCAL_TEST_LLM_BASE_URL": "http://127.0.0.1:18000/v1",
            "LOCAL_TEST_LLM_MODEL": "qwen3-local-test",
        }
    )
    provider = LocalGroundedAnswerProvider(settings)
    expected = ExpectedGroundedAnswerProvider().generate_grounded_answer(context)
    captured: dict[str, object] = {}

    def fake_request(path: str, payload: dict[str, object]):
        captured["path"] = path
        captured["payload"] = payload
        return {"choices": [{"message": {"content": expected.model_dump_json()}}]}

    monkeypatch.setattr(provider._client, "_request_json", fake_request)
    draft = provider.generate_grounded_answer(context)

    assert draft == expected
    assert captured["path"] == "chat/completions"
    payload = captured["payload"]
    assert isinstance(payload, dict)
    assert payload["response_format"]["type"] == "json_schema"
    assert payload["response_format"]["json_schema"]["strict"]
    assert "실제 값, 날짜, 개수" in payload["messages"][0]["content"]
    system_prompt = payload["messages"][0]["content"]
    assert context.products[0].product_id not in system_prompt
    assert context.products[0].product_name not in system_prompt
    assert context.products[0].ticker not in system_prompt
    assert context.products[0].fields[3].normalized_value not in system_prompt
    assert context.products[0].fields[3].as_of.isoformat() not in system_prompt
    schema = payload["response_format"]["json_schema"]["schema"]
    assert schema["properties"]["lead"]["enum"]
    assert (
        schema["properties"]["products"]["prefixItems"][0]["properties"]["result_ref"]["const"]
        == "result_1"
    )


def test_draft_contract_rejects_duplicate_evidence_fields() -> None:
    try:
        ProductAnswerDraft(
            result_ref="result_1",
            evidence_fields=["aum", "aum"],
            explanation="근거를 사용했습니다.",
        )
    except ValueError as error:
        assert "evidence_fields must be unique" in str(error)
    else:
        raise AssertionError("duplicate evidence fields must be rejected")


def test_draft_contract_rejects_duplicate_products() -> None:
    product = ProductAnswerDraft(
        result_ref="result_1",
        evidence_fields=["aum"],
        explanation="근거를 사용했습니다.",
    )
    try:
        GroundedAnswerDraft(
            lead="근거 기반 결과입니다.",
            products=[product, product],
            acknowledged_warning_codes=[],
        )
    except ValueError as error:
        assert "draft result references must be unique" in str(error)
    else:
        raise AssertionError("duplicate products must be rejected")
