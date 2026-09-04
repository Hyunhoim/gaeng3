from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from finance_agent_core.config import ValueType, load_field_registry
from finance_agent_core.contracts.queryplan import (
    AggregateFunction,
    ProductFamily,
    QueryPlan,
)


class AggregatePlanParseError(ValueError):
    """Raised when an aggregate request cannot be compiled without guessing."""


@dataclass(frozen=True)
class _Mention:
    field: str
    start: int
    end: int
    phrase: str


_COUNT_CONTEXT = re.compile(
    r"(?:상품|ETF|ETN|ETP|채권|펀드)(?:\s*(?:상품|종목))?(?:의)?\s*"
    r"수(?:를|가|는)?\s*(?:(?:정확히|모두|총)\s*)?"
    r"(?:계산|집계|총합|알려|세어|헤아려)"
)
_NOUN_COUNT = re.compile(
    r"몇\s*(?:개|건|종(?!가)|종목|상품|ETF|ETN|ETP|채권|펀드)|"
    r"(?:종목|상품)(?:이|가|은|는)?\s*몇\s*(?:개|건)?|"
    r"(?:전체\s*|각\s*)?(?:종목|상품|펀드)\s*수|"
    r"경우(?:의)?\s*수|"
    r"(?:유효|미유효|결측|누락|기재|미기재|입력|미입력|"
    r"확인\s*가능|확인\s*불가|채워진|비어\s*있는|"
    r"보유|미보유|적힌|공란|제공|미제공).{0,12}?"
    # A bare count noun must end at a grammatical boundary.  Without this
    # guard, ``유효한 매수수익률`` was parsed as COUNT because the ``수`` in
    # ``매수`` matched the generic count alternative.
    r"(?:개수|건수|수(?=\s*(?:를|가|는|도|만|$|[,.?])))"
)
_GROUPED_COUNT_CONTEXT = re.compile(
    r"(?:상품|종목|ETF|ETN|ETP|채권|펀드)(?:의)?\s*수(?:를|가|는)?"
    r".{0,48}?(?:별|마다)(?:로)?\s*(?:묶|나눠|구분|세(?:고|어|어서)?|헤아)",
    re.IGNORECASE,
)
_GROUP_LABEL_ORDER = re.compile(
    r"(?:대분류|소분류|상품\s*유형|유형|자산\s*유형|운용\s*속성|"
    r"위험\s*등급|투자\s*지역|거래\s*통화)(?:명)?"
    r"(?:을|를|은|는|이|가)?\s*(?:오름차순|가나다순|알파벳순)",
    re.IGNORECASE,
)
_FUNCTION_PATTERNS = {
    AggregateFunction.COUNT: re.compile(
        rf"{_NOUN_COUNT.pattern}|(?:전체\s*)?후보(?:의)?\s*(?:수|개수|건수)|"
        rf"개수|건수|{_COUNT_CONTEXT.pattern}"
    ),
    AggregateFunction.AVG: re.compile(r"평균"),
    AggregateFunction.MEDIAN: re.compile(
        r"중앙값|중앙\s*값|중간값|중간\s*값|메디안|median",
        re.IGNORECASE,
    ),
    AggregateFunction.SUM: re.compile(r"합계|총합"),
    AggregateFunction.MIN: re.compile(r"최솟값|최소값|최저값|최소(?!\s*\d+\s*개)"),
    AggregateFunction.MAX: re.compile(
        r"최댓값|최대값|최고값|최고\s*수준|최고\s*(?:수익률|보수율|이율)|"
        r"최대(?!\s*\d+\s*개)"
    ),
}
# "비중" is also the tail of four declared public-fund allocation fields
# (국내주식 비중 …). Treating those as a distribution request routed a plain
# threshold question such as "국내주식 비중 0.5% 이상인 공모펀드" to the aggregate
# parser, which then refused it for having no group key — while the same
# question written with "구성비율" executed normally.
_DISTRIBUTION = re.compile(r"분포|(?<!주식)(?<!채권)(?<!주식\s)(?<!채권\s)비중")
_AVAILABILITY_SURFACE = re.compile(
    r"유효|사용할\s*수\s*(?:있|없)|값이?\s*(?:있|없)|"
    r"기재|미기재|입력|미입력|누락|결측|"
    r"확인되|확인할\s*수\s*(?:있|없)|"
    r"채워진|비어\s*있|보유|미보유|적힌|공란|"
    r"제공|미제공|0\s*(?:또는|[\xB7·/와과,])",
    re.IGNORECASE,
)
_AVAILABILITY_CONTRAST = re.compile(
    r"(?:유효.{0,28}?(?:미유효|사용할\s*수\s*없|0|누락|결측)|"
    r"(?:있는|기재된|입력된|확인된).{0,28}?"
    r"(?:없는|미기재|미입력|누락|결측)|"
    r"(?:채워진|보유|적힌|제공).{0,28}?"
    r"(?:비어\s*있|미보유|공란|미제공)|"
    r"(?:값이?\s*있|기재|입력|확인되).{0,28}?"
    r"(?:값이?\s*없|미기재|미입력|누락|결측)|"
    r"(?:미유효|사용할\s*수\s*없|값이?\s*없|0|누락|결측|"
    r"미기재|미입력|비어\s*있|미보유|공란|미제공).{0,28}?"
    r"(?:유효|사용할\s*수\s*있|값이?\s*있|기재|입력|확인되|"
    r"채워진|보유|적힌|제공))",
    re.IGNORECASE,
)
_TRADING_STATUS_CONTRAST = re.compile(
    r"(?:거래\s*(?:가능|정상).{0,32}?거래?\s*(?:정지|중지)|"
    r"거래?\s*(?:정지|중지).{0,32}?거래\s*(?:가능|정상))"
    r".{0,16}?(?:각각|나눠|구분|별로)",
    re.IGNORECASE,
)
_IDENTITY_GROUP_FIELDS = {
    "product_id",
    "product_name",
    "short_name",
    "ticker",
    "isin",
}
_CURATED_GROUP_PHRASES = {
    "product_type": (
        "상품유형",
        "상품 유형",
        "상품의 유형",
        "ETF·ETN",
        "ETF/ETN",
        "ETP 유형",
    ),
    "asset_type": ("자산유형", "자산 유형", "자산군", "투자 자산 유형"),
    "investment_region": ("투자지역", "투자 지역", "지역"),
    "exchange_code": ("거래소",),
    "trading_currency": ("통화", "거래 통화"),
    "risk_level": ("위험등급", "위험 등급"),
    "bond_market": ("채권시장", "채권 시장"),
    "bond_major_class": ("채권대분류", "채권 대분류", "대분류"),
    "bond_type": ("채권종류", "채권 종류", "채종"),
    "manager": ("운용사",),
    "sellable": ("판매여부", "판매 여부", "판매 상태"),
    "company_sellable": (
        "판매회사여부",
        "판매 회사 여부",
        "판매사여부",
        "판매사 여부",
        "판매 회사 기준",
        "당사 판매 여부",
        "당사 판매 가능 여부",
        "미래에셋 판매 여부",
        "미래에셋 판매 가능 여부",
        "미래에셋증권 판매 여부",
        "미래에셋증권 판매 가능 여부",
    ),
    "interest_rate_type": (
        "금리유형",
        "금리 유형",
        "이자율유형",
        "이자율 유형",
        "금리 적용 방식",
        "이자율 적용 방식",
    ),
    "interest_payment_type": (
        "이자지급유형",
        "이자 지급 유형",
        "이자지급방식",
        "이자 지급 방식",
    ),
    "fund_geography_scope": ("국내외구분", "국내외 구분"),
    "fund_management_attribute": ("펀드운용속성", "펀드 운용속성", "운용속성"),
    "investor_type": ("투자자유형", "투자자 유형"),
    "currency_hedged": ("환헤지여부", "환헤지 여부"),
    "trading_suspended": (
        "거래상태",
        "거래 상태",
        "거래정지여부",
        "거래 정지 여부",
        "거래중지여부",
        "거래 중지 여부",
    ),
}
_CURATED_NUMERIC_PHRASES = {
    "aum": (
        "운용규모",
        "운용 규모",
        "운용자산 규모",
        "운용 자산 규모",
        "자산 규모",
    ),
    "total_expense_ratio_pct": (
        "운용 비용",
        "연간 비용",
        "상품 비용",
        "비용",
    ),
    "one_month_return_pct": ("한 달 성과", "월간 성과"),
    "three_month_return_pct": (
        "분기 수익률",
        "분기 성과",
        "석 달 수익률",
        "석 달 성과",
    ),
    "six_month_return_pct": ("반년 성과", "여섯 달 성과"),
    "one_year_return_pct": ("1년 성과", "연간 성과"),
}

