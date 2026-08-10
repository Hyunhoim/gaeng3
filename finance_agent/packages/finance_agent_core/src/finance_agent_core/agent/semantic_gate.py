from __future__ import annotations

import re
from dataclasses import dataclass

from finance_agent_core.agent.safety import data_scope_policy_surface, normalize_user_question


@dataclass(frozen=True)
class SemanticCoverageDecision:
    """Meaning-bearing spans that cannot yet acquire execution authority."""

    ambiguity_spans: tuple[str, ...] = ()
    unsupported_spans: tuple[str, ...] = ()

    @property
    def blocked(self) -> bool:
        return bool(self.ambiguity_spans or self.unsupported_spans)


_TRANSACTIONAL_ACTION = re.compile(
    r"(?:매수|매도|구매|판매|주문|가입|환매)(?:를|을)?\s*"
    r"(?:실행|처리|진행|해\s*줘|해주세요|해라|하자|해\s*주세요)|"
    r"(?:사|구입(?:해)?|매입(?:해)?|구매(?:해)?)\s*"
    r"(?:줘|주세요|주십시오|달라)|"
    r"(?:팔아|처분(?:해)?|환매(?:해)?)\s*(?:줘|주세요|주십시오|달라)|"
    r"(?:주문|오더).{0,24}?(?:넣어|걸어|접수해)\s*"
    r"(?:줘|주세요|주십시오|달라)|"
    r"(?:체결|결제)(?:해)?\s*(?:줘|주세요|주십시오|달라)|"
    r"(?:알림|푸시|메일|문자).{0,16}?(?:설정|등록|보내|전송)|"
    r"(?:CSV|엑셀|파일).{0,16}?(?:다운로드|내보내|저장)",
    re.IGNORECASE,
)
_EXTERNAL_SOURCE_ACTION = re.compile(
    r"(?:인터넷|웹|온라인|외부\s*지식)(?:을|에서|으로)?\s*.{0,48}?"
    r"(?:검색|찾아|조회|가져|수집|크롤링|확인)",
    re.IGNORECASE,
)
_CREATIVE_ACTION = re.compile(
    r"(?:이|삼|사|오|육|칠|팔|구|N)\s*행시|끝말잇기|말장난|"
    r"(?:아재\s*)?개그|농담|역할극|가사(?:를)?\s*(?:써|작성|만들)|"
    r"(?:소설|시)(?:을|를)?\s*(?:써|작성|만들)",
    re.IGNORECASE,
)
_PERSONAL_SUITABILITY = re.compile(
    r"(?:내게|나에게|나한테|내\s*상황에|내\s*성향에).{0,20}?"
    r"(?:맞는|적합한)|맞춤(?:형)?\s*(?:상품|추천)",
    re.IGNORECASE,
)
_UNSUPPORTED_DATA_ACTION = re.compile(
    r"배당\s*수익률|추적\s*오차(?:율)?|실시간\s*(?:가격|시세)|거래량",
    re.IGNORECASE,
)
_EXPLICIT_RETURN = re.compile(
    r"(?:1\s*일|일간|1D|1\s*주|일주일|주간|1W|1\s*개월|한\s*달|월간|1M|"
    r"3\s*개월|석\s*달|3M|6\s*개월|반년|6M|1\s*년|연간|1Y|YTD|"
    r"연초\s*(?:이후|대비))\s*(?:수익률|수익|성과|return)|"
    r"(?:매수|세후|세전)\s*수익률|(?:표면\s*(?:이율|금리)|쿠폰\s*금리)",
    re.IGNORECASE,
)
_BARE_RETURN = re.compile(r"수익률|성과|\breturn\b", re.IGNORECASE)
_CHEAP = re.compile(r"저렴|싼\s*(?:상품|것|거)", re.IGNORECASE)
_EXPLICIT_CHEAP_METRIC = re.compile(
    r"총\s*보수(?:율)?|보수율|종가|마감\s*가격",
    re.IGNORECASE,
)
_LIQUIDITY = re.compile(r"유동성", re.IGNORECASE)
_EXPLICIT_LIQUIDITY_METRIC = re.compile(
    r"(?:일간?\s*)?거래대금\s*(?:기준|으로|을\s*기준)",
    re.IGNORECASE,
)
_SIZE = re.compile(r"규모", re.IGNORECASE)
_EXPLICIT_SIZE_METRIC = re.compile(
    r"(?:AUM|순자산|운용\s*자산|발행\s*(?:잔액|금액))"
    r"\s*(?:규모|기준|으로|을\s*기준)|운용\s*규모",
    re.IGNORECASE,
)
_RISK = re.compile(r"리스크|위험(?:\s*등급|도|성)?", re.IGNORECASE)
_EXACT_RISK = re.compile(
    r"(?:매우높은|높은|다소높은|보통|낮은|매우낮은)위험\s*\([1-6]등급\)|"
    r"(?:매우높은|높은|다소높은|보통|낮은|매우낮은)위험\s*[1-6]등급",
    re.IGNORECASE,
)
_RISK_GROUP = re.compile(r"위험\s*등급\s*(?:별|분포|비중)", re.IGNORECASE)
_UNMAPPED_METRIC = re.compile(
    r"(?P<metric>가격|비용|변동성|테마|섹터|산업|ESG|친환경|금리|이율|기간|수량)"
    r".{0,16}?(?:높|낮|크|작|많|적|길|짧|저렴|비싸)",
    re.IGNORECASE,
)
_DIRECTION_TERM = re.compile(
    r"(?:가장\s*|제일\s*)?"
    r"(?:높은|낮은|큰|작은|많은|적은|긴|짧은|빠른|오래된|"
    r"상위|하위|오름차순|내림차순|최신)|"
    r"highest\s*[- ]to\s*[- ]lowest|lowest\s*[- ]to\s*[- ]highest|"
    r"ascending|descending",
    re.IGNORECASE,
)
_DIRECTION_METRIC_PATTERN = (
    r"(?:AUM|순자산|운용\s*(?:자산|규모)|발행\s*(?:잔액|금액)|매수\s*가능\s*수량|"
    r"거래대금|종가|마감\s*가격|총\s*보수(?:율)?|보수율|"
    r"(?:1\s*일|1D|1\s*주|1W|1\s*개월|1M|3\s*개월|3M|6\s*개월|6M|"
    r"1\s*년|1Y|YTD|연초\s*이후)\s*(?:수익률|수익|성과|return)|"
    r"매수\s*수익률|세후\s*수익률|표면\s*(?:이율|금리)|쿠폰\s*금리|"
    r"잔존\s*(?:일수|일|기일|기간)|만기(?:까지\s*남은\s*(?:일수|날|기간))?|"
    r"듀레이션|duration|동적\s*(?:지표\s*)?기준일|정적\s*(?:지표\s*)?기준일|"
    r"상품명|짧은\s*이름|이름|티커|종목\s*코드|"
    r"규모|유동성|리스크|위험(?:\s*등급|도|성)?|"
    r"가격|비용|변동성|테마|섹터|산업|ESG|친환경|금리|이율|기간|수량)"
)
_DIRECTION_METRIC = re.compile(_DIRECTION_METRIC_PATTERN, re.IGNORECASE)
_DIRECTION_METRIC_PREFIX = re.compile(
    _DIRECTION_METRIC_PATTERN + r"(?:이|가|은|는|을|를|의|\s|기준|순서)*$",
    re.IGNORECASE,
)
_EXCLUSION = re.compile(
    r"제외(?:하고|한|해)?|이외(?:의|에는|로)?|말고|빼고|"
    r"(?<!국내)(?<!해외)\s외(?:의|에는|로|\s|$)",
    re.IGNORECASE,
)
_NO_EXTRA_CONDITION_DISCLAIMER = re.compile(
    r"(?:다른|추가(?:적인)?)\s*조건(?:은|을|이|가)?\s*"
    r"(?:걸지|적용하지)\s*말고",
    re.IGNORECASE,
)
_SAFE_EXCLUSION = re.compile(
    r"(?:ETF|ETN)(?:가|이|은|는|을|를|\s)*"
    r"(?:아닌|제외(?:하고|한|해)?|이외(?:의|에는|로)?|말고|빼고|"
    r"외(?:의|에는|로)?)|"
    r"거래\s*(?:중지|정지)(?:된|인)?\s*(?:상품|것)?"
    r"(?:은|는|을|를)?\s*(?:제외(?:하고|한|해)?|말고|빼고)|"
    r"미국\s*제외\s*글로벌|"
    r"연금\s*거래\s*불가.{0,16}?(?:제외(?:하고|한|해)?|말고|빼고)|"
    r"잔존\s*(?:일수|일).{0,20}?초과.{0,16}?(?:제외|말고|빼고)|"
    r"환헤지(?:를)?\s*하지\s*않는.{0,16}?(?:제외|말고|빼고)|"
    r"사모.{0,12}?(?:제외|말고|빼고).{0,12}?공모",
    re.IGNORECASE,
)
_QUOTED_PRODUCT = re.compile(
    r'["“‘\']([^"”’\'\n]{2,100})["”’\']',
    re.IGNORECASE,
)
_BRANDED_PRODUCT = re.compile(
    r"\b(?:TIGER|KODEX|ACE|RISE|SOL|HANARO|TIMEFOLIO|ARIRANG|KBSTAR)\b"
    r"\s*[A-Za-z0-9가-힣][A-Za-z0-9가-힣._+&/-]{1,60}",
    re.IGNORECASE,
)
_UNLABELED_PRODUCT_NAME = re.compile(
    r"(?:[A-Za-z0-9가-힣._+&/-]{2,}(?:상품|펀드|채권)\s*"
    r"(?:국내|해외)?\s*(?:ETF|ETN|ETP|공모펀드|국내채권)|"
    r"(?:국내|해외)\s*(?:ETF|ETN|ETP)\s*"
    r"[A-Za-z0-9가-힣._+&/-]{2,}(?:상품|펀드|채권))",
    re.IGNORECASE,
)
_EXPLICIT_NAME_FILTER = re.compile(
    r"(?:상품명|정식\s*상품명|짧은\s*이름|약어명)"
    r".{0,40}?(?:포함|들어간|contains)",
    re.IGNORECASE,
)

