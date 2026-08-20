from __future__ import annotations

import hashlib
import json
from datetime import date
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from finance_agent_core.contracts.queryplan import ProductFamily
from finance_agent_core.retrieval.models import DocumentSourceKind
from finance_agent_core.retrieval.relations import RelationType


class KnowledgeContractModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


_RELATION_FAMILIES: dict[RelationType, frozenset[ProductFamily]] = {
    RelationType.ISSUED_BY: frozenset({ProductFamily.BOND}),
    RelationType.MANAGED_BY: frozenset({ProductFamily.DOMESTIC_ETP}),
    RelationType.TRACKS_INDEX: frozenset({ProductFamily.DOMESTIC_ETP}),
    RelationType.CLASSIFIED_AS_ASSET: frozenset(
        {ProductFamily.DOMESTIC_ETP, ProductFamily.OVERSEAS_ETP}
    ),
    RelationType.INVESTS_IN_REGION: frozenset(
        {ProductFamily.DOMESTIC_ETP, ProductFamily.OVERSEAS_ETP}
    ),
}


def _require_canonical_enum_tuple(values: tuple[object, ...], label: str) -> None:
    serialized = [getattr(item, "value", str(item)) for item in values]
    if len(serialized) != len(set(serialized)):
        raise ValueError(f"{label} must not contain duplicates")
    if serialized != sorted(serialized):
        raise ValueError(f"{label} must use canonical sorted order")


