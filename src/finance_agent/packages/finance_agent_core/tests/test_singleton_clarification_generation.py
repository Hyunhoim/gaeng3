from __future__ import annotations

import pytest

from finance_agent_core.agent import RoutedFinanceAgent
from finance_agent_core.agent.backend_adapter import (
    _finalize_with_official_hclx,
    routed_result_to_backend,
)
from finance_agent_core.agent.providers.hyperclova import HyperClovaXResponseError
from finance_agent_core.agent.router import IntentRouter
from finance_agent_core.answering.final_generation import (
    FinalAnswerContext,
    FinalAnswerDraft,
    _validated_clarification_question,
    build_clarification_guidance,
    compose_final_answer,
)


class _NoGuidanceContext:
    clarification_guidance = None


class _ClarificationProvider:
    provider_name = "fake-hclx-clarification"
    model_name = "HCX-007"

    def __init__(self, question: str) -> None:
        self.question = question
        self.contexts: list[FinalAnswerContext] = []

    def generate_final_answer(self, context: FinalAnswerContext) -> FinalAnswerDraft:
        self.contexts.append(context)
        return FinalAnswerDraft(
            lead=self.question,
            answer_body_sha256=context.answer_body_sha256,
        )


def _context(question: str = "수익률 좋은 ETF 알려줘") -> FinalAnswerContext:
    guidance = build_clarification_guidance(
        question=question,
        reason_code="subjective_condition",
        required_fields=["objective_threshold"],
        product_families=["domestic_etp", "overseas_etp"],
    )
    return FinalAnswerContext(
        question=question,
        status="clarification",
        intent="clarify",
        product_families=["domestic_etp", "overseas_etp"],
        candidate_count=None,
        evidence_count=0,
        citation_count=0,
        as_of_dates=[],
        deterministic_answer="판단 기준이나 임계값이 명시되지 않은 주관적 조건",
        clarification_guidance=guidance,
    )


def test_return_clarification_is_a_short_hclx_generated_reverse_question() -> None:
    context = _context()
    provider = _ClarificationProvider(
        "어느 기간의 수익률을 기준으로 볼까요? 1개월, 3개월, 1년 중에서 선택해 주시겠어요?"
    )

    composition = compose_final_answer(context, provider)

    assert composition.answer == provider.question
    assert composition.clarification_question == provider.question
    assert context.clarification_guidance is not None
    assert context.clarification_guidance.missing_slots == ["return_period"]
    assert context.clarification_guidance.allowed_options == ["1개월", "3개월", "1년"]
    assert len(provider.contexts) == 1


@pytest.mark.parametrize(
    "generated",
    [
        "1년 수익률이 가장 높은 ETF를 추천합니다.",
        "어느 기간의 수익률을 볼까요?",
        "1개월, 3개월, 1년은 https://example.com 에서 고르세요?",
        "1개월, 3개월, 1년 중에서 선택해 주세요?\n다음 질문을 기다립니다.",
        (
            "KODEX 상품을 추천합니다. 어느 기간의 수익률을 볼까요? "
            "1개월, 3개월, 1년 중에서 선택해 주시겠어요?"
        ),
    ],
)
def test_clarification_verifier_replaces_answering_missing_or_unapproved_content(
    generated: str,
) -> None:
    # HCX was called, but its sentence broke the closed contract. The request
    # must still end as a clarification (never a 503), and the generated text
    # must not leak: the server-owned fallback question is asked instead.
    context = _context()
    composition = compose_final_answer(context, _ClarificationProvider(generated))

    assert context.clarification_guidance is not None
    assert composition.clarification_fallback_used is True
    assert composition.answer == context.clarification_guidance.fallback_question
    assert composition.clarification_question == composition.answer
    assert generated not in composition.answer
    assert composition.answer.endswith("?")
    for option in context.clarification_guidance.allowed_options:
        assert option in composition.answer


def test_clarification_without_guidance_still_fails_closed() -> None:
    with pytest.raises(HyperClovaXResponseError):
        _validated_clarification_question(
            _NoGuidanceContext(),  # type: ignore[arg-type]
            "어느 기간의 수익률을 볼까요?",
        )


