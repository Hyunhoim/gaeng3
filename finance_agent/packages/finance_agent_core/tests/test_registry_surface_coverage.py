"""Every numeric sortable surface the registry declares must resolve.

``field_registry.yaml`` is the runtime control layer, so a label or alias it
carries is a promise: asking with that wording has to reach that field. Before
this suite existed, 36 of 140 declared surfaces (25.7%) either asked the user to
clarify or ranked by the wrong field, which loses the point outright in a
single-turn evaluation.
"""

from __future__ import annotations

import re

import pytest

from finance_agent_core.agent.compiler import ServerQueryPlanCompiler
from finance_agent_core.agent.router import IntentRouter
from finance_agent_core.config import load_field_registry
from finance_agent_core.config.registry import ValueType

_FAMILY_NOUN = {
    "bond": "국내채권",
    "domestic_etp": "국내 ETF",
    "overseas_etp": "해외 ETF",
    "fund": "공모펀드",
}
_HANGUL = re.compile(r"[가-힣]")

# These surfaces name another product family inside themselves, so pairing them
# with a family noun asks about two families at once and the compiler refuses.
# That refusal is the intended fail-closed behaviour, not a coverage gap.
_CROSS_FAMILY_SURFACES = frozenset(
    {
        ("domestic_bond_ratio_pct", "국내 채권 구성비"),
        ("overseas_bond_ratio_pct", "해외 채권 구성비"),
        ("base_price", "펀드 기준가"),
    }
)


def _subject_particle(word: str) -> str:
    last = word[-1]
    if not ("가" <= last <= "힣"):
        return "이"
    return "이" if (ord(last) - 0xAC00) % 28 else "가"


def _declared_surfaces() -> list[tuple[str, str, str]]:
    registry = load_field_registry()
    cases: list[tuple[str, str, str]] = []
    for field_id, definition in registry.fields.items():
        if not definition.sortable or definition.value_type is not ValueType.NUMBER:
            continue
        if not definition.datasets:
            continue
        noun = _FAMILY_NOUN.get(definition.datasets[0])
        if noun is None:
            continue
        for surface in (definition.label, *definition.aliases):
            if not surface or _HANGUL.search(surface) is None:
                continue
            if (field_id, surface) in _CROSS_FAMILY_SURFACES:
                continue
            cases.append((field_id, surface, noun))
    return cases


_CASES = _declared_surfaces()


def test_the_registry_declares_surfaces_for_every_numeric_sortable_field() -> None:
    """Guard the guard: an empty case list would make the sweep below vacuous."""

    assert len(_CASES) >= 130
    assert len({field_id for field_id, _, _ in _CASES}) >= 40


@pytest.mark.parametrize(("field_id", "surface", "noun"), _CASES)
def test_declared_surface_ranks_by_its_own_field(
    field_id: str,
    surface: str,
    noun: str,
) -> None:
    question = f"{surface}{_subject_particle(surface)} 높은 {noun} 3개"

    decision = IntentRouter().route(question, f"surface-{field_id}")

    assert decision.disposition.value == "execute", question
    plan = ServerQueryPlanCompiler({}).compile(decision)
    assert [item.field for item in plan.ranking] == [field_id], question


@pytest.mark.parametrize(
    "question",
    [
        # A shorter surface must not match inside a longer one and answer with
        # the wrong window or the wrong amount.
        "120거래일 연환산 변동성이 높은 국내 ETF 3개",
        "총발행금액이 높은 국내채권 3개",
        "순자산가치가 높은 국내 ETF 3개",
    ],
)
def test_a_longer_surface_wins_over_the_shorter_one_it_contains(question: str) -> None:
    decision = IntentRouter().route(question, "surface-boundary")

    assert decision.disposition.value == "execute", question


@pytest.mark.parametrize(
    "question",
    [
        # A vague metric with no registry surface behind it stays ambiguous.
        "가격이 높은 국내 ETF 3개",
        "비용이 낮은 국내 ETF 3개",
    ],
)
def test_a_vague_metric_still_fails_closed(question: str) -> None:
    decision = IntentRouter().route(question, "surface-vague")

    assert decision.disposition.value != "execute", question


# A missed threshold is worse than a missed sort: the question still runs and the
# condition silently disappears, so the answer lists products that do not meet it.
_FILTER_CROSS_FAMILY = frozenset(
    {
        ("domestic_bond_ratio_pct", "국내 채권 구성비"),
        ("overseas_bond_ratio_pct", "해외 채권 구성비"),
        ("base_price", "펀드 기준가"),
    }
)


def _threshold_surfaces() -> list[tuple[str, str, str, str]]:
    registry = load_field_registry()
    cases: list[tuple[str, str, str, str]] = []
    for field_id, definition in registry.fields.items():
        if definition.value_type is not ValueType.NUMBER or not definition.queryable:
            continue
        if not definition.datasets:
            continue
        if not {"gte", "lte"} & set(definition.allowed_operators):
            continue
        noun = _FAMILY_NOUN.get(definition.datasets[0])
        if noun is None:
            continue
        amount = "0.5%" if (definition.unit or "").startswith("pct") else "100"
        for surface in (definition.label, *definition.aliases):
            if not surface or _HANGUL.search(surface) is None:
                continue
            if (field_id, surface) in _FILTER_CROSS_FAMILY:
                continue
            cases.append((field_id, surface, noun, amount))
    return cases


_THRESHOLD_CASES = _threshold_surfaces()


def test_the_registry_declares_threshold_surfaces() -> None:
    assert len(_THRESHOLD_CASES) >= 120


@pytest.mark.parametrize(("field_id", "surface", "noun", "amount"), _THRESHOLD_CASES)
def test_declared_surface_carries_its_threshold(
    field_id: str,
    surface: str,
    noun: str,
    amount: str,
) -> None:
    for phrase, operator in (("이상", "gte"), ("이하", "lte")):
        question = f"{surface} {amount} {phrase}인 {noun} 3개"

        decision = IntentRouter().route(question, f"threshold-{field_id}")

        assert decision.disposition.value == "execute", question
        plan = ServerQueryPlanCompiler({}).compile(decision)
        applied = {
            (item.field, getattr(item.operator, "value", item.operator))
            for item in plan.constraints
        }
        assert (field_id, operator) in applied, question
