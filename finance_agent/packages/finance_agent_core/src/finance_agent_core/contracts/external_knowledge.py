from __future__ import annotations

import hashlib
import json
from datetime import date
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from finance_agent_core.config.knowledge_coverage import KnowledgeCapability
from finance_agent_core.contracts.queryplan import ProductFamily, SortDirection
from finance_agent_core.contracts.routing import InteractionIntent


class ExternalKnowledgeContractModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class KnowledgePredicate(StrEnum):
    HOLDS_COMPANY = "holds_company"
    SUBSIDIARY_OF = "subsidiary_of"
    THEME_RELATED = "theme_related"
    DESCRIBES_FUND_STRUCTURE_TREND = "describes_fund_structure_trend"
    HAS_RISK_FACTOR = "has_risk_factor"


_CAPABILITY_PREDICATE: dict[KnowledgeCapability, KnowledgePredicate] = {
    KnowledgeCapability.PORTFOLIO_HOLDINGS: KnowledgePredicate.HOLDS_COMPANY,
    KnowledgeCapability.CORPORATE_SUBSIDIARY: KnowledgePredicate.SUBSIDIARY_OF,
    KnowledgeCapability.SECTOR_THEME_HISTORY: KnowledgePredicate.THEME_RELATED,
    KnowledgeCapability.FUND_STRUCTURE_STRATEGY_TREND: (
        KnowledgePredicate.DESCRIBES_FUND_STRUCTURE_TREND
    ),
    KnowledgeCapability.PRODUCT_RISK_FACTORS: KnowledgePredicate.HAS_RISK_FACTOR,
}


def predicate_for_capability(capability: KnowledgeCapability) -> KnowledgePredicate:
    return _CAPABILITY_PREDICATE[capability]


