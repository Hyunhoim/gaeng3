from __future__ import annotations

import re
from collections.abc import Iterable
from time import perf_counter

from finance_agent_core.agent.field_language import (
    registry_field_mentions,
    registry_implied_families,
)
from finance_agent_core.agent.numeric_language import requested_result_limit
from finance_agent_core.agent.planning_policy import (
    AdaptiveShadowPlanningPolicy,
    PlanningDecision,
    PlanningDecisionStatus,
    PlanningPolicy,
    PlanningTrace,
)
from finance_agent_core.agent.presentation import semantic_query_surface
from finance_agent_core.agent.product_identity import (
    extract_named_product_mentions,
    is_domestic_etp_brand_question,
)
from finance_agent_core.agent.safety import (
    SafetyDisposition,
    SafetyEnvelope,
    safety_policy_surface,
)
from finance_agent_core.agent.semantic_aliases import (
    ranking_intent_present,
    typed_alias_matches,
)
from finance_agent_core.agent.semantic_gate import (
    SemanticCoverageDecision,
    SemanticCoverageGate,
)
from finance_agent_core.agent.semantic_ledger import build_resolved_span_ledger
from finance_agent_core.config.capability import CapabilityMatrix, load_capability_matrix
from finance_agent_core.contracts.queryplan import Intent, ProductFamily
from finance_agent_core.contracts.routing import (
    InteractionIntent,
    MinimalQueryDraft,
    RouteDecision,
    RouteDisposition,
)
from finance_agent_core.observability import (
    AuditOutcome,
    AuditStage,
    MetricCounter,
    current_request_audit,
)

_COMPARE = re.compile(
    r"비교(?!\s*표)|대조|차이(?!나)|나란히|versus|\bvs\b|"
    r"(?:어떻게|무엇이|뭐가)\s*다른지|"
    r"(?:둘|두\s*(?:개(?:의)?|상품|공모\s*펀드|펀드|채권|ETF|ETN))"
    r".{0,40}?중(?:에서)?|"
    r"어느\s*(?:게|것이).{0,40}?(?:높|낮|크|작|많|적|길|짧)",
    re.IGNORECASE,
)
_TWO_ID_COMPARATIVE = re.compile(
    r"(?:가운데|중(?:에서)?).{0,64}?(?:더\s*)?"
    r"(?:높|낮|크|큰|작|작은|많|적|긴|길|짧)|"
    r"더\s*(?:높|높은|낮|낮은|크|큰|작|작은|많|많은|적|적은|긴|길|짧)|"
    r"먼저\s*(?:오|돌아오)|"
    r"어느\s*(?:쪽|상품|종목)",
    re.IGNORECASE,
)
_AGGREGATE = re.compile(
    r"몇\s*(?:개|건|종(?!가)|종목|상품|ETF|ETN|ETP|채권|펀드)|"
    r"(?:종목|상품)(?:이|가|은|는)?\s*몇\s*(?:개|건)?|"
    r"(?:전체\s*)?후보(?:의)?\s*(?:수|개수|건수)|"
    r"(?:전체\s*|각\s*)?(?:종목|상품|펀드)\s*수|"
    r"개수(?!\s*[:：]\s*\d+)|건수|평균|중앙값|중간값|합계|총합|집계|분포|비중|"
    r"(?:상품|ETF|ETN|ETP|채권|펀드)(?:\s*(?:상품|종목))?(?:의)?\s*"
    r"수(?:를|가|는)?\s*"
    r"(?:계산|집계|총합|알려|세어|헤아려)|"
    r"(?:상품|ETF|ETN|ETP|채권|펀드)(?:을|를)?\s*(?:세어|헤아려)|"
    r"(?:유효|미유효|사용\s*가능|사용할\s*수\s*없|기재|입력|확인|적힌|"
    r"채워진|누락|결측|공란|빈\s*값|비어\s*있는).{0,48}?"
    r"(?:(?:건수|개수|수)|경우(?:의)?\s*수)(?:를|가|는)?\s*"
    r"(?:계산|집계|알려|세어|구해|나눠|구분|보여)|"
    r"(?:상품|종목|ETF|ETN|ETP|채권|펀드)(?:의)?\s*수(?:를|가|는)?"
    r".{0,32}?(?:별|마다)(?:로)?\s*(?:세|헤아리|계산|집계|나눠|구분)|"
    r"(?:상품|종목|ETF|ETN|ETP|채권|펀드)(?:의)?\s*수(?:를|가|는)?"
    r".{0,48}?(?:별|마다)(?:로)?\s*(?:묶|나눠|구분)|"
    r"최댓값|최대값|최솟값|최소값|최고값|최저값|"
    r"(?:AUM|보수율|수익률|이율|잔존일수|듀레이션)\s*(?:최대|최소)",
    re.IGNORECASE,
)
_GROUPED_VALUE_AGGREGATE = re.compile(
    r"(?:별|마다)(?:로)?\s*(?:각각\s*)?"
    r"(?:"
    r"(?:최댓값|최대값|최대|최솟값|최소값|최소|최고값|최저값|평균|중앙값|중간값|합계|총합)"
    r"|"
    r"(?:[0-9A-Za-z가-힣_%]+\s*){1,3}"
    r"(?:최댓값|최대값|최대|최솟값|최소값|최소|최고값|최저값|평균|중앙값|중간값|합계|총합)"
    r")",
    re.IGNORECASE,
)
_GROUPED_PRODUCT_RANKING = re.compile(
    r"(?:상위|하위|TOP|오름차순|내림차순|큰\s*순|작은\s*순|높은\s*순|낮은\s*순)"
    r".{0,32}?(?:상품|종목)|"
    r"(?:상품|종목).{0,32}?"
    r"(?:상위|하위|TOP|오름차순|내림차순|큰\s*순|작은\s*순|높은\s*순|낮은\s*순)",
    re.IGNORECASE,
)
_GROUP_LABEL_ORDER = re.compile(
    r"(?:대분류|소분류|상품\s*유형|유형|자산\s*유형|운용\s*속성|"
    r"위험\s*등급|투자\s*지역|거래\s*통화)(?:명)?"
    r"(?:을|를|은|는|이|가)?\s*(?:오름차순|가나다순|알파벳순)",
    re.IGNORECASE,
)
_PORTFOLIO_RATIO_FIELD = re.compile(
    r"(?:국내|해외)\s*(?:주식|채권)\s*(?:구성\s*비율|비중)",
    re.IGNORECASE,
)
_EXPLAIN = re.compile(r"설명|무슨\s*뜻|뭐(?:야|고)|의미|장점|요소|왜\s")
_DEFINITION = re.compile(r"무슨\s*뜻|뭐(?:야|고)|의미")
_DETAIL = re.compile(
    r"상세|세부|자세히|어떤\s*상품|정보\s*조회|상품\s*(?:번호|ID|아이디)|"
    r"종목\s*(?:코드|번호)|티커",
    re.IGNORECASE,
)
_AMBIGUOUS = re.compile(
    r"적당한|적당히|괜찮은|안전한|좋은\s*상품|추천(?:해|하|받|할\s*만한)|"
    r"좀\s*낮아도|많이\s*주는|제일\s*수익률|리스크.*신경\s*안|"
    r"뭐가\s*더\s*좋|어느.*더\s*좋|(?:두\s*종목|둘)\s*중.{0,20}?나은|"
    r"오래\s*들고.{0,20}?부담\s*없|(?:보수|비용|매수)?.{0,12}?"
    r"부담(?:이|가)?\s*(?:없|되지\s*않)|무난(?:한|하다)?|"
    r"잘\s*나가|거래가?\s*활발|"
    r"분배(?:를|가)?\s*(?:많이|잘)|"
    r"(?:가격.*왜|왜.*가격)",
    re.IGNORECASE,
)
_UNSUPPORTED = re.compile(
    r"전망|예측|예상\s*수익|수익\s*보장|원금\s*보장|"
    r"매수(?:를)?\s*추천|투자\s*추천|사야\s*할|사면\s*좋|가장\s*좋은|"
    r"상승할\s*것으로\s*예상|"
    r"기대되는\s*(?:고)?수익|오를\s*것|오를까|호재|악재|"
    r"유상증자|유상감자|사모\s*CB|"
    r"기관.*(?:구매|순매수|매매량)|"
    r"오늘.*가장.*(?:오른|상승)",
    re.IGNORECASE,
)
_EXTERNAL_POLICY = re.compile(r"세율|세금|과세|거래\s*수수료", re.IGNORECASE)
_UNAVAILABLE_MARKET_DATA = re.compile(
    r"배당.*(?:가장|최대)|"
    r"하락장.*잘\s*버티|"
    r"시장\s*전망",
    re.IGNORECASE,
)
_MIXED_ETP_COST = re.compile(
    r"(?=.*ETF)(?=.*ETN)(?=.*(?:보수|수수료|비용))",
    re.IGNORECASE,
)
_EXPLICIT_TOTAL_EXPENSE_RATIO = re.compile(r"총\s*보수(?:율)?|보수율", re.IGNORECASE)
_TICKER_ORDERING = re.compile(
    r"(?:티커|종목\s*코드).{0,24}"
    r"(?:오름차순|내림차순|큰\s*순|작은\s*순|높은\s*순|낮은\s*순|순서)",
    re.IGNORECASE,
)
_RANKED_SET_SEARCH = re.compile(
    r"끼리(?:만)?.{0,100}?"
    r"(?:오름차순|내림차순|높은\s*순|낮은\s*순|큰\s*순|작은\s*순|"
    r"많은\s*순|적은\s*순|상위|하위|ascending|descending|"
    r"highest\s*[- ]to\s*[- ]lowest|lowest\s*[- ]to\s*[- ]highest)",
    re.IGNORECASE,
)
_ORDINAL_MULTI_SEARCH = re.compile(
    r"(?<![0-9A-Za-z가-힣])(?:첫째|둘째|셋째|넷째)(?:\s*[,.:：])?",
    re.IGNORECASE,
)
_EXPLICIT_PER_FAMILY_SEARCH = re.compile(
    r"각각|나란히|한\s*답변에서|한\s*번에|모두\s*포함한\s*목록|"
    r"상품군별로|종류별로",
    re.IGNORECASE,
)
_CROSS_FAMILY_GLOBAL_SEARCH = re.compile(
    r"통합\s*(?:비교|정렬|순위)|한\s*줄(?:에|로)?\s*(?:비교|정렬|순위)|"
    r"통합\s*(?:높은|낮은)\s*순|함께\s*(?:비교|정렬|순위)|"
    r"한\s*(?:목록|리스트)(?:으로|에)?|(?:한\s*순위에\s*)?합쳐|"
    r"따로\s*나누지\s*말고|직접\s*비교",
    re.IGNORECASE,
)


