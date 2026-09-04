from __future__ import annotations

from datetime import date
from decimal import ROUND_HALF_EVEN, Decimal, localcontext
from functools import cmp_to_key

from finance_agent_core.config import AsOfBasis, QualityStatus, load_field_registry
from finance_agent_core.contracts.queryplan import (
    AggregateFunction,
    Constraint,
    ConstraintExpression,
    ConstraintOperator,
    Intent,
    LogicalOperator,
    NullPlacement,
    QueryPlan,
    Ranking,
    SortDirection,
    constraint_key,
)
from finance_agent_core.deadline import raise_if_request_stopped
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
    ranking_requires_usable_value,
    require_aggregate_contract,
    require_comparison_contract,
    require_fund_public_scope,
)
from finance_agent_core.execution.verification_types import VerifierRecord


class ResultVerificationError(ValueError):
    """Raised when SQL output differs from the independent Python oracle."""


_AVERAGE_QUANTUM = Decimal("0.000000000001")
_DEADLINE_CHECK_INTERVAL = 256


def _periodic_deadline_check(index: int) -> None:
    if index % _DEADLINE_CHECK_INTERVAL == 0:
        raise_if_request_stopped()


def _logical_product_universe(
    plan: QueryPlan,
    universe: list[VerifierRecord],
) -> list[VerifierRecord]:
    """Rebuild the SQL logical-product scope independently in Python."""

    if plan.product_families[0].value != "bond":
        return universe
    canonical: dict[str, VerifierRecord] = {}
    for record in universe:
        if (
            record.is_quarantined
            or not _matches_plan_constraints(record, plan)
            or any(
                ranking_requires_usable_value(plan, ranking.field)
                and _query_metric_value(record, ranking.field) is None
                for ranking in plan.ranking
            )
        ):
            continue
        logical_id = record.product_id.split(":", 1)[0]
        previous = canonical.get(logical_id)
        if previous is None or _logical_record_preference(plan, record) < (
            _logical_record_preference(plan, previous)
        ):
            canonical[logical_id] = record
    return list(canonical.values())


def _comparable_value(record: VerifierRecord, field_name: str) -> object | None:
    quality, _ = record.row_level_quality(field_name)
    if quality in {
        QualityStatus.UNKNOWN,
        QualityStatus.INVALID,
        QualityStatus.UNSUPPORTED,
    }:
        return None
    return record.canonical_value(field_name)


def _query_metric_value(record: VerifierRecord, field_name: str) -> object | None:
    """Mirror the official numeric 0/missing exclusion independently of SQL."""

    value = _comparable_value(record, field_name)
    if value is None:
        return None
    definition = load_field_registry().require_field(field_name, [record.product_family])
    if (
        definition.value_type.value == "number"
        and not isinstance(value, bool)
        and value == 0
    ):
        return None
    return value


def _logical_record_preference(
    plan: QueryPlan,
    record: VerifierRecord,
) -> tuple[int, str, int]:
    """Mirror the bond representative-row rule independently of SQLite."""

    if plan.intent is not Intent.AGGREGATE:
        return (0, "", record.source_row)
    preference_fields = tuple(
        dict.fromkeys(
            [
                *plan.intent_payload.group_by,
                *(
                    item.field
                    for item in plan.intent_payload.aggregations
                    if item.field != "product_id"
                ),
            ]
        )
    )
    usable_count = sum(
        _query_metric_value(record, field_name) is not None
        for field_name in preference_fields
    )
    # Lower tuples win: maximize supported requested fields, then use the
    # source-defined physical ID and row order for a deterministic representative.
    return (-usable_count, record.product_id, record.source_row)


