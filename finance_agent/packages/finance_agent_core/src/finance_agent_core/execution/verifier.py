from __future__ import annotations

from datetime import date
from decimal import ROUND_HALF_EVEN, Decimal, localcontext
from functools import cmp_to_key

from finance_agent_core.config import AsOfBasis, QualityStatus, load_field_registry
from finance_agent_core.contracts.queryplan import (
    AggregateFunction,
    Constraint,
    ConstraintOperator,
    Intent,
    NullPlacement,
    QueryPlan,
    Ranking,
    SortDirection,
)
from finance_agent_core.domain import (
    AggregateGroup,
    AggregateGroupKey,
    AggregateMetric,
    ExecutedAggregation,
    ExecutedSearch,
    VerifiedAggregation,
    VerifiedSearch,
)
from finance_agent_core.execution.policy import (
    comparison_product_ids,
    require_aggregate_contract,
    require_comparison_contract,
    require_fund_public_scope,
)
from finance_agent_core.execution.verification_types import VerifierRecord


class ResultVerificationError(ValueError):
    """Raised when SQL output differs from the independent Python oracle."""


_AVERAGE_QUANTUM = Decimal("0.000000000001")


def _comparable_value(record: VerifierRecord, field_name: str) -> object | None:
    quality, _ = record.row_level_quality(field_name)
    if quality in {
        QualityStatus.UNKNOWN,
        QualityStatus.INVALID,
        QualityStatus.UNSUPPORTED,
    }:
        return None
    return record.canonical_value(field_name)


def _expected_value(record_value: object, query_value: object) -> object:
    if isinstance(record_value, Decimal):
        if isinstance(query_value, bool) or not isinstance(query_value, (int, float)):
            raise ResultVerificationError("numeric field received non-numeric query value")
        return Decimal(str(query_value))
    if isinstance(record_value, date):
        if not isinstance(query_value, str):
            raise ResultVerificationError("date field received non-string query value")
        return date.fromisoformat(query_value)
    return query_value


def _matches_constraint(
    record: VerifierRecord,
    constraint: Constraint,
) -> bool:
    actual = _comparable_value(record, constraint.field)
    if actual is None:
        return False
    operator = constraint.operator
    expected = constraint.value

    if operator in {ConstraintOperator.IN, ConstraintOperator.NOT_IN}:
        assert isinstance(expected, list)
        converted = [_expected_value(actual, value) for value in expected]
        is_member = actual in converted
        return is_member if operator is ConstraintOperator.IN else not is_member
    if operator is ConstraintOperator.BETWEEN:
        assert isinstance(expected, list)
        lower, upper = (_expected_value(actual, value) for value in expected)
        return lower <= actual <= upper
    if operator is ConstraintOperator.CONTAINS:
        assert isinstance(actual, str)
        assert isinstance(expected, str)
        return expected.casefold() in actual.casefold()

    assert not isinstance(expected, list)
    converted = _expected_value(actual, expected)
    if operator is ConstraintOperator.EQ:
        return actual == converted
    if operator is ConstraintOperator.NEQ:
        return actual != converted
    if operator is ConstraintOperator.LT:
        return actual < converted
    if operator is ConstraintOperator.LTE:
        return actual <= converted
    if operator is ConstraintOperator.GT:
        return actual > converted
    if operator is ConstraintOperator.GTE:
        return actual >= converted
    raise ResultVerificationError(f"unsupported operator: {operator}")


def _compare_values(
    left: object | None,
    right: object | None,
    ranking: Ranking,
) -> int:
    if left is None and right is None:
        return 0
    if left is None:
        return -1 if ranking.nulls is NullPlacement.FIRST else 1
    if right is None:
        return 1 if ranking.nulls is NullPlacement.FIRST else -1
    comparison = (left > right) - (left < right)
    return -comparison if ranking.direction is SortDirection.DESC else comparison


def _record_comparator(plan: QueryPlan):
    def compare(
        left: VerifierRecord,
        right: VerifierRecord,
    ) -> int:
        for ranking in plan.ranking:
            comparison = _compare_values(
                _comparable_value(left, ranking.field),
                _comparable_value(right, ranking.field),
                ranking,
            )
            if comparison:
                return comparison
        return (left.product_id > right.product_id) - (left.product_id < right.product_id)

    return compare