def _ordinal_query_clauses(question: str) -> tuple[str, ...]:
    """Return only the query sentence from each explicit ordinal clause."""

    markers = list(_ORDINAL_MULTI_SEARCH.finditer(question))
    if len(markers) < 2:
        return ()
    clauses: list[str] = []
    for index, marker in enumerate(markers):
        clause_end = markers[index + 1].start() if index + 1 < len(markers) else len(question)
        raw_clause = question[marker.end() : clause_end].strip()
        query_sentence = re.split(r"(?<=[.!?])\s+", raw_clause, maxsplit=1)[0].strip()
        if query_sentence:
            clauses.append(query_sentence)
    return tuple(clauses)


_EXPLICIT_ETP_TYPE = re.compile(
    r"(?:ETP\s*유형|ETF\s*여부|상품\s*유형)(?:이|가|은|는)?\s*[:：]?\s*(?:ETF|ETN)|"
    r"(?<![A-Z])(?:ETF|ETN)(?:인(?!지)|이며|이고|인데|에\s*해당|로\s*되어)",
    re.IGNORECASE,
)
_OVERSEAS_UNAVAILABLE_METRIC = re.compile(
    r"(?:(?:1\s*주|일주일|1\s*개월|한\s*달|3\s*개월|6\s*개월|1\s*년|YTD|"
    r"연초\s*이후)\s*(?:수익률|수익|성과))|변동성|하락장|오른",
    re.IGNORECASE,
)
_FINANCE_SCOPE_SIGNAL = re.compile(
    r"(?<![A-Z])(?:ETF|ETN|ETP)(?![A-Z])|공모\s*펀드|국내\s*채권|"
    r"(?:금융|투자)\s*상품|"
    r"상품\s*(?:군|유형|목록|정보|조회|검색|추천|순|기준|가운데|"
    r"을|를|이|가|은|는|중|있(?:어|나|나요))|"
    r"AUM|수익률|총\s*보수|보수율|매수\s*수익률|잔존\s*일수|듀레이션|"
    r"종목\s*코드|티커|만기|채권|펀드",
    re.IGNORECASE,
)
_FUND_UNAVAILABLE_DETAIL = re.compile(
    r"언제든.*(?:꺼내|환매)",
    re.IGNORECASE,
)
_QUOTED = (
    re.compile(r'"([^"\n]+)"'),
    re.compile(r"'([^'\n]+)'"),
    re.compile(r"“([^”\n]+)”"),
    re.compile(r"‘([^’\n]+)’"),
)
_KNOWN_ID = re.compile(
    # Domestic ETP tickers are seven characters: an `A` (ETF) or `Q` (ETN)
    # prefix, four digits, then two alphanumerics. Requiring the four digits
    # keeps brand words such as ARIRANG or KODEX from being read as an
    # identifier, and every approved domestic ticker matches this shape.
    r"(?<![A-Z0-9])(?:(?-i:KR)[A-Z0-9]{10}|(?:[A-Z]{2,5}|[0-9]{3}):[A-Z0-9._-]+|"
    r"[AQ][0-9]{4}[A-Z0-9]{2}|"
    r"[A-Z][A-Z0-9]{1,9}\.[A-Z0-9]{1,5})(?![A-Z0-9])",
    re.IGNORECASE,
)
_BARE_OVERSEAS_TICKER = re.compile(r"(?<![A-Z0-9.])([A-Z]{2,5})(?![A-Z0-9.])")
_PARENTHESIZED_OVERSEAS_TICKER = re.compile(r"\(([A-Z]{2,5}(?:\.[A-Z0-9]{1,5})?)\)")
_RESERVED_OVERSEAS_TICKER_TOKENS = frozenset(
    {
        "AUM",
        "ID",
        "RIC",
        "ETF",
        "ETN",
        "ETP",
        "TOP",
        "USD",
        "KRW",
        "EUR",
        "JPY",
        "YTD",
        "NYSE",
        "NASDAQ",
        "AMEX",
        "NAS",
        "NYS",
        "AMX",
        "AI",
        "ACE",
        "ARIRANG",
        "HANARO",
        "KBSTAR",
        "KODEX",
        "RISE",
        "SOL",
        "TIGER",
        "TIMEFOLIO",
    }
)
_OVERSEAS_COMPARISON_METRIC = re.compile(
    r"(?<![A-Z0-9])AUM(?![A-Z0-9])|총\s*보수(?:율)?|연간\s*보수(?:율)?|일(?:간)?\s*거래량|"
    r"거래량|종가|마감\s*가격|(?:1\s*일|하루|당일)\s*(?:수익률|수익|성과)",
    re.IGNORECASE,
)
_LABELED_ID = re.compile(
    r"(?:상품\s*(?:번호|ID|아이디)|종목\s*(?:코드|번호)|티커|식별자)"
    r"(?:가|는|은|이)?\s*[:：]?\s*"
    r"([A-Z0-9._:-]{2,30})",
    re.IGNORECASE,
)
_FAMILY_ENUMERATION_MENTION = re.compile(
    r"^\s*(?:(?:국내|해외)\s*(?:ETF|ETN|ETP)|(?:공모\s*)?펀드|"
    r"(?:국내|원화)\s*채권|ETF|ETN|ETP|채권)"
    r"(?:\s*(?:[·,/]|와|과|및)\s*"
    r"(?:(?:국내|해외)\s*(?:ETF|ETN|ETP)|(?:공모\s*)?펀드|"
    r"(?:국내|원화)\s*채권|ETF|ETN|ETP|채권))+\s*$",
    re.IGNORECASE,
)
_RESTRICTED_ALPHANUMERIC_PRODUCT = re.compile(
    r"(?P<core>[0-9A-Za-z가-힣&·+._/-]{3,48})\s+(?P<kind>상품)"
    r"(?=\s*만(?:을|를|이|가|은|는|\s|[?!.,]|$))"
)
_RESTRICTED_PRODUCT_FAMILY_PREFIX = re.compile(
    r"(?:국내|해외)\s*(?:ETF|ETN|ETP)|(?:공모\s*)?펀드|"
    r"(?:국내|원화)\s*채권|(?<![A-Za-z])(?:ETF|ETN|ETP)(?![A-Za-z])",
    re.IGNORECASE,
)

_QUOTED_FILTER_VALUE_PREDICATE = re.compile(
    r"^\s*[\"'”’]?\s*(?:이|가|은|는|을|를|로|으로)?\s*"
    r"(?:들어|포함|일치|해당|같|표시|분류|기재|등록|인(?:\s|$)|인\s*상품)",
    re.IGNORECASE,
)
_QUOTED_CONTAINS_VALUE_PREDICATE = re.compile(
    r"^\s*[\"'”’]?\s*(?:이|가|은|는|을|를|로|으로)?\s*"
    r"(?:들어|포함|일치|같|표시|분류|기재|등록)",
    re.IGNORECASE,
)
_IDENTITY_FIELD_IDS = frozenset(
    {"product_id", "product_name", "short_name", "ticker", "isin"}
)


