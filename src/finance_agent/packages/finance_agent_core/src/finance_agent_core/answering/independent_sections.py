from __future__ import annotations

import hashlib
import json
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

INDEPENDENT_RANKING_NOTICE = (
    "아래 두 섹션은 서로 독립적으로 검증한 결과이며, 하나로 합친 통합 순위가 아닙니다."
)


class IndependentSectionModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class IndependentSectionId(StrEnum):
    OFFICIAL_EXTERNAL_EVIDENCE = "official_external_evidence"
    PROVIDED_DATABASE_COMPOUND = "provided_database_compound"


class IndependentSectionStatus(StrEnum):
    SUCCESS = "success"
    NOT_FOUND = "not_found"
    CANNOT_VERIFY = "cannot_verify"
    CLARIFICATION = "clarification"
    UNSUPPORTED = "unsupported"
    ERROR = "error"


class IndependentSectionRanking(IndependentSectionModel):
    field_id: str = Field(min_length=1, max_length=127)
    direction: Literal["asc", "desc"]
    limit: int = Field(ge=1, le=100)
    ordered_product_ids: tuple[str, ...] = Field(max_length=100)
    value_source: Literal[
        "provided_product_database",
        "verified_official_evidence",
    ]

    @model_validator(mode="after")
    def validate_ordered_products(self) -> IndependentSectionRanking:
        if len(self.ordered_product_ids) != len(set(self.ordered_product_ids)):
            raise ValueError("section ranking product IDs must be unique")
        if len(self.ordered_product_ids) > self.limit:
            raise ValueError("section ranking contains more products than its limit")
        return self


class IndependentSectionCitation(IndependentSectionModel):
    section_id: IndependentSectionId
    citation_id: str = Field(min_length=1, max_length=400)
    evidence_refs: tuple[str, ...] = Field(min_length=1, max_length=100)

    @model_validator(mode="after")
    def validate_namespace(self) -> IndependentSectionCitation:
        prefix = f"{self.section_id.value}::"
        if not self.citation_id.startswith(prefix):
            raise ValueError("section citation ID is outside its namespace")
        if len(self.evidence_refs) != len(set(self.evidence_refs)):
            raise ValueError("section citation evidence references must be unique")
        if any(not item.startswith(prefix) for item in self.evidence_refs):
            raise ValueError("section evidence reference is outside its namespace")
        return self


class IndependentAnswerSection(IndependentSectionModel):
    section_id: IndependentSectionId
    status: IndependentSectionStatus
    plan_kind: str = Field(min_length=1, max_length=100)
    plan_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    candidate_count: int | None = Field(default=None, ge=0)
    product_ids: tuple[str, ...] = Field(max_length=100)
    evidence_refs: tuple[str, ...] = Field(max_length=400)
    citation_ids: tuple[str, ...] = Field(max_length=400)
    ranking: IndependentSectionRanking | None = None
    deterministic_answer: str | None = Field(default=None, max_length=200_000)

    @model_validator(mode="after")
    def validate_terminal_state(self) -> IndependentAnswerSection:
        for label, values in (
            ("product IDs", self.product_ids),
            ("evidence references", self.evidence_refs),
            ("citation IDs", self.citation_ids),
        ):
            if len(values) != len(set(values)):
                raise ValueError(f"section {label} must be unique")
        prefix = f"{self.section_id.value}::"
        if any(not item.startswith(prefix) for item in self.evidence_refs):
            raise ValueError("section evidence reference is outside its namespace")
        if any(not item.startswith(prefix) for item in self.citation_ids):
            raise ValueError("section citation ID is outside its namespace")
        if self.ranking is not None and self.ranking.ordered_product_ids != self.product_ids:
            raise ValueError("section ranking order must equal its public product order")

        complete = self.status in {
            IndependentSectionStatus.SUCCESS,
            IndependentSectionStatus.NOT_FOUND,
        }
        if complete:
            if self.candidate_count is None or not self.deterministic_answer:
                raise ValueError("complete section requires count and deterministic answer")
            if self.status is IndependentSectionStatus.NOT_FOUND:
                if self.candidate_count != 0 or self.product_ids:
                    raise ValueError("not_found section requires an empty product result")
            elif self.candidate_count < len(self.product_ids):
                raise ValueError("section candidate count is smaller than its products")
            if self.status is IndependentSectionStatus.SUCCESS and (
                not self.evidence_refs or not self.citation_ids
            ):
                raise ValueError("complete section requires evidence and citations")
            if bool(self.evidence_refs) != bool(self.citation_ids):
                raise ValueError("section evidence and citations must be present together")
        elif any(
            (
                self.candidate_count is not None,
                bool(self.product_ids),
                bool(self.evidence_refs),
                bool(self.citation_ids),
                self.ranking is not None,
                self.deterministic_answer is not None,
            )
        ):
            raise ValueError("incomplete section cannot assert partial result state")
        return self


