from __future__ import annotations

import hashlib
import json
from enum import StrEnum
from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, model_validator

from finance_agent_core.config import load_field_registry
from finance_agent_core.contracts.queryplan import (
    Constraint,
    ConstraintStrength,
    ProductFamily,
    QueryPlan,
    Ranking,
    SortDirection,
)
from finance_agent_core.contracts.routing import InteractionIntent

_SHA256_PATTERN = r"^[0-9a-f]{64}$"
_FIELD_ID_PATTERN = r"^[a-z][a-z0-9_]{1,127}$"
_NONE_FIELD = "__none__"


def canonical_sha256(value: object) -> str:
    """Hash one semantic contract with a stable, non-lossy JSON encoding."""

    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


class SemanticResolutionError(ValueError):
    """Raised when advisory semantics cannot acquire server execution authority."""


class SemanticModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class SpanRole(StrEnum):
    PRODUCT_FAMILY = "product_family"
    PRODUCT_IDENTITY = "product_identity"
    HARD_FILTER = "hard_filter"
    RANKING = "ranking"
    LIMIT = "limit"
    RESIDUAL = "residual"


class SpanSource(StrEnum):
    LEXICAL = "lexical"
    SCHEMA_DENSE = "schema_dense"
    HCLX = "hclx"


class ResolutionOperation(StrEnum):
    RANK = "rank"
    PROJECT = "project"
    FILTER = "filter"
    AGGREGATE = "aggregate"


class ResolutionDecision(StrEnum):
    RESOLVE = "resolve"
    CLARIFY = "clarify"
    UNSUPPORTED = "unsupported"


class ResolutionPath(StrEnum):
    CONTROL = "control"
    DETERMINISTIC_FAST = "deterministic_fast"
    SCHEMA_DENSE = "schema_dense_resolution"
    HCLX = "hclx_semantic_resolution"


class ResolvedSpan(SemanticModel):
    """One request-local span already understood by deterministic server rules."""

    text: str = Field(min_length=1, max_length=200)
    start: int = Field(ge=0, le=2_000)
    end: int = Field(gt=0, le=2_000)
    role: SpanRole
    source: Literal[SpanSource.LEXICAL] = SpanSource.LEXICAL
    field_id: str | None = Field(default=None, pattern=_FIELD_ID_PATTERN)

    @model_validator(mode="after")
    def validate_bounds_and_field_role(self) -> ResolvedSpan:
        if self.end <= self.start:
            raise ValueError("resolved span end must be greater than start")
        field_roles = {SpanRole.HARD_FILTER, SpanRole.RANKING}
        if (self.role in field_roles) != (self.field_id is not None):
            raise ValueError("only hard-filter and ranking spans require a field ID")
        return self


class ResidualSpan(SemanticModel):
    """Meaning-bearing text that deterministic rules deliberately did not resolve."""

    text: str = Field(min_length=1, max_length=200)
    start: int = Field(ge=0, le=2_000)
    end: int = Field(gt=0, le=2_000)

    @model_validator(mode="after")
    def validate_bounds(self) -> ResidualSpan:
        if self.end <= self.start:
            raise ValueError("residual span end must be greater than start")
        return self

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.text.encode("utf-8")).hexdigest()


class ResolvedSpanLedger(SemanticModel):
    """Request-local coverage ledger; raw text is never suitable for durable Audit."""

    schema_version: Literal["1.0"] = "1.0"
    question_sha256: str = Field(pattern=_SHA256_PATTERN)
    interaction_intent: InteractionIntent
    product_families: tuple[ProductFamily, ...] = Field(max_length=4)
    resolved_spans: tuple[ResolvedSpan, ...] = Field(default=(), max_length=50)
    residual_spans: tuple[ResidualSpan, ...] = Field(default=(), max_length=20)

    @model_validator(mode="after")
    def validate_unique_non_overlapping_residuals(self) -> ResolvedSpanLedger:
        families = tuple(dict.fromkeys(self.product_families))
        if families != self.product_families:
            raise ValueError("ledger product families must be unique")
        residual_ranges = [(item.start, item.end) for item in self.residual_spans]
        if len(residual_ranges) != len(set(residual_ranges)):
            raise ValueError("ledger residual spans must be unique")
        ordered = sorted(residual_ranges)
        if any(left[1] > right[0] for left, right in zip(ordered, ordered[1:], strict=False)):
            raise ValueError("ledger residual spans cannot overlap")
        return self


