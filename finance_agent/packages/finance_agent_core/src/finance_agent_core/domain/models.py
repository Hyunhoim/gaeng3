from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from finance_agent_core.config import QualityStatus
from finance_agent_core.contracts.queryplan import AggregateFunction, QueryPlan

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


class NormalizedPublicFundRecord(DomainModel):
    source_dataset: Literal["fund"] = "fund"
    source_id: Literal["PRFD01N001"] = "PRFD01N001"
    source_row: int = Field(ge=2)
    source_snapshot_date: date
    present_source_fields: int = Field(ge=0)
    is_quarantined: Literal[False] = False
    quarantine_reason: None = None
    row_quality: Literal[QualityStatus.VALID] = QualityStatus.VALID
    source_values: dict[str, RawScalar]
    attribute_count: int = Field(ge=1)

    product_id: str
    product_family: Literal["fund"] = "fund"
    product_name: str
    short_name: str
    public_offering: bool | None
    sellable: bool
    company_sellable: bool | None
    trading_currency: str
    investment_region: str | None
    fund_geography_scope: str | None
    fund_management_attribute: str | None
    investor_type: str | None
    currency_hedged: bool | None
    risk_level: str | None
    aum: Decimal | None = Field(default=None, ge=0)
    base_index: str | None
    one_week_return_pct: Decimal | None
    one_month_return_pct: Decimal | None
    three_month_return_pct: Decimal | None
    six_month_return_pct: Decimal | None
    eighteen_month_return_pct: Decimal | None
    one_year_return_pct: Decimal | None
    two_year_return_pct: Decimal | None
    three_year_return_pct: Decimal | None
    five_year_return_pct: Decimal | None
    static_as_of: date
    dynamic_as_of: date
    field_quality: dict[str, QualityStatus]
    field_quality_reasons: dict[str, str | None]

    @model_validator(mode="after")
    def validate_quality_contract(self) -> NormalizedPublicFundRecord:
        if set(self.field_quality) != set(self.field_quality_reasons):
            raise ValueError("field quality and reason keys must agree")
        if self.aum is None or self.aum == 0:
            if self.field_quality.get("aum") is not QualityStatus.UNKNOWN:
                raise ValueError("missing or zero fund AUM must remain UNKNOWN")
        elif self.field_quality.get("aum") is not QualityStatus.VALID:
            raise ValueError("positive fund AUM must be VALID")
        if self.fund_management_attribute is None:
            if self.field_quality.get("fund_management_attribute") is not QualityStatus.UNKNOWN:
                raise ValueError("missing or code 06 fund attribute must remain UNKNOWN")
        for field_name in (
            "one_week_return_pct",
            "one_month_return_pct",
            "three_month_return_pct",
            "six_month_return_pct",
        ):
            value = getattr(self, field_name)
            quality = self.field_quality.get(field_name)
            expected = QualityStatus.UNKNOWN if value is None else QualityStatus.PARTIAL
            if quality is not expected:
                raise ValueError(f"{field_name} does not match the short-return contract")
        for field_name in (
            "eighteen_month_return_pct",
            "two_year_return_pct",
            "three_year_return_pct",
            "five_year_return_pct",
        ):
            if self.field_quality.get(field_name) is not QualityStatus.UNKNOWN:
                raise ValueError(f"{field_name} must remain display-only UNKNOWN")
        one_year_quality = self.field_quality.get("one_year_return_pct")
        expected_one_year_quality = (
            QualityStatus.UNKNOWN if self.one_year_return_pct is None else QualityStatus.PARTIAL
        )
        if one_year_quality is not expected_one_year_quality:
            raise ValueError("one_year_return_pct does not match the raw-source contract")
        return self

    def canonical_value(self, field_name: str) -> object:
        return getattr(self, field_name)

    def row_level_quality(self, field_name: str) -> tuple[QualityStatus | None, str | None]:
        return (
            self.field_quality.get(field_name),
            self.field_quality_reasons.get(field_name),
        )


class NormalizedPublicFundAttribute(DomainModel):
    source_dataset: Literal["fund"] = "fund"
    source_id: Literal["PRFD01N001"] = "PRFD01N001"
    source_row: int = Field(ge=2)
    product_id: str
    attribute_code: str
    quality: Literal[QualityStatus.UNKNOWN] = QualityStatus.UNKNOWN
    quality_reason: Literal["attribute_codebook_unconfirmed"] = "attribute_codebook_unconfirmed"