class KnowledgeIntentRequirement(ExternalKnowledgeContractModel):
    """HCX may point at spans, but it cannot authorize retrieval or invent entities."""

    capability: KnowledgeCapability
    subject_span: str = Field(min_length=1, max_length=120)
    relation_span: str = Field(min_length=1, max_length=120)
    search_aliases: tuple[str, ...] = Field(default=(), max_length=3)

    @field_validator("subject_span", "relation_span")
    @classmethod
    def reject_blank_spans(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("knowledge intent spans cannot be blank")
        return value

    @field_validator("search_aliases")
    @classmethod
    def validate_search_aliases(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if len(values) != len(set(values)):
            raise ValueError("knowledge search aliases must be unique")
        if any(
            value != value.strip()
            or not 2 <= len(value) <= 80
            or "http://" in value.casefold()
            or "https://" in value.casefold()
            or any(ord(character) < 0x20 for character in value)
            for value in values
        ):
            raise ValueError("knowledge search aliases must be bounded plain entity text")
        return values


class KnowledgeTimeRange(ExternalKnowledgeContractModel):
    start: date | None = None
    end: date

    @model_validator(mode="after")
    def validate_order(self) -> KnowledgeTimeRange:
        if self.start is not None and self.start > self.end:
            raise ValueError("knowledge time range start cannot exceed end")
        return self


class KnowledgeRequirement(ExternalKnowledgeContractModel):
    requirement_id: str = Field(pattern=r"^need_[1-9][0-9]?$", max_length=8)
    capability: KnowledgeCapability
    predicate: KnowledgePredicate
    subject: str = Field(min_length=1, max_length=120)
    search_aliases: tuple[str, ...] = Field(default=(), max_length=3)
    subject_evidence_span: str = Field(min_length=1, max_length=120)
    relation_evidence_span: str = Field(min_length=1, max_length=120)
    product_families: tuple[ProductFamily, ...] = Field(default=(), max_length=4)
    time_range: KnowledgeTimeRange | None = None
    source_registry_ids: tuple[str, ...] = Field(min_length=1, max_length=20)
    depends_on: tuple[str, ...] = Field(default=(), max_length=4)

    @model_validator(mode="after")
    def validate_requirement(self) -> KnowledgeRequirement:
        if self.predicate is not predicate_for_capability(self.capability):
            raise ValueError("knowledge predicate differs from its capability")
        families = [item.value for item in self.product_families]
        if families != sorted(families) or len(families) != len(set(families)):
            raise ValueError("knowledge product families must be unique and sorted")
        if list(self.source_registry_ids) != sorted(self.source_registry_ids):
            raise ValueError("source registry IDs must be sorted")
        if len(self.source_registry_ids) != len(set(self.source_registry_ids)):
            raise ValueError("source registry IDs must be unique")
        if self.search_aliases != tuple(sorted(set(self.search_aliases))):
            raise ValueError("knowledge search aliases must be unique and sorted")
        if list(self.depends_on) != sorted(self.depends_on):
            raise ValueError("knowledge dependencies must be sorted")
        if len(self.depends_on) != len(set(self.depends_on)):
            raise ValueError("knowledge dependencies must be unique")
        return self


class KnowledgeProductSelection(ExternalKnowledgeContractModel):
    rank_field: Literal["aum", "one_year_return_pct"]
    direction: SortDirection
    limit: int = Field(ge=1, le=20)
    value_source: Literal["provided_product_database"] = "provided_product_database"


class CompositeKnowledgeQueryPlan(ExternalKnowledgeContractModel):
    """Server-owned multi-hop plan; every external fact remains evidence-bound."""

    schema_version: Literal["1.0"] = "1.0"
    plan_kind: Literal["composite_external_knowledge"] = "composite_external_knowledge"
    question_id: str = Field(min_length=1, max_length=128)
    question: str = Field(min_length=1, max_length=2000)
    interaction_intent: Literal[
        InteractionIntent.SEARCH,
        InteractionIntent.DETAIL,
        InteractionIntent.COMPARE,
        InteractionIntent.EXPLAIN,
    ]
    evidence_cutoff: date
    requirements: tuple[KnowledgeRequirement, ...] = Field(min_length=1, max_length=8)
    product_selection: KnowledgeProductSelection | None = None
    requested_product_fields: tuple[
        Literal[
            "aum",
            "investment_region",
            "manager",
            "product_name",
            "risk_level",
            "ticker",
        ],
        ...,
    ] = Field(default=(), max_length=6)
    join_order: tuple[str, ...] = Field(min_length=2, max_length=9)
    execution_authorized: bool = False

    @model_validator(mode="after")
    def validate_plan(self) -> CompositeKnowledgeQueryPlan:
        requirement_ids = [item.requirement_id for item in self.requirements]
        if len(requirement_ids) != len(set(requirement_ids)):
            raise ValueError("knowledge requirement IDs must be unique")
        if self.join_order[-1] != "official_product_db":
            raise ValueError("external knowledge plans must finish at the official product DB")
        if set(self.join_order[:-1]) != set(requirement_ids):
            raise ValueError("join order must contain every knowledge requirement exactly once")
        if len(self.join_order[:-1]) != len(requirement_ids):
            raise ValueError("join order must not duplicate knowledge requirements")
        for requirement in self.requirements:
            earlier_ids = requirement_ids[: requirement_ids.index(requirement.requirement_id)]
            if any(dependency not in earlier_ids for dependency in requirement.depends_on):
                raise ValueError("knowledge dependency must refer to an earlier requirement")
            if requirement.subject_evidence_span not in self.question:
                raise ValueError("knowledge subject evidence is absent from the question")
            if requirement.relation_evidence_span not in self.question:
                raise ValueError("knowledge relation evidence is absent from the question")
            if (
                requirement.time_range is not None
                and requirement.time_range.end > self.evidence_cutoff
            ):
                raise ValueError("knowledge time range exceeds the evidence cutoff")
        if self.requested_product_fields != tuple(sorted(set(self.requested_product_fields))):
            raise ValueError("requested external product fields must be unique and sorted")
        return self


class ExternalKnowledgeOutcomeStatus(StrEnum):
    """Terminal states for the DB-first external-evidence path."""

    SUCCESS = "success"
    NOT_FOUND = "not_found"
    CANNOT_VERIFY = "cannot_verify"
    CLARIFY = "clarify"
    UNSUPPORTED = "unsupported"


class ExternalSearchBudget(ExternalKnowledgeContractModel):
    """Hard limits owned by the server, never by HCX."""

    deadline_ms: int = Field(default=60_000, ge=100, le=60_000)
    max_search_queries: int = Field(default=8, ge=1, le=40)
    max_candidates_per_requirement: int = Field(default=8, ge=1, le=20)
    max_fetches: int = Field(default=12, ge=1, le=40)
    max_total_bytes: int = Field(default=16 * 1024 * 1024, ge=1, le=64 * 1024 * 1024)


class ProvidedProductCandidate(ExternalKnowledgeContractModel):
    """One eligible product identity loaded only from the provided database."""

    product_family: ProductFamily
    product_id: str = Field(min_length=1, max_length=128)
    product_name: str = Field(min_length=1, max_length=500)
    aliases: tuple[str, ...] = Field(default=(), max_length=20)

    @field_validator("aliases")
    @classmethod
    def validate_aliases(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if values != tuple(sorted(values)) or len(values) != len(set(values)):
            raise ValueError("provided product aliases must be unique and sorted")
        if any(not value.strip() or len(value) > 500 for value in values):
            raise ValueError("provided product aliases must be non-blank and bounded")
        return values


class ProvidedProductCandidateUniverse(ExternalKnowledgeContractModel):
    """Immutable product set fixed before any external discovery is attempted."""

    schema_version: Literal["1.0"] = "1.0"
    candidates: tuple[ProvidedProductCandidate, ...]
    database_sha256s: dict[ProductFamily, str]
    candidate_filter_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    explicit_identity_bound: bool = False

    @model_validator(mode="after")
    def validate_universe(self) -> ProvidedProductCandidateUniverse:
        keys = [(item.product_family.value, item.product_id) for item in self.candidates]
        if keys != sorted(keys) or len(keys) != len(set(keys)):
            raise ValueError("provided candidates must be unique and sorted")
        if any(
            len(value) != 64 or any(ch not in "0123456789abcdef" for ch in value)
            for value in self.database_sha256s.values()
        ):
            raise ValueError("provided database hashes must be SHA-256 values")
        families = {item.product_family for item in self.candidates}
        if not families <= set(self.database_sha256s):
            raise ValueError("provided candidate family lacks a database hash")
        if self.explicit_identity_bound and not self.candidates:
            raise ValueError("an explicit product identity cannot bind an empty universe")
        return self

    @property
    def canonical_sha256(self) -> str:
        payload = (
            json.dumps(
                self.model_dump(mode="json"),
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n"
        ).encode("utf-8")
        return hashlib.sha256(payload).hexdigest()


class ExternalKnowledgeSearchPlan(ExternalKnowledgeContractModel):
    """Runtime search authority bound to a DB candidate universe and source policy."""

    schema_version: Literal["1.0"] = "1.0"
    composite_plan: CompositeKnowledgeQueryPlan
    candidate_universe_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    candidate_count: int = Field(ge=0)
    source_registry_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    budget: ExternalSearchBudget = Field(default_factory=ExternalSearchBudget)

    @model_validator(mode="after")
    def validate_search_plan(self) -> ExternalKnowledgeSearchPlan:
        if not self.composite_plan.execution_authorized:
            raise ValueError("external search plan requires server execution authority")
        return self


class ExternalKnowledgeOutcome(ExternalKnowledgeContractModel):
    """Fail-closed terminal state shared by runtime and evaluation."""

    status: ExternalKnowledgeOutcomeStatus
    search_complete: bool
    verified_product_ids: tuple[str, ...] = ()
    reason_codes: tuple[str, ...] = Field(min_length=1, max_length=20)

    @model_validator(mode="after")
    def validate_outcome(self) -> ExternalKnowledgeOutcome:
        if self.status is ExternalKnowledgeOutcomeStatus.SUCCESS:
            if not self.search_complete or not self.verified_product_ids:
                raise ValueError("SUCCESS requires a complete verified DB intersection")
        elif self.status is ExternalKnowledgeOutcomeStatus.NOT_FOUND:
            if not self.search_complete or self.verified_product_ids:
                raise ValueError("NOT_FOUND requires a complete empty DB intersection")
        elif self.status is ExternalKnowledgeOutcomeStatus.CANNOT_VERIFY:
            if self.search_complete or self.verified_product_ids:
                raise ValueError("CANNOT_VERIFY requires an incomplete evidence chain")
        elif self.verified_product_ids:
            raise ValueError("control outcomes cannot carry product IDs")
        if self.reason_codes != tuple(sorted(set(self.reason_codes))):
            raise ValueError("external outcome reasons must be unique and sorted")
        return self


__all__ = [
    "CompositeKnowledgeQueryPlan",
    "ExternalKnowledgeOutcome",
    "ExternalKnowledgeOutcomeStatus",
    "ExternalKnowledgeSearchPlan",
    "ExternalSearchBudget",
    "KnowledgeIntentRequirement",
    "KnowledgePredicate",
    "KnowledgeProductSelection",
    "KnowledgeRequirement",
    "KnowledgeTimeRange",
    "ProvidedProductCandidate",
    "ProvidedProductCandidateUniverse",
    "predicate_for_capability",
]
