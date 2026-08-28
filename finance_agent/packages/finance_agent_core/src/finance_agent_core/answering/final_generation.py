from __future__ import annotations

import hashlib
import time
from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field


class FinalGenerationModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


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


_LEADS = {
    "success": (
        "검증된 제공 데이터와 검색 조건을 기준으로 결과를 정리했습니다.",
        "검증된 상품 데이터와 근거를 바탕으로 조회 결과를 안내합니다.",
    ),
    "not_found": (
        "검증된 제공 데이터에서 요청 조건과 일치하는 결과를 찾지 못했습니다.",
    ),
    "cannot_verify": (
        "공식 외부 근거 탐색을 끝까지 검증하지 못해 존재 여부를 단정할 수 없습니다.",
    ),
    "clarification": (
        "안전하고 정확한 조회를 위해 질문의 조건을 더 확인해야 합니다.",
    ),
    "unsupported": (
        "제공 데이터와 허용된 금융상품 조회 범위를 벗어나 요청을 실행하지 않았습니다.",
    ),
}


def allowed_final_answer_leads(context: FinalAnswerContext) -> tuple[str, ...]:
    return _LEADS[context.status]


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
    if draft.lead not in allowed_final_answer_leads(context):
        from finance_agent_core.agent.providers.hyperclova import HyperClovaXResponseError

        raise HyperClovaXResponseError("HyperCLOVA X returned an unapproved answer lead")
    return FinalAnswerComposition(
        answer=f"{draft.lead}\n{context.deterministic_answer}",
        model_name=provider.model_name,
        generation_latency_ms=latency_ms,
    )


__all__ = [
    "FinalAnswerComposition",
    "FinalAnswerContext",
    "FinalAnswerDraft",
    "FinalAnswerProvider",
    "allowed_final_answer_leads",
    "compose_final_answer",
]
