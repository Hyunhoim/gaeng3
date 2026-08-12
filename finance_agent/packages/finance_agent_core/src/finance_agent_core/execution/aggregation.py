from __future__ import annotations

from collections.abc import Iterable
from datetime import date
from decimal import ROUND_HALF_EVEN, Decimal, localcontext

from finance_agent_core.config import AsOfBasis, QualityStatus, load_field_registry
from finance_agent_core.contracts.queryplan import AggregateFunction, QueryPlan
from finance_agent_core.deadline import raise_if_request_stopped
from finance_agent_core.domain import (
    AggregateEvidence,
    AggregateGroup,
    AggregateGroupKey,
    AggregateMetric,
    VerifiedAggregation,
)
from finance_agent_core.execution.verification_types import VerifierRecord

_EXCLUDED_QUALITIES = {
    QualityStatus.UNKNOWN,
    QualityStatus.INVALID,
    QualityStatus.UNSUPPORTED,
}
_AVERAGE_QUANTUM = Decimal("0.000000000001")
_DEADLINE_CHECK_INTERVAL = 256


def _periodic_deadline_check(index: int) -> None:
    if index % _DEADLINE_CHECK_INTERVAL == 0:
        raise_if_request_stopped()


def _field_quality(
    record: VerifierRecord,
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
    record: VerifierRecord,
    field_name: str,
) -> date:
    definition = load_field_registry().require_field(field_name, [record.product_family])
    if definition.as_of_basis is AsOfBasis.DYNAMIC:
        return record.dynamic_as_of
    if definition.as_of_basis is AsOfBasis.SNAPSHOT:
        return record.source_snapshot_date
    return record.static_as_of


def _usable_value(
    record: VerifierRecord,
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
    largest_digits = 0
    for index, value in enumerate(values):
        _periodic_deadline_check(index)
        largest_digits = max(largest_digits, len(value.as_tuple().digits))
    raise_if_request_stopped()
    with localcontext() as context:
        context.prec = max(50, largest_digits + 20)
        total = Decimal("0")
        for index, value in enumerate(values):
            _periodic_deadline_check(index)
            total += value
        return (total / Decimal(len(values))).quantize(
            _AVERAGE_QUANTUM,
            rounding=ROUND_HALF_EVEN,
        )


def _reduce_numeric(
    function: AggregateFunction,
    values: list[Decimal],
) -> Decimal:
    if function is AggregateFunction.AVG:
        return _average(values)
    if function is AggregateFunction.SUM:
        reduced = Decimal("0")
        for index, value in enumerate(values):
            _periodic_deadline_check(index)
            reduced += value
        return reduced
    reduced = values[0]
    for index, value in enumerate(values):
        if index == 0:
            continue
        _periodic_deadline_check(index)
        if function is AggregateFunction.MIN:
            reduced = min(reduced, value)
        elif function is AggregateFunction.MAX:
            reduced = max(reduced, value)
        else:  # pragma: no cover - enum exhaustiveness guard
            raise ValueError(f"unsupported aggregate function: {function}")
    return reduced


def _group_key(
    record: VerifierRecord,
    fields: list[str],
) -> tuple[str | int | bool | None, ...]:
    return tuple(_evidence_scalar(_usable_value(record, field)) for field in fields)


def _group_sort_key(
    item: tuple[tuple[str | int | bool | None, ...], list[VerifierRecord]],
) -> tuple[object, ...]:
    raise_if_request_stopped()
    key, records = item
    rendered = tuple(
        (value is None, type(value).__name__, "" if value is None else str(value).casefold())
        for value in key
    )
    return (-len(records), *rendered)


def _aggregate_quality(
    *,
    field_name: str,
    records: list[VerifierRecord],
    valid_records: list[VerifierRecord],
) -> tuple[QualityStatus, str | None]:
    raise_if_request_stopped()
    if not valid_records:
        return QualityStatus.UNKNOWN, "all_values_missing_or_unusable"
    statuses: list[QualityStatus] = []
    as_of_dates: set[date] = set()
    for index, record in enumerate(valid_records):
        _periodic_deadline_check(index)
        statuses.append(_field_quality(record, field_name)[0])
        as_of_dates.add(_field_as_of(record, field_name))
    raise_if_request_stopped()
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
    records: list[VerifierRecord],
    *,
    function: AggregateFunction,
    field_name: str,
    group_values: dict[str, str | int | bool | None],
) -> AggregateMetric:
    raise_if_request_stopped()
    registry = load_field_registry()
    family = plan.product_families[0].value
    definition = registry.require_field(field_name, [family])
    valid_records: list[VerifierRecord] = []
    for index, record in enumerate(records):
        _periodic_deadline_check(index)
        if _usable_value(record, field_name) is not None:
            valid_records.append(record)
    raise_if_request_stopped()

    currency_unknown = (
        function is not AggregateFunction.COUNT
        and definition.unit == "source_currency_amount"
        and "trading_currency" in group_values
        and group_values["trading_currency"] is None
    )
    if currency_unknown:
        valid_records = []

    values: list[object] = []
    for index, record in enumerate(valid_records):
        _periodic_deadline_check(index)
        value = record.canonical_value(field_name)
        assert value is not None
        values.append(value)
    raise_if_request_stopped()
    if function is AggregateFunction.COUNT:
        value: str | int | None = len(valid_records)
    elif not values:
        value = None
    else:
        numeric: list[Decimal] = []
        for index, item in enumerate(values):
            _periodic_deadline_check(index)
            numeric.append(_decimal_value(item))
        raise_if_request_stopped()
        reduced = _reduce_numeric(function, numeric)
        value = _decimal_text(reduced)

    quality, quality_reason = _aggregate_quality(
        field_name=field_name,
        records=records,
        valid_records=valid_records,
    )
    if currency_unknown:
        quality_reason = "trading_currency_unknown_for_amount_group"
    metric_as_of_dates: list[date] = []
    for index, record in enumerate(valid_records):
        _periodic_deadline_check(index)
        metric_as_of_dates.append(_field_as_of(record, field_name))
    return AggregateMetric(
        function=function,
        field=field_name,
        value=value,
        unit=definition.unit,
        valid_count=len(valid_records),
        missing_count=len(records) - len(valid_records),
        as_of_start=min(metric_as_of_dates) if metric_as_of_dates else None,
        as_of_end=max(metric_as_of_dates) if metric_as_of_dates else None,
        quality=quality,
        quality_reason=quality_reason,
    )


def aggregate_records(
    plan: QueryPlan,
    records: Iterable[VerifierRecord],
) -> tuple[int, list[AggregateGroup]]:
    """Reduce already-filtered rows using exact Decimal arithmetic.

    Groups are ordered by row count descending and then by canonical group values.
    ``plan.limit`` limits returned groups, while the first tuple item preserves the
    full group count for truncation disclosure.
    """

    raise_if_request_stopped()
    selected: list[VerifierRecord] = []
    for index, record in enumerate(records):
        _periodic_deadline_check(index)
        selected.append(record)
    raise_if_request_stopped()
    if not selected:
        return 0, []
    grouped: dict[
        tuple[str | int | bool | None, ...],
        list[VerifierRecord],
    ] = {}
    group_fields = plan.intent_payload.group_by
    for index, record in enumerate(selected):
        _periodic_deadline_check(index)
        key = _group_key(record, group_fields)
        grouped.setdefault(key, []).append(record)

    groups: list[AggregateGroup] = []
    for group_index, (key, group_records) in enumerate(
        sorted(grouped.items(), key=_group_sort_key)
    ):
        _periodic_deadline_check(group_index)
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
    raise_if_request_stopped()
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