class HardFilterLock(SemanticModel):
    """Immutable server interpretation that Dense and HCLX are forbidden to alter."""

    schema_version: Literal["1.0"] = "1.0"
    product_families: tuple[ProductFamily, ...] = Field(min_length=1, max_length=4)
    constraints: tuple[Constraint, ...] = Field(default=(), max_length=20)
    rankings: tuple[Ranking, ...] = Field(default=(), max_length=5)
    requested_limit: int | None = Field(default=None, ge=1, le=100)
    product_mentions: tuple[str, ...] = Field(default=(), max_length=20)
    payload_sha256: str = Field(pattern=_SHA256_PATTERN)

    @staticmethod
    def _payload(
        *,
        product_families: tuple[ProductFamily, ...],
        constraints: tuple[Constraint, ...],
        rankings: tuple[Ranking, ...],
        requested_limit: int | None,
        product_mentions: tuple[str, ...],
    ) -> dict[str, object]:
        return {
            "product_families": [item.value for item in product_families],
            "constraints": [item.model_dump(mode="json") for item in constraints],
            "rankings": [item.model_dump(mode="json") for item in rankings],
            "requested_limit": requested_limit,
            "product_mentions": list(product_mentions),
        }

    @classmethod
    def create(
        cls,
        *,
        product_families: tuple[ProductFamily, ...],
        constraints: tuple[Constraint, ...] = (),
        rankings: tuple[Ranking, ...] = (),
        requested_limit: int | None = None,
        product_mentions: tuple[str, ...] = (),
    ) -> HardFilterLock:
        product_families = tuple(product_families)
        constraints = tuple(
            Constraint.model_validate(item.model_dump(mode="json")) for item in constraints
        )
        rankings = tuple(Ranking.model_validate(item.model_dump(mode="json")) for item in rankings)
        product_mentions = tuple(product_mentions)
        payload = cls._payload(
            product_families=product_families,
            constraints=constraints,
            rankings=rankings,
            requested_limit=requested_limit,
            product_mentions=product_mentions,
        )
        return cls(
            product_families=product_families,
            constraints=constraints,
            rankings=rankings,
            requested_limit=requested_limit,
            product_mentions=product_mentions,
            payload_sha256=canonical_sha256(payload),
        )

    @classmethod
    def from_plan(
        cls,
        plan: QueryPlan,
        *,
        requested_limit: int | None = None,
        product_mentions: tuple[str, ...] = (),
    ) -> HardFilterLock:
        return cls.create(
            product_families=tuple(plan.product_families),
            constraints=tuple(plan.constraints),
            rankings=tuple(plan.ranking),
            requested_limit=requested_limit,
            product_mentions=product_mentions,
        )

    @model_validator(mode="after")
    def validate_lock(self) -> HardFilterLock:
        if len(self.product_families) != len(set(self.product_families)):
            raise ValueError("hard-filter product families must be unique")
        if len(self.product_mentions) != len(set(self.product_mentions)):
            raise ValueError("hard-filter product mentions must be unique")
        if any(item.strength is not ConstraintStrength.LOCKED for item in self.constraints):
            raise ValueError("hard-filter constraints must use locked strength")
        expected = canonical_sha256(
            self._payload(
                product_families=self.product_families,
                constraints=self.constraints,
                rankings=self.rankings,
                requested_limit=self.requested_limit,
                product_mentions=self.product_mentions,
            )
        )
        if self.payload_sha256 != expected:
            raise ValueError("hard-filter payload SHA-256 differs")
        return self

    def require_preserved(self, plan: QueryPlan) -> None:
        """Reject a compiled plan that changed any pre-model server interpretation."""

        current_lock_hash = canonical_sha256(
            self._payload(
                product_families=self.product_families,
                constraints=self.constraints,
                rankings=self.rankings,
                requested_limit=self.requested_limit,
                product_mentions=self.product_mentions,
            )
        )
        if current_lock_hash != self.payload_sha256:
            raise SemanticResolutionError("hard-filter lock changed after validation")
        if tuple(plan.product_families) != self.product_families:
            raise SemanticResolutionError("semantic resolution changed the product family")
        plan_constraints = tuple(plan.constraints)
        if plan_constraints != self.constraints:
            raise SemanticResolutionError("semantic resolution changed a locked constraint")
        plan_rankings = tuple(plan.ranking)
        if any(item not in plan_rankings for item in self.rankings):
            raise SemanticResolutionError("semantic resolution changed a locked ranking")
        if self.requested_limit is not None and plan.limit != self.requested_limit:
            raise SemanticResolutionError("semantic resolution changed the requested limit")