def _ranking_metric_value(record: VerifierRecord, field_name: str) -> object | None:
    value = _query_metric_value(record, field_name)
    if field_name != "risk_level" or value is None:
        return value
    normalized = str(value).replace(" ", "")
    semantic_risk_order = {
        "매우낮은위험(6등급)": 1,
        "매우낮은위험": 1,
        "낮은위험(5등급)": 2,
        "낮은위험": 2,
        "보통위험(4등급)": 3,
        "보통위험": 3,
        "다소높은위험(3등급)": 4,
        "다소높은위험": 4,
        "높은위험(2등급)": 5,
        "높은위험": 5,
        "매우높은위험(1등급)": 6,
        "매우높은위험": 6,
    }
    return semantic_risk_order.get(normalized)


def _public_product_id(record: VerifierRecord) -> str:
    if record.product_family == "bond":
        return record.product_id.split(":", 1)[0]
    if record.product_family == "overseas_etp":
        return record.product_id.split(":", 1)[-1]
    return record.product_id


def _requested_comparison_id(
    record: VerifierRecord,
    requested_ids: set[str],
) -> str:
    """Match either a physical ID or its approved public shorthand.

    Overseas queries may legitimately use ``exchange:ticker`` or a unique
    ticker, while bond queries expose a logical ticker over venue rows.  Prefer
    the exact requested spelling so verification does not rewrite one accepted
    identity form into another.
    """

    if record.product_id in requested_ids:
        return record.product_id
    public_id = _public_product_id(record)
    if public_id in requested_ids:
        return public_id
    return public_id


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
    actual = (
        record.product_id.split(":", 1)[0]
        if constraint.field == "product_id" and record.product_family == "bond"
        else _query_metric_value(record, constraint.field)
    )
    operator = constraint.operator
    if operator in {
        ConstraintOperator.IS_PRESENT,
        ConstraintOperator.IS_MISSING,
    }:
        present = actual is not None
        if isinstance(actual, str):
            present = bool(actual.strip())
        elif (
            isinstance(actual, (int, float, Decimal))
            and not isinstance(actual, bool)
        ):
            present = actual != 0
        return present if operator is ConstraintOperator.IS_PRESENT else not present
    if actual is None:
        return False
    expected = constraint.value

    def canonical_expected(value: object) -> object:
        if (
            constraint.field == "product_id"
            and record.product_family == "bond"
            and isinstance(value, str)
        ):
            return value.split(":", 1)[0]
        return value

    if operator in {ConstraintOperator.IN, ConstraintOperator.NOT_IN}:
        assert isinstance(expected, list)
        converted = [_expected_value(actual, canonical_expected(value)) for value in expected]
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
    converted = _expected_value(actual, canonical_expected(expected))
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


def _matches_plan_constraints(record: VerifierRecord, plan: QueryPlan) -> bool:
    """Evaluate the same Boolean constraint tree independently of SQLite."""

    expression = plan.constraint_expression
    if expression is None:
        return all(_matches_constraint(record, item) for item in plan.constraints)
    constraints_by_key = {constraint_key(item): item for item in plan.constraints}

    def expression_keys(node: ConstraintExpression) -> set[str]:
        keys = set(node.constraint_keys)
        for child in node.children:
            keys.update(expression_keys(child))
        return keys

    def matches_expression(node: ConstraintExpression) -> bool:
        values = [
            _matches_constraint(record, constraints_by_key[key])
            for key in node.constraint_keys
        ]
        values.extend(matches_expression(child) for child in node.children)
        if node.operator is LogicalOperator.ALL:
            return all(values)
        return any(values)

    logical_keys = expression_keys(expression)
    return all(
        _matches_constraint(record, item)
        for item in plan.constraints
        if constraint_key(item) not in logical_keys
    ) and matches_expression(expression)


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


