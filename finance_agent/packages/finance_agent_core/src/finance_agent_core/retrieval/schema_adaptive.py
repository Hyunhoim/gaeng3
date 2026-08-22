from __future__ import annotations

import re
import threading
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from finance_agent_core.agent.semantic_resolution import (
    ResolutionOperation,
    SchemaFieldCandidate,
)
from finance_agent_core.config import FieldRegistry, load_field_registry
from finance_agent_core.contracts.queryplan import ProductFamily
from finance_agent_core.contracts.routing import InteractionIntent
from finance_agent_core.deadline import raise_if_request_stopped, remaining_request_timeout
from finance_agent_core.retrieval.schema_dense import (
    DenseSchemaIndex,
    SchemaDenseActivationPolicy,
    SchemaDenseContractError,
)

_TOKEN = re.compile(r"[0-9a-z가-힣_]+", re.IGNORECASE)


class AdaptiveSchemaModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class AdaptiveSchemaLinkStatus(StrEnum):
    FOUND = "found"
    CONFLICT = "conflict"
    ABSTAIN = "abstain"


class AdaptiveSchemaLinkUnavailable(RuntimeError):
    """Raised when bounded Schema Dense capacity is unavailable."""


class AdaptiveSchemaCandidateSet(AdaptiveSchemaModel):
    status: AdaptiveSchemaLinkStatus
    reason_code: Literal[
        "dense_single_candidate",
        "lexical_dense_agreement",
        "lexical_dense_conflict",
        "dense_low_score",
        "dense_low_margin",
        "no_capable_candidate",
    ]
    candidates: tuple[SchemaFieldCandidate, ...] = Field(default=(), max_length=10)
    margin: float | None = Field(default=None, ge=0, le=2.000002)
    hclx_eligible: bool = False

    @model_validator(mode="after")
    def validate_status(self) -> AdaptiveSchemaCandidateSet:
        ranks = [candidate.rank for candidate in self.candidates]
        if ranks != list(range(1, len(ranks) + 1)):
            raise ValueError("adaptive schema candidates must use contiguous ranks")
        if self.status is AdaptiveSchemaLinkStatus.FOUND and not self.candidates:
            raise ValueError("found schema result requires candidates")
        if self.hclx_eligible and (
            self.status is AdaptiveSchemaLinkStatus.FOUND or len(self.candidates) < 2
        ):
            raise ValueError("only unresolved multi-candidate results can call HCLX")
        return self


def _normalize(value: str) -> str:
    return " ".join(item.casefold() for item in _TOKEN.findall(value))


def _field_capable(
    registry: FieldRegistry,
    family: ProductFamily,
    field_id: str,
    operation: ResolutionOperation,
) -> bool:
    definition = registry.require_field(field_id, [family.value])
    return {
        ResolutionOperation.RANK: definition.sortable,
        ResolutionOperation.PROJECT: definition.selectable,
        ResolutionOperation.FILTER: definition.queryable,
        ResolutionOperation.AGGREGATE: definition.aggregatable,
    }[operation]


def _intent_allows_operation(
    intent: InteractionIntent,
    operation: ResolutionOperation,
) -> bool:
    return (
        operation
        in {
            InteractionIntent.SEARCH: {
                ResolutionOperation.RANK,
                ResolutionOperation.PROJECT,
                ResolutionOperation.FILTER,
            },
            InteractionIntent.DETAIL: {ResolutionOperation.PROJECT},
            InteractionIntent.COMPARE: {ResolutionOperation.PROJECT},
            InteractionIntent.AGGREGATE: {ResolutionOperation.AGGREGATE},
            InteractionIntent.EXPLAIN: {ResolutionOperation.PROJECT},
            InteractionIntent.CLARIFY: set(),
            InteractionIntent.UNSUPPORTED: set(),
        }[intent]
    )