class SchemaFieldCandidate(SemanticModel):
    field_id: str = Field(pattern=_FIELD_ID_PATTERN)
    rank: int = Field(ge=1, le=20)
    dense_score: float | None = Field(default=None, ge=-1.000001, le=1.000001)
    lexical_rank: int | None = Field(default=None, ge=1, le=20)


class SemanticResolutionRequest(SemanticModel):
    """Minimum-privilege request passed to an advisory HCLX resolver."""

    schema_version: Literal["1.0"] = "1.0"
    request_id: str = Field(min_length=1, max_length=128)
    residual_span: str = Field(min_length=1, max_length=200)
    product_family: ProductFamily
    interaction_intent: InteractionIntent
    allowed_operations: tuple[ResolutionOperation, ...] = Field(min_length=1, max_length=4)
    candidates: tuple[SchemaFieldCandidate, ...] = Field(min_length=1, max_length=10)
    expected_direction: SortDirection | None = None
    hard_filter_lock_sha256: str = Field(pattern=_SHA256_PATTERN)

    @model_validator(mode="after")
    def validate_candidates(self) -> SemanticResolutionRequest:
        ranks = [item.rank for item in self.candidates]
        if ranks != list(range(1, len(ranks) + 1)):
            raise ValueError("semantic candidates must use contiguous ranks")
        fields = [item.field_id for item in self.candidates]
        if len(fields) != len(set(fields)):
            raise ValueError("semantic candidate fields must be unique")
        if len(self.allowed_operations) != len(set(self.allowed_operations)):
            raise ValueError("semantic operations must be unique")
        return self


class SemanticResolutionDraft(SemanticModel):
    """Untrusted Dense/HCLX proposal; it is never directly compilable."""

    decision: ResolutionDecision
    selected_field_id: str = Field(min_length=2, max_length=127)
    operation: ResolutionOperation
    direction: SortDirection
    reason_code: Literal[
        "candidate_context_match",
        "multiple_interpretations",
        "unsupported_meaning",
    ]

    @model_validator(mode="after")
    def validate_control_field(self) -> SemanticResolutionDraft:
        if (self.decision is ResolutionDecision.RESOLVE) == (self.selected_field_id == _NONE_FIELD):
            raise ValueError("resolved drafts require one field; control drafts require __none__")
        return self


