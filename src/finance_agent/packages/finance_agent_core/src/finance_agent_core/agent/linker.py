from __future__ import annotations

import re
from collections import defaultdict
from datetime import date
from decimal import Decimal
from typing import Any

from finance_agent_core.agent.field_language import (
    literal_field_pattern,
    registry_field_mentions,
    registry_field_phrases,
)
from finance_agent_core.agent.numeric_language import requested_result_limit
from finance_agent_core.agent.presentation import semantic_query_surface
from finance_agent_core.agent.safety import normalize_user_question
from finance_agent_core.agent.semantic_aliases import (
    explicit_product_name_order,
    filter_alias_matches,
    rank_alias_matches,
    ranking_cue_spans,
    ranking_intent_present,
)
from finance_agent_core.agent.semantic_gate import SemanticCoverageGate
from finance_agent_core.config import ValueType, load_field_registry
from finance_agent_core.contracts import QueryPlan
from finance_agent_core.contracts.queryplan import (
    SEARCH_PROJECTION_BY_FAMILY,
    Constraint,
    constraint_key,
    search_projection,
)

NUMBER = r"-?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?"
SCALED_NUMBER = (
    rf"{NUMBER}\s*(?:천억\s*원|백억\s*원|십억\s*원|조\s*원|억\s*원|만\s*원|"
    r"천\s*원|천억원|백억원|십억원|조원|억원|만원|천원|천억|백억|십억|억|조|만|천|원)?"
)
_NEGATED_LITERAL_SUFFIX = re.compile(
    r"^\s*(?:형|유형|지역|시장|방식|상품|종목)?\s*"
    r"(?:가|이|은|는|을|를|으로|로|이나|나)?\s*"
    r"(?:아닌|아니|아님|지\s*않|하지\s*않|없|제외|이외|말고|빼고|"
    r"외(?:의|에는)?(?![0-9A-Za-z가-힣]))"
)

_SEMANTIC_COVERAGE_GATE = SemanticCoverageGate()

# Direction words that turn a metric surface into a ranking request. Kept in one
# place so registry-derived patterns speak the same language as the hand written
# rules below.
_RANK_DIRECTION_SUFFIX = (
    r"(?:이|가|은|는|을|를)?\s*(?:더\s*|가장\s*|제일\s*)?"
    r"(?:큰|크|작은|작|높은|높|낮은|낮|많은|많|적은|적|저렴한|비싼|우수한|좋은|"
    r"상위|하위|순)"
)
_HANGUL = re.compile(r"[가-힣]")


def _registry_surface_pattern(surface: str) -> str:
    """Match one registry surface while tolerating the writer's spacing.

    The left boundary stops a shorter surface from matching inside a longer one.
    Without it ``20거래일 변동성`` matches inside ``120거래일 변동성`` and answers with
    the wrong window, and ``발행금액`` matches inside ``총발행금액``.
    """

    parts = [re.escape(token) for token in surface.split() if token]
    return r"(?<![0-9A-Za-z가-힣])" + r"\s*".join(parts)


def registry_numeric_aliases(family: str) -> tuple[tuple[str, tuple[str, ...]], ...]:
    """Derive numeric filter aliases from the registry for one family.

    A missed filter surface is worse than a missed sort: the question still runs
    but the threshold silently disappears, so the answer lists products that do
    not meet the stated condition. Longer surfaces come first for the same
    reason as in :func:`registry_rank_patterns`.
    """

    registry = load_field_registry()
    entries: list[tuple[int, str, tuple[str, ...]]] = []
    for field_id, definition in registry.fields.items():
        if definition.value_type is not ValueType.NUMBER or not definition.queryable:
            continue
        try:
            resolved = registry.require_field(field_id, [family])
        except ValueError:
            continue
        if not {"gte", "lte", "gt", "lt"} & set(resolved.allowed_operators):
            continue
        surfaces = tuple(
            surface
            for surface in (resolved.label, *resolved.aliases)
            if surface and _HANGUL.search(surface) is not None
        )
        if surfaces:
            entries.append((max(len(item) for item in surfaces), field_id, surfaces))
    entries.sort(key=lambda item: (-item[0], item[1]))
    return tuple((field_id, surfaces) for _, field_id, surfaces in entries)


def registry_rank_patterns(family: str) -> tuple[tuple[str, str], ...]:
    """Derive ranking patterns from every numeric sortable field of a family.

    ``field_registry.yaml`` is the runtime control layer, so a surface it
    declares as a label or alias must be answerable. Hand written rules cover
    the phrasings that need extra context; this fills the rest so that adding an
    alias to the registry is enough to make it work.

    Longer surfaces come first so a specific one (``순자산가치``) is preferred over
    a shorter one it contains (``순자산``).
    """

    registry = load_field_registry()
    entries: list[tuple[int, str, str]] = []
    for field_id in registry.sortable_fields():
        try:
            definition = registry.require_field(field_id, [family])
        except ValueError:
            continue
        if definition.value_type is not ValueType.NUMBER:
            continue
        for surface in (definition.label, *definition.aliases):
            if not surface or _HANGUL.search(surface) is None:
                continue
            entries.append((len(surface), surface, field_id))
    entries.sort(key=lambda item: (-item[0], item[1]))

    # Ranking resolution prefers the shortest match, so a surface that is the
    # tail of a longer one steals it: ``세후 매수수익률`` lost to ``매수수익률``
    # and answered with the pre-tax field. Forbid the short surface exactly
    # where a longer registry surface would own the text.
    surfaces = {surface for _, surface, _ in entries}
    guards: dict[str, str] = {}
    for surface in surfaces:
        prefixes = sorted(
            {
                longer[: -len(surface)]
                for longer in surfaces
                if longer != surface and longer.endswith(surface)
            }
        )
        guards[surface] = "".join(
            f"(?<!{re.escape(prefix)})" for prefix in prefixes if prefix
        )

    return tuple(
        (
            guards[surface] + _registry_surface_pattern(surface) + _RANK_DIRECTION_SUFFIX,
            field_id,
        )
        for _, surface, field_id in entries
    )


_DATA_SCOPE_PREFIX = re.compile(
    r"^(?:(?:이번|현재|최신|공식|제공|배포(?:된)?)\s*)?"
    r"(?:DB|데이터(?:베이스)?|자료)(?:\s*(?:안|내|에서|의))?\s*",
    re.IGNORECASE,
)


def _literal_actor_value(value: str) -> str:
    """Remove a data-scope preamble from an extracted actor literal.

    Natural evaluation questions often begin with ``배포 DB에서`` or
    ``제공 데이터에서``.  Those words constrain the source of truth; they
    are not part of an asset-manager or issuer name.
    """

    cleaned = value.strip(" '‘’\"")
    while True:
        stripped = _DATA_SCOPE_PREFIX.sub("", cleaned, count=1).strip()
        if stripped == cleaned:
            return cleaned
        cleaned = stripped


def _looks_like_schema_scope(value: str) -> bool:
    """Return whether a loose possessive captured a field phrase, not an actor."""

    return re.search(
        r"(?:자산\s*군|자산\s*유형|투자\s*지역|지역|국가|거래\s*통화|"
        r"통화|거래소|시장|상품\s*유형|위험\s*등급|신용\s*등급|"
        r"수익률|보수(?:율)?|AUM|순자산|"
        r"(?:단기\s*자금|원자재|대체\s*(?:자산|투자)|혼합\s*자산)"
        r"(?:\s*(?:성격|노출|위주))?|"
        r"(?:국내|해외)\s*(?:ETF|ETN|ETP)(?:\s*(?:상품군|전체))?)\s*$",
        value.strip(),
        flags=re.IGNORECASE,
    ) is not None


def _explicit_name_order_direction(question: str) -> str | None:
    """Resolve lexical ordering only when a name/identifier field owns it."""

    name_surface = (
        r"(?:상품명|종목명|펀드명|ETF\s*명|ETN\s*명|채권명|짧은\s*이름|"
        r"이름|티커|종목\s*코드)"
    )
    if re.search(
        name_surface
        + r".{0,20}?(?:역순|내림차순|기준\s*뒤쪽|뒤에서\s*부터)",
        question,
        flags=re.IGNORECASE,
    ):
        return "desc"
    if re.search(
        name_surface
        + r".{0,20}?(?:가나다\s*순|알파벳\s*순|이름\s*순|오름차순|"
        r"앞선\s*순서|기준\s*앞쪽)",
        question,
        flags=re.IGNORECASE,
    ):
        return "asc"
    return None


def _official_enum_literal(
    field_id: str,
    dataset: str,
    raw_value: str,
) -> str | None:
    """Return the canonical official enum value for one explicit literal.

    The organizer schema, rather than a hand-maintained shortlist, owns the
    admissible values.  This keeps long labels such as ``Asia Pacific ex
    Japan`` available while preventing an arbitrary user string from becoming
    an executable SQL enum filter.
    """

    registry = load_field_registry()
    try:
        definition = registry.fields[field_id].resolve(dataset)
    except (KeyError, ValueError):
        return None
    normalized = re.sub(r"\s+", " ", raw_value).strip().casefold()
    for value in definition.enum_values:
        if re.sub(r"\s+", " ", value).strip().casefold() == normalized:
            return value
    return None


def _explicit_official_enum_matches(
    question: str,
    field_id: str,
    dataset: str,
    field_surface: str,
) -> tuple[tuple[re.Match[str], str], ...]:
    """Bind an official enum only when the caller labels the field.

    Both Korean word orders are common: ``자산유형이 Equity인`` and
    ``원자재 자산군``.  Values come directly from the approved Field Registry,
    so this grammar cannot invent a new SQL literal and automatically supports
    newly approved enum values without adding entity-specific branches.
    """

    registry = load_field_registry()
    try:
        definition = registry.fields[field_id].resolve(dataset)
    except (KeyError, ValueError):
        return ()
    values = sorted(definition.enum_values, key=lambda value: (-len(value), value))
    if not values:
        return ()
    value_pattern = "|".join(re.escape(value) for value in values)
    suffix = r"(?=\s*(?:인|이고|이며|만|중|가운데|대상|상품|종목|ETF|ETN|ETP|펀드|채권|[,.;:!?]|$))"
    patterns = (
        re.compile(
            rf"(?:{field_surface})(?:이|가|은|는)?\s*[:：]?\s*"
            rf"(?P<value>{value_pattern}){suffix}",
            re.IGNORECASE,
        ),
        re.compile(
            rf"(?P<value>{value_pattern})\s*(?:{field_surface})",
            re.IGNORECASE,
        ),
    )
    found: list[tuple[re.Match[str], str]] = []
    occupied: list[tuple[int, int]] = []
    for pattern in patterns:
        for match in pattern.finditer(question):
            if any(match.start() < end and start < match.end() for start, end in occupied):
                continue
            canonical = _official_enum_literal(field_id, dataset, match.group("value"))
            if canonical is not None:
                found.append((match, canonical))
                occupied.append(match.span())
    return tuple(found)


_EXPLICIT_TEXT_VALUE = (
    r"[0-9A-Za-z가-힣]"
    r"[0-9A-Za-z가-힣 .,&+_/'()·:%-]{0,239}?"
)


def _explicit_labeled_text_match(
    question: str,
    *,
    field_surface: str,
) -> tuple[re.Match[str], str, str] | None:
    """Capture one bounded literal only when its source field is explicit.

    Free-text organizer columns must not be inferred from an unlabelled theme
    or company mention.  Quoted equality, a family-bounded Korean equality,
    and an explicit ``contains`` construction are the only accepted forms.
    """

    patterns = (
        (
            re.compile(
                r"['‘“\"](?P<value>[^'’”\"\n]{1,240})['’”\"]\s*"
                r"(?:이|가|은|는|이라는|라는)?\s*"
                rf"(?:{field_surface})(?:에|에는|이|가)?\s*"
                r"(?:포함(?:된|되는|하는)|들어(?:간|가는|있는)|적힌)",
                re.IGNORECASE,
            ),
            "contains",
        ),
        (
            re.compile(
                r"(?<![0-9A-Za-z가-힣&·+._/-])"
                r"(?P<value>[0-9A-Za-z가-힣&·+._/-]{1,80})\s*"
                r"(?:이|가|은|는|이라는|라는)?\s*"
                rf"(?:{field_surface})(?:에|에는|이|가)?\s*"
                r"(?:포함(?:된|되는|하는)|들어(?:간|가는|있는)|적힌)",
                re.IGNORECASE,
            ),
            "contains",
        ),
        (
            re.compile(
                rf"(?:{field_surface})(?:에|에는|에서)\s*[:：]?\s*"
                r"['‘“\"](?P<value>[^'’”\"\n]{1,240})['’”\"]\s*"
                r"(?:이|가)?\s*(?:(?:문구|단어)(?:가|이)?\s*)?"
                r"(?:포함(?:된|되는|하는)|들어(?:간|가는|있는)|적힌)",
                re.IGNORECASE,
            ),
            "contains",
        ),
        (
            re.compile(
                rf"(?:{field_surface})(?:에|에는|에서)\s*[:：]?\s*"
                rf"(?P<value>{_EXPLICIT_TEXT_VALUE})\s*(?:이|가)?\s*"
                r"(?:(?:라는\s*)?(?:문구|단어)(?:가|이)?\s*)?"
                r"(?:포함(?:된|되는|하는)|들어(?:간|가는|있는)|적힌)",
                re.IGNORECASE,
            ),
            "contains",
        ),
        (
            re.compile(
                rf"(?:{field_surface})(?:이|가|은|는)?\s*[:：]?\s*"
                r"['‘“\"](?P<value>[^'’”\"\n]{1,240})['’”\"]"
                r"(?=\s*(?:인|이고|이며|만|[,.;:!?]|$))",
                re.IGNORECASE,
            ),
            "eq",
        ),
        (
            re.compile(
                rf"(?:{field_surface})(?:이|가|은|는)?\s*[:：]?\s*"
                rf"(?P<value>{_EXPLICIT_TEXT_VALUE})\s*인\s*"
                r"(?:해외\s*)?(?:ETF|ETN|ETP)",
                re.IGNORECASE,
            ),
            "eq",
        ),
    )
    for pattern, operator in patterns:
        match = pattern.search(question)
        if match is None:
            continue
        value = re.sub(r"\s+", " ", match.group("value")).strip(" '‘’“”\"")
        if value.casefold() in {"etf", "etn", "etp", "해외", "상품", "종목"}:
            continue
        return match, value, operator
    return None


def _risk_grade_number(value: str) -> int | None:
    """Map every official risk-label spelling to its numeric grade.

    The fund source preserves several spacing variants while the domestic ETP
    and bond sources use parenthesized labels.  Query compilation must select
    the official enum values themselves, not invent a new ordinal DB column.
    """

    compact = re.sub(r"\s+", "", value)
    explicit = re.search(r"([1-6])등급", compact)
    if explicit is not None:
        return int(explicit.group(1))
    for phrase, grade in (
        ("매우높은위험", 1),
        ("다소높은위험", 3),
        ("높은위험", 2),
        ("보통위험", 4),
        ("매우낮은위험", 6),
        ("낮은위험", 5),
    ):
        if compact == phrase:
            return grade
    return None


def _explicit_projection_fields(question: str, family: str) -> list[str]:
    """Link field labels and aliases that the user explicitly asks to see."""

    registry = load_field_registry()
    normalized_question = question.casefold()
    requested: list[str] = []
    for field, definition in registry.fields.items():
        if family not in definition.datasets or not definition.selectable:
            continue
        phrases = (definition.label, *definition.aliases)
        if any(phrase.casefold() in normalized_question for phrase in phrases if phrase.strip()):
            requested.append(field)
    return requested


# A calendar month is treated as 30 days so that "6개월 이내" keeps matching the
# 180-day bound the deterministic planner already used for "반년 안".
_MATURITY_UNIT_DAYS = {"년": 365, "개월": 30, "일": 1}


def _negated_literal_span(question: str, literal: str) -> str | None:
    """Return local text when an identical literal is semantically negated.

    A short lexical match must not hide the suffix in ``ETF가 아닌`` or
    ``미국을 제외한``.  If the same token appears once positively and once
    negatively, the string-only linker cannot identify which occurrence a
    downstream model meant, so the whole request still fails closed.
    """

    start = 0
    while True:
        index = question.find(literal, start)
        if index < 0:
            return None
        suffix = question[index + len(literal) :][:20]
        match = _NEGATED_LITERAL_SUFFIX.search(suffix)
        if match is not None:
            return question[index : index + len(literal) + match.end()].strip()
        start = index + max(len(literal), 1)


def _alias_group(aliases: tuple[str, ...]) -> str:
    """Join field aliases so a short one cannot match inside a longer word.

    Without the left boundary the leverage alias ``배수`` matches inside
    ``분배수익률`` and attaches a leverage threshold to a distribution-yield
    question. Longer aliases go first so the most specific one wins.
    """

    ordered = sorted(aliases, key=len, reverse=True)
    return r"(?<![0-9A-Za-z가-힣])(?:" + "|".join(re.escape(alias) for alias in ordered) + ")"


def _negated_comparison_span(
    question: str,
    aliases: tuple[str, ...],
) -> str | None:
    alias_pattern = _alias_group(aliases)
    match = re.search(
        rf"(?:{alias_pattern}).{{0,30}}?"
        r"(?:이하|미만|이상|초과|정확히|이전|이후|까지)"
        r"\s*(?:가|이|은|는)?\s*(?:아닌|아니|아님)",
        question,
        flags=re.IGNORECASE,
    )
    return None if match is None else match.group(0)


def _product_family(question: str) -> str | None:
    if "공모펀드" in question:
        return "fund"
    if re.search(r"해외\s*상장\s*지수\s*(?:펀드|상품|증권)", question):
        return "overseas_etp"
    if re.search(r"상장\s*지수\s*펀드", question):
        return "domestic_etp"
    if re.search(r"국내\s*상장\s*지수\s*증권", question):
        return "domestic_etp"
    if re.search(r"국내\s*상장\s*상품", question):
        return "domestic_etp"
    mentions_etp = re.search(r"ETF|ETN|ETP", question)
    # The approved universe contains public funds only.  A bare Korean
    # ``펀드``/``투자신탁`` therefore has one executable family unless the same
    # question explicitly names an ETP wrapper.
    if not mentions_etp and re.search(r"펀드|투자신탁", question):
        return "fund"
    if not mentions_etp and re.search(
        r"채권|회사채|국채|국공채|국고채|특수채|금융채|지역개발채|도시철도공채",
        question,
    ):
        return "bond"
    if "국내" in question:
        return "domestic_etp"
    if re.search(r"(?:NAS|NYS|AMX)(?:의|에서|\s*거래소)", question, re.IGNORECASE):
        return "overseas_etp"
    if "해외" in question:
        return "overseas_etp"
    return None


def _scaled_number(text: str) -> int | float:
    compact = (
        text.replace(",", "")
        .replace(" ", "")
        .replace("%", "")
        .replace("배", "")
        .removesuffix("일")
        .removesuffix("년")
    )
    scales = {
        "천억원": Decimal("100000000000"),
        "백억원": Decimal("10000000000"),
        "십억원": Decimal("1000000000"),
        "조원": Decimal("1000000000000"),
        "억원": Decimal("100000000"),
        "만원": Decimal("10000"),
        "천원": Decimal("1000"),
        "천억": Decimal("100000000000"),
        "백억": Decimal("10000000000"),
        "십억": Decimal("1000000000"),
        "억": Decimal("100000000"),
        "조": Decimal("1000000000000"),
        "만": Decimal("10000"),
        "천": Decimal("1000"),
        "원": Decimal("1"),
    }
    scale = Decimal("1")
    for suffix in scales:
        if compact.endswith(suffix):
            compact = compact[: -len(suffix)]
            scale = scales[suffix]
            break
    value = Decimal(compact) * scale
    return int(value) if value == value.to_integral_value() else float(value)


def _field_number(field: str, text: str, unit: str | None = None) -> int | float:
    """Normalize a captured number in the canonical unit of one field.

    ``remaining_days`` is stored in days even when a user states years or
    months. Other numeric fields already use the unit written in the public
    Field Registry, so their captured value is preserved.
    """

    value = _scaled_number(text)
    if field == "remaining_days" and unit in _MATURITY_UNIT_DAYS:
        return value * _MATURITY_UNIT_DAYS[unit]
    return value


_SMALL_KOREAN_INTEGERS = {
    "한": 1,
    "하나": 1,
    "두": 2,
    "둘": 2,
    "세": 3,
    "셋": 3,
    "네": 4,
    "넷": 4,
    "다섯": 5,
    "여섯": 6,
    "일곱": 7,
    "여덟": 8,
    "아홉": 9,
    "열": 10,
}


def _small_count(text: str) -> int:
    return int(text) if text.isdigit() else _SMALL_KOREAN_INTEGERS[text]


def _flexible_alias_pattern(aliases: tuple[str, ...]) -> str:
    """Match a frozen alias independently of optional Korean word spacing.

    Financial labels are commonly written with any of ``판매회사보수``,
    ``판매회사 보수`` or ``판매 회사 보수``.  Treating those spellings as
    separate semantic meanings caused a labelled numeric value to become an
    unrelated return threshold.  This helper changes spacing only; it never
    introduces a synonym or a field choice.
    """

    patterns: list[str] = []
    for alias in aliases:
        compact = re.sub(r"\s+", "", alias.strip())
        if compact:
            pattern = r"\s*".join(re.escape(character) for character in compact)
            # Do not match a numeric/Latin label inside a longer schema label
            # (``20거래일`` inside ``120거래일`` is the important case).
            if compact[:1].isascii() and compact[:1].isalnum():
                pattern = rf"(?<![0-9A-Za-z]){pattern}"
            if compact[-1:].isascii() and compact[-1:].isalnum():
                pattern = rf"{pattern}(?![0-9A-Za-z])"
            patterns.append(pattern)
    return "|".join(dict.fromkeys(patterns))


def _merge_registry_numeric_fields(
    numeric_fields: list[tuple[str, tuple[str, ...]]],
    family: str,
) -> list[tuple[str, tuple[str, ...]]]:
    """Add every approved numeric query field without replacing domain aliases."""

    merged = {field: list(aliases) for field, aliases in numeric_fields}
    registry = load_field_registry()
    for field, base_definition in registry.fields.items():
        if family not in base_definition.datasets or field == "buyable_quantity":
            continue
        definition = base_definition.resolve(family)
        if definition.value_type is not ValueType.NUMBER or not definition.queryable:
            continue
        phrases = merged.setdefault(field, [])
        for phrase in registry_field_phrases(definition):
            if phrase not in phrases:
                phrases.append(phrase)
    return [(field, tuple(phrases)) for field, phrases in merged.items()]


def _explicit_registry_constraints(question: str, family: str) -> list[dict[str, Any]]:
    """Parse only fully labelled enum/boolean/quoted-text constraints.

    Values remain Registry-owned.  Free text requires quotes and enum values
    must exactly equal an approved value, so adding newly exposed schema fields
    does not turn an unlabelled user phrase into SQL authority.
    """

    constraints: list[dict[str, Any]] = []
    mentions = registry_field_mentions(question, family, "queryable")
    for mention in mentions:
        field = mention.field_id
        definition = mention.definition
        if field in {"product_family", "buyable_quantity"}:
            continue
        tail = question[mention.end : mention.end + 280]
        prefix = r"^\s*(?:은|는|이|가)?\s*(?:[:：=]\s*)"
        if definition.value_type is ValueType.ENUM:
            values = sorted(definition.enum_values, key=lambda value: (-len(value), value))
            if not values:
                continue
            match = re.match(
                prefix
                + rf"(?P<value>{'|'.join(re.escape(value) for value in values)})"
                + r"(?=\s*(?:인|이고|이며|,|;|\.|$))",
                tail,
                flags=re.IGNORECASE,
            )
            if match is None:
                continue
            raw = match.group("value")
            value = next(
                candidate for candidate in values if candidate.casefold() == raw.casefold()
            )
            constraints.append({"field": field, "operator": "eq", "value": value})
        elif definition.value_type is ValueType.BOOLEAN:
            match = re.match(
                prefix + r"(?P<value>Y|N|예|아니오|true|false)"
                r"(?=\s*(?:인|이고|이며|,|;|\.|$))",
                tail,
                flags=re.IGNORECASE,
            )
            if match is None:
                continue
            constraints.append(
                {
                    "field": field,
                    "operator": "eq",
                    "value": match.group("value").casefold() in {"y", "예", "true"},
                }
            )
        elif definition.value_type is ValueType.STRING:
            contains = re.match(
                r"^\s*(?:에|에는|에서)\s*['‘“\"]"
                r"(?P<value>[^'’”\"\n]{1,240})['’”\"]\s*"
                r"(?:이|가)?\s*(?:포함(?:된|되는|하는)|들어간|적힌)",
                tail,
                flags=re.IGNORECASE,
            )
            equality = re.match(
                prefix
                + r"['‘“\"](?P<value>[^'’”\"\n]{1,240})['’”\"]"
                r"(?=\s*(?:인|이고|이며|,|;|\.|$))",
                tail,
                flags=re.IGNORECASE,
            )
            match = contains or equality
            operator = "contains" if contains is not None else "eq"
            if match is not None and operator in definition.allowed_operators:
                constraints.append(
                    {
                        "field": field,
                        "operator": operator,
                        "value": re.sub(r"\s+", " ", match.group("value")).strip(),
                    }
                )
    return constraints