class QuarantinedPublicFundRow(DomainModel):
    source_dataset: Literal["fund"] = "fund"
    source_id: Literal["PRFD01N001"] = "PRFD01N001"
    source_row: int = Field(ge=2)
    source_snapshot_date: date
    present_source_fields: int = Field(ge=0)
    raw_item_number: str | None
    raw_attribute_code: str | None
    quarantine_reason: str
    row_quality: Literal[QualityStatus.INVALID] = QualityStatus.INVALID
    source_values: dict[str, RawScalar]


type NormalizedEtpRecord = NormalizedOverseasEtpRecord | NormalizedDomesticEtpRecord
type NormalizedProductRecord = (
    NormalizedEtpRecord | NormalizedBondRecord | NormalizedPublicFundRecord
)


class DatabaseManifest(DomainModel):
    schema_version: Literal["1.0", "1.1"] = "1.0"
    dataset: Literal["overseas_etp", "domestic_etp", "bond", "fund"] = "overseas_etp"
    registry_schema_version: str
    source_file_name: str
    source_file_sha256: str
    source_file_size_bytes: int = Field(gt=0)
    source_snapshot_date: date
    total_rows: int = Field(gt=0)
    searchable_rows: int = Field(ge=0)
    quarantined_rows: int = Field(ge=0)
    logical_product_rows: int | None = Field(default=None, gt=0)
    attribute_rows: int | None = Field(default=None, ge=0)
    scope_excluded_rows: int | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def validate_counts(self) -> DatabaseManifest:
        if self.dataset != "fund":
            if any(
                value is not None
                for value in (
                    self.logical_product_rows,
                    self.attribute_rows,
                    self.scope_excluded_rows,
                )
            ):
                raise ValueError("fund-specific manifest counts require dataset fund")
            if self.searchable_rows + self.quarantined_rows != self.total_rows:
                raise ValueError("searchable and quarantined rows must equal total rows")
            return self

        if self.schema_version != "1.1":
            raise ValueError("fund manifest requires schema version 1.1")
        if (
            self.logical_product_rows is None
            or self.attribute_rows is None
            or self.scope_excluded_rows is None
        ):
            raise ValueError("fund manifest requires logical, attribute, and scope counts")
        if self.attribute_rows + self.quarantined_rows != self.total_rows:
            raise ValueError("fund attribute and quarantined rows must equal raw total rows")
        if self.searchable_rows + self.scope_excluded_rows != self.logical_product_rows:
            raise ValueError("fund searchable and scope-excluded rows must equal logical products")
        return self


class ExecutedSearch(DomainModel):
    question_id: str
    candidate_count: int = Field(ge=0)
    records: list[NormalizedProductRecord]
    manifest: DatabaseManifest
    sql_template: str
    sql_parameters: list[str | int | float | bool]


class AggregateGroupKey(DomainModel):
    field: str
    value: EvidenceScalar
    unit: str


class AggregateMetric(DomainModel):
    function: AggregateFunction
    field: str
    value: str | int | None
    unit: str
    valid_count: int = Field(ge=0)
    missing_count: int = Field(ge=0)
    as_of_start: date | None
    as_of_end: date | None
    quality: QualityStatus
    quality_reason: str | None

    @model_validator(mode="after")
    def validate_metric_dates(self) -> AggregateMetric:
        if (self.as_of_start is None) != (self.as_of_end is None):
            raise ValueError("aggregate metric date bounds must both be present or absent")
        if (
            self.as_of_start is not None
            and self.as_of_end is not None
            and self.as_of_start > self.as_of_end
        ):
            raise ValueError("aggregate metric date bounds are reversed")
        if self.valid_count == 0 and self.quality is not QualityStatus.UNKNOWN:
            raise ValueError("aggregate metric without valid values must be UNKNOWN")
        return self


