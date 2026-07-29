from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from finance_agent_core.config import QualityStatus
from finance_agent_core.contracts.queryplan import QueryPlan

type RawScalar = str | int | bool | None
type EvidenceScalar = str | int | bool | None


class DomainModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class NormalizedOverseasEtpRecord(DomainModel):
    source_dataset: Literal["overseas_etp"] = "overseas_etp"
    source_id: Literal["PREF02N001"] = "PREF02N001"
    source_row: int = Field(ge=2)
    source_snapshot_date: date
    present_source_fields: int = Field(ge=0)
    is_quarantined: bool
    quarantine_reason: str | None
    row_quality: QualityStatus
    source_values: dict[str, RawScalar]

    product_id: str
    product_family: Literal["overseas_etp"] = "overseas_etp"
    product_type: Literal["ETF", "ETN"]
    product_name: str
    exchange_code: str
    ticker: str
    isin: str | None
    sellable: bool | None
    trading_suspended: bool | None
    asset_type: str | None
    investment_region: str | None
    total_expense_ratio_pct: Decimal = Field(ge=0)
    total_expense_ratio_quality: QualityStatus
    total_expense_ratio_quality_reason: str | None
    aum: Decimal | None = Field(default=None, ge=0)
    aum_quality: QualityStatus
    aum_quality_reason: str | None
    trading_currency: str
    static_as_of: date
    dynamic_as_of: date

    @model_validator(mode="after")
    def validate_identity_and_quality(self) -> NormalizedOverseasEtpRecord:
        expected_id = f"{self.exchange_code}:{self.ticker}"
        if self.product_id != expected_id:
            raise ValueError(f"product_id must be {expected_id}")
        if self.is_quarantined != (self.quarantine_reason is not None):
            raise ValueError("quarantine flag and reason must agree")
        if self.is_quarantined and self.row_quality is QualityStatus.VALID:
            raise ValueError("quarantined rows cannot have VALID row quality")
        if self.total_expense_ratio_pct == 0:
            if self.total_expense_ratio_quality is not QualityStatus.UNKNOWN:
                raise ValueError("zero expense ratio must remain UNKNOWN")
        elif self.total_expense_ratio_quality is not QualityStatus.VALID:
            raise ValueError("positive expense ratio must be VALID")
        if self.aum is None or self.aum == 0:
            if self.aum_quality is not QualityStatus.UNKNOWN:
                raise ValueError("missing or zero AUM must remain UNKNOWN")
        elif self.aum_quality is not QualityStatus.VALID:
            raise ValueError("positive AUM must be VALID")
        return self

    def canonical_value(self, field_name: str) -> object:
        if field_name == "total_expense_ratio_pct":
            return self.total_expense_ratio_pct
        return getattr(self, field_name)

    def row_level_quality(self, field_name: str) -> tuple[QualityStatus | None, str | None]:
        if field_name == "total_expense_ratio_pct":
            return (
                self.total_expense_ratio_quality,
                self.total_expense_ratio_quality_reason,
            )
        if field_name == "aum":
            return self.aum_quality, self.aum_quality_reason
        return None, None