def _registry_availability_hints(
    question: str,
    family: str,
) -> tuple[dict[str, Any], ...]:
    """Bind explicit field availability without inventing a data value.

    The organizer explicitly permits questions about supplied/missing values.
    Availability is therefore represented as a typed source-observation
    predicate, not as ``field = 0`` or an empty-string guess.  A field label
    must own the phrase, so generic wording such as ``정보가 없는 상품`` never
    gains execution authority.
    """

    availability_prefix = (
        r"^\s*(?:이|가|은|는)?\s*(?:모두\s*)?"
        r"(?:(?:명|값|수치|데이터|정보|칸)(?:이|가|은|는|에)?)?\s*"
    )
    missing_pattern = re.compile(
        availability_prefix
        + r"(?:비어\s*있(?:는|고|으며)|없(?:는|고|으며)|"
        r"공란(?:인|이고|이며|으로\s*된)?|누락(?:된|되어\s*있는)?|"
        r"기재(?:되어\s*)?있지\s*않(?:은|고)|기재되지\s*않(?:은|고)|"
        r"등록(?:되어\s*)?있지\s*않(?:은|고)|등록되지\s*않(?:은|고)|"
        r"기록(?:되어\s*)?있지\s*않(?:은|고)|기록되지\s*않(?:은|고)|"
        r"제공(?:되어\s*)?있지\s*않(?:은|고)|제공되지\s*않(?:은|고))",
        flags=re.IGNORECASE,
    )
    present_pattern = re.compile(
        availability_prefix
        + r"(?:있(?:는|고|으며)|기재(?:된|되어\s*있는)|등록(?:된|되어\s*있는)|"
        r"기록(?:된|되어\s*있는)|제공(?:된|되어\s*있는)|"
        r"입력(?:된|되어\s*있는)|확인(?:된|되는|되어\s*있는)|"
        r"유효(?:한|하고|하며)?)",
        flags=re.IGNORECASE,
    )
    coordinated_mentions = registry_field_mentions(question, family, "queryable")
    hints: list[dict[str, Any]] = []
    occupied: set[tuple[str, int, int]] = set()
    for mention_index, mention in enumerate(coordinated_mentions):
        tail = question[mention.end : mention.end + 56]
        missing = missing_pattern.match(tail)
        present = present_pattern.match(tail)
        match = missing or present
        if match is None:
            continue

        # A single availability predicate can own a contiguous AND-like list
        # of explicitly named fields: ``AUM과 일간 수익률이 있고`` or
        # ``1년 수익률·60일 변동성 유효``.  Walk backwards only across
        # registry-owned queryable mentions separated by pure coordination.
        # Any predicate, value, clause boundary, or OR-like connector stops
        # propagation, so an availability suffix cannot absorb an unrelated
        # field from an earlier clause.
        owned_mentions = [mention]
        cursor = mention_index - 1
        next_start = mention.start
        while cursor >= 0:
            previous = coordinated_mentions[cursor]
            connector = question[previous.end : next_start]
            if re.fullmatch(
                r"\s*(?:(?:와|과|및)|[,·ㆍ&+]\s*)\s*",
                connector,
                flags=re.IGNORECASE,
            ) is None:
                break
            owned_mentions.append(previous)
            next_start = previous.start
            cursor -= 1

        span_end = mention.end + match.end()
        span_start = min(item.start for item in owned_mentions)
        span = question[span_start:span_end].strip()
        operator = "is_missing" if missing is not None else "is_present"
        for owned in reversed(owned_mentions):
            key = (owned.field_id, owned.start, span_end)
            if key in occupied:
                continue
            occupied.add(key)
            hints.append(
                {
                    "field": owned.field_id,
                    "operator": operator,
                    "value": True,
                    "span": span,
                }
            )
    return tuple(hints)


def _explicit_registry_rank_matches(
    question: str,
    family: str,
) -> tuple[tuple[str, re.Match[str]], ...]:
    """Return field-labelled rank expressions backed by sortable metadata."""

    found: list[tuple[str, re.Match[str]]] = []
    for mention in registry_field_mentions(question, family, "sortable"):
        value_type = mention.definition.value_type
        if value_type is ValueType.DATE:
            cue = r"최신(?:\s*순)?|오래된\s*순|빠른\s*순|늦은\s*순|오름차순|내림차순"
        elif value_type in {ValueType.STRING, ValueType.ENUM, ValueType.BOOLEAN}:
            cue = r"가나다\s*순|알파벳\s*순|오름차순|내림차순"
        else:
            cue = (
                r"상위|하위|높은\s*순|낮은\s*순|큰\s*순|작은\s*순|"
                r"많은\s*순|적은\s*순|오름차순|내림차순"
            )
        pattern = re.compile(
            literal_field_pattern(mention.phrase)
            + rf"(?:이|가|은|는|을|를)?\s*(?:기준(?:으로)?\s*)?(?:{cue})",
            re.IGNORECASE,
        )
        match = pattern.search(question, mention.start)
        if match is not None and match.start() == mention.start:
            found.append((mention.field_id, match))
    deduplicated = {
        (field, match.start(), match.end()): (field, match)
        for field, match in found
    }
    return tuple(
        sorted(
            deduplicated.values(),
            key=lambda item: (item[1].start(), -(item[1].end() - item[1].start()), item[0]),
        )
    )


def _numeric_hint(
    question: str,
    field: str,
    aliases: tuple[str, ...],
    *,
    owned_field_mentions: tuple[Any, ...] = (),
) -> dict[str, Any] | None:
    alias_pattern = _flexible_alias_pattern(aliases)

    def crosses_another_field(match: re.Match[str], numeric_group: int) -> bool:
        """Reject a number whose nearest label belongs to another field.

        In ``일간 수익률이 있고 총보수 0.5% 이하`` the numeric value is
        owned by ``총보수``.  A bounded look-ahead alone used to copy 0.5 into
        the earlier return field.  Registry spans provide deterministic
        parameter ownership without relying on word distance tuning.
        """

        numeric_start = match.start(numeric_group)
        return any(
            mention.field_id != field
            and match.start() < mention.start
            and mention.end <= numeric_start
            for mention in owned_field_mentions
        )
    # Keep the complete inclusive interval owned by the labelled field.  The
    # public questions commonly use both ``3.2% 이상 3.4% 이하`` and
    # ``3.2%부터 3.4%까지``.  Previously the latter, and currency ranges such
    # as ``1억 달러 이상 10억 달러 이하``, degraded to a lower bound only.
    # That silently widened SQL and is more dangerous than rejecting a query.
    bounded = re.search(
        rf"(?:{alias_pattern}).{{0,16}}?(?P<bounded_value_a>{SCALED_NUMBER})\s*"
        r"(?P<bounded_unit_a>%|퍼센트|배|일|년|개월|주|좌|원)?\s*"
        r"(?:달러|USD|KRW)?\s*"
        r"(?:이상\s*(?:이고|이면서|,|부터)?|부터)\s*"
        rf"(?P<bounded_value_b>{SCALED_NUMBER})\s*"
        r"(?P<bounded_unit_b>%|퍼센트|배|일|년|개월|주|좌|원)?\s*"
        r"(?:달러|USD|KRW)?\s*(?:이하|까지)",
        question,
        flags=re.IGNORECASE,
    )
    if bounded and not crosses_another_field(bounded, bounded.re.groupindex["bounded_value_a"]):
        return {
            "field": field,
            "operator": "between",
            "value": [
                _field_number(
                    field,
                    bounded.group("bounded_value_a"),
                    bounded.group("bounded_unit_a"),
                ),
                _field_number(
                    field,
                    bounded.group("bounded_value_b"),
                    bounded.group("bounded_unit_b"),
                ),
            ],
        }
    tilde = re.search(
        rf"(?:{alias_pattern}).{{0,12}}?(?P<tilde_value_a>{SCALED_NUMBER})\s*"
        r"(?P<tilde_unit_a>%|퍼센트|배|일|년|개월|주|좌|원)?\s*"
        r"(?:~|∼|～)\s*"
        rf"(?P<tilde_value_b>{SCALED_NUMBER})\s*"
        r"(?P<tilde_unit_b>%|퍼센트|배|일|년|개월|주|좌|원)?",
        question,
        flags=re.IGNORECASE,
    )
    if tilde and not crosses_another_field(tilde, tilde.re.groupindex["tilde_value_a"]):
        unit_a = tilde.group("tilde_unit_a") or tilde.group("tilde_unit_b")
        unit_b = tilde.group("tilde_unit_b") or tilde.group("tilde_unit_a")
        return {
            "field": field,
            "operator": "between",
            "value": [
                _field_number(field, tilde.group("tilde_value_a"), unit_a),
                _field_number(field, tilde.group("tilde_value_b"), unit_b),
            ],
        }
    between = re.search(
        rf"(?:{alias_pattern}).{{0,12}}?(?P<between_value_a>{SCALED_NUMBER})\s*"
        r"(?P<between_unit_a>%|퍼센트|배|일|년|개월|주|원|좌)?\s*"
        rf"(?:달러|USD|KRW)?\s*(?:에서|부터)\s*(?P<between_value_b>{SCALED_NUMBER})\s*"
        r"(?P<between_unit_b>%|퍼센트|배|일|년|개월|주|원|좌)?\s*"
        rf"(?:달러|USD|KRW)?\s*사이",
        question,
        flags=re.IGNORECASE,
    )
    if between and not crosses_another_field(
        between,
        between.re.groupindex["between_value_a"],
    ):
        unit_a = between.group("between_unit_a") or between.group("between_unit_b")
        unit_b = between.group("between_unit_b") or between.group("between_unit_a")
        return {
            "field": field,
            "operator": "between",
            "value": [
                _field_number(field, between.group("between_value_a"), unit_a),
                _field_number(field, between.group("between_value_b"), unit_b),
            ],
        }
    prefixed_boundary = re.search(
        rf"(?:{alias_pattern}).{{0,16}}?"
        r"(최소|최대|적어도|많아야)\s*"
        rf"({SCALED_NUMBER})\s*(?:%|퍼센트|배|일|년|주|좌)?\s*(?:달러|USD|KRW)?",
        question,
        flags=re.IGNORECASE,
    )
    if prefixed_boundary and not crosses_another_field(prefixed_boundary, 2):
        # ``거래량 기준 최대 4개`` limits the number of returned rows; it
        # does not mean ``daily_volume <= 4``.  Keep result counters owned by
        # the shared limit parser instead of silently inventing a hard filter.
        following = question[prefixed_boundary.end() : prefixed_boundary.end() + 12]
        if re.match(r"\s*(?:개|건|종목|상품)(?!군)", following) is None:
            return {
                "field": field,
                "operator": (
                    "gte"
                    if prefixed_boundary.group(1) in {"최소", "적어도"}
                    else "lte"
                ),
                "value": _scaled_number(prefixed_boundary.group(2)),
            }
    comparison = re.search(
        rf"(?:{alias_pattern}).{{0,16}}?({SCALED_NUMBER})\s*(?:%|퍼센트|배|일|년|주|좌)?\s*"
        r"(?:달러|USD|KRW)?(?:을|를)?\s*"
        r"(이하|미만|이상|초과|정확히|밑(?:인|이고|이며)?|아래(?:인|이고|이며)?|"
        r"넘는|넘은|넘게|넘지\s*않는|안쪽(?:인|에\s*있는)?|"
        r"초과하지\s*않는|보다\s*(?:낮|작|적)지\s*않(?:은|는)|"
        r"보다\s*(?:높|크|많)지\s*않(?:은|는)|"
        r"보다\s*(?:낮(?:은|고|거나)|작(?:은|고|거나)|적(?:은|고|거나))|"
        r"보다\s*(?:높(?:은|고|거나)|큰|크거나|많(?:은|고|거나)))",
        question,
        flags=re.IGNORECASE,
    )
    if comparison and not crosses_another_field(comparison, 1):
        operators = {
            "이하": "lte",
            "미만": "lt",
            "이상": "gte",
            "초과": "gt",
            "정확히": "eq",
            "넘는": "gt",
            "넘은": "gt",
            "넘게": "gt",
            "넘지 않는": "lte",
            "안쪽": "lte",
            "안쪽인": "lte",
            "안쪽에 있는": "lte",
            "초과하지 않는": "lte",
            "보다 낮지 않은": "gte",
            "보다 낮지 않는": "gte",
            "보다 작지 않은": "gte",
            "보다 작지 않는": "gte",
            "보다 적지 않은": "gte",
            "보다 적지 않는": "gte",
            "보다 높지 않은": "lte",
            "보다 높지 않는": "lte",
            "보다 크지 않은": "lte",
            "보다 크지 않는": "lte",
            "보다 많지 않은": "lte",
            "보다 많지 않는": "lte",
        }
        boundary = re.sub(r"\s+", " ", comparison.group(2).strip())
        if boundary in operators:
            operator = operators[boundary]
        elif boundary.startswith(("밑", "아래", "보다 낮", "보다 작", "보다 적")):
            operator = "lt"
        elif boundary.startswith(("보다 높", "보다 크", "보다 큰", "보다 많")):
            operator = "gt"
        else:
            raise AssertionError(f"unmapped numeric boundary: {boundary}")
        return {
            "field": field,
            "operator": operator,
            "value": _scaled_number(comparison.group(1)),
        }
    polarity = re.search(
        rf"(?:{alias_pattern})(?:이|가|은|는)?\s*"
        r"(?P<polarity>음수|마이너스|0\s*(?:보다\s*)?(?:작(?:은)?|낮(?:은)?)|"
        r"양수|플러스|0\s*(?:보다\s*)?(?:큰|크|높(?:은)?))",
        question,
        flags=re.IGNORECASE,
    )
    if polarity is not None:
        surface = re.sub(r"\s+", "", polarity.group("polarity"))
        negative = surface.startswith(("음수", "마이너스", "0보다작", "0보다낮"))
        return {
            "field": field,
            "operator": "lt" if negative else "gt",
            "value": 0,
        }
    if field == "leverage_factor":
        direct = re.search(
            rf"({NUMBER})\s*배(?!\s*(?:이상|초과|이하|미만))",
            question,
        )
        if direct:
            return {
                "field": field,
                "operator": "eq",
                "value": _scaled_number(direct.group(1)),
            }
    return None


_NUMERIC_BOUNDARY_OPERATORS = {
    "이하": "lte",
    "미만": "lt",
    "이상": "gte",
    "초과": "gt",
}
_NUMERIC_COORDINATOR = re.compile(
    r"^\s*(?:,\s*)?(?:또는|혹은|아니면|이거나|거나|OR)\s*$",
    re.IGNORECASE,
)
_SHARED_METRIC_COORDINATOR = re.compile(r"\s*(?:·|/|,|및|와|과|하고)\s*")
_SHARED_METRIC_PERIOD = (
    r"(?:\d+\s*(?:거래일|일|주|개월|달|년)|한\s*달|석\s*달|반년|일주일)"
)


def _local_numeric_boundary(
    question: str,
    *,
    start: int,
    field: str,
) -> tuple[dict[str, Any], int] | None:
    """Parse a field-local numeric boundary immediately after one label."""

    prefixed = re.match(
        rf"(?:률|율)?(?:이|가|은|는|을|를)?\s*(?:[:：=]\s*)?"
        r"(?P<boundary>최소|적어도|최대|많아야)\s*"
        rf"(?P<value>{SCALED_NUMBER})\s*"
        r"(?P<unit>%|퍼센트|배|일|년|개월|주|좌|원)?\s*"
        r"(?:달러|USD|KRW)?",
        question[start : start + 96],
        flags=re.IGNORECASE,
    )
    if prefixed is not None:
        return (
            {
                "field": field,
                "operator": (
                    "gte"
                    if prefixed.group("boundary") in {"최소", "적어도"}
                    else "lte"
                ),
                "value": _field_number(
                    field,
                    prefixed.group("value"),
                    prefixed.group("unit"),
                ),
            },
            start + prefixed.end(),
        )
    match = re.match(
        rf"(?:률|율)?(?:이|가|은|는|을|를)?\s*(?:[:：=]\s*)?"
        rf"(?P<value>{SCALED_NUMBER})\s*"
        r"(?P<unit>%|퍼센트|배|일|년|개월|주|좌|원)?\s*"
        r"(?:달러|USD|KRW)?(?:을|를)?\s*"
        r"(?P<boundary>이하|미만|이상|초과)",
        question[start : start + 96],
        flags=re.IGNORECASE,
    )
    if match is None:
        return None
    return (
        {
            "field": field,
            "operator": _NUMERIC_BOUNDARY_OPERATORS[match.group("boundary")],
            "value": _field_number(field, match.group("value"), match.group("unit")),
        },
        start + match.end(),
    )


def _numeric_field_occurrences(
    question: str,
    numeric_fields: list[tuple[str, tuple[str, ...]]],
    owned_field_mentions: tuple[Any, ...],
) -> list[tuple[int, int, str]]:
    """Return non-overlapping, longest typed numeric field labels."""

    candidates = {
        (mention.start, mention.end, mention.field_id)
        for mention in owned_field_mentions
    }
    for field, aliases in numeric_fields:
        for match in re.finditer(_flexible_alias_pattern(aliases), question, re.IGNORECASE):
            candidates.add((match.start(), match.end(), field))
    ordered = sorted(candidates, key=lambda item: (item[0], -(item[1] - item[0]), item[2]))
    selected: list[tuple[int, int, str]] = []
    for candidate in ordered:
        start, end, _field = candidate
        overlapping = [
            item for item in selected if start < item[1] and item[0] < end
        ]
        if not overlapping:
            selected.append(candidate)
            continue
        if all((end - start) > (item[1] - item[0]) for item in overlapping):
            selected = [item for item in selected if item not in overlapping]
            selected.append(candidate)
    return sorted(selected)


def _shared_metric_constraints(
    question: str,
    numeric_fields: list[tuple[str, tuple[str, ...]]],
) -> list[dict[str, Any]]:
    """Expand ``20일·60일 변동성 모두 10% 이하`` without dropping a field."""

    match = re.search(
        rf"(?P<periods>{_SHARED_METRIC_PERIOD}(?:\s*(?:·|/|,|및|와|과|하고)\s*"
        rf"{_SHARED_METRIC_PERIOD})+)\s*(?:의\s*)?"
        r"(?P<metric>수익률|수익|성과|변동성|흔들림)"
        r"(?:이|가|은|는|을|를)?\s*(?:모두|전부|각각|각|둘\s*다)?\s*"
        rf"(?P<value>{SCALED_NUMBER})\s*"
        r"(?P<unit>%|퍼센트|배|일|년|개월|주|좌|원)?\s*"
        r"(?P<boundary>이하|미만|이상|초과)",
        question,
        flags=re.IGNORECASE,
    )
    if match is None:
        return []

    periods = [
        item.strip()
        for item in _SHARED_METRIC_COORDINATOR.split(match.group("periods"))
        if item.strip()
    ]
    compact_aliases: dict[str, set[str]] = defaultdict(set)
    for field, aliases in numeric_fields:
        compact_aliases[field].update(re.sub(r"\s+", "", alias).casefold() for alias in aliases)

    fields: list[str] = []
    for period in periods:
        surface = re.sub(r"\s+", "", period + match.group("metric")).casefold()
        owners = [field for field, aliases in compact_aliases.items() if surface in aliases]
        if len(owners) != 1:
            return []
        fields.append(owners[0])
    if len(fields) != len(set(fields)):
        return []
    operator = _NUMERIC_BOUNDARY_OPERATORS[match.group("boundary")]
    return [
        {
            "field": field,
            "operator": operator,
            "value": _field_number(field, match.group("value"), match.group("unit")),
        }
        for field in fields
    ]


def _coordinated_numeric_constraints(
    question: str,
    numeric_fields: list[tuple[str, tuple[str, ...]]],
    owned_field_mentions: tuple[Any, ...],
) -> tuple[list[dict[str, Any]], list[list[dict[str, Any]]]]:
    """Extract explicit AND/OR numeric clauses while preserving their connective."""

    additions = _shared_metric_constraints(question, numeric_fields)
    any_groups: list[list[dict[str, Any]]] = []
    occurrences = _numeric_field_occurrences(
        question,
        numeric_fields,
        owned_field_mentions,
    )
    clauses: list[tuple[int, int, dict[str, Any]]] = []
    for start, end, field in occurrences:
        parsed = _local_numeric_boundary(question, start=end, field=field)
        if parsed is None:
            continue
        constraint, clause_end = parsed
        clauses.append((start, clause_end, constraint))

        # The second side may inherit the already explicit field:
        # ``1년 수익률 10% 이상 또는 -5% 이하``.
        inherited = re.match(
            rf"\s*(?:,\s*)?(?:또는|혹은|아니면|이거나|거나|OR)\s*"
            rf"(?P<value>{SCALED_NUMBER})\s*"
            r"(?P<unit>%|퍼센트|배|일|년|개월|주|좌|원)?\s*"
            r"(?P<boundary>이하|미만|이상|초과)",
            question[clause_end : clause_end + 96],
            flags=re.IGNORECASE,
        )
        if inherited is not None:
            alternative = {
                "field": field,
                "operator": _NUMERIC_BOUNDARY_OPERATORS[inherited.group("boundary")],
                "value": _field_number(
                    field,
                    inherited.group("value"),
                    inherited.group("unit"),
                ),
            }
            if alternative not in additions:
                additions.append(alternative)
            if constraint not in additions:
                additions.append(constraint)
            any_groups.append([constraint, alternative])

    # Two fully labelled clauses joined by an explicit alternative connective.
    for left, right in zip(clauses, clauses[1:], strict=False):
        bridge = question[left[1] : right[0]]
        if _NUMERIC_COORDINATOR.fullmatch(bridge):
            group = [left[2], right[2]]
            for constraint in group:
                if constraint not in additions:
                    additions.append(constraint)
            if group not in any_groups:
                any_groups.append(group)
    return additions, any_groups


_NONNEGATIVE_PRESENT_FIELDS = frozenset(
    {
        "total_expense_ratio_pct",
        "aum",
        "close_price",
        "daily_trading_value",
        "daily_volume",
        "tracking_error_pct",
        "applied_yield_pct",
        "coupon_rate_pct",
        "buy_yield_pct",
        "after_tax_yield_pct",
        "distribution_yield_pct",
        "distribution_payments_per_year",
        "one_month_average_trading_value",
        "issue_amount",
        "total_issue_amount",
        "evaluation_price",
        "dirty_price",
        "remaining_days",
        "duration_years",
        "sale_company_fee_pct",
        "management_company_fee_pct",
        "trustee_fee_pct",
        "domestic_stock_ratio_pct",
        "domestic_bond_ratio_pct",
        "overseas_stock_ratio_pct",
        "overseas_bond_ratio_pct",
        "latest_distribution_pct",
        "preservation_ratio_pct",
    }
)


