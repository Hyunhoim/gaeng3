from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, model_validator

from finance_agent_core.contracts.queryplan import Intent, ProductFamily


class RoutingModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class InteractionIntent(StrEnum):
    SEARCH = "search"
    DETAIL = "detail"
    COMPARE = "compare"
    AGGREGATE = "aggregate"
    EXPLAIN = "explain"
    CLARIFY = "clarify"
    UNSUPPORTED = "unsupported"


class RouteDisposition(StrEnum):
    EXECUTE = "execute"
    CLARIFY = "clarify"
    UNSUPPORTED = "unsupported"


class MinimalQueryDraft(RoutingModel):
    """Small, non-executable interpretation produced before server compilation."""

    request_id: str = Field(min_length=1, max_length=128)
    question: str = Field(min_length=1, max_length=2000)
    intent: InteractionIntent
    product_families: list[ProductFamily] = Field(max_length=4)
    product_mentions: list[str] = Field(max_length=20)
    requested_limit: int | None = Field(default=None, ge=1, le=100)

    @model_validator(mode="after")
    def validate_unique_values(self) -> MinimalQueryDraft:
        if len(self.product_families) != len(set(self.product_families)):
            raise ValueError("product_families must be unique")
        if len(self.product_mentions) != len(set(self.product_mentions)):
            raise ValueError("product_mentions must be unique")
        return self


class RouteDecision(RoutingModel):
    schema_version: str = "1.0"
    draft: MinimalQueryDraft
    disposition: RouteDisposition
    reason_code: str = Field(min_length=1, max_length=100)
    reason: str = Field(min_length=1, max_length=500)
    query_plan_intent: Intent | None
    capability_matrix_version: str = Field(min_length=1, max_length=20)

    @model_validator(mode="after")
    def validate_execution_contract(self) -> RouteDecision:
        if self.disposition is RouteDisposition.EXECUTE:
            if self.query_plan_intent is None:
                raise ValueError("executable routes require a QueryPlan intent")
            if not self.draft.product_families:
                raise ValueError("executable routes require at least one product family")
            if len(self.draft.product_families) > 1 and (
                self.draft.intent is not InteractionIntent.SEARCH
                or self.query_plan_intent is not Intent.SEARCH
            ):
                raise ValueError("multi-family execution is limited to independent SEARCH routes")
        elif self.query_plan_intent is not None:
            raise ValueError("non-executable routes must not expose a QueryPlan intent")
        if self.disposition is RouteDisposition.CLARIFY:
            if self.draft.intent is not InteractionIntent.CLARIFY and self.reason_code not in {
                "missing_product_identity",
                "ambiguous_product_family",
            }:
                raise ValueError("clarification route requires a clarification reason")
        return self


class RoutedExecutionError(RuntimeError):
    """Carry the last trusted route across the private atomic error seam."""

    def __init__(self, decision: RouteDecision, cause: Exception) -> None:
        self.decision = RouteDecision.model_validate_json(decision.model_dump_json())
        self.cause = cause
        super().__init__("routed execution failed after a trusted route was established")