_EXPLICIT_FUND_PUBLIC_OFFERING = re.compile(
    r"(?:모집\s*구분.{0,12}?공모|"
    r"공모\s*(?:형|로\s*모집|모집)|"
    r"공개\s*모집)",
    re.IGNORECASE,
)
_ETP_TYPE_GROUPING = re.compile(
    r"ETF인지\s*ETN인지|ETF와\s*ETN으로\s*(?:분류|구분|나눠)|"
    r"ETF\s*여부별|ETP\s*유형별|ETF\s*[·/]\s*ETN\s*유형별|"
    r"(?:ETF\s*(?:와|과|및|또는|[·/])\s*ETN|"
    r"ETN\s*(?:와|과|및|또는|[·/])\s*ETF)"
    r".{0,40}?(?:상품\s*)?(?:유형|종류|구분)\s*(?:별|마다)|"
    r"(?:상품\s*)?(?:유형|종류|구분)\s*(?:별|마다)"
    r".{0,40}?(?:ETF\s*(?:와|과|및|또는|[·/])\s*ETN|"
    r"ETN\s*(?:와|과|및|또는|[·/])\s*ETF)|"
    r"(?:ETF\s*(?:와|과|및|[·/])\s*ETN|"
    r"ETN\s*(?:와|과|및|[·/])\s*ETF)"
    r"(?:으로)?\s*(?:나눠|나누|갈라|구분)",
    re.IGNORECASE,
)
_FUND_GEOGRAPHY_GROUPING = re.compile(
    r"(?:국내\s*(?:와|과|및|[·/,])\s*해외|"
    r"해외\s*(?:와|과|및|[·/,])\s*국내)"
    r"(?:\s*(?:와|과|및|[·/,])\s*(?:국내외\s*)?혼합)?"
    r".{0,20}?(?:(?:지역|범위)\s*)?(?:구분\s*)?"
    r"(?:별|마다|각각|나눠|구분해)",
    re.IGNORECASE,
)