class ResultVerifier:
    def verify(
        self,
        plan: QueryPlan,
        executed: ExecutedSearch,
        universe: list[VerifierRecord] | None = None,
    ) -> VerifiedSearch:
        if plan.intent is Intent.COMPARE:
            require_comparison_contract(plan)
        if executed.question_id != plan.question_id:
            raise ResultVerificationError("question_id changed during execution")
        if executed.manifest.dataset != plan.product_families[0].value:
            raise ResultVerificationError("search manifest dataset differs from the plan")
        if plan.intent is Intent.COMPARE:
            requested_ids = comparison_product_ids(plan)
            actual_ids = [record.product_id for record in executed.records]
            if executed.candidate_count != len(actual_ids):
                raise ResultVerificationError(
                    "comparison candidate_count mismatch: "
                    f"SQL={executed.candidate_count}, records={len(actual_ids)}"
                )
            if (
                len(actual_ids) != len(set(actual_ids))
                or not set(actual_ids).issubset(requested_ids)
                or actual_ids != sorted(actual_ids)
            ):
                raise ResultVerificationError(
                    f"comparison results mismatch: SQL={actual_ids}, "
                    f"requested={sorted(requested_ids)}"
                )
            if any(
                record.is_quarantined
                or not all(_matches_constraint(record, item) for item in plan.constraints)
                for record in executed.records
            ):
                raise ResultVerificationError(
                    "comparison result violates a locked identity or scope constraint"
                )
            return VerifiedSearch(
                question_id=plan.question_id,
                candidate_count=len(actual_ids),
                records=executed.records,
                manifest=executed.manifest,
            )
        require_fund_public_scope(plan)
        if universe is None:
            raise ResultVerificationError("search verification requires the record universe")
        candidates = [
            record
            for record in universe
            if not record.is_quarantined
            and all(_matches_constraint(record, item) for item in plan.constraints)
        ]
        candidates.sort(key=cmp_to_key(_record_comparator(plan)))
        expected = candidates[: plan.limit]
        expected_ids = [record.product_id for record in expected]
        actual_ids = [record.product_id for record in executed.records]
        if executed.candidate_count != len(candidates):
            raise ResultVerificationError(
                "candidate_count mismatch: "
                f"SQL={executed.candidate_count}, Python={len(candidates)}"
            )
        if actual_ids != expected_ids:
            raise ResultVerificationError(
                f"top results mismatch: SQL={actual_ids}, Python={expected_ids}"
            )
        if len(actual_ids) != len(set(actual_ids)):
            raise ResultVerificationError("execution returned duplicate products")
        return VerifiedSearch(
            question_id=plan.question_id,
            candidate_count=len(candidates),
            records=executed.records,
            manifest=executed.manifest,
        )


def _verifier_as_of(record: VerifierRecord, field_name: str) -> date:
    definition = load_field_registry().require_field(field_name, [record.product_family])
    if definition.as_of_basis is AsOfBasis.DYNAMIC:
        return record.dynamic_as_of
    if definition.as_of_basis is AsOfBasis.SNAPSHOT:
        return record.source_snapshot_date
    return record.static_as_of


def _verifier_quality(
    record: VerifierRecord,
    field_name: str,
) -> QualityStatus:
    override, _ = record.row_level_quality(field_name)
    if override is not None:
        return override
    definition = load_field_registry().require_field(field_name, [record.product_family])
    if record.canonical_value(field_name) is None:
        return QualityStatus.UNKNOWN
    return definition.quality


def _verifier_usable(record: VerifierRecord, field_name: str) -> object | None:
    if _verifier_quality(record, field_name) in {
        QualityStatus.UNKNOWN,
        QualityStatus.INVALID,
        QualityStatus.UNSUPPORTED,
    }:
        return None
    return record.canonical_value(field_name)


def _verifier_scalar(value: object | None) -> str | int | bool | None:
    if value is None or isinstance(value, (str, int, bool)):
        return value
    if isinstance(value, Decimal):
        return _verifier_decimal_text(value)
    if isinstance(value, date):
        return value.isoformat()
    raise ResultVerificationError(f"unsupported aggregate group value: {value!r}")


def _verifier_decimal(value: object) -> Decimal:
    if isinstance(value, bool):
        raise ResultVerificationError("boolean cannot drive numeric aggregation")
    if isinstance(value, Decimal):
        return value
    if isinstance(value, int):
        return Decimal(value)
    raise ResultVerificationError(f"aggregate field is not numeric: {value!r}")


def _verifier_decimal_text(value: Decimal) -> str:
    rendered = format(value, "f")
    if "." in rendered:
        rendered = rendered.rstrip("0").rstrip(".")
    return rendered or "0"


def _verifier_average(values: list[Decimal]) -> Decimal:
    largest_digits = max(len(value.as_tuple().digits) for value in values)
    with localcontext() as context:
        context.prec = max(50, largest_digits + 20)
        return (sum(values, Decimal("0")) / Decimal(len(values))).quantize(
            _AVERAGE_QUANTUM,
            rounding=ROUND_HALF_EVEN,
        )