def _quoted_span_is_field_literal(question: str, start: int, end: int) -> bool:
    """Keep quoted filter values out of the product-identity set.

    Quotes are presentation syntax, not proof of a named product.  A value such
    as ``상품명에 '미국'이 들어간`` or ``위험등급이 '1등급'인`` belongs to
    the adjacent typed field predicate.  Conversely, an exact quoted product
    name remains an identity so DETAIL/COMPARE continues to work.
    """

    suffix = question[end : min(len(question), end + 32)]
    predicate = _QUOTED_FILTER_VALUE_PREDICATE.search(suffix)
    if predicate is None:
        return False
    nearby: dict[tuple[int, int, str], object] = {}
    for family in ("bond", "domestic_etp", "overseas_etp", "fund"):
        for mention in registry_field_mentions(question, family, "queryable"):
            if 0 <= start - mention.end <= 20:
                nearby[(mention.start, mention.end, mention.field_id)] = mention
    if not nearby:
        return False
    closest = max(nearby.values(), key=lambda item: item.end)
    if closest.field_id not in _IDENTITY_FIELD_IDS:
        return True
    return _QUOTED_CONTAINS_VALUE_PREDICATE.search(suffix) is not None
_IDENTIFIER_LIKE = re.compile(
    r"(?<![A-Z0-9])(?:(?-i:KR)[A-Z0-9]{4,30}|"
    r"(?:[A-Z]{2,5}|[0-9]{3}):[A-Z0-9._-]{1,24}|"
    r"[A-Z][A-Z0-9]{1,9}\.[A-Z0-9]{1,5})(?![A-Z0-9])",
    re.IGNORECASE,
)
_IDENTIFIER_POSTPOSITIONS = (
    "으로부터",
    "에게서",
    "으로는",
    "에서는",
    "으로",
    "에서",
    "에게",
    "까지",
    "부터",
    "처럼",
    "보다",
    "의",
    "은",
    "는",
    "이",
    "가",
    "을",
    "를",
    "와",
    "과",
    "에",
    "로",
    "도",
)
_CONTROL_FAMILY_PRIORITY = {
    ProductFamily.FUND: 0,
    ProductFamily.BOND: 1,
    ProductFamily.DOMESTIC_ETP: 2,
    ProductFamily.OVERSEAS_ETP: 3,
}


def _ordered_unique(values: Iterable[str]) -> list[str]:
    return list(dict.fromkeys(value.strip() for value in values if value.strip()))


def _bare_overseas_comparison_pair(question: str) -> list[re.Match[str]]:
    """Return an exact pair of bare overseas tickers in a comparison clause.

    Short all-uppercase tokens are accepted only when there are exactly two,
    an explicit comparison cue, and an approved overseas-ETP metric.  This
    keeps ordinary acronyms and credit grades from acquiring product identity
    while allowing natural requests such as ``SPY와 QQQ 중 AUM 비교``.
    """

    if _COMPARE.search(question) is None or _OVERSEAS_COMPARISON_METRIC.search(question) is None:
        return []
    matches = [
        match
        for match in _BARE_OVERSEAS_TICKER.finditer(question)
        if match.group(1).upper() not in _RESERVED_OVERSEAS_TICKER_TOKENS
    ]
    unique = {match.group(1).upper() for match in matches}
    return matches if len(matches) == 2 and len(unique) == 2 else []


def _normalize_product_mention(value: str) -> str:
    """Detach a Korean postposition only when the remaining token is a known ID form."""

    stripped = value.strip()
    for suffix in _IDENTIFIER_POSTPOSITIONS:
        if stripped.endswith(suffix):
            candidate = stripped[: -len(suffix)].strip()
            if _KNOWN_ID.fullmatch(candidate) is not None:
                return candidate
    return stripped


def _drop_enclosed_mentions(spans: list[tuple[int, int, str]]) -> list[tuple[int, int, str]]:
    """Keep the widest span when one mention is printed inside another.

    A quoted product name can contain its own ticker, and a labelled identifier
    repeats the bare identifier. Counting those twice turns a single DETAIL
    target into an ambiguous two-product request. Spans whose position could not
    be located are neither dropped nor used to drop others.
    """

    return [
        (start, end, value)
        for start, end, value in spans
        if start < 0
        or not any(
            other_start >= 0
            and (other_start, other_end) != (start, end)
            and other_start <= start
            and end <= other_end
            for other_start, other_end, _ in spans
        )
    ]


def _product_mentions(question: str) -> list[str]:
    mentions: list[tuple[int, int, str]] = []
    for pattern in _QUOTED:
        mentions.extend(
            (match.start(1), match.end(1), match.group(1))
            for match in pattern.finditer(question)
            if not _quoted_span_is_field_literal(question, match.start(1), match.end(1))
        )
    known_id_matches = list(_KNOWN_ID.finditer(question))
    mentions.extend((match.start(), match.end(), match.group(0)) for match in known_id_matches)
    mentions.extend(
        (match.start(1), match.end(1), match.group(1)) for match in _LABELED_ID.finditer(question)
    )
    for match in _RESTRICTED_ALPHANUMERIC_PRODUCT.finditer(question):
        core = match.group("core")
        # ``<name> 상품만`` is a hard identity restriction only when it
        # follows an explicit product family and has an identifier-like digit.
        # Quoted and labelled plain-text names are already handled above.  The
        # digit requirement prevents ranking grammar such as ``높은 상품만``
        # or ``상위 상품만`` from collapsing the candidate universe.
        sentence_start = max(
            question.rfind(mark, 0, match.start()) for mark in (".", "?", "!", "\n")
        )
        family_prefix = question[max(sentence_start + 1, match.start() - 100) : match.start()]
        if (
            any(character.isdigit() for character in core)
            and any(character.isalpha() for character in core)
            and _RESTRICTED_PRODUCT_FAMILY_PREFIX.search(family_prefix) is not None
            and re.fullmatch(r"(?:TOP|BOTTOM)\d+", core, re.IGNORECASE) is None
        ):
            mentions.append((match.start(), match.end(), match.group(0)))
    parenthesized_tickers = list(_PARENTHESIZED_OVERSEAS_TICKER.finditer(question))
    # A KR ISIN is a stronger, unambiguous identity than a short token printed
    # in the product name. Fund class/hedge suffixes such as ``(UH)`` share the
    # same surface shape as an overseas ticker and must not become a second
    # product identity when the official KR code is already present.
    has_kr_identity = any(match.group(0).upper().startswith("KR") for match in known_id_matches)
    mentions.extend(
        (match.start(1), match.end(1), match.group(1))
        for match in parenthesized_tickers
        if not has_kr_identity and match.group(1).upper() not in _RESERVED_OVERSEAS_TICKER_TOKENS
    )
    named_mentions = [
        value
        for value in extract_named_product_mentions(question)
        if _FAMILY_ENUMERATION_MENTION.fullmatch(value) is None
    ]
    mentions.extend(
        (question.find(value), question.find(value) + len(value), value) for value in named_mentions
    )
    mentions.extend(
        (match.start(1), match.end(1), match.group(1))
        for match in _bare_overseas_comparison_pair(question)
    )
    if re.search(r"해외\s*(?:ETF|ETN|ETP)", question, re.IGNORECASE):
        bare_ticker_matches = [
            match
            for match in _BARE_OVERSEAS_TICKER.finditer(question)
            if match.group(1).upper() not in _RESERVED_OVERSEAS_TICKER_TOKENS
            and not any(
                match.start() < known.end() and known.start() < match.end()
                for known in known_id_matches
            )
        ]
        exact_comparison_pair = (
            _COMPARE.search(question) is not None
            and len(parenthesized_tickers) + len(bare_ticker_matches) == 2
        )
        if (not parenthesized_tickers and known_id_matches) or exact_comparison_pair:
            mentions.extend(
                (match.start(1), match.end(1), match.group(1)) for match in bare_ticker_matches
            )
        elif (
            len(bare_ticker_matches) == 1
            and _COMPARE.search(question) is None
            and not ranking_intent_present(question)
            and re.search(
                r"(?:상세|세부|정보|데이터\s*필드|제공\s*필드|정확히\s*조회|"
                r"조회해|확인해)",
                question,
                re.IGNORECASE,
            )
            is not None
        ):
            # A single non-reserved uppercase token in an explicit overseas
            # ETP detail request is a ticker identity, not a free-text acronym.
            # Ranking and comparison requests remain outside this narrow rule.
            match = bare_ticker_matches[0]
            mentions.append((match.start(1), match.end(1), match.group(1)))
    kept = _drop_enclosed_mentions(mentions)
    return _ordered_unique(_normalize_product_mention(value) for _, _, value in sorted(kept))