def test_clarification_contract_carries_no_conversation_state() -> None:
    first = _context("수익률 좋은 ETF 알려줘")
    second = _context("수익률 괜찮은 ETF를 보여줘")

    assert "history" not in first.model_dump()
    assert "session" not in first.model_dump()
    assert first.question != second.question
    assert first.clarification_guidance == second.clarification_guidance


def test_backend_keeps_clarify_control_state_and_exports_generated_question() -> None:
    question = "수익률 좋은 ETF 알려줘"
    generated = "어느 기간의 수익률을 기준으로 볼까요? 1개월, 3개월, 1년 중에서 선택해 주시겠어요?"
    response = routed_result_to_backend(
        RoutedFinanceAgent({}).answer(question, "singleton-clarification-backend")
    )

    class _Service:
        official_answer_provider = _ClarificationProvider(generated)
        require_official_hclx = True

    finalized = _finalize_with_official_hclx(_Service(), question, response)

    assert finalized.status.value == "clarification"
    assert finalized.answer == generated
    assert finalized.answer_mode.value == "control"
    assert finalized.provider_model is None
    assert finalized.query_plan is None
    assert finalized.products == []
    assert finalized.clarification is not None
    assert finalized.clarification.message == generated
    assert finalized.clarification.required_fields == ["return_period"]
    assert finalized.clarification.options == ["1개월", "3개월", "1년"]


@pytest.mark.parametrize(
    ("question", "expected_disposition"),
    [
        ("수익률 좋은 ETF 알려줘", "clarify"),
        ("최근 1년 수익률이 높은 국내 ETF 3개 알려줘", "execute"),
        ("1년 수익률이 18.17 이상인 국내 ETF 알려줘", "execute"),
    ],
)
def test_return_ambiguity_changes_only_the_control_response_not_clear_execution(
    question: str,
    expected_disposition: str,
) -> None:
    decision = IntentRouter().route(question, "singleton-return-contract")

    assert decision.disposition.value == expected_disposition


@pytest.mark.parametrize(
    (
        "question",
        "reason_code",
        "required_fields",
        "families",
        "expected_slots",
        "expected_options",
    ),
    [
        (
            "해외 ETF 상세 정보를 알려줘",
            "missing_product_identity",
            ["product_identity"],
            ["overseas_etp"],
            ["product_identity"],
            [],
        ),
        (
            "AUM이 큰 상품을 알려줘",
            "ambiguous_product_family",
            ["product_family"],
            [],
            ["product_family"],
            ["국내채권", "국내 ETF·ETN", "해외 ETF·ETN", "공모펀드"],
        ),
        (
            "안전한 국내 ETF를 알려줘",
            "subjective_condition",
            ["objective_threshold"],
            ["domestic_etp"],
            ["objective_criterion"],
            [],
        ),
        (
            "편입 관계가 있는 ETF를 알려줘",
            "missing_relation_entity",
            ["relation_entity"],
            ["domestic_etp"],
            ["relation_entity"],
            [],
        ),
        (
            "이 상품의 외부 관계를 알려줘",
            "multiple_relation_predicates",
            ["relation_type"],
            ["domestic_etp"],
            ["relation_type"],
            ["구성종목", "자회사", "테마", "동향"],
        ),
        (
            "조건이 모호한 국내 ETF를 알려줘",
            "semantic_coverage_incomplete",
            ["query_condition"],
            ["domestic_etp"],
            ["query_condition"],
            [],
        ),
    ],
)
def test_representative_clarification_types_accept_their_closed_fallback_question(
    question: str,
    reason_code: str,
    required_fields: list[str],
    families: list[str],
    expected_slots: list[str],
    expected_options: list[str],
) -> None:
    guidance = build_clarification_guidance(
        question=question,
        reason_code=reason_code,
        required_fields=required_fields,
        product_families=families,
    )
    context = FinalAnswerContext(
        question=question,
        status="clarification",
        intent="clarify",
        product_families=families,
        candidate_count=None,
        evidence_count=0,
        citation_count=0,
        as_of_dates=[],
        deterministic_answer="서버가 실행을 보류했습니다.",
        clarification_guidance=guidance,
    )

    composition = compose_final_answer(
        context,
        _ClarificationProvider(guidance.fallback_question),
    )

    assert composition.answer == guidance.fallback_question
    assert guidance.missing_slots == expected_slots
    assert guidance.allowed_options == expected_options
