from __future__ import annotations

from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, model_validator

from finance_agent_core.contracts.external_knowledge import KnowledgeIntentRequirement
from finance_agent_core.contracts.queryplan import ProductFamily
from finance_agent_core.contracts.routing import (
    InteractionIntent,
    RouteDecision,
    RouteDisposition,
)


class IntentAnalysisModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class IntentAnalysisDraft(IntentAnalysisModel):
    """Non-executable HCX interpretation with no field or SQL authority."""

    schema_version: Literal["1.0"] = "1.0"
    intent: InteractionIntent
    product_families: list[ProductFamily] = Field(max_length=4)
    evidence_spans: list[str] = Field(max_length=8)
    needs_clarification: bool
    knowledge_requirements: list[KnowledgeIntentRequirement] = Field(
        default_factory=list,
        max_length=8,
    )

    @model_validator(mode="after")
    def validate_shape(self) -> IntentAnalysisDraft:
        if len(self.product_families) != len(set(self.product_families)):
            raise ValueError("product_families must be unique")
        if len(self.evidence_spans) != len(set(self.evidence_spans)):
            raise ValueError("evidence_spans must be unique")
        knowledge_keys = [
            (item.capability, item.subject_span, item.relation_span)
            for item in self.knowledge_requirements
        ]
        if len(knowledge_keys) != len(set(knowledge_keys)):
            raise ValueError("knowledge requirements must be unique")
        if self.intent in {InteractionIntent.CLARIFY, InteractionIntent.UNSUPPORTED}:
            if not self.needs_clarification and self.intent is InteractionIntent.CLARIFY:
                raise ValueError("CLARIFY intent must acknowledge clarification")
        elif self.needs_clarification:
            raise ValueError("executable intent cannot request clarification")
        if self.needs_clarification and self.knowledge_requirements:
            raise ValueError("clarification intent cannot carry executable knowledge requirements")
        return self


class IntentAnalysisProvider(Protocol):
    @property
    def provider_name(self) -> str: ...

    @property
    def model_name(self) -> str: ...

    def analyze_intent(self, question: str, question_id: str) -> IntentAnalysisDraft: ...


class IntentAnalysisVerification(IntentAnalysisModel):
    passed: bool
    reason_code: Literal[
        "intent_analysis_verified",
        "intent_analysis_invalid_evidence",
        "intent_analysis_requires_clarification",
        "intent_analysis_intent_conflict",
        "intent_analysis_family_conflict",
    ]


def verify_intent_analysis(
    question: str,
    server_decision: RouteDecision,
    draft: IntentAnalysisDraft,
) -> IntentAnalysisVerification:
    """Cross-check HCX without allowing it to upgrade server authority."""

    knowledge_spans = [
        span
        for requirement in draft.knowledge_requirements
        for span in (requirement.subject_span, requirement.relation_span)
    ]
    if any(
        not span.strip() or span not in question
        for span in [*draft.evidence_spans, *knowledge_spans]
    ):
        return IntentAnalysisVerification(
            passed=False,
            reason_code="intent_analysis_invalid_evidence",
        )

    # Deterministic terminal decisions remain terminal. HCX participates in
    # intent analysis but can never downgrade UNSUPPORTED/CLARIFY to execution.
    if server_decision.disposition is not RouteDisposition.EXECUTE:
        return IntentAnalysisVerification(
            passed=True,
            reason_code="intent_analysis_verified",
        )

    if draft.needs_clarification or draft.intent in {
        InteractionIntent.CLARIFY,
        InteractionIntent.UNSUPPORTED,
    }:
        return IntentAnalysisVerification(
            passed=False,
            reason_code="intent_analysis_requires_clarification",
        )
    if draft.intent is not server_decision.draft.intent:
        return IntentAnalysisVerification(
            passed=False,
            reason_code="intent_analysis_intent_conflict",
        )
    # Product-family order carries no execution meaning.  Require the exact
    # same unique set while allowing HCX to serialize multi-family mentions in
    # a different order from the deterministic router.
    if set(draft.product_families) != set(server_decision.draft.product_families):
        return IntentAnalysisVerification(
            passed=False,
            reason_code="intent_analysis_family_conflict",
        )
    return IntentAnalysisVerification(
        passed=True,
        reason_code="intent_analysis_verified",
    )


__all__ = [
    "IntentAnalysisDraft",
    "IntentAnalysisProvider",
    "IntentAnalysisVerification",
    "verify_intent_analysis",
]