def _field_phrases(field_name: str, definition: Any) -> list[str]:
    values = [
        definition.label,
        *definition.aliases,
        field_name,
        field_name.replace("_", " "),
    ]
    return sorted(
        {value.strip() for value in values if value.strip()},
        key=lambda value: (-len(value), value),
    )


def _whitespace_tolerant_phrase(phrase: str) -> str:
    """Match one registry phrase independent of user-inserted whitespace."""

    compact = re.sub(r"\s+", "", phrase)
    return r"\s*".join(re.escape(character) for character in compact)


def _numeric_mentions(question: str, family: ProductFamily) -> list[_Mention]:
    registry = load_field_registry()
    mentions: list[_Mention] = []
    for field_name, base_definition in registry.fields.items():
        if family.value not in base_definition.datasets:
            continue
        definition = base_definition.resolve(family.value)
        if not definition.aggregatable or definition.value_type is not ValueType.NUMBER:
            continue
        best_by_span: dict[tuple[int, int], _Mention] = {}
        phrases = {
            *_field_phrases(field_name, definition),
            *_CURATED_NUMERIC_PHRASES.get(field_name, ()),
        }
        for phrase in sorted(phrases, key=lambda value: (-len(value), value)):
            for match in re.finditer(
                _whitespace_tolerant_phrase(phrase),
                question,
                flags=re.IGNORECASE,
            ):
                mention = _Mention(field_name, match.start(), match.end(), match.group(0))
                span = (mention.start, mention.end)
                current = best_by_span.get(span)
                if current is None or len(mention.phrase) > len(current.phrase):
                    best_by_span[span] = mention
        mentions.extend(best_by_span.values())
    owned = [
        mention
        for mention in mentions
        if not any(
            other.field != mention.field
            and other.start <= mention.start
            and mention.end <= other.end
            and (other.end - other.start) > (mention.end - mention.start)
            for other in mentions
        )
    ]
    return sorted(owned, key=lambda item: (item.start, -len(item.phrase), item.field))