_NUMERIC_SORT_METRIC_PATTERN = (
    r"(?:AUM|순자산|운용\s*자산|발행\s*(?:잔액|금액)|매수\s*가능\s*수량|"
    r"거래대금|종가|마감\s*가격|총\s*보수(?:율)?|보수율|"
    r"(?:1\s*일|1D|1\s*주|1W|1\s*개월|1M|3\s*개월|3M|6\s*개월|6M|"
    r"1\s*년|1Y|YTD|연초\s*이후)\s*(?:수익률|수익|성과|return)|"
    r"매수\s*수익률|세후\s*수익률|표면\s*이율|쿠폰\s*금리|"
    r"잔존\s*(?:일수|일|기일|기간)|만기|듀레이션|duration)"
)
_DIRECTIONLESS_SORT = re.compile(
    rf"{_NUMERIC_SORT_METRIC_PATTERN}.{{0,24}}?(?:기준(?:으로)?\s*)?(?:정렬|순서(?:대로|로)?)",
    re.IGNORECASE,
)
_CROSS_FAMILY_MIXED_RANKING = re.compile(
    r"섞(?:어|어서).{0,16}?(?:정렬|순위)|"
    r"환율\s*지정\s*없이.{0,32}?(?:금액|순위|정렬)|"
    r"ETF.{0,40}?1\s*개월\s*수익률.{0,80}?펀드.{0,40}?3\s*개월\s*수익률"
    r".{0,40}?(?:같은\s*기준|정렬)|"
    r"펀드.{0,40}?3\s*개월\s*수익률.{0,80}?ETF.{0,40}?1\s*개월\s*수익률"
    r".{0,40}?(?:같은\s*기준|정렬)",
    re.IGNORECASE,
)
_VAGUE_PERIOD_COMPARISON = re.compile(
    r"전기보다\s*(?:좋아진|나아진|개선된)|"
    r"20\d{2}.{0,20}?20\d{2}.{0,30}?(?:더\s*나은|좋은)\s*기간",
    re.IGNORECASE,
)
_FUND_AUM = re.compile(r"공모\s*펀드|공모펀드", re.IGNORECASE)
_AUM_TERM = re.compile(r"AUM|순자산|운용\s*자산", re.IGNORECASE)
_EXPLICIT_CURRENCY_SCOPE = re.compile(
    r"(?<![A-Z])(?:USD|KRW|EUR|JPY)(?![A-Z])|달러|원화|유로|엔화|"
    r"(?:거래\s*)?통화\s*별|(?:거래\s*)?통화(?:를|를\s*기준으로)?\s*그룹",
    re.IGNORECASE,
)
_UNSUPPORTED_INVERSE_EXCLUSION = re.compile(
    r"미국\s*(?:을|은)?\s*외(?:의|\s*지역)?|"
    r"회사채(?:가|이|는|은)?\s*아닌",
    re.IGNORECASE,
)


