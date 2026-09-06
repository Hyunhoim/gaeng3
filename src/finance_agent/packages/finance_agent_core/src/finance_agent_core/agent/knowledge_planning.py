from __future__ import annotations

import calendar
import re
from datetime import date, timedelta

from finance_agent_core.agent.document_reference import (
    extract_dated_official_document_references,
)
from finance_agent_core.agent.intent_analysis import (
    IntentAnalysisDraft,
    external_frame_supports_requirement,
    holding_document_top_n_matches,
    holding_document_top_n_scope,
    knowledge_requirement_signal_matches,
    question_has_knowledge_capability_signal,
)
from finance_agent_core.agent.product_family_language import (
    explicit_etp_market_families,
    has_standalone_bond_product_scope,
)
from finance_agent_core.agent.source_selection import (
    GeneralizedOfficialSourceSelector,
    SourceSelectionSignals,
)
from finance_agent_core.config.knowledge_coverage import KnowledgeCapability
from finance_agent_core.config.source_registry import (
    OfficialSourceRegistry,
    load_official_source_registry,
)
from finance_agent_core.contracts.external_knowledge import (
    CompositeKnowledgeQueryPlan,
    KnowledgeDocumentTopNScope,
    KnowledgeEvidenceSelection,
    KnowledgeMatchMode,
    KnowledgeProductSelection,
    KnowledgeRequirement,
    KnowledgeTimeRange,
    predicate_for_capability,
)
from finance_agent_core.contracts.queryplan import ProductFamily, SortDirection
from finance_agent_core.contracts.routing import InteractionIntent


class CompositeKnowledgePlanningError(RuntimeError):
    """Raised when HCX evidence cannot be compiled into a server-owned plan."""


class EvidencePeriodAfterCutoffError(CompositeKnowledgePlanningError):
    """The question names a period that starts after the approved evidence cutoff."""

    reason_code = "evidence_period_after_cutoff"