def _unresolved_identifier_spans(question: str, mentions: list[str]) -> list[str]:
    """Find identifier-shaped spans that were not normalized into an exact identity."""

    normalized_mentions = {mention.casefold() for mention in mentions}
    residuals = []
    for match in _IDENTIFIER_LIKE.finditer(question):
        normalized = _normalize_product_mention(match.group(0))
        if normalized.casefold() not in normalized_mentions:
            residuals.append(match.group(0))
    return _ordered_unique(residuals)


def _bind_product_identity_intent(
    intent: InteractionIntent,
    mentions: list[str],
    question: str,
) -> tuple[InteractionIntent, str | None]:
    """Prevent an explicit product identity from being dropped by a broad route.

    Korean postpositions are deliberately outside the identifier patterns, so
    ``KR...의`` is already captured as the exact ``KR...`` identity.  A single
    identity in an otherwise generic SEARCH is therefore an exact DETAIL, not
    a request to search the whole family.  Multiple identities without an
    explicit comparison, or an identity mixed into an aggregate, remain
    ambiguous and cannot acquire SQL authority.
    """

    if not mentions:
        return intent, None
    if len(_product_families(question)) > 1 and (
        len(_ORDINAL_MULTI_SEARCH.findall(question)) >= 2
        or (
            _EXPLICIT_PER_FAMILY_SEARCH.search(question) is not None
            and not (len(mentions) == 1 and not ranking_intent_present(question))
        )
    ):
        # A single identity cannot turn a multi-family request into DETAIL.
        # Every identity is rebound inside a compiler-validated family clause;
        # unscoped prose still fails closed before Oracle execution.
        return InteractionIntent.SEARCH, None
    if intent is InteractionIntent.SEARCH:
        if len(mentions) == 1:
            if ranking_intent_present(question) and re.search(
                r"(?:보다|대비).{0,80}?(?:다른|나머지|제외한)|"
                r"(?:다른|나머지)\s*(?:상품|종목).{0,80}?(?:순|상위|하위)",
                question,
                re.IGNORECASE,
            ):
                # The exact product supplies a numeric reference threshold;
                # it is deliberately excluded from the returned family set.
                # Keeping SEARCH here lets the typed relative compiler prove
                # the metric, comparison direction and final ranking.
                return InteractionIntent.SEARCH, None
            return InteractionIntent.DETAIL, None
        if 2 <= len(mentions) <= 20 and ranking_intent_present(question):
            return InteractionIntent.SEARCH, None
        return InteractionIntent.CLARIFY, "multiple_product_identities_unbound"
    if intent is InteractionIntent.AGGREGATE:
        if (
            len(mentions) == 1
            and re.search(
                r"(?:알려|확인|보여|원천값|원천\s*데이터)",
                question,
                re.IGNORECASE,
            )
            and re.search(
                r"(?:평균|합계|중앙값|최댓값|최솟값|최대값|최소값)",
                question,
                re.IGNORECASE,
            )
            is None
        ):
            return InteractionIntent.DETAIL, None
        return InteractionIntent.CLARIFY, "aggregate_product_identity_unbound"
    if intent is InteractionIntent.COMPARE and len(mentions) == 1:
        # Conversational preambles often contain ``비교`` even though the
        # actual request asks for fields of one exact product. Conversely, an
        # explicit reference-product screen is a SEARCH over the rest of its
        # family. Bind those structural forms instead of letting one incidental
        # word force a two-product COMPARE contract.
        anchor_screen = (
            re.search(
                r"(?:기준(?:점|\s*상품)?으로|기준으로\s*비교|"
                r"이\s*상품보다|이\s*상품\s*(?:이상|이하|초과|미만))",
                question,
                re.IGNORECASE,
            )
            is not None
            and re.search(
                r"(?:다른|나머지|제외한)\s*상품",
                question,
                re.IGNORECASE,
            )
            is not None
        )
        if anchor_screen and ranking_intent_present(question):
            return InteractionIntent.SEARCH, None
        unresolved_counterpart = re.search(
            r"(?:다른|나머지|경쟁|유사한?|비슷한?)\s*상품",
            question,
            re.IGNORECASE,
        )
        if unresolved_counterpart is not None:
            return InteractionIntent.CLARIFY, "comparison_counterpart_unbound"
        if re.search(
            r"(?:정보|상세|기본\s*정보|원천값|수치|값|기록|"
            r"알려|보여|확인|어떻게|찾아|알고|궁금)",
            question,
            re.IGNORECASE,
        ):
            return InteractionIntent.DETAIL, None
        return InteractionIntent.CLARIFY, "comparison_counterpart_unbound"
    if intent in {InteractionIntent.DETAIL, InteractionIntent.EXPLAIN} and len(mentions) != 1:
        return InteractionIntent.CLARIFY, "multiple_product_identities_unbound"
    return intent, None