class IndependentSectionBundle(IndependentSectionModel):
    schema_version: Literal["1.0"] = "1.0"
    combination_policy: Literal["independent_sections_no_cross_ranking"] = (
        "independent_sections_no_cross_ranking"
    )
    ranking_notice: Literal[
        "아래 두 섹션은 서로 독립적으로 검증한 결과이며, 하나로 합친 통합 순위가 아닙니다."
    ] = INDEPENDENT_RANKING_NOTICE
    sections: tuple[IndependentAnswerSection, IndependentAnswerSection]
    citations: tuple[IndependentSectionCitation, ...] = Field(max_length=800)
    deterministic_answer: str = Field(min_length=1, max_length=400_000)

    @model_validator(mode="after")
    def validate_atomic_projection(self) -> IndependentSectionBundle:
        expected_ids = (
            IndependentSectionId.OFFICIAL_EXTERNAL_EVIDENCE,
            IndependentSectionId.PROVIDED_DATABASE_COMPOUND,
        )
        if tuple(item.section_id for item in self.sections) != expected_ids:
            raise ValueError("independent response requires the two canonical sections in order")
        if any(
            item.status
            not in {
                IndependentSectionStatus.SUCCESS,
                IndependentSectionStatus.NOT_FOUND,
            }
            for item in self.sections
        ):
            raise ValueError("one incomplete section forbids a partial public answer")

        citation_ids = tuple(item.citation_id for item in self.citations)
        if len(citation_ids) != len(set(citation_ids)):
            raise ValueError("section citation IDs must be globally unique")
        expected_citation_ids = tuple(
            citation_id for section in self.sections for citation_id in section.citation_ids
        )
        if citation_ids != expected_citation_ids:
            raise ValueError("section citations must preserve canonical section order")
        citations_by_id = {item.citation_id: item for item in self.citations}
        for section in self.sections:
            cited_refs = {
                evidence_ref
                for citation_id in section.citation_ids
                for evidence_ref in citations_by_id[citation_id].evidence_refs
            }
            if cited_refs != set(section.evidence_refs):
                raise ValueError("section citations and evidence references differ")
        if self.deterministic_answer != render_independent_section_answer(self.sections):
            raise ValueError("sectioned deterministic answer changed")
        return self

    @property
    def section_manifest_sha256(self) -> str:
        payload = {
            "schema_version": self.schema_version,
            "combination_policy": self.combination_policy,
            "ranking_notice": self.ranking_notice,
            "sections": [item.model_dump(mode="json") for item in self.sections],
            "citations": [item.model_dump(mode="json") for item in self.citations],
        }
        encoded = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    @property
    def answer_body_sha256(self) -> str:
        return hashlib.sha256(self.deterministic_answer.encode("utf-8")).hexdigest()


class SectionedFinalAnswerContext(IndependentSectionModel):
    schema_version: Literal["1.0"] = "1.0"
    section_manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    answer_body_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    deterministic_answer: str = Field(min_length=1, max_length=400_000)
    allowed_leads: tuple[str, ...] = Field(min_length=1, max_length=8)

    @model_validator(mode="after")
    def validate_binding(self) -> SectionedFinalAnswerContext:
        if (
            self.answer_body_sha256
            != hashlib.sha256(self.deterministic_answer.encode("utf-8")).hexdigest()
        ):
            raise ValueError("sectioned answer body hash differs")
        if len(self.allowed_leads) != len(set(self.allowed_leads)):
            raise ValueError("sectioned answer leads must be unique")
        return self