def _countable_field_mentions(question: str, family: ProductFamily) -> list[_Mention]:
    """Return field mentions that may own an explicit availability count.

    ``COUNT(product_id)`` remains the default.  A source field owns the count
    only when the user explicitly asks whether that field is present/usable.
    This keeps ordinary requests such as ``주식형 펀드 개수`` from being
    rewritten merely because a filter field also appears in the sentence.
    """

    if _AVAILABILITY_SURFACE.search(question) is None:
        return []
    registry = load_field_registry()
    mentions: list[_Mention] = []
    for field_name, base_definition in registry.fields.items():
        if family.value not in base_definition.datasets or field_name in _IDENTITY_GROUP_FIELDS:
            continue
        definition = base_definition.resolve(family.value)
        if not definition.selectable:
            continue
        phrases = {
            *_field_phrases(field_name, definition),
            *_CURATED_NUMERIC_PHRASES.get(field_name, ()),
            *_CURATED_GROUP_PHRASES.get(field_name, ()),
        }
        for phrase in sorted(phrases, key=lambda value: (-len(value), value)):
            for match in re.finditer(
                _whitespace_tolerant_phrase(phrase),
                question,
                flags=re.IGNORECASE,
            ):
                mentions.append(
                    _Mention(field_name, match.start(), match.end(), match.group(0))
                )
    return [
        mention
        for mention in sorted(mentions, key=lambda item: (item.start, -len(item.phrase)))
        if not any(
            other.field != mention.field
            and other.start <= mention.start
            and mention.end <= other.end
            and (other.end - other.start) > (mention.end - mention.start)
            for other in mentions
        )
    ]


def _count_target_field(
    question: str,
    match: re.Match[str],
    family: ProductFamily,
    group_by: list[str],
) -> str:
    # A one-sided request must keep its ordinary IS_PRESENT/IS_MISSING row
    # predicate and count the matching products.  COUNT(field) is necessary
    # only when both availability states must survive into one reduction.
    if _AVAILABILITY_CONTRAST.search(question) is None:
        return "product_id"
    mentions = _countable_field_mentions(question, family)
    if not mentions:
        raise AggregatePlanParseError(
            "값 존재 여부를 나눠 집계할 대상 필드를 확인할 수 없음"
        )
    non_group_mentions = [mention for mention in mentions if mention.field not in group_by]
    owned_mentions = non_group_mentions or mentions
    distances = [(_distance(match, mention), mention) for mention in owned_mentions]
    shortest = min(distance for distance, _ in distances)
    closest = {
        mention.field
        for distance, mention in distances
        if distance == shortest
    }
    if len(closest) == 1:
        return closest.pop()
    raise AggregatePlanParseError(
        f"count 대상 필드가 둘 이상으로 해석됨: {sorted(closest)}"
    )


def _distance(function_match: re.Match[str], mention: _Mention) -> int:
    if mention.end <= function_match.start():
        return function_match.start() - mention.end
    if function_match.end() <= mention.start:
        return mention.start - function_match.end()
    return 0