def _product_families(question: str) -> list[ProductFamily]:
    semantic_question = re.sub(
        r"(?m)(?:^|(?<=[.!?])\s+)"
        r"국내채권\s*,\s*국내\s*ETF\s*,\s*해외\s*ETF\s*,\s*공모펀드\s*"
        r"순서[^.!?]*(?:[.!?]|$)",
        " ",
        question,
        flags=re.IGNORECASE,
    ).strip()
    if semantic_question and semantic_question != question:
        return _product_families(semantic_question)
    ordinal_clauses = _ordinal_query_clauses(question)
    if ordinal_clauses:
        # Presentation instructions after the final clause often list all four
        # families only to prescribe answer order.  Family authority must come
        # from the actual first/second/... query clauses, never that formatting
        # sentence.
        clause_families = [
            family for clause in ordinal_clauses for family in _product_families(clause)
        ]
        if clause_families:
            return list(dict.fromkeys(clause_families))

    mentions: list[tuple[int, ProductFamily]] = []

    bare_overseas_pair = _bare_overseas_comparison_pair(question)
    if bare_overseas_pair:
        mentions.append((bare_overseas_pair[0].start(1), ProductFamily.OVERSEAS_ETP))

    def add_matches(pattern: str, family: ProductFamily, *, flags: int = 0) -> None:
        mentions.extend((match.start(), family) for match in re.finditer(pattern, question, flags))

    add_matches(
        r"공모\s*펀드|공모펀드|증권(?:자)?투자신탁",
        ProductFamily.FUND,
        flags=re.IGNORECASE,
    )
    add_matches(
        r"국내\s*채권(?!\s*형[^와과,\n]{0,12}?(?:ETF|ETN|ETP))|"
        r"회사채|일반사채|국채|국공채|국고채|특수채|특수은행채|금융채|신용카드채|"
        r"MBS|시설대여채|"
        r"지역개발채|도시철도공채|채권\s*상품|"
        r"채권(?!형|혼합|\s*(?:ETF|ETN|ETP))|"
        r"(?:고정\+변동|고정|변동)\s*금리|이표채|복리채|할인채|단리채",
        ProductFamily.BOND,
        flags=re.IGNORECASE,
    )
    add_matches(
        r"해외\s*상장\s*지수\s*(?:펀드|상품)",
        ProductFamily.OVERSEAS_ETP,
        flags=re.IGNORECASE,
    )
    add_matches(
        r"(?<!해외\s)(?:국내\s*)?상장\s*지수\s*펀드|"
        r"국내\s*상장\s*지수\s*(?:상품|증권)|국내\s*상장\s*상품",
        ProductFamily.DOMESTIC_ETP,
        flags=re.IGNORECASE,
    )

    etp_token = r"(?<![A-Z])(?:ETF|ETN|ETP)(?![A-Z])"
    domestic_pattern = (
        rf"(?:국내|한국|코스피|코스닥)(?!\s*채권)[^와과,\n]{{0,20}}?{etp_token}|"
        rf"{etp_token}[^와과,\n]{{0,30}}?(?:국내|한국)(?!\s*채권)"
    )
    overseas_pattern = (
        rf"(?:해외|글로벌)[^와과,\n]{{0,20}}?{etp_token}|"
        rf"{etp_token}\s*해외"
    )
    domestic_matches = list(re.finditer(domestic_pattern, question, re.IGNORECASE))
    overseas_matches = list(re.finditer(overseas_pattern, question, re.IGNORECASE))
    overseas_matches = [
        match
        for match in overseas_matches
        if re.search(
            rf"(?:국내|한국)[^와과,\n]{{0,20}}?{etp_token}",
            match.group(0),
            re.IGNORECASE,
        )
        is None
    ]
    mentions.extend((match.start(), ProductFamily.DOMESTIC_ETP) for match in domestic_matches)
    mentions.extend((match.start(), ProductFamily.OVERSEAS_ETP) for match in overseas_matches)

    if re.search(etp_token, question, re.IGNORECASE):
        explicit_etp_family = domestic_matches or overseas_matches
        if not explicit_etp_family:
            if re.search(r"해외|글로벌", question, re.IGNORECASE):
                match = re.search(r"해외|글로벌", question, re.IGNORECASE)
                assert match is not None
                mentions.append((match.start(), ProductFamily.OVERSEAS_ETP))
            elif re.search(r"국내|한국", question, re.IGNORECASE):
                match = re.search(r"국내|한국", question, re.IGNORECASE)
                assert match is not None
                mentions.append((match.start(), ProductFamily.DOMESTIC_ETP))
            elif match := re.search(
                r"KR\d{10}|"
                r"(?:1\s*개월|한\s*달|3\s*개월|석\s*달|6\s*개월|반년|"
                r"1\s*년|연초\s*이후|YTD)\s*(?:수익률|수익|성과)|"
                r"(?:20|60|120|252)\s*거래일\s*연환산\s*변동성|"
                r"(?:연환산\s*)?분배\s*수익률|연\s*\d+\s*회\s*분배|"
                r"\d[\d,]*\s*만?\s*원(?:화)?|"
                r"(?:매우높은|높은|다소높은|보통|낮은|매우낮은)"
                r"위험\s*\([1-6]등급\)|연금\s*(?:편입|거래)",
                question,
                re.IGNORECASE,
            ):
                # The official overseas ETP table exposes only 1-day return
                # and no domestic risk/pension fields. These cues therefore
                # identify the domestic ETP schema without model inference.
                mentions.append((match.start(), ProductFamily.DOMESTIC_ETP))
            elif match := re.search(
                r"(?:하루|일간?)\s*거래대금|"
                r"(?:(?:투자\s*)?자산군(?:이|은|가)?\s*"
                r"(?:Commodity|Alternatives|Equity|Money\s*Market|Mixed\s*Assets)|"
                r"(?:Commodity|Alternatives|Equity|Money\s*Market|Mixed\s*Assets)\s*자산군)|"
                r"(?:China|Japan|India|Europe|Global\s*Emerging\s*Markets)\s*투자\s*지역|"
                r"거래소\s*코드(?:가|는|은|이)?\s*(?:NAS|NYS|AMX)|"
                r"(?:NAS|NYS|AMX)(?:의|에서\s*거래|\s*거래소)",
                question,
                re.IGNORECASE,
            ):
                if re.search(r"(?:하루|일간?)\s*거래대금", match.group(0), re.IGNORECASE):
                    mentions.append((match.start(), ProductFamily.DOMESTIC_ETP))
                else:
                    mentions.append((match.start(), ProductFamily.OVERSEAS_ETP))
            elif match := re.search(r"외화\s*거래대금", question, re.IGNORECASE):
                mentions.append((match.start(), ProductFamily.OVERSEAS_ETP))
            elif match := re.search(
                r"합성\s*복제|실물\s*복제|추적\s*오차(?:율)?|괴리율|"
                r"(?:20|60|120|252)\s*(?:거래일|일).{0,12}(?:변동성|흔들림)",
                question,
                re.IGNORECASE,
            ):
                mentions.append((match.start(), ProductFamily.DOMESTIC_ETP))
            elif re.search(
                r"NYSE|NASDAQ|AMEX|나스닥|뉴욕\s*(?:증권\s*)?거래소|아멕스",
                question,
                re.IGNORECASE,
            ):
                match = re.search(
                    r"NYSE|NASDAQ|AMEX|나스닥|뉴욕\s*(?:증권\s*)?거래소|아멕스",
                    question,
                    re.IGNORECASE,
                )
                assert match is not None
                mentions.append((match.start(), ProductFamily.OVERSEAS_ETP))
            elif re.search(r"미국", question):
                match = re.search(r"미국", question)
                assert match is not None
                mentions.append((match.start(), ProductFamily.OVERSEAS_ETP))

    if is_domestic_etp_brand_question(question):
        domestic_brand = re.search(
            r"\b(?:TIGER|KODEX|ACE|RISE|SOL|HANARO|TIMEFOLIO|ARIRANG|KBSTAR)\b",
            question,
            re.IGNORECASE,
        )
        assert domestic_brand is not None
        mentions.append((domestic_brand.start(), ProductFamily.DOMESTIC_ETP))
    fund_surface = re.sub(r"상장\s*지수\s*펀드", " ", question, flags=re.IGNORECASE)
    fund_surface = re.sub(r"펀드\s*통화\s*코드", " ", fund_surface, flags=re.IGNORECASE)
    fund = re.search(r"(?<!사모)\s*펀드", fund_surface, re.IGNORECASE)
    if fund is not None:
        mentions.append((fund.start(), ProductFamily.FUND))

    if ProductFamily.FUND in {family for _, family in mentions} and re.search(
        r"(?:국내|해외)\s*채권\s*(?:구성비율|비중|편입\s*(?:비|비중|비율))",
        question,
        re.IGNORECASE,
    ):
        mentions = [item for item in mentions if item[1] is not ProductFamily.BOND]
    if (
        {ProductFamily.DOMESTIC_ETP, ProductFamily.OVERSEAS_ETP}
        & {family for _, family in mentions}
        and re.search(
            r"(?:채권(?:에|을)\s*투자(?:하는|한)|"
            r"(?:투자\s*)?자산군(?:이|은|가)?\s*채권|"
            r"채권\s*(?:자산\s*)?형|채권\s*(?=ETF|ETN|ETP))"
            r".{0,30}?(?:ETF|ETN|ETP)",
            question,
            re.IGNORECASE,
        )
    ):
        mentions = [item for item in mentions if item[1] is not ProductFamily.BOND]

    if not mentions:
        if match := re.search(
            r"(?:경과이자(?:를)?\s*포함(?:한)?\s*(?:단가|가격)|이자부단가|"
            r"평가일단가|민평(?:에서)?\s*(?:산출|계산)?\s*(?:수익률|금리)|"
            r"표면\s*(?:금리|이율)|컨벡시티|듀레이션)",
            question,
            re.IGNORECASE,
        ):
            mentions.append((match.start(), ProductFamily.BOND))
        elif match := re.search(
            r"(?<![A-Z0-9])[A-Z][A-Z0-9]{0,9}\.[A-Z0-9]{1,5}(?![A-Z0-9])",
            question,
            re.IGNORECASE,
        ):
            mentions.append((match.start(), ProductFamily.OVERSEAS_ETP))

    if not mentions:
        implied = registry_implied_families(question)
        if len(implied) == 1:
            mentions.append((0, ProductFamily(implied[0])))

    # A full multi-clause request can contain one explicitly overseas ETF and a
    # separate bare ``ETF`` whose domestic-only fields identify the domestic
    # schema.  Looking only at the whole sentence suppresses that inference
    # because an explicit ETP family exists somewhere else.  Re-evaluate each
    # ordinal clause in isolation and add only families the deterministic
    # single-clause grammar can resolve.
    ordinal_markers = list(_ORDINAL_MULTI_SEARCH.finditer(question))
    for index, marker in enumerate(ordinal_markers):
        clause_end = (
            ordinal_markers[index + 1].start()
            if index + 1 < len(ordinal_markers)
            else len(question)
        )
        clause = question[marker.end() : clause_end].strip()
        if clause:
            for family in _product_families(clause):
                mentions.append((marker.end(), family))

    ordered = [family for _, family in sorted(mentions, key=lambda item: item[0])]
    return list(dict.fromkeys(ordered))


def _requested_limit(question: str) -> int | None:
    return requested_result_limit(question)