_RECENT_MONTHS = re.compile(r"최근\s*(\d{1,2})\s*개월", re.IGNORECASE)
_RECENT_DAYS = re.compile(r"(?:최근|지난)\s*(\d{1,3})\s*일", re.IGNORECASE)
_RECENT_WEEKS = re.compile(r"(?:최근|지난)\s*(\d{1,2})\s*주", re.IGNORECASE)
_RECENT_QUARTERS = re.compile(
    r"(?:최근|지난)\s*(\d{1,2}|한|두|세|네)\s*분기", re.IGNORECASE
)
_RECENT_HALF_YEAR = re.compile(r"(?:최근|지난)\s*(?:반년|반\s*년)", re.IGNORECASE)
_RECENT_WORD_MONTHS = re.compile(
    r"(?:최근|지난)\s*(한|두|석|세|넉|네|여섯|열두)\s*(?:달|개월)",
    re.IGNORECASE,
)
_ANCHORED_BACK_MONTHS = re.compile(
    r"(?P<year>20\d{2})\s*년\s*(?P<month>\d{1,2})\s*월\s*"
    r"(?P<day>\d{1,2})\s*일\s*"
    r"(?:(?:에서|부터)|(?:을|를)?\s*기준(?:으로|으로부터)?)?\s*"
    r"(?P<count>\d{1,2}|한|두|석|세|넉|네|여섯|열두)\s*"
    r"(?:개월|달)(?:을|를)?\s*"
    r"(?:거슬러\s*올라간|거슬러\s*간|전(?:부터|까지)?(?:의)?)"
    r"(?:\s*기간)?",
    re.IGNORECASE,
)
_KOREAN_MONTH_COUNTS = {
    "한": 1,
    "두": 2,
    "석": 3,
    "세": 3,
    "넉": 4,
    "네": 4,
    "여섯": 6,
    "열두": 12,
}
_SINCE_MONTH = re.compile(
    r"(?:(20\d{2})\s*년\s*)?(?:(올해)\s*)?(\d{1,2})\s*월\s*(?:이후|부터)",
    re.IGNORECASE,
)
_EXPLICIT_MONTH_RANGE = re.compile(
    r"(20\d{2})\s*년\s*(\d{1,2})\s*[~～-]\s*(\d{1,2})\s*월",
    re.IGNORECASE,
)
_EXPLICIT_FROM_TO_MONTH_RANGE = re.compile(
    r"(20\d{2})\s*년\s*(\d{1,2})\s*월?\s*부터\s*"
    r"(?:(20\d{2})\s*년\s*)?(\d{1,2})\s*월\s*까지",
    re.IGNORECASE,
)
_EXPLICIT_FROM_TO_DAY_RANGE = re.compile(
    r"(?P<start_year>20\d{2})\s*(?:년\s*|[-/.])"
    r"(?P<start_month>\d{1,2})\s*(?:월\s*|[-/.])"
    r"(?P<start_day>\d{1,2})\s*일?\s*부터\s*"
    r"(?:(?P<end_year>20\d{2})\s*(?:년\s*|[-/.]))?"
    r"(?P<end_month>\d{1,2})\s*(?:월\s*|[-/.])"
    r"(?P<end_day>\d{1,2})\s*일?\s*까지",
    re.IGNORECASE,
)
_EXPLICIT_MONTH = re.compile(
    r"(20\d{2})\s*년\s*(\d{1,2})\s*월(?:\s*말)?",
    re.IGNORECASE,
)
_KNOWLEDGE_TIME_PATTERNS: tuple[re.Pattern[str], ...] = (
    _ANCHORED_BACK_MONTHS,
    _RECENT_MONTHS,
    _RECENT_DAYS,
    _RECENT_WEEKS,
    _RECENT_QUARTERS,
    _RECENT_HALF_YEAR,
    _RECENT_WORD_MONTHS,
    _EXPLICIT_FROM_TO_DAY_RANGE,
    _EXPLICIT_FROM_TO_MONTH_RANGE,
    _EXPLICIT_MONTH_RANGE,
    _SINCE_MONTH,
    _EXPLICIT_MONTH,
)
_LIMIT = re.compile(
    r"(?:TOP\s*(?P<top>\d{1,2})|(?P<count>\d{1,2})\s*(?:개|건)(?:씩)?|"
    r"(?P<word>한|두|세|네|다섯|여섯|일곱|여덟|아홉|열)\s*(?:개|건)(?:씩)?)",
    re.IGNORECASE,
)
# A document-internal Top-N is evidence scope, not the number of products the
# user wants returned.  Parsing is centralized in ``intent_analysis`` so the
# Planner, coverage gate and Runtime cannot disagree about the same phrase.
_KOREAN_LIMITS = {
    "한": 1,
    "두": 2,
    "세": 3,
    "네": 4,
    "다섯": 5,
    "여섯": 6,
    "일곱": 7,
    "여덟": 8,
    "아홉": 9,
    "열": 10,
}
_NET_ASSET_TOTAL = re.compile(r"순\s*자산\s*총액", re.IGNORECASE)
_AUM = re.compile(r"AUM|순\s*자산(?!\s*총액)|운용\s*자산", re.IGNORECASE)
_TOTAL_EXPENSE_RATIO = re.compile(
    r"총\s*보수(?:율)?|연간\s*보수(?:율)?|expense\s*ratio|\bfee\b",
    re.IGNORECASE,
)
_ONE_DAY_RETURN = re.compile(
    r"(?:1\s*일|하루|일간|one[ -]?day)\s*(?:수익률|성과|실적|return)",
    re.IGNORECASE,
)
_ONE_MONTH_RETURN = re.compile(
    r"(?:1\s*개월|한\s*달|월간|one[ -]?month|1M)\s*(?:수익률|성과|실적|return)",
    re.IGNORECASE,
)
_THREE_MONTH_RETURN = re.compile(
    r"(?:3\s*개월|석\s*달|three[ -]?month|3M)\s*(?:수익률|성과|실적|return)",
    re.IGNORECASE,
)
_ONE_YEAR_RETURN = re.compile(
    r"(?:최근\s*)?(?:1\s*년|일\s*년|12\s*개월|열두\s*달|one[ -]?year|1Y)\s*"
    r"(?:수익률|성과|실적|return)",
    re.IGNORECASE,
)
_DAILY_TRADING_VALUE = re.compile(
    r"(?:일(?:간)?\s*)?거래\s*대금|daily\s*trading\s*value|turnover",
    re.IGNORECASE,
)
_DAILY_VOLUME = re.compile(
    r"(?:일(?:간)?\s*)?거래량|daily\s*(?:trading\s*)?volume",
    re.IGNORECASE,
)
_TWENTY_DAY_VOLATILITY = re.compile(
    r"(?:20\s*(?:거래)?일|1\s*개월|한\s*달)\s*(?:연환산\s*)?(?:변동성|흔들림)|"
    r"20[ -]?day\s*volatility",
    re.IGNORECASE,
)
_TRACKING_ERROR = re.compile(r"추적\s*오차(?:율)?|tracking\s*error", re.IGNORECASE)
_ASCENDING = re.compile(
    r"낮(?:은|게)|적(?:은|게)|작(?:은|게)|오름차순|하위|최저|"
    r"BOTTOM|ascending|lowest|smallest",
    re.IGNORECASE,
)
_DESCENDING = re.compile(
    r"큰|높(?:은|게)|많(?:은|게)|상위|TOP|내림차순|최고|"
    r"descending|highest|largest|most",
    re.IGNORECASE,
)
_GOOD_PERFORMANCE_DESCENDING = re.compile(
    r"(?:수익률|성과|실적)(?:이|가)?\s*좋(?:은|게)\s*(?:순|상위|TOP)",
    re.IGNORECASE,
)
_EVIDENCE_DATE_ORDERING = re.compile(
    r"(?P<span>(?P<scope>"
    r"(?:(?:테마|섹터|산업)(?:\s*(?:연결|관계|관련))?|"
    r"구성\s*종목|편입|보유|위험\s*요인|리스크\s*요인|"
    r"펀드\s*(?:구조|전략|동향))?\s*"
    r"(?:공식\s*)?(?:근거|자료|문서|공시|내역|편입표|출처|reference))"
    r"(?:의|가|은|는|을|를)?\s*(?:중\s*)?"
    r"(?:(?P<date_kind>발행|게시|공개|작성|기준)\s*일(?:자)?"
    r"(?:이|가|은|는)?\s*)?"
    r"(?P<direction>최신|최근|새로운|오래된|예전)(?:인|한)?\s*"
    r"(?:순(?:서)?|것부터))",
    re.IGNORECASE,
)
_PERFORMANCE_RANKING = re.compile(
    r"(?:수익률|성과|실적)\s*(?:이\s*)?(?:높은\s*)?(?:순|상위|TOP)|"
    r"(?:순|상위|TOP).{0,8}(?:수익률|성과|실적)",
    re.IGNORECASE,
)
_RANK_FIELD_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("net_asset_total", _NET_ASSET_TOTAL),
    ("aum", _AUM),
    ("total_expense_ratio_pct", _TOTAL_EXPENSE_RATIO),
    ("tracking_error_pct", _TRACKING_ERROR),
    ("one_day_return_pct", _ONE_DAY_RETURN),
    ("one_month_return_pct", _ONE_MONTH_RETURN),
    ("three_month_return_pct", _THREE_MONTH_RETURN),
    ("one_year_return_pct", _ONE_YEAR_RETURN),
    ("daily_volume", _DAILY_VOLUME),
    ("daily_trading_value", _DAILY_TRADING_VALUE),
    ("twenty_day_volatility_pct", _TWENTY_DAY_VOLATILITY),
)
_ANY_HOLDING_MATCH = re.compile(
    r"(?:중\s*)?(?:하나|한\s*개)\s*이상|적어도\s*(?:하나|한\s*개)|"
    r"어느\s*(?:하나|한\s*개)|\s(?:또는|혹은)\s|"
    r"(?<=[0-9A-Za-z가-힣])(?:이나|나)\s+(?=[0-9A-Za-z가-힣])|"
    r"\b(?:either|or|any\s+of|at\s+least\s+one)\b",
    re.IGNORECASE,
)
_EXPLICIT_ALL_HOLDING_MATCH = re.compile(
    r"모두|전부|동시에|둘\s*다|\b(?:both|all)\b",
    re.IGNORECASE,
)
_SUBJECTIVE_THEME = re.compile(r"테마로\s*(?:볼|분류할)\s*수\s*있는", re.IGNORECASE)
_REQUESTED_PRODUCT_FIELD_PATTERNS: dict[str, re.Pattern[str]] = {
    "aum": _AUM,
    "daily_trading_value": _DAILY_TRADING_VALUE,
    "daily_volume": _DAILY_VOLUME,
    "investment_region": re.compile(r"투자\s*지역|지역", re.IGNORECASE),
    "manager": re.compile(r"운용사|자산\s*운용사|발행사", re.IGNORECASE),
    "net_asset_total": _NET_ASSET_TOTAL,
    "one_day_return_pct": _ONE_DAY_RETURN,
    "one_month_return_pct": _ONE_MONTH_RETURN,
    "one_year_return_pct": _ONE_YEAR_RETURN,
    "product_name": re.compile(r"상품명|상품\s*이름|상품\s*정보", re.IGNORECASE),
    "risk_level": re.compile(r"위험\s*등급", re.IGNORECASE),
    "three_month_return_pct": _THREE_MONTH_RETURN,
    "ticker": re.compile(r"티커|ticker", re.IGNORECASE),
    "total_expense_ratio_pct": _TOTAL_EXPENSE_RATIO,
    "tracking_error_pct": _TRACKING_ERROR,
    "twenty_day_volatility_pct": _TWENTY_DAY_VOLATILITY,
}
_CORPORATE_PAIR = re.compile(
    r"(?P<parent>[0-9A-Za-z가-힣&·+._/-]{2,80}?)\s*"
    r"(?:의\s*)?(?:자회사|계열사|가족사)인\s*"
    r"(?P<child>[0-9A-Za-z가-힣&·+._/-]{2,80}?)"
    r"(?=$|(?:이|가|을|를|은|는)?\s*(?:편입|보유|담|넣|포함|들어|ETF|ETN|ETP))",
    re.IGNORECASE,
)
_CORPORATE_SELECTION = re.compile(
    r"(?P<parent>[0-9A-Za-z가-힣&·+._/-]{2,80}?)\s*(?:의\s*)?"
    r"(?:자회사|계열사|가족사)\s*(?:가운데|중(?:에서)?)\s*"
    r"(?P<child>[0-9A-Za-z가-힣&·+._/-]{2,80}?)"
    r"(?=(?:이|가|을|를)?\s*(?:편입|보유|담|넣|포함|들어))",
    re.IGNORECASE,
)
_CORPORATE_DESCENDANT = re.compile(
    r"자회사|계열사|가족사|그룹사|"
    r"(?:계열|관계|소속|산하)\s*(?:기업|회사|법인)",
    re.IGNORECASE,
)
_EXPLICIT_ENTITY_LIST = re.compile(
    r"(?<=[0-9A-Za-z가-힣])(?:와|과)\s+|\s(?:및|또는|혹은)\s|[,·ㆍ]",
    re.IGNORECASE,
)
_GENERIC_HOLDING_SUBJECT = re.compile(
    r"^(?:(?:이|그|해당|각|두|세|여러|위|아래|앞선|전술한)\s*)?"
    r"(?:종목|기업|회사|상품|구성\s*종목)(?:들)?(?:의|이|가|을|를|은|는)?$|"
    r"^(?:이들|그들|전부|모두|양쪽|둘)(?:의|이|가|을|를|은|는)?$",
    re.IGNORECASE,
)
_HOLDING_PROJECTION_RELATION = re.compile(
    r"^(?:편입|보유|구성\s*종목)\s*(?:비중|기준일|날짜|시점|순위|정보)$",
    re.IGNORECASE,
)
_CAPABILITY_ORDER = {
    KnowledgeCapability.CORPORATE_SUBSIDIARY: 0,
    KnowledgeCapability.PORTFOLIO_HOLDINGS: 1,
    KnowledgeCapability.SECTOR_THEME_HISTORY: 2,
    KnowledgeCapability.FUND_STRUCTURE_STRATEGY_TREND: 3,
    KnowledgeCapability.PRODUCT_RISK_FACTORS: 4,
}
_REQUIREMENT_FAMILY_CUES: dict[ProductFamily, re.Pattern[str]] = {
    ProductFamily.FUND: re.compile(
        r"(?<!상장지수)(?<!상장 지수)(?:공모\s*)?펀드",
        re.IGNORECASE,
    ),
}


