from __future__ import annotations

import re
from dataclasses import dataclass
from functools import lru_cache
from typing import Literal

from finance_agent_core.config import FieldDefinition, ValueType, load_field_registry

FieldCapability = Literal["queryable", "sortable", "comparable", "aggregatable"]


# These aliases are valid display metadata, but too broad to grant execution
# authority when they appear by themselves in an arbitrary sentence.  The
# official field label remains available, as do the registry's more specific
# aliases.  Product identities are masked before the comparison parser calls
# this module, which provides an additional boundary against name collisions.
_LOW_CONFIDENCE_ALIASES = frozenset(
    {
        "name",
        "fee",
        "지역",
        "국가",
        "통화",
        "발행사",
        "거래소",
        "배수",
    }
)


@dataclass(frozen=True, slots=True)
class RegistryFieldMention:
    field_id: str
    definition: FieldDefinition
    phrase: str
    start: int
    end: int


@lru_cache(maxsize=64)
def _registry_field_patterns(
    family: str,
    capability: FieldCapability,
    value_type_values: tuple[str, ...] | None,
    include_aliases: bool,
) -> tuple[tuple[str, FieldDefinition, str, re.Pattern[str]], ...]:
    """Compile the immutable Field Registry language surface once per policy."""

    registry = load_field_registry()
    accepted_types = (
        None
        if value_type_values is None
        else frozenset(ValueType(value) for value in value_type_values)
    )
    patterns: list[tuple[str, FieldDefinition, str, re.Pattern[str]]] = []
    for field_id, base_definition in registry.fields.items():
        if family not in base_definition.datasets:
            continue
        definition = base_definition.resolve(family)
        if not getattr(definition, capability):
            continue
        if accepted_types is not None and definition.value_type not in accepted_types:
            continue
        for phrase in registry_field_phrases(
            definition,
            include_aliases=include_aliases,
        ):
            patterns.append(
                (
                    field_id,
                    definition,
                    phrase,
                    re.compile(literal_field_pattern(phrase), re.IGNORECASE),
                )
            )
    return tuple(patterns)


def registry_field_phrases(
    definition: FieldDefinition,
    *,
    include_aliases: bool = True,
) -> tuple[str, ...]:
    """Return the official label and execution-safe approved aliases."""

    candidates = [definition.label]
    if include_aliases:
        candidates.extend(
            alias
            for alias in definition.aliases
            if alias.strip().casefold() not in _LOW_CONFIDENCE_ALIASES
        )
    return tuple(
        sorted(
            {phrase.strip() for phrase in candidates if phrase.strip()},
            key=lambda phrase: (-len(phrase), phrase.casefold()),
        )
    )


def literal_field_pattern(phrase: str) -> str:
    """Compile one frozen phrase with whitespace-only spelling tolerance.

    The function does not invent synonyms.  Alphanumeric boundaries prevent a
    shorter label such as ``20거래일`` from matching inside ``120거래일``;
    Korean particles are accepted only after the complete phrase.
    """

    escaped = re.escape(phrase.strip()).replace(r"\ ", r"\s*")
    leading = r"(?<![0-9A-Za-z])" if phrase[:1].isascii() and phrase[:1].isalnum() else ""
    trailing = (
        r"(?![0-9A-Za-z])"
        if phrase[-1:].isascii() and phrase[-1:].isalnum()
        else ""
    )
    return f"{leading}{escaped}{trailing}"


def registry_field_mentions(
    question: str,
    family: str,
    capability: FieldCapability,
    *,
    value_types: frozenset[ValueType] | None = None,
    include_aliases: bool = True,
) -> tuple[RegistryFieldMention, ...]:
    """Find explicit registry-owned field surfaces with longest-span ownership.

    Nested labels are common in the official schema (``순자산`` vs
    ``순자산총액`` and ``거래대금`` vs ``1개월 평균 거래대금``).  A shorter
    match contained by a longer field phrase never receives parser authority.
    """

    found: list[RegistryFieldMention] = []
    value_type_values = (
        None if value_types is None else tuple(sorted(item.value for item in value_types))
    )
    for field_id, definition, phrase, pattern in _registry_field_patterns(
        family,
        capability,
        value_type_values,
        include_aliases,
    ):
        for match in pattern.finditer(question):
            found.append(
                RegistryFieldMention(
                    field_id=field_id,
                    definition=definition,
                    phrase=phrase,
                    start=match.start(),
                    end=match.end(),
                )
            )

    deduplicated = {
        (item.field_id, item.start, item.end): item
        for item in sorted(
            found,
            key=lambda item: (
                item.start,
                -(item.end - item.start),
                item.field_id,
            ),
        )
    }
    candidates = list(deduplicated.values())
    owned = [
        item
        for item in candidates
        if not any(
            other.field_id != item.field_id
            and other.start <= item.start
            and item.end <= other.end
            and (other.end - other.start) > (item.end - item.start)
            for other in candidates
        )
    ]
    return tuple(
        sorted(
            owned,
            key=lambda item: (item.start, -(item.end - item.start), item.field_id),
        )
    )


def registry_implied_families(question: str) -> tuple[str, ...]:
    """Return a family only when explicit field surfaces prove it uniquely.

    This is a schema intersection, not a language-model guess.  Every maximal
    field phrase in the question must exist as a queryable or sortable field in
    the same family.  A common surface such as AUM therefore stays ambiguous,
    while bond-only ``세후 매수수익률`` can safely recover the omitted family.
    Contradictory surfaces produce no family rather than choosing one.
    """

    families = ("bond", "domestic_etp", "overseas_etp", "fund")
    observed: list[tuple[int, int, str, str]] = []
    for family in families:
        family_mentions: dict[tuple[int, int, str], RegistryFieldMention] = {}
        for capability in ("queryable", "sortable"):
            for mention in registry_field_mentions(question, family, capability):
                family_mentions[(mention.start, mention.end, mention.field_id)] = mention
        observed.extend(
            (mention.start, mention.end, mention.phrase.casefold(), family)
            for mention in family_mentions.values()
        )
    if not observed:
        return ()

    # Keep global longest-span ownership so a broad nested alias cannot add an
    # unrelated family beside a more specific official phrase.
    maximal = [
        item
        for item in observed
        if not any(
            other[0] <= item[0]
            and item[1] <= other[1]
            and (other[1] - other[0]) > (item[1] - item[0])
            for other in observed
        )
    ]
    by_span: dict[tuple[int, int, str], set[str]] = {}
    for start, end, phrase, family in maximal:
        by_span.setdefault((start, end, phrase), set()).add(family)
    eligible = set(families)
    for supported in by_span.values():
        eligible.intersection_update(supported)
    return tuple(family for family in families if family in eligible)
