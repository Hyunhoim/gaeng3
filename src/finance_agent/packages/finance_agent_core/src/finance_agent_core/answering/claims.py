from __future__ import annotations

import time
from typing import Annotated, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, model_validator

from finance_agent_core.contracts.knowledge import (
    KnowledgeQueryPlan,
    RelationKnowledgeOperation,
)
from finance_agent_core.contracts.queryplan import ProductFamily
from finance_agent_core.retrieval.models import (
    DocumentEvidence,
    DocumentSearchResponse,
)
from finance_agent_core.retrieval.relations import (
    RelationEvidence,
    RelationSearchResponse,
    RelationType,
)


class ClaimModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class RelationClaimDraft(ClaimModel):
    kind: Literal["relation"] = "relation"
    result_ref: str = Field(pattern=r"^result_(?:[1-9]|1[0-9]|20)$")
    evidence_id: str = Field(min_length=1, max_length=200)
    relation_type: RelationType
    product_family: ProductFamily
    product_id: str = Field(min_length=1, max_length=128)
    product_name: str = Field(min_length=1, max_length=500)
    ticker: str | None = Field(default=None, max_length=100)
    entity_id: str = Field(min_length=1, max_length=128)
    entity_label: str = Field(min_length=1, max_length=500)


class DocumentClaimDraft(ClaimModel):
    kind: Literal["document"] = "document"
    result_ref: str = Field(pattern=r"^result_(?:[1-9]|1[0-9]|20)$")
    evidence_id: str = Field(min_length=1, max_length=200)
    document_id: str = Field(min_length=1, max_length=128)
    title: str = Field(min_length=1, max_length=500)
    excerpt: str = Field(min_length=1, max_length=2000)


type KnowledgeClaimDraft = Annotated[
    RelationClaimDraft | DocumentClaimDraft,
    Field(discriminator="kind"),
]


class KnowledgeAnswerDraft(ClaimModel):
    schema_version: Literal["1.0"] = "1.0"
    claims: tuple[KnowledgeClaimDraft, ...] = Field(min_length=1, max_length=20)

    @model_validator(mode="after")
    def validate_unique_refs(self) -> KnowledgeAnswerDraft:
        result_refs = [item.result_ref for item in self.claims]
        evidence_ids = [item.evidence_id for item in self.claims]
        if len(result_refs) != len(set(result_refs)):
            raise ValueError("claim result references must be unique")
        if len(evidence_ids) != len(set(evidence_ids)):
            raise ValueError("claim evidence IDs must be unique")
        return self


class KnowledgeAnswerContext(ClaimModel):
    schema_version: Literal["1.0"] = "1.0"
    plan: KnowledgeQueryPlan
    relation_response: RelationSearchResponse | None = None
    document_response: DocumentSearchResponse | None = None
    deterministic_answer: str = Field(min_length=1)

    @model_validator(mode="after")
    def validate_response_kind(self) -> KnowledgeAnswerContext:
        if isinstance(self.plan.operation, RelationKnowledgeOperation):
            if self.relation_response is None or self.document_response is not None:
                raise ValueError("relation plan requires only a relation response")
            if self.relation_response.query != self.plan.operation.query:
                raise ValueError("relation response query differs from the plan")
        elif self.document_response is None or self.relation_response is not None:
            raise ValueError("document plan requires only a document response")
        elif self.document_response.query != self.plan.operation.query:
            raise ValueError("document response query differs from the plan")
        return self

    @property
    def evidence_count(self) -> int:
        if self.relation_response is not None:
            return len(self.relation_response.evidence)
        assert self.document_response is not None
        return len(self.document_response.evidence)


class ClaimVerification(ClaimModel):
    passed: bool
    checks: dict[str, bool]
    violations: tuple[str, ...]


class KnowledgeAnswerComposition(ClaimModel):
    mode: Literal["deterministic", "structured_grounded", "deterministic_fallback"]
    answer: str = Field(min_length=1)
    provider_name: str | None = Field(default=None, min_length=1, max_length=100)
    model_name: str | None = Field(default=None, min_length=1, max_length=128)
    generation_latency_ms: float = Field(ge=0)
    draft: KnowledgeAnswerDraft | None
    verification: ClaimVerification

    @model_validator(mode="after")
    def validate_mode(self) -> KnowledgeAnswerComposition:
        if (self.provider_name is None) != (self.model_name is None):
            raise ValueError("provider and model must be both present or both absent")
        if self.mode == "structured_grounded":
            if self.draft is None or not self.verification.passed:
                raise ValueError("structured grounded answer requires a verified draft")
        elif self.mode == "deterministic":
            if self.draft is not None or not self.verification.passed:
                raise ValueError("deterministic answer cannot carry a failed draft")
        elif self.verification.passed:
            raise ValueError("deterministic fallback requires a failed verification")
        return self