def _requirement_families(
    subject: str,
    relation: str,
    global_families: tuple[ProductFamily, ...],
) -> tuple[ProductFamily, ...]:
    """Bind an external relation to its explicit local product-family clause."""

    surface = f"{subject} {relation}"
    scoped = tuple(
        sorted(
            {
                *explicit_etp_market_families(surface),
                *((ProductFamily.BOND,) if has_standalone_bond_product_scope(surface) else ()),
                *(
                    family
                    for family, cue in _REQUIREMENT_FAMILY_CUES.items()
                    if cue.search(surface)
                ),
            },
            key=lambda item: item.value,
        )
    )
    if scoped:
        allowed = set(global_families)
        return tuple(
            sorted(
                (family for family in scoped if not allowed or family in allowed),
                key=lambda item: item.value,
            )
        )
    if re.search(
        r"(?<![A-Za-z])(?:ETF|ETN|ETP)(?![A-Za-z])|상장\s*지수\s*(?:상품|펀드)",
        surface,
        re.IGNORECASE,
    ):
        etp_families = {
            ProductFamily.DOMESTIC_ETP,
            ProductFamily.OVERSEAS_ETP,
        }
        selected = etp_families.intersection(global_families)
        if selected:
            return tuple(sorted(selected, key=lambda item: item.value))
    return global_families


def _subtract_months(value: date, months: int) -> date:
    year = value.year
    month = value.month - months
    while month <= 0:
        month += 12
        year -= 1
    day = min(value.day, calendar.monthrange(year, month)[1])
    return date(year, month, day)


def knowledge_time_evidence_spans(question: str) -> tuple[str, ...]:
    """Return the literal span owned by an external-evidence time range.

    The ordering intentionally mirrors ``_knowledge_time_range``.  The caller
    decides whether the parsed range belongs to an external requirement; this
    helper only preserves the exact user text so the DB prefilter cannot
    reinterpret ``최근 6개월`` as a six-month product-return condition.
    """

    document_references = extract_dated_official_document_references(question)
    if len(document_references) > 1:
        return tuple(reference.time_evidence_span for reference in document_references)
    for pattern in _KNOWLEDGE_TIME_PATTERNS:
        if match := pattern.search(question):
            return (match.group(0),)
    return ()


def _knowledge_time_range(question: str, cutoff: date) -> KnowledgeTimeRange | None:
    """Normalize bounded temporal language against the signed evidence cutoff."""

    if anchored := _ANCHORED_BACK_MONTHS.search(question):
        raw_count = anchored.group("count")
        months = int(raw_count) if raw_count.isdigit() else _KOREAN_MONTH_COUNTS[raw_count]
        if not 1 <= months <= 60:
            raise CompositeKnowledgePlanningError("anchored month range is out of bounds")
        try:
            end = date(
                int(anchored.group("year")),
                int(anchored.group("month")),
                int(anchored.group("day")),
            )
        except ValueError as error:
            raise CompositeKnowledgePlanningError(
                "anchored evidence date is invalid"
            ) from error
        start = _subtract_months(end, months)
        if start > cutoff:
            raise EvidencePeriodAfterCutoffError(
                "requested evidence period starts after the approved evidence cutoff"
            )
        return KnowledgeTimeRange(start=start, end=min(end, cutoff))

    if recent := _RECENT_MONTHS.search(question):
        months = int(recent.group(1))
        if not 1 <= months <= 60:
            raise CompositeKnowledgePlanningError("recent month range is out of bounds")
        return KnowledgeTimeRange(start=_subtract_months(cutoff, months), end=cutoff)
    if recent := _RECENT_DAYS.search(question):
        days = int(recent.group(1))
        if not 1 <= days <= 1_825:
            raise CompositeKnowledgePlanningError("recent day range is out of bounds")
        return KnowledgeTimeRange(start=cutoff - timedelta(days=days), end=cutoff)
    if recent := _RECENT_WEEKS.search(question):
        weeks = int(recent.group(1))
        if not 1 <= weeks <= 260:
            raise CompositeKnowledgePlanningError("recent week range is out of bounds")
        return KnowledgeTimeRange(start=cutoff - timedelta(weeks=weeks), end=cutoff)
    if recent := _RECENT_QUARTERS.search(question):
        raw = recent.group(1)
        count = int(raw) if raw.isdigit() else {"한": 1, "두": 2, "세": 3, "네": 4}[raw]
        if not 1 <= count <= 20:
            raise CompositeKnowledgePlanningError("recent quarter range is out of bounds")
        return KnowledgeTimeRange(start=_subtract_months(cutoff, count * 3), end=cutoff)
    if _RECENT_HALF_YEAR.search(question):
        return KnowledgeTimeRange(start=_subtract_months(cutoff, 6), end=cutoff)
    if recent := _RECENT_WORD_MONTHS.search(question):
        months = _KOREAN_MONTH_COUNTS[recent.group(1)]
        return KnowledgeTimeRange(start=_subtract_months(cutoff, months), end=cutoff)

    day_range = _EXPLICIT_FROM_TO_DAY_RANGE.search(question)
    from_to_range = _EXPLICIT_FROM_TO_MONTH_RANGE.search(question)
    explicit_range = _EXPLICIT_MONTH_RANGE.search(question)
    explicit_month = _EXPLICIT_MONTH.search(question)
    if day_range is not None:
        start_year = int(day_range.group("start_year"))
        end_year = int(day_range.group("end_year") or start_year)
        try:
            start = date(
                start_year,
                int(day_range.group("start_month")),
                int(day_range.group("start_day")),
            )
            end = date(
                end_year,
                int(day_range.group("end_month")),
                int(day_range.group("end_day")),
            )
        except ValueError as error:
            raise CompositeKnowledgePlanningError(
                "explicit evidence date range is invalid"
            ) from error
        if start > end:
            raise CompositeKnowledgePlanningError("explicit evidence date range is reversed")
        result = KnowledgeTimeRange(start=start, end=end)
    elif from_to_range is not None:
        start_year = int(from_to_range.group(1))
        start_month = int(from_to_range.group(2))
        end_year = int(from_to_range.group(3) or start_year)
        end_month = int(from_to_range.group(4))
        if not 1 <= start_month <= 12 or not 1 <= end_month <= 12:
            raise CompositeKnowledgePlanningError("explicit evidence month range is invalid")
        start = date(start_year, start_month, 1)
        end = date(end_year, end_month, calendar.monthrange(end_year, end_month)[1])
        if start > end:
            raise CompositeKnowledgePlanningError("explicit evidence month range is reversed")
        result = KnowledgeTimeRange(start=start, end=end)
    elif explicit_range is not None:
        year = int(explicit_range.group(1))
        start_month = int(explicit_range.group(2))
        end_month = int(explicit_range.group(3))
        if not 1 <= start_month <= end_month <= 12:
            raise CompositeKnowledgePlanningError("explicit evidence month range is invalid")
        result = KnowledgeTimeRange(
            start=date(year, start_month, 1),
            end=date(year, end_month, calendar.monthrange(year, end_month)[1]),
        )
    elif since := _SINCE_MONTH.search(question):
        year = int(since.group(1)) if since.group(1) else cutoff.year
        month = int(since.group(3))
        if not 1 <= month <= 12:
            raise CompositeKnowledgePlanningError("evidence start month is invalid")
        start = date(year, month, 1)
        if start > cutoff:
            raise CompositeKnowledgePlanningError("evidence start is after the cutoff")
        result = KnowledgeTimeRange(start=start, end=cutoff)
    elif explicit_month is not None:
        year = int(explicit_month.group(1))
        month = int(explicit_month.group(2))
        if not 1 <= month <= 12:
            raise CompositeKnowledgePlanningError("explicit evidence month is invalid")
        result = KnowledgeTimeRange(
            start=date(year, month, 1),
            end=date(year, month, calendar.monthrange(year, month)[1]),
        )
    else:
        return None
    if result.start is not None and result.start > cutoff:
        # "2026년 9월 1일 기준 …" asked after the signed evidence cutoff
        # (2026-08-24).  No approved evidence can exist for that period, so
        # say so instead of clamping into a reversed range (HTTP 500).
        raise EvidencePeriodAfterCutoffError(
            "requested evidence period starts after the approved evidence cutoff"
        )
    if result.end > cutoff:
        return KnowledgeTimeRange(start=result.start, end=cutoff)
    return result


