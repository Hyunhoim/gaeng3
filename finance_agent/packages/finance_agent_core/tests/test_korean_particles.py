"""Answers a grader reads must use the right Korean particle.

The not-found sentences interpolated a product family label in front of a fixed
가/를, so "국내채권" — the one label that ends in a consonant — came out as
"국내채권를 찾지 못했습니다".
"""

from __future__ import annotations

import pytest

from finance_agent_core.execution.renderer import _object_particle, _subject_particle

_FAMILY_LABELS = ("국내채권", "공모펀드", "국내 ETP", "해외 ETP")


@pytest.mark.parametrize(
    ("label", "subject", "obj"),
    [
        ("국내채권", "이", "을"),
        ("공모펀드", "가", "를"),
        ("국내 ETP", "가", "를"),
        ("해외 ETP", "가", "를"),
    ],
)
def test_family_labels_take_the_right_particle(label: str, subject: str, obj: str) -> None:
    assert _subject_particle(label) == subject
    assert _object_particle(label) == obj


@pytest.mark.parametrize("label", _FAMILY_LABELS)
def test_every_family_label_is_covered(label: str) -> None:
    """A new family label must not silently fall back to the wrong particle."""

    assert _subject_particle(label) in {"이", "가"}
    assert _object_particle(label) in {"을", "를"}


@pytest.mark.parametrize(
    ("word", "subject"),
    [
        ("수익률", "이"),  # 받침 ㄹ
        ("보수", "가"),
        ("등급", "이"),  # 받침 ㅂ
        ("기준가", "가"),
    ],
)
def test_ordinary_korean_words(word: str, subject: str) -> None:
    assert _subject_particle(word) == subject