class SectionedFinalAnswerDraft(IndependentSectionModel):
    schema_version: Literal["1.0"] = "1.0"
    lead: str = Field(min_length=1, max_length=160)
    section_manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    answer_body_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class SectionedAnswerVerificationError(ValueError):
    """HCX attempted to change an atomically verified sectioned answer."""


class SectionedAnswerAtomicityError(ValueError):
    """One independent section failed, so no partial result may be exposed."""


def namespace_section_reference(section_id: IndependentSectionId, raw_id: str) -> str:
    """Bind a citation or evidence reference to exactly one public section."""

    value = raw_id.strip()
    if not value:
        raise ValueError("section reference cannot be blank")
    known_prefixes = tuple(f"{item.value}::" for item in IndependentSectionId)
    if value.startswith(known_prefixes):
        expected_prefix = f"{section_id.value}::"
        if not value.startswith(expected_prefix):
            raise ValueError("section reference cannot move between namespaces")
        return value
    return f"{section_id.value}::{value}"


def render_independent_section_answer(
    sections: tuple[IndependentAnswerSection, IndependentAnswerSection],
) -> str:
    """Render the immutable two-section body without calculating a joint rank."""

    labels = {
        IndependentSectionId.OFFICIAL_EXTERNAL_EVIDENCE: "[공식 외부 근거 결과]",
        IndependentSectionId.PROVIDED_DATABASE_COMPOUND: "[독립 제공 DB 계산 결과]",
    }
    bodies = [INDEPENDENT_RANKING_NOTICE]
    for section in sections:
        if not section.deterministic_answer:
            raise ValueError("cannot render an incomplete independent section")
        bodies.extend((labels[section.section_id], section.deterministic_answer))
    return "\n\n".join(bodies)


def build_independent_section_bundle(
    *,
    external_section: IndependentAnswerSection,
    database_section: IndependentAnswerSection,
    citations: tuple[IndependentSectionCitation, ...],
) -> IndependentSectionBundle:
    """Atomically publish two complete sections or raise before any projection."""

    sections = (external_section, database_section)
    if any(
        section.status
        not in {
            IndependentSectionStatus.SUCCESS,
            IndependentSectionStatus.NOT_FOUND,
        }
        for section in sections
    ):
        raise SectionedAnswerAtomicityError(
            "one incomplete section forbids a partial public answer"
        )
    return IndependentSectionBundle(
        sections=sections,
        citations=citations,
        deterministic_answer=render_independent_section_answer(sections),
    )


def bind_sectioned_final_answer(
    bundle: IndependentSectionBundle,
    *,
    allowed_leads: tuple[str, ...],
) -> SectionedFinalAnswerContext:
    """Freeze section/product/rank state before calling the answer model."""

    return SectionedFinalAnswerContext(
        section_manifest_sha256=bundle.section_manifest_sha256,
        answer_body_sha256=bundle.answer_body_sha256,
        deterministic_answer=bundle.deterministic_answer,
        allowed_leads=allowed_leads,
    )


def verify_sectioned_final_answer(
    context: SectionedFinalAnswerContext,
    draft: SectionedFinalAnswerDraft,
) -> str:
    """Accept only an HCX lead plus exact server-owned section/body hashes."""

    if draft.lead not in context.allowed_leads:
        raise SectionedAnswerVerificationError("HCX returned an unapproved sectioned answer lead")
    if draft.section_manifest_sha256 != context.section_manifest_sha256:
        raise SectionedAnswerVerificationError(
            "HCX changed section, product, ranking, or evidence membership"
        )
    if draft.answer_body_sha256 != context.answer_body_sha256:
        raise SectionedAnswerVerificationError("HCX changed the sectioned deterministic answer")
    return f"{draft.lead}\n{context.deterministic_answer}"


__all__ = [
    "INDEPENDENT_RANKING_NOTICE",
    "IndependentAnswerSection",
    "IndependentSectionBundle",
    "IndependentSectionCitation",
    "IndependentSectionId",
    "IndependentSectionRanking",
    "IndependentSectionStatus",
    "SectionedAnswerAtomicityError",
    "SectionedAnswerVerificationError",
    "SectionedFinalAnswerContext",
    "SectionedFinalAnswerDraft",
    "bind_sectioned_final_answer",
    "build_independent_section_bundle",
    "namespace_section_reference",
    "render_independent_section_answer",
    "verify_sectioned_final_answer",
]