class NormalizedDomesticEtpRecord(DomainModel):
    source_dataset: Literal["domestic_etp"] = "domestic_etp"
    source_id: Literal["PREF01N001"] = "PREF01N001"
    source_row: int = Field(ge=2)
    source_snapshot_date: date
    present_source_fields: int = Field(ge=0)
    is_quarantined: bool
    quarantine_reason: str | None
    row_quality: QualityStatus
    source_values: dict[str, RawScalar]

    product_id: str
    product_family: Literal["domestic_etp"] = "domestic_etp"
    product_type: Literal["ETF", "ETN"]
    product_name: str
    short_name: str
    exchange_code: str
    ticker: str
    isin: str
    sellable: bool | None
    trading_suspended: bool | None
    asset_type: str
    investment_region: str
    manager: str
    base_index: str | None
    strategy: str | None
    leverage_factor: Decimal | None
    risk_level: str
    pension_eligible: bool | None
    core_etf: bool | None
    total_expense_ratio_pct: Decimal | None = Field(default=None, ge=0)
    total_expense_ratio_quality: QualityStatus
    total_expense_ratio_quality_reason: str | None
    aum: Decimal | None = Field(default=None, ge=0)
    aum_quality: QualityStatus
    aum_quality_reason: str | None
    trading_currency: Literal["KRW"] = "KRW"
    close_price: Decimal | None = Field(default=None, ge=0)
    one_day_return_pct: Decimal | None
    one_month_return_pct: Decimal | None
    three_month_return_pct: Decimal | None
    six_month_return_pct: Decimal | None
    one_year_return_pct: Decimal | None
    ytd_return_pct: Decimal | None
    daily_trading_value: Decimal | None = Field(default=None, ge=0)
    static_as_of: date
    dynamic_as_of: date
    field_quality: dict[str, QualityStatus]
    field_quality_reasons: dict[str, str | None]

    @model_validator(mode="after")
    def validate_identity_and_quality(self) -> NormalizedDomesticEtpRecord:
        if not self.is_quarantined and self.product_id != self.isin:
            raise ValueError("domestic ETP product_id must equal source ISIN")
        if not self.is_quarantined and self.exchange_code != "EXG_MKT_NO_001":
            raise ValueError("domestic ETP exchange code is outside the frozen dataset")
        if self.is_quarantined != (self.quarantine_reason is not None):
            raise ValueError("quarantine flag and reason must agree")
        if self.is_quarantined and self.row_quality is QualityStatus.VALID:
            raise ValueError("quarantined rows cannot have VALID row quality")
        if self.total_expense_ratio_pct is None or self.total_expense_ratio_pct == 0:
            if self.total_expense_ratio_quality is not QualityStatus.UNKNOWN:
                raise ValueError("missing or zero expense ratio must remain UNKNOWN")
        elif self.total_expense_ratio_quality is not QualityStatus.VALID:
            raise ValueError("positive expense ratio must be VALID")
        if self.aum is None or self.aum == 0:
            if self.aum_quality is not QualityStatus.UNKNOWN:
                raise ValueError("missing or zero AUM must remain UNKNOWN")
        elif self.aum_quality is not QualityStatus.VALID:
            raise ValueError("positive AUM must be VALID")
        if set(self.field_quality) != set(self.field_quality_reasons):
            raise ValueError("field quality and reason keys must agree")
        return self

    def canonical_value(self, field_name: str) -> object:
        return getattr(self, field_name)

    def row_level_quality(self, field_name: str) -> tuple[QualityStatus | None, str | None]:
        if field_name == "total_expense_ratio_pct":
            return (
                self.total_expense_ratio_quality,
                self.total_expense_ratio_quality_reason,
            )
        if field_name == "aum":
            return self.aum_quality, self.aum_quality_reason
        return (
            self.field_quality.get(field_name),
            self.field_quality_reasons.get(field_name),
        )