def _unique(values: list[str]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(value.strip() for value in values if value.strip()))


def _metric_has_explicit_mapping(question: str, match: re.Match[str]) -> bool:
    prefix = question[max(0, match.start() - 24) : match.start()]
    metric = match.group("metric").casefold()
    known_prefixes = {
        "가격": r"마감\s*$",
        "금리": r"(?:쿠폰|표면)\s*$",
        "이율": r"표면\s*$",
        "기간": r"(?:잔존|만기까지\s*남은)\s*$",
        "수량": r"매수\s*가능\s*$",
    }
    pattern = known_prefixes.get(metric)
    return pattern is not None and re.search(pattern, prefix, flags=re.IGNORECASE) is not None


def _direction_has_metric(question: str, match: re.Match[str]) -> bool:
    if any(
        start <= match.start() and match.end() <= end
        for start, end in (risk.span() for risk in _EXACT_RISK.finditer(question))
    ):
        return True
    prefix = question[max(0, match.start() - 48) : match.start()]
    if _EXPLICIT_SIZE_METRIC.search(prefix) is not None:
        return True
    if _DIRECTION_METRIC_PREFIX.search(prefix) is not None:
        return True
    # A trailing "높은 순" can refer back to one and only one explicit metric
    # used earlier as a threshold (for example, "1주 수익률 5% 이상 ... 높은
    # 순"). Multiple possible metric antecedents remain ambiguous.
    return len(list(_DIRECTION_METRIC.finditer(question))) == 1


