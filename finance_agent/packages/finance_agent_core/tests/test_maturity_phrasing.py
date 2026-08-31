from __future__ import annotations

import pytest

from finance_agent_core.agent.linker import build_lexical_hints


def _remaining_day_constraints(question: str) -> set[tuple[str, object]]:
    hints = build_lexical_hints(question)
    return {
        (
            item["operator"],
            tuple(item["value"]) if isinstance(item["value"], list) else item["value"],
        )
        for item in hints["required_constraints"]
        if item["field"] == "remaining_days"
    }


@pytest.mark.parametrize(
    ("question", "expected"),
    [
        # "N년도 안 남은" is the same bound as "N년 미만" and previously produced no
        # constraint at all, so every bond was returned as a candidate.
        ("만기가 1년도 안 남은 국내채권 3개", {("between", (0, 364))}),
        ("만기가 2년도 안 남은 채권 3개", {("between", (0, 729))}),
        ("잔존 만기가 6개월도 안 남은 채권", {("between", (0, 179))}),
        # Months and days join the years the planner already understood.
        ("만기 6개월 이내인 국내채권 3개 알려줘", {("between", (0, 180))}),
        ("만기 18개월 이하 국내채권", {("between", (0, 540))}),
        ("만기 90일 미만 국내채권", {("between", (0, 89))}),
    ],
)
def test_maturity_phrasings_become_remaining_day_bounds(
    question: str,
    expected: set[tuple[str, object]],
) -> None:
    assert _remaining_day_constraints(question) == expected


@pytest.mark.parametrize(
    ("question", "expected"),
    [
        ("만기가 1년 이하인 채권 찾아줘", {("between", (0, 365))}),
        ("만기 3년 이상 국내채권", {("gte", 1095)}),
        ("만기 3년 초과 국내채권", {("gt", 1095)}),
    ],
)
def test_previously_supported_maturity_bounds_are_unchanged(
    question: str,
    expected: set[tuple[str, object]],
) -> None:
    assert _remaining_day_constraints(question) == expected


def test_a_question_without_a_maturity_bound_claims_none() -> None:
    assert _remaining_day_constraints("국내채권 3개 알려줘") == set()
