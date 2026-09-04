"""Deterministic Hangul phonetic key shared by live extraction and cache lookup.

The key is not a linguistic transliteration.  It only lets a Hangul proper name
in a question be compared with a Latin spelling that an approved official
document (or a graph relation built from one) already contains.  It never
creates evidence or authorizes a source on its own.
"""

from __future__ import annotations

import unicodedata

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
