from __future__ import annotations

import hashlib
import re
import time
from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, model_validator


class FinalGenerationModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ClarificationGuidance(FinalGenerationModel):
    """Server-owned limits for one HCLX clarification sentence.

    The model may phrase the reverse question, but it cannot change the
    ambiguous slots or manufacture choices.  No conversation/session state is
    carried in this contract, so every request remains a singleton.
    """

    reason_code: str = Field(min_length=1, max_length=100)
    missing_slots: list[str] = Field(min_length=1, max_length=10)
    allowed_options: list[str] = Field(default_factory=list, max_length=12)
    required_terms: list[str] = Field(default_factory=list, max_length=8)
    fallback_question: str = Field(min_length=1, max_length=160)


class FinalAnswerContext(FinalGenerationModel):
    schema_version: Literal["1.0"] = "1.0"
    question: str = Field(min_length=1, max_length=2000)
    status: Literal[
        "success",
        "not_found",
        "cannot_verify",
        "clarification",
        "unsupported",
    ]
    intent: str = Field(min_length=1, max_length=32)
    product_families: list[str] = Field(max_length=4)
    candidate_count: int | None = Field(default=None, ge=0)
    evidence_count: int = Field(ge=0)
    citation_count: int = Field(ge=0)
    as_of_dates: list[str] = Field(max_length=32)
    deterministic_answer: str = Field(min_length=1, max_length=200_000)
    clarification_guidance: ClarificationGuidance | None = None

    @model_validator(mode="after")
    def validate_clarification_guidance(self) -> FinalAnswerContext:
        has_guidance = self.clarification_guidance is not None
        if (self.status == "clarification") != has_guidance:
            raise ValueError("clarification status and guidance must agree")
        return self

    @property
    def answer_body_sha256(self) -> str:
        return hashlib.sha256(self.deterministic_answer.encode("utf-8")).hexdigest()


class FinalAnswerDraft(FinalGenerationModel):
    schema_version: Literal["1.0"] = "1.0"
    lead: str = Field(min_length=1, max_length=160)
    answer_body_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class FinalAnswerProvider(Protocol):
    @property
    def provider_name(self) -> str: ...

    @property
    def model_name(self) -> str: ...

    def generate_final_answer(self, context: FinalAnswerContext) -> FinalAnswerDraft: ...


class FinalAnswerComposition(FinalGenerationModel):
    mode: Literal["llm_grounded"] = "llm_grounded"
    answer: str = Field(min_length=1)
    model_name: str = Field(min_length=1, max_length=128)
    generation_latency_ms: float = Field(ge=0)
    clarification_question: str | None = Field(default=None, max_length=160)
    clarification_fallback_used: bool = False


_LEADS = {
    "success": (
        "검증된 제공 데이터와 검색 조건을 기준으로 결과를 정리했습니다.",
        "검증된 상품 데이터와 근거를 바탕으로 조회 결과를 안내합니다.",
    ),
    "not_found": ("검증된 제공 데이터에서 요청 조건과 일치하는 결과를 찾지 못했습니다.",),
    "cannot_verify": (
        "공식 외부 근거 탐색을 끝까지 검증하지 못해 존재 여부를 단정할 수 없습니다.",
    ),
    "clarification": ("안전하고 정확한 조회를 위해 질문의 조건을 더 확인해야 합니다.",),
    "unsupported": (
        "제공 데이터와 허용된 금융상품 조회 범위를 벗어나 요청을 실행하지 않았습니다.",
    ),
}


def allowed_final_answer_leads(context: FinalAnswerContext) -> tuple[str, ...]:
    return _LEADS[context.status]


_RETURN_SIGNAL = re.compile(r"(?:수익률|수익|성과|성적)", re.IGNORECASE)
_RETURN_PERIOD = re.compile(
    r"(?:1\s*일|하루|당일|1\s*주|일주일|1\s*개월|한\s*달|3\s*개월|분기|"
    r"6\s*개월|반기|1\s*년|12\s*개월|연초\s*이후|YTD)",
    re.IGNORECASE,
)
_SUBJECTIVE_SIGNAL = re.compile(
    r"(?:좋은|괜찮은|알짜|안전한|안정적인|부담\s*(?:없는|적은)|효율(?:이)?\s*높은)",
    re.IGNORECASE,
)