def _target_field(
    function: AggregateFunction,
    match: re.Match[str],
    mentions: list[_Mention],
) -> str:
    if function is AggregateFunction.COUNT:
        return "product_id"
    if not mentions:
        raise AggregatePlanParseError(f"{function.value} 집계에 사용할 수치 필드를 확인할 수 없음")
    distances = [(_distance(match, mention), mention) for mention in mentions]
    shortest = min(distance for distance, _ in distances)
    closest_fields = {mention.field for distance, mention in distances if distance == shortest}
    if len(closest_fields) != 1:
        raise AggregatePlanParseError(
            f"{function.value} 대상 수치 필드가 둘 이상으로 해석됨: {sorted(closest_fields)}"
        )
    return closest_fields.pop()


def _coordinated_numeric_fields(
    question: str,
    match: re.Match[str],
    mentions: list[_Mention],
    *,
    lower_bound: int,
) -> list[str]:
    """Bind one trailing aggregate function to an explicit metric list.

    Korean commonly shares one predicate across coordinated nouns, for
    example ``AUM과 총보수 평균``.  Nearest-field binding alone silently
    discards AUM.  Admit multiple owners only when every adjacent field is
    joined by an explicit coordinator and the final field is followed only by
    an optional genitive particle before the function.  Anything less precise
    falls back to the existing single-owner rule.
    """

    local = [
        mention
        for mention in mentions
        if lower_bound <= mention.start and mention.end <= match.start()
    ]
    if len({mention.field for mention in local}) < 2:
        return []
    # Nested/duplicate registry phrases have already been removed by
    # ``_numeric_mentions``.  Require a pure coordinated list so arbitrary
    # prose between two metrics cannot acquire aggregate authority.
    for left, right in zip(local, local[1:], strict=False):
        if re.fullmatch(
            r"\s*(?:은|는|이|가|을|를)?\s*"
            r"(?:와|과|및|하고|이랑|랑|[·ㆍ,/&+])\s*",
            question[left.end : right.start],
            flags=re.IGNORECASE,
        ) is None:
            return []
    if re.fullmatch(
        r"\s*(?:의|에\s*대한)?\s*",
        question[local[-1].end : match.start()],
        flags=re.IGNORECASE,
    ) is None:
        return []
    return list(dict.fromkeys(mention.field for mention in local))


def _coordinated_group_candidates(
    question: str,
    family: ProductFamily,
) -> list[tuple[int, str]]:
    """Resolve at most two categorical labels sharing one ``별/마다``.

    ``투자지역과 위험등급별`` is two axes even though Korean writes the
    grouping suffix only once.  The contract accepts only registry-backed,
    selectable non-numeric fields connected by an explicit coordinator.
    """

    registry = load_field_registry()
    mentions: list[_Mention] = []
    for field_name, base_definition in registry.fields.items():
        if family.value not in base_definition.datasets or field_name in _IDENTITY_GROUP_FIELDS:
            continue
        definition = base_definition.resolve(family.value)
        if (
            not definition.selectable
            or definition.value_type in {ValueType.NUMBER, ValueType.DATE}
        ):
            continue
        phrases = {
            *_field_phrases(field_name, definition),
            *_CURATED_GROUP_PHRASES.get(field_name, ()),
        }
        for phrase in sorted(phrases, key=lambda value: (-len(value), value)):
            for phrase_match in re.finditer(
                _whitespace_tolerant_phrase(phrase),
                question,
                flags=re.IGNORECASE,
            ):
                mentions.append(
                    _Mention(
                        field_name,
                        phrase_match.start(),
                        phrase_match.end(),
                        phrase_match.group(0),
                    )
                )
    owned = [
        mention
        for mention in mentions
        if not any(
            other.field != mention.field
            and other.start <= mention.start
            and mention.end <= other.end
            and (other.end - other.start) > (mention.end - mention.start)
            for other in mentions
        )
    ]
    ordered = sorted(owned, key=lambda item: (item.start, -len(item.phrase), item.field))
    for left, right in zip(ordered, ordered[1:], strict=False):
        if left.field == right.field:
            continue
        if re.fullmatch(
            r"\s*(?:와|과|및|하고|이랑|랑|[·ㆍ,/&+])\s*",
            question[left.end : right.start],
            flags=re.IGNORECASE,
        ) is None:
            continue
        if re.match(
            r"\s*(?:별(?:로)?|마다)(?:\s*(?:나눠|나누|구분|묶))?",
            question[right.end :],
            flags=re.IGNORECASE,
        ) is None:
            continue
        return [(left.start, left.field), (right.start, right.field)]
    return []


