from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Literal, Protocol

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    field_validator,
    model_validator,
)

from finance_agent_core.agent.semantic_gate import SemanticCoverageDecision
from finance_agent_core.contracts.queryplan import ProductFamily
from finance_agent_core.contracts.routing import RouteDecision, RouteDisposition


class PlanningModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class PlanningMode(StrEnum):
    ADAPTIVE_SHADOW = "adaptive_shadow"


class PlanningPath(StrEnum):
    CONTROL = "control"
    DETERMINISTIC_FAST = "deterministic_fast"
    SCHEMA_LINK_SHADOW = "schema_link_shadow"
    GROUNDED_MODEL = "grounded_model"


class PlanningSemanticIssue(StrEnum):
    NONE = "none"
    TRUE_AMBIGUITY = "true_ambiguity"
    SCHEMA_LINK_GAP = "schema_link_gap"
    UNSUPPORTED = "unsupported"


class PlanningDecisionStatus(StrEnum):
    OK = "ok"
    POLICY_ERROR = "policy_error"


PlanningSpan = Annotated[str, Field(min_length=1, max_length=200)]


class PlanningDecision(PlanningModel):
    """Server-owned shadow authority for the next planning stage.

    Stage 1 never derives a Dense or HCLX permission from user text.  It mirrors
    an explicitly enabled server-owned HCLX planning capability only on the
    deterministic fast path and records why every control route must remain
    closed.  Dense remains shadow-only until a later policy version.
    """

    schema_version: Literal["1.0"] = "1.0"
    policy_version: Literal["adaptive-shadow-v1"] = "adaptive-shadow-v1"
    mode: Literal[PlanningMode.ADAPTIVE_SHADOW] = PlanningMode.ADAPTIVE_SHADOW
    enforced: Literal[False] = False
    authority_scope: Literal["legacy_route_eligibility"] = "legacy_route_eligibility"
    decision_status: PlanningDecisionStatus = PlanningDecisionStatus.OK
    path: PlanningPath
    semantic_issue: PlanningSemanticIssue
    unresolved_spans: tuple[PlanningSpan, ...] = Field(default=(), max_length=20)
    product_families: tuple[ProductFamily, ...] = Field(default=(), max_length=4)
    route_reason_code: str = Field(min_length=1, max_length=100)
    reason_code: str = Field(min_length=1, max_length=100)
    dense_allowed: StrictBool = Field(
        default=False,
        description="Schema Dense planning permission only; never Product Dense authority",
    )
    hclx_allowed: StrictBool = Field(
        default=False,
        description=(
            "Explicit server-owned Typed QueryPlan HCLX permission only; "
            "never answer-generation authority"
        ),
    )
    sql_allowed: StrictBool = Field(
        default=False,
        description="Shadow mirror of the legacy route, not a ValidatedPlan execution token",
    )
    compiler_allowed: StrictBool = Field(
        default=False,
        description="Shadow mirror; downstream compiler guards remain mandatory",
    )
    oracle_allowed: StrictBool = Field(
        default=False,
        description="Shadow mirror; downstream DB and dataset guards remain mandatory",
    )

    @field_validator("unresolved_spans")
    @classmethod
    def validate_nonblank_spans(
        cls,
        value: tuple[str, ...],
    ) -> tuple[str, ...]:
        if any(not span.strip() for span in value):
            raise ValueError("unresolved_spans cannot contain blank values")
        return value

    @model_validator(mode="after")
    def validate_shadow_authority(self) -> PlanningDecision:
        if len(self.unresolved_spans) != len(set(self.unresolved_spans)):
            raise ValueError("unresolved_spans must be unique")
        if len(self.product_families) != len(set(self.product_families)):
            raise ValueError("product_families must be unique")
        if self.dense_allowed:
            raise ValueError("adaptive-shadow-v1 cannot authorize Dense calls")
        if self.hclx_allowed and self.path is not PlanningPath.DETERMINISTIC_FAST:
            raise ValueError("HCLX planning permission requires the deterministic fast path")

        if self.decision_status is PlanningDecisionStatus.POLICY_ERROR:
            if self.path is not PlanningPath.CONTROL:
                raise ValueError("policy error must use the control path")
            if self.semantic_issue is not PlanningSemanticIssue.NONE:
                raise ValueError("policy error cannot be counted as a user semantic issue")
            if self.unresolved_spans:
                raise ValueError("policy error cannot expose unresolved user spans")
            if self.sql_allowed or self.compiler_allowed or self.oracle_allowed:
                raise ValueError("policy error cannot acquire execution authority")
            if self.reason_code != "planning_policy_error":
                raise ValueError("policy error requires the stable planning reason code")
            return self

        if self.reason_code == "planning_policy_error":
            raise ValueError("healthy planning decision cannot use the policy error reason")
        if self.path is PlanningPath.GROUNDED_MODEL:
            raise ValueError("adaptive-shadow-v1 cannot emit the grounded model path")
        if self.semantic_issue is PlanningSemanticIssue.NONE and self.unresolved_spans:
            raise ValueError("resolved planning decision cannot carry unresolved spans")
        if self.path is PlanningPath.SCHEMA_LINK_SHADOW:
            if not self.unresolved_spans:
                raise ValueError("schema link shadow requires an unresolved span")
            if not self.product_families:
                raise ValueError("schema link shadow requires an approved product family")

        deterministic_permissions = (
            self.sql_allowed and self.compiler_allowed and self.oracle_allowed
        )
        if self.path is PlanningPath.DETERMINISTIC_FAST:
            if self.semantic_issue is not PlanningSemanticIssue.NONE:
                raise ValueError("deterministic fast path cannot carry a semantic issue")
            if not self.product_families:
                raise ValueError("deterministic fast path requires a product family")
            if not deterministic_permissions:
                raise ValueError(
                    "deterministic fast path must mirror compiler, SQL, and Oracle authority"
                )
        elif self.sql_allowed or self.compiler_allowed or self.oracle_allowed:
            raise ValueError("non-deterministic shadow paths cannot acquire execution authority")

        expected_issue = {
            PlanningPath.CONTROL: {
                PlanningSemanticIssue.TRUE_AMBIGUITY,
                PlanningSemanticIssue.UNSUPPORTED,
            },
            PlanningPath.DETERMINISTIC_FAST: {PlanningSemanticIssue.NONE},
            PlanningPath.SCHEMA_LINK_SHADOW: {PlanningSemanticIssue.SCHEMA_LINK_GAP},
            PlanningPath.GROUNDED_MODEL: set(),
        }
        if self.semantic_issue not in expected_issue[self.path]:
            raise ValueError("planning path and semantic issue disagree")
        return self

    @classmethod
    def fail_closed(cls, route_decision: RouteDecision) -> PlanningDecision:
        """Return a non-executable record for an already closed legacy route."""

        return cls(
            path=PlanningPath.CONTROL,
            semantic_issue=PlanningSemanticIssue.NONE,
            decision_status=PlanningDecisionStatus.POLICY_ERROR,
            product_families=tuple(route_decision.draft.product_families),
            route_reason_code=route_decision.reason_code,
            reason_code="planning_policy_error",
        )