def _record_comparator(
    plan: QueryPlan,
    ranking_values: dict[int, tuple[object | None, ...]],
):
    comparison_count = 0

    def compare(
        left: VerifierRecord,
        right: VerifierRecord,
    ) -> int:
        nonlocal comparison_count
        comparison_count += 1
        _periodic_deadline_check(comparison_count)
        left_values = ranking_values[id(left)]
        right_values = ranking_values[id(right)]
        for index, ranking in enumerate(plan.ranking):
            comparison = _compare_values(
                left_values[index],
                right_values[index],
                ranking,
            )
            if comparison:
                return comparison
        left_id = _public_product_id(left)
        right_id = _public_product_id(right)
        return (left_id > right_id) - (left_id < right_id)

    return compare


class ResultVerifier:
    def verify(
        self,
        plan: QueryPlan,
        executed: ExecutedSearch,
        universe: list[VerifierRecord] | None = None,
    ) -> VerifiedSearch:
        raise_if_request_stopped()
        if plan.intent is Intent.COMPARE:
            require_comparison_contract(plan)
        if executed.question_id != plan.question_id:
            raise ResultVerificationError("question_id changed during execution")
        if executed.manifest.dataset != plan.product_families[0].value:
            raise ResultVerificationError("search manifest dataset differs from the plan")
        if plan.intent is Intent.COMPARE:
            requested_ids = comparison_product_ids(plan)
            requested_id_set = set(requested_ids)
            physical_ids = [record.product_id for record in executed.records]
            actual_ids = [
                _requested_comparison_id(record, requested_id_set)
                for record in executed.records
            ]
            if executed.candidate_count != len(physical_ids):
                raise ResultVerificationError(
                    "comparison candidate_count mismatch: "
                    f"SQL={executed.candidate_count}, records={len(physical_ids)}"
                )
            if (
                len(actual_ids) != len(set(actual_ids))
                or not set(actual_ids).issubset(requested_ids)
            ):
                raise ResultVerificationError(
                    f"comparison results mismatch: SQL={actual_ids}, "
                    f"requested={sorted(requested_ids)}"
                )
            # Oracle rows have a deterministic storage-ID order, which need
            # not equal the order in which the caller named the products.  The
            # comparison builder reorders these independently verified rows to
            # ``comparison_product_ids`` before calculating deltas.  Set
            # coverage plus uniqueness preserves identity safety here without
            # rejecting a valid reverse input order.
            if any(
                record.is_quarantined
                or not _matches_plan_constraints(record, plan)
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
        universe = _logical_product_universe(plan, universe)
        candidates: list[VerifierRecord] = []
        ranking_values: dict[int, tuple[object | None, ...]] = {}
        for index, record in enumerate(universe):
            _periodic_deadline_check(index)
            if record.is_quarantined or not _matches_plan_constraints(record, plan):
                continue
            values: list[object | None] = []
            ranking_is_usable = True
            for ranking in plan.ranking:
                value = _ranking_metric_value(record, ranking.field)
                values.append(value)
                if ranking_requires_usable_value(plan, ranking.field) and value is None:
                    ranking_is_usable = False
                    break
            if not ranking_is_usable:
                continue
            candidates.append(record)
            ranking_values[id(record)] = tuple(values)
        raise_if_request_stopped()
        candidates.sort(key=cmp_to_key(_record_comparator(plan, ranking_values)))
        raise_if_request_stopped()
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


def _verifier_median(values: list[Decimal]) -> Decimal:
    ordered = sorted(values)
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[middle]
    return _verifier_average([ordered[middle - 1], ordered[middle]])


def _verifier_reduce_numeric(
    function: AggregateFunction,
    values: list[Decimal],
) -> Decimal:
    if function is AggregateFunction.AVG:
        return _verifier_average(values)
    if function is AggregateFunction.MEDIAN:
        return _verifier_median(values)
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
            raise ResultVerificationError(f"unsupported aggregate function: {function}")
    return reduced


def _verifier_metric(
    plan: QueryPlan,
    records: list[VerifierRecord],
    function: AggregateFunction,
    field_name: str,
    group_values: dict[str, str | int | bool | None],
) -> AggregateMetric:
    raise_if_request_stopped()
    definition = load_field_registry().require_field(
        field_name,
        [plan.product_families[0].value],
    )
    valid_records: list[VerifierRecord] = []
    for index, record in enumerate(records):
        _periodic_deadline_check(index)
        usable_value = _verifier_usable(record, field_name)
        if usable_value is not None and not (
            definition.value_type.value == "number"
            and not isinstance(usable_value, bool)
            and usable_value == 0
        ):
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
            numeric.append(_verifier_decimal(item))
        raise_if_request_stopped()
        value = _verifier_decimal_text(_verifier_reduce_numeric(function, numeric))

    if not valid_records:
        quality = QualityStatus.UNKNOWN
        reason = (
            "trading_currency_unknown_for_amount_group"
            if currency_unknown
            else "all_values_missing_or_unusable"
        )
    else:
        statuses: list[QualityStatus] = []
        dates: set[date] = set()
        for index, record in enumerate(valid_records):
            _periodic_deadline_check(index)
            statuses.append(_verifier_quality(record, field_name))
            dates.add(_verifier_as_of(record, field_name))
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
    as_of_dates: list[date] = []
    for index, record in enumerate(valid_records):
        _periodic_deadline_check(index)
        as_of_dates.append(_verifier_as_of(record, field_name))
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
    raise_if_request_stopped()
    if not records:
        return 0, []
    grouped: dict[
        tuple[str | int | bool | None, ...],
        list[VerifierRecord],
    ] = {}
    fields = plan.intent_payload.group_by
    for index, record in enumerate(records):
        _periodic_deadline_check(index)
        key = tuple(_verifier_scalar(_verifier_usable(record, field)) for field in fields)
        # Mirror the organizer's missing/zero rule independently from the
        # execution reducer: NULL is an unavailable group key, not a category.
        if fields and any(value is None for value in key):
            continue
        grouped.setdefault(key, []).append(record)
    raise_if_request_stopped()

    def sort_key(
        item: tuple[
            tuple[str | int | bool | None, ...],
            list[VerifierRecord],
        ],
    ) -> tuple[object, ...]:
        raise_if_request_stopped()
        key, _group_records = item
        rendered = tuple(
            (value is None, type(value).__name__, "" if value is None else str(value).casefold())
            for value in key
        )
        return rendered

    expected: list[AggregateGroup] = []
    family = plan.product_families[0].value
    registry = load_field_registry()
    for group_index, (key, group_records) in enumerate(sorted(grouped.items(), key=sort_key)):
        _periodic_deadline_check(group_index)
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
    raise_if_request_stopped()
    return len(expected), expected[: plan.limit]


class AggregateResultVerifier:
    """Independently recompute aggregate candidates and metrics in pure Python."""

    def verify(
        self,
        plan: QueryPlan,
        executed: ExecutedAggregation,
        universe: list[VerifierRecord],
    ) -> VerifiedAggregation:
        raise_if_request_stopped()
        require_aggregate_contract(plan)
        if executed.question_id != plan.question_id:
            raise ResultVerificationError("question_id changed during aggregate execution")
        if executed.manifest.dataset != plan.product_families[0].value:
            raise ResultVerificationError("aggregate manifest dataset differs from the plan")
        # COUNT(field) availability summaries count organizer rows just like
        # COUNT(product_id).  Numeric reductions retain logical-bond
        # de-duplication; every-count plans must mirror the Oracle's physical
        # row scope so the independent verifier checks the same declared unit.
        physical_bond_count = plan.product_families[0].value == "bond" and all(
            item.function is AggregateFunction.COUNT
            for item in plan.intent_payload.aggregations
        )
        if not physical_bond_count:
            universe = _logical_product_universe(plan, universe)
        candidates: list[VerifierRecord] = []
        for index, record in enumerate(universe):
            _periodic_deadline_check(index)
            if not record.is_quarantined and _matches_plan_constraints(record, plan):
                candidates.append(record)
        raise_if_request_stopped()
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
