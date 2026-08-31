from __future__ import annotations

import re
from collections.abc import Iterable
from time import perf_counter

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
    r"비교(?!\s*표)|대조|차이|나란히|versus|\bvs\b|"
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
    r"몇\s*개|개수(?!\s*[:：]\s*\d+)|건수|평균|합계|총합|집계|분포|비중|"
    r"(?:상품|ETF|ETN|ETP|채권|펀드)(?:의)?\s*수(?:를|가|는)?\s*(?:계산|집계|총합|알려)|"
    r"최댓값|최대값|최솟값|최소값|최고값|최저값|"
    r"(?:AUM|보수율|수익률|이율|잔존일수|듀레이션)\s*(?:최대|최소)",
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
    r"오늘.*가장.*(?:오른|상승)|"
    r"국내주식.*해외주식|해외주식.*국내주식",
    re.IGNORECASE,
)
_EXTERNAL_POLICY = re.compile(r"세율|세금|과세|거래\s*수수료", re.IGNORECASE)
_UNAVAILABLE_MARKET_DATA = re.compile(
    r"배당.*(?:가장|최대)|"
    r"우주항공.*수익률|하락장.*잘\s*버티|"
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
    r"연초\s*이후)\s*(?:수익률|수익|성과))|변동성|하락장|오른|S&P\s*500",
    re.IGNORECASE,
)
_FINANCE_SCOPE_SIGNAL = re.compile(
    r"(?<![A-Z])(?:ETF|ETN|ETP)(?![A-Z])|공모\s*펀드|국내\s*채권|"
    r"(?:금융|투자)\s*상품|"
    r"상품\s*(?:군|유형|목록|정보|조회|검색|추천|을|를|이|가|은|는|중|있(?:어|나|나요))|"
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
    r"(?<![A-Z0-9])(?:KR[A-Z0-9]{10}|(?:[A-Z]{2,5}|[0-9]{3}):[A-Z0-9._-]+|"
    r"[A-Z][A-Z0-9]{0,9}\.[A-Z0-9]{1,5})(?![A-Z0-9])",
    re.IGNORECASE,
)
_BARE_OVERSEAS_TICKER = re.compile(r"(?<![A-Z0-9.])([A-Z]{2,5})(?![A-Z0-9.])")
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
_LABELED_ID = re.compile(
    r"(?:상품\s*(?:번호|ID|아이디)|종목\s*(?:코드|번호)|티커)"
    r"(?:가|는|은|이)?\s*[:：]?\s*"
    r"([A-Z0-9._:-]{2,30})",
    re.IGNORECASE,
)
_IDENTIFIER_LIKE = re.compile(
    r"(?<![A-Z0-9])(?:KR[A-Z0-9]{4,30}|"
    r"(?:[A-Z]{2,5}|[0-9]{3}):[A-Z0-9._-]{1,24}|"
    r"[A-Z][A-Z0-9]{0,9}\.[A-Z0-9]{1,5})(?![A-Z0-9])",
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


def _normalize_product_mention(value: str) -> str:
    """Detach a Korean postposition only when the remaining token is a known ID form."""

    stripped = value.strip()
    for suffix in _IDENTIFIER_POSTPOSITIONS:
        if stripped.endswith(suffix):
            candidate = stripped[: -len(suffix)].strip()
            if _KNOWN_ID.fullmatch(candidate) is not None:
                return candidate
    return stripped


def _product_mentions(question: str) -> list[str]:
    mentions: list[tuple[int, str]] = []
    for pattern in _QUOTED:
        mentions.extend((match.start(), match.group(1)) for match in pattern.finditer(question))
    known_id_matches = list(_KNOWN_ID.finditer(question))
    mentions.extend((match.start(), match.group(0)) for match in known_id_matches)
    mentions.extend((match.start(), match.group(1)) for match in _LABELED_ID.finditer(question))
    named_mentions = extract_named_product_mentions(question)
    mentions.extend((question.find(value), value) for value in named_mentions)
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
        if known_id_matches or (
            len(bare_ticker_matches) == 2 and _COMPARE.search(question) is not None
        ):
            mentions.extend((match.start(), match.group(1)) for match in bare_ticker_matches)
    return _ordered_unique(_normalize_product_mention(value) for _, value in sorted(mentions))


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
        intent is InteractionIntent.SEARCH
        or len(_ORDINAL_MULTI_SEARCH.findall(question)) >= 2
        or _EXPLICIT_PER_FAMILY_SEARCH.search(question) is not None
    ):
        # A single identity cannot turn a multi-family request into DETAIL.
        # Every identity is rebound inside a compiler-validated family clause;
        # unscoped prose still fails closed before Oracle execution.
        return InteractionIntent.SEARCH, None
    if intent is InteractionIntent.SEARCH:
        if len(mentions) == 1:
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

    def add_matches(pattern: str, family: ProductFamily, *, flags: int = 0) -> None:
        mentions.extend((match.start(), family) for match in re.finditer(pattern, question, flags))

    add_matches(r"공모\s*펀드|공모펀드", ProductFamily.FUND, flags=re.IGNORECASE)
    add_matches(
        r"국내\s*채권(?!\s*형[^와과,\n]{0,12}?(?:ETF|ETN|ETP))|"
        r"회사채|일반사채|국채|국공채|국고채|특수채|특수은행채|금융채|신용카드채|"
        r"MBS|시설대여채|"
        r"지역개발채|도시철도공채|채권\s*상품|채권(?!형)",
        ProductFamily.BOND,
        flags=re.IGNORECASE,
    )
    add_matches(r"(?:국내\s*)?상장\s*지수\s*펀드", ProductFamily.DOMESTIC_ETP)

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
                r"(?:NAS|NYS|AMX)에서\s*거래",
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
            elif re.search(r"NYSE|NASDAQ|AMEX", question, re.IGNORECASE):
                match = re.search(r"NYSE|NASDAQ|AMEX", question, re.IGNORECASE)
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
    domestic_underlying = re.search(
        r"(?:삼성전자.*하이닉스|하이닉스.*삼성전자).*레버리지",
        question,
        re.IGNORECASE,
    )
    if domestic_underlying is not None:
        mentions.append((domestic_underlying.start(), ProductFamily.DOMESTIC_ETP))
    overseas_ticker = re.search(r"(?<![A-Z])SOXL(?![A-Z])", question, re.IGNORECASE)
    if overseas_ticker is not None:
        mentions.append((overseas_ticker.start(), ProductFamily.OVERSEAS_ETP))

    fund_surface = re.sub(r"상장\s*지수\s*펀드", " ", question, flags=re.IGNORECASE)
    fund_surface = re.sub(r"펀드\s*통화\s*코드", " ", fund_surface, flags=re.IGNORECASE)
    fund = re.search(r"(?<!사모)\s*펀드", fund_surface, re.IGNORECASE)
    if fund is not None:
        mentions.append((fund.start(), ProductFamily.FUND))

    if ProductFamily.FUND in {family for _, family in mentions} and re.search(
        r"(?:국내|해외)\s*채권\s*(?:구성비율|비중)",
        question,
        re.IGNORECASE,
    ):
        mentions = [item for item in mentions if item[1] is not ProductFamily.BOND]
    if ProductFamily.DOMESTIC_ETP in {family for _, family in mentions} and re.search(
        r"채권(?:에|을)\s*투자(?:하는|한).{0,30}?(?:ETF|ETN|ETP)",
        question,
        re.IGNORECASE,
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
        if not families and _FINANCE_SCOPE_SIGNAL.search(stripped) is None:
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
        if intent is InteractionIntent.COMPARE and len(mentions) != 2:
            return self._planning_trace(
                self._control(
                    draft,
                    RouteDisposition.CLARIFY,
                    "missing_product_identity",
                    "비교에는 서로 다른 두 상품의 정확한 식별자가 필요",
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