class KnowledgeClaimProvider(Protocol):
    @property
    def provider_name(self) -> str: ...

    @property
    def model_name(self) -> str: ...

    def generate_claims(self, context: KnowledgeAnswerContext) -> KnowledgeAnswerDraft: ...


_RELATION_LABELS = {
    RelationType.ISSUED_BY: "발행사",
    RelationType.MANAGED_BY: "운용사",
    RelationType.TRACKS_INDEX: "기초지수",
    RelationType.CLASSIFIED_AS_ASSET: "자산유형",
    RelationType.INVESTS_IN_REGION: "투자지역",
}


def _relation_claim(index: int, evidence: RelationEvidence) -> RelationClaimDraft:
    return RelationClaimDraft(
        result_ref=f"result_{index}",
        evidence_id=evidence.evidence_id,
        relation_type=evidence.relation_type,
        product_family=evidence.product_family,
        product_id=evidence.product_id,
        product_name=evidence.product_name,
        ticker=evidence.ticker,
        entity_id=evidence.entity_id,
        entity_label=evidence.entity_label,
    )


def _document_claim(index: int, evidence: DocumentEvidence) -> DocumentClaimDraft:
    return DocumentClaimDraft(
        result_ref=f"result_{index}",
        evidence_id=evidence.evidence_id,
        document_id=evidence.document_id,
        title=evidence.title,
        excerpt=evidence.text,
    )


def expected_knowledge_answer_draft(context: KnowledgeAnswerContext) -> KnowledgeAnswerDraft:
    if context.relation_response is not None:
        claims: tuple[KnowledgeClaimDraft, ...] = tuple(
            _relation_claim(index, evidence)
            for index, evidence in enumerate(context.relation_response.evidence, start=1)
        )
    else:
        assert context.document_response is not None
        claims = tuple(
            _document_claim(index, evidence)
            for index, evidence in enumerate(context.document_response.evidence, start=1)
        )
    return KnowledgeAnswerDraft(claims=claims)


def _relation_answer(response: RelationSearchResponse) -> str:
    if response.status == "not_found":
        return (
            "승인된 제공 데이터 관계에서 조건에 맞는 상품을 찾지 못했습니다. "
            "관계명이나 상품군을 확인해 주세요."
        )
    lines = [f"승인된 제공 데이터 관계에서 {len(response.evidence)}건을 찾았습니다."]
    for index, evidence in enumerate(response.evidence, start=1):
        ticker = f" · {evidence.ticker}" if evidence.ticker else ""
        columns = "/".join(evidence.source_columns)
        lines.append(
            f"{index}. {evidence.product_name} ({evidence.product_id}{ticker}) — "
            f"{_RELATION_LABELS[evidence.relation_type]}: {evidence.entity_label} "
            f"[근거: {evidence.source_id} 원본 행 {evidence.source_row}, {columns}, "
            f"기준일 {evidence.as_of.isoformat()}, evidence {evidence.evidence_id}]"
        )
    lines.append(
        "관계는 제공 데이터의 표기를 그대로 조회한 결과이며 "
        "투자 추천이나 인과관계를 뜻하지 않습니다."
    )
    return "\n".join(lines)


def _document_answer(response: DocumentSearchResponse) -> str:
    if response.status == "not_found":
        return "승인된 문서에서 답변 근거를 찾지 못했습니다. 문서 범위나 검색어를 확인해 주세요."
    lines = [f"승인된 문서에서 근거 {len(response.evidence)}건을 찾았습니다."]
    for index, evidence in enumerate(response.evidence, start=1):
        lines.append(
            f"{index}. {evidence.title}: {evidence.text} "
            f"[근거: {evidence.source_uri}, 문서 {evidence.document_id}, "
            f"chunk {evidence.chunk_ordinal}, 기준일 {evidence.as_of.isoformat()}, "
            f"evidence {evidence.evidence_id}]"
        )
    return "\n".join(lines)


def build_knowledge_answer_context(
    plan: KnowledgeQueryPlan,
    response: RelationSearchResponse | DocumentSearchResponse,
) -> KnowledgeAnswerContext:
    if isinstance(response, RelationSearchResponse):
        return KnowledgeAnswerContext(
            plan=plan,
            relation_response=response,
            deterministic_answer=_relation_answer(response),
        )
    return KnowledgeAnswerContext(
        plan=plan,
        document_response=response,
        deterministic_answer=_document_answer(response),
    )