def _verifier_metric(
    plan: QueryPlan,
    records: list[VerifierRecord],
    function: AggregateFunction,
    field_name: str,
    group_values: dict[str, str | int | bool | None],
) -> AggregateMetric:
    definition = load_field_registry().require_field(
        field_name,
        [plan.product_families[0].value],
    )
    valid_records = [
        record for record in records if _verifier_usable(record, field_name) is not None
    ]
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
        numeric = [_verifier_decimal(item) for item in values]
        reducers = {
            AggregateFunction.MIN: lambda: min(numeric),
            AggregateFunction.MAX: lambda: max(numeric),
            AggregateFunction.SUM: lambda: sum(numeric, Decimal("0")),
            AggregateFunction.AVG: lambda: _verifier_average(numeric),
        }
        value = _verifier_decimal_text(reducers[function]())

    if not valid_records:
        quality = QualityStatus.UNKNOWN
        reason = (
            "trading_currency_unknown_for_amount_group"
            if currency_unknown
            else "all_values_missing_or_unusable"
        )
    else:
        statuses = [_verifier_quality(record, field_name) for record in valid_records]
        dates = {_verifier_as_of(record, field_name) for record in valid_records}
        missing = len(records) - len(valid_records)
        reasons: list[str] = []
        if missing:
            reasons.append(f"missing_or_unusable_values_excluded={missing}")
        if len(dates) > 1:
            reasons.append("mixed_as_of_dates")
        if QualityStatus.STALE in statuses:
            quality = QualityStatus.STALE
            reasons.append("one_or_more_inputs_stale")
        elif QualityStatus.PARTIAL in statuses or missing or len(dates) > 1:
            quality = QualityStatus.PARTIAL
        else:
            quality = QualityStatus.VALID
        reason = "; ".join(reasons) or None
    as_of_dates = [_verifier_as_of(record, field_name) for record in valid_records]
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
        quality_reason=reason,
    )


def _expected_aggregate_groups(
    plan: QueryPlan,
    records: list[VerifierRecord],
) -> tuple[int, list[AggregateGroup]]:
    if not records:
        return 0, []
    grouped: dict[
        tuple[str | int | bool | None, ...],
        list[VerifierRecord],
    ] = {}
    fields = plan.intent_payload.group_by
    for record in records:
        key = tuple(_verifier_scalar(_verifier_usable(record, field)) for field in fields)
        grouped.setdefault(key, []).append(record)

    def sort_key(
        item: tuple[
            tuple[str | int | bool | None, ...],
            list[VerifierRecord],
        ],
    ) -> tuple[object, ...]:
        key, group_records = item
        rendered = tuple(
            (value is None, type(value).__name__, "" if value is None else str(value).casefold())
            for value in key
        )
        return (-len(group_records), *rendered)

    expected: list[AggregateGroup] = []
    family = plan.product_families[0].value
    registry = load_field_registry()
    for key, group_records in sorted(grouped.items(), key=sort_key):
        values = dict(zip(fields, key, strict=True))
        expected.append(
            AggregateGroup(
                keys=[
                    AggregateGroupKey(
                        field=field,
                        value=value,
                        unit=registry.require_field(field, [family]).unit,
                    )
                    for field, value in values.items()
                ],
                row_count=len(group_records),
                metrics=[
                    _verifier_metric(
                        plan,
                        group_records,
                        aggregation.function,
                        aggregation.field,
                        values,
                    )
                    for aggregation in plan.intent_payload.aggregations
                ],
            )
        )
    return len(expected), expected[: plan.limit]


class AggregateResultVerifier:
    """Independently recompute aggregate candidates and metrics in pure Python."""

    def verify(
        self,
        plan: QueryPlan,
        executed: ExecutedAggregation,
        universe: list[VerifierRecord],
    ) -> VerifiedAggregation:
        require_aggregate_contract(plan)
        if executed.question_id != plan.question_id:
            raise ResultVerificationError("question_id changed during aggregate execution")
        if executed.manifest.dataset != plan.product_families[0].value:
            raise ResultVerificationError("aggregate manifest dataset differs from the plan")
        candidates = [
            record
            for record in universe
            if not record.is_quarantined
            and all(_matches_constraint(record, item) for item in plan.constraints)
        ]
        total_group_count, expected_groups = _expected_aggregate_groups(plan, candidates)
        if executed.candidate_count != len(candidates):
            raise ResultVerificationError(
                "aggregate candidate_count mismatch: "
                f"execution={executed.candidate_count}, Python={len(candidates)}"
            )
        if executed.total_group_count != total_group_count:
            raise ResultVerificationError(
                "aggregate total_group_count mismatch: "
                f"execution={executed.total_group_count}, Python={total_group_count}"
            )
        if executed.groups != expected_groups:
            raise ResultVerificationError("aggregate groups or metrics differ from Python verifier")
        return VerifiedAggregation(
            question_id=plan.question_id,
            candidate_count=len(candidates),
            total_group_count=total_group_count,
            groups=executed.groups,
            manifest=executed.manifest,
        )
