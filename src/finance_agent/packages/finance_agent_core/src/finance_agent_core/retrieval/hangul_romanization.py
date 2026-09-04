"""Deterministic Hangul phonetic key shared by live extraction and cache lookup.

The key is not a linguistic transliteration.  It only lets a Hangul proper name
in a question be compared with a Latin spelling that an approved official
document (or a graph relation built from one) already contains.  It never
creates evidence or authorizes a source on its own.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable
from difflib import SequenceMatcher

_HANGUL_INITIAL = (
    "g",
    "kk",
    "n",
    "d",
    "tt",
    "r",
    "m",
    "b",
    "pp",
    "s",
    "ss",
    "",
    "j",
    "jj",
    "ch",
    "k",
    "t",
    "p",
    "h",
)
_HANGUL_VOWEL = (
    "a",
    "ae",
    "ya",
    "yae",
    "eo",
    "e",
    "yeo",
    "ye",
    "o",
    "wa",
    "wae",
    "oe",
    "yo",
    "u",
    "wo",
    "we",
    "wi",
    "yu",
    "eu",
    "ui",
    "i",
)
_HANGUL_FINAL = (
    "",
    "k",
    "k",
    "k",
    "n",
    "n",
    "n",
    "t",
    "l",
    "k",
    "m",
    "p",
    "t",
    "t",
    "p",
    "t",
    "m",
    "p",
    "p",
    "t",
    "t",
    "ng",
    "t",
    "t",
    "k",
    "t",
    "p",
    "t",
)


def hangul_romanization_key(value: str) -> str:
    """Return a deterministic phonetic key, not a linguistic translation.

    The key is used only to recover an exact Latin spelling already present in
    an approved official document.  It never creates evidence or authorizes a
    source on its own.
    """

    output: list[str] = []
    for character in unicodedata.normalize("NFKC", value):
        codepoint = ord(character)
        if 0xAC00 <= codepoint <= 0xD7A3:
            offset = codepoint - 0xAC00
            initial = offset // 588
            vowel = (offset % 588) // 28
            final = offset % 28
            output.extend(
                (
                    _HANGUL_INITIAL[initial],
                    _HANGUL_VOWEL[vowel],
                    _HANGUL_FINAL[final],
                )
            )
    return "".join(output)


LATIN_LABEL = re.compile(r"[A-Za-z][A-Za-z0-9&.,'()/ -]{1,120}")
PHONETIC_MATCH_THRESHOLD = 0.64
PHONETIC_MARGIN = 0.08


def latin_label_phonetic_score(subject_key: str, label: str) -> float:
    """Score how close a Latin label sounds to a Hangul subject's phonetic key.

    Returns 0.0 unless the label is Latin text whose length is within 0.55-1.45
    of the key; otherwise the ``SequenceMatcher`` ratio between the two.
    """

    if len(subject_key) < 5 or LATIN_LABEL.fullmatch(label) is None:
        return 0.0
    folded = "".join(ch for ch in label.casefold() if ch.isalnum())
    if not folded or not 0.55 <= len(folded) / len(subject_key) <= 1.45:
        return 0.0
    return SequenceMatcher(None, subject_key, folded).ratio()


def best_phonetic_label(subject: str, labels: Iterable[str]) -> tuple[str, float] | None:
    """Return the Latin label closest to ``subject`` when it clears the threshold."""

    key = hangul_romanization_key(subject)
    if len(key) < 5:
        return None
    ranked = sorted(
        ((latin_label_phonetic_score(key, label), label) for label in dict.fromkeys(labels)),
        key=lambda item: (-item[0], item[1].casefold()),
    )
    if not ranked or ranked[0][0] < PHONETIC_MATCH_THRESHOLD:
        return None
    return ranked[0][1], ranked[0][0]
