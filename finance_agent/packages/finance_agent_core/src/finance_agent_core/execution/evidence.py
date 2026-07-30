from __future__ import annotations

from datetime import date
from decimal import Decimal

from finance_agent_core.config import (
    AsOfBasis,
    QualityStatus,
    load_field_registry,
)
from finance_agent_core.contracts.queryplan import QueryPlan
from finance_agent_core.domain import (
    FieldEvidence,
    NormalizedProductRecord,
    ProductEvidence,
    VerifiedSearch,
)


def _evidence_scalar(value: object) -> str | int | bool | None:
    if value is None or isinstance(value, (str, int, bool)):
        return value
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, date):
        return value.isoformat()
    raise TypeError(f"unsupported evidence value: {value!r}")


def _as_of(record: NormalizedProductRecord, basis: AsOfBasis) -> date:
    if basis is AsOfBasis.DYNAMIC:
        return record.dynamic_as_of
    if basis is AsOfBasis.SNAPSHOT:
        return record.source_snapshot_date
    return record.static_as_of


def _quality(
    record: NormalizedProductRecord,
    field_name: str,
) -> tuple[QualityStatus, str | None]:
    override, reason = record.row_level_quality(field_name)
    if override is not None:
        return override, reason
    registry = load_field_registry()
    definition = registry.require_field(field_name, [record.product_family])
    value = record.canonical_value(field_name)
    if value is None:
        return QualityStatus.UNKNOWN, f"{field_name}_missing"
    return (
        definition.quality,
        None if definition.quality is QualityStatus.VALID else definition.notes,
    )


def _evidence_fields(plan: QueryPlan) -> list[str]:
    fields: list[str] = []
    for name in [
        *plan.projection,
        *(constraint.field for constraint in plan.constraints),
        *(ranking.field for ranking in plan.ranking),
        *plan.intent_payload.comparison_fields,
    ]:
        if name not in fields:
            fields.append(name)
    return fields


def build_product_evidence(
    plan: QueryPlan,
    verified: VerifiedSearch,
) -> list[ProductEvidence]:
    registry = load_field_registry()
    evidence_fields = _evidence_fields(plan)
    products: list[ProductEvidence] = []
    for record in verified.records:
        fields: list[FieldEvidence] = []
        for field_name in evidence_fields:
            definition = registry.require_field(field_name, [record.product_family])
            source_columns = definition.source.columns
            quality, reason = _quality(record, field_name)
            fields.append(
                FieldEvidence(
                    canonical_field=field_name,
                    source_dataset=record.source_dataset,
                    source_id=record.source_id,
                    source_key={
                        column: str(record.source_values.get(column))
                        for column in registry.datasets[record.product_family].primary_key
                    },
                    source_row=record.source_row,
                    source_columns=source_columns,
                    raw_values={
                        column: record.source_values.get(column) for column in source_columns
                    },
                    normalized_value=_evidence_scalar(record.canonical_value(field_name)),
                    unit=definition.unit,
                    as_of=_as_of(record, definition.as_of_basis),
                    quality=quality,
                    quality_reason=reason,
                )
            )
        products.append(
            ProductEvidence(
                product_id=record.product_id,
                product_name=record.product_name,
                ticker=getattr(record, "ticker", None),
                fields=fields,
            )
        )
    return products