def _group_fields(question: str, family: ProductFamily) -> list[str]:
    registry = load_field_registry()
    candidates: list[tuple[int, str]] = []
    distribution_requested = _DISTRIBUTION.search(question) is not None
    explicit_etp_type_grouping = _ETP_TYPE_GROUPING.search(question)
    explicit_fund_geography_grouping = _FUND_GEOGRAPHY_GROUPING.search(question)
    explicit_bond_market_grouping = re.search(
        r"(?:장내\s*(?:와|과|및|[·ㆍ/,])\s*장외|"
        r"장외\s*(?:와|과|및|[·ㆍ/,])\s*장내)"
        r"(?:\s*채권)?(?:으?로)?\s*(?:나눠|나누|구분)|"
        r"장내\s*[·/]\s*장외\s*시장별",
        question,
        flags=re.IGNORECASE,
    )
    explicit_trading_status_grouping = _TRADING_STATUS_CONTRAST.search(question)
    for field_name, base_definition in registry.fields.items():
        if family.value not in base_definition.datasets:
            continue
        definition = base_definition.resolve(family.value)
        if (
            not definition.selectable
            or definition.value_type in {ValueType.NUMBER, ValueType.DATE}
            or field_name in _IDENTITY_GROUP_FIELDS
        ):
            continue
        phrases = {
            *_field_phrases(field_name, definition),
            *_CURATED_GROUP_PHRASES.get(field_name, ()),
        }
        if field_name == "product_type" and explicit_etp_type_grouping is not None:
            candidates.append((explicit_etp_type_grouping.start(), field_name))
            continue
        if (
            field_name == "fund_geography_scope"
            and explicit_fund_geography_grouping is not None
        ):
            candidates.append((explicit_fund_geography_grouping.start(), field_name))
            continue
        if field_name == "bond_market" and explicit_bond_market_grouping is not None:
            candidates.append((explicit_bond_market_grouping.start(), field_name))
            continue
        if field_name == "trading_suspended" and explicit_trading_status_grouping is not None:
            candidates.append((explicit_trading_status_grouping.start(), field_name))
            continue
        for phrase in sorted(phrases, key=lambda value: (-len(value), value)):
            by_match = re.search(
                rf"{re.escape(phrase)}\s*(?:별|마다)(?:로)?"
                r"(?:\s*(?:묶|나눠|구분))?",
                question,
                flags=re.IGNORECASE,
            )
            if by_match:
                candidates.append((by_match.start(), field_name))
                break
            if distribution_requested:
                distribution_match = re.search(
                    rf"{re.escape(phrase)}\s*(?:에\s*따라|기준(?:으로)?)?\s*"
                    r"(?:분포|비중)",
                    question,
                    flags=re.IGNORECASE,
                )
                if distribution_match:
                    candidates.append((distribution_match.start(), field_name))
                    break
    candidates.extend(_coordinated_group_candidates(question, family))
    ordered = list(dict.fromkeys(field for _, field in sorted(candidates)))
    # ``판매 회사 여부`` is the longer and more specific public-fund group
    # key.  Its trailing ``판매 여부`` surface must not create a second,
    # unrelated product-level group.
    if "company_sellable" in ordered and "sellable" in ordered:
        ordered.remove("sellable")
    if distribution_requested and not ordered:
        raise AggregatePlanParseError("분포·비중 집계에 사용할 그룹 기준을 확인할 수 없음")
    if len(ordered) > 2:
        raise AggregatePlanParseError("그룹 기준은 한 번에 최대 두 개까지 지원")
    return ordered


