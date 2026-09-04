from __future__ import annotations

import calendar
import re
from datetime import date, timedelta

from finance_agent_core.agent.intent_analysis import IntentAnalysisDraft
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


_CAPABILITY_SIGNAL: dict[KnowledgeCapability, re.Pattern[str]] = {
    KnowledgeCapability.PORTFOLIO_HOLDINGS: re.compile(
        r"편입|보유|구성\s*종목|담(?:은|긴|고\s*있는)|"
        r"넣(?:은|고\s*있는)|"
        r"포함(?:된|한|하고|하는|되어)|(?:들어|담겨)\s*있|들어간|"
        r"구성\s*종목(?:으)?로\s*둔|가진|가지(?:는|고\s*있는|고\s*있|고)",
        re.IGNORECASE,
    ),
    KnowledgeCapability.CORPORATE_SUBSIDIARY: re.compile(
        r"자회사|계열사|가족사|그룹사|"
        r"(?:계열|관계|소속|산하)\s*(?:기업|회사|법인)",
        re.IGNORECASE,
    ),
    KnowledgeCapability.SECTOR_THEME_HISTORY: re.compile(
        r"테마|섹터|산업|관련|연결|연관|연계|접점|노출|"
        r"관련\s*(?:된\s*)?(?:국내\s*|해외\s*)?"
        r"(?:ETF|ETN|ETP|상장\s*지수\s*(?:상품|펀드))|"
        r"관련\s*(?:투자\s*)?상품|"
        r"^(?:국내\s*|해외\s*)?(?:ETF|ETN|ETP|상장\s*지수\s*(?:상품|펀드))$|"
        r"[0-9A-Za-z가-힣&·+._/-]{2,40}\s+(?:국내\s*|해외\s*)?"
        r"(?:ETF|ETN|ETP)",
        re.IGNORECASE,
    ),
    KnowledgeCapability.FUND_STRUCTURE_STRATEGY_TREND: re.compile(
        r"구조|조성\s*(?:구조|방식)|설계(?:\s*방식)?|"
        r"자금\s*(?:조달|배분)\s*구조|(?:투자|운용)\s*(?:전략|방침)|"
        r"운용\s*기조|(?:판매|모집|출시)(?:\s*(?:동향|현황|일정|"
        r"완료|종료|진행\s*(?:상황|현황)))|운용\s*(?:동향|방향|방침|계획|현황)|"
        r"전략\s*동향|(?:향후|앞으로)\s*(?:운용\s*)?(?:일정|계획)|"
        r"진행\s*(?:상황|현황)",
        re.IGNORECASE,
    ),
    KnowledgeCapability.PRODUCT_RISK_FACTORS: re.compile(
        r"위험\s*요인|리스크\s*요인|"
        r"투자\s*위험(?:을|은|이|도)?\s*(?:알려|설명|정리|확인)|"
        r"손실.{0,12}유의|주의\s*(?:사항|점)|유의\s*(?:사항|점)|"
        r"원금\s*손실\s*가능성",
        re.IGNORECASE,
    ),
}
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
_EXPLICIT_MONTH = re.compile(
    r"(20\d{2})\s*년\s*(\d{1,2})\s*월(?:\s*말)?",
    re.IGNORECASE,
)
_LIMIT = re.compile(
    r"(?:TOP\s*(?P<top>\d{1,2})|(?P<count>\d{1,2})\s*(?:개|건)(?:씩)?|"
    r"(?P<word>한|두|세|네|다섯|여섯|일곱|여덟|아홉|열)\s*(?:개|건)(?:씩)?)",
    re.IGNORECASE,
)
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
_AUM = re.compile(r"AUM|순\s*자산|운용\s*자산", re.IGNORECASE)
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
_PERFORMANCE_RANKING = re.compile(
    r"(?:수익률|성과|실적)\s*(?:이\s*)?(?:높은\s*)?(?:순|상위|TOP)|"
    r"(?:순|상위|TOP).{0,8}(?:수익률|성과|실적)",
    re.IGNORECASE,
)
_RANK_FIELD_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("aum", _AUM),
    ("total_expense_ratio_pct", _TOTAL_EXPENSE_RATIO),
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
    "one_day_return_pct": _ONE_DAY_RETURN,
    "one_month_return_pct": _ONE_MONTH_RETURN,
    "one_year_return_pct": _ONE_YEAR_RETURN,
    "product_name": re.compile(r"상품명|상품\s*이름|상품\s*정보", re.IGNORECASE),
    "risk_level": re.compile(r"위험\s*등급", re.IGNORECASE),
    "three_month_return_pct": _THREE_MONTH_RETURN,
    "ticker": re.compile(r"티커|ticker", re.IGNORECASE),
    "total_expense_ratio_pct": _TOTAL_EXPENSE_RATIO,
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
_CAPABILITY_ORDER = {
    KnowledgeCapability.CORPORATE_SUBSIDIARY: 0,
    KnowledgeCapability.PORTFOLIO_HOLDINGS: 1,
    KnowledgeCapability.SECTOR_THEME_HISTORY: 2,
    KnowledgeCapability.FUND_STRUCTURE_STRATEGY_TREND: 3,
    KnowledgeCapability.PRODUCT_RISK_FACTORS: 4,
}
_REQUIREMENT_FAMILY_CUES: dict[ProductFamily, re.Pattern[str]] = {
    ProductFamily.BOND: re.compile(r"(?:원화|국내)?\s*채권", re.IGNORECASE),
    ProductFamily.DOMESTIC_ETP: re.compile(
        r"국내\s*(?:ETF|ETN|ETP|상장\s*지수\s*(?:상품|펀드))",
        re.IGNORECASE,
    ),
    ProductFamily.OVERSEAS_ETP: re.compile(
        r"해외\s*(?:ETF|ETN|ETP|상장\s*지수\s*(?:상품|펀드))",
        re.IGNORECASE,
    ),
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
        family for family, cue in _REQUIREMENT_FAMILY_CUES.items() if cue.search(surface)
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


def _knowledge_time_range(question: str, cutoff: date) -> KnowledgeTimeRange | None:
    """Normalize bounded temporal language against the signed evidence cutoff."""

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

    from_to_range = _EXPLICIT_FROM_TO_MONTH_RANGE.search(question)
    explicit_range = _EXPLICIT_MONTH_RANGE.search(question)
    explicit_month = _EXPLICIT_MONTH.search(question)
    if from_to_range is not None:
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


def question_binds_external_ranking(question: str) -> bool:
    """Report whether a direction word is bound to a supported product metric.

    A conflicting binding also counts: the plan compiler raises its own CLARIFY
    for that case, so the coverage boundary does not need to repeat it.
    """

    try:
        return _ranking_choice(question) is not None
    except CompositeKnowledgePlanningError:
        return True


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
    retained = []
    for item in requirements:
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
        retained.append(item)
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
            if not _CAPABILITY_SIGNAL[item.capability].search(item.relation_span):
                raise CompositeKnowledgePlanningError(
                    "HCX knowledge capability is not supported by its relation span"
                )
            key = (item.capability, item.subject_span, item.relation_span)
            if key in unique_keys:
                raise CompositeKnowledgePlanningError("duplicate external knowledge requirement")
            unique_keys.add(key)
            raw_requirements.append(item)
        raw_requirements = _deduplicate_structural_requirements(question, raw_requirements)
        raw_requirements.sort(key=lambda item: _CAPABILITY_ORDER[item.capability])

        time_range = _knowledge_time_range(question, self.registry.evidence_cutoff)

        families = tuple(sorted(set(intent.product_families), key=lambda item: item.value))
        requirement_ids_by_capability: dict[KnowledgeCapability, list[str]] = {}
        for index, item in enumerate(raw_requirements, start=1):
            requirement_ids_by_capability.setdefault(item.capability, []).append(f"need_{index}")
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
                dependencies = tuple(
                    requirement_id
                    for capability in (
                        KnowledgeCapability.PORTFOLIO_HOLDINGS,
                        KnowledgeCapability.SECTOR_THEME_HISTORY,
                    )
                    for requirement_id in requirement_ids_by_capability.get(capability, ())
                )
            requirement_families = _requirement_families(
                subject_span,
                relation_span,
                families,
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

        product_selection = None
        ranking_choice = _ranking_choice(question)
        if _PERFORMANCE_RANKING.search(question) is not None and ranking_choice is None:
            raise CompositeKnowledgePlanningError(
                "external performance ranking requires an explicit supported period"
            )
        if ranking_choice is not None:
            rank_field, direction = ranking_choice
            requested = [
                (
                    int(match.group("top") or match.group("count"))
                    if match.group("top") or match.group("count")
                    else _KOREAN_LIMITS[match.group("word")]
                )
                for match in _LIMIT.finditer(question)
            ]
            limit = requested[-1] if requested else 1
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
            requested_product_fields=requested_product_fields,
            join_order=tuple(
                [item.requirement_id for item in requirements] + ["official_product_db"]
            ),
        )


__all__ = [
    "CompositeKnowledgePlanCompiler",
    "CompositeKnowledgePlanningError",
]