def build_clarification_guidance(
    *,
    question: str,
    reason_code: str,
    required_fields: list[str],
    product_families: list[str],
    existing_options: list[str] | None = None,
) -> ClarificationGuidance:
    """Derive a closed clarification contract from the current question only."""

    normalized = " ".join(question.split())
    families = set(product_families)
    options = list(dict.fromkeys(existing_options or []))
    slots = list(dict.fromkeys(required_fields)) or ["query_condition"]
    required_terms: list[str] = []

    if _RETURN_SIGNAL.search(normalized):
        required_terms = ["수익률"]
        is_bond_only = families == {"bond"}
        is_overseas_only = families == {"overseas_etp"}
        if is_bond_only and not re.search(
            r"(?:매수\s*수익률|세후\s*수익률|표면\s*(?:이율|금리|이자율)|민평)",
            normalized,
        ):
            slots = ["return_basis"]
            options = ["매수수익률", "세후수익률", "표면이율"]
            fallback = (
                "어떤 채권 수익률을 기준으로 볼까요? "
                "매수수익률, 세후수익률, 표면이율 중에서 선택해 주시겠어요?"
            )
        elif not _RETURN_PERIOD.search(normalized) and not is_overseas_only:
            slots = ["return_period"]
            options = ["1개월", "3개월", "1년"]
            fallback = "어느 기간의 수익률을 기준으로 볼까요? " + (
                "1개월, 3개월, 1년 중에서 선택해 주시겠어요?"
            )
        elif _SUBJECTIVE_SIGNAL.search(normalized):
            slots = ["ranking_rule"]
            options = ["높은 순", "최소 기준값 이상"]
            fallback = "수익률을 높은 순으로 볼까요, 아니면 최소 기준값 이상인 상품을 찾을까요?"
        else:
            slots = ["return_condition"]
            fallback = "수익률의 기간과 비교 기준을 어떻게 적용할지 구체적으로 알려주시겠어요?"
    elif reason_code == "evidence_period_after_cutoff":
        # The question dates its evidence after the approved cutoff; no
        # official source can answer it, so ask for a period inside the range.
        slots = ["evidence_period"]
        required_terms = ["기준일"]
        fallback = (
            "요청하신 시점이 공식 근거 마감일 이후라 확인할 수 없는데, "
            "마감일 이전의 어느 기준일로 확인할까요?"
        )
    elif reason_code == "ambiguous_product_family" or "product_family" in slots:
        slots = ["product_family"]
        options = ["국내채권", "국내 ETF·ETN", "해외 ETF·ETN", "공모펀드"]
        required_terms = ["상품군"]
        fallback = (
            "어느 상품군을 조회할까요? 국내채권, 국내 ETF·ETN, 해외 ETF·ETN, "
            "공모펀드 중에서 선택해 주시겠어요?"
        )
    elif "product_identity" in slots or reason_code in {
        "missing_product_identity",
        "unresolved_product_identity_span",
    }:
        slots = ["product_identity"]
        required_terms = ["상품"]
        fallback = "확인할 상품명이나 상품코드를 정확히 알려주시겠어요?"
    elif "relation_entity" in slots or "knowledge_subject" in slots:
        required_terms = ["대상"]
        fallback = "확인할 대상 상품이나 기업과 관계 조건을 구체적으로 알려주시겠어요?"
    elif "relation_type" in slots or "knowledge_relation" in slots:
        required_terms = ["관계"]
        fallback = "구성종목, 자회사, 테마, 동향 중 어떤 관계를 확인할까요?"
        options = ["구성종목", "자회사", "테마", "동향"]
    elif reason_code == "subjective_condition" or _SUBJECTIVE_SIGNAL.search(normalized):
        slots = ["objective_criterion"]
        required_terms = ["기준"]
        fallback = "어떤 수치나 조건을 기준으로 상품을 찾을지 구체적으로 알려주시겠어요?"
    else:
        fallback = "정확한 조회를 위해 어떤 조건을 기준으로 볼지 구체적으로 알려주시겠어요?"

    return ClarificationGuidance(
        reason_code=reason_code,
        missing_slots=slots,
        allowed_options=options,
        required_terms=required_terms,
        fallback_question=fallback,
    )