class SemanticCoverageGate:
    """Reject semantic downgrades from a constrained request to a generic search.

    This gate is intentionally lexical and conservative. It does not invent a
    field mapping: when a phrase can name more than one metric or requires an
    action outside the read-only Oracle, it returns a control issue instead.
    """

    def evaluate(
        self,
        question: str,
        *,
        interaction_intent: str | None = None,
        check_exclusions: bool = True,
    ) -> SemanticCoverageDecision:
        normalized = normalize_user_question(question)
        scope_surface = data_scope_policy_surface(normalized)
        if interaction_intent in {"clarify", "unsupported"}:
            return SemanticCoverageDecision()

        unsupported: list[str] = []
        ambiguities: list[str] = []

        if match := _TRANSACTIONAL_ACTION.search(normalized):
            unsupported.append(match.group(0))
        if match := _EXTERNAL_SOURCE_ACTION.search(scope_surface):
            unsupported.append(match.group(0))
        if match := _CREATIVE_ACTION.search(normalized):
            unsupported.append(match.group(0))
        if match := _UNSUPPORTED_DATA_ACTION.search(normalized):
            unsupported.append(match.group(0))

        # Routed COMPARE/EXPLAIN requests have dedicated fail-closed parsers
        # with more specific control messages.  The legacy agent calls this
        # gate without an interaction intent, so it still cannot bypass the
        # checks below with a bare comparison metric.
        if interaction_intent in {"compare", "explain"}:
            return SemanticCoverageDecision(unsupported_spans=_unique(unsupported))

        if match := _CROSS_FAMILY_MIXED_RANKING.search(normalized):
            ambiguities.append(match.group(0))
        if match := _VAGUE_PERIOD_COMPARISON.search(normalized):
            ambiguities.append(match.group(0))
        if match := _PERSONAL_SUITABILITY.search(normalized):
            ambiguities.append(match.group(0))
        if match := _UNSUPPORTED_INVERSE_EXCLUSION.search(normalized):
            ambiguities.append(match.group(0))
        if (
            _FUND_AUM.search(normalized) is not None
            and _AUM_TERM.search(normalized) is not None
            and _EXPLICIT_CURRENCY_SCOPE.search(normalized) is None
        ):
            ambiguities.append("trading_currency(AUM 비교 통화)")

        for match in _DIRECTIONLESS_SORT.finditer(normalized):
            if _DIRECTION_TERM.search(match.group(0)) is None:
                ambiguities.append(match.group(0))

        return_surface = _EXPLICIT_RETURN.sub(" ", normalized)
        if match := _BARE_RETURN.search(return_surface):
            ambiguities.append(match.group(0))

        if (match := _CHEAP.search(normalized)) and _EXPLICIT_CHEAP_METRIC.search(
            normalized
        ) is None:
            ambiguities.append(match.group(0))
        if (match := _LIQUIDITY.search(normalized)) and _EXPLICIT_LIQUIDITY_METRIC.search(
            normalized
        ) is None:
            ambiguities.append(match.group(0))
        if (match := _SIZE.search(normalized)) and _EXPLICIT_SIZE_METRIC.search(normalized) is None:
            ambiguities.append(match.group(0))
        if (
            (match := _RISK.search(normalized))
            and _EXACT_RISK.search(normalized) is None
            and _RISK_GROUP.search(normalized) is None
        ):
            ambiguities.append(match.group(0))

        for match in _UNMAPPED_METRIC.finditer(normalized):
            if not _metric_has_explicit_mapping(normalized, match):
                ambiguities.append(match.group(0))

        for match in _DIRECTION_TERM.finditer(normalized):
            if not _direction_has_metric(normalized, match):
                ambiguities.append(match.group(0))

        if check_exclusions:
            exclusion_surface = _NO_EXTRA_CONDITION_DISCLAIMER.sub(" ", normalized)
            exclusion_matches = list(_EXCLUSION.finditer(exclusion_surface))
            safe_exclusion_spans = [
                match.span() for match in _SAFE_EXCLUSION.finditer(exclusion_surface)
            ]
            for match in exclusion_matches:
                is_safe = any(
                    start <= match.start() and match.end() <= end
                    for start, end in safe_exclusion_spans
                )
                if not is_safe:
                    ambiguities.append(match.group(0))

        if interaction_intent in {None, "search"}:
            explicit_name_filter = _EXPLICIT_NAME_FILTER.search(normalized) is not None
            if not explicit_name_filter:
                for match in _QUOTED_PRODUCT.finditer(normalized):
                    if match.group(1).strip().upper() not in {"ETF", "ETN", "ETP"}:
                        ambiguities.append(match.group(0))
                if match := _BRANDED_PRODUCT.search(normalized):
                    ambiguities.append(match.group(0))
                if match := _UNLABELED_PRODUCT_NAME.search(normalized):
                    ambiguities.append(match.group(0))

        return SemanticCoverageDecision(
            ambiguity_spans=_unique(ambiguities),
            unsupported_spans=_unique(unsupported),
        )