class SemanticResolutionReceipt(SemanticModel):
    """Server-validated authority receipt consumed by the compiler."""

    schema_version: Literal["1.0"] = "1.0"
    decision: Literal[ResolutionDecision.RESOLVE] = ResolutionDecision.RESOLVE
    source: Literal[SpanSource.SCHEMA_DENSE, SpanSource.HCLX]
    request_id_sha256: str = Field(pattern=_SHA256_PATTERN)
    residual_span_sha256: str = Field(pattern=_SHA256_PATTERN)
    product_family: ProductFamily
    field_id: str = Field(pattern=_FIELD_ID_PATTERN)
    operation: ResolutionOperation
    direction: SortDirection
    admitted_candidate_ids: tuple[str, ...] = Field(min_length=1, max_length=10)
    hard_filter_lock_sha256: str = Field(pattern=_SHA256_PATTERN)
    receipt_sha256: str = Field(pattern=_SHA256_PATTERN)

    @staticmethod
    def _payload(**values: object) -> dict[str, object]:
        return values

    @classmethod
    def create(
        cls,
        *,
        source: Literal[SpanSource.SCHEMA_DENSE, SpanSource.HCLX],
        request: SemanticResolutionRequest,
        field_id: str,
        operation: ResolutionOperation,
        direction: SortDirection,
    ) -> SemanticResolutionReceipt:
        values = {
            "source": source.value,
            "request_id_sha256": hashlib.sha256(request.request_id.encode("utf-8")).hexdigest(),
            "residual_span_sha256": hashlib.sha256(
                request.residual_span.encode("utf-8")
            ).hexdigest(),
            "product_family": request.product_family.value,
            "field_id": field_id,
            "operation": operation.value,
            "direction": direction.value,
            "admitted_candidate_ids": [item.field_id for item in request.candidates],
            "hard_filter_lock_sha256": request.hard_filter_lock_sha256,
        }
        return cls(
            source=source,
            request_id_sha256=values["request_id_sha256"],
            residual_span_sha256=values["residual_span_sha256"],
            product_family=request.product_family,
            field_id=field_id,
            operation=operation,
            direction=direction,
            admitted_candidate_ids=tuple(values["admitted_candidate_ids"]),
            hard_filter_lock_sha256=request.hard_filter_lock_sha256,
            receipt_sha256=canonical_sha256(values),
        )

    @model_validator(mode="after")
    def validate_receipt(self) -> SemanticResolutionReceipt:
        values = {
            "source": self.source.value,
            "request_id_sha256": self.request_id_sha256,
            "residual_span_sha256": self.residual_span_sha256,
            "product_family": self.product_family.value,
            "field_id": self.field_id,
            "operation": self.operation.value,
            "direction": self.direction.value,
            "admitted_candidate_ids": list(self.admitted_candidate_ids),
            "hard_filter_lock_sha256": self.hard_filter_lock_sha256,
        }
        if self.receipt_sha256 != canonical_sha256(values):
            raise ValueError("semantic resolution receipt SHA-256 differs")
        if self.field_id not in self.admitted_candidate_ids:
            raise ValueError("semantic receipt field is outside the admitted candidates")
        return self


class SemanticResolverProvider(Protocol):
    @property
    def provider_name(self) -> Literal["hyperclova"]: ...

    @property
    def model_name(self) -> str: ...

    def resolve_semantics(self, request: SemanticResolutionRequest) -> SemanticResolutionDraft: ...


class SemanticResolutionGate:
    """Turn a bounded proposal into authority only after registry and lock checks."""

    def admit(
        self,
        draft: SemanticResolutionDraft,
        request: SemanticResolutionRequest,
        *,
        source: Literal[SpanSource.SCHEMA_DENSE, SpanSource.HCLX],
    ) -> SemanticResolutionReceipt:
        if draft.decision is not ResolutionDecision.RESOLVE:
            raise SemanticResolutionError("semantic resolver abstained")
        candidates = {item.field_id for item in request.candidates}
        if draft.selected_field_id not in candidates:
            raise SemanticResolutionError("semantic resolver selected a field outside candidates")
        if draft.operation not in request.allowed_operations:
            raise SemanticResolutionError("semantic resolver selected an unapproved operation")
        if (
            request.expected_direction is not None
            and draft.direction is not request.expected_direction
        ):
            raise SemanticResolutionError("semantic resolver changed the requested direction")

        definition = load_field_registry().require_field(
            draft.selected_field_id,
            [request.product_family.value],
        )
        capability = {
            ResolutionOperation.RANK: definition.sortable,
            ResolutionOperation.PROJECT: definition.selectable,
            ResolutionOperation.FILTER: definition.queryable,
            ResolutionOperation.AGGREGATE: definition.aggregatable,
        }[draft.operation]
        if not capability:
            raise SemanticResolutionError("semantic resolver selected an incapable field")
        return SemanticResolutionReceipt.create(
            source=source,
            request=request,
            field_id=draft.selected_field_id,
            operation=draft.operation,
            direction=draft.direction,
        )