def _validated_clarification_question(
    context: FinalAnswerContext,
    generated: str,
) -> str:
    from finance_agent_core.agent.providers.hyperclova import HyperClovaXResponseError

    guidance = context.clarification_guidance
    if guidance is None:
        raise HyperClovaXResponseError("clarification guidance is missing")
    question = generated.strip()
    if "\n" in question or "\r" in question or not question.endswith("?"):
        raise HyperClovaXResponseError("HyperCLOVA X clarification must be one question")
    if re.search(r"[.!。]", question[:-1]) or re.search(
        r"(?:추천|찾았습니다|확인했습니다|결과는|(?:입니다|있습니다|높습니다|낮습니다))",
        question,
    ):
        raise HyperClovaXResponseError("HyperCLOVA X clarification contains an answer claim")
    if any(token in question for token in ("```", "http://", "https://")):
        raise HyperClovaXResponseError("HyperCLOVA X clarification contains unapproved content")
    if any(term not in question for term in guidance.required_terms):
        raise HyperClovaXResponseError("HyperCLOVA X clarification omitted a required concept")
    if any(option not in question for option in guidance.allowed_options):
        raise HyperClovaXResponseError("HyperCLOVA X clarification changed the allowed choices")
    return question


def compose_final_answer(
    context: FinalAnswerContext,
    provider: FinalAnswerProvider,
) -> FinalAnswerComposition:
    """Require HCX generation; never fall back on the official response path."""

    started = time.perf_counter()
    draft = provider.generate_final_answer(context)
    latency_ms = round((time.perf_counter() - started) * 1000, 3)
    if draft.answer_body_sha256 != context.answer_body_sha256:
        from finance_agent_core.agent.providers.hyperclova import HyperClovaXResponseError

        raise HyperClovaXResponseError("HyperCLOVA X changed the grounded answer body reference")
    if context.status == "clarification":
        from finance_agent_core.agent.providers.hyperclova import HyperClovaXResponseError

        fallback_used = False
        try:
            clarification_question = _validated_clarification_question(context, draft.lead)
        except HyperClovaXResponseError:
            # HCX was called and answered, but its sentence broke the closed
            # clarification contract (two sentences, a claim, a changed option).
            # The server-owned fallback question carries the same slots and
            # options, so asking it beats failing the whole request with 503.
            guidance = context.clarification_guidance
            if guidance is None:
                raise
            clarification_question = guidance.fallback_question
            fallback_used = True
        return FinalAnswerComposition(
            answer=clarification_question,
            model_name=provider.model_name,
            generation_latency_ms=latency_ms,
            clarification_question=clarification_question,
            clarification_fallback_used=fallback_used,
        )
    if draft.lead not in allowed_final_answer_leads(context):
        from finance_agent_core.agent.providers.hyperclova import HyperClovaXResponseError

        raise HyperClovaXResponseError("HyperCLOVA X returned an unapproved answer lead")
    return FinalAnswerComposition(
        answer=f"{draft.lead}\n{context.deterministic_answer}",
        model_name=provider.model_name,
        generation_latency_ms=latency_ms,
    )


__all__ = [
    "ClarificationGuidance",
    "FinalAnswerComposition",
    "FinalAnswerContext",
    "FinalAnswerDraft",
    "FinalAnswerProvider",
    "allowed_final_answer_leads",
    "build_clarification_guidance",
    "compose_final_answer",
]
