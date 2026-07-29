from __future__ import annotations

from datetime import date
from decimal import Decimal
from functools import cmp_to_key

from finance_agent_core.config import QualityStatus
from finance_agent_core.contracts.queryplan import (
    Constraint,
    ConstraintOperator,
    NullPlacement,
    QueryPlan,
    Ranking,
    SortDirection,
)
from finance_agent_core.domain import (
    ExecutedSearch,
    NormalizedProductRecord,
    VerifiedSearch,
)


class ResultVerificationError(ValueError):
    """Raised when SQL output differs from the independent Python oracle."""


def _comparable_value(record: NormalizedProductRecord, field_name: str) -> object | None:
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
    record: NormalizedProductRecord,
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
        left: NormalizedProductRecord,
        right: NormalizedProductRecord,
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
        universe: list[NormalizedProductRecord],
    ) -> VerifiedSearch:
        if executed.question_id != plan.question_id:
            raise ResultVerificationError("question_id changed during execution")
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
