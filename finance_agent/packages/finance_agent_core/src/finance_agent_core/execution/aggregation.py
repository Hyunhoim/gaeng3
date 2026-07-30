from __future__ import annotations

from collections.abc import Iterable
from datetime import date
from decimal import ROUND_HALF_EVEN, Decimal, localcontext

from finance_agent_core.config import AsOfBasis, QualityStatus, load_field_registry
from finance_agent_core.contracts.queryplan import AggregateFunction, QueryPlan
from finance_agent_core.domain import (
    AggregateEvidence,
    AggregateGroup,
    AggregateGroupKey,
    AggregateMetric,
    NormalizedProductRecord,
    VerifiedAggregation,
)

_EXCLUDED_QUALITIES = {
    QualityStatus.UNKNOWN,
    QualityStatus.INVALID,
    QualityStatus.UNSUPPORTED,
}
_AVERAGE_QUANTUM = Decimal("0.000000000001")


def _field_quality(
    record: NormalizedProductRecord,
    field_name: str,
) -> tuple[QualityStatus, str | None]:
    override, reason = record.row_level_quality(field_name)
    if override is not None:
        return override, reason
    definition = load_field_registry().require_field(field_name, [record.product_family])
    if record.canonical_value(field_name) is None:
        return QualityStatus.UNKNOWN, f"{field_name}_missing"
    return (
        definition.quality,
        None if definition.quality is QualityStatus.VALID else definition.notes,
    )


def _field_as_of(
    record: NormalizedProductRecord,
    field_name: str,
) -> date:
    definition = load_field_registry().require_field(field_name, [record.product_family])
    if definition.as_of_basis is AsOfBasis.DYNAMIC:
        return record.dynamic_as_of
    if definition.as_of_basis is AsOfBasis.SNAPSHOT:
        return record.source_snapshot_date
    return record.static_as_of


def _usable_value(
    record: NormalizedProductRecord,
    field_name: str,
) -> object | None:
    quality, _ = _field_quality(record, field_name)
    if quality in _EXCLUDED_QUALITIES:
        return None
    return record.canonical_value(field_name)


def _evidence_scalar(value: object | None) -> str | int | bool | None:
    if value is None or isinstance(value, (str, int, bool)):
        return value
    if isinstance(value, Decimal):
        return _decimal_text(value)
    if isinstance(value, date):
        return value.isoformat()
    raise TypeError(f"unsupported aggregate scalar: {value!r}")


def _decimal_value(value: object) -> Decimal:
    if isinstance(value, bool):
        raise TypeError("boolean values cannot drive numeric aggregation")
    if isinstance(value, Decimal):
        return value
    if isinstance(value, int):
        return Decimal(value)
    raise TypeError(f"aggregate field is not numeric: {value!r}")


def _decimal_text(value: Decimal) -> str:
    rendered = format(value, "f")
    if "." in rendered:
        rendered = rendered.rstrip("0").rstrip(".")
    return rendered or "0"


def _average(values: list[Decimal]) -> Decimal:
    largest_digits = max(len(value.as_tuple().digits) for value in values)
    with localcontext() as context:
        context.prec = max(50, largest_digits + 20)
        return (sum(values, Decimal("0")) / Decimal(len(values))).quantize(
            _AVERAGE_QUANTUM,
            rounding=ROUND_HALF_EVEN,
        )


def _group_key(
    record: NormalizedProductRecord,
    fields: list[str],
) -> tuple[str | int | bool | None, ...]:
    return tuple(_evidence_scalar(_usable_value(record, field)) for field in fields)


def _group_sort_key(
    item: tuple[tuple[str | int | bool | None, ...], list[NormalizedProductRecord]],
) -> tuple[object, ...]:
    key, records = item
    rendered = tuple(
        (value is None, type(value).__name__, "" if value is None else str(value).casefold())
        for value in key
    )
    return (-len(records), *rendered)


def _aggregate_quality(
    *,
    field_name: str,
    records: list[NormalizedProductRecord],
    valid_records: list[NormalizedProductRecord],
) -> tuple[QualityStatus, str | None]:
    if not valid_records:
        return QualityStatus.UNKNOWN, "all_values_missing_or_unusable"
    statuses = [_field_quality(record, field_name)[0] for record in valid_records]
    as_of_dates = {_field_as_of(record, field_name) for record in valid_records}
    reasons: list[str] = []
    missing_count = len(records) - len(valid_records)
    if missing_count:
        reasons.append(f"missing_or_unusable_values_excluded={missing_count}")
    if len(as_of_dates) > 1:
        reasons.append("mixed_as_of_dates")
    if QualityStatus.STALE in statuses:
        quality = QualityStatus.STALE
        reasons.append("one_or_more_inputs_stale")
    elif QualityStatus.PARTIAL in statuses or missing_count or len(as_of_dates) > 1:
        quality = QualityStatus.PARTIAL
    else:
        quality = QualityStatus.VALID
    return quality, "; ".join(reasons) or None