class RelationKnowledgeOperation(KnowledgeContractModel):
    kind: Literal["relation_search"] = "relation_search"
    query: str = Field(min_length=1, max_length=500)
    relation_types: tuple[RelationType, ...] = Field(min_length=1, max_length=1)
    product_families: tuple[ProductFamily, ...] = Field(min_length=1, max_length=3)
    top_k: int = Field(default=5, ge=1, le=20)
    as_of_on_or_before: date | None = None

    @field_validator("query")
    @classmethod
    def reject_blank_query(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("relation query cannot be blank")
        return value

    @model_validator(mode="after")
    def validate_relation_scope(self) -> RelationKnowledgeOperation:
        _require_canonical_enum_tuple(self.relation_types, "relation_types")
        _require_canonical_enum_tuple(self.product_families, "product_families")
        families = frozenset(self.product_families)
        if ProductFamily.FUND in families:
            raise ValueError("fund relation search is disabled before its source contract")
        for relation_type in self.relation_types:
            if not families & _RELATION_FAMILIES[relation_type]:
                raise ValueError(
                    f"{relation_type.value} is unavailable for the selected product families"
                )
        for family in families:
            if not any(family in _RELATION_FAMILIES[item] for item in self.relation_types):
                raise ValueError(f"selected relation types do not apply to {family.value}")
        return self


class DocumentKnowledgeOperation(KnowledgeContractModel):
    kind: Literal["document_search"] = "document_search"
    query: str = Field(min_length=1, max_length=1000)
    source_kinds: tuple[DocumentSourceKind, ...] = Field(min_length=1, max_length=2)
    document_ids: tuple[str, ...] = Field(default=(), max_length=100)
    as_of_on_or_before: date | None = None
    metadata_equals: dict[str, str] = Field(default_factory=dict)
    top_k: int = Field(default=5, ge=1, le=20)

    @field_validator("query")
    @classmethod
    def reject_blank_query(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("document query cannot be blank")
        return value

    @field_validator("document_ids")
    @classmethod
    def validate_document_ids(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        for value in values:
            if (
                not value
                or len(value) > 128
                or not value[0].isalnum()
                or any(not (character.isalnum() or character in "._:-") for character in value)
            ):
                raise ValueError("document_ids contain an invalid identifier")
        return values

    @field_validator("metadata_equals")
    @classmethod
    def validate_metadata(cls, values: dict[str, str]) -> dict[str, str]:
        for key, value in values.items():
            if (
                not key
                or len(key) > 64
                or not key.replace("_", "").isalnum()
                or not value
                or len(value) > 500
            ):
                raise ValueError("metadata filters require short safe keys and values")
        return values

    @model_validator(mode="after")
    def validate_document_scope(self) -> DocumentKnowledgeOperation:
        _require_canonical_enum_tuple(self.source_kinds, "source_kinds")
        if len(self.document_ids) != len(set(self.document_ids)):
            raise ValueError("document_ids must not contain duplicates")
        if list(self.document_ids) != sorted(self.document_ids):
            raise ValueError("document_ids must use canonical sorted order")
        if list(self.metadata_equals) != sorted(self.metadata_equals):
            raise ValueError("metadata filters must use canonical key order")
        return self


type KnowledgeOperation = Annotated[
    RelationKnowledgeOperation | DocumentKnowledgeOperation,
    Field(discriminator="kind"),
]


class KnowledgeQueryPlan(KnowledgeContractModel):
    schema_version: Literal["1.0"] = "1.0"
    question_id: str = Field(min_length=1, max_length=128)
    question: str = Field(min_length=1, max_length=2000)
    operation: KnowledgeOperation

    @field_validator("question_id", "question")
    @classmethod
    def reject_blank_text(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("question_id and question cannot be blank")
        return value


class KnowledgePlanAuthorityReceipt(KnowledgeContractModel):
    schema_version: Literal["1.0"] = "1.0"
    status: Literal["authorized_exact_match"] = "authorized_exact_match"
    plan_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    operation_kind: Literal["relation_search", "document_search"]
    compiler: Literal["server_knowledge_plan_v1"] = "server_knowledge_plan_v1"
    proposal_provider_name: str | None = Field(default=None, min_length=1, max_length=100)
    proposal_model_name: str | None = Field(default=None, min_length=1, max_length=128)

    @model_validator(mode="after")
    def validate_provider_pair(self) -> KnowledgePlanAuthorityReceipt:
        if (self.proposal_provider_name is None) != (self.proposal_model_name is None):
            raise ValueError("proposal provider and model must be both present or both absent")
        return self


class ValidatedKnowledgePlan(KnowledgeContractModel):
    plan: KnowledgeQueryPlan
    receipt: KnowledgePlanAuthorityReceipt

    @model_validator(mode="after")
    def validate_receipt(self) -> ValidatedKnowledgePlan:
        if self.receipt.plan_sha256 != canonical_knowledge_plan_sha256(self.plan):
            raise ValueError("knowledge plan receipt hash differs")
        if self.receipt.operation_kind != self.plan.operation.kind:
            raise ValueError("knowledge plan receipt operation differs")
        return self


class KnowledgePlanAuthorityError(RuntimeError):
    """Raised before retrieval when a proposal differs from the server plan."""


def canonical_knowledge_plan_sha256(plan: KnowledgeQueryPlan) -> str:
    payload = json.dumps(
        plan.model_dump(mode="json"),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


class KnowledgePlanAuthorityGate:
    """Grant retrieval only to an exact, detached copy of a server-owned plan."""

    def authorize(
        self,
        server_plan: KnowledgeQueryPlan,
        proposal: KnowledgeQueryPlan | None = None,
        *,
        proposal_provider_name: str | None = None,
        proposal_model_name: str | None = None,
    ) -> ValidatedKnowledgePlan:
        if type(server_plan) is not KnowledgeQueryPlan:
            raise TypeError("server_plan must be a KnowledgeQueryPlan")
        trusted = KnowledgeQueryPlan.model_validate_json(server_plan.model_dump_json())
        if proposal is None:
            if proposal_provider_name is not None or proposal_model_name is not None:
                raise ValueError("provider metadata requires a proposal")
        else:
            if type(proposal) is not KnowledgeQueryPlan:
                raise TypeError("proposal must be a KnowledgeQueryPlan")
            candidate = KnowledgeQueryPlan.model_validate_json(proposal.model_dump_json())
            if candidate != trusted:
                raise KnowledgePlanAuthorityError(
                    "knowledge plan proposal differs from the server-owned exact plan"
                )
            if proposal_provider_name is None or proposal_model_name is None:
                raise ValueError("proposal provider and model metadata are required")
        receipt = KnowledgePlanAuthorityReceipt(
            plan_sha256=canonical_knowledge_plan_sha256(trusted),
            operation_kind=trusted.operation.kind,
            proposal_provider_name=proposal_provider_name,
            proposal_model_name=proposal_model_name,
        )
        return ValidatedKnowledgePlan(plan=trusted, receipt=receipt)


__all__ = [
    "DocumentKnowledgeOperation",
    "KnowledgeOperation",
    "KnowledgePlanAuthorityError",
    "KnowledgePlanAuthorityGate",
    "KnowledgePlanAuthorityReceipt",
    "KnowledgeQueryPlan",
    "RelationKnowledgeOperation",
    "ValidatedKnowledgePlan",
    "canonical_knowledge_plan_sha256",
]