def _span_distance(left: tuple[int, int], right: tuple[int, int]) -> int:
    if left[1] < right[0]:
        return right[0] - left[1]
    if right[1] < left[0]:
        return left[0] - right[1]
    return 0


def _ranking_choice(question: str) -> tuple[str, SortDirection] | None:
    """Bind one explicit direction token to its nearest supported DB metric."""

    metrics = [
        (field, match.span())
        for field, pattern in _RANK_FIELD_PATTERNS
        for match in pattern.finditer(question)
    ]
    directions = [(SortDirection.ASC, match.span()) for match in _ASCENDING.finditer(question)] + [
        (SortDirection.DESC, match.span()) for match in _DESCENDING.finditer(question)
    ] + [
        (SortDirection.DESC, match.span())
        for match in _GOOD_PERFORMANCE_DESCENDING.finditer(question)
    ]
    choices: set[tuple[str, SortDirection]] = set()
    for direction, direction_span in directions:
        distances = [
            (_span_distance(metric_span, direction_span), field, metric_span)
            for field, metric_span in metrics
        ]
        if not distances:
            continue
        nearest_distance = min(item[0] for item in distances)
        if nearest_distance > 32:
            continue
        nearest = [item for item in distances if item[0] == nearest_distance]
        if len({item[1] for item in nearest}) != 1:
            raise CompositeKnowledgePlanningError(
                "external ranking direction is ambiguous between product metrics"
            )
        field = nearest[0][1]
        between_start = min(nearest[0][2][1], direction_span[1])
        between_end = max(nearest[0][2][0], direction_span[0])
        if re.search(r"[.!?。！？]", question[between_start:between_end]):
            continue
        choices.add((field, direction))
    if not choices:
        return None
    if len(choices) != 1:
        raise CompositeKnowledgePlanningError(
            "external product selection contains multiple or conflicting rankings"
        )
    return next(iter(choices))


def _family_aware_ranking_field(
    field: str,
    families: tuple[ProductFamily, ...],
) -> str:
    """Resolve a shared Korean label without collapsing distinct source facts.

    ``순자산총액`` is the official ``pd_net_tamt`` field for domestic ETPs,
    while the same surface remains the existing recent-AUM concept for the
    other supported product families.  A single cross-family ordering cannot
    honestly use both measures, so keep that request fail-closed.
    """

    if field == "tracking_error_pct":
        if set(families) != {ProductFamily.DOMESTIC_ETP}:
            raise CompositeKnowledgePlanningError(
                "external tracking-error ranking requires domestic ETP only"
            )
        return field
    if field != "net_asset_total":
        return field
    family_set = set(families)
    if family_set == {ProductFamily.DOMESTIC_ETP}:
        return field
    if ProductFamily.DOMESTIC_ETP in family_set:
        raise CompositeKnowledgePlanningError(
            "external net-asset-total ranking requires one compatible product family"
        )
    return "aum"


def _external_product_result_limit(question: str) -> int:
    """Return only a final product count, excluding evidence-table sizes."""

    document_scopes = holding_document_top_n_matches(question)
    requested = [
        (
            int(match.group("top") or match.group("count"))
            if match.group("top") or match.group("count")
            else _KOREAN_LIMITS[match.group("word")]
        )
        for match in _LIMIT.finditer(question)
        if not any(
            scope[0] <= match.start() and match.end() <= scope[1]
            for scope in document_scopes
        )
    ]
    return requested[-1] if requested else 1


def question_binds_external_ranking(question: str) -> bool:
    """Report whether a direction word is bound to a supported product metric.

    A conflicting binding also counts: the plan compiler raises its own CLARIFY
    for that case, so the coverage boundary does not need to repeat it.
    """

    try:
        return _ranking_choice(question) is not None
    except CompositeKnowledgePlanningError:
        return True


def _evidence_ordering_capability(scope: str) -> KnowledgeCapability | None:
    if re.search(r"테마|섹터|산업", scope, re.IGNORECASE):
        return KnowledgeCapability.SECTOR_THEME_HISTORY
    if re.search(r"구성\s*종목|편입|보유", scope, re.IGNORECASE):
        return KnowledgeCapability.PORTFOLIO_HOLDINGS
    if re.search(r"위험\s*요인|리스크\s*요인", scope, re.IGNORECASE):
        return KnowledgeCapability.PRODUCT_RISK_FACTORS
    if re.search(r"펀드\s*(?:구조|전략|동향)", scope, re.IGNORECASE):
        return KnowledgeCapability.FUND_STRUCTURE_STRATEGY_TREND
    return None


def _compile_evidence_date_selection(
    question: str,
    indexed_requirements: tuple[
        tuple[str, KnowledgeCapability, bool],
        ...,
    ],
) -> KnowledgeEvidenceSelection | None:
    matches = tuple(_EVIDENCE_DATE_ORDERING.finditer(question))
    if not matches:
        return None
    if len(matches) != 1:
        raise CompositeKnowledgePlanningError(
            "multiple evidence-date orderings require clarification"
        )
    match = matches[0]
    explicit_capability = _evidence_ordering_capability(match.group("scope"))
    candidates = tuple(
        requirement_id
        for requirement_id, capability, independent in indexed_requirements
        if independent
        and capability
        in {
            KnowledgeCapability.PORTFOLIO_HOLDINGS,
            KnowledgeCapability.SECTOR_THEME_HISTORY,
            KnowledgeCapability.FUND_STRUCTURE_STRATEGY_TREND,
            KnowledgeCapability.PRODUCT_RISK_FACTORS,
        }
        and (explicit_capability is None or capability is explicit_capability)
    )
    if len(candidates) != 1:
        raise CompositeKnowledgePlanningError(
            "evidence-date ordering must bind exactly one external requirement"
        )
    direction = (
        SortDirection.ASC
        if match.group("direction").casefold() in {"오래된", "예전"}
        else SortDirection.DESC
    )
    return KnowledgeEvidenceSelection(
        order_by=(
            "as_of"
            if (match.group("date_kind") or "").casefold() == "기준"
            else "published_at"
        ),
        direction=direction,
        limit=_external_product_result_limit(question),
        source_requirement_id=candidates[0],
        selection_evidence_span=match.group("span"),
    )