def _remove_group_surface_false_filters(
    question: str,
    constraints: list[dict[str, Any]],
    group_by: list[str],
) -> list[dict[str, Any]]:
    """Do not interpret the label of a status group as a row predicate.

    The lexical filter linker correctly treats a bare ``거래 정지`` as a
    suspended-only filter.  In ``거래 정지 여부별`` that same text belongs to
    the aggregate group grammar instead.  This narrowly-scoped ownership rule
    prevents a boolean group from collapsing to a single value while preserving
    ordinary explicit status filters in every non-grouped query.
    """

    group_surface: dict[str, re.Pattern[str]] = {
        "product_type": _ETP_TYPE_GROUPING,
        "fund_geography_scope": _FUND_GEOGRAPHY_GROUPING,
        "trading_suspended": re.compile(
            r"거래\s*(?:정지|중지)\s*여부\s*별|"
            r"거래\s*상태\s*별|"
            rf"{_TRADING_STATUS_CONTRAST.pattern}",
            re.IGNORECASE,
        ),
        "sellable": re.compile(
            r"판매\s*(?:여부|상태)\s*별",
            re.IGNORECASE,
        ),
        "company_sellable": re.compile(
            r"(?:당사|미래에셋(?:증권)?)\s*판매\s*(?:가능\s*)?여부\s*별|"
            r"판매\s*(?:회사|사)\s*(?:가능\s*)?여부\s*별",
            re.IGNORECASE,
        ),
    }
    owned_fields = {
        field
        for field in group_by
        if field in group_surface and group_surface[field].search(question)
    }
    if not owned_fields:
        return constraints
    return [item for item in constraints if item.get("field") not in owned_fields]


def _remove_implicit_fund_dataset_scope(
    question: str,
    constraints: list[dict[str, Any]],
    family: ProductFamily,
) -> list[dict[str, Any]]:
    """Keep the fund table name separate from an explicit offering filter.

    The organizer names PRFD01N001 the public-fund master, so ``공모펀드`` in a
    family request identifies that supplied dataset.  It must not silently narrow an
    aggregate to rows whose independent ``public_offering`` attribute is true.  A user
    can still request that attribute explicitly with wording such as ``공모형`` or
    ``모집구분이 공모``.
    """

    if family is not ProductFamily.FUND or _EXPLICIT_FUND_PUBLIC_OFFERING.search(question):
        return constraints
    return [item for item in constraints if item.get("field") != "public_offering"]


def _remove_contrastive_availability_filters(
    question: str,
    constraints: list[dict[str, Any]],
    aggregation_items: list[dict[str, str]],
) -> list[dict[str, Any]]:
    """Keep both sides of an explicitly requested availability comparison.

    The lexical linker intentionally creates ``is_present``/``is_missing``
    filters for one-sided search requests.  In an aggregate asking for both
    states, either filter would delete the opposite state before counting it.
    The COUNT(field) metric already carries ``valid_count`` and
    ``missing_count``, so only that field's contrastive filter is removed.
    """

    if _AVAILABILITY_CONTRAST.search(question) is None:
        return constraints
    count_fields = {
        item["field"]
        for item in aggregation_items
        if item["function"] == AggregateFunction.COUNT.value
        and item["field"] != "product_id"
    }
    return [
        item
        for item in constraints
        if not (
            item.get("field") in count_fields
            and item.get("operator") in {"is_present", "is_missing"}
        )
    ]


