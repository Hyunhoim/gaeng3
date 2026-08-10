from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence
from importlib.resources import files
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from finance_agent_core.contracts.queryplan import ProductFamily
from finance_agent_core.evaluation.official_mock import OfficialMockCase

_AUDIT_RESOURCE = "official_mock_v1_gold_audit_v1.json"


class GoldAuditModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class GoldAuditCorrection(GoldAuditModel):
    case_id: str = Field(pattern=r"^official-mock-v1-[0-9]{3}$")
    product_family: ProductFamily
    ordering_field: str = Field(min_length=1, max_length=100)
    requested_direction: Literal["asc", "desc"]
    historical_direction: Literal["asc", "desc"]
    candidate_count: int = Field(ge=0)
    product_ids: list[str] = Field(min_length=1, max_length=100)
    sort_values: list[int | float] = Field(min_length=1, max_length=100)
    database_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    tie_breaker: str = Field(min_length=1, max_length=200)
    verification_note: str = Field(min_length=1, max_length=1000)

    @model_validator(mode="after")
    def validate_correction(self) -> GoldAuditCorrection:
        if self.requested_direction == self.historical_direction:
            raise ValueError("gold correction must document a changed sort direction")
        if len(self.product_ids) != len(self.sort_values):
            raise ValueError("corrected product IDs and sort values must align")
        if len(self.product_ids) != len(set(self.product_ids)):
            raise ValueError("corrected product IDs must be unique")
        return self


class OfficialMockGoldAudit(GoldAuditModel):
    schema_version: Literal["1.0"]
    audit_id: Literal["official-mock-v1-gold-audit-v1"]
    status: Literal["corrected_overlay_preserves_historical_suite"]
    source_suite_id: Literal["official-mock-v1-30"]
    source_suite_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    corrections: list[GoldAuditCorrection] = Field(min_length=1)
    interpretation_limits: list[str] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_unique_cases(self) -> OfficialMockGoldAudit:
        case_ids = [correction.case_id for correction in self.corrections]
        if len(case_ids) != len(set(case_ids)):
            raise ValueError("gold audit correction case IDs must be unique")
        return self


class LoadedOfficialMockGoldAudit(GoldAuditModel):
    audit: OfficialMockGoldAudit
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    resource_name: str


def load_official_mock_gold_audit() -> LoadedOfficialMockGoldAudit:
    resource = files("finance_agent_core.evaluation.suites").joinpath(_AUDIT_RESOURCE)
    raw = resource.read_bytes()
    return LoadedOfficialMockGoldAudit(
        audit=OfficialMockGoldAudit.model_validate_json(raw),
        sha256=hashlib.sha256(raw).hexdigest(),
        resource_name=_AUDIT_RESOURCE,
    )


def apply_official_mock_gold_audit(
    cases: Sequence[OfficialMockCase],
    *,
    source_suite_sha256: str,
    database_sha256_by_family: Mapping[str, str],
    active_case_ids: set[str] | None = None,
) -> tuple[list[OfficialMockCase], LoadedOfficialMockGoldAudit]:
    """Apply a hash-pinned overlay without mutating the historical source suite."""
    loaded = load_official_mock_gold_audit()
    audit = loaded.audit
    if audit.source_suite_sha256 != source_suite_sha256:
        raise ValueError("gold audit source suite SHA-256 differs")
    corrections = {correction.case_id: correction for correction in audit.corrections}
    source_by_id = {case.id: case for case in cases}
    if missing := sorted(set(corrections) - set(source_by_id)):
        raise ValueError(f"gold audit references missing source cases: {missing}")

    corrected: list[OfficialMockCase] = []
    for case in cases:
        correction = corrections.get(case.id)
        if correction is None or (active_case_ids is not None and case.id not in active_case_ids):
            corrected.append(case)
            continue
        if case.coverage_family is not correction.product_family:
            raise ValueError(f"gold audit family differs for {case.id}")
        observed_database_sha256 = database_sha256_by_family.get(correction.product_family.value)
        if observed_database_sha256 != correction.database_sha256:
            raise ValueError(f"gold audit database SHA-256 differs for {case.id}")
        expectation = case.expectation.model_copy(
            update={
                "candidate_count": correction.candidate_count,
                "product_ids": list(correction.product_ids),
            }
        )
        corrected.append(case.model_copy(update={"expectation": expectation}))
    return corrected, loaded