class KnowledgeClaimVerifier:
    """Accept only an exact structured restatement of retrieved evidence."""

    def verify(
        self,
        context: KnowledgeAnswerContext,
        draft: KnowledgeAnswerDraft,
    ) -> ClaimVerification:
        checks = {
            "claim_count": len(draft.claims) == context.evidence_count,
            "claim_kind": True,
            "claim_order": True,
            "claim_values": True,
            "evidence_refs": True,
        }
        violations: list[str] = []
        if not checks["claim_count"]:
            violations.append("claim count differs from retrieved evidence")
        try:
            expected = expected_knowledge_answer_draft(context)
        except ValueError:
            checks["claim_values"] = False
            violations.append("retrieved evidence cannot form a claim draft")
            return ClaimVerification(
                passed=False,
                checks=checks,
                violations=tuple(violations),
            )
        for index, claim in enumerate(draft.claims):
            if index >= len(expected.claims):
                checks["claim_order"] = False
                checks["claim_values"] = False
                checks["evidence_refs"] = False
                continue
            expected_claim = expected.claims[index]
            if claim.kind != expected_claim.kind:
                checks["claim_kind"] = False
            if claim.result_ref != expected_claim.result_ref:
                checks["claim_order"] = False
            if claim.evidence_id != expected_claim.evidence_id:
                checks["evidence_refs"] = False
            if claim != expected_claim:
                checks["claim_values"] = False
        for name, passed in checks.items():
            if not passed and not any(name in item for item in violations):
                violations.append(f"{name} verification failed")
        return ClaimVerification(
            passed=all(checks.values()),
            checks=checks,
            violations=tuple(violations),
        )


def _provider_failure(message: str) -> ClaimVerification:
    return ClaimVerification(
        passed=False,
        checks={"provider_completed": False},
        violations=(message,),
    )


def compose_knowledge_answer(
    context: KnowledgeAnswerContext,
    provider: KnowledgeClaimProvider | None = None,
) -> KnowledgeAnswerComposition:
    if context.evidence_count == 0 or provider is None:
        provider_used = provider if context.evidence_count > 0 else None
        return KnowledgeAnswerComposition(
            mode="deterministic",
            answer=context.deterministic_answer,
            provider_name=None if provider_used is None else provider_used.provider_name,
            model_name=None if provider_used is None else provider_used.model_name,
            generation_latency_ms=0,
            draft=None,
            verification=ClaimVerification(
                passed=True,
                checks={
                    "not_found_provider_not_called": context.evidence_count == 0,
                    "deterministic_evidence_compiler": context.evidence_count > 0,
                },
                violations=(),
            ),
        )
    started = time.perf_counter()
    try:
        draft = provider.generate_claims(context)
    except Exception as error:  # noqa: BLE001 - deterministic fallback is the contract
        return KnowledgeAnswerComposition(
            mode="deterministic_fallback",
            answer=context.deterministic_answer,
            provider_name=provider.provider_name,
            model_name=provider.model_name,
            generation_latency_ms=round((time.perf_counter() - started) * 1000, 3),
            draft=None,
            verification=_provider_failure(f"{type(error).__name__}: {error}"),
        )
    verification = KnowledgeClaimVerifier().verify(context, draft)
    latency_ms = round((time.perf_counter() - started) * 1000, 3)
    if not verification.passed:
        return KnowledgeAnswerComposition(
            mode="deterministic_fallback",
            answer=context.deterministic_answer,
            provider_name=provider.provider_name,
            model_name=provider.model_name,
            generation_latency_ms=latency_ms,
            draft=draft,
            verification=verification,
        )
    return KnowledgeAnswerComposition(
        mode="structured_grounded",
        answer=context.deterministic_answer,
        provider_name=provider.provider_name,
        model_name=provider.model_name,
        generation_latency_ms=latency_ms,
        draft=draft,
        verification=verification,
    )


__all__ = [
    "ClaimVerification",
    "DocumentClaimDraft",
    "KnowledgeAnswerComposition",
    "KnowledgeAnswerContext",
    "KnowledgeAnswerDraft",
    "KnowledgeClaimDraft",
    "KnowledgeClaimProvider",
    "KnowledgeClaimVerifier",
    "RelationClaimDraft",
    "build_knowledge_answer_context",
    "compose_knowledge_answer",
    "expected_knowledge_answer_draft",
]