def compile_evidence_date_selection(
    question: str,
    intent: IntentAnalysisDraft,
) -> KnowledgeEvidenceSelection | None:
    """Build the same typed evidence ordering used by the full compiler.

    RoutedService uses this before opening external retrieval so a bare
    ``최신`` is owned only after it has become a frozen, requirement-bound
    selection.  No source, product, or fact is authorized here.
    """

    raw_requirements = _deduplicate_structural_requirements(
        question,
        list(intent.knowledge_requirements),
    )
    raw_requirements.sort(key=lambda item: _CAPABILITY_ORDER[item.capability])
    indexed = tuple(
        (
            f"need_{index}",
            item.capability,
            item.capability is not KnowledgeCapability.CORPORATE_SUBSIDIARY,
        )
        for index, item in enumerate(raw_requirements, start=1)
    )
    selection = _compile_evidence_date_selection(question, indexed)
    if selection is not None and _ranking_choice(question) is not None:
        raise CompositeKnowledgePlanningError(
            "DB product ranking and evidence-date ordering conflict"
        )
    return selection


def _holding_match_mode(
    question: str,
    requirements: tuple,
) -> KnowledgeMatchMode:
    holdings = [
        item for item in requirements if item.capability is KnowledgeCapability.PORTFOLIO_HOLDINGS
    ]
    if len(holdings) < 2:
        return KnowledgeMatchMode.ALL
    positions = [
        position
        for item in holdings
        for value in (item.subject_evidence_span, item.relation_evidence_span)
        if (position := question.find(value)) >= 0
    ]
    if not positions:
        raise CompositeKnowledgePlanningError("holding relation evidence is not grounded")
    start = max(0, min(positions) - 12)
    longest_relation = max(len(item.relation_evidence_span) for item in holdings)
    end = min(len(question), max(positions) + longest_relation + 24)
    surface = question[start:end]
    any_match = _ANY_HOLDING_MATCH.search(surface) is not None
    all_match = _EXPLICIT_ALL_HOLDING_MATCH.search(surface) is not None
    if any_match and all_match:
        raise CompositeKnowledgePlanningError("holding ALL/ANY semantics are ambiguous")
    return KnowledgeMatchMode.ANY if any_match else KnowledgeMatchMode.ALL


_HOLDING_RESTATEMENT_GRAMMAR_ATOMS = (
    "으로",
    "에서",
    "에게",
    "부터",
    "까지",
    "이나",
    "나",
    "의",
    "이",
    "가",
    "은",
    "는",
    "을",
    "를",
    "와",
    "과",
    "도",
    "에",
    "로",
)

# HCX occasionally promotes the predicate/linker part of a Korean clause to a
# second relation subject (for example, ``... 이력이 *있는* 관련 ETF``).  Such
# a surface does not name an entity and must never open another official-source
# search.  Keep this as a small grammar vocabulary, not a product/question
# allowlist: a real entity remains admissible as soon as any non-grammar text
# is left after exact tiling.
_PREDICATE_ONLY_EXTERNAL_SUBJECT_ATOMS = (
    "관련되어있는",
    "연결되어있는",
    "연관되어있는",
    "편입되어있는",
    "포함되어있는",
    "보유하고있는",
    "담고있는",
    "가지고있는",
    "관련있는",
    "연결있는",
    "연관있는",
    "관련된",
    "연결된",
    "연관된",
    "연계된",
    "편입된",
    "포함된",
    "보유한",
    "담은",
    "가진",
    "알려줘",
    "찾아줘",
    "보여줘",
    "정리해줘",
    "설명해줘",
    "확인해줘",
    "관련",
    "연결",
    "연관",
    "연계",
    "편입",
    "포함",
    "보유",
    "위험요인",
    "리스크요인",
    "구성종목",
    "투자전략",
    "운용전략",
    "동향",
    "구조",
    "전략",
    "있는",
    "없는",
    "있어",
    "없어",
    "존재",
    "상품",
    "종목",
    "기업",
    "회사",
    "국내etf",
    "해외etf",
    "etf",
    "etn",
    "etp",
    "펀드",
    "채권",
    "테마",
    "섹터",
    "산업",
    *_HOLDING_RESTATEMENT_GRAMMAR_ATOMS,
)


def _is_predicate_only_external_subject(value: str) -> bool:
    """Return true only when a subject is fully made of relation grammar.

    This conservative word-break check prevents a model-created connective
    from multiplying requirements while preserving unseen entity names.  It
    deliberately does not use a list of companies, funds, themes, or products.
    """

    normalized = "".join(character.casefold() for character in value if character.isalnum())
    if not normalized:
        return True
    atoms = tuple(
        sorted(
            set(_PREDICATE_ONLY_EXTERNAL_SUBJECT_ATOMS),
            key=lambda atom: (-len(atom), atom),
        )
    )
    reachable = {0}
    for offset in range(len(normalized) + 1):
        if offset not in reachable:
            continue
        for atom in atoms:
            if normalized.startswith(atom, offset):
                reachable.add(offset + len(atom))
    return len(normalized) in reachable


_GENERIC_DEPENDENT_RISK_SUBJECT = re.compile(
    r"^(?:(?:이|그|해당|각|두|세|여러|위|아래|앞선|전술한)\s*)?"
    r"(?:(?:국내|해외)\s*)?(?:상품|종목|ETF|ETN|ETP|펀드)"
    r"(?:들)?(?:의|이|가|을|를|은|는)?$|"
    r"^(?:최종\s*)?후보(?:들)?(?:별)?(?:로)?(?:의|이|가|을|를|은|는)?$|"
    r"^(?:이들|그들|전부|모두|양쪽|둘)(?:의|이|가|을|를|은|는)?$",
    re.IGNORECASE,
)


def _is_generic_dependent_risk_subject(value: str) -> bool:
    return (
        _is_predicate_only_external_subject(value)
        or _GENERIC_DEPENDENT_RISK_SUBJECT.fullmatch(value.strip()) is not None
    )


def _is_atomic_holding_restatement(
    subject_key: str,
    atomic_key: str,
    relation_keys: set[str],
) -> bool:
    """Recognize only an atomic entity plus its already-grounded relation.

    HCX can emit both ``캠브리콘`` and ``캠브리콘이 편입된`` as holding
    subjects.  The second edge adds no semantics when its complete remainder
    is tiled by the first edge's literal relation and Korean grammar.  A mere
    substring is deliberately insufficient: ``한국전력`` must not consume the
    distinct company ``한국전력기술``.
    """

    if not atomic_key or subject_key.count(atomic_key) != 1:
        return False
    residual = subject_key.replace(atomic_key, "", 1)
    if not residual:
        return False
    relation_atoms = {key for key in relation_keys if key}
    if not relation_atoms:
        return False
    atoms = tuple(
        sorted(
            (*relation_atoms, *_HOLDING_RESTATEMENT_GRAMMAR_ATOMS),
            key=lambda value: (-len(value), value),
        )
    )
    reachable: dict[int, bool] = {0: False}
    for offset in range(len(residual) + 1):
        if offset not in reachable:
            continue
        used_relation = reachable[offset]
        for atom in atoms:
            if residual.startswith(atom, offset):
                next_offset = offset + len(atom)
                reachable[next_offset] = reachable.get(next_offset, False) or (
                    used_relation or atom in relation_atoms
                )
    return reachable.get(len(residual), False)