class ProductionHybridSchemaLinker:
    """Synchronous candidate-only Lexical + Dense linker with calibrated abstention."""

    def __init__(
        self,
        index: DenseSchemaIndex,
        policy: SchemaDenseActivationPolicy,
        *,
        registry: FieldRegistry | None = None,
    ) -> None:
        if (
            not index.manifest.production_enabled
            or index.manifest.scope != "production_candidate"
            or index.manifest.activation_policy_sha256 != policy.policy_sha256
            or index.manifest.provider.model_id != policy.model_id
            or index.manifest.provider.model_revision != policy.model_revision
        ):
            raise SchemaDenseContractError(
                "adaptive schema linker requires an exact approved production candidate"
            )
        self.index = index
        self.policy = policy
        self.registry = registry or load_field_registry()
        self._query_lock = threading.Lock()

    def _eligible_fields(
        self,
        family: ProductFamily,
        intent: InteractionIntent,
        operation: ResolutionOperation,
    ) -> set[str]:
        if not _intent_allows_operation(intent, operation):
            return set()
        return {
            field_id
            for field_id, definition in self.registry.fields.items()
            if family.value in definition.datasets
            and _field_capable(self.registry, family, field_id, operation)
        }

    def _lexical_rank(self, span: str, family: ProductFamily, eligible: set[str]) -> list[str]:
        normalized = _normalize(span)
        scored: list[tuple[int, int, str]] = []
        for field_id in eligible:
            definition = self.registry.require_field(field_id, [family.value])
            surfaces = tuple(
                filter(
                    None,
                    (
                        _normalize(value)
                        for value in (field_id, definition.label, *definition.aliases)
                    ),
                )
            )
            exact = any(normalized == value for value in surfaces)
            contained = max(
                (
                    min(len(normalized), len(value))
                    for value in surfaces
                    if min(len(normalized), len(value)) >= 2
                    and (normalized in value or value in normalized)
                ),
                default=0,
            )
            if exact:
                scored.append(
                    (
                        2,
                        max(len(value) for value in surfaces if value == normalized),
                        field_id,
                    )
                )
            elif contained:
                scored.append((1, contained, field_id))
        scored.sort(key=lambda item: (-item[0], -item[1], item[2]))
        return [field_id for _, _, field_id in scored[: self.policy.top_k]]

    def link(
        self,
        *,
        residual_span: str,
        product_family: ProductFamily,
        interaction_intent: InteractionIntent,
        operation: ResolutionOperation,
    ) -> AdaptiveSchemaCandidateSet:
        if not residual_span.strip():
            raise ValueError("adaptive schema residual span cannot be blank")
        eligible = self._eligible_fields(product_family, interaction_intent, operation)
        if not eligible:
            return AdaptiveSchemaCandidateSet(
                status=AdaptiveSchemaLinkStatus.ABSTAIN,
                reason_code="no_capable_candidate",
            )
        lexical = self._lexical_rank(residual_span, product_family, eligible)
        raise_if_request_stopped()
        acquired = self._query_lock.acquire(
            timeout=remaining_request_timeout(self.policy.queue_timeout_seconds)
        )
        if not acquired:
            raise AdaptiveSchemaLinkUnavailable("Schema Dense capacity is busy")
        try:
            dense = [
                candidate
                for candidate in self.index.search(
                    residual_span,
                    product_family,
                    top_k=self.policy.top_k,
                    allowed_field_ids=eligible,
                )
                if candidate.field_id in eligible
            ]
        finally:
            self._query_lock.release()
        raise_if_request_stopped()
        if not dense:
            return AdaptiveSchemaCandidateSet(
                status=AdaptiveSchemaLinkStatus.ABSTAIN,
                reason_code="no_capable_candidate",
            )

        ordered_ids = list(dict.fromkeys([*lexical, *(item.field_id for item in dense)]))[
            : self.policy.top_k
        ]
        dense_by_id = {item.field_id: item.score for item in dense}
        lexical_rank = {field_id: rank for rank, field_id in enumerate(lexical, start=1)}
        candidates = tuple(
            SchemaFieldCandidate(
                field_id=field_id,
                rank=rank,
                dense_score=dense_by_id.get(field_id),
                lexical_rank=lexical_rank.get(field_id),
            )
            for rank, field_id in enumerate(ordered_ids, start=1)
        )
        top_dense = dense[0]
        next_dense_score = dense[1].score if len(dense) > 1 else -1.0
        margin = round(max(0.0, top_dense.score - next_dense_score), 9)
        hclx_candidate_floor_met = (
            top_dense.score >= self.policy.hclx_candidate_min_score and len(candidates) >= 2
        )
        if top_dense.score < self.policy.dense_min_score:
            return AdaptiveSchemaCandidateSet(
                status=AdaptiveSchemaLinkStatus.ABSTAIN,
                reason_code="dense_low_score",
                candidates=candidates,
                margin=margin,
                hclx_eligible=hclx_candidate_floor_met,
            )
        if lexical and lexical[0] != top_dense.field_id:
            return AdaptiveSchemaCandidateSet(
                status=AdaptiveSchemaLinkStatus.CONFLICT,
                reason_code="lexical_dense_conflict",
                candidates=candidates,
                margin=margin,
                hclx_eligible=hclx_candidate_floor_met,
            )
        if margin < self.policy.minimum_margin:
            return AdaptiveSchemaCandidateSet(
                status=AdaptiveSchemaLinkStatus.ABSTAIN,
                reason_code="dense_low_margin",
                candidates=candidates,
                margin=margin,
                hclx_eligible=hclx_candidate_floor_met,
            )
        return AdaptiveSchemaCandidateSet(
            status=AdaptiveSchemaLinkStatus.FOUND,
            reason_code=("lexical_dense_agreement" if lexical else "dense_single_candidate"),
            candidates=candidates,
            margin=margin,
            hclx_eligible=False,
        )


__all__ = [
    "AdaptiveSchemaCandidateSet",
    "AdaptiveSchemaLinkUnavailable",
    "AdaptiveSchemaLinkStatus",
    "ProductionHybridSchemaLinker",
]
