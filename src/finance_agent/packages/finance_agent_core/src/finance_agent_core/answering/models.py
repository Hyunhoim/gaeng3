from __future__ import annotations

from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, model_validator

from finance_agent_core.contracts import QueryPlan
from finance_agent_core.domain import DatabaseManifest, ProductEvidence

type ProviderFailureReason = Literal[
    "authentication_failed",
    "configuration_failed",
    "provider_failed",
    "rate_limited",
    "response_rejected",
    "service_failed",
    "timed_out",
    "transport_failed",
]


class AnswerModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class AnswerWarning(AnswerModel):
    code: str = Field(min_length=1, max_length=100)
    message: str = Field(min_length=1, max_length=500)


class ProductAnswerDraft(AnswerModel):
    result_ref: str = Field(pattern=r"^result_(?:[1-9][0-9]?|100)$")
    evidence_fields: list[str] = Field(min_length=1, max_length=20)
    explanation: str = Field(min_length=1, max_length=240)

    @model_validator(mode="after")
    def validate_unique_fields(self) -> ProductAnswerDraft:
        if len(self.evidence_fields) != len(set(self.evidence_fields)):
            raise ValueError("evidence_fields must be unique")
        return self


class GroundedAnswerDraft(AnswerModel):
    schema_version: Literal["1.0"] = "1.0"
    lead: str = Field(min_length=1, max_length=300)
    products: list[ProductAnswerDraft] = Field(min_length=1, max_length=100)
    acknowledged_warning_codes: list[str] = Field(max_length=20)

    @model_validator(mode="after")
    def validate_unique_products_and_warnings(self) -> GroundedAnswerDraft:
        result_refs = [product.result_ref for product in self.products]
        if len(result_refs) != len(set(result_refs)):
            raise ValueError("draft result references must be unique")
        if len(self.acknowledged_warning_codes) != len(set(self.acknowledged_warning_codes)):
            raise ValueError("acknowledged warning codes must be unique")
        return self


class GroundedAnswerContext(AnswerModel):
    schema_version: Literal["1.0"] = "1.0"
    question: str = Field(min_length=1, max_length=2000)
    query_plan: QueryPlan
    candidate_count: int = Field(ge=0)
    products: list[ProductEvidence]
    warnings: list[AnswerWarning]
    source_manifest: DatabaseManifest
    deterministic_answer: str = Field(min_length=1)


class AnswerVerification(AnswerModel):
    passed: bool
    checks: dict[str, bool]
    violations: list[str]


class AnswerComposition(AnswerModel):
    mode: Literal["llm_grounded", "deterministic", "deterministic_fallback"]
    answer: str = Field(min_length=1)
    model: str | None
    generation_latency_ms: float = Field(ge=0)
    draft: GroundedAnswerDraft | None
    verification: AnswerVerification
    provider_failure_reason: ProviderFailureReason | None = None


class GroundedAnswerProvider(Protocol):
    @property
    def provider_name(self) -> str: ...

    @property
    def model_name(self) -> str | None: ...

    def generate_grounded_answer(
        self,
        context: GroundedAnswerContext,
    ) -> GroundedAnswerDraft: ...