class AdaptivePlanningDecisionV2(SemanticModel):
    """Enforced minimum-privilege authority after semantic admission."""

    schema_version: Literal["2.0"] = "2.0"
    policy_version: Literal["adaptive-semantic-v2"] = "adaptive-semantic-v2"
    enforced: Literal[True] = True
    path: ResolutionPath
    dense_allowed: bool
    hclx_resolver_allowed: bool
    compiler_allowed: bool
    sql_allowed: bool
    oracle_allowed: bool
    receipt_sha256: str | None = Field(default=None, pattern=_SHA256_PATTERN)

    @model_validator(mode="after")
    def validate_authority(self) -> AdaptivePlanningDecisionV2:
        if self.path is ResolutionPath.CONTROL:
            if any(
                (
                    self.dense_allowed,
                    self.hclx_resolver_allowed,
                    self.compiler_allowed,
                    self.sql_allowed,
                    self.oracle_allowed,
                    self.receipt_sha256 is not None,
                )
            ):
                raise ValueError("control planning path cannot acquire authority")
            return self
        if not (self.compiler_allowed and self.sql_allowed and self.oracle_allowed):
            raise ValueError("executable planning path requires full deterministic authority")
        if self.path is ResolutionPath.DETERMINISTIC_FAST:
            if self.dense_allowed or self.hclx_resolver_allowed or self.receipt_sha256 is not None:
                raise ValueError("deterministic fast path cannot carry semantic resolver authority")
        elif self.path is ResolutionPath.SCHEMA_DENSE:
            if not self.dense_allowed or self.hclx_resolver_allowed or self.receipt_sha256 is None:
                raise ValueError("Schema Dense path requires only Dense resolver authority")
        elif self.path is ResolutionPath.HCLX:
            if not self.dense_allowed or not self.hclx_resolver_allowed:
                raise ValueError("HCLX path requires prior Dense candidate authority")
            if self.receipt_sha256 is None:
                raise ValueError("HCLX path requires a semantic receipt")
        return self

    @classmethod
    def from_receipt(cls, receipt: SemanticResolutionReceipt) -> AdaptivePlanningDecisionV2:
        return cls(
            path=(
                ResolutionPath.SCHEMA_DENSE
                if receipt.source is SpanSource.SCHEMA_DENSE
                else ResolutionPath.HCLX
            ),
            dense_allowed=True,
            hclx_resolver_allowed=receipt.source is SpanSource.HCLX,
            compiler_allowed=True,
            sql_allowed=True,
            oracle_allowed=True,
            receipt_sha256=receipt.receipt_sha256,
        )


class AdaptivePlanningPolicyV2:
    """Server-owned factory for every adaptive authority decision."""

    @staticmethod
    def control() -> AdaptivePlanningDecisionV2:
        return AdaptivePlanningDecisionV2(
            path=ResolutionPath.CONTROL,
            dense_allowed=False,
            hclx_resolver_allowed=False,
            compiler_allowed=False,
            sql_allowed=False,
            oracle_allowed=False,
        )

    @staticmethod
    def admit(receipt: SemanticResolutionReceipt) -> AdaptivePlanningDecisionV2:
        detached = SemanticResolutionReceipt.model_validate(receipt.model_dump(mode="python"))
        return AdaptivePlanningDecisionV2.from_receipt(detached)


__all__ = [
    "AdaptivePlanningDecisionV2",
    "AdaptivePlanningPolicyV2",
    "HardFilterLock",
    "ResidualSpan",
    "ResolutionDecision",
    "ResolutionOperation",
    "ResolutionPath",
    "ResolvedSpan",
    "ResolvedSpanLedger",
    "SchemaFieldCandidate",
    "SemanticResolutionDraft",
    "SemanticResolutionError",
    "SemanticResolutionGate",
    "SemanticResolutionReceipt",
    "SemanticResolutionRequest",
    "SemanticResolverProvider",
    "SpanRole",
    "SpanSource",
    "canonical_sha256",
]