_RELATION_CLAUSE_BOUNDARY = re.compile(
    r"[.!?\u3002\uff01\uff1f;\uff1b\n]"
    r"|(?:[,\uff0c]\s*)?(?:별도(?:로|의)?|한편|반면|이와\s*별개로|그와\s*별개로)\s+",
    re.IGNORECASE,
)
_INDEPENDENT_RELATION_BOUNDARY = re.compile(
    r"별도|한편|반면|별개",
    re.IGNORECASE,
)
_DEPENDENT_RISK_ANAPHORA = re.compile(
    r"(?:각|해당|이들|그들|앞선|전술한|최종\s*후보(?:별)?|후보(?:별)?)\s*"
    r"(?:상품|ETF|ETN|ETP|펀드|종목)(?:들)?",
    re.IGNORECASE,
)


def _literal_evidence_window(question: str, subject: str, relation: str) -> tuple[int, int] | None:
    """Locate the tightest grounded subject/relation pair in the question.

    Intent spans do not carry offsets.  Pairing the nearest literal occurrences
    gives dependency construction a bounded clause location without trusting
    model ordering or accepting a subject/relation that is absent from the
    original question.
    """

    subjects = tuple(re.finditer(re.escape(subject), question, re.IGNORECASE))
    relations = tuple(re.finditer(re.escape(relation), question, re.IGNORECASE))
    if not subjects or not relations:
        return None
    candidates = []
    for subject_match in subjects:
        for relation_match in relations:
            start = min(subject_match.start(), relation_match.start())
            end = max(subject_match.end(), relation_match.end())
            gap = max(
                0,
                relation_match.start() - subject_match.end(),
                subject_match.start() - relation_match.end(),
            )
            candidates.append((gap, end - start, start, end))
    best_gap, best_width, _, _ = min(candidates)
    best_windows = {
        (start, end)
        for gap, width, start, end in candidates
        if gap == best_gap and width == best_width
    }
    # Intent spans have no offsets.  Repeated identical subject/relation text
    # therefore cannot be bound to one clause without guessing.  Fail closed
    # instead of silently attaching the first risk projection to the wrong
    # product selector.
    if len(best_windows) != 1:
        return None
    start, end = next(iter(best_windows))
    return start, end


def _same_relation_clause(question: str, left_end: int, right_start: int) -> bool:
    """Require dependency evidence to stay inside one explicit question clause."""

    if left_end > right_start:
        return True
    return _RELATION_CLAUSE_BOUNDARY.search(question[left_end:right_start]) is None


def _relation_clause_start(question: str, position: int) -> int:
    boundaries = tuple(_RELATION_CLAUSE_BOUNDARY.finditer(question, 0, position))
    return boundaries[-1].end() if boundaries else 0


def _dependent_selector_indexes_for_risk(
    question: str,
    requirements: list,
    risk_requirement,
) -> tuple[int, ...]:
    """Bind a generic risk projection only to its proven local selectors.

    A literal ``상품/ETF의 위험요인`` may project over products selected by a
    holdings or theme edge.  It must not inherit every selector in the entire
    question.  Only retained, grounded selectors before the risk span and in
    the same clause are eligible.  When the question contains more than one
    risk projection, the previous risk span starts a new dependency segment,
    preventing two independent selector/risk pairs from becoming one global
    intersection.
    """

    risk_window = _literal_evidence_window(
        question,
        risk_requirement.subject_span,
        risk_requirement.relation_span,
    )
    if risk_window is None:
        return ()
    risk_start, risk_end = risk_window
    generic_risk_subject = _is_generic_dependent_risk_subject(risk_requirement.subject_span)
    if generic_risk_subject:
        # A generic result noun can be shared by the upstream selector and its
        # risk projection: ``분류명 ETF의 위험요인``.  The tight evidence window
        # starts at ``ETF``, which incorrectly places the completed theme edge
        # after the risk anchor.  A unique literal risk predicate is the safe
        # dependency anchor; repeated predicates remain ambiguous and retain
        # the fail-closed window above.
        relation_matches = tuple(
            re.finditer(
                re.escape(risk_requirement.relation_span),
                question,
                re.IGNORECASE,
            )
        )
        if len(relation_matches) == 1:
            relation_match = relation_matches[0]
            if risk_start <= relation_match.start() < risk_end:
                risk_start = relation_match.start()
    preceding_boundaries = tuple(_RELATION_CLAUSE_BOUNDARY.finditer(question, 0, risk_start))
    preceding_boundary = preceding_boundaries[-1] if preceding_boundaries else None
    risk_clause_start = preceding_boundary.end() if preceding_boundary is not None else 0
    independent_scope = (
        preceding_boundary is not None
        and _INDEPENDENT_RELATION_BOUNDARY.search(preceding_boundary.group(0)) is not None
    )
    allows_previous_clause = (
        _DEPENDENT_RISK_ANAPHORA.search(
            question[max(risk_clause_start, risk_start - 24) : risk_end]
        )
        is not None
    )
    dependency_segment_start = risk_clause_start
    if (
        not allows_previous_clause
        and not independent_scope
        and _GENERIC_DEPENDENT_RISK_SUBJECT.fullmatch(risk_requirement.subject_span.strip())
    ):
        # ``A와 B가 들어 있는 ETF를 찾아줘. ETF 손실 유의점도 알려줘``
        # uses the bare family noun as an ordinary Korean anaphor.  Admit that
        # previous sentence only when it contains exactly one selector scope.
        # Multiple independent selector phrases remain unbound rather than
        # attaching risk evidence to the wrong product set.
        # A DB-only enrichment sentence can legitimately sit between the
        # external selector and a trailing generic risk projection.  In that
        # case the immediately previous sentence contains no external selector,
        # but the section still has exactly one server-grounded selector scope.
        # Search back only to the previous risk boundary and require that sole
        # scope; two independent holdings/theme scopes remain unbound.
        selector_segment_start = max(
            (
                window[1]
                for other in requirements
                if other is not risk_requirement
                and other.capability is KnowledgeCapability.PRODUCT_RISK_FACTORS
                and (window := _literal_evidence_window(
                    question,
                    other.subject_span,
                    other.relation_span,
                ))
                is not None
                and window[1] <= risk_start
            ),
            default=0,
        )
        previous_scope_keys = {
            (
                candidate.capability,
                "".join(
                    character.casefold()
                    for character in candidate.relation_span
                    if character.isalnum()
                ),
            )
            for candidate in requirements
            if candidate.capability
            in {
                KnowledgeCapability.PORTFOLIO_HOLDINGS,
                KnowledgeCapability.SECTOR_THEME_HISTORY,
            }
            and (
                candidate_window := _literal_evidence_window(
                    question,
                    candidate.subject_span,
                    candidate.relation_span,
                )
            )
            is not None
            and selector_segment_start <= candidate_window[1] <= risk_start
        }
        if len(previous_scope_keys) == 1:
            allows_previous_clause = True
            dependency_segment_start = selector_segment_start
    if allows_previous_clause and risk_clause_start:
        dependency_segment_start = min(
            dependency_segment_start,
            _relation_clause_start(question, risk_clause_start - 1),
        )

    prior_risk_end = 0
    for other in requirements:
        if (
            other is risk_requirement
            or other.capability is not KnowledgeCapability.PRODUCT_RISK_FACTORS
        ):
            continue
        other_window = _literal_evidence_window(
            question,
            other.subject_span,
            other.relation_span,
        )
        if other_window is not None and other_window[1] <= risk_start:
            prior_risk_end = max(prior_risk_end, other_window[1])

    selector_indexes = []
    for index, candidate in enumerate(requirements):
        if candidate.capability not in {
            KnowledgeCapability.PORTFOLIO_HOLDINGS,
            KnowledgeCapability.SECTOR_THEME_HISTORY,
        }:
            continue
        candidate_window = _literal_evidence_window(
            question,
            candidate.subject_span,
            candidate.relation_span,
        )
        if candidate_window is None:
            continue
        _, candidate_end = candidate_window
        if not max(prior_risk_end, dependency_segment_start) <= candidate_end <= risk_start:
            continue
        if not allows_previous_clause and not _same_relation_clause(
            question,
            candidate_end,
            risk_start,
        ):
            continue
        selector_indexes.append(index)
    return tuple(selector_indexes)