class NormalizedBondRecord(DomainModel):
    source_dataset: Literal["bond"] = "bond"
    source_id: Literal["PRBD01N001"] = "PRBD01N001"
    source_row: int = Field(ge=2)
    source_snapshot_date: date
    present_source_fields: int = Field(ge=0)
    is_quarantined: bool = False
    quarantine_reason: str | None = None
    row_quality: QualityStatus
    source_values: dict[str, RawScalar]

    product_id: str
    product_family: Literal["bond"] = "bond"
    product_name: str
    ticker: str
    short_name: str | None
    bond_market: Literal["장내", "장외"]
    issuer: str | None
    bond_major_class: str
    bond_subclass: str | None
    bond_type: str | None
    trading_currency: str
    issue_amount: Decimal = Field(ge=0)
    issue_date: date | None
    maturity_date: date | None
    coupon_rate_pct: Decimal | None = Field(default=None, ge=0)
    credit_rating: str | None
    bond_risk_code: str
    buy_yield_pct: Decimal | None
    after_tax_yield_pct: Decimal | None
    buyable_quantity: Decimal | None = Field(default=None, ge=0)
    currently_buyable: bool | None
    remaining_days: int | None
    duration_years: Decimal | None = Field(default=None, ge=0)
    static_as_of: date
    dynamic_as_of: date
    field_quality: dict[str, QualityStatus]
    field_quality_reasons: dict[str, str | None]

    @model_validator(mode="after")
    def validate_identity_and_derived_fields(self) -> NormalizedBondRecord:
        if self.product_id != self.ticker:
            raise ValueError("bond product_id and ticker must both equal PD_NO")
        if self.is_quarantined != (self.quarantine_reason is not None):
            raise ValueError("quarantine flag and reason must agree")
        if self.is_quarantined and self.row_quality is QualityStatus.VALID:
            raise ValueError("quarantined rows cannot have VALID row quality")
        if set(self.field_quality) != set(self.field_quality_reasons):
            raise ValueError("field quality and reason keys must agree")

        expected_remaining = (
            None
            if self.maturity_date is None
            else (self.maturity_date - self.source_snapshot_date).days
        )
        if self.remaining_days != expected_remaining:
            raise ValueError("remaining_days must be recomputed from maturity and snapshot")
        expected_buyable = (
            None
            if self.buyable_quantity is None or self.maturity_date is None
            else self.buyable_quantity > 0 and self.maturity_date >= self.source_snapshot_date
        )
        if self.currently_buyable != expected_buyable:
            raise ValueError("currently_buyable must be derived fail-closed")
        return self

    def canonical_value(self, field_name: str) -> object:
        return getattr(self, field_name)

    def row_level_quality(self, field_name: str) -> tuple[QualityStatus | None, str | None]:
        return (
            self.field_quality.get(field_name),
            self.field_quality_reasons.get(field_name),
        )


type NormalizedEtpRecord = NormalizedOverseasEtpRecord | NormalizedDomesticEtpRecord
type NormalizedProductRecord = NormalizedEtpRecord | NormalizedBondRecord


class DatabaseManifest(DomainModel):
    schema_version: Literal["1.0"] = "1.0"
    dataset: Literal["overseas_etp", "domestic_etp", "bond"] = "overseas_etp"
    registry_schema_version: str
    source_file_name: str
    source_file_sha256: str
    source_file_size_bytes: int = Field(gt=0)
    source_snapshot_date: date
    total_rows: int = Field(gt=0)
    searchable_rows: int = Field(ge=0)
    quarantined_rows: int = Field(ge=0)

    @model_validator(mode="after")
    def validate_counts(self) -> DatabaseManifest:
        if self.searchable_rows + self.quarantined_rows != self.total_rows:
            raise ValueError("searchable and quarantined rows must equal total rows")
        return self


class ExecutedSearch(DomainModel):
    question_id: str
    candidate_count: int = Field(ge=0)
    records: list[NormalizedProductRecord]
    manifest: DatabaseManifest
    sql_template: str
    sql_parameters: list[str | int | float | bool]


class FieldEvidence(DomainModel):
    canonical_field: str
    source_dataset: str
    source_id: str
    source_key: dict[str, str]
    source_row: int
    source_columns: list[str]
    raw_values: dict[str, RawScalar]
    normalized_value: EvidenceScalar
    unit: str
    as_of: date
    quality: QualityStatus
    quality_reason: str | None


class ProductEvidence(DomainModel):
    product_id: str
    product_name: str
    ticker: str
    fields: list[FieldEvidence]


class VerifiedSearch(DomainModel):
    question_id: str
    candidate_count: int = Field(ge=0)
    records: list[NormalizedProductRecord]
    manifest: DatabaseManifest
    verifier_version: Literal["1.0"] = "1.0"


class AgentResponse(DomainModel):
    request_id: str
    provider: Literal["mock", "local_test"]
    answer: str
    query_plan: QueryPlan
    candidate_count: int = Field(ge=0)
    products: list[ProductEvidence]
    warnings: list[str]
    source_manifest: DatabaseManifest