def compile_aggregate_plan(
    *,
    question: str,
    question_id: str,
    family: ProductFamily,
    base_payload: dict[str, Any],
    requested_limit: int | None,
) -> QueryPlan:
    mentions = _numeric_mentions(question, family)
    requested: list[tuple[int, AggregateFunction, re.Match[str]]] = []
    for function, pattern in _FUNCTION_PATTERNS.items():
        for match in pattern.finditer(question):
            if function is AggregateFunction.SUM and any(
                context.start() <= match.start() < context.end()
                for context in _COUNT_CONTEXT.finditer(question)
            ):
                continue
            requested.append((match.start(), function, match))
    group_by = _group_fields(question, family)
    if _DISTRIBUTION.search(question) and not any(
        function is AggregateFunction.COUNT for _, function, _ in requested
    ):
        synthetic = _DISTRIBUTION.search(question)
        assert synthetic is not None
        requested.append((synthetic.start(), AggregateFunction.COUNT, synthetic))
    if (
        group_by
        and not requested
        and (synthetic := _GROUPED_COUNT_CONTEXT.search(question)) is not None
    ):
        requested.append((synthetic.start(), AggregateFunction.COUNT, synthetic))
    if not requested:
        raise AggregatePlanParseError(
            "집계 함수가 명확하지 않음; 개수·평균·중앙값·합계·최솟값·최댓값 중 하나가 필요"
        )

    aggregation_items: list[dict[str, str]] = []
    ordered_requests = sorted(requested, key=lambda item: item[0])
    previous_function_end = 0
    for _, function, match in ordered_requests:
        if function is AggregateFunction.COUNT:
            fields = [_count_target_field(question, match, family, group_by)]
        else:
            fields = _coordinated_numeric_fields(
                question,
                match,
                mentions,
                lower_bound=previous_function_end,
            ) or [_target_field(function, match, mentions)]
        for field in fields:
            item = {"function": function.value, "field": field}
            if item not in aggregation_items:
                aggregation_items.append(item)
        previous_function_end = max(previous_function_end, match.end())

    projection = list(
        dict.fromkeys(
            [
                "product_id",
                *group_by,
                *(item["field"] for item in aggregation_items),
            ]
        )
    )
    amount_requested = any(
        item["function"] != AggregateFunction.COUNT.value
        and load_field_registry().require_field(item["field"], [family.value]).unit
        == "source_currency_amount"
        for item in aggregation_items
    )
    constraints = _remove_group_surface_false_filters(
        question,
        list(base_payload["constraints"]),
        group_by,
    )
    constraints = _remove_implicit_fund_dataset_scope(question, constraints, family)
    constraints = _remove_contrastive_availability_filters(
        question,
        constraints,
        aggregation_items,
    )
    has_currency_scope = any(item["field"] == "trading_currency" for item in constraints)
    if (
        amount_requested
        and "trading_currency" not in group_by
        and not has_currency_scope
        and family in {ProductFamily.OVERSEAS_ETP, ProductFamily.DOMESTIC_ETP}
    ):
        constraints.append(
            {
                "field": "trading_currency",
                "operator": "eq",
                "value": "USD" if family is ProductFamily.OVERSEAS_ETP else "KRW",
                "unit": "code",
                "strength": "locked",
            }
        )

    payload = {
        **base_payload,
        "question_id": question_id,
        "intent": "aggregate",
        "product_families": [family.value],
        "constraints": constraints,
        "ranking": [],
        "projection": projection,
        "limit": requested_limit or (100 if group_by else 1),
        "intent_payload": {
            "comparison_fields": [],
            "group_by": group_by,
            "aggregations": aggregation_items,
            "explain_product_ids": [],
        },
    }
    if group_by and _GROUP_LABEL_ORDER.search(question) is not None:
        payload["ambiguities"] = [
            item
            for item in base_payload["ambiguities"]
            if str(item.get("span", "")).strip().casefold()
            not in {"오름차순", "가나다순", "알파벳순"}
        ]
    if "trading_currency" in group_by:
        payload["ambiguities"] = [
            item for item in payload["ambiguities"] if item["span"] != "AUM 비교 통화"
        ]
    return QueryPlan.model_validate(payload)