def _deduplicate_structural_requirements(question: str, requirements: list) -> list:
    """Drop syntax-expanded restatements while retaining distinct entity edges."""

    corporate_pair = _CORPORATE_PAIR.search(question) or _CORPORATE_SELECTION.search(question)
    corporate_child = corporate_pair.group("child") if corporate_pair is not None else None
    exact_child_holding = bool(
        corporate_child
        and any(
            item.capability is KnowledgeCapability.PORTFOLIO_HOLDINGS
            and item.subject_span == corporate_child
            for item in requirements
        )
    )
    risk_subjects = [
        item.subject_span
        for item in requirements
        if item.capability is KnowledgeCapability.PRODUCT_RISK_FACTORS
    ]
    atomic_holding_relations: dict[str, set[str]] = {}
    for item in requirements:
        if (
            item.capability is not KnowledgeCapability.PORTFOLIO_HOLDINGS
            or _EXPLICIT_ENTITY_LIST.search(item.subject_span) is not None
            or _GENERIC_HOLDING_SUBJECT.fullmatch(item.subject_span.strip()) is not None
        ):
            continue
        subject_key = "".join(
            character.casefold() for character in item.subject_span if character.isalnum()
        )
        relation_key = "".join(
            character.casefold() for character in item.relation_span if character.isalnum()
        )
        if not subject_key:
            continue
        relation_keys = atomic_holding_relations.setdefault(subject_key, set())
        if relation_key:
            relation_keys.add(relation_key)
            # A provider may include the subject inside the relation evidence.
            # The remaining literal relation is still safe for exact tiling.
            if relation_key.count(subject_key) == 1:
                stripped_relation = relation_key.replace(subject_key, "", 1)
                if stripped_relation:
                    relation_keys.add(stripped_relation)
    atomic_holding_keys = set(atomic_holding_relations)
    retained = []
    deferred_generic_risks = []
    retained_keys: set[tuple[KnowledgeCapability, str, str]] = set()
    for item in requirements:
        predicate_only_subject = _is_predicate_only_external_subject(item.subject_span)
        generic_dependent_risk = (
            item.capability is KnowledgeCapability.PRODUCT_RISK_FACTORS
            and _is_generic_dependent_risk_subject(item.subject_span)
        )
        if predicate_only_subject or generic_dependent_risk:
            # A relation predicate without an entity is already represented by
            # the legitimate requirement for this clause.  Retaining it would
            # widen the source set, duplicate network work, and can turn one
            # complete search into an incomplete one.  If it is the only item,
            # the plan remains non-executable downstream rather than inventing
            # an entity.
            #
            # A generic risk target is decided only after every selector has
            # passed the normal structural filters below.  Looking at the raw
            # list here would let a predicate-only/fake selector authorize a
            # live risk search.
            if generic_dependent_risk:
                deferred_generic_risks.append(item)
            continue
        subject_key = "".join(
            character.casefold() for character in item.subject_span if character.isalnum()
        )
        if item.capability is KnowledgeCapability.PORTFOLIO_HOLDINGS:
            if (
                _GENERIC_HOLDING_SUBJECT.fullmatch(item.subject_span.strip()) is not None
                or _HOLDING_PROJECTION_RELATION.fullmatch(item.relation_span.strip()) is not None
            ):
                continue
            covered_atomic_keys = {
                key
                for key in atomic_holding_keys
                if key != subject_key and key and key in subject_key
            }
            # HCX may restate ``A와 B`` as one holding subject after the
            # server has already recovered the two literal entities.  Keeping
            # all three changes ALL/ANY semantics and spends the search budget
            # twice; retain the atomic edges only.
            if len(covered_atomic_keys) >= 2:
                continue
            if len(covered_atomic_keys) == 1:
                atomic_key = next(iter(covered_atomic_keys))
                if _is_atomic_holding_restatement(
                    subject_key,
                    atomic_key,
                    atomic_holding_relations[atomic_key],
                ):
                    continue
        if (
            exact_child_holding
            and item.capability is KnowledgeCapability.PORTFOLIO_HOLDINGS
            and item.subject_span != corporate_child
            and _CORPORATE_DESCENDANT.search(item.subject_span) is not None
            and corporate_child in item.subject_span
        ):
            continue
        if (
            item.capability is KnowledgeCapability.PRODUCT_RISK_FACTORS
            and _EXPLICIT_ENTITY_LIST.search(item.subject_span) is not None
            and sum(
                subject != item.subject_span and subject in item.subject_span
                for subject in risk_subjects
            )
            >= 2
        ):
            continue
        relation_key = "".join(
            character.casefold() for character in item.relation_span if character.isalnum()
        )
        deduplication_key = (
            item.capability,
            subject_key,
            "" if item.capability is KnowledgeCapability.PORTFOLIO_HOLDINGS else relation_key,
        )
        if deduplication_key in retained_keys:
            continue
        retained.append(item)
        retained_keys.add(deduplication_key)
    dependency_context = [*retained, *deferred_generic_risks]
    for item in deferred_generic_risks:
        selector_indexes = _dependent_selector_indexes_for_risk(
            question,
            dependency_context,
            item,
        )
        if not selector_indexes:
            continue
        subject_key = "".join(
            character.casefold() for character in item.subject_span if character.isalnum()
        )
        relation_key = "".join(
            character.casefold() for character in item.relation_span if character.isalnum()
        )
        deduplication_key = (item.capability, subject_key, relation_key)
        if deduplication_key in retained_keys:
            continue
        retained.append(item)
        retained_keys.add(deduplication_key)
    return retained


