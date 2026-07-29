from __future__ import annotations

from datetime import date
from enum import StrEnum
from functools import lru_cache
from importlib.resources import files
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator


class QualityStatus(StrEnum):
    VALID = "VALID"
    PARTIAL = "PARTIAL"
    UNKNOWN = "UNKNOWN"
    INVALID = "INVALID"
    STALE = "STALE"
    UNSUPPORTED = "UNSUPPORTED"


class ValueType(StrEnum):
    STRING = "string"
    NUMBER = "number"
    BOOLEAN = "boolean"
    DATE = "date"
    ENUM = "enum"


class AsOfBasis(StrEnum):
    STATIC = "static"
    DYNAMIC = "dynamic"
    SNAPSHOT = "snapshot"


class SourceTransform(StrEnum):
    IDENTITY = "identity"
    COMPOSITE_KEY = "composite_key"
    CONSTANT = "constant"
    BOOLEAN_CODE = "boolean_code"
    CODE_MAP = "code_map"
    DAYS_FROM_SNAPSHOT = "days_from_snapshot"
    AVAILABLE_ON_SNAPSHOT = "available_on_snapshot"


class SourceDefinition(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    dataset: str
    columns: list[str] = Field(default_factory=list)
    transform: SourceTransform = SourceTransform.IDENTITY
    constant_value: str | None = None
    value_map: dict[str, str | int | bool] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_transform_inputs(self) -> SourceDefinition:
        if self.transform in {
            SourceTransform.IDENTITY,
            SourceTransform.BOOLEAN_CODE,
            SourceTransform.CODE_MAP,
            SourceTransform.DAYS_FROM_SNAPSHOT,
        }:
            if len(self.columns) != 1:
                raise ValueError(f"{self.transform} requires exactly one source column")
        elif self.transform is SourceTransform.COMPOSITE_KEY:
            if len(self.columns) < 2:
                raise ValueError("composite_key requires at least two source columns")
        elif self.transform is SourceTransform.AVAILABLE_ON_SNAPSHOT:
            if len(self.columns) != 2:
                raise ValueError("available_on_snapshot requires quantity and maturity columns")
        elif self.transform is SourceTransform.CONSTANT:
            if self.columns or self.constant_value is None:
                raise ValueError("constant requires constant_value and no source columns")

        if self.transform in {SourceTransform.BOOLEAN_CODE, SourceTransform.CODE_MAP}:
            if not self.value_map:
                raise ValueError(f"{self.transform} requires a non-empty value_map")
        elif self.value_map:
            raise ValueError("value_map is only valid for boolean_code or code_map")
        if len(self.columns) != len(set(self.columns)):
            raise ValueError("source columns must be unique")
        return self


class DatasetProvenance(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    raw_file_pattern: str
    schema_file_pattern: str
    static_as_of_column: str
    dynamic_as_of_column: str


class DatasetDefinition(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    source_id: str
    source_label: str
    logical_grain: str
    primary_key: list[str] = Field(min_length=1)
    raw_primary_key: list[str] = Field(default_factory=list)
    row_count: int = Field(gt=0)
    logical_row_count: int | None = Field(default=None, gt=0)
    snapshot_date: date
    quarantined_rows: int = Field(ge=0)
    execution_enabled: bool = True
    notes: str = ""
    provenance: DatasetProvenance

    @model_validator(mode="after")
    def validate_primary_key(self) -> DatasetDefinition:
        if len(self.primary_key) != len(set(self.primary_key)):
            raise ValueError("primary_key fields must be unique")
        if len(self.raw_primary_key) != len(set(self.raw_primary_key)):
            raise ValueError("raw_primary_key fields must be unique")
        return self


class FieldDatasetOverride(BaseModel):
    """Dataset-specific provenance and capability overrides for a canonical field."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    source: SourceDefinition
    quality: QualityStatus | None = None
    coverage_pct: float | None = Field(default=None, ge=0, le=100)
    queryable: bool | None = None
    selectable: bool | None = None
    sortable: bool | None = None
    aggregatable: bool | None = None
    allowed_operators: list[str] | None = None
    comparison_scope: str | None = None
    as_of_basis: AsOfBasis | None = None
    sentinel_values: dict[str, str] | None = None
    notes: str | None = None


class FieldDefinition(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    label: str
    aliases: list[str] = Field(default_factory=list)
    datasets: list[str] = Field(min_length=1)
    value_type: ValueType
    unit: str = "none"
    source: SourceDefinition
    quality: QualityStatus
    coverage_pct: float = Field(ge=0, le=100)
    queryable: bool = False
    selectable: bool = True
    sortable: bool = False
    aggregatable: bool = False
    allowed_operators: list[str] = Field(default_factory=list)
    enum_values: list[str] = Field(default_factory=list)
    comparison_scope: str = "same_dataset"
    as_of_basis: AsOfBasis = AsOfBasis.STATIC
    sentinel_values: dict[str, str] = Field(default_factory=dict)
    notes: str
    dataset_overrides: dict[str, FieldDatasetOverride] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_capabilities(self) -> FieldDefinition:
        allowed = {
            "eq",
            "neq",
            "in",
            "not_in",
            "lt",
            "lte",
            "gt",
            "gte",
            "between",
            "contains",
        }
        unknown_operators = set(self.allowed_operators) - allowed
        if unknown_operators:
            raise ValueError(f"unknown operators: {sorted(unknown_operators)}")
        if self.queryable != bool(self.allowed_operators):
            raise ValueError("queryable and allowed_operators must agree")
        if self.value_type is ValueType.ENUM and not self.enum_values:
            raise ValueError("enum fields require enum_values")
        if self.value_type is not ValueType.ENUM and self.enum_values:
            raise ValueError("enum_values are only valid for enum fields")
        if len(self.enum_values) != len(set(self.enum_values)):
            raise ValueError("enum_values must be unique")
        if len(self.datasets) != len(set(self.datasets)):
            raise ValueError("datasets must be unique")
        if len(self.allowed_operators) != len(set(self.allowed_operators)):
            raise ValueError("allowed_operators must be unique")
        numeric_operators = {"lt", "lte", "gt", "gte", "between"}
        if numeric_operators & set(self.allowed_operators) and self.value_type not in {
            ValueType.NUMBER,
            ValueType.DATE,
        }:
            raise ValueError("range operators require a number or date field")
        if self.quality in {QualityStatus.INVALID, QualityStatus.UNSUPPORTED} and (
            self.queryable or self.sortable or self.aggregatable
        ):
            raise ValueError("invalid or unsupported fields cannot drive execution")
        if len(self.aliases) != len(set(self.aliases)):
            raise ValueError("aliases must be unique")
        supported_units = {
            "none",
            "code",
            "boolean",
            "pct_point",
            "source_currency_amount",
            "source_quantity",
            "day",
            "year",
            "date",
        }
        if self.unit not in supported_units:
            raise ValueError(f"unsupported unit: {self.unit}")
        unknown_sentinel_statuses = set(self.sentinel_values.values()) - {
            status.value for status in QualityStatus
        }
        if unknown_sentinel_statuses:
            raise ValueError(f"unknown sentinel statuses: {sorted(unknown_sentinel_statuses)}")
        if (
            self.source.transform is SourceTransform.BOOLEAN_CODE
            and self.value_type is not ValueType.BOOLEAN
        ):
            raise ValueError("boolean_code sources require a boolean field")
        if (
            self.source.transform is SourceTransform.COMPOSITE_KEY
            and self.value_type is not ValueType.STRING
        ):
            raise ValueError("composite_key sources require a string field")
        return self

    def resolve(self, dataset: str) -> FieldDefinition:
        override = self.dataset_overrides.get(dataset)
        if override is None:
            if self.source.dataset != dataset:
                raise ValueError(f"field has no source mapping for dataset: {dataset}")
            return self
        payload = self.model_dump(mode="python", exclude={"dataset_overrides"})
        payload.update(override.model_dump(mode="python", exclude_unset=True))
        payload["datasets"] = [dataset]
        payload["dataset_overrides"] = {}
        return FieldDefinition.model_validate(payload)


class FieldRegistry(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: str
    datasets: dict[str, DatasetDefinition]
    fields: dict[str, FieldDefinition]

    @model_validator(mode="after")
    def validate_references(self) -> FieldRegistry:
        for name, field_definition in self.fields.items():
            if field_definition.source.dataset not in self.datasets:
                raise ValueError(f"{name} references an unknown source dataset")
            unknown_datasets = set(field_definition.datasets) - set(self.datasets)
            if unknown_datasets:
                raise ValueError(
                    f"{name} is exposed for unknown datasets: {sorted(unknown_datasets)}"
                )
            if field_definition.source.dataset not in field_definition.datasets:
                raise ValueError(f"{name} source dataset is not in its datasets list")
            unknown_overrides = set(field_definition.dataset_overrides) - set(
                field_definition.datasets
            )
            if unknown_overrides:
                raise ValueError(
                    f"{name} overrides unexposed datasets: {sorted(unknown_overrides)}"
                )
            for dataset_name, override in field_definition.dataset_overrides.items():
                if override.source.dataset != dataset_name:
                    raise ValueError(
                        f"{name} override key and source dataset differ: {dataset_name}"
                    )
                field_definition.resolve(dataset_name)
            missing_sources = set(field_definition.datasets) - {
                field_definition.source.dataset,
                *field_definition.dataset_overrides,
            }
            if missing_sources:
                raise ValueError(
                    f"{name} has no source mapping for datasets: {sorted(missing_sources)}"
                )
        return self

    def require_dataset(self, product_family: str) -> DatasetDefinition:
        try:
            return self.datasets[product_family]
        except KeyError as error:
            raise ValueError(
                f"product family has no frozen field registry: {product_family}"
            ) from error

    def require_executable_dataset(self, product_family: str) -> DatasetDefinition:
        dataset = self.require_dataset(product_family)
        if not dataset.execution_enabled:
            raise ValueError(
                f"product family contract is frozen but execution is not enabled: {product_family}"
            )
        return dataset

    def executable_dataset_names(self) -> list[str]:
        return [name for name, definition in self.datasets.items() if definition.execution_enabled]

    def require_field(self, name: str, product_families: list[str]) -> FieldDefinition:
        try:
            field_definition = self.fields[name]
        except KeyError as error:
            raise ValueError(f"unknown canonical field: {name}") from error
        unsupported = set(product_families) - set(field_definition.datasets)
        if unsupported:
            raise ValueError(f"{name} is unavailable for product families: {sorted(unsupported)}")
        if len(product_families) != 1:
            raise ValueError("cross-family field resolution is not executable yet")
        return field_definition.resolve(product_families[0])

    def _field_datasets(self, definition: FieldDefinition, *, executable_only: bool) -> list[str]:
        if not executable_only:
            return definition.datasets
        executable = set(self.executable_dataset_names())
        return [dataset for dataset in definition.datasets if dataset in executable]

    def queryable_fields(self, *, executable_only: bool = False) -> list[str]:
        return sorted(
            name
            for name, definition in self.fields.items()
            if any(
                definition.resolve(dataset).queryable
                for dataset in self._field_datasets(definition, executable_only=executable_only)
            )
        )

    def selectable_fields(self, *, executable_only: bool = False) -> list[str]:
        return sorted(
            name
            for name, definition in self.fields.items()
            if any(
                definition.resolve(dataset).selectable
                for dataset in self._field_datasets(definition, executable_only=executable_only)
            )
        )

    def sortable_fields(self, *, executable_only: bool = False) -> list[str]:
        return sorted(
            name
            for name, definition in self.fields.items()
            if any(
                definition.resolve(dataset).sortable
                for dataset in self._field_datasets(definition, executable_only=executable_only)
            )
        )

    def aggregatable_fields(self, *, executable_only: bool = False) -> list[str]:
        return sorted(
            name
            for name, definition in self.fields.items()
            if any(
                definition.resolve(dataset).aggregatable
                for dataset in self._field_datasets(definition, executable_only=executable_only)
            )
        )


@lru_cache(maxsize=1)
def load_field_registry() -> FieldRegistry:
    resource = files("finance_agent_core.config").joinpath("field_registry.yaml")
    payload: Any = yaml.safe_load(resource.read_text(encoding="utf-8"))
    return FieldRegistry.model_validate(payload)