def _metric(
    plan: QueryPlan,
    records: list[NormalizedProductRecord],
    *,
    function: AggregateFunction,
    field_name: str,
    group_values: dict[str, str | int | bool | None],
) -> AggregateMetric:
    registry = load_field_registry()
    family = plan.product_families[0].value
    definition = registry.require_field(field_name, [family])
    valid_records = [record for record in records if _usable_value(record, field_name) is not None]

    currency_unknown = (
        function is not AggregateFunction.COUNT
        and definition.unit == "source_currency_amount"
        and "trading_currency" in group_values
        and group_values["trading_currency"] is None
    )
    if currency_unknown:
        valid_records = []

    values = [record.canonical_value(field_name) for record in valid_records]
    if function is AggregateFunction.COUNT:
        value: str | int | None = len(valid_records)
    elif not values:
        value = None
    else:
        numeric = [_decimal_value(item) for item in values]
        if function is AggregateFunction.MIN:
            reduced = min(numeric)
        elif function is AggregateFunction.MAX:
            reduced = max(numeric)
        elif function is AggregateFunction.SUM:
            reduced = sum(numeric, Decimal("0"))
        elif function is AggregateFunction.AVG:
            reduced = _average(numeric)
        else:  # pragma: no cover - enum exhaustiveness guard
            raise ValueError(f"unsupported aggregate function: {function}")
        value = _decimal_text(reduced)

    quality, quality_reason = _aggregate_quality(
        field_name=field_name,
        records=records,
        valid_records=valid_records,
    )
    if currency_unknown:
        quality_reason = "trading_currency_unknown_for_amount_group"
    as_of_dates = [_field_as_of(record, field_name) for record in valid_records]
    return AggregateMetric(
        function=function,
        field=field_name,
        value=value,
        unit=definition.unit,
        valid_count=len(valid_records),
        missing_count=len(records) - len(valid_records),
        as_of_start=min(as_of_dates) if as_of_dates else None,
        as_of_end=max(as_of_dates) if as_of_dates else None,
        quality=quality,
        quality_reason=quality_reason,
    )


def aggregate_records(
    plan: QueryPlan,
    records: Iterable[NormalizedProductRecord],
) -> tuple[int, list[AggregateGroup]]:
    """Reduce already-filtered rows using exact Decimal arithmetic.

    Groups are ordered by row count descending and then by canonical group values.
    ``plan.limit`` limits returned groups, while the first tuple item preserves the
    full group count for truncation disclosure.
    """

    selected = list(records)
    if not selected:
        return 0, []
    grouped: dict[
        tuple[str | int | bool | None, ...],
        list[NormalizedProductRecord],
    ] = {}
    group_fields = plan.intent_payload.group_by
    for record in selected:
        key = _group_key(record, group_fields)
        grouped.setdefault(key, []).append(record)

    groups: list[AggregateGroup] = []
    for key, group_records in sorted(grouped.items(), key=_group_sort_key):
        group_values = dict(zip(group_fields, key, strict=True))
        groups.append(
            AggregateGroup(
                keys=[
                    AggregateGroupKey(
                        field=field,
                        value=value,
                        unit=load_field_registry()
                        .require_field(field, [plan.product_families[0].value])
                        .unit,
                    )
                    for field, value in group_values.items()
                ],
                row_count=len(group_records),
                metrics=[
                    _metric(
                        plan,
                        group_records,
                        function=aggregation.function,
                        field_name=aggregation.field,
                        group_values=group_values,
                    )
                    for aggregation in plan.intent_payload.aggregations
                ],
            )
        )
    return len(groups), groups[: plan.limit]


def build_aggregate_evidence(
    plan: QueryPlan,
    verified: VerifiedAggregation,
) -> list[AggregateEvidence]:
    registry = load_field_registry()
    family = plan.product_families[0].value
    dataset = registry.require_dataset(family)
    evidence: list[AggregateEvidence] = []
    for group_index, group in enumerate(verified.groups, start=1):
        group_values = {key.field: key.value for key in group.keys}
        group_sources = {
            key.field: registry.require_field(key.field, [family]).source.columns
            for key in group.keys
        }
        for metric_index, metric in enumerate(group.metrics, start=1):
            definition = registry.require_field(metric.field, [family])
            evidence.append(
                AggregateEvidence(
                    evidence_id=(
                        f"aggregate_{group_index}_{metric_index}_"
                        f"{metric.function.value}_{metric.field}"
                    ),
                    function=metric.function,
                    field=metric.field,
                    label=definition.label,
                    value=metric.value,
                    unit=metric.unit,
                    group_values=group_values,
                    group_source_columns=group_sources,
                    row_count=group.row_count,
                    valid_count=metric.valid_count,
                    missing_count=metric.missing_count,
                    source_dataset=family,
                    source_id=dataset.source_id,
                    source_columns=definition.source.columns,
                    source_snapshot_date=verified.manifest.source_snapshot_date,
                    as_of_start=metric.as_of_start,
                    as_of_end=metric.as_of_end,
                    quality=metric.quality,
                    quality_reason=metric.quality_reason,
                )
            )
    return evidence