class CompositeKnowledgePlanCompiler:
    """Validate HCX span hints and compile a non-executable candidate plan."""

    def __init__(self, registry: OfficialSourceRegistry | None = None) -> None:
        self.registry = registry or load_official_source_registry()
        self.source_selector = GeneralizedOfficialSourceSelector(self.registry)

    def _source_ids(
        self,
        capability: KnowledgeCapability,
        *,
        question: str,
        subject: str,
        signals: SourceSelectionSignals | None,
    ) -> tuple[str, ...]:
        try:
            return self.source_selector.select_source_ids(
                capability,
                question=question,
                subject=subject,
                signals=signals,
            )
        except ValueError as error:
            raise CompositeKnowledgePlanningError(
                f"no approved snapshot-intake source for {capability.value}"
            ) from error

    def compile(
        self,
        question: str,
        question_id: str,
        intent: IntentAnalysisDraft,
        *,
        source_selection_signals: SourceSelectionSignals | None = None,
    ) -> CompositeKnowledgeQueryPlan:
        if not question.strip() or not question_id.strip():
            raise ValueError("question and question_id cannot be blank")
        if intent.needs_clarification or not intent.knowledge_requirements:
            raise CompositeKnowledgePlanningError(
                "external knowledge planning requires explicit HCX requirement spans"
            )
        if intent.intent not in {
            InteractionIntent.SEARCH,
            InteractionIntent.DETAIL,
            InteractionIntent.COMPARE,
            InteractionIntent.EXPLAIN,
        }:
            raise CompositeKnowledgePlanningError(
                "external knowledge planning supports only SEARCH, DETAIL, COMPARE, or EXPLAIN"
            )
        unique_keys: set[tuple[KnowledgeCapability, str, str]] = set()
        raw_requirements = []
        for item in intent.knowledge_requirements:
            if item.subject_span not in question or item.relation_span not in question:
                raise CompositeKnowledgePlanningError(
                    "HCX knowledge evidence spans must occur verbatim in the question"
                )
            frame_backed = external_frame_supports_requirement(
                question,
                intent,
                item,
            )
            if not frame_backed and not knowledge_requirement_signal_matches(
                question,
                item,
            ):
                raise CompositeKnowledgePlanningError(
                    "HCX knowledge capability is not supported by its relation span"
                )
            if (
                item.capability is KnowledgeCapability.SECTOR_THEME_HISTORY
                and not frame_backed
                and not question_has_knowledge_capability_signal(
                    item.capability,
                    question,
                    subject=item.subject_span,
                )
            ):
                raise CompositeKnowledgePlanningError(
                    "theme-history planning requires a theme, sector, or industry cue"
                )
            key = (item.capability, item.subject_span, item.relation_span)
            if key in unique_keys:
                raise CompositeKnowledgePlanningError("duplicate external knowledge requirement")
            unique_keys.add(key)
            raw_requirements.append(item)
        raw_requirements = _deduplicate_structural_requirements(question, raw_requirements)
        raw_requirements.sort(key=lambda item: _CAPABILITY_ORDER[item.capability])

        document_references = extract_dated_official_document_references(question)
        has_document_scoped_holdings = (
            len(document_references) > 1
            and any(
                item.capability is KnowledgeCapability.PORTFOLIO_HOLDINGS
                for item in raw_requirements
            )
        )
        time_range = (
            None
            if has_document_scoped_holdings
            else _knowledge_time_range(question, self.registry.evidence_cutoff)
        )
        holding_scope = holding_document_top_n_scope(question)

        families = tuple(sorted(set(intent.product_families), key=lambda item: item.value))
        corporate_requirements = tuple(
            (f"need_{index}", item)
            for index, item in enumerate(raw_requirements, start=1)
            if item.capability is KnowledgeCapability.CORPORATE_SUBSIDIARY
        )
        compiled_requirements = []
        for index, item in enumerate(raw_requirements, start=1):
            subject_span = item.subject_span
            relation_span = item.relation_span
            if item.capability is KnowledgeCapability.CORPORATE_SUBSIDIARY:
                corporate_pair = _CORPORATE_PAIR.search(question) or _CORPORATE_SELECTION.search(
                    question
                )
                if corporate_pair is not None:
                    subject_span = corporate_pair.group("parent")
                    relation_span = corporate_pair.group(0)
            dependencies: tuple[str, ...] = ()
            if item.capability is KnowledgeCapability.PORTFOLIO_HOLDINGS:
                subject_key = "".join(
                    character.casefold() for character in subject_span if character.isalnum()
                )
                dependencies = tuple(
                    requirement_id
                    for requirement_id, corporate in corporate_requirements
                    if (
                        subject_key
                        == "".join(
                            character.casefold()
                            for character in corporate.subject_span
                            if character.isalnum()
                        )
                        or subject_span in corporate.relation_span
                    )
                )
            elif item.capability is KnowledgeCapability.PRODUCT_RISK_FACTORS:
                if _is_generic_dependent_risk_subject(subject_span):
                    dependencies = tuple(
                        f"need_{selector_index + 1}"
                        for selector_index in _dependent_selector_indexes_for_risk(
                            question,
                            raw_requirements,
                            item,
                        )
                    )
            requirement_families = _requirement_families(
                subject_span,
                relation_span,
                families,
            )
            if (
                item.capability is KnowledgeCapability.PRODUCT_RISK_FACTORS
                and dependencies
            ):
                dependent_families = {
                    family
                    for requirement in compiled_requirements
                    if requirement.requirement_id in dependencies
                    for family in requirement.product_families
                }
                if dependent_families:
                    # Risk evidence describes the products selected by the
                    # upstream holdings/theme relation.  A nearby phrase such
                    # as ``해외 ETF`` must not narrow a cross-family result and
                    # silently drop the domestic branch.
                    requirement_families = tuple(
                        sorted(dependent_families, key=lambda family: family.value)
                    )
            if not requirement_families:
                raise CompositeKnowledgePlanningError(
                    "external relation product-family scope conflicts with the request"
                )
            requirement_signals = (
                None
                if source_selection_signals is None
                else SourceSelectionSignals(
                    product_families=requirement_families,
                    db_confirmed_aliases=source_selection_signals.db_confirmed_aliases,
                    matched_product_names=source_selection_signals.matched_product_names,
                    matched_source_owners=source_selection_signals.matched_source_owners,
                    explicit_identity_bound=source_selection_signals.explicit_identity_bound,
                )
            )
            compiled_requirements.append(
                KnowledgeRequirement(
                    requirement_id=f"need_{index}",
                    capability=item.capability,
                    predicate=predicate_for_capability(item.capability),
                    subject=subject_span.strip(),
                    search_aliases=tuple(sorted(item.search_aliases)),
                    subject_evidence_span=subject_span,
                    relation_evidence_span=relation_span,
                    product_families=requirement_families,
                    time_range=(
                        time_range
                        if item.capability
                        in {
                            KnowledgeCapability.FUND_STRUCTURE_STRATEGY_TREND,
                            KnowledgeCapability.PORTFOLIO_HOLDINGS,
                            KnowledgeCapability.SECTOR_THEME_HISTORY,
                        }
                        else None
                    ),
                    document_scope=(
                        KnowledgeDocumentTopNScope(
                            limit=holding_scope[0],
                            selection_evidence_span=holding_scope[1],
                        )
                        if (
                            item.capability is KnowledgeCapability.PORTFOLIO_HOLDINGS
                            and holding_scope is not None
                        )
                        else None
                    ),
                    source_registry_ids=self._source_ids(
                        item.capability,
                        question=question,
                        subject=subject_span,
                        signals=requirement_signals,
                    ),
                    depends_on=dependencies,
                )
            )
        requirements = tuple(compiled_requirements)

        if _SUBJECTIVE_THEME.search(question) is not None and any(
            item.capability is KnowledgeCapability.SECTOR_THEME_HISTORY for item in requirements
        ):
            raise CompositeKnowledgePlanningError(
                "theme meaning is subjective; state an explicit related/linked theme condition"
            )

        evidence_selection = _compile_evidence_date_selection(
            question,
            tuple(
                (
                    requirement.requirement_id,
                    requirement.capability,
                    not requirement.depends_on,
                )
                for requirement in requirements
            ),
        )
        product_selection = None
        ranking_choice = _ranking_choice(question)
        if evidence_selection is not None and ranking_choice is not None:
            raise CompositeKnowledgePlanningError(
                "DB product ranking and evidence-date ordering conflict"
            )
        if _PERFORMANCE_RANKING.search(question) is not None and ranking_choice is None:
            raise CompositeKnowledgePlanningError(
                "external performance ranking requires an explicit supported period"
            )
        if ranking_choice is not None:
            rank_field, direction = ranking_choice
            rank_field = _family_aware_ranking_field(rank_field, families)
            limit = _external_product_result_limit(question)
            if not 1 <= limit <= 20:
                raise CompositeKnowledgePlanningError("knowledge result limit is out of bounds")
            product_selection = KnowledgeProductSelection(
                rank_field=rank_field,
                direction=direction,
                limit=limit,
            )
        requested_product_fields = tuple(
            sorted(
                field
                for field, pattern in _REQUESTED_PRODUCT_FIELD_PATTERNS.items()
                if pattern.search(question)
            )
        )

        return CompositeKnowledgeQueryPlan(
            question_id=question_id,
            question=question,
            interaction_intent=intent.intent,
            evidence_cutoff=self.registry.evidence_cutoff,
            requirements=requirements,
            holding_match_mode=_holding_match_mode(question, requirements),
            product_selection=product_selection,
            evidence_selection=evidence_selection,
            requested_product_fields=requested_product_fields,
            join_order=tuple(
                [item.requirement_id for item in requirements] + ["official_product_db"]
            ),
        )


__all__ = [
    "CompositeKnowledgePlanCompiler",
    "CompositeKnowledgePlanningError",
    "compile_evidence_date_selection",
]