def _intent(question: str, families: list[ProductFamily]) -> InteractionIntent:
    semantic_question = semantic_query_surface(question)
    policy_surface = safety_policy_surface(semantic_question)
    after_tax_yield_alias = any(
        item.field_id == "after_tax_yield_pct"
        for family in families
        for item in typed_alias_matches(semantic_question, family)
        if item.operation == "rank"
    )
    if (
        _UNSUPPORTED.search(policy_surface)
        or (_EXTERNAL_POLICY.search(semantic_question) and not after_tax_yield_alias)
        or _UNAVAILABLE_MARKET_DATA.search(semantic_question)
        or (
            families == [ProductFamily.OVERSEAS_ETP]
            and _OVERSEAS_UNAVAILABLE_METRIC.search(semantic_question)
            and not _COMPARE.search(semantic_question)
        )
        or (ProductFamily.FUND in families and _FUND_UNAVAILABLE_DETAIL.search(semantic_question))
    ):
        return InteractionIntent.UNSUPPORTED
    product_mentions = _product_mentions(question)
    exact_two_product_comparison = bool(
        len(product_mentions) == 2
        and (_COMPARE.search(semantic_question) or _TWO_ID_COMPARATIVE.search(semantic_question))
    )
    resolved_rank_alias = any(
        item.operation == "rank"
        for family in families
        for item in typed_alias_matches(semantic_question, family)
    )
    if (_AMBIGUOUS.search(policy_surface) and not resolved_rank_alias) or (
        _MIXED_ETP_COST.search(semantic_question)
        and _EXPLICIT_ETP_TYPE.search(semantic_question) is None
        and _EXPLICIT_TOTAL_EXPENSE_RATIO.search(semantic_question) is None
        and not exact_two_product_comparison
    ):
        return InteractionIntent.CLARIFY
    if (
        not product_mentions
        and _GROUPED_VALUE_AGGREGATE.search(semantic_question)
        and not _GROUPED_PRODUCT_RANKING.search(semantic_question)
    ):
        # ``자산군별 최대 AUM`` asks for one aggregate value per category.
        # Ranking aliases also recognize ``최대 AUM``, so grouped value
        # aggregates must claim this grammar before the ranked-product branch.
        # Explicit product-ranking language still remains a SEARCH.
        return InteractionIntent.AGGREGATE
    if (
        not product_mentions
        and _AGGREGATE.search(semantic_question)
        and (
            not ranking_intent_present(semantic_question)
            or _GROUP_LABEL_ORDER.search(semantic_question) is not None
        )
        # The same allocation-field guard the later branch applies. "비중" is an
        # aggregate cue but also the tail of 국내주식 비중 and its three siblings,
        # so a plain threshold question reached this branch first and was refused
        # for having no aggregate function.
        and not (
            _PORTFOLIO_RATIO_FIELD.search(semantic_question)
            and re.search(
                r"(?:이상|이하|초과|미만|넘|보다\s*(?:크|작))",
                semantic_question,
            )
        )
    ):
        # An explicit aggregate function owns a bare metric mention.  Typed
        # aliases may recognize the metric phrase itself (for example
        # ``매수수익``), but without an ordering cue that must not convert
        # ``매수수익률 평균`` into a ranked SEARCH.
        return InteractionIntent.AGGREGATE
    if not product_mentions and (
        resolved_rank_alias
        or (
            ranking_intent_present(semantic_question)
            and re.search(
                r"(?:TOP|상위|하위|오름차순|내림차순|순(?:서)?(?:으로|대로)?|"
                r"높은|낮은|큰|작은|많은|적은|싼|저렴한)",
                semantic_question,
                re.IGNORECASE,
            )
        )
    ):
        # Conversational endings such as ``뭐야?`` do not turn a fully
        # specified ranked universe query into a definition request.  No
        # product identity is bound here, so a genuine product comparison is
        # still handled by the dedicated branch below.
        return InteractionIntent.SEARCH
    if not product_mentions and families and re.search(
        r"-?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?\s*(?:%|퍼센트|원|달러|년|일)?"
        r"\s*(?:은|는|이|가|을|를)?\s*"
        r"(?:이상|이하|초과|미만|넘지\s*않|안\s*넘|보다\s*(?:크|작|높|낮))",
        semantic_question,
        re.IGNORECASE,
    ):
        # ``NAV 대비 가격 차이가 0.3%를 안 넘는 ...`` is a literal
        # threshold search, not a two-product comparison merely because it
        # contains the relational word ``대비``.
        return InteractionIntent.SEARCH
    if _DEFINITION.search(semantic_question):
        return InteractionIntent.EXPLAIN
    if len(families) > 1 and (
        len(_ORDINAL_MULTI_SEARCH.findall(semantic_question)) >= 2
        or (
            _CROSS_FAMILY_GLOBAL_SEARCH.search(semantic_question) is not None
            and ranking_intent_present(semantic_question)
        )
    ):
        # An explicit multi-clause request or a global TOP-K is a SEARCH batch,
        # not an unconstrained product-to-product comparison.  The compiler
        # still has to isolate every clause and prove common metric/unit safety.
        return InteractionIntent.SEARCH
    if _RANKED_SET_SEARCH.search(semantic_question) and len(product_mentions) != 2:
        return InteractionIntent.SEARCH
    if len(product_mentions) >= 3 and ranking_intent_present(semantic_question):
        return InteractionIntent.SEARCH
    if not product_mentions and ranking_intent_present(semantic_question):
        # A metric-ranked universe query remains SEARCH even when its
        # conversational preamble says "대조" or "비교표".  Exact two-product
        # comparisons are handled above and retain their dedicated contract.
        return InteractionIntent.SEARCH
    if ranking_intent_present(semantic_question) and re.search(
        r"(?:상위\s*목록과\s*필터\s*통과\s*건수|"
        r"(?:전체|총)\s*(?:후보|개수|건수)|후보(?:가|는|를)?\s*(?:총|몇)|"
        r"조건(?:에\s*부합하|을\s*통과하)는?\s*(?:전체\s*)?(?:개수|건수|상품\s*수)|"
        r"조건을\s*통과한\s*상품\s*수)",
        semantic_question,
        re.IGNORECASE,
    ):
        return InteractionIntent.SEARCH
    if (
        len(product_mentions) == 1
        and ranking_intent_present(semantic_question)
        and re.search(
            r"(?:보다|대비).{0,80}?(?:다른|나머지|제외한)|"
            r"(?:다른|나머지)\s*(?:상품|종목).{0,80}?(?:순|상위|하위)",
            semantic_question,
            re.IGNORECASE,
        )
    ):
        # The named product supplies a comparison threshold, not the result
        # identity.  Treating it as DETAIL discards the requested universe and
        # blocks the server-owned relative-search compiler.
        return InteractionIntent.SEARCH
    if (
        len(product_mentions) == 1
        and _DETAIL.search(semantic_question)
        and _EXPLAIN.search(semantic_question) is None
    ):
        return InteractionIntent.DETAIL
    if _COMPARE.search(semantic_question) or (
        len(product_mentions) == 2 and _TWO_ID_COMPARATIVE.search(semantic_question)
    ):
        return InteractionIntent.COMPARE
    # In the official fund schema, "해외채권 비중" is a row-level numeric
    # field.  The bare word "비중" must not turn a filtered/ranked product
    # search into a distribution aggregate.
    if _PORTFOLIO_RATIO_FIELD.search(semantic_question) and (
        ranking_intent_present(semantic_question)
        or re.search(
            r"(?:이상|이하|초과|미만|넘|보다\s*(?:크|작))",
            semantic_question,
        )
    ):
        return InteractionIntent.SEARCH
    if _AGGREGATE.search(semantic_question):
        return InteractionIntent.AGGREGATE
    if _TICKER_ORDERING.search(semantic_question):
        return InteractionIntent.SEARCH
    if (
        _EXPLAIN.search(semantic_question)
        and not resolved_rank_alias
        and not ranking_intent_present(semantic_question)
    ):
        return InteractionIntent.EXPLAIN
    if _DETAIL.search(semantic_question):
        return InteractionIntent.DETAIL
    return InteractionIntent.SEARCH


def classify_interaction_intent(question: str) -> InteractionIntent:
    """Classify a normalized question without emitting Audit or planning events.

    Public specialist routers use this read-only classifier only to decide whether
    they may handle a SEARCH.  Every other intent remains owned by ``IntentRouter``
    so adding a specialist cannot preempt DETAIL, COMPARE, AGGREGATE or EXPLAIN.
    """

    stripped = question.strip()
    if not stripped:
        raise ValueError("question cannot be blank")
    return _intent(stripped, _product_families(stripped))


def extract_product_routing_hints(
    question: str,
) -> tuple[tuple[ProductFamily, ...], tuple[str, ...]]:
    """Return lexical family/identity hints without routing side effects.

    External evidence planning needs the main Router's product surfaces, but a
    second Router invocation would repeat Safety, PlanningPolicy and Audit.
    These hints therefore carry no execution authority.
    """

    stripped = question.strip()
    if not stripped:
        return (), ()
    return tuple(_product_families(stripped)), tuple(_product_mentions(stripped))