def _numeric_presence_hint(
    question: str,
    field: str,
    aliases: tuple[str, ...],
) -> dict[str, Any] | None:
    """Lower explicit non-missing language only for non-negative measures.

    The organizer's 2026-08-26 rule treats zero and null numeric values as
    unavailable for filtering/ranking.  ``AUM이 잡히는`` and ``보수
    데이터가 있는`` can therefore become ``> 0`` for measures whose
    domain is non-negative.  Return fields are intentionally excluded because
    a negative return is still a valid observed value.
    """

    if field not in _NONNEGATIVE_PRESENT_FIELDS:
        return None
    alias_pattern = _flexible_alias_pattern(aliases)
    present = re.search(
        rf"(?:{alias_pattern})(?:이|가|은|는|을|를)?\s*"
        r"(?:(?:값|수치|데이터|정보)(?:이|가)?\s*)?"
        r"(?:있(?:는|고)|잡히(?:는|고)|표시(?:된|되는)|"
        r"기재(?:된|되는)|제공(?:된|되는))",
        question,
        flags=re.IGNORECASE,
    )
    if present is None:
        return None
    return {"field": field, "operator": "gt", "value": 0}


_COORDINATED_FILTER_BRIDGE = re.compile(
    r"^\s*(?:이나|나|또는|혹은|및|와|과|하고|/|·|,)\s*$",
    re.IGNORECASE,
)


def _coalesce_coordinated_filter_aliases(
    question: str,
    required: list[dict[str, Any]],
    matches: tuple[Any, ...],
) -> None:
    """Turn explicit alternatives for the same field into one locked IN.

    Multiple ``eq`` constraints would mean AND in SQL and can silently produce
    an empty set.  Coalescing is allowed only when adjacent typed aliases are
    joined by an explicit Korean/ASCII alternative coordinator.
    """

    by_field: dict[str, list[Any]] = defaultdict(list)
    for match in matches:
        if match.operator == "eq":
            by_field[match.field_id].append(match)
    for field, field_matches in by_field.items():
        ordered = sorted(field_matches, key=lambda item: (item.start, item.end))
        clusters: list[list[Any]] = []
        current: list[Any] = []
        for match in ordered:
            if not current:
                current = [match]
                continue
            bridge = question[current[-1].end : match.start]
            if _COORDINATED_FILTER_BRIDGE.fullmatch(bridge):
                current.append(match)
            else:
                clusters.append(current)
                current = [match]
        if current:
            clusters.append(current)
        for cluster in clusters:
            values = list(dict.fromkeys(item.value for item in cluster))
            if len(values) < 2:
                continue
            required[:] = [
                item
                for item in required
                if not (
                    item["field"] == field
                    and item["operator"] == "eq"
                    and item["value"] in values
                )
            ]
            hint = {"field": field, "operator": "in", "value": values}
            if hint not in required:
                required.append(hint)


def _excluded_numeric_hint(
    question: str,
    field: str,
    aliases: tuple[str, ...],
) -> dict[str, Any] | None:
    """Invert a simple excluded numeric range into an executable constraint.

    ``잔존일수 365일 초과는 빼고`` means ``remaining_days <= 365``.
    Treating the visible ``초과`` as a positive constraint silently reverses
    the request, so only this small, explicit exclusion grammar is lowered.
    """

    alias_pattern = _flexible_alias_pattern(aliases)
    comparison = re.search(
        rf"(?:{alias_pattern}).{{0,16}}?({SCALED_NUMBER})\s*(?:%|퍼센트|배|일|년)?\s*"
        r"(?:달러|USD|KRW)?\s*(이하|미만|이상|초과)"
        r"(?P<bridge>.{0,20}?)(?:제외(?:하고|한|해)?|빼고|말고)",
        question,
        flags=re.IGNORECASE,
    )
    if comparison is None:
        return None
    if re.search(r"빠뜨리|누락|빼먹|잊", comparison.group("bridge"), re.IGNORECASE):
        # ``4% 이상이라는 조건을 빠뜨리지 말고`` reinforces the lower
        # bound; it does not negate or exclude that bound.
        return None
    inverse_operators = {
        "이하": "gt",
        "미만": "gte",
        "이상": "lt",
        "초과": "lte",
    }
    return {
        "field": field,
        "operator": inverse_operators[comparison.group(2)],
        "value": _scaled_number(comparison.group(1)),
    }


def _date_hint(
    question: str,
    field: str,
    aliases: tuple[str, ...],
) -> dict[str, Any] | None:
    alias_pattern = _flexible_alias_pattern(aliases)
    date_token = r"(?:\d{4}-\d{2}-\d{2}|\d{4}\s*년\s*\d{1,2}\s*월\s*\d{1,2}\s*일)"

    def canonical_date(surface: str) -> str | None:
        compact = surface.strip()
        korean = re.fullmatch(
            r"(?P<year>\d{4})\s*년\s*(?P<month>\d{1,2})\s*월\s*"
            r"(?P<day>\d{1,2})\s*일",
            compact,
        )
        try:
            if korean is not None:
                return date(
                    int(korean.group("year")),
                    int(korean.group("month")),
                    int(korean.group("day")),
                ).isoformat()
            return date.fromisoformat(compact).isoformat()
        except ValueError:
            return None

    between = re.search(
        rf"(?:{alias_pattern})(?:이|가|은|는)?\s*(?:기준(?:으로)?\s*)?"
        rf"({date_token})\s*"
        rf"(?:에서|부터)\s*({date_token})\s*(?:사이|까지)",
        question,
    )
    if between:
        lower = canonical_date(between.group(1))
        upper = canonical_date(between.group(2))
        if lower is not None and upper is not None:
            return {
                "field": field,
                "operator": "between",
                "value": [lower, upper],
            }
    leading_between = re.search(
        rf"({date_token})\s*(?:에서|부터)\s*({date_token})\s*"
        rf"(?:사이|까지)\s*(?:에\s*)?(?:{alias_pattern})(?:된|한|인)?",
        question,
        flags=re.IGNORECASE,
    )
    if leading_between is not None:
        lower = canonical_date(leading_between.group(1))
        upper = canonical_date(leading_between.group(2))
        if lower is not None and upper is not None:
            return {
                "field": field,
                "operator": "between",
                "value": [lower, upper],
            }
    leading_comparison = re.search(
        rf"({date_token})\s*"
        r"(이전|이후|이하|이상|초과|정확히|까지|부터)"
        rf"\s*(?:에\s*)?(?:{alias_pattern})(?:된|한|인)?",
        question,
        flags=re.IGNORECASE,
    )
    if leading_comparison is not None:
        operators = {
            "이전": "lt",
            "이후": "gte",
            "이하": "lte",
            "이상": "gte",
            "초과": "gt",
            "정확히": "eq",
            "까지": "lte",
            "부터": "gte",
        }
        value = canonical_date(leading_comparison.group(1))
        if value is not None:
            return {
                "field": field,
                "operator": operators[leading_comparison.group(2)],
                "value": value,
            }
    comparison = re.search(
        rf"(?:{alias_pattern})(?:이|가|은|는)?\s*(?:기준(?:으로)?\s*)?"
        rf"({date_token})\s*"
        r"(이전|이후|이하|이상|초과|정확히|까지|부터)",
        question,
    )
    if comparison:
        operators = {
            "이전": "lt",
            "이후": "gte",
            "이하": "lte",
            "이상": "gte",
            "초과": "gt",
            "정확히": "eq",
            "까지": "lte",
            "부터": "gte",
        }
        value = canonical_date(comparison.group(1))
        if value is not None:
            return {
                "field": field,
                "operator": operators[comparison.group(2)],
                "value": value,
            }
    year_range = re.search(
        rf"(?:(?:{alias_pattern}).{{0,12}}?)?"
        r"(?P<start>\d{4})\s*년\s*부터\s*(?P<end>\d{4})\s*년\s*까지"
        rf"(?:.{{0,12}}?(?:{alias_pattern})(?:된|한|인)?)?",
        question,
        flags=re.IGNORECASE,
    )
    if year_range is not None and re.search(alias_pattern, year_range.group(0), re.IGNORECASE):
        return {
            "field": field,
            "operator": "between",
            "value": [f"{year_range.group('start')}-01-01", f"{year_range.group('end')}-12-31"],
        }
    exact_year = re.search(
        rf"(?:(?:{alias_pattern})(?:이|가|은|는)?\s*)?"
        r"(?P<year>\d{4})\s*년(?:에|인)"
        rf"(?:.{{0,8}}?(?:{alias_pattern})(?:된|한|인)?)?",
        question,
        flags=re.IGNORECASE,
    )
    if exact_year is not None and re.search(alias_pattern, exact_year.group(0), re.IGNORECASE):
        return {
            "field": field,
            "operator": "between",
            "value": [f"{exact_year.group('year')}-01-01", f"{exact_year.group('year')}-12-31"],
        }
    year_boundary = re.search(
        rf"(?:{alias_pattern})(?:이|가|은|는)?\s*(?P<year>\d{{4}})\s*년\s*"
        r"(?P<boundary>이후|이상|부터|이전|이하|까지)",
        question,
        flags=re.IGNORECASE,
    )
    if year_boundary is not None:
        lower = year_boundary.group("boundary") in {"이후", "이상", "부터"}
        return {
            "field": field,
            "operator": "gte" if lower else "lte",
            "value": (
                f"{year_boundary.group('year')}-01-01"
                if lower
                else f"{year_boundary.group('year')}-12-31"
            ),
        }
    return None