class AggregateGroup(DomainModel):
    keys: list[AggregateGroupKey]
    row_count: int = Field(ge=0)
    metrics: list[AggregateMetric]

    @model_validator(mode="after")
    def validate_group_counts(self) -> AggregateGroup:
        key_fields = [key.field for key in self.keys]
        if len(key_fields) != len(set(key_fields)):
            raise ValueError("aggregate group key fields must be unique")
        metric_keys = [(metric.function, metric.field) for metric in self.metrics]
        if len(metric_keys) != len(set(metric_keys)):
            raise ValueError("aggregate metrics must be unique within a group")
        for metric in self.metrics:
            if metric.valid_count + metric.missing_count != self.row_count:
                raise ValueError("aggregate metric counts must equal the group row count")
            if metric.function is AggregateFunction.COUNT:
                if not isinstance(metric.value, int) or metric.value != metric.valid_count:
                    raise ValueError("count metric value must equal valid_count")
            elif metric.value is not None and not isinstance(metric.value, str):
                raise ValueError("numeric aggregate values must use exact decimal strings")
        return self


class ExecutedAggregation(DomainModel):
    question_id: str
    candidate_count: int = Field(ge=0)
    total_group_count: int = Field(ge=0)
    groups: list[AggregateGroup]
    manifest: DatabaseManifest
    sql_template: str
    sql_parameters: list[str | int | float | bool]

    @model_validator(mode="after")
    def validate_group_window(self) -> ExecutedAggregation:
        if len(self.groups) > self.total_group_count:
            raise ValueError("returned aggregate groups exceed total_group_count")
        if self.candidate_count == 0 and (self.total_group_count or self.groups):
            raise ValueError("empty aggregate candidates cannot contain groups")
        return self


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
    ticker: str | None
    fields: list[FieldEvidence]


class ComparisonCellEvidence(DomainModel):
    target_index: Literal[1, 2]
    product_id: str
    product_name: str | None
    value: EvidenceScalar
    trading_currency: str | None
    quality: QualityStatus | None
    quality_reason: str | None
    as_of: date | None
    source_dataset: str | None
    source_id: str | None
    source_row: int | None
    source_columns: list[str]
    evidence_ref: str | None


class ComparisonEvidence(DomainModel):
    canonical_field: str
    label: str
    unit: str
    status: Literal[
        "numeric_delta",
        "value_only",
        "currency_mismatch",
        "as_of_mismatch",
        "stale_input",
        "unavailable",
        "incomplete",
    ]
    delta: str | None
    delta_basis: Literal["second_minus_first"] | None
    reason: str | None
    cells: list[ComparisonCellEvidence] = Field(min_length=2, max_length=2)

    @model_validator(mode="after")
    def validate_comparison_cells(self) -> ComparisonEvidence:
        if [cell.target_index for cell in self.cells] != [1, 2]:
            raise ValueError("comparison cells must preserve target order 1, 2")
        if self.delta is None and self.delta_basis is not None:
            raise ValueError("comparison without delta cannot expose a delta basis")
        if self.delta is not None and self.delta_basis != "second_minus_first":
            raise ValueError("comparison delta requires second_minus_first basis")
        return self


class AggregateEvidence(DomainModel):
    evidence_id: str
    function: AggregateFunction
    field: str
    label: str
    value: str | int | None
    unit: str
    group_values: dict[str, EvidenceScalar]
    group_source_columns: dict[str, list[str]]
    row_count: int = Field(ge=0)
    valid_count: int = Field(ge=0)
    missing_count: int = Field(ge=0)
    source_dataset: str
    source_id: str
    source_columns: list[str]
    source_snapshot_date: date
    as_of_start: date | None
    as_of_end: date | None
    quality: QualityStatus
    quality_reason: str | None

    @model_validator(mode="after")
    def validate_evidence_counts_and_dates(self) -> AggregateEvidence:
        if self.valid_count + self.missing_count != self.row_count:
            raise ValueError("aggregate evidence counts must equal row_count")
        if (self.as_of_start is None) != (self.as_of_end is None):
            raise ValueError("aggregate evidence date bounds must both be present or absent")
        if (
            self.as_of_start is not None
            and self.as_of_end is not None
            and self.as_of_start > self.as_of_end
        ):
            raise ValueError("aggregate evidence date bounds are reversed")
        return self


class VerifiedSearch(DomainModel):
    question_id: str
    candidate_count: int = Field(ge=0)
    records: list[NormalizedProductRecord]
    manifest: DatabaseManifest
    verifier_version: Literal["1.0"] = "1.0"


class VerifiedAggregation(DomainModel):
    question_id: str
    candidate_count: int = Field(ge=0)
    total_group_count: int = Field(ge=0)
    groups: list[AggregateGroup]
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