class PlanningTrace(PlanningModel):
    """Request-local pair; public RouteDecision and Backend DTO stay unchanged.

    Raw questions and unresolved spans are deliberately not persisted here.
    A later redacted AuditEvent contract must own durable shadow telemetry.
    """

    route_decision: RouteDecision
    planning_decision: PlanningDecision

    @model_validator(mode="after")
    def validate_route_alignment(self) -> PlanningTrace:
        planning = self.planning_decision
        route = self.route_decision
        if planning.route_reason_code != route.reason_code:
            raise ValueError("planning and route reason codes differ")
        if planning.product_families != tuple(route.draft.product_families):
            raise ValueError("planning and route product families differ")

        if planning.decision_status is PlanningDecisionStatus.POLICY_ERROR:
            if route.disposition is RouteDisposition.EXECUTE:
                raise ValueError("policy error cannot leave an executable route open")
            return self

        if route.disposition is RouteDisposition.UNSUPPORTED:
            if (
                planning.path is not PlanningPath.CONTROL
                or planning.semantic_issue is not PlanningSemanticIssue.UNSUPPORTED
            ):
                raise ValueError("unsupported route cannot acquire planning authority")
        elif route.disposition is RouteDisposition.CLARIFY:
            if planning.path is not PlanningPath.CONTROL or planning.semantic_issue not in {
                PlanningSemanticIssue.TRUE_AMBIGUITY,
                PlanningSemanticIssue.UNSUPPORTED,
            }:
                raise ValueError("clarification route cannot acquire planning authority")
        elif planning.path not in {
            PlanningPath.DETERMINISTIC_FAST,
            PlanningPath.SCHEMA_LINK_SHADOW,
        }:
            raise ValueError("executable legacy route has an invalid shadow path")
        return self


class PlanningPolicy(Protocol):
    """Injected policies must be deterministic, stateless, and thread-safe."""

    def decide(
        self,
        route_decision: RouteDecision,
        coverage: SemanticCoverageDecision,
    ) -> PlanningDecision: ...


class AdaptiveShadowPlanningPolicy:
    """Classify existing router outcomes without granting new execution paths."""

    def __init__(self, *, hclx_planning_enabled: bool = False) -> None:
        if type(hclx_planning_enabled) is not bool:
            raise TypeError("hclx_planning_enabled must be a boolean")
        self.hclx_planning_enabled = hclx_planning_enabled

    def decide(
        self,
        route_decision: RouteDecision,
        coverage: SemanticCoverageDecision,
    ) -> PlanningDecision:
        common = {
            "product_families": tuple(route_decision.draft.product_families),
            "route_reason_code": route_decision.reason_code,
        }

        if coverage.unsupported_spans or (
            route_decision.disposition is RouteDisposition.UNSUPPORTED
        ):
            return PlanningDecision(
                path=PlanningPath.CONTROL,
                semantic_issue=PlanningSemanticIssue.UNSUPPORTED,
                unresolved_spans=coverage.unsupported_spans,
                reason_code="route_is_unsupported",
                **common,
            )

        if coverage.ambiguity_spans or (route_decision.disposition is RouteDisposition.CLARIFY):
            return PlanningDecision(
                path=PlanningPath.CONTROL,
                semantic_issue=PlanningSemanticIssue.TRUE_AMBIGUITY,
                unresolved_spans=coverage.ambiguity_spans,
                reason_code="route_requires_clarification",
                **common,
            )

        if coverage.schema_link_gap_spans:
            return PlanningDecision(
                path=PlanningPath.SCHEMA_LINK_SHADOW,
                semantic_issue=PlanningSemanticIssue.SCHEMA_LINK_GAP,
                unresolved_spans=coverage.schema_link_gap_spans,
                reason_code="schema_link_gap_observed",
                **common,
            )

        if route_decision.disposition is RouteDisposition.EXECUTE:
            return PlanningDecision(
                path=PlanningPath.DETERMINISTIC_FAST,
                semantic_issue=PlanningSemanticIssue.NONE,
                reason_code="deterministic_route_executable",
                hclx_allowed=self.hclx_planning_enabled,
                sql_allowed=True,
                compiler_allowed=True,
                oracle_allowed=True,
                **common,
            )

        raise ValueError("unknown route disposition")