def build_lexical_hints(
    question: str,
    product_family_hint: str | None = None,
    *,
    force_product_family_hint: bool = False,
) -> dict[str, Any]:
    question = semantic_query_surface(normalize_user_question(question))
    question = re.sub(
        r"(?m)(?:^|(?<=[.!?])\s+)"
        r"순위값의\s*방향이\s*높은\s*순인지\s*낮은\s*순인지"
        r"[^.!?]*(?:[.!?]|$)",
        " ",
        question,
        flags=re.IGNORECASE,
    ).strip()
    family = (
        product_family_hint
        if force_product_family_hint and product_family_hint in SEARCH_PROJECTION_BY_FAMILY
        else _product_family(question)
    )
    if family is None and product_family_hint in SEARCH_PROJECTION_BY_FAMILY:
        family = product_family_hint
    required: list[dict[str, Any]] = []
    any_constraint_groups: list[list[dict[str, Any]]] = []
    semantic_ambiguity_spans: list[str] = []
    semantic_unsupported_spans: list[str] = []
    explicit_fund_major_type: re.Match[str] | None = None
    explicit_overseas_region: str | None = None
    explicit_enum_owners: list[tuple[int, int, str, Any]] = []
    availability_spans: list[str] = []

    def add(field: str, value: Any, operator: str = "eq") -> None:
        hint = {"field": field, "operator": operator, "value": value}
        if hint not in required:
            required.append(hint)

    negated_product_types = {
        product_type: span
        for product_type in ("ETF", "ETN")
        if (span := _negated_literal_span(question, product_type)) is not None
    }
    explicit_product_type = re.search(
        r"(?:ETP\s*유형|ETF\s*여부|상품\s*유형)(?:이|가|은|는)?\s*[:：]?\s*(ETF|ETN)",
        question,
        re.IGNORECASE,
    )
    if explicit_product_type is None:
        explicit_product_type = re.search(
            r"(?<![A-Z])(ETF|ETN)(?:\s*유형(?:이|가|은|는)?\s*"
            r"(?:인|이며|이고|인데|에\s*해당)|"
            r"라면|이라면|인(?!지)|이며|이고|인데|에\s*해당|로\s*되어)",
            question,
            re.IGNORECASE,
        )
    explicit_product_type_set = re.search(
        r"(?:ETP\s*유형|ETF\s*ETN\s*구분|상품\s*유형)"
        r"(?:이|가|은|는)?\s*[:：]?\s*"
        r"ETF\s*(?:또는|및|와|과|나|·|/)\s*ETN|"
        r"ETF인지\s*ETN인지\s*(?:알려|구분|확인)",
        question,
        re.IGNORECASE,
    )
    conditional_product_type = re.search(
        r"(?<![A-Z])(ETF|ETN)(?:라면|이라면)",
        question,
        re.IGNORECASE,
    )
    if explicit_product_type_set is not None and conditional_product_type is None:
        for product_type in ("ETF", "ETN"):
            if product_type not in negated_product_types:
                add("product_type", product_type)
    elif (
        explicit_product_type is not None
        and explicit_product_type.group(1).upper() not in negated_product_types
    ):
        add("product_type", explicit_product_type.group(1).upper())
    else:
        positive_product_types = [
            product_type
            for product_type in ("ETF", "ETN")
            if re.search(rf"(?<![A-Z]){product_type}(?![A-Z])", question, re.IGNORECASE)
            and product_type not in negated_product_types
        ]
        if len(positive_product_types) == 1:
            add("product_type", positive_product_types[0])
    explicit_manager_text: tuple[re.Match[str], str, str] | None = None
    if family in {"domestic_etp", "overseas_etp"}:
        for product_type in negated_product_types:
            add("product_type", product_type, "neq")
        if len(negated_product_types) == 2:
            semantic_ambiguity_spans.append("ETF·ETN 모두 제외")
    else:
        semantic_ambiguity_spans.extend(negated_product_types.values())
    if family == "fund":
        negated_public = _negated_literal_span(question, "공모")
        if negated_public is None:
            add("public_offering", True)
        else:
            semantic_unsupported_spans.append(negated_public)

    # Explicit field/value syntax is owned before short literal aliases.  This
    # distinguishes ``투자지역이 국내`` from the different fund field
    # ``국내외 펀드 구분=국내`` and prevents a shorter nested enum (``Global``)
    # from widening ``Global Ex US``.
    explicit_enum_specs = []
    if family in {"domestic_etp", "overseas_etp"}:
        explicit_enum_specs.append(
            (
                "asset_type",
                r"(?:투자\s*자산(?:\s*(?:군|유형|종류|클래스))?|"
                r"자산\s*(?:군|유형|종류|클래스))",
            )
        )
    if family in {"domestic_etp", "overseas_etp", "fund"}:
        explicit_enum_specs.append(
            (
                "investment_region",
                r"(?:투자\s*)?(?:지역|국가)",
            )
        )
    if family == "overseas_etp":
        explicit_enum_specs.append(
            (
                "index_replication_method",
                r"(?:지수|인덱스)?\s*복제\s*(?:방법|방식)",
            )
        )
    for field_id, field_surface in explicit_enum_specs:
        for match, value in _explicit_official_enum_matches(
            question,
            field_id,
            family,
            field_surface,
        ):
            parenthetical = re.match(
                r"\s*\(\s*(?P<value>[^()]{1,80}?)\s*\)",
                question[match.end() :],
            )
            parenthetical_value = (
                None
                if parenthetical is None
                else _official_enum_literal(
                    field_id,
                    family,
                    parenthetical.group("value"),
                )
            )
            if parenthetical_value is not None and parenthetical_value != value:
                # An adjacent parenthetical official label is the precise
                # machine-readable value; the preceding Korean phrase is its
                # human explanation.  Bind only the exact official value so
                # ``글로벌 지역(Global Ex US)`` cannot widen into an OR over
                # both ``글로벌`` and ``Global Ex US``.
                add(field_id, parenthetical_value)
                owner_end = match.end() + parenthetical.end()
                explicit_enum_owners.append(
                    (match.start(), owner_end, field_id, parenthetical_value)
                )
                if family == "overseas_etp" and field_id == "investment_region":
                    explicit_overseas_region = parenthetical_value
                continue
            add(field_id, value)
            explicit_enum_owners.append((match.start(), match.end(), field_id, value))
            if family == "overseas_etp" and field_id == "investment_region":
                explicit_overseas_region = value

    if family is not None:
        for constraint in _explicit_registry_constraints(question, family):
            add(
                constraint["field"],
                constraint["value"],
                constraint["operator"],
            )
        for availability in _registry_availability_hints(question, family):
            add(
                availability["field"],
                availability["value"],
                availability["operator"],
            )
            availability_spans.append(availability["span"])

    if family in {"domestic_etp", "overseas_etp"}:
        if re.search(r"상장\s*지수\s*펀드", question, flags=re.IGNORECASE):
            add("product_type", "ETF")
        if re.search(r"상장\s*지수\s*증권", question, flags=re.IGNORECASE):
            add("product_type", "ETN")

    common_lookup_patterns = [
        (
            r"상품\s*(?:ID|아이디|번호)(?:가|는|은|이)?\s*[:：]?\s*"
            r"([A-Z0-9._:-]{2,30})",
            "product_id",
        ),
        (
            r"(?:종목\s*(?:코드|번호)|티커)(?:가|는|은|이)?\s*[:：]?\s*"
            r"([A-Z0-9._:-]{2,30})",
            "ticker",
        ),
    ]
    for pattern, field in common_lookup_patterns:
        match = re.search(pattern, question, flags=re.IGNORECASE)
        if match:
            identity = match.group(1)
            if negated_identity := _negated_literal_span(question, identity):
                semantic_ambiguity_spans.append(negated_identity)
            else:
                canonical_identity_field = (
                    "product_id" if family == "fund" and field == "ticker" else field
                )
                add(canonical_identity_field, identity, "eq")
    explicit_name_filters = (
        (
            re.compile(
                r"(?:정식\s*)?(?:상품명|종목명|펀드명|ETF명|ETN명|채권명|상품\s*이름)"
                r"(?:이|은|는)?\s*[:：]?\s*"
                r"['‘“\"](?P<value>[^'’”\"]{1,80})['’”\"]\s*(?:인|으로\s*표시|$)",
                re.IGNORECASE,
            ),
            "eq",
        ),
        (
            re.compile(
                r"(?:정식\s*)?(?:상품명|종목명|펀드명|ETF명|ETN명|채권명|상품\s*이름)"
                r"(?:(?:이|은|는)\s*|[:：]\s*)"
                r"(?P<value>[0-9A-Za-z가-힣&·+._/ -]{2,80}?)\s*인\s*"
                r"(?:(?:국내|해외|원화)\s*)?(?:ETF|ETN|ETP|채권|공모\s*펀드|펀드)",
                re.IGNORECASE,
            ),
            "eq",
        ),
        (
            re.compile(
                r"(?:상품명|종목명|펀드명|ETF명|ETN명|채권명|상품\s*이름)(?:에|에는)\s*"
                r"(?P<value>[0-9A-Za-z가-힣&·+._/-]{1,60}?)"
                r"(?:이?라는\s*(?:문구|단어)(?:가|이)?|(?:가|이))?\s*"
                r"(?:들어간|포함된|붙은)",
                re.IGNORECASE,
            ),
            "contains",
        ),
        (
            re.compile(
                r"(?P<value>[0-9A-Za-z가-힣&·+._/-]{1,60})\s*"
                r"(?:상품명|종목명|펀드명|ETF명|ETN명|채권명|상품\s*이름)"
                r"(?:이|에)?\s*(?:들어간|붙은|포함된)",
                re.IGNORECASE,
            ),
            "contains",
        ),
    )
    for name_pattern, operator in explicit_name_filters:
        explicit_name = name_pattern.search(question)
        if explicit_name is not None:
            add("product_name", explicit_name.group("value").strip(), operator)
            break
    for match in re.finditer(
        r"(?<![A-Z0-9])([A-Z0-9][A-Z0-9._:-]{1,29})"
        r"\s*(?:을|를|은|는)?\s*(?:제외|이외|말고|빼고|외(?:에는|의)?)",
        question,
        flags=re.IGNORECASE,
    ):
        if match.group(1).upper() in {"ETF", "ETN", "ETP"}:
            continue
        semantic_ambiguity_spans.append(match.group(0))

    explicit_fund_scope: re.Match[str] | None = None
    explicit_fund_currency: re.Match[str] | None = None
    if family == "overseas_etp":
        explicit_fund_currency = re.search(
            r"(?:펀드|상품)\s*통화\s*코드(?:가|는|은|이)?\s*([A-Z]{3})",
            question,
            flags=re.IGNORECASE,
        )
    if family == "domestic_etp":
        if re.search(
            r"국내\s+(?:주식|채권|원자재|대체투자|단기자금)\s*(?:형\s*)?ETF",
            question,
            flags=re.IGNORECASE,
        ):
            add("investment_region", "국내")
        if re.search(r"투자\s*지역(?:이|은|가)?\s*국내|국내(?:에|로)\s*투자", question):
            add("investment_region", "국내")
        if re.search(r"국내\s*지역(?:에|으로)\s*투자", question):
            add("investment_region", "국내")
        if re.search(r"주식(?:에|으로|을)\s*투자(?:하는|한)", question):
            add("asset_type", "주식")
        if re.search(r"채권(?:에|으로|을)\s*투자(?:하는|한)", question):
            add("asset_type", "채권")
        if re.search(r"단기\s*자금(?:에|으로|을)\s*투자(?:하는|한)", question):
            add("asset_type", "단기자금")
        for surface, value in (
            ("주식", "주식"),
            ("채권", "채권"),
            ("원자재", "원자재"),
            ("대체투자", "대체투자"),
            ("단기자금", "단기자금"),
        ):
            if re.search(
                rf"투자\s*자산\s*군(?:이|은|가)?\s*{re.escape(surface)}",
                question,
            ):
                add("asset_type", value)
            elif re.search(
                rf"{re.escape(surface)}(?:에|로|으로|을|를)?\s*"
                r"(?:주로\s*)?투자(?:하는|한|하는\s*형태)",
                question,
            ):
                # The investment predicate makes this a schema filter rather
                # than a company/holding mention. Values remain registry-owned.
                add("asset_type", value)
            elif re.search(
                rf"국내\s+{re.escape(surface)}\s*(?:형\s*)?(?:ETF|ETN|ETP)",
                question,
                flags=re.IGNORECASE,
            ):
                add("asset_type", value)
        if re.search(r"^국내\s+(?:채권형|혼합자산형)", question):
            add("investment_region", "국내")
    elif family == "fund":
        explicit_fund_major_type = re.search(
            r"제로인\s*대유형(?:이|은|가)?\s*"
            r"(?P<value>[0-9A-Za-z가-힣&·+._/-]+?)"
            r"(?=\s*인\s*(?:공모\s*)?펀드|\s*(?:이고|이며|으로))",
            question,
            flags=re.IGNORECASE,
        )
        if explicit_fund_major_type is not None:
            add("fund_major_type", explicit_fund_major_type.group("value"))
        explicit_fund_scope = re.search(
            r"해외\s*펀드\s*구분(?:이|은|가)?\s*(국내|해외)",
            question,
            flags=re.IGNORECASE,
        )
        if explicit_fund_scope is not None:
            add("fund_geography_scope", explicit_fund_scope.group(1))
        domestic_investment = re.search(
            r"국내(?:에|로)?\s*투자",
            question,
            flags=re.IGNORECASE,
        )
        if domestic_investment is not None:
            add("investment_region", "국내")
    elif family == "overseas_etp":
        explicit_asset_type = re.search(
            r"(?:투자\s*자산\s*군|자산\s*군)(?:이|은|가)?\s*"
            r"(?P<value>[0-9A-Za-z][0-9A-Za-z .&+/_-]{0,60}?)"
            r"(?=\s*(?:인|(?:로|으로)\s*(?:분류|표시|기재|등록)"
            r"(?:된|되어|되고|되며|되었고|되었으며)))",
            question,
            flags=re.IGNORECASE,
        )
        if explicit_asset_type is not None:
            if value := _official_enum_literal(
                "asset_type",
                family,
                explicit_asset_type.group("value"),
            ):
                add("asset_type", value)
            else:
                semantic_ambiguity_spans.append(explicit_asset_type.group(0))
        explicit_region_literal = re.search(
            r"투자\s*지역(?:이|은|가)?\s*"
            r"(?P<value>[0-9A-Za-z][0-9A-Za-z .&+/_-]{0,80}?)"
            r"(?=\s*(?:인|(?:로|으로)\s*(?:분류|표시|기재|등록)"
            r"(?:된|되어|되고|되며|되었고|되었으며)))",
            question,
            flags=re.IGNORECASE,
        )
        if explicit_region_literal is not None:
            if value := _official_enum_literal(
                "investment_region",
                family,
                explicit_region_literal.group("value"),
            ):
                add("investment_region", value)
                explicit_overseas_region = value
            else:
                semantic_ambiguity_spans.append(explicit_region_literal.group(0))
        for surface, value in (
            ("Equity", "Equity"),
            ("Bond", "Bond"),
            ("Commodity", "Commodity"),
            ("Alternatives", "Alternatives"),
            ("Mixed Assets", "Mixed Assets"),
            ("Money Market", "Money Market"),
        ):
            if re.search(
                rf"(?:투자\s*자산\s*군|자산\s*군)(?:이|은|가)?\s*"
                rf"{re.escape(surface)}(?=\s|인|이고|이며|$)",
                question,
                flags=re.IGNORECASE,
            ):
                add("asset_type", value)
            elif re.search(
                rf"{re.escape(surface)}(?:로|으로)\s*"
                r"(?:분류|표시|기재|등록)"
                r"(?:된|되어|되고|되며|되었고|되었으며)",
                question,
                flags=re.IGNORECASE,
            ):
                add("asset_type", value)
            elif re.search(
                rf"{re.escape(surface)}\s*(?:투자\s*)?자산\s*군(?:의|에|인)?\s*"
                r"(?:해외\s*)?(?:ETF|ETN|ETP)",
                question,
                flags=re.IGNORECASE,
            ):
                add("asset_type", value)
            elif re.search(
                rf"{re.escape(surface)}\s*(?:해외\s*)?(?:ETF|ETN|ETP)",
                question,
                flags=re.IGNORECASE,
            ):
                add("asset_type", value)
        for surface, value in (
            ("United States of America", "United States of America"),
            ("Global Emerging Markets", "Global Emerging Markets"),
            ("Global Ex US", "Global Ex US"),
            ("Global", "Global"),
            ("China", "China"),
            ("Japan", "Japan"),
            ("India", "India"),
            ("Europe", "Europe"),
        ):
            if re.search(
                rf"(?:투자\s*지역(?:이|은|가)?\s*{re.escape(surface)}"
                r"(?=\s|인|이고|이며|$)|"
                rf"{re.escape(surface)}(?:로|으로)\s*(?:표시|분류|기재|등록)"
                r"(?:된|되어|되고|되며|되었고|되었으며))",
                question,
                flags=re.IGNORECASE,
            ):
                if explicit_overseas_region is None:
                    # Ordered longest-first above: once a specific official
                    # label owns the span, do not additionally admit its
                    # shorter prefix (``Global`` inside ``Global Emerging
                    # Markets``).
                    add("investment_region", value)
                    explicit_overseas_region = value
            elif re.search(
                rf"{re.escape(surface)}\s*(?:투자\s*)?지역(?:의|에|인)?\s*"
                r"(?:해외\s*)?(?:ETF|ETN|ETP)",
                question,
                flags=re.IGNORECASE,
            ):
                add("investment_region", value)
            elif re.search(
                rf"{re.escape(surface)}\s*(?:(?:Equity|Bond|Commodity|Alternatives|"
                r"Mixed\s+Assets|Money\s+Market)\s*)?(?:해외\s*)?(?:ETF|ETN|ETP)",
                question,
                flags=re.IGNORECASE,
            ):
                add("investment_region", value)
        explicit_region_set = re.search(
            r"투자\s*지역(?:이|은|가)?\s*"
            r"((?:[A-Za-z ]+\s*[,·/]\s*)+[A-Za-z ]+)\s*중\s*하나",
            question,
            flags=re.IGNORECASE,
        )
        if explicit_region_set is not None:
            allowed_regions = {
                "china": "China",
                "japan": "Japan",
                "india": "India",
                "europe": "Europe",
                "global": "Global",
            }
            for token in re.split(r"\s*[,·/]\s*", explicit_region_set.group(1)):
                if value := allowed_regions.get(token.strip().casefold()):
                    add("investment_region", value)
        if re.search(r"Global\s*Ex\s*US", question, flags=re.IGNORECASE):
            add("investment_region", "Global Ex US")
        for surface, value in (("NAS", "NAS"), ("NYS", "NYS"), ("AMX", "AMX")):
            if re.search(
                rf"거래소\s*코드(?:가|는|은|이)?\s*{surface}(?=\s|인|이고|이며|$)",
                question,
                flags=re.IGNORECASE,
            ):
                add("exchange_code", value)
            elif re.search(
                rf"{surface}\s*(?:거래소\s*코드|에서\s*거래|의)",
                question,
                flags=re.IGNORECASE,
            ):
                add("exchange_code", value)
        if re.search(
            r"글로벌\s*(?:투자(?:\s*지역)?|주식형|채권형|원자재형|혼합형|"
            r".{0,20}?(?:해외\s*)?(?:ETF|ETN|ETP))",
            question,
            flags=re.IGNORECASE,
        ):
            add("investment_region", "Global")

    if family in {"domestic_etp", "overseas_etp"}:
        explicit_manager = re.search(
            r"운용사(?:가|는|은|이)?\s*[:：]?\s*"
            r"(?P<value>[0-9A-Za-z가-힣&·+._/ -]{1,80}?)"
            r"(?=\s*인\s*(?:(?:국내|해외)\s*)?(?:ETF|ETN|ETP))",
            question,
            flags=re.IGNORECASE,
        )
        if explicit_manager is not None:
            manager_value = _literal_actor_value(explicit_manager.group("value"))
            if manager_value:
                add("manager", manager_value, "eq")

        # Colloquial manager predicates still carry an explicit actor and
        # therefore must not disappear before SQL.  Use ``contains`` for the
        # shortened brand surface (for example ``미래에셋`` versus the official
        # ``미래에셋자산운용``), while the labelled ``운용사: ...`` grammar above
        # retains exact-match semantics.
        natural_manager = re.search(
            r"(?:^|(?:중|가운데|중에서)\s*)"
            r"(?P<value>[0-9A-Za-z가-힣&·+._/-][0-9A-Za-z가-힣&·+._/ -]{0,39}?)\s*"
            r"(?:가|이|에서)\s*"
            r"(?:운용(?:하는|하고\s*있는|\s*중인)|굴리는|관리하는|운영하는|발행한)\s*"
            r"(?:(?:국내|해외)\s*)?(?:ETF|ETN|ETP)",
            question,
            flags=re.IGNORECASE,
        )
        if natural_manager is not None:
            manager_value = _literal_actor_value(natural_manager.group("value"))
            if manager_value:
                required[:] = [item for item in required if item["field"] != "manager"]
                add("manager", manager_value, "contains")

        # The noun ``이름`` belongs to its nearest labelled field.  Without
        # this owner, ``운용사 이름에 Vanguard가 들어간`` is captured by the
        # generic product-name grammar as ``product_name CONTAINS Vanguard``.
        # Parse the longer label first, then suppress only an overlapping
        # generic-name match below.
        explicit_manager_text = _explicit_labeled_text_match(
            question,
            field_surface=r"(?:운용사(?:명|\s*이름)?)",
        )
        if explicit_manager_text is not None:
            _, manager_value, manager_operator = explicit_manager_text
            required[:] = [item for item in required if item["field"] != "manager"]
            add("manager", _literal_actor_value(manager_value), manager_operator)

    explicit_product_name = _explicit_labeled_text_match(
        question,
        field_surface=(
            r"(?:상품\s*이름|상품명|종목명|펀드명|ETF\s*명|ETN\s*명|채권명|이름)"
        ),
    )
    product_name_owned_by_manager = (
        explicit_product_name is not None
        and explicit_manager_text is not None
        and explicit_product_name[0].start() < explicit_manager_text[0].end()
        and explicit_manager_text[0].start() < explicit_product_name[0].end()
    )
    if explicit_product_name is not None and not product_name_owned_by_manager:
        match, value, operator = explicit_product_name
        if negated_value := _negated_literal_span(question, value):
            semantic_ambiguity_spans.append(negated_value)
        else:
            required[:] = [item for item in required if item["field"] != "product_name"]
            add("product_name", value, operator)

    # Static financial meanings are resolved by the one versioned Typed Alias
    # Registry shared with Router, Compiler coverage and the span ledger.
    # Dynamic values (numbers, dates, IDs and captured names) remain in their
    # dedicated parsers above and below this boundary.
    typed_filter_aliases = filter_alias_matches(question, family)
    for alias in typed_filter_aliases:
        if any(
            alias.start < end
            and start < alias.end
            and (alias.field_id != field_id or alias.value != value)
            for start, end, field_id, value in explicit_enum_owners
        ):
            # The longer labelled field/value expression owns the span.  A
            # short literal alias may agree, but it may never reinterpret it as
            # a different field or a less specific value.
            continue
        if (
            explicit_fund_major_type is not None
            and alias.field_id in {"fund_geography_scope", "fund_management_attribute"}
            and alias.start < explicit_fund_major_type.end()
            and alias.end > explicit_fund_major_type.start()
        ):
            # ``제로인 대유형`` is an official source field.  Do not replace an
            # exact value such as ``해외채권형`` with a looser geography +
            # management-attribute decomposition.
            continue
        if (
            explicit_overseas_region is not None
            and alias.field_id == "investment_region"
            and alias.value != explicit_overseas_region
        ):
            # A longer official region label (for example Global Emerging
            # Markets) owns the span; a nested ``Global`` alias must not widen
            # the SQL filter.
            continue
        if negated_alias := _negated_literal_span(question, alias.text):
            semantic_ambiguity_spans.append(negated_alias)
            continue
        if alias.field_id == "manager":
            # A context-bound brand alias is more precise than the generic
            # actor capture and may also normalize a Korean brand spelling to
            # the official manager value (for example 블랙록 -> BlackRock).
            required[:] = [item for item in required if item["field"] != "manager"]
        add(alias.field_id, alias.value, alias.operator)
    _coalesce_coordinated_filter_aliases(question, required, typed_filter_aliases)

    etp_family = family in {"overseas_etp", "domestic_etp"}
    displayed_leverage_factor = re.search(
        r"(?:레버리지\s*)?배수(?:\s*표기)?(?:가|는|은|이)?\s*"
        r"(-?\d+(?:\.\d+)?)\s*(?:배)?\s*"
        r"(?:인(?=\s*(?:국내|해외|ETF|ETN|ETP|상장지수상품))|"
        r"(?:으로|로)\s*(?:적힌|표시(?:된|되어)?|기재(?:된|되어)?))",
        question,
        flags=re.IGNORECASE,
    )
    if etp_family and displayed_leverage_factor is not None:
        add("leverage_factor", float(displayed_leverage_factor.group(1)))
    if etp_family and (
        leverage_factor := re.search(
            r"(?:레버리지\s*)?배수(?:가|는|은|이)?\s*"
            r"(-?\d+(?:\.\d+)?)\s*(?:배)?\s*"
            r"(?P<boundary>이상|초과|이하|미만)?(?=\s*(?:인|이고|이며|인\s*것|상품|종목|$))",
            question,
            flags=re.IGNORECASE,
        )
    ):
        boundary = leverage_factor.group("boundary")
        operator = {
            None: "eq",
            "이상": "gte",
            "초과": "gt",
            "이하": "lte",
            "미만": "lt",
        }[boundary]
        add("leverage_factor", float(leverage_factor.group(1)), operator)
    negated_trade_available = re.search(
        r"(?:현재\s*)?거래(?:가|는|이)?\s*가능.{0,8}"
        r"(?:하지\s*않|아닌|없)",
        question,
    )
    if negated_trade_available is not None:
        semantic_ambiguity_spans.append(negated_trade_available.group(0))
    if etp_family and "현재 거래 가능" in question and negated_trade_available is None:
        add("sellable", True)
        add("trading_suspended", False)
    if etp_family and re.search(r"매매(?:가|는|이)?\s*가능", question):
        add("sellable", True)
    if (
        etp_family
        and negated_trade_available is None
        and re.search(
            r"거래\s*중(?:인|이고|이며|인\s*상품|인\s*종목)?",
            question,
        )
        and re.search(r"거래\s*(?:중지|정지)", question) is None
    ):
        add("sellable", True)
        add("trading_suspended", False)
    excluded_non_suspended = re.search(
        r"거래\s*(?:중지|정지)(?:가|는|이)?\s*"
        r"(?:아니|아닌|아님|되지\s*않(?:은|는)?)"
        r".{0,24}?(?:상품|종목|것)?(?:은|는|을|를)?\s*"
        r"(?:제외(?:하고|한|해)?|빼고|말고)",
        question,
    )
    negated_trading_suspension = re.search(
        r"거래(?:가|는|이)?\s*(?:중지|정지)(?:도|가|는|이)?\s*"
        r"(?:되지\s*않|아니|아닌|아님)|"
        r"거래\s*(?:중지|정지)"
        r"(?:된|인)?\s*(?:상품|종목|것)?"
        r"(?:은|는|을|를)?\s*(?:제외(?:하고|한|해)?|빼고|말고)|"
        r"거래(?:가|는|이)?\s*멈추지\s*않|"
        r"(?<![가-힣])(?:거래\s*)?(?:중지|정지)(?:가|는|이)?\s*"
        r"되지\s*않",
        question,
    )
    if etp_family and excluded_non_suspended is not None:
        # ``정지 아닌 상품을 제외`` keeps only suspended products.  The
        # exclusion reverses the already-negated predicate once more.
        add("trading_suspended", True)
    elif etp_family and negated_trading_suspension is not None:
        add("trading_suspended", False)
    if etp_family and re.search(
        r"레버리지(?:형|\s*상품|\s*종목)?(?:은|는|을|를)?\s*"
        r"(?:제외(?:하고|한|해)?|빼고|말고)",
        question,
        re.IGNORECASE,
    ):
        # The official field is a numeric leverage factor.  Values above 1
        # are leveraged exposure; inverse (negative) and ordinary 1x products
        # are not silently collapsed into the excluded class.
        add("leverage_factor", 1, "lte")
    elif etp_family and re.search(
        r"레버리지(?:형|\s*상품|\s*종목)?.{0,40}?"
        r"(?:제외(?:하고|한|해)?|빼고|말고)",
        question,
        re.IGNORECASE,
    ):
        # Support coordinated exclusions such as ``레버리지형과 거래정지
        # 상품을 빼고``.  The exclusion verb still has to be in the same
        # short clause, so a later unrelated ranking cannot negate leverage.
        add("leverage_factor", 1, "lte")
    excluded_sale_unavailable = re.search(
        r"판매(?:\s*(?:여부|상태))?(?:가|이|은|는)?\s*"
        r"(?:불가|불가능|할\s*수\s*없(?:는|음)?)"
        r".{0,24}?(?:상품|종목|것)?(?:은|는|을|를)?\s*"
        r"(?:제외(?:하고|한|해)?|빼고|말고)",
        question,
    )
    excluded_sale_available = re.search(
        r"판매(?:\s*(?:여부|상태))?(?:가|이|은|는)?\s*가능(?:한|인)?"
        r".{0,24}?(?:상품|종목|것)?(?:은|는|을|를)?\s*"
        r"(?:제외(?:하고|한|해)?|빼고|말고)",
        question,
    )
    negated_sale_available = re.search(
        r"판매(?:\s*(?:여부|상태))?(?:가|이|은|는)?\s*"
        r"(?:가능\s*하지\s*않|가능(?:한|인)?\s*"
        r"(?:상품|종목|것)?(?:이|가|은|는)?\s*아닌)",
        question,
    )
    if etp_family and excluded_sale_unavailable is not None:
        add("sellable", True)
    elif etp_family and excluded_sale_available is not None:
        add("sellable", False)
    elif etp_family and negated_sale_available is not None:
        add("sellable", False)
    elif etp_family and re.search(
        r"판매(?:\s*(?:여부|상태))?(?:가|이|은|는)?\s*가능",
        question,
    ):
        add("sellable", True)
    elif etp_family and re.search(r"(?:현재\s*)?판매\s*중(?:인|이고|이며)?", question):
        add("sellable", True)
    if etp_family and excluded_sale_unavailable is None and re.search(
        r"판매(?:\s*(?:여부|상태))?(?:가|이|은|는)?\s*"
        r"(?:불가|불가능|완료|중지)|판매할\s*수\s*없",
        question,
    ):
        add("sellable", False)
    if (
        etp_family
        and negated_trading_suspension is None
        and excluded_non_suspended is None
        and re.search(
            r"거래(?:가)?\s*(?:중지|정지)"
            r"(?!\s*(?:(?:가\s*)?(?:아니|아닌|아님)|되지\s*않))"
            r"(?:된|됨| 상태)?",
            question,
        )
    ):
        add("trading_suspended", True)
    if family == "domestic_etp":
        excluded_pension_ineligible = re.search(
            r"연금\s*거래\s*(?:불가|불가능).{0,20}?"
            r"(?:제외(?:하고|한|해)?|빼고|말고)",
            question,
        )
        negated_pension = re.search(
            r"연금.{0,16}(?:거래|구매)?(?:가|는|이)?\s*가능.{0,8}"
            r"(?:하지\s*않|아닌|없)",
            question,
        )
        explicit_pension_eligible = re.search(
            r"연금(?:\s*거래)?\s*가능(?!\s*여부)",
            question,
            flags=re.IGNORECASE,
        )
        if excluded_pension_ineligible is not None:
            add("pension_eligible", True)
        elif negated_pension is not None:
            add("pension_eligible", False)
        elif explicit_pension_eligible is not None:
            add("pension_eligible", True)
        elif (
            re.search(
                r"연금(?:\s*계좌)?(?:에서|으로|로|에서도|로도)?\s*"
                r"(?:(?:사고\s*팔|거래|구매)(?:가)?\s*(?:가능|(?:할\s*)?수\s*있)|"
                r"거래가?\s*가능)",
                question,
            )
            and re.search(r"연금.{0,16}?가능\s*여부", question) is None
        ):
            add("pension_eligible", True)
        elif re.search(
            r"연금\s*계좌(?:에|에서|로)?\s*(?:담을|편입할|거래할|살)\s*"
            r"수\s*있(?:는|고|다)",
            question,
        ):
            add("pension_eligible", True)
        if (
            negated_pension is None
            and re.search(r"연금.{0,16}?가능\s*여부", question) is None
            and re.search(
                r"(?:퇴직\s*)?연금.{0,16}(?:편입|거래).{0,8}(?:가능|Y)",
                question,
            )
        ):
            required[:] = [item for item in required if item["field"] != "pension_eligible"]
            add("pension_eligible", True)
        if re.search(r"주식(?:을|이)?\s*기초\s*자산", question):
            add("asset_type", "주식")
        if excluded_pension_ineligible is None and any(
            phrase in question
            for phrase in ("연금 거래가 불가능", "연금 거래 불가", "연금 거래 불가능")
        ):
            required[:] = [item for item in required if item["field"] != "pension_eligible"]
            add("pension_eligible", False)
        if "핵심" in question and "ETF" in question:
            add("core_etf", True)
        for risk in (
            "매우높은위험(1등급)",
            "높은위험(2등급)",
            "다소높은위험(3등급)",
            "보통위험(4등급)",
            "낮은위험(5등급)",
            "매우낮은위험(6등급)",
        ):
            loose = risk.replace("(", " ").replace(")", "")
            if risk in question or loose in question:
                add("risk_level", risk)
        for phrase, value in (
            ("실물복제", "실물복제"),
            ("합성복제", "합성복제"),
            ("액티브 전략", "액티브"),
            ("전략 코드 C", "C"),
        ):
            if phrase in question:
                add("strategy", value)

        lookup_patterns = [
            (
                r"(?:국내\s*)?ETF\s*[‘'\"]([^’'\"]+)[’'\"]",
                "short_name",
                "eq",
            ),
            (r"운용사에\s*(.+?)이 포함", "manager", "contains"),
            (r"약어명에\s*(.+?)(?:가|이) 들어", "short_name", "contains"),
            (r"기초지수에\s*(.+?)(?:가|이) 포함", "base_index", "contains"),
            (
                r"종목코드(?:가|는|은|이)?\s*[:：]?\s*([A-Z0-9]+)(?:인|입니다|$|\s)",
                "ticker",
                "eq",
            ),
            (
                r"상품번호(?:가|는|은|이)?\s*[:：]?\s*([A-Z0-9]+)(?:인|입니다|$|\s)",
                "product_id",
                "eq",
            ),
        ]
        for pattern, field, operator in lookup_patterns:
            match = re.search(pattern, question)
            if match:
                value = match.group(1).strip()
                if negated_value := _negated_literal_span(question, value):
                    semantic_ambiguity_spans.append(negated_value)
                else:
                    add(field, value, operator)
        manager = re.search(
            r"(.{1,40}?)\s*(?:가|이)\s*운용하는\s*(?:국내\s*)?(?:ETF|ETN)",
            question,
        )
        if manager is not None:
            manager_value = _literal_actor_value(manager.group(1))
            if manager_value:
                required[:] = [item for item in required if item["field"] != "manager"]
                add("manager", manager_value, "contains")
        bare_manager = re.search(
            r"(?:^|[.!?。]\s*|(?:첫째|둘째|셋째|넷째),?\s*)"
            r"([0-9A-Za-z가-힣&·+_ -]{2,60}?)\s+운용\s+(?:국내\s*)?(?:ETF|ETN)",
            question,
            flags=re.IGNORECASE,
        )
        if bare_manager is not None:
            manager_value = _literal_actor_value(bare_manager.group(1))
            if manager_value and not _looks_like_schema_scope(manager_value) and (
                re.search(
                    r"(?:상품\s*(?:ID|아이디|번호)|종목\s*코드|티커)$",
                    manager_value,
                    flags=re.IGNORECASE,
                )
                is None
            ):
                required[:] = [item for item in required if item["field"] != "manager"]
                add("manager", manager_value, "contains")
        stated_manager = re.search(
            r"([0-9A-Za-z가-힣&·+._/-]{1,40})(?:가|이)\s*"
            r"운용사(?:로)?\s*(?:적힌|표시|기재)(?:된|되어)?",
            question,
        )
        if stated_manager is not None:
            add("manager", stated_manager.group(1), "eq")
    if family == "fund":
        company_sale_context = re.search(
            r"(?:당사|미래에셋(?:증권)?).{0,12}판매",
            question,
        )
        fund_sale_unavailable = re.search(
            r"판매(?:\s*(?:여부|상태))?(?:가|이|은|는)?\s*"
            r"(?:가능\s*하지\s*않|가능(?:한|인)?\s*"
            r"(?:상품|종목|것)?(?:이|가|은|는)?\s*아닌)",
            question,
        )
        if fund_sale_unavailable is not None:
            add("sellable", False)
        elif (
            re.search(
                r"판매(?:\s*(?:여부|상태))?(?:가|이|은|는|도)?\s*가능|"
                r"살\s*수\s*있",
                question,
            )
            and company_sale_context is None
        ):
            add("sellable", True)
        if re.search(r"판매\s*중", question):
            add("sellable", True)
        if re.search(r"판매\s*중(?:으로)?\s*(?:적힌|표시|기재)", question):
            add("sellable", True)
        if any(phrase in question for phrase in ("판매가 완료", "판매 완료")):
            add("sellable", False)
        if re.search(
            r"(?:우리\s*회사|당사|미래에셋(?:증권)?)(?:에서|에서도)?\s*"
            r"(?:팔\s*수\s*있(?:는|고)|판매할\s*수\s*있(?:는|고)|"
            r"판매(?:하는|\s*중인|\s*상품)?|취급하는|.{0,12}?(?:Y|가능))",
            question,
            re.IGNORECASE,
        ):
            add("company_sellable", True)
        if "개인용" in question or "개인 투자자 대상" in question or "개인으로 분류" in question:
            add("investor_type", "개인")
        if "법인용" in question or "법인 투자자 대상" in question or "법인으로 분류" in question:
            add("investor_type", "법인")
        # Own polarity before the generic ``환헤지`` positive branch.
        # Korean speakers express the same negative predicate as both
        # ``환헤지를 하지 않는`` and ``환헤지는 안 하는``.  The
        # exclusion form reverses that predicate once more.
        unhedged_surface = (
            r"(?:환\s*헤지(?:는|은|를|가|도)?\s*"
            r"(?:안\s*하(?:는|고|며)?|하지\s*않(?:는|고|으며)?|"
            r"없(?:는|이|음)?)|"
            r"환\s*노출)"
        )
        excluded_unhedged = re.search(
            unhedged_surface
            + r".{0,20}?(?:제외(?:하고|한|해)?|빼고|말고)",
            question,
        )
        if excluded_unhedged is not None:
            add("currency_hedged", True)
        elif re.search(unhedged_surface, question) is not None:
            add("currency_hedged", False)
        elif any(
            phrase in question for phrase in ("환헤지", "환율 변동을 막는", "환율 변동을 줄이는")
        ):
            add("currency_hedged", True)
        explicit_fund_risk = None
        for risk in (
            "매우높은위험(1등급)",
            "높은위험(2등급)",
            "다소높은위험(3등급)",
            "보통위험(4등급)",
            "낮은위험(5등급)",
            "매우낮은위험(6등급)",
        ):
            loose = risk.replace("(", " ").replace(")", "")
            if risk in question or loose in question:
                explicit_fund_risk = risk
                add("risk_level", risk)
                break
        exact_fund_risk_label = re.search(
            r"(?:(높은\s*위험|낮은\s*위험)으로\s*(?:표기|기재|적힌)|"
            r"위험\s*등급\s*명(?:이|은|가)?\s*(높은\s*위험|낮은\s*위험)(?:인|으로))",
            question,
        )
        if explicit_fund_risk is None and exact_fund_risk_label is not None:
            exact_label = next(
                value for value in exact_fund_risk_label.groups() if value is not None
            )
            add("risk_level", re.sub(r"\s+", " ", exact_label), "eq")
        fund_risk_aliases = (
            (r"매우\s*높은\s*위험(?:\s*\(?1등급\)?)?", ["매우 높은 위험", "매우높은위험(1등급)"]),
            (r"다소\s*높은\s*위험(?:\s*\(?3등급\)?)?", ["다소 높은 위험", "다소높은위험(3등급)"]),
            (
                r"(?<!매우\s)(?<!다소\s)높은\s*위험(?:\s*\(?2등급\)?)?",
                ["높은 위험", "높은위험", "높은위험(2등급)"],
            ),
            (r"보통\s*위험(?:\s*\(?4등급\)?)?", ["보통 위험", "보통위험", "보통위험(4등급)"]),
            (r"매우\s*낮은\s*위험(?:\s*\(?6등급\)?)?", ["매우 낮은 위험", "매우낮은위험(6등급)"]),
            (r"(?<!매우\s)낮은\s*위험(?:\s*\(?5등급\)?)?", ["낮은 위험", "낮은위험(5등급)"]),
        )
        if explicit_fund_risk is None and exact_fund_risk_label is None:
            for pattern, values in fund_risk_aliases:
                if re.search(pattern, question):
                    add("risk_level", values, "in")
        lookup_patterns = [
            (
                r"상품번호(?:가|는|은|이)?\s*[:：]?\s*([A-Z0-9]+)",
                "product_id",
                "eq",
            ),
            (r"짧은 이름에\s*(.+?)(?:가|이)\s*들어간", "short_name", "contains"),
            (
                r"(?:정식\s*)?상품명에\s*(.+?)(?:가|이)\s*포함된",
                "product_name",
                "contains",
            ),
            (
                r"클래스\s*판매채널(?:이|은|가)?\s*[:：]?\s*(온라인|오프라인)",
                "sales_channel",
                "eq",
            ),
        ]
        for pattern, field, operator in lookup_patterns:
            match = re.search(pattern, question)
            if match:
                value = match.group(1).strip()
                if negated_value := _negated_literal_span(question, value):
                    semantic_ambiguity_spans.append(negated_value)
                else:
                    add(field, value, operator)
        if re.search(
            r"온라인\s*(?:클래스|판매\s*(?:채널)?|으로\s*(?:판매|가입|매수)|"
            r"에서\s*(?:판매|가입|매수))",
            question,
        ):
            required[:] = [item for item in required if item["field"] != "sales_channel"]
            add("sales_channel", "온라인", "contains")
        if re.search(
            r"오프라인\s*(?:판매(?:\s*채널)?|클래스|으로\s*(?:판매|가입|매수)|"
            r"에서\s*(?:판매|가입|매수))",
            question,
        ):
            required[:] = [item for item in required if item["field"] != "sales_channel"]
            add("sales_channel", "오프라인", "contains")
        if re.search(r"이자배당구분(?:이|은|가)?\s*배당|배당형", question):
            add("interest_dividend_type", "배당")
        if re.search(r"이자배당구분(?:이|은|가)?\s*이자|이자형", question):
            add("interest_dividend_type", "이자")
        if re.search(r"(?:수수료를?\s*따로\s*)?(?:징구하지\s*않|수수료\s*미징구)", question):
            add("fee_type", "수수료미징구")
        if re.search(r"(?:수수료\s*선취|선취\s*수수료)", question):
            add("fee_type", "수수료선취")
        if re.search(r"(?:수수료\s*후취|후취\s*수수료)", question):
            add("fee_type", "수수료후취")
        for field, aliases in (
            ("domestic_stock_ratio_pct", r"국내\s*주식\s*(?:구성\s*비율|비중)"),
            ("domestic_bond_ratio_pct", r"국내\s*채권\s*(?:구성\s*비율|비중)"),
            ("overseas_stock_ratio_pct", r"해외\s*주식\s*(?:구성\s*비율|비중)"),
            ("overseas_bond_ratio_pct", r"해외\s*채권\s*(?:구성\s*비율|비중)"),
        ):
            if re.search(rf"{aliases}.{{0,16}}절반(?:을|보다)?\s*(?:넘|초과)", question):
                add(field, 50, "gt")

    if family is not None:
        for date_mention in registry_field_mentions(
            question,
            family,
            "queryable",
            value_types=frozenset({ValueType.DATE}),
        ):
            date_phrases = registry_field_phrases(date_mention.definition)
            if negated_date := _negated_comparison_span(question, date_phrases):
                semantic_ambiguity_spans.append(negated_date)
                continue
            date_hint = _date_hint(
                question,
                date_mention.field_id,
                date_phrases,
            )
            if date_hint is not None:
                add(
                    date_hint["field"],
                    date_hint["value"],
                    date_hint["operator"],
                )

    if family == "bond":
        buy_unavailable = re.search(
            r"(?:매수|구매|주문)(?:가|는|이)?\s*가능.{0,8}"
            r"(?:하지\s*않|아닌|없)",
            question,
        )
        if buy_unavailable is not None:
            semantic_unsupported_spans.append(buy_unavailable.group(0))
        elif re.search(
            r"(?:현재\s*)?(?:매수|구매)(?:가|는|이)?\s*가능|"
            r"(?:매수|구매)\s*가능(?:한|인)?",
            question,
        ):
            # Organizer notice dated 2026-08-24 invalidates the quantity
            # column and declares every supplied non-delisted bond buyable.
            # Preserve that requested condition as an explicit constant slot
            # instead of silently dropping it from a compound plan.
            add("currently_buyable", True)
        if re.search(
            r"(?:모집\s*구분(?:이|은|가)?\s*)?공모|공개\s*모집(?:된|한)?",
            question,
        ):
            add("public_offering", True)
        if re.search(r"(?:모집\s*구분(?:이|은|가)?\s*)?사모", question):
            add("public_offering", False)
        if re.search(r"(?:퇴직\s*)?연금.{0,16}(?:편입|거래).{0,8}(?:가능|Y)", question):
            add("pension_eligible", True)
        if re.search(
            r"(?:퇴직\s*)?연금(?:에|으로|계좌에)?\s*"
            r"(?:담|편입).{0,12}?(?:수\s*있|가능)",
            question,
        ):
            add("pension_eligible", True)
        if re.search(r"(?:퇴직\s*)?연금.{0,16}(?:편입|거래).{0,8}(?:불가|N)", question):
            add("pension_eligible", False)
        if re.search(r"온라인.{0,20}오프라인.{0,24}(?:모두|겸용|가능)", question):
            add("sales_channel", "온오프 겸용")
        for risk in (
            "매우높은위험(1등급)",
            "높은위험(2등급)",
            "다소높은위험(3등급)",
            "보통위험(4등급)",
            "낮은위험(5등급)",
            "매우낮은위험(6등급)",
        ):
            if risk in question or risk.replace("(", " ").replace(")", "") in question:
                add("risk_level", risk)
        # The organizer's 2026-08-24 rule invalidates BUYABLE_QUANTITY and
        # treats every provided, non-delisted bond as buyable.  The supplied
        # schema has no delisting field, so positive buyability wording is an
        # understood scope phrase, not a row-level filter.
        lookup_patterns = [
            (
                r"([0-9A-Za-z가-힣&·+._ -]{2,60}?)(?:가|이)\s*발행한\s*(?:국내\s*)?채권",
                "issuer",
                # Legal suffixes such as ``(주)`` are present in the official
                # source even when users naturally omit them.  A bounded
                # issuer substring is therefore safer and more accurate than
                # widening the request after an exact-string miss.
                "contains",
            ),
            (
                r"발행(?:기관|사|자)(?:가|는|은|이)?\s*"
                r"([0-9A-Za-z가-힣㈜()·&._-]{2,60}?)\s*"
                r"(?:으로|로)\s*(?:잡힌|표시(?:된|되어)?|기재(?:된|되어)?|등록(?:된|되어)?)",
                "issuer",
                "contains",
            ),
            (r"발행(?:기관|사|자)에\s*(.+?)(?:가|이)?\s*포함", "issuer", "contains"),
            (r"발행(?:기관|사|자)\s*(.+?)의", "issuer", "contains"),
            (r"상품명에\s*(.+?)(?:가|이)?\s*포함", "product_name", "contains"),
            (
                r"종목코드(?:가|는|은|이)?\s*[:：]?\s*([A-Z0-9]+)(?:인|입니다|$|\s)",
                "ticker",
                "eq",
            ),
            (
                r"상품번호(?:가|는|은|이)?\s*[:：]?\s*([A-Z0-9]+)(?:인|입니다|$|\s)",
                "product_id",
                "eq",
            ),
            (
                r"(?:신용\s*등급(?:이|은|가)?\s*(?:정확히|정확하게|딱)?\s*"
                r"(AAA|AA\+|AA0|AA-|A\+|A0|A-|BBB\+|BBB0|BBB-|"
                r"BB\+|BB0|BB-|B\+|B0|B-|CCC|CC0|C0|C)(?:인|이고|이며|$|\s)|"
                r"(?<![A-Z])(AAA|AA\+|AA0|AA-|A\+|A0|A-|BBB\+|BBB0|BBB-|"
                r"BB\+|BB0|BB-|B\+|B0|B-|CCC|CC0|C0|C)\s*등급|"
                r"(?<![A-Z])(AAA|AA\+|AA0|AA-|A\+|A0|A-|BBB\+|BBB0|BBB-|"
                r"BB\+|BB0|BB-|B\+|B0|B-|CCC|CC0|C0|C)(?:가|이)?\s*적용된)",
                "credit_rating",
                "eq",
            ),
        ]
        for pattern, field, operator in lookup_patterns:
            match = re.search(pattern, question)
            if match:
                value = next(group for group in match.groups() if group is not None).strip()
                if negated_value := _negated_literal_span(question, value):
                    semantic_ambiguity_spans.append(negated_value)
                else:
                    add(field, value, operator)
        excluded_issuers: list[str] = []
        for pattern in (
            r"발행(?:기관|사|자)(?:가|는|은|이)?\s*"
            r"(?P<issuer>[0-9A-Za-z가-힣㈜()·&._ -]{2,60}?)\s*"
            r"(?:이|가)?\s*아닌",
            r"(?P<issuer>[0-9A-Za-z가-힣㈜()·&._ -]{2,60}?)(?:가|이)\s*"
            r"발행한\s*(?:국내\s*)?(?:채권|상품|종목).{0,20}?"
            r"(?:제외|빼고|말고)",
            r"(?:^|[,.;!?]\s*|\s중\s+)"
            r"(?P<issuer>[0-9A-Za-z가-힣㈜()·&._-]{2,40})\s*"
            r"발행분(?:이|가|은|는|을|를)?\s*(?:제외|빼)",
        ):
            for match in re.finditer(pattern, question, flags=re.IGNORECASE):
                issuer = _literal_actor_value(match.group("issuer"))
                if issuer and issuer not in excluded_issuers:
                    excluded_issuers.append(issuer)
        if excluded_issuers:
            required[:] = [item for item in required if item["field"] != "issuer"]
            for issuer in excluded_issuers:
                add("issuer", issuer, "neq")
        connected_ratings = re.findall(
            r"(?<![A-Z])(AAA|AA\+|AA0|AA-|A\+|A0|A-|BBB\+|BBB0|BBB-|"
            r"BB\+|BB0|BB-|B\+|B0|B-|CCC|CC0|C0|C)(?![A-Z])",
            question,
            flags=re.IGNORECASE,
        )
        if len(set(connected_ratings)) >= 2 and re.search(r"(?:또는|이나|나|혹은|,)", question):
            required[:] = [item for item in required if item["field"] != "credit_rating"]
            for rating in dict.fromkeys(value.upper() for value in connected_ratings):
                add("credit_rating", rating, "eq")
        elif bare_rating := re.search(
            r"(?<![A-Z])(AAA|AA\+|AA0|AA-|A\+|A0|A-|BBB\+|BBB0|BBB-|"
            r"BB\+|BB0|BB-|B\+|B0|B-|CCC|CC0|C0|C)\s*채권",
            question,
            flags=re.IGNORECASE,
        ):
            required[:] = [item for item in required if item["field"] != "credit_rating"]
            add("credit_rating", bare_rating.group(1).upper(), "eq")
        elif condition_rating := re.search(
            r"(?<![A-Z])(AAA|AA\+|AA0|AA-|A\+|A0|A-|BBB\+|BBB0|BBB-|"
            r"BB\+|BB0|BB-|B\+|B0|B-|CCC|CC0|C0|C)\s*"
            r"(?:조건|기준)(?:을|를|이|가|은|는)?",
            question,
            flags=re.IGNORECASE,
        ):
            required[:] = [item for item in required if item["field"] != "credit_rating"]
            add("credit_rating", condition_rating.group(1).upper(), "eq")
        remaining_year_range = re.search(
            r"(?:잔여|잔존)\s*기간(?:이|은|가|을|를)?\s*"
            r"(\d+)\s*년\s*이상\s*(\d+)\s*년\s*이하",
            question,
        )
        if remaining_year_range is not None:
            lower, upper = map(int, remaining_year_range.groups())
            add("remaining_days", [lower * 365, upper * 365], "between")
        natural_remaining_range = re.search(
            r"(?:만기까지\s*)?(\d+)\s*(년|개월|일)\s*(?:이상|부터)\s*"
            r"(\d+)\s*(년|개월|일)\s*(?:이하|까지)\s*(?:남(?:은|아)|잔존)",
            question,
        )
        if natural_remaining_range is not None:
            lower = int(natural_remaining_range.group(1)) * _MATURITY_UNIT_DAYS[
                natural_remaining_range.group(2)
            ]
            upper = int(natural_remaining_range.group(3)) * _MATURITY_UNIT_DAYS[
                natural_remaining_range.group(4)
            ]
            if lower <= upper:
                add("remaining_days", [lower, upper], "between")
            else:
                semantic_ambiguity_spans.append(natural_remaining_range.group(0))
        direct_remaining_bound = re.search(
            r"(?:(?:만기까지\s*)?(?:잔여|잔존|남은)\s*기간)"
            r"(?:이|은|가|을|를)?\s*"
            r"(\d+)\s*(년|개월|일)\s*(이하|미만|이내|이상|초과)",
            question,
        )
        if direct_remaining_bound is not None:
            amount = int(direct_remaining_bound.group(1))
            days = amount * _MATURITY_UNIT_DAYS[direct_remaining_bound.group(2)]
            boundary = direct_remaining_bound.group(3)
            if boundary in {"이하", "이내"}:
                add("remaining_days", [0, days], "between")
            elif boundary == "미만":
                add("remaining_days", [0, max(0, days - 1)], "between")
            elif boundary == "이상":
                add("remaining_days", days, "gte")
            else:
                add("remaining_days", days, "gt")
        maturity_within_years = re.search(
            r"(\d+)\s*년\s*(?:안|이내)에?.{0,24}?(?:만기|상환)|"
            r"(?:만기|상환).{0,24}?(\d+)\s*년\s*(?:안|이내)",
            question,
        )
        if maturity_within_years is not None:
            years = next(int(value) for value in maturity_within_years.groups() if value)
            add("remaining_days", years * 365, "lte")
        maturity_bounded = re.search(
            r"(?:잔존\s*)?만기(?:일)?(?:가|는|를|까지|는\s*기간)?\s*(\d+)\s*"
            r"(년|개월|일)\s*(이하|미만|이내|이상|초과)",
            question,
        )
        if maturity_bounded:
            days = int(maturity_bounded.group(1)) * _MATURITY_UNIT_DAYS[maturity_bounded.group(2)]
            boundary = maturity_bounded.group(3)
            if boundary in {"이하", "이내"}:
                add("remaining_days", [0, days], "between")
            elif boundary == "미만":
                add("remaining_days", [0, max(0, days - 1)], "between")
            elif boundary == "이상":
                add("remaining_days", days, "gte")
            else:
                add("remaining_days", days, "gt")
        # "만기가 1년도 안 남은" states the same bound as "만기 1년 미만" but was
        # previously dropped, so the whole maturity condition disappeared from the
        # plan and every bond was returned as a candidate.
        elif maturity_remaining := re.search(
            r"(?:만기|잔존)[^.?!]{0,16}?(\d+)\s*(년|개월|일)\s*(?:도|은|는)?\s*"
            r"(?:안|못)\s*(?:남|되)",
            question,
        ):
            days = (
                int(maturity_remaining.group(1)) * _MATURITY_UNIT_DAYS[maturity_remaining.group(2)]
            )
            add("remaining_days", [0, max(0, days - 1)], "between")
        if remaining_days := re.search(
            r"(\d+)\s*일\s*(?:안|이내)에?.{0,24}?(?:만기|상환)",
            question,
        ):
            add("remaining_days", int(remaining_days.group(1)), "lte")
        if re.search(r"(?:앞으로\s*)?(?:반년|6\s*개월)\s*안(?:에)?", question):
            add("remaining_days", 180, "lte")
        elif re.search(
            r"(?:앞으로\s*)?(?:1\s*년|일\s*년)\s*(?:안|내)(?:에)?"
            r"(?:.{0,16}?(?:만기|상환))?",
            question,
        ):
            add("remaining_days", 365, "lte")
        elif re.search(r"만기까지\s*2\s*년(?:을)?\s*넘기지\s*않", question):
            add("remaining_days", 730, "lte")
        rating_group = re.search(
            r"(?<![A-Z])(AA|A|BBB|BB|B)\s*급(?:\s*\(([^)]*)\))?",
            question,
            flags=re.IGNORECASE,
        )
        if rating_group is not None:
            explicit_values = re.findall(
                r"AAA|AA\+|AA0|AA-|A\+|A0|A-|BBB\+|BBB0|BBB-|"
                r"BB\+|BB0|BB-|B\+|B0|B-",
                rating_group.group(2) or "",
                flags=re.IGNORECASE,
            )
            defaults = {
                "AA": ["AA+", "AA0", "AA-"],
                "A": ["A+", "A0", "A-"],
                "BBB": ["BBB+", "BBB0", "BBB-"],
                "BB": ["BB+", "BB0", "BB-"],
                "B": ["B+", "B0", "B-"],
            }
            required[:] = [item for item in required if item["field"] != "credit_rating"]
            add(
                "credit_rating",
                [value.upper() for value in explicit_values]
                or defaults[rating_group.group(1).upper()],
                "in",
            )
    if family == "overseas_etp":
        explicit_text_fields = (
            (
                "base_index",
                r"(?:기초\s*지수|추종\s*지수|benchmark|base\s*index)",
            ),
            (
                "strategy_description",
                r"(?:해외\s*ETP\s*)?운용\s*전략(?:\s*설명)?|"
                r"투자\s*전략\s*설명|investment\s*strategy",
            ),
        )
        replication_field_surface = r"(?:지수|인덱스)?\s*복제\s*(?:방법|방식)"
        for _, field_surface in (
            *explicit_text_fields,
            ("index_replication_method", replication_field_surface),
        ):
            negated_explicit_field = re.search(
                rf"(?:{field_surface}).{{0,240}}?"
                r"(?:아닌|아니며|포함(?:되지|하지)\s*않|"
                r"제외(?:한|하고|하며)?|말고|빼고)",
                question,
                flags=re.IGNORECASE,
            )
            if negated_explicit_field is not None:
                semantic_ambiguity_spans.append(negated_explicit_field.group(0))
        base_index_placeholders = {
            "index is not provided by management company",
            "index is not available on lipper database",
        }
        for field_id, field_surface in explicit_text_fields:
            explicit_text = _explicit_labeled_text_match(
                question,
                field_surface=field_surface,
            )
            if explicit_text is None:
                continue
            match, value, operator = explicit_text
            if field_id == "base_index" and value.casefold() in base_index_placeholders:
                semantic_ambiguity_spans.append(match.group(0))
            elif negated_value := _negated_literal_span(question, value):
                semantic_ambiguity_spans.append(negated_value)
            else:
                add(field_id, value, operator)

        reverse_base_index = re.search(
            rf"(?P<value>{_EXPLICIT_TEXT_VALUE})\s*(?:을|를)\s*"
            r"(?:기초\s*지수|추종\s*지수)(?:로|으로)\s*"
            r"(?:하는|삼는|사용하는)\s*(?:해외\s*)?(?:ETF|ETN|ETP)",
            question,
            flags=re.IGNORECASE,
        )
        if reverse_base_index is not None:
            value = re.sub(r"\s+", " ", reverse_base_index.group("value")).strip()
            value = re.split(r"(?:중에서|가운데|에서|중)\s*", value)[-1].strip()
            value = _literal_actor_value(value)
            if value.casefold() in base_index_placeholders:
                semantic_ambiguity_spans.append(reverse_base_index.group(0))
            elif negated_value := _negated_literal_span(question, value):
                semantic_ambiguity_spans.append(negated_value)
            else:
                add("base_index", value, "eq")

        explicit_replication = _explicit_labeled_text_match(
            question,
            field_surface=replication_field_surface,
        )
        if explicit_replication is not None and not any(
            item["field"] == "index_replication_method" for item in required
        ):
            match, value, operator = explicit_replication
            canonical = _official_enum_literal(
                "index_replication_method",
                family,
                value,
            )
            if operator == "eq" and canonical is not None:
                add("index_replication_method", canonical, "eq")
            else:
                # Replication method is a four-value official enum.  Unknown
                # or substring requests cannot be widened into an arbitrary
                # source-text predicate.
                semantic_ambiguity_spans.append(match.group(0))

        lookup_patterns = [
            (
                r"(?:종목코드|티커)(?:가|는|은|이)?\s*[:：]?\s*"
                r"([A-Z0-9._-]+)(?:인|의|입니다|$|\s)",
                "ticker",
            ),
            (
                r"상품번호(?:가|는|은|이)?\s*[:：]?\s*"
                r"((?:[A-Z]{2,5}|[0-9]{3}):[A-Z0-9._-]+)(?:인|의|입니다|$|\s)",
                "product_id",
            ),
        ]
        for pattern, field in lookup_patterns:
            match = re.search(pattern, question, flags=re.IGNORECASE)
            if match:
                value = match.group(1).strip()
                if negated_value := _negated_literal_span(question, value):
                    semantic_ambiguity_spans.append(negated_value)
                else:
                    add(field, value, "eq")
        manager = re.search(
            r"(.{1,80}?)\s*(?:가|이)\s*운용하는\s*(?:해외\s*)?(?:ETF|ETN)",
            question,
            flags=re.IGNORECASE,
        )
        if manager is not None:
            manager_value = _literal_actor_value(manager.group(1))
            if manager_value:
                required[:] = [item for item in required if item["field"] != "manager"]
                add("manager", manager_value, "contains")
        bare_manager = re.search(
            r"(?:^|[.!?。]\s*|(?:첫째|둘째|셋째|넷째),?\s*)"
            r"([0-9A-Za-z가-힣&·+._ -]{2,80}?)"
            r"(?:의\s+(?:해외\s*)?|\s+운용\s+(?:해외\s*)?)(?:ETF|ETN)",
            question,
            flags=re.IGNORECASE,
        )
        if bare_manager is not None:
            manager_value = _literal_actor_value(bare_manager.group(1))
            if manager_value and not _looks_like_schema_scope(manager_value) and (
                re.search(
                    r"(?:상품\s*(?:ID|아이디|번호)|종목\s*코드|티커)$",
                    manager_value,
                    flags=re.IGNORECASE,
                )
                is None
            ):
                required[:] = [item for item in required if item["field"] != "manager"]
                add("manager", manager_value, "contains")
        inverse_short_status = re.search(
            r"(?:인버스\s*(?:또는|·|/)?\s*숏|인버스·숏)\s*여부"
            r"(?:가|는|은|이)?\s*[:：]?\s*(Y|N|예|아니오)",
            question,
            flags=re.IGNORECASE,
        )
        if inverse_short_status is not None:
            add(
                "inverse_short",
                inverse_short_status.group(1).upper() in {"Y", "예"},
            )
        elif re.search(
            r"(?:인버스\s*(?:또는|·|/|이나|나)\s*숏|인버스·숏)(?:형)?(?:인|만|\s*상품)",
            question,
            flags=re.IGNORECASE,
        ):
            add("inverse_short", True)
        if re.search(r"지수\s*추적\s*여부.{0,12}(?:Y|예)", question):
            add("index_tracking", True)
        if explicit_fund_currency is not None:
            add("fund_currency", explicit_fund_currency.group(1).upper())
        listing_date = re.search(
            r"(?:설정일|상장일)(?:이|가)?\s*(\d{4})\s*년\s*(\d{1,2})\s*월\s*"
            r"(\d{1,2})\s*일\s*(이후|이상|초과|이전|이하|미만)",
            question,
        )
        if listing_date is not None:
            year, month, day, boundary = listing_date.groups()
            operator = {
                "이후": "gte",
                "이상": "gte",
                "초과": "gt",
                "이전": "lte",
                "이하": "lte",
                "미만": "lt",
            }[boundary]
            add("listing_date", f"{year}-{int(month):02d}-{int(day):02d}", operator)
        generic_listing_date = _date_hint(
            question,
            "listing_date",
            ("설정일", "상장일", "설정", "상장"),
        )
        if generic_listing_date is not None:
            add(
                generic_listing_date["field"],
                generic_listing_date["value"],
                generic_listing_date["operator"],
            )

    if family == "bond":
        numeric_fields = [
            (
                "issue_amount",
                ("발행잔액", "발행액", "발행 금액", "남은 발행 물량", "남아 있는 발행 물량"),
            ),
            ("total_issue_amount", ("총발행금액", "총 발행금액", "총 발행 규모", "총발행액")),
            (
                "coupon_rate_pct",
                (
                    "표면이율",
                    "표면이자율",
                    "이표율",
                    "쿠폰금리",
                    "표면금리",
                    "액면 쿠폰",
                    "쿠폰",
                ),
            ),
            (
                "buy_yield_pct",
                (
                    "매수수익률",
                    "매수 수익률",
                    "세전 매수수익률",
                    "세전 매수 수익률",
                    "지금 살 때 세전 수익률",
                    "살 때 세전 수익률",
                    "지금 샀을 때 세전 수익률",
                    "샀을 때 세전 수익률",
                ),
            ),
            ("after_tax_yield_pct", ("세후수익률", "세후 수익률")),
            (
                "applied_yield_pct",
                (
                    "민평수익률",
                    "민평 수익률",
                    "민평금리",
                    "민평 금리",
                    "민평 기준 수익률",
                    "민간평가 금리",
                    "평가기관 수익률",
                    "민평 산출 수익률",
                    "적용수익률",
                    "적용 수익률",
                    "적용금리",
                    "적용 금리",
                ),
            ),
            (
                "evaluation_price",
                (
                    "평가일단가",
                    "평가단가",
                    "평가가격",
                    "클린 평가가격",
                    "클린 프라이스",
                    "이자 제외 평가단가",
                    "이자 제외 평가가격",
                    "이자를 뺀 평가가격",
                ),
            ),
            (
                "dirty_price",
                ("이자부단가", "경과이자 포함 단가", "경과이자 포함 가격"),
            ),
            ("convexity", ("컨벡시티", "가격 굴곡")),
            (
                "remaining_days",
                (
                    "잔존",
                    "잔존일수",
                    "잔존일",
                    "잔존기간",
                    "잔존 기간",
                    "만기까지 남은 일수",
                    "만기까지 남은 기간",
                ),
            ),
            ("duration_years", ("듀레이션", "금리 민감도")),
        ]
    elif family == "fund":
        numeric_fields = [
            ("aum", ("AUM", "순자산", "운용 자산")),
            ("sale_company_fee_pct", ("판매회사보수", "판매 회사 보수", "판매보수")),
            (
                "management_company_fee_pct",
                ("집합투자업자보수", "집합 투자 업자 보수", "운용사보수"),
            ),
            ("trustee_fee_pct", ("신탁업자보수", "신탁 업자 보수", "수탁보수")),
            (
                "domestic_stock_ratio_pct",
                (
                    "국내주식 구성비율",
                    "국내주식 비중",
                    "국내 주식 구성 비율",
                    "국내 주식 비중",
                    "국내주식 편입비중",
                    "국내 주식 편입 비중",
                    "국내주식 편입비",
                    "국내 주식 편입비",
                    "국내주식 편입비율",
                    "국내 주식 편입비율",
                ),
            ),
            (
                "domestic_bond_ratio_pct",
                (
                    "국내채권 구성비율", "국내채권 비중", "국내 채권 구성 비율",
                    "국내 채권 비중", "국내채권 편입비", "국내 채권 편입비",
                    "국내채권 편입비중", "국내 채권 편입 비중",
                ),
            ),
            (
                "overseas_stock_ratio_pct",
                (
                    "해외주식 구성비율", "해외주식 비중", "해외 주식 구성 비율",
                    "해외 주식 비중", "해외주식 편입비", "해외 주식 편입비",
                    "해외주식 편입비중", "해외 주식 편입 비중",
                ),
            ),
            (
                "overseas_bond_ratio_pct",
                (
                    "해외채권 구성비율", "해외채권 비중", "해외 채권 구성 비율",
                    "해외 채권 비중", "해외채권 편입비", "해외 채권 편입비",
                    "해외채권 편입비중", "해외 채권 편입 비중",
                ),
            ),
            ("latest_distribution_pct", ("최근 분배율", "최신 분배율")),
            (
                "preservation_ratio_pct",
                (
                    "보전율",
                    "보전 비율",
                    "원본 보존 비율",
                    "원본보전 비율",
                    "원본보전 관련 비율",
                    "원본을 남겨 둔 비율",
                ),
            ),
            ("one_week_return_pct", ("1주 수익률", "일주일 수익률", "1주 성과")),
            (
                "one_month_return_pct",
                (
                    "1개월 수익률",
                    "한 달 수익률",
                    "1개월 성과",
                    "최근 1개월 성과",
                    "한 달 성과",
                    "한 달 성적",
                    "월간 성과",
                ),
            ),
            (
                "three_month_return_pct",
                ("3개월 수익률", "석 달 수익률", "3개월 성과", "석 달 성과", "석 달 성적"),
            ),
            ("six_month_return_pct", ("6개월 수익률", "반년 수익률", "6개월 성과")),
            ("one_year_return_pct", ("1년 수익률", "1년 성과", "1Y 수익률")),
            ("eighteen_month_return_pct", ("18개월 수익률", "18개월 성과", "18M 수익률")),
            ("two_year_return_pct", ("2년 수익률", "2년 성과", "2Y 수익률")),
            (
                "three_year_return_pct",
                ("3년 수익률", "3년 성과", "3년 누적 성과", "3Y 수익률"),
            ),
            ("five_year_return_pct", ("5년 수익률", "5년 성과", "5Y 수익률")),
        ]
    else:
        numeric_fields = [
            (
                "total_expense_ratio_pct",
                (
                    "총보수",
                    "보수",
                    "연간보수",
                    "연간보수율",
                    "보수율",
                    "경비율",
                    "관리비",
                    "운용비",
                    "운영비",
                ),
            ),
            (
                "aum",
                (
                    "AUM",
                    "순자산",
                    "자산규모",
                    "자산 규모",
                    "일간 순자산총액",
                    "최종 순자산",
                ),
            ),
            ("close_price", ("종가", "마감 가격")),
            ("daily_trading_value", ("일 거래대금", "거래대금", "외화거래대금")),
            (
                "daily_volume",
                ("일 거래량", "일거래량", "거래량", "일간 거래량", "하루 거래량"),
            ),
            (
                "discrepancy_pct",
                ("괴리율", "가격 괴리율", "NAV 괴리율", "NAV와 시장가격 괴리"),
            ),
        ]
    if family == "domestic_etp":
        numeric_fields.extend(
            [
                ("net_asset_value", ("최종 NAV", "NAV", "순자산가치", "최종 기준가")),
                (
                    "tracking_error_pct",
                    ("추적오차율", "추적 오차율", "추적 오차", "지수 추종 오차"),
                ),
                (
                    "twenty_day_volatility_pct",
                    (
                        "20거래일 연환산 변동성",
                        "20거래일 변동성",
                        "20일 변동성",
                        "1개월 변동성",
                        "한 달 변동성",
                        "최근 한 달 변동성",
                        "20거래일 흔들림",
                    ),
                ),
                (
                    "sixty_day_volatility_pct",
                    ("60거래일 변동성", "60일 변동성", "3개월 변동성", "60거래일 흔들림"),
                ),
                (
                    "one_hundred_twenty_day_volatility_pct",
                    ("120거래일 변동성", "120일 변동성", "6개월 변동성", "120거래일 흔들림"),
                ),
                (
                    "two_hundred_fifty_two_day_volatility_pct",
                    ("252거래일 변동성", "252일 변동성", "1년 변동성", "252거래일 흔들림"),
                ),
                (
                    "distribution_yield_pct",
                    ("분배수익률", "분배 수익률", "연환산 분배수익률"),
                ),
                (
                    "distribution_payments_per_year",
                    (
                        "연간 분배 횟수",
                        "연간 분배 지급횟수",
                        "연간 분배 지급 횟수",
                        "분배 지급 횟수",
                        "분배 횟수",
                    ),
                ),
                (
                    "one_day_return_pct",
                    (
                        "1일 수익률",
                        "하루 수익률",
                        "일간 수익률",
                        "당일 수익률",
                        "오늘 상승률",
                        "당일 상승률",
                        "하루 등락률",
                        "하루 등락",
                        "일간 등락률",
                        "일간 등락",
                        "오늘 성과",
                        "당일 성과",
                    ),
                ),
                (
                    "one_month_return_pct",
                    (
                        "1개월 수익률",
                        "한 달 수익률",
                        "한 달 성과",
                        "한 달 성적",
                        "한 달 성적표",
                        "월간 성과",
                        "1M 수익률",
                    ),
                ),
                (
                    "three_month_return_pct",
                    ("3개월 수익률", "석 달 수익률", "석 달 성과", "3M 수익률"),
                ),
                (
                    "six_month_return_pct",
                    ("6개월 수익률", "반년 수익률", "반년 수익", "6M 수익률"),
                ),
                ("one_year_return_pct", ("1년 수익률", "1Y 수익률")),
                (
                    "ytd_return_pct",
                    (
                        "YTD 수익률",
                        "연초 이후 수익률",
                        "연초 이후 상승률",
                        "연초대비 수익률",
                    ),
                ),
                (
                    "leverage_factor",
                    ("레버리지 배수", "레버리지배수", "레버리지", "배율"),
                ),
            ]
        )
    elif family == "overseas_etp":
        numeric_fields.extend(
            [
                (
                    "leverage_factor",
                    ("레버리지 배수", "레버리지배수", "레버리지", "배율"),
                ),
                (
                    "one_day_return_pct",
                    (
                        "1일 수익률",
                        "하루 수익률",
                        "당일 수익률",
                        "일간 수익률",
                        "오늘 상승률",
                        "당일 상승률",
                        "하루 등락률",
                        "하루 등락",
                        "일간 등락률",
                        "일간 등락",
                        "오늘 성과",
                        "당일 성과",
                    ),
                ),
                ("base_price", ("기준가", "펀드 기준가")),
            ]
        )
    if family == "domestic_etp":
        numeric_fields.append(
            (
                "one_month_average_trading_value",
                (
                    "1개월 평균 거래대금",
                    "한 달 평균 거래대금",
                    "월평균 거래대금",
                    "최근 1개월 평균 거래대금",
                    "최근 한 달 평균 거래대금",
                ),
            )
        )
    if family is not None:
        numeric_fields = _merge_registry_numeric_fields(numeric_fields, family)
    if family == "domestic_etp" and re.search(r"순\s*자산\s*총액", question):
        # 국내 ETP에는 최근 AUM(du_last_aum)과 공식 순자산총액
        # (pd_net_tamt)이 별도 field다. 긴 명시 표현을 짧은 '순자산'
        # alias가 가로채지 못하게 clause-local로 분리한다.
        numeric_fields = [
            (
                field,
                tuple(alias for alias in aliases if alias != "순자산"),
            )
            for field, aliases in numeric_fields
        ]
        numeric_fields.insert(
            0,
            ("net_asset_total", ("순자산총액", "순 자산 총액", "국내 ETF 순자산총액")),
        )
    owned_numeric_mentions = (
        registry_field_mentions(
            question,
            family,
            "queryable",
            value_types=frozenset({ValueType.NUMBER}),
        )
        if family is not None
        else ()
    )
    for field, aliases in numeric_fields:
        # A longer official field label owns its entire span.  This prevents
        # ``순자산총액`` from also becoming AUM and ``120거래일`` from also
        # becoming the shorter 20-day field.  Curated paraphrases still work
        # when no explicit registry label is present.
        alias_occurrences = tuple(
            re.finditer(_flexible_alias_pattern(aliases), question, flags=re.IGNORECASE)
        )
        shadowed_by_longer_field = bool(alias_occurrences) and all(
            any(
                mention.field_id != field
                and mention.start <= occurrence.start()
                and occurrence.end() <= mention.end
                for mention in owned_numeric_mentions
            )
            for occurrence in alias_occurrences
        )
        if shadowed_by_longer_field:
            continue
        if excluded_hint := _excluded_numeric_hint(question, field, aliases):
            add(
                excluded_hint["field"],
                excluded_hint["value"],
                excluded_hint["operator"],
            )
            continue
        if negated_comparison := _negated_comparison_span(question, aliases):
            semantic_ambiguity_spans.append(negated_comparison)
            continue
        hint = _numeric_hint(
            question,
            field,
            aliases,
            owned_field_mentions=owned_numeric_mentions,
        )
        if hint is not None:
            add(hint["field"], hint["value"], hint["operator"])
        elif any(
            item["field"] == field
            and item["operator"] in {"is_present", "is_missing"}
            for item in required
        ):
            continue
        elif presence_hint := _numeric_presence_hint(question, field, aliases):
            add(
                presence_hint["field"],
                presence_hint["value"],
                presence_hint["operator"],
            )
    if family is not None:
        coordinated_constraints, coordinated_any_groups = _coordinated_numeric_constraints(
            question,
            numeric_fields,
            owned_numeric_mentions,
        )
        for constraint in coordinated_constraints:
            add(
                constraint["field"],
                constraint["value"],
                constraint["operator"],
            )
        any_constraint_groups.extend(coordinated_any_groups)
    if family == "domestic_etp" and (
        payment_count := re.search(
            r"(?:연(?:간)?\s*)?(?:분배\s*)?(?:지급\s*)?횟수(?:가|는|은|이)?\s*"
            r"(?P<count_a>\d+|한|하나|두|둘|세|셋|네|넷|다섯|여섯|일곱|여덟|아홉|열)"
            r"\s*(?:번|회)?\s*(?P<boundary_a>이상|초과|이하|미만)?|"
            r"(?:분배금을?\s*)?(?:해마다|매년|연간|연)\s*"
            r"(?P<count_b>\d+|한|하나|두|둘|세|셋|네|넷|다섯|여섯|일곱|여덟|아홉|열)"
            r"\s*(?:번|회)\s*(?P<boundary_b>이상|초과|이하|미만)?\s*(?:분배|지급)",
            question,
        )
    ):
        count_text = payment_count.group("count_a") or payment_count.group("count_b")
        boundary = payment_count.group("boundary_a") or payment_count.group("boundary_b")
        add(
            "distribution_payments_per_year",
            _small_count(count_text),
            {None: "eq", "이상": "gte", "초과": "gt", "이하": "lte", "미만": "lt"}[
                boundary
            ],
        )
    if family == "fund" and re.search(
        r"(?:3개월|석 달)\s*(?:수익률|수익|성과).{0,12}마이너스가 아닌",
        question,
    ):
        add("three_month_return_pct", 0, "gte")
    if family == "domestic_etp" and (
        ytd_gain := re.search(
            r"(?:YTD|연초\s*이후|올해\s*들어)\s*"
            rf"({SCALED_NUMBER})\s*(?:%|퍼센트)\s*"
            r"(?:넘게|이상|초과)\s*(?:오른|상승(?:한|한\s*것))",
            question,
            flags=re.IGNORECASE,
        )
    ):
        add("ytd_return_pct", _scaled_number(ytd_gain.group(1)), "gt")
    if family == "bond":
        for field, aliases in [
            ("issue_date", ("발행일", "발행")),
            ("maturity_date", ("만기일", "만기", "상환일", "상환")),
        ]:
            if negated_comparison := _negated_comparison_span(question, aliases):
                semantic_ambiguity_spans.append(negated_comparison)
                continue
            hint = _date_hint(question, field, aliases)
            if hint is not None:
                add(hint["field"], hint["value"], hint["operator"])
        issued_since_year = re.search(
            r"(\d{4})\s*년\s*(?:이후|부터)\s*발행(?:된|한)?",
            question,
        )
        if issued_since_year is not None:
            add("issue_date", f"{issued_since_year.group(1)}-01-01", "gte")
        maturity_year_end = re.search(
            r"(?:상환|만기)(?:일자|일)?(?:가|는|은|이)?\s*"
            r"(\d{4})\s*년\s*말(?:까지|이전|이하)",
            question,
        )
        if maturity_year_end is not None:
            add("maturity_date", f"{maturity_year_end.group(1)}-12-31", "lte")
        maturity_year_span = re.search(
            r"(?:상환|만기)(?:일자|일)?(?:가|는|은|이)?\s*"
            r"(?P<from_year>\d{4})\s*(?:~|∼|～)\s*"
            r"(?P<to_year>\d{4})\s*년",
            question,
        )
        if maturity_year_span is not None:
            from_year = int(maturity_year_span.group("from_year"))
            to_year = int(maturity_year_span.group("to_year"))
            if from_year <= to_year:
                add(
                    "maturity_date",
                    [f"{from_year}-01-01", f"{to_year}-12-31"],
                    "between",
                )
            else:
                semantic_ambiguity_spans.append(maturity_year_span.group(0))
        remaining_years = re.search(
            r"(?:(?:상환|만기)(?:까지)?\s*|(?:잔여|잔존)\s*기간(?:이|은|가)?\s*)"
            r"(\d+)\s*년\s*(이상|초과|이하|미만)(?:\s*남(?:은|아|은\s*것))?",
            question,
        )
        if remaining_years is not None:
            operator = {
                "이상": "gte",
                "초과": "gt",
                "이하": "lte",
                "미만": "lt",
            }[remaining_years.group(2)]
            add("remaining_days", int(remaining_years.group(1)) * 365, operator)

    if family is not None:
        dynamic_date = _date_hint(
            question,
            "dynamic_as_of",
            ("동적 지표 기준일", "동적 기준일", "AUM 업데이트일", "최신 기준일"),
        )
        if dynamic_date is not None:
            add(
                dynamic_date["field"],
                dynamic_date["value"],
                dynamic_date["operator"],
            )

    ordered_risk_grade = re.search(
        r"위험\s*(?:등급|단계)(?:이|가|은|는)?\s*([1-6])\s*(?:등급)?\s*"
        r"(이상|이하|초과|미만)",
        question,
        flags=re.IGNORECASE,
    )
    if family in {"bond", "domestic_etp", "fund"} and ordered_risk_grade is not None:
        threshold = int(ordered_risk_grade.group(1))
        boundary = ordered_risk_grade.group(2)
        if boundary == "이하":
            accepted_grades = set(range(1, threshold + 1))
        elif boundary == "미만":
            accepted_grades = set(range(1, threshold))
        elif boundary == "이상":
            accepted_grades = set(range(threshold, 7))
        else:
            accepted_grades = set(range(threshold + 1, 7))
        official_values = load_field_registry().require_field(
            "risk_level", [family]
        ).enum_values
        selected_values = [
            value
            for value in official_values
            if _risk_grade_number(value) in accepted_grades
        ]
        required[:] = [item for item in required if item["field"] != "risk_level"]
        if selected_values:
            add("risk_level", selected_values, "in")
        else:  # pragma: no cover - registry invariant guard
            semantic_ambiguity_spans.append(ordered_risk_grade.group(0))

    exact_risk_grade = re.search(
        r"위험\s*(?:등급|단계)?(?:이|가|은|는)?\s*([1-6])\s*등급"
        r"(?!\s*(?:이상|이하|초과|미만))",
        question,
        flags=re.IGNORECASE,
    )
    if (
        family in {"bond", "domestic_etp", "fund"}
        and ordered_risk_grade is None
        and exact_risk_grade is not None
        and not any(item["field"] == "risk_level" for item in required)
    ):
        grade = int(exact_risk_grade.group(1))
        official_values = load_field_registry().require_field(
            "risk_level", [family]
        ).enum_values
        selected = next(
            (value for value in official_values if _risk_grade_number(value) == grade),
            None,
        )
        required[:] = [item for item in required if item["field"] != "risk_level"]
        if selected is not None:
            add("risk_level", selected, "eq")
        else:  # pragma: no cover - registry invariant guard
            semantic_ambiguity_spans.append(exact_risk_grade.group(0))

    unsupported_patterns = [
        "배당수익률",
        "원화로 환산",
        "가격 전망",
    ]
    if family == "fund":
        unsupported_patterns.extend(
            [
                "운용사 이름",
                "오늘 기준 최신 수익률",
                "총보수",
                "판매수수료",
                "대표 펀드",
                "클래스는 합쳐서",
            ]
        )
    elif family == "bond":
        unsupported_patterns.extend(["1일 수익률", "3개월 수익률", "국내 ETF"])
    elif family == "overseas_etp":
        unsupported_patterns.extend(["3개월 수익률", "국내 ETF"])
    if family == "bond":
        unsupported_patterns.extend(["AUM", "총보수", "거래정지"])
    ambiguity_patterns = ["적당한", "안전한", "괜찮은"]
    for match in re.finditer(
        r"(?:AUM|순자산|운용\s*자산|총보수|보수|수익률|거래대금|종가|"
        r"잔존\s*(?:일수|일|기일)|듀레이션|발행잔액|매수\s*수익률)"
        r".{0,14}(?:크|높|낮|작|적|짧|길)지\s*않",
        question,
        flags=re.IGNORECASE,
    ):
        semantic_ambiguity_spans.append(match.group(0))

    rankings: list[dict[str, str]] = []
    ascending = any(
        phrase in question
        for phrase in (
            "오름차순",
            "하위",
            "낮은 순",
            "낮은 쪽부터",
            "적은 순",
            "적은 쪽부터",
            "가장 적은",
            "싼 순",
            "싸게",
            "가벼운 순",
            "작은 순",
            "작은 쪽부터",
            "짧은 순",
            "짧은 쪽부터",
            "빠른 순",
            "가까운 순",
            "오래된 순",
            "lowest-to-highest",
            "ascending",
            "가나다순",
            "알파벳순",
            "이름순",
            "앞선 순서",
        )
    )
    descending = (
        any(
            phrase in question
            for phrase in (
                "내림차순",
                "큰 순",
                "높은 순",
                "최신",
                "상위",
                "좋은",
                "성과순",
                "앞선",
                "가장 많이",
                "제일 큰",
                "높은 쪽부터",
                "큰 쪽부터",
                "긴 순",
                "highest-to-lowest",
                "descending",
                "역순",
            )
        )
        or re.search(r"\d+\s*(?:등(?!급)|위(?!험))", question) is not None
    )
    explicit_rank_patterns: list[tuple[str, str]] = []
    period_rank_fields = (
        (r"(?:1\s*일|하루|당일|일간|1D)", "one_day_return_pct"),
        (r"(?:1\s*개월|한\s*달|30\s*일|1M)", "one_month_return_pct"),
        (r"(?:3\s*개월|석\s*달|90\s*일|3M)", "three_month_return_pct"),
        (r"(?:6\s*개월|반년|6M)", "six_month_return_pct"),
        (r"(?:1\s*년|일\s*년|12\s*개월|1Y)", "one_year_return_pct"),
        (r"(?:2\s*년|2Y)", "two_year_return_pct"),
        (r"(?:3\s*년|3Y)", "three_year_return_pct"),
        (r"(?:5\s*년|5Y)", "five_year_return_pct"),
        (r"(?:YTD|연초\s*이후|올해\s*들어)", "ytd_return_pct"),
    )
    period_rank_gap = (
        80
        if re.search(
            r"통합\s*(?:비교|정렬|순위)|직접\s*비교|한\s*줄\s*순위|"
            r"(?:한\s*순위에\s*)?합쳐|따로\s*나누지\s*말고",
            question,
            re.IGNORECASE,
        )
        else 24
    )
    for period, field_id in period_rank_fields:
        if re.search(
            rf"{period}\s*(?:수익률|수익|성과|성적(?:표)?).{{0,{period_rank_gap}}}"
            r"(?:앞선|우수한|높은|낮은|상위|하위|좋은|순)",
            question,
            flags=re.IGNORECASE,
        ):
            explicit_rank_patterns.append(
                (
                    rf"{period}\s*(?:수익률|수익|성과|성적(?:표)?).{{0,{period_rank_gap}}}"
                    r"(?:앞선|우수한|높은|낮은|상위|하위|좋은|순)",
                    field_id,
                )
            )
    if family == "fund":
        explicit_rank_patterns.append(
            (r"짧은 이름.{0,30}(?:순서|순으로|오름차순|내림차순|이름순)", "short_name")
        )
    explicit_rank_patterns.extend(
        [
            (
                r"(?:상품명|종목명|펀드명|ETF\s*명|ETN\s*명|채권명|이름).{0,20}"
                r"(?:순서|순으로|오름차순|내림차순|이름순|가나다순|"
                r"알파벳순|역순|앞선\s*순서|기준\s*(?:앞쪽|뒤쪽))",
                "product_name",
            ),
            (r"(?:티커|종목코드).{0,20}(?:순서|순으로|오름차순|내림차순)", "ticker"),
        ]
    )
    if family == "bond":
        explicit_rank_patterns.extend(
            [
                (
                    r"(?:세후\s*매수\s*수익률|세후수익률|세후 수익률)"
                    r".{0,30}(?:큰|작은|높은|낮은|순)",
                    "after_tax_yield_pct",
                ),
                (
                    r"(?<!세후)(?<!세후\s)(?:매수수익률|매수 수익률).{0,30}"
                    r"(?:큰|작은|높은|낮은|순)",
                    "buy_yield_pct",
                ),
                (
                    r"(?:민평\s*(?:(?:기준\s*)?수익률|금리)|적용\s*수익률|"
                    r"민간평가\s*금리|평가기관\s*수익률|"
                    r"민평에서\s*(?:계산|산출)(?:된|한)\s*(?:수익률|금리)).{0,30}"
                    r"(?:센|큰|작은|높은|낮은|상위|하위|순)",
                    "applied_yield_pct",
                ),
                (
                    r"(?:평가일단가|평가\s*(?:단가|가격)|클린\s*프라이스|"
                    r"이자(?:를)?\s*(?:제외|뺀)\s*평가(?:단가|가격)).{0,30}"
                    r"(?:큰|작은|높은|낮은|상위|하위|오름차순|내림차순|순)",
                    "evaluation_price",
                ),
                (
                    r"(?:이자부\s*(?:단가|가격)|경과이자(?:가|를)?\s*"
                    r"포함(?:된|한)?\s*(?:단가|가격)).{0,30}"
                    r"(?:큰|작은|높은|낮은|상위|하위|순)",
                    "dirty_price",
                ),
                (
                    r"(?:컨벡시티|가격\s*굴곡).{0,30}"
                    r"(?:큰|작은|높은|낮은|상위|하위|순)",
                    "convexity",
                ),
                (
                    r"(?:표면\s*(?:이자율|이율|금리)|쿠폰(?:\s*금리)?|액면\s*쿠폰|"
                    r"액면에?\s*붙은\s*이자율)"
                    r".{0,30}(?:큰|작은|높은|낮은|순)",
                    "coupon_rate_pct",
                ),
                (
                    r"(?:발행잔액|(?<!총)\s?발행\s*금액|(?<!총)\s?발행액|"
                    r"남은\s*발행\s*물량).{0,30}"
                    r"(?:큰|작은|높은|낮은|많은|적은|상위|하위|순)",
                    "issue_amount",
                ),
                (
                    r"(?:총발행금액|총\s*발행금액|총\s*발행\s*규모|총발행액).{0,30}"
                    r"(?:큰|작은|높은|낮은|많은|적은|순)",
                    "total_issue_amount",
                ),
                (
                    r"(?:잔존\s*(?:일수|일|기일|기간)|잔여\s*(?:일|기간)|"
                    r"(?:상환|만기)까지\s*남은\s*(?:일수|날짜|날|기간)|남은\s*기간).{0,30}"
                    r"(?:큰|작은|높은|낮은|많은|적은|긴|짧은|순)",
                    "remaining_days",
                ),
                (
                    r"(?:듀레이션|duration|금리\s*민감도).{0,30}"
                    r"(?:큰|작은|높은|낮은|긴|짧은|순|ascending|descending)",
                    "duration_years",
                ),
                (
                    r"만기(?!일)(?:가|까지의 기간)?.{0,30}"
                    r"(?:긴|짧은|많은|적은|빠른|늦은)",
                    "remaining_days",
                ),
                (
                    r"(?:상환|만기)(?!\s*시점).{0,30}?"
                    r"(?:먼저|빨리|가까운\s*순|가장\s*먼저)",
                    "remaining_days",
                ),
                (
                    r"만기일.{0,30}(?:긴|짧은|빠른|늦은|먼저|순|최신)",
                    "maturity_date",
                ),
                (
                    r"(?:상환|만기)(?:일자|일)?(?:가|는|은|이)?\s*"
                    r"(?:늦게\s*(?:오|도래)|나중에\s*(?:오|도래)|"
                    r"뒤에\s*(?:오|도래)).{0,20}?"
                    r"(?:순|정렬|골라|보여|찾|\d+\s*(?:개|종목|상품))",
                    "maturity_date",
                ),
            ]
        )
    elif family == "fund":
        explicit_rank_patterns.extend(
            [
                (
                    r"(?:판매회사보수|판매\s*회사\s*보수|판매보수).{0,24}"
                    r"(?:큰|작은|높은|낮은|싼|저렴한|가벼운|상위|하위|순)",
                    "sale_company_fee_pct",
                ),
                (
                    r"(?:신탁업자보수|신탁\s*업자\s*보수|수탁보수).{0,24}"
                    r"(?:큰|작은|높은|낮은|싼|저렴한|가벼운|상위|하위|순)",
                    "trustee_fee_pct",
                ),
                (
                    r"(?:집합투자업자보수|집합\s*투자\s*업자\s*보수|운용사보수)"
                    r".{0,24}(?:큰|작은|높은|낮은|상위|하위|순)",
                    "management_company_fee_pct",
                ),
                (
                    r"(?:국내\s*주식\s*(?:구성\s*비율|비중|편입\s*(?:비|비중|비율))).{0,24}"
                    r"(?:큰|작은|높은|낮은|많은|적은|상위|하위|순)",
                    "domestic_stock_ratio_pct",
                ),
                (
                    r"(?:국내\s*채권\s*(?:구성\s*비율|비중|편입\s*(?:비|비중|비율))).{0,24}"
                    r"(?:큰|작은|높은|낮은|많은|적은|상위|하위|순)",
                    "domestic_bond_ratio_pct",
                ),
                (
                    r"(?:해외\s*주식\s*(?:구성\s*비율|비중|편입\s*(?:비|비중|비율))).{0,24}"
                    r"(?:큰|작은|높은|낮은|많은|적은|상위|하위|순)",
                    "overseas_stock_ratio_pct",
                ),
                (
                    r"(?:해외\s*채권\s*(?:구성\s*비율|비중|편입\s*(?:비|비중|비율))).{0,24}"
                    r"(?:큰|작은|높은|낮은|많은|적은|상위|하위|순)",
                    "overseas_bond_ratio_pct",
                ),
                (
                    r"(?:최근|최신)\s*분배율.{0,24}"
                    r"(?:큰|작은|높은|낮은|상위|하위|순)",
                    "latest_distribution_pct",
                ),
                (
                    r"(?:보전율|보전\s*비율|원본\s*보존\s*비율|"
                    r"원본(?:을)?\s*남겨\s*둔\s*비율).{0,24}"
                    r"(?:큰|작은|높은|낮은|상위|하위|순)",
                    "preservation_ratio_pct",
                ),
                (
                    r"(?:1주|일주일)\s*(?:수익률|수익|성과).{0,120}"
                    r"(?:큰|작은|높은|낮은|좋은|상위|하위|성과순|순)",
                    "one_week_return_pct",
                ),
                (
                    r"(?:1개월|한 달)\s*(?:수익률|수익|성과|성적(?:표)?).{0,120}"
                    r"(?:큰|작은|높은|낮은|좋은|상위|하위|성과순|순)",
                    "one_month_return_pct",
                ),
                (
                    r"(?:3개월|3M|석 달)\s*(?:수익률|수익|성과|성적(?:표)?|return).{0,120}"
                    r"(?:큰|작은|높은|낮은|좋은|상위|하위|성과순|순)",
                    "three_month_return_pct",
                ),
                (
                    r"(?:6개월|반년)\s*(?:수익률|수익|성과).{0,120}"
                    r"(?:큰|작은|높은|낮은|좋은|상위|하위|성과순|순)",
                    "six_month_return_pct",
                ),
                (
                    r"(?:1년|1Y)\s*(?:수익률|수익|성과).{0,120}"
                    r"(?:큰|작은|높은|낮은|좋은|상위|하위|성과순|순)",
                    "one_year_return_pct",
                ),
                (
                    r"18\s*개월\s*(?:수익률|수익|성과).{0,120}"
                    r"(?:큰|작은|높은|낮은|좋은|상위|하위|성과순|순)",
                    "eighteen_month_return_pct",
                ),
                (
                    r"2\s*년\s*(?:수익률|수익|성과).{0,120}"
                    r"(?:큰|작은|높은|낮은|좋은|상위|하위|성과순|순)",
                    "two_year_return_pct",
                ),
                (
                    r"3\s*년\s*(?:수익률|수익|성과).{0,120}"
                    r"(?:큰|작은|높은|낮은|좋은|상위|하위|성과순|순)",
                    "three_year_return_pct",
                ),
                (
                    r"5\s*년\s*(?:수익률|수익|성과).{0,120}"
                    r"(?:큰|작은|높은|낮은|좋은|상위|하위|성과순|순)",
                    "five_year_return_pct",
                ),
                (
                    r"(?:AUM|순자산|운용\s*규모).{0,30}"
                    r"(?:큰|작은|높은|낮은|많은|적은|상위|순)",
                    "aum",
                ),
                (r"돈이.{0,30}(?:가장 많이|많이) 모인", "aum"),
            ]
        )
    else:
        explicit_rank_patterns.extend(
            [
                (
                    r"(?:일 거래대금|거래대금|외화거래대금).{0,20}"
                    r"(?:큰|작은|높은|낮은|많은|적은|순)",
                    "daily_trading_value",
                ),
                (
                    r"(?:일 거래량|거래량|일간 거래량|하루 거래량).{0,20}"
                    r"(?:큰|작은|높은|낮은|많은|적은|상위|하위|순)",
                    "daily_volume",
                ),
                (
                    r"(?:추적\s*오차(?:율)?).{0,20}"
                    r"(?:큰|작은|높은|낮은|많은|적은|순)",
                    "tracking_error_pct",
                ),
                (
                    r"(?:괴리율|가격\s*괴리율|NAV\s*괴리율).{0,20}"
                    r"(?:큰|작은|높은|낮은|많은|적은|순)",
                    "discrepancy_pct",
                ),
                (
                    r"(?<!\d)(?:20\s*거래일|20\s*일|1\s*개월|한\s*달).{0,8}?"
                    r"(?:연환산\s*)?(?:변동성|흔들림)"
                    r".{0,20}(?:큰|작은|높은|낮은|많은|적은|순)",
                    "twenty_day_volatility_pct",
                ),
                (
                    r"(?<!\d)(?:60\s*거래일|60\s*일|3\s*개월).{0,8}?"
                    r"(?:연환산\s*)?(?:변동성|흔들림)"
                    r".{0,20}(?:큰|작은|높은|낮은|많은|적은|순)",
                    "sixty_day_volatility_pct",
                ),
                (
                    r"(?<!\d)(?:120\s*거래일|120\s*일|6\s*개월).{0,8}?"
                    r"(?:연환산\s*)?(?:변동성|흔들림)"
                    r".{0,20}(?:큰|작은|높은|낮은|많은|적은|순)",
                    "one_hundred_twenty_day_volatility_pct",
                ),
                (
                    r"(?<!\d)(?:252\s*거래일|252\s*일|1\s*년).{0,8}?"
                    r"(?:연환산\s*)?(?:변동성|흔들림)"
                    r".{0,20}(?:큰|작은|높은|낮은|많은|적은|순)",
                    "two_hundred_fifty_two_day_volatility_pct",
                ),
                (
                    r"(?:최근\s*1\s*년\s*)?(?:연환산\s*)?분배(?:금)?\s*수익률.{0,20}"
                    r"(?:큰|작은|높은|낮은|많은|적은|상위|하위|순)",
                    "distribution_yield_pct",
                ),
                (
                    r"(?:1일|하루|당일|오늘|일간)\s*"
                    r"(?:수익률|수익|성과|성적(?:표)?|상승률|등락률)"
                    r".{0,120}(?:큰|작은|높은|낮은|상위|하위|순)",
                    "one_day_return_pct",
                ),
                (
                    r"(?:1개월|한\s*달|1M)\s*(?:수익률|수익|성과|성적(?:표)?).{0,120}"
                    r"(?:큰|작은|높은|낮은|상위|하위|순)",
                    "one_month_return_pct",
                ),
                (
                    r"(?:3개월|석\s*달|3M)\s*(?:수익률|수익|성과|성적(?:표)?).{0,120}"
                    r"(?:큰|작은|높은|낮은|상위|하위|순)",
                    "three_month_return_pct",
                ),
                (
                    r"(?:6개월|반년|6M)\s*(?:수익률|수익|성과).{0,120}"
                    r"(?:큰|작은|높은|낮은|상위|하위|순)",
                    "six_month_return_pct",
                ),
                (
                    r"(?:1년|일\s*년|1Y)\s*(?:수익률|수익|성과).{0,120}"
                    r"(?:큰|작은|높은|낮은|상위|하위|순)",
                    "one_year_return_pct",
                ),
                (
                    r"(?:YTD|연초\s*이후|연초\s*대비)\s*(?:수익률|수익|성과).{0,120}"
                    r"(?:큰|작은|높은|낮은|상위|하위|순)",
                    "ytd_return_pct",
                ),
                (
                    r"(?:1\s*개월|한\s*달|월)\s*평균\s*거래대금.{0,24}"
                    r"(?:큰|작은|높은|낮은|많은|적은|상위|하위|순)",
                    "one_month_average_trading_value",
                ),
                (
                    r"(?:종가|마감\s*가격).{0,20}(?:큰|작은|높은|낮은|순)",
                    "close_price",
                ),
                (
                    r"(?:AUM|순자산(?!가치)"
                    + (r"(?!\s*총액)" if family == "domestic_etp" else r"(?:\s*총액)?")
                    + r"|일간\s*순자산총액|최종\s*(?:AUM|순자산)|"
                    r"운용\s*(?:자산|규모)).{0,20}"
                    r"(?:큰|작은|높은|낮은|많은|적은|상위|제일\s*큰|순|"
                    r"highest\s*[- ]to\s*[- ]lowest|lowest\s*[- ]to\s*[- ]highest|"
                    r"ascending|descending)",
                    "aum",
                ),
                (
                    r"(?:총보수|보수|연간\s*보수율).{0,20}"
                    r"(?:큰|작은|높은|낮은|많은|적은|저렴한|비싼|상위|순)",
                    "total_expense_ratio_pct",
                ),
                (
                    r"(?:동적\s*(?:지표\s*)?기준일|AUM 업데이트일|최신 기준일)"
                    r".{0,80}(?:순|최신|오래된)",
                    "dynamic_as_of",
                ),
                (
                    r"정적\s*(?:지표\s*)?기준일.{0,80}(?:순|최신|오래된)",
                    "static_as_of",
                ),
            ]
        )
        if family == "domestic_etp":
            explicit_rank_patterns.append(
                (
                    r"순\s*자산\s*총액.{0,20}"
                    r"(?:큰|작은|높은|낮은|많은|적은|상위|하위|순)",
                    "net_asset_total",
                )
            )
    if family is not None:
        # The registry is the control layer: every numeric sortable surface it
        # declares must resolve. These come last so the hand written rules above
        # keep priority where they encode extra context.
        explicit_rank_patterns.extend(registry_rank_patterns(family))
    if re.search(r"\d+\s*(?:등(?!급)|위(?!험))", question):
        for field, aliases in numeric_fields:
            alias_pattern = _alias_group(aliases)
            explicit_rank_patterns.append(
                (rf"(?:{alias_pattern}).{{0,24}}?\d+\s*(?:등(?!급)|위(?!험))", field)
            )

    # Field-first ordinal grammar is independent of each field's hand-written
    # adjective aliases.  It covers ``순자산 하위 4개 국내 ETF`` and the same
    # structure for every approved numeric field while retaining the registry's
    # family/sortability validation below.
    for field, aliases in numeric_fields:
        ordered_aliases = sorted(set(aliases), key=lambda value: (-len(value), value))
        alias_pattern = "|".join(re.escape(alias) for alias in ordered_aliases)
        explicit_rank_patterns.append(
            (
                rf"(?:{alias_pattern})(?:이|가|은|는|을|를)?\s*"
                r"(?:기준(?:으로)?\s*)?(?:상위|하위)",
                field,
            )
        )

    # An adjective inside one official risk-grade enum is part of the filter
    # literal, not a separate ranking request.  Ownership by the longer enum
    # span prevents ``높은위험(2등급)`` from silently adding
    # ``risk_level DESC`` while preserving genuine requests such as
    # ``위험등급이 높은 순``.
    explicit_risk_grade_spans = tuple(
        match.span()
        for match in re.finditer(
            r"(?:매우\s*(?:높은|낮은)|다소\s*높은|높은|보통|낮은)\s*"
            r"위험\s*\(\s*[1-6]\s*등급\s*\)",
            question,
            flags=re.IGNORECASE,
        )
    )
    alias_matches = tuple(
        item
        for item in rank_alias_matches(question, family)
        if re.match(
            rf"^(?:률|율)?(?:이|가|은|는|을|를)?\s*{NUMBER}\s*"
            r"(?:%|퍼센트)?(?:을|를)?\s*(?:이상|이하|초과|미만|"
            r"보다\s*(?:큰|높은|많은|작은|낮은|적은))",
            question[item.end : item.end + 48],
            flags=re.IGNORECASE,
        )
        is None
        and not any(
            item.start < enum_end and enum_start < item.end
            for enum_start, enum_end in explicit_risk_grade_spans
        )
    )
    alias_keys = {(item.field_id, item.direction.value) for item in alias_matches}
    alias_resolution = alias_matches[0] if len(alias_keys) == 1 else None
    if len(alias_keys) > 1:
        semantic_ambiguity_spans.extend(item.text for item in alias_matches)

    registry_rank_matches = (
        _explicit_registry_rank_matches(question, family) if family is not None else ()
    )
    registry_rank_fields = {field for field, _ in registry_rank_matches}
    registry_rank_resolution = (
        registry_rank_matches[0] if len(registry_rank_fields) == 1 else None
    )
    if len(registry_rank_fields) > 1:
        semantic_ambiguity_spans.extend(match.group(0) for _, match in registry_rank_matches)

    ranking_candidates: list[tuple[int, int, int, str, re.Match[str]]] = []
    for priority, (pattern, field) in enumerate(explicit_rank_patterns):
        match = re.search(pattern, question, flags=re.IGNORECASE)
        if match is not None:
            ranking_candidates.append((len(match.group(0)), -match.start(), priority, field, match))
    ranking_field: str | None = None
    ranking_match: re.Match[str] | None = None
    if ranking_candidates:
        _, _, _, ranking_field, ranking_match = min(ranking_candidates)
    if ranking_field is None and family is not None:
        referential_direction = re.search(
            r"(?:TOP|BOTTOM)\s*\d*|"
            r"(?:(?:그|이|해당)\s*(?:값|수치)(?:이|가|은|는)?\s*)?"
            r"(?:높은|낮은|큰|작은|많은|적은)\s*순",
            question,
            flags=re.IGNORECASE,
        )
        if referential_direction is not None:
            antecedent_fields = {
                mention.field_id
                for mention in owned_numeric_mentions
                if mention.start < referential_direction.start()
                and mention.definition.sortable
            }
            if len(antecedent_fields) == 1:
                ranking_field = next(iter(antecedent_fields))
                ranking_match = referential_direction
            elif len(antecedent_fields) > 1:
                semantic_ambiguity_spans.append(referential_direction.group(0))
    if registry_rank_resolution is not None:
        registry_field, registry_match = registry_rank_resolution
        existing_fields = {
            field
            for field in (
                ranking_field,
                alias_resolution.field_id if alias_resolution is not None else None,
            )
            if field is not None
        }
        conflicting_spans: list[tuple[int, int]] = []
        if (
            ranking_field is not None
            and ranking_field != registry_field
            and ranking_match is not None
        ):
            conflicting_spans.append(ranking_match.span())
        if alias_resolution is not None and alias_resolution.field_id != registry_field:
            conflicting_spans.append((alias_resolution.start, alias_resolution.end))
        nested_conflict = bool(conflicting_spans) and all(
            start < registry_match.end() and registry_match.start() < end
            for start, end in conflicting_spans
        )
        if not existing_fields or existing_fields == {registry_field} or nested_conflict:
            ranking_field = registry_field
            ranking_match = registry_match
            if alias_resolution is not None and alias_resolution.field_id != registry_field:
                alias_resolution = None
        elif registry_field not in existing_fields:
            semantic_ambiguity_spans.extend(
                [
                    registry_match.group(0),
                    *(question[start:end] for start, end in conflicting_spans),
                ]
            )
            ranking_field = None
            ranking_match = None
            alias_resolution = None
    if ranking_field is not None and family is not None:
        try:
            sortable = (
                load_field_registry()
                .require_field(
                    ranking_field,
                    [family],
                )
                .sortable
            )
        except ValueError:
            sortable = False
        if not sortable:
            ranking_field = None
    if (
        ranking_field is not None
        and alias_resolution is not None
        and ranking_field != alias_resolution.field_id
    ):
        # A broad lexical metric regex may start at a threshold field and run
        # across to the direction belonging to a later, precise Typed Alias
        # (``보수 0.15% 이하 ... 몸집 큰 순``).  The nested alias is the
        # clause-local ranking expression; treating the whole bridge as a
        # second rank silently confuses filter and ordering semantics.
        if (
            ranking_match is not None
            and ranking_match.start() <= alias_resolution.start
            and alias_resolution.end <= ranking_match.end()
        ):
            ranking_field = None
            ranking_match = None
        elif (
            ranking_match is not None
            and alias_resolution.start <= ranking_match.start()
            and (
                ranking_match.end() <= alias_resolution.end
                or re.fullmatch(
                    r"\s*(?:순|순서)(?:으로|로)?\s*",
                    question[alias_resolution.end : ranking_match.end()],
                    flags=re.IGNORECASE,
                )
                is not None
            )
        ):
            alias_is_locked_filter = any(
                item["field"] == alias_resolution.field_id
                and item["operator"] in {"gt", "gte", "lt", "lte", "between"}
                for item in required
            )
            if alias_is_locked_filter and alias_resolution.start < ranking_match.start():
                # Broad period aliases intentionally accept long global-order
                # sentences, but they must not steal the direction from a
                # later field-labelled rank after their own numeric filter:
                # ``3개월 수익률 5% 이상 ... 순자산 낮은 순``.  The filter
                # remains locked and the final explicit metric owns ranking.
                alias_resolution = None
            else:
                # A short generic field phrase may be nested inside a more
                # precise Typed Alias (``거래대금`` inside ``한 달 평균
                # 거래대금``). The longest typed semantic owner wins.
                ranking_field = None
                ranking_match = None
        else:
            semantic_ambiguity_spans.extend(
                (ranking_match.group(0), alias_resolution.text)
                if ranking_match is not None
                else (alias_resolution.text,)
            )

    # If the user supplies exactly one numeric threshold and then says
    # ``그중 높은/낮은 순``, the threshold field is the only possible ranking
    # metric.  Reusing it is deterministic; multiple numeric fields still
    # fail closed because the referent would be ambiguous.
    generic_threshold_ranking = None
    if ranking_field is None and alias_resolution is None and family is not None:
        generic_threshold_ranking = re.search(
            r"(?:(?:인\s*)?(?:(?:것|상품|종목)\s*)?중|"
            r"(?:상품|종목)(?:을|를|이|가|은|는)?|"
            r"(?:\d+|한|두|세|셋|네|넷|다섯|여섯|일곱|여덟|아홉|열)\s*"
            r"(?:개|종목|상품)(?:를|을)?)\s*"
            r"(?:제일|가장)?\s*"
            r"(?P<direction>상위|하위|높은|낮은|큰|작은|저렴한|비싼)"
            r"(?:\s*순(?:서)?(?:로)?)?",
            question,
            flags=re.IGNORECASE,
        )
        threshold_fields = {
            item["field"]
            for item in required
            if item["operator"] in {"gt", "gte", "lt", "lte", "between"}
        }
        if generic_threshold_ranking is not None and len(threshold_fields) == 1:
            candidate_field = next(iter(threshold_fields))
            try:
                sortable = (
                    load_field_registry()
                    .require_field(candidate_field, [family])
                    .sortable
                )
            except ValueError:
                sortable = False
            if sortable:
                ranking_field = candidate_field
                ranking_match = generic_threshold_ranking
    if ranking_field is not None:
        assert ranking_match is not None
        ranking_surface = ranking_match.group(0)
        name_order_direction = (
            _explicit_name_order_direction(question)
            if ranking_field in {"product_name", "short_name", "ticker"}
            else None
        )
        local_ascending = re.search(
            r"하위|낮은|낮게|작은|작게|짧은|짧게|적은|적게|저렴한|저렴하게|"
            r"싼|싸게|가벼운|가볍게|"
            r"빠른|빠르게|가까운|오래된|오래된\s*순|"
            r"BOTTOM|lowest\s*[- ]to\s*[- ]highest|ascending",
            ranking_surface,
            flags=re.IGNORECASE,
        )
        local_descending = re.search(
            r"높은|높게|큰|크게|긴|길게|많은|많이|비싼|비싸게|앞선|뒤쪽|늦게|"
            r"\d+\s*(?:등(?!급)|위(?!험))|TOP|"
            r"highest\s*[- ]to\s*[- ]lowest|descending",
            ranking_surface,
            flags=re.IGNORECASE,
        )
        if name_order_direction is not None:
            direction = name_order_direction
        elif local_ascending is not None and local_descending is not None:
            semantic_ambiguity_spans.append(ranking_surface)
            direction = "asc"
        elif local_ascending is not None:
            direction = "asc"
        elif local_descending is not None:
            direction = "desc"
        elif alias_resolution is not None and alias_resolution.field_id == ranking_field:
            # The explicit pattern identifies the field, while the typed alias
            # may carry the only unambiguous direction (for example
            # ``상환이 먼저 오는``). Do not replace that evidence with the
            # generic descending fallback.
            direction = alias_resolution.direction.value
        elif ranking_field in {"product_name", "short_name", "ticker"} and not descending:
            direction = "asc"
        else:
            direction = "asc" if ascending else "desc"
        rankings.append({"field": ranking_field, "direction": direction, "nulls": "last"})
    elif alias_resolution is not None:
        rankings.append(
            {
                "field": alias_resolution.field_id,
                "direction": alias_resolution.direction.value,
                "nulls": "last",
            }
        )

    # When a shared cross-family sentence names the period once and then says
    # only "수익률이 0보다 ...", bind the threshold to the already verified
    # ranking field.  This avoids dropping a hard filter while still refusing
    # to guess a period when no return ranking was resolved.
    if rankings and rankings[0]["field"] in {
        "one_day_return_pct",
        "one_week_return_pct",
        "one_month_return_pct",
        "three_month_return_pct",
        "six_month_return_pct",
        "one_year_return_pct",
        "eighteen_month_return_pct",
        "two_year_return_pct",
        "three_year_return_pct",
        "five_year_return_pct",
        "ytd_return_pct",
    }:
        def numeric_threshold_already_owned(
            value: int | float | list[int | float],
            operator: str,
        ) -> bool:
            """Do not attach a field-labelled number to the ranking metric again.

            Cross-family questions may state the shared return period once and
            then use an unlabeled threshold (``1% 초과``), which is safe.  In a
            bounded query such as ``순자산 100억 초과 ... 1개월 수익률 순``
            the same number is already owned by AUM and must not be duplicated
            as a return constraint.
            """

            return any(
                (
                    item["value"] == value
                    or (
                        not isinstance(value, list)
                        and isinstance(item["value"], list)
                        and value in item["value"]
                    )
                )
                and (
                    item["field"] == rankings[0]["field"]
                    or item["operator"] == operator
                    or item["operator"] == "between"
                )
                for item in required
            )

        generic_return_band = re.search(
            rf"({NUMBER})\s*(?:%|퍼센트)?\s*이상\s*"
            rf"({NUMBER})\s*(?:%|퍼센트)?\s*이하",
            question,
            flags=re.IGNORECASE,
        )
        if generic_return_band is not None:
            value = [
                _scaled_number(generic_return_band.group(1)),
                _scaled_number(generic_return_band.group(2)),
            ]
            if not numeric_threshold_already_owned(value, "between"):
                add(rankings[0]["field"], value, "between")
        generic_return_comparison = re.search(
            rf"({NUMBER})\s*(?:%|퍼센트)?(?:을|를)?\s*"
            r"(이상|이하|초과|미만)",
            question,
            flags=re.IGNORECASE,
        )
        if generic_return_comparison is not None:
            value = _scaled_number(generic_return_comparison.group(1))
            operator = {
                "이상": "gte",
                "이하": "lte",
                "초과": "gt",
                "미만": "lt",
            }[generic_return_comparison.group(2)]
            if not numeric_threshold_already_owned(value, operator):
                add(rankings[0]["field"], value, operator)
        generic_return_threshold = re.search(
            rf"(?:(?:수익률|수익|성과)(?:이|가)?\s*)?"
            rf"({NUMBER})\s*(?:%|퍼센트)?\s*보다\s*"
            r"(큰|높은|작은|낮은)",
            question,
            flags=re.IGNORECASE,
        )
        if generic_return_threshold is not None:
            operator = "gt" if generic_return_threshold.group(2) in {"큰", "높은"} else "lt"
            value = _scaled_number(generic_return_threshold.group(1))
            if not numeric_threshold_already_owned(value, operator):
                add(rankings[0]["field"], value, operator)

    coverage = _SEMANTIC_COVERAGE_GATE.evaluate(
        question,
        interaction_intent="search",
    )
    coverage_ambiguity_spans = list(coverage.ambiguity_spans)
    if availability_spans:
        compact_availability = {
            re.sub(r"\s+", "", span).casefold() for span in availability_spans
        }
        coverage_ambiguity_spans = [
            span
            for span in coverage_ambiguity_spans
            if not any(
                re.sub(r"\s+", "", span).casefold() in surface
                for surface in compact_availability
                if span.strip()
            )
        ]
    resolved_fields = {
        item["field"] for item in required
    } | {item["field"] for item in rankings}
    resolved_numeric_aliases = {
        field: aliases
        for field, aliases in numeric_fields
        if field in resolved_fields
    }

    def span_is_owned_by_one_resolved_field(span: str) -> bool:
        compact_span = re.sub(r"\s+", "", span).casefold()
        if not compact_span:
            return False
        owners = {
            field
            for field, aliases in resolved_numeric_aliases.items()
            if any(
                (
                    compact_span in re.sub(r"\s+", "", alias).casefold()
                    or re.sub(r"\s+", "", alias).casefold() in compact_span
                )
                and re.search(_flexible_alias_pattern((alias,)), question, re.IGNORECASE)
                is not None
                for alias in aliases
            )
        }
        return len(owners) == 1

    coverage_ambiguity_spans = [
        span
        for span in coverage_ambiguity_spans
        if not span_is_owned_by_one_resolved_field(span)
    ]
    semantic_ambiguity_spans = [
        span
        for span in semantic_ambiguity_spans
        if not span_is_owned_by_one_resolved_field(span)
    ]
    locked_numeric_fields = {
        item["field"]
        for item in required
        if item["operator"] in {"gt", "gte", "lt", "lte", "between"}
    }
    constrained_numeric_fields = {
        item["field"]
        for item in required
        if item["field"] in {field for field, _aliases in numeric_fields}
    }
    ambiguous_numeric_referent = re.search(
        r"(?:그|이|해당)\s*값(?:이|가|은|는|을|를)?\s*"
        r"(?:가장\s*)?(?:높|높은|낮|낮은|크|큰|작|작은|많|많은|적|적은|긴|짧)",
        question,
        flags=re.IGNORECASE,
    )
    if ambiguous_numeric_referent is not None and len(constrained_numeric_fields) != 1:
        # A pronoun may inherit one explicit numeric antecedent, never choose
        # between several.  Longest-span alias ownership must not accidentally
        # turn the first of two metrics into the winner.
        rankings.clear()
        semantic_ambiguity_spans.append(ambiguous_numeric_referent.group(0))
    if (
        len(rankings) == 1
        and len(locked_numeric_fields) == 1
        and rankings[0]["field"] in locked_numeric_fields
    ):
        # Pronouns such as ``그 값`` and ``그것부터`` have exactly one
        # possible antecedent when the same field owns both the numeric bound
        # and the ranking.  Resolve only that single-owner case; multiple
        # numeric fields remain fail-closed.
        coverage_ambiguity_spans = [
            span
            for span in coverage_ambiguity_spans
            if re.fullmatch(
                r"(?:(?:그|이|해당)\s*)?(?:값(?:이|을|은|는)?|것(?:부터|을|이|은|는)?)",
                span.strip(),
                flags=re.IGNORECASE,
            )
            is None
        ]
    if generic_threshold_ranking is not None and ranking_field is not None:
        direction = generic_threshold_ranking.group("direction")
        resolved_direction_spans = {
            direction,
            f"{direction} 순",
            f"{direction} 순서",
            generic_threshold_ranking.group(0).strip(),
        }
        coverage_ambiguity_spans = [
            span
            for span in coverage_ambiguity_spans
            if not any(
                span.strip() in resolved_span or resolved_span in span.strip()
                for resolved_span in resolved_direction_spans
                if span.strip() and resolved_span
            )
        ]
    if ranking_match is not None and ranking_field is not None:
        ranking_surface = ranking_match.group(0).strip()
        coverage_ambiguity_spans = [
            span
            for span in coverage_ambiguity_spans
            if not (
                span.strip()
                and (
                    re.sub(r"\s+", "", span)
                    == re.sub(r"\s+", "", ranking_surface)
                    or (
                        re.sub(r"\s+", "", span).startswith(
                            re.sub(r"\s+", "", ranking_surface)
                        )
                        and re.fullmatch(
                            r"\s*(?:순|순서)(?:으로|로)?\s*",
                            span[len(ranking_surface) :],
                            flags=re.IGNORECASE,
                        )
                        is not None
                    )
                    or (
                        span.strip() in ranking_surface
                        and re.fullmatch(
                            r"(?:가장\s*|제일\s*)?(?:높은|낮은|큰|작은|많은|적은|"
                            r"긴|짧은|빠른|오래된|상위|하위|오름차순|내림차순)",
                            span.strip(),
                            flags=re.IGNORECASE,
                        )
                    )
                )
            )
        ]
    semantic_ambiguity_spans.extend(
        "AUM 비교 통화" if span == "trading_currency(AUM 비교 통화)" else span
        for span in coverage_ambiguity_spans
    )
    semantic_unsupported_spans.extend(coverage.unsupported_spans)

    if ranking_intent_present(question) and not rankings:
        semantic_ambiguity_spans.extend(
            coverage_ambiguity_spans or ranking_cue_spans(question) or ("ranking_metric",)
        )
    if (
        ranking_intent_present(question)
        and rankings
        and all(item["field"] in {"product_name", "short_name", "ticker"} for item in rankings)
        and not explicit_product_name_order(question)
    ):
        semantic_ambiguity_spans.extend(ranking_cue_spans(question) or ("ranking_metric",))

    # A natural range expression can be observed twice by independent lexical
    # rules (for example, ``1년 이상 5년 이하`` as both BETWEEN and GTE).
    # Preserve the range and discard only an exactly duplicated inclusive
    # endpoint; all genuinely narrower constraints remain intact.
    between_endpoints = {
        (item["field"], item["value"][0], item["value"][1])
        for item in required
        if item["operator"] == "between"
        and isinstance(item["value"], list)
        and len(item["value"]) == 2
    }
    if between_endpoints:
        required[:] = [
            item
            for item in required
            if not any(
                item["field"] == field
                and (
                    (item["operator"] == "gte" and item["value"] == lower)
                    or (item["operator"] == "lte" and item["value"] == upper)
                )
                for field, lower, upper in between_endpoints
            )
        ]

    exact_lookup = any(item["field"] in {"ticker", "product_id", "isin"} for item in required)
    unresolved_relative_preference = re.search(
        r"(?:(?:기준|비교)\s*상품|[A-Z][A-Z0-9._-]{1,15}|[A-Z]?\d{6})"
        r"(?:을|를|보다|에\s*비해|대비)?\s*.{0,16}?"
        r"(?:나은|좋은|우수한|괜찮은)",
        question,
        flags=re.IGNORECASE,
    )
    if unresolved_relative_preference is not None and not rankings:
        # A comparison target without an explicit metric cannot authorize a
        # default product-name sort.  Preserve the referent as an unresolved
        # semantic span so the request ends in CLARIFY instead of broad SQL.
        semantic_ambiguity_spans.append(unresolved_relative_preference.group(0))
    if (
        family == "fund"
        and not rankings
        and not exact_lookup
        and not any(phrase in question for phrase in unsupported_patterns)
        and not any(phrase in question for phrase in ambiguity_patterns)
        and not semantic_unsupported_spans
        and not semantic_ambiguity_spans
        and not ranking_intent_present(question)
    ):
        rankings.append({"field": "product_name", "direction": "asc", "nulls": "last"})

    requested_limit = requested_result_limit(question)
    limit = requested_limit if requested_limit is not None else (1 if exact_lookup else 5)
    unsupported_spans = [
        *[phrase for phrase in unsupported_patterns if phrase in question],
        *semantic_unsupported_spans,
    ]
    if family == "fund" and unsupported_spans:
        rankings = []
    if family == "bond":
        rating_token = re.search(
            r"(?:신용\s*등급(?:이|은|가)?\s*(?:정확히|정확하게|딱)?\s*"
            r"(?P<prefix>[A-Z]{1,5}(?:[+0-])?)"
            r"(?=인|\s|$)|(?P<suffix>[A-Z]{1,5}(?:[+0-])?)\s*등급|"
            r"(?P<label_first>[A-Z]{1,5}(?:[+0-])?)\s*신용\s*등급|"
            r"(?<![A-Z])(?P<bare>[A-D]{1,5}(?:[+0-])?)\s*(?:국내\s*)?채권)",
            question,
        )
        rating_value = (
            None
            if rating_token is None
            else (
                rating_token.group("prefix")
                or rating_token.group("suffix")
                or rating_token.group("label_first")
                or rating_token.group("bare")
            )
        )
        if rating_value is not None:
            required[:] = [item for item in required if item["field"] != "credit_rating"]
            # A standalone rating directly owning the bond-family phrase is
            # an exact categorical filter whether the request is SEARCH or
            # AGGREGATE.  Preserve unknown literals as well: the downstream
            # Registry/plan boundary must fail closed instead of silently
            # widening the query after a misspelled rating.
            add("credit_rating", rating_value.upper(), "eq")
        ordered_rating = re.search(
            r"(?:신용\s*등급(?:이|은|가)?\s*)?"
            r"(AAA|AA(?:\+|0|-)?|A(?:\+|0|-)?|BBB(?:\+|0|-)?|"
            r"BB(?:\+|0|-)?|B(?:\+|0|-)?|CCC|CC0|C0|C)\s*"
            r"(?:급|등급)?\s*(이상|이하|초과|미만)",
            question,
        )
        if ordered_rating:
            required[:] = [item for item in required if item["field"] != "credit_rating"]
            ratings = list(
                load_field_registry().require_field("credit_rating", ["bond"]).enum_values
            )
            requested_rating = ordered_rating.group(1)
            boundary = ordered_rating.group(2)
            parent_members = {
                "AA": ("AA+", "AA0", "AA-"),
                "A": ("A+", "A0", "A-"),
                "BBB": ("BBB+", "BBB0", "BBB-"),
                "BB": ("BB+", "BB0", "BB-"),
                "B": ("B+", "B0", "B-"),
            }.get(requested_rating)
            if parent_members is not None:
                # Inclusive boundaries contain every official member of the
                # named band; strict boundaries exclude the whole band.
                threshold_rating = (
                    parent_members[-1]
                    if boundary in {"이상", "미만"}
                    else parent_members[0]
                )
            else:
                threshold_rating = requested_rating
            threshold_index = ratings.index(threshold_rating)
            if boundary == "이상":
                selected_ratings = ratings[: threshold_index + 1]
            elif boundary == "초과":
                selected_ratings = ratings[:threshold_index]
            elif boundary == "이하":
                selected_ratings = ratings[threshold_index:]
            else:
                selected_ratings = ratings[threshold_index + 1 :]
            if selected_ratings:
                add("credit_rating", selected_ratings, "in")
            else:
                unsupported_spans.append(ordered_rating.group(0))
        else:
            vague_ordered_rating = re.search(
                r"신용등급.{0,20}?(?:높은|낮은)",
                question,
            )
            if vague_ordered_rating and not any(
                item["field"] == "credit_rating" for item in required
            ):
                required[:] = [item for item in required if item["field"] != "credit_rating"]
                unsupported_spans.append(vague_ordered_rating.group(0))
    ambiguity_spans = [
        *[phrase for phrase in ambiguity_patterns if phrase in question],
        *semantic_ambiguity_spans,
    ]
    if family == "fund" and {item["field"] for item in required} >= {
        "sellable",
        "company_sellable",
    }:
        # Both predicates share the word ``판매``.  Preserve their stable
        # semantic order (market sale status, then our-company availability)
        # regardless of whether a typed alias or the legacy parser observed
        # either surface first.
        priority = {"public_offering": 0, "sellable": 1, "company_sellable": 2}
        required.sort(key=lambda item: priority.get(item["field"], 3))
    return {
        "product_family": family,
        "required_constraints": required,
        "any_constraint_groups": any_constraint_groups,
        "required_eq_constraints": [item for item in required if item["operator"] == "eq"],
        "required_rankings": rankings,
        "limit": limit,
        "unsupported_spans": list(dict.fromkeys(unsupported_spans)),
        "ambiguity_spans": list(dict.fromkeys(ambiguity_spans)),
    }