class IntentRouter:
    """Deterministic, fail-closed router in front of any model-generated plan."""

    def __init__(
        self,
        matrix: CapabilityMatrix | None = None,
        *,
        safety_envelope: SafetyEnvelope | None = None,
        semantic_coverage_gate: SemanticCoverageGate | None = None,
        planning_policy: PlanningPolicy | None = None,
        hclx_planning_enabled: bool = False,
        adaptive_semantic_enabled: bool = False,
    ) -> None:
        if type(hclx_planning_enabled) is not bool:
            raise TypeError("hclx_planning_enabled must be a boolean")
        if hclx_planning_enabled:
            raise ValueError("legacy HCLX QueryPlan execution is retired")
        if type(adaptive_semantic_enabled) is not bool:
            raise TypeError("adaptive_semantic_enabled must be a boolean")
        self.matrix = matrix or load_capability_matrix()
        self.safety_envelope = safety_envelope or SafetyEnvelope()
        self.semantic_coverage_gate = semantic_coverage_gate or SemanticCoverageGate(
            schema_link_gaps_enabled=adaptive_semantic_enabled
        )
        self.adaptive_semantic_enabled = adaptive_semantic_enabled
        self._authority_planning_policy = AdaptiveShadowPlanningPolicy()
        self.planning_policy = planning_policy or self._authority_planning_policy

    def route(self, question: str, request_id: str) -> RouteDecision:
        """Return the frozen legacy contract while evaluating Stage 1 shadow policy."""

        return self.route_with_planning(question, request_id).route_decision

    def route_with_planning(self, question: str, request_id: str) -> PlanningTrace:
        """Pair the legacy route with non-enforcing, server-owned shadow metadata."""

        return self._evaluate_with_planning(question, request_id)

    def _planning_trace(
        self,
        route_decision: RouteDecision,
        coverage: SemanticCoverageDecision,
    ) -> PlanningTrace:
        started = perf_counter()
        # Pydantic frozen models are shallow: nested product-family and mention
        # lists can still be changed in place. Snapshot the authoritative route
        # before an injected shadow policy sees an isolated copy.
        trusted_route = RouteDecision.model_validate_json(route_decision.model_dump_json())
        try:
            planning_coverage = coverage
            if (
                trusted_route.draft.intent
                in {InteractionIntent.CLARIFY, InteractionIntent.UNSUPPORTED}
                and not coverage.unsupported_spans
            ):
                # The legacy gate intentionally skips deeper scanning when the
                # lexical router already chose a control intent. Shadow policy
                # performs an additional read-only scan so an unsupported
                # action (for example CSV export) still outranks ambiguity,
                # without changing the frozen legacy RouteDecision.
                supplemental = self.semantic_coverage_gate.evaluate(
                    trusted_route.draft.question,
                    interaction_intent=None,
                    check_exclusions=False,
                )
                if supplemental.unsupported_spans:
                    planning_coverage = SemanticCoverageDecision(
                        ambiguity_spans=coverage.ambiguity_spans,
                        unsupported_spans=tuple(
                            dict.fromkeys(
                                (
                                    *coverage.unsupported_spans,
                                    *supplemental.unsupported_spans,
                                )
                            )
                        ),
                        schema_link_gap_spans=coverage.schema_link_gap_spans,
                    )
            authoritative = self._authority_planning_policy.decide(
                RouteDecision.model_validate_json(trusted_route.model_dump_json()),
                planning_coverage,
            )
            candidate = authoritative
            if self.planning_policy is not self._authority_planning_policy:
                candidate = self.planning_policy.decide(
                    RouteDecision.model_validate_json(trusted_route.model_dump_json()),
                    planning_coverage,
                )
            if not isinstance(candidate, PlanningDecision):
                raise TypeError("planning policy must return PlanningDecision")
            # Pydantic model_copy(update=...) does not re-run validators. Treat
            # an injected policy as untrusted and rebuild from plain data before
            # accepting its authority flags.
            planning_decision = PlanningDecision.model_validate(candidate.model_dump(mode="python"))
            if planning_decision != authoritative:
                raise ValueError("planning policy differs from adaptive-shadow-v1 authority")
            semantic_ledger = build_resolved_span_ledger(
                question=trusted_route.draft.question,
                interaction_intent=trusted_route.draft.intent,
                product_families=tuple(trusted_route.draft.product_families),
                coverage=planning_coverage,
            )
            trace = PlanningTrace(
                route_decision=trusted_route,
                planning_decision=planning_decision,
                semantic_ledger=semantic_ledger,
            )
        except Exception:
            # A malformed or escalating policy result must not reach Compiler,
            # HCLX, SQL, or Oracle. Existing control reasons remain intact; an
            # otherwise executable route is converted to a stable fail-closed
            # response without exposing the exception text.
            if trusted_route.disposition is RouteDisposition.EXECUTE:
                closed_draft = MinimalQueryDraft.model_validate(
                    {
                        **trusted_route.draft.model_dump(mode="python"),
                        "intent": InteractionIntent.UNSUPPORTED,
                    }
                )
                trusted_route = self._control(
                    closed_draft,
                    RouteDisposition.UNSUPPORTED,
                    "planning_policy_error",
                    "내부 계획 정책을 안전하게 검증하지 못해 요청을 실행하지 않았습니다.",
                )
            planning_decision = PlanningDecision.fail_closed(trusted_route)
            trace = PlanningTrace(
                route_decision=trusted_route,
                planning_decision=planning_decision,
                semantic_ledger=None,
            )
        audit = current_request_audit()
        if audit is not None:
            audit.emit(
                stage=AuditStage.PLANNING,
                outcome=(
                    AuditOutcome.BLOCKED
                    if planning_decision.decision_status is PlanningDecisionStatus.POLICY_ERROR
                    else AuditOutcome.SUCCEEDED
                ),
                reason_code=(
                    "policy_blocked"
                    if planning_decision.decision_status is PlanningDecisionStatus.POLICY_ERROR
                    else "policy_completed"
                ),
                duration_ms=(perf_counter() - started) * 1000,
                route_disposition=trusted_route.disposition,
                interaction_intent=(
                    InteractionIntent.UNSUPPORTED
                    if trusted_route.disposition is RouteDisposition.UNSUPPORTED
                    else trusted_route.draft.intent
                ),
                product_families=trusted_route.draft.product_families,
            )
        return trace

    def _evaluate_with_planning(
        self,
        question: str,
        request_id: str,
    ) -> PlanningTrace:
        safety_started = perf_counter()
        try:
            safety = self.safety_envelope.evaluate(question)
        except Exception:
            audit = current_request_audit()
            if audit is not None:
                audit.emit(
                    stage=AuditStage.SAFETY,
                    outcome=AuditOutcome.FAILED,
                    reason_code="guard_failed",
                    duration_ms=(perf_counter() - safety_started) * 1000,
                )
            raise
        audit = current_request_audit()
        if audit is not None:
            if safety.blocked:
                try:
                    audit.sink.metrics.increment(MetricCounter.SAFETY_BLOCKS)
                except Exception:
                    # Metrics are operational telemetry, never routing authority.
                    pass
            audit.emit(
                stage=AuditStage.SAFETY,
                outcome={
                    SafetyDisposition.ALLOW: AuditOutcome.SUCCEEDED,
                    SafetyDisposition.CLARIFY: AuditOutcome.CLARIFIED,
                    SafetyDisposition.UNSUPPORTED: AuditOutcome.UNSUPPORTED,
                }[safety.disposition],
                reason_code=(
                    "guard_allowed" if safety.gate is None else f"guard_{safety.gate.value}"
                ),
                duration_ms=(perf_counter() - safety_started) * 1000,
            )
        stripped = safety.normalized_question
        if not stripped:
            raise ValueError("question cannot be blank")
        if not request_id.strip():
            raise ValueError("request_id cannot be blank")

        lexical_started = perf_counter()
        if audit is not None:
            try:
                audit.sink.metrics.increment(MetricCounter.LEXICAL_CALLS)
            except Exception:
                # A broken metrics backend must not change the route decision.
                pass
        families = _product_families(stripped)
        mentions = _product_mentions(stripped)
        identifier_residuals = _unresolved_identifier_spans(stripped, mentions)
        lexical_intent = _intent(stripped, families)
        intent, identity_control = _bind_product_identity_intent(
            lexical_intent,
            mentions,
            stripped,
        )
        # Server-recognized names are identity-bearing just like product codes.
        # Unsupported knowledge requirements are still checked before the
        # coverage gate applies this DETAIL-specific branch.
        coverage_intent = intent if mentions else lexical_intent
        requested_limit = _requested_limit(stripped)
        if audit is not None:
            audit.emit(
                stage=AuditStage.LEXICAL,
                outcome=AuditOutcome.SUCCEEDED,
                reason_code="lexical_completed",
                duration_ms=(perf_counter() - lexical_started) * 1000,
                product_families=families,
            )
        cross_family_control = len(families) > 1 and intent is not InteractionIntent.SEARCH
        ordinal_clauses = (
            _ordinal_query_clauses(stripped)
            if len(families) > 1 and intent is InteractionIntent.SEARCH
            else ()
        )
        if ordinal_clauses:
            clause_coverage = []
            for clause in ordinal_clauses:
                clause_mentions = _product_mentions(clause)
                clause_intent, _ = _bind_product_identity_intent(
                    _intent(clause, _product_families(clause)),
                    clause_mentions,
                    clause,
                )
                clause_coverage.append(
                    self.semantic_coverage_gate.evaluate(
                        clause,
                        interaction_intent=clause_intent.value,
                        check_exclusions=False,
                    )
                )
            coverage = SemanticCoverageDecision(
                ambiguity_spans=tuple(
                    _ordered_unique(
                        span for item in clause_coverage for span in item.ambiguity_spans
                    )
                ),
                unsupported_spans=tuple(
                    _ordered_unique(
                        span for item in clause_coverage for span in item.unsupported_spans
                    )
                ),
                schema_link_gap_spans=tuple(
                    _ordered_unique(
                        span for item in clause_coverage for span in item.schema_link_gap_spans
                    )
                ),
            )
        else:
            coverage = self.semantic_coverage_gate.evaluate(
                stripped,
                interaction_intent=coverage_intent.value,
                check_exclusions=False,
            )
        if (
            coverage_intent in {InteractionIntent.CLARIFY, InteractionIntent.UNSUPPORTED}
            and not coverage.unsupported_spans
        ):
            # Unsupported actions have terminal precedence over a lexical
            # ambiguity.  The intent-aware gate deliberately skips deep search
            # semantics for an already controlled request, so scan once more
            # only for unsupported actions (for example CSV export) and never
            # use this pass to invent executable meaning.
            terminal_action_coverage = self.semantic_coverage_gate.evaluate(
                stripped,
                interaction_intent=None,
                check_exclusions=False,
            )
            if terminal_action_coverage.unsupported_spans:
                coverage = SemanticCoverageDecision(
                    ambiguity_spans=coverage.ambiguity_spans,
                    unsupported_spans=terminal_action_coverage.unsupported_spans,
                    schema_link_gap_spans=coverage.schema_link_gap_spans,
                )
        if safety.disposition is SafetyDisposition.UNSUPPORTED or coverage.unsupported_spans:
            intent = InteractionIntent.UNSUPPORTED
        elif safety.disposition is SafetyDisposition.CLARIFY or (
            coverage.ambiguity_spans and not cross_family_control
        ):
            intent = InteractionIntent.CLARIFY
        if len(families) > 1 and intent is not InteractionIntent.SEARCH:
            # Control responses do not execute a family sequence. Keep their DTO order
            # stable across router vocabulary changes and preserve the frozen contract.
            families = sorted(families, key=_CONTROL_FAMILY_PRIORITY.__getitem__)
        draft = MinimalQueryDraft(
            request_id=request_id,
            question=stripped,
            intent=intent,
            product_families=families,
            product_mentions=mentions,
            requested_limit=requested_limit,
        )

        if safety.blocked:
            assert safety.gate is not None
            assert safety.reason is not None
            disposition = (
                RouteDisposition.CLARIFY
                if safety.disposition is SafetyDisposition.CLARIFY
                else RouteDisposition.UNSUPPORTED
            )
            return self._planning_trace(
                self._control(
                    draft,
                    disposition,
                    f"safety_{safety.gate.value}",
                    safety.reason,
                ),
                coverage,
            )
        if coverage.unsupported_spans:
            spans = ", ".join(coverage.unsupported_spans)
            return self._planning_trace(
                self._control(
                    draft,
                    RouteDisposition.UNSUPPORTED,
                    "semantic_unmapped_action",
                    (f"읽기 전용 데이터 조회로 실행할 수 없는 조건·행동: {spans}")[:500],
                ),
                coverage,
            )
        if coverage.ambiguity_spans and not cross_family_control:
            spans = ", ".join(coverage.ambiguity_spans)
            return self._planning_trace(
                self._control(
                    draft,
                    RouteDisposition.CLARIFY,
                    "semantic_coverage_incomplete",
                    f"필드나 기준을 하나로 확정할 수 없는 조건: {spans}"[:500],
                ),
                coverage,
            )
        if identifier_residuals:
            return self._planning_trace(
                self._control(
                    draft.model_copy(update={"intent": InteractionIntent.CLARIFY}),
                    RouteDisposition.CLARIFY,
                    "unresolved_product_identity_span",
                    "상품 식별자처럼 보이는 표현을 정확한 상품코드로 확정할 수 없음",
                ),
                coverage,
            )
        if not families and not mentions and _FINANCE_SCOPE_SIGNAL.search(stripped) is None:
            return self._planning_trace(
                self._control(
                    draft,
                    RouteDisposition.UNSUPPORTED,
                    "safety_scope",
                    "현재 서비스는 승인된 금융상품 데이터 조회만 지원합니다.",
                ),
                coverage,
            )
        if intent is InteractionIntent.UNSUPPORTED:
            return self._planning_trace(
                self._control(
                    draft,
                    RouteDisposition.UNSUPPORTED,
                    "prohibited_financial_request",
                    "제공 데이터로 검증할 수 없는 예측·보장·단정적 추천 요청",
                ),
                coverage,
            )
        if intent is InteractionIntent.CLARIFY:
            if identity_control is not None:
                return self._planning_trace(
                    self._control(
                        draft,
                        RouteDisposition.CLARIFY,
                        identity_control,
                        (
                            "상품 식별자가 다른 조건과 함께 남아 있어 실행 범위를 "
                            "하나로 확정할 수 없음"
                        ),
                    ),
                    coverage,
                )
            return self._planning_trace(
                self._control(
                    draft,
                    RouteDisposition.CLARIFY,
                    "subjective_condition",
                    "판단 기준이나 임계값이 명시되지 않은 주관적 조건",
                ),
                coverage,
            )
        if not families:
            return self._planning_trace(
                self._control(
                    draft,
                    RouteDisposition.CLARIFY,
                    "ambiguous_product_family",
                    "실행할 상품군을 확정할 수 없음",
                ),
                coverage,
            )
        if len(families) > 1:
            if intent is not InteractionIntent.SEARCH:
                return self._planning_trace(
                    self._control(
                        draft,
                        RouteDisposition.CLARIFY,
                        "ambiguous_product_family",
                        "복수 상품군은 현재 상품군별 독립 검색만 지원",
                    ),
                    coverage,
                )
            capabilities = [
                self.matrix.require(family, InteractionIntent.SEARCH) for family in families
            ]
            blocked = next(
                (
                    capability
                    for capability in capabilities
                    if capability.status != "executable"
                    or capability.query_plan_intent is not Intent.SEARCH
                ),
                None,
            )
            if blocked is not None:
                return self._planning_trace(
                    self._control(
                        draft,
                        RouteDisposition.UNSUPPORTED,
                        "capability_not_implemented",
                        blocked.reason,
                    ),
                    coverage,
                )
            return self._planning_trace(
                RouteDecision(
                    draft=draft,
                    disposition=RouteDisposition.EXECUTE,
                    reason_code="cross_family_search_executable",
                    reason="복수 상품군을 각각 독립적으로 검색하고 검증",
                    query_plan_intent=Intent.SEARCH,
                    capability_matrix_version=self.matrix.matrix_version,
                ),
                coverage,
            )
        if intent in {InteractionIntent.DETAIL, InteractionIntent.EXPLAIN} and len(mentions) != 1:
            return self._planning_trace(
                self._control(
                    draft,
                    RouteDisposition.CLARIFY,
                    "missing_product_identity",
                    "상세 조회·설명에는 정확한 상품번호나 종목코드가 필요",
                ),
                coverage,
            )
        if intent is InteractionIntent.COMPARE and not 2 <= len(mentions) <= 3:
            return self._planning_trace(
                self._control(
                    draft,
                    RouteDisposition.CLARIFY,
                    "missing_product_identity",
                    "비교에는 서로 다른 2~3개 상품의 정확한 식별자가 필요",
                ),
                coverage,
            )

        capability = self.matrix.require(families[0], intent)
        if capability.status == "unsupported":
            return self._planning_trace(
                self._control(
                    draft,
                    RouteDisposition.UNSUPPORTED,
                    "capability_not_implemented",
                    capability.reason,
                ),
                coverage,
            )
        if capability.status == "control":
            disposition = (
                RouteDisposition.CLARIFY
                if intent is InteractionIntent.CLARIFY
                else RouteDisposition.UNSUPPORTED
            )
            return self._planning_trace(
                self._control(
                    draft,
                    disposition,
                    f"{intent.value}_control",
                    capability.reason,
                ),
                coverage,
            )
        return self._planning_trace(
            RouteDecision(
                draft=draft,
                disposition=RouteDisposition.EXECUTE,
                reason_code="capability_executable",
                reason=capability.reason,
                query_plan_intent=capability.query_plan_intent,
                capability_matrix_version=self.matrix.matrix_version,
            ),
            coverage,
        )

    def _control(
        self,
        draft: MinimalQueryDraft,
        disposition: RouteDisposition,
        reason_code: str,
        reason: str,
    ) -> RouteDecision:
        return RouteDecision(
            draft=draft,
            disposition=disposition,
            reason_code=reason_code,
            reason=reason,
            query_plan_intent=None,
            capability_matrix_version=self.matrix.matrix_version,
        )