def _hint_constraints(
    hints: list[dict[str, Any]],
    product_family: str,
) -> list[dict[str, Any]]:
    registry = load_field_registry()
    grouped_equalities: dict[str, list[Any]] = defaultdict(list)
    constraints: list[dict[str, Any]] = []
    for hint in hints:
        if hint["operator"] == "eq":
            if hint["value"] not in grouped_equalities[hint["field"]]:
                grouped_equalities[hint["field"]].append(hint["value"])
            continue
        definition = registry.require_field(hint["field"], [product_family])
        constraints.append(
            {
                **hint,
                "unit": definition.unit,
                "strength": "locked",
            }
        )
    for field, values in grouped_equalities.items():
        definition = registry.require_field(field, [product_family])
        values = sorted(values, key=lambda value: (type(value).__name__, repr(value)))
        constraints.append(
            {
                "field": field,
                "operator": "eq" if len(values) == 1 else "in",
                "value": values[0] if len(values) == 1 else values,
                "unit": definition.unit,
                "strength": "locked",
            }
        )
    return constraints


def _constraint_expression_payload(
    constraints: list[dict[str, Any]],
    any_groups: list[list[dict[str, Any]]],
    product_family: str,
) -> dict[str, Any] | None:
    """Bind server-parsed OR branches to stable typed constraint keys."""

    children: list[dict[str, Any]] = []
    for raw_group in any_groups:
        typed_members = _hint_constraints(raw_group, product_family)
        keys: list[str] = []
        for member in typed_members:
            matches = [constraint for constraint in constraints if constraint == member]
            if len(matches) != 1:
                raise ValueError("logical constraint branch was dropped or duplicated")
            keys.append(constraint_key(Constraint.model_validate(matches[0])))
        keys = list(dict.fromkeys(keys))
        if len(keys) < 2:
            raise ValueError("OR requires two distinct typed constraint branches")
        children.append(
            {
                "operator": "any",
                "constraint_keys": keys,
                "children": [],
            }
        )
    if not children:
        return None
    if len(children) == 1:
        return children[0]
    return {
        "operator": "all",
        "constraint_keys": [],
        "children": children,
    }


def canonicalize_query_plan_payload(
    question: str,
    payload: dict[str, Any],
    *,
    force_product_family_hint: bool = False,
) -> dict[str, Any]:
    raw_families = payload.get("product_families")
    product_family_hint = (
        raw_families[0]
        if isinstance(raw_families, list)
        and len(raw_families) == 1
        and isinstance(raw_families[0], str)
        else None
    )
    hints = build_lexical_hints(
        question,
        product_family_hint,
        force_product_family_hint=force_product_family_hint,
    )
    family = hints["product_family"]
    if family is None:
        family = "overseas_etp"
    payload["schema_version"] = "1.0"
    payload["intent"] = "search"
    payload["product_families"] = [family]
    constraints = _hint_constraints(
        hints["required_constraints"],
        family,
    )
    rankings = hints["required_rankings"]
    explicitly_requested_fields = _explicit_projection_fields(question, family)
    payload["constraints"] = constraints
    constraint_expression = _constraint_expression_payload(
        constraints,
        hints["any_constraint_groups"],
        family,
    )
    if constraint_expression is None:
        payload.pop("constraint_expression", None)
    else:
        payload["constraint_expression"] = constraint_expression
    payload["ranking"] = rankings
    payload["projection"] = search_projection(
        family,
        *(
            constraint["field"]
            for constraint in constraints
            if constraint["field"] != "public_offering"
        ),
        *(ranking["field"] for ranking in rankings),
        *explicitly_requested_fields,
    )
    payload["limit"] = hints["limit"]
    payload["intent_payload"] = {
        "comparison_fields": [],
        "group_by": [],
        "aggregations": [],
        "explain_product_ids": [],
    }

    unsupported_spans = hints["unsupported_spans"]
    ambiguity_spans = hints["ambiguity_spans"]
    if unsupported_spans:
        payload["unsupported_conditions"] = [
            {
                "span": span,
                "reason": "현재 동결된 field registry 또는 상품군에서 지원하지 않는 조건",
            }
            for span in unsupported_spans
        ]
        payload["ambiguities"] = []
    elif ambiguity_spans:
        payload["ambiguities"] = [
            {
                "span": span,
                "reason": "판단 기준이나 임계값이 명시되지 않음",
                "options": ["판단 기준을 구체화한다"],
            }
            for span in ambiguity_spans
        ]
        payload["unsupported_conditions"] = []
    else:
        payload["ambiguities"] = []
        payload["unsupported_conditions"] = []
    return payload


def canonicalize_linked_query_plan(question: str, plan: QueryPlan) -> QueryPlan:
    payload = canonicalize_query_plan_payload(question, plan.model_dump(mode="json"))
    return QueryPlan.model_validate(payload)
