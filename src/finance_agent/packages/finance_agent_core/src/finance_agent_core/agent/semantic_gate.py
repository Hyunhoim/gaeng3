from __future__ import annotations

import re
from dataclasses import dataclass

from finance_agent_core.agent.document_reference import (
    extract_dated_official_document_references,
    overlaps_dated_document_reference,
)
from finance_agent_core.agent.field_language import registry_field_mentions
from finance_agent_core.agent.presentation import mask_response_presentation
from finance_agent_core.agent.safety import (
    data_scope_policy_surface,
    normalize_user_question,
    safety_policy_surface,
)
from finance_agent_core.agent.semantic_aliases import (
    TypedAliasMatch,
    typed_alias_matches,
)
from finance_agent_core.config import ValueType
from finance_agent_core.contracts.queryplan import ProductFamily


@dataclass(frozen=True)
class SemanticCoverageDecision:
    """Meaning-bearing spans that cannot yet acquire execution authority."""

    ambiguity_spans: tuple[str, ...] = ()
    unsupported_spans: tuple[str, ...] = ()
    # Reserved for a trusted, server-owned Schema Linker signal.  The default
    # lexical gate deliberately does not infer this from an unknown phrase.
    schema_link_gap_spans: tuple[str, ...] = ()

    @property
    def has_control_issue(self) -> bool:
        """Whether the request is truly ambiguous or outside supported actions."""

        return bool(self.ambiguity_spans or self.unsupported_spans)

    @property
    def has_unresolved_semantics(self) -> bool:
        """Whether normal planning must stop for any semantic reason."""

        return bool(self.has_control_issue or self.schema_link_gap_spans)

    @property
    def blocked(self) -> bool:
        """Preserve the legacy control contract for existing consumers."""

        return self.has_control_issue


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
    r"배당\s*수익률|실시간\s*(?:가격|시세)",
    re.IGNORECASE,
)
_UNSUPPORTED_PERCENTILE_FILTER = re.compile(
    r"(?:상위|하위)\s*\d+(?:\.\d+)?\s*(?:%|퍼센트)(?:\s*(?:안|이내|구간))?"
    r"(?:에|에\s*드는|인|만|의)?",
    re.IGNORECASE,
)
_UNSUPPORTED_ROW_DATE_ARITHMETIC = re.compile(
    r"(?:만기일?|상환일?|설정일?|발행일?)\s*(?:이|가|은|는)?\s*"
    r"(?:만기일?|상환일?|설정일?|발행일?)\s*보다\s*"
    r"(?:정확히\s*)?\d+\s*(?:년|개월|일)\s*(?:뒤|후|전)",
    re.IGNORECASE,
)
_UNSUPPORTED_DERIVED_AGGREGATE_THRESHOLD = re.compile(
    r"(?:평균|중앙\s*값|중위수|메디안).{0,32}?"
    r"(?:그\s*값|해당\s*값|평균값|중앙값)(?:의)?\s*"
    r"(?:절반|반|\d+(?:\.\d+)?\s*배)(?:\s*(?:이상|이하|초과|미만))",
    re.IGNORECASE,
)
_INVALID_BUYABLE_QUANTITY_METRIC = re.compile(
    r"(?:매수\s*가능\s*수량|매수가능수량|주문\s*가능\s*수량)"
    r"(?:이|가|은|는|을|를)?\s*"
    r"(?:\d|이상|이하|초과|미만|많|적|높|낮|순|정렬|상위|하위)",
    re.IGNORECASE,
)
_NEGATED_RANKING = re.compile(
    r"(?:상위|하위|오름차순|내림차순|"
    r"(?:큰|높은|많은|작은|낮은|적은|빠른|늦은|짧은|긴)\s*순(?:서)?)"
    r"(?:이|가|은|는)?\s*(?:아닌|아니고|아니라|말고|제외(?:하고|한|해)?)",
    re.IGNORECASE,
)
_UNAVAILABLE_SNAPSHOT_FIELD = re.compile(
    r"(?:(?=.*(?:공모\s*펀드|공모펀드|국내\s*(?:ETF|ETN|ETP)))"
    r"(?=.*(?:1\s*주|일주일|한\s*주).{0,16}?(?:수익률|수익|성과|성적))|"
    r"(?=.*(?:공모\s*펀드|공모펀드))"
    r"(?=.*(?:YTD|연초\s*이후|올해\s*들어).{0,16}?(?:수익률|수익|성과|성적)))"
    r".+",
    re.IGNORECASE,
)
_OVERSEAS_ONE_YEAR_FIELD = re.compile(
    r"(?=.*(?:해외\s*(?:ETF|ETN|ETP)))"
    r"(?=.*(?:1\s*년|일\s*년|12\s*개월|1Y).{0,16}?(?:수익률|수익|성과|성적)).+",
    re.IGNORECASE,
)
_UNAPPROVED_RELATION_FILTER = re.compile(
    r"(?:섹터|테마|산업)(?:에|로|를|을)?\s*(?:투자|편입|포함)|"
    r"(?:섹터|테마|산업)(?:와|과|에|으로)?\s*(?:관련|연결|연관)"
    r"(?:된|있는|이력|기록)?|"
    r"(?:섹터|테마|산업)(?:로|라고)?\s*(?:소개|분류|언급)(?:된|한|되는)?|"
    r"(?:편입|보유|구성)(?:한|하는|하고\s*있는|된|된\s*종목|\s*종목|\s*자산)|"
    r"(?:구성\s*종목|보유\s*종목|편입\s*종목)|"
    r"(?:연결|연관)\s*이력|자회사.{0,24}?(?:편입|보유|구성)|"
    # "X를 담은 ETF" names a constituent holding, which the approved relation
    # index does not carry. Requiring the object marker keeps this apart from the
    # supported text search "이름에 X가 포함된", and requiring a product noun after
    # the verb keeps metric definitions such as "경과이자를 포함한 단가" executable.
    r"[0-9A-Za-z가-힣&·+._-]{2,40}(?:을|를)\s*"
    r"(?:담(?:은|고\s*있는|긴)|포함(?:한|하는)|들고\s*있는)\s*"
    r"(?:국내\s*|해외\s*)?(?:ETF|ETN|ETP|공모\s*펀드|펀드|채권|상품|종목)|"
    # A company/constituent weight requires external holdings evidence.  Keep
    # the four approved public-fund allocation fields executable instead of
    # treating them as unapproved constituent relations.
    r"(?<![0-9A-Za-z가-힣&·+._-])(?<!국내\s)(?<!해외\s)"
    r"(?!(?:국내|해외)\s*(?:주식|채권)\s*비중(?:이|을)?\s*(?:높은|큰|많은))"
    r"[0-9A-Za-z가-힣&·+._-]{2,40}\s*비중(?:이|을)?\s*(?:높은|큰|많은)|"
    r"[0-9A-Za-z가-힣&·+._/-]{2,60}\s*(?:관련|연관|연결|연계)\s*"
    r"(?:된\s*)?(?:(?:국내|해외)\s*)?(?:상장\s*)?(?:투자\s*)?"
    r"(?:상품|ETF|ETN|ETP)",
    re.IGNORECASE,
)
_UNAPPROVED_DOCUMENT_KNOWLEDGE = re.compile(
    r"(?:공모\s*펀드|[0-9A-Za-z가-힣&·+._/-]{2,48}펀드)"
    r".{0,48}?(?:구조|투자\s*전략\s*동향|운용\s*동향)|"
    r"(?:위험|리스크)\s*요인",
    re.IGNORECASE,
)
_EXPLICIT_RETURN = re.compile(
    r"(?:1\s*일|하루|당일|오늘|일간|1D|1\s*주|일주일|주간|1W|"
    r"1\s*개월|한\s*달|30\s*일|월간|1M|3\s*개월|석\s*달|90\s*일|"
    r"분기(?:별)?|3M|6\s*개월|반년|두\s*분기|2\s*분기|6M|"
    r"18\s*개월|18M|1\s*년|12\s*개월|연간|1Y|"
    r"2\s*년|2Y|3\s*년|3Y|5\s*년|5Y|YTD|올해\s*들어|"
    r"연초\s*(?:이후|대비))\s*(?:수익률|수익|성과|return)|"
    r"(?:매수|세후|세전|적용)\s*수익률|민평\s*(?:(?:기준|산출)\s*)?(?:수익률|금리)|"
    r"민평에서\s*(?:계산|산출)(?:된|한)?\s*(?:수익률|금리)|"
    r"(?:민간평가\s*금리|평가기관\s*수익률)|(?:연환산\s*)?분배\s*수익률|"
    r"(?:표면\s*(?:이율|금리)|쿠폰\s*금리)",
    re.IGNORECASE,
)
_EXPLICIT_PERIOD_RETURN = re.compile(
    r"(?:1\s*일|하루|당일|오늘|일간|1D|1\s*주|일주일|주간|1W|"
    r"1\s*개월|한\s*달|30\s*일|월간|1M|3\s*개월|석\s*달|90\s*일|"
    r"분기(?:별)?|3M|6\s*개월|반년|두\s*분기|2\s*분기|6M|"
    r"18\s*개월|18M|1\s*년|12\s*개월|연간|1Y|"
    r"2\s*년|2Y|3\s*년|3Y|5\s*년|5Y|YTD|올해\s*들어|"
    r"연초\s*(?:이후|대비))\s*(?:수익률|수익|성과|return)",
    re.IGNORECASE,
)
_BARE_RETURN = re.compile(
    r"수익률|"
    # ``속성과`` contains the characters ``성과`` but means "attributes
    # and", not investment performance.  Require a real Korean token start
    # while retaining ordinary particles such as ``성과가``.
    r"(?<![0-9A-Za-z가-힣])성과(?=(?:이|가|은|는|을|를|로)?(?:\s|[?!.,]|$))|"
    r"\breturn\b",
    re.IGNORECASE,
)
_CHEAP = re.compile(r"저렴|싼\s*(?:상품|것|거)", re.IGNORECASE)
_EXPLICIT_CHEAP_METRIC = re.compile(
    r"(?:총|연간)?\s*보수(?:율)?|(?:보유|유지|관리|운용|운영)\s*비용|"
    r"종가|마감\s*가격",
    re.IGNORECASE,
)
_LIQUIDITY = re.compile(r"유동성", re.IGNORECASE)
_EXPLICIT_LIQUIDITY_METRIC = re.compile(
    r"(?:일간?\s*)?거래대금\s*(?:기준|으로|을\s*기준)",
    re.IGNORECASE,
)
_SIZE = re.compile(r"규모", re.IGNORECASE)
_EXPLICIT_SIZE_METRIC = re.compile(
    r"(?:AUM|순자산(?:총액)?|운용\s*자산|발행\s*(?:잔액|금액))"
    r"\s*(?:규모|기준|으로|을\s*기준)|운용\s*규모|총\s*발행\s*규모",
    re.IGNORECASE,
)
_RISK = re.compile(r"리스크|위험(?:\s*등급|도|성)?", re.IGNORECASE)
_EXACT_RISK = re.compile(
    r"(?:매우높은|높은|다소높은|보통|낮은|매우낮은)위험\s*\([1-6]등급\)|"
    r"(?:매우높은|높은|다소높은|보통|낮은|매우낮은)위험\s*[1-6]등급|"
    r"(?:매우\s*높은|높은|다소\s*높은|보통|낮은|매우\s*낮은)\s*위험|"
    r"위험\s*(?:등급|단계)(?:이|가|은|는)?\s*[1-6]\s*(?:등급)?"
    r"(?:\s*(?:이상|이하|초과|미만))?",
    re.IGNORECASE,
)
_RISK_GROUP = re.compile(r"위험\s*등급\s*(?:별|분포|비중)", re.IGNORECASE)
_RISK_PROJECTION = re.compile(
    r"위험\s*(?:등급(?:\s*명)?|도|성)(?:을|를|은|는|이|가|과|와|,|\s)*"
    r"(?:(?:원천값|데이터에\s*적힌\s*값)(?:\s*그대로|대로)?\s*)?"
    r"(?:알려|보여|정리|조회|제시|설명|확인)|"
    r"(?:알려|보여|정리|조회|제시|설명|확인).{0,20}"
    r"위험\s*(?:등급(?:\s*명)?|도|성)",
    re.IGNORECASE,
)
_VAGUE_STABILITY = re.compile(
    r"(?:안정적(?:인|으로)?|안정성이\s*(?:높|좋)|안전한).{0,16}?"
    r"(?:국내\s*|해외\s*)?(?:ETF|ETN|ETP|공모\s*펀드|펀드|채권|상품)",
    re.IGNORECASE,
)
_GENERIC_PREFERENCE = re.compile(
    r"대표적(?:인|이다)?|"
    r"인기(?:도(?:가|는|를|도)?|가\s*(?:높|많)(?:은|다)?|\s*(?:있는|많은))|"
    r"효율(?:이|가)?\s*높(?:은|다)?|"
    r"부담(?:이|가)?\s*(?:(?:적|작)(?:은|다)?|덜(?:한|하다)?|"
    r"없(?:는|다)?|되지\s*않(?:는|다)?)|"
    r"무난(?:한|하다)?|유망(?:한|하다)?|알짜(?:인|다)?|"
    r"조건(?:이|가)?\s*(?:괜찮|좋)|비용\s*대비\s*괜찮(?:은|다)?|"
    r"추천할\s*만한|(?<!않은\s)좋은|나은|괜찮은|(?<!비)싼",
    re.IGNORECASE,
)
_TRAILING_DIRECTION = re.compile(
    r"(?:이|가|은|는|을|를)?\s*(?:높|낮|크|작|많|적|길|짧|저렴|비싸)\S*$"
)
_UNMAPPED_METRIC = re.compile(
    r"(?P<metric>가격|비용|변동성|테마|섹터|산업|ESG|친환경|금리|이율|기간|수량)"
    r".{0,16}?(?:높|낮|크|작|많|적(?!용)|길|짧|저렴|비싸)",
    re.IGNORECASE,
)
_DIRECTION_TERM = re.compile(
    r"(?:가장\s*|제일\s*)?"
    # ``긴`` must not match the suffix of the two-stage verb ``남긴``.
    r"(?:높은|높게|낮은|낮게|큰|크게|작은|작게|많은|많게|적은|적게|"
    r"(?<!남)긴|길게|짧은|짧게|빠른|오래된|비싼|싼|싸게|저렴한|저렴하게|(?<!퍼)센(?!트)|"
    r"상위|하위|TOP|BOTTOM|오름차순|내림차순|최신|좋은|빨리|먼저|가까운)|"
    r"highest\s*[- ]to\s*[- ]lowest|lowest\s*[- ]to\s*[- ]highest|"
    r"ascending|descending",
    re.IGNORECASE,
)
_AGGREGATE_GROUP_LABEL_ORDER = re.compile(
    r"(?:대분류|소분류|상품\s*유형|유형|자산\s*유형|운용\s*속성|"
    r"위험\s*등급|투자\s*지역|거래\s*통화)(?:명)?"
    r"(?:을|를|은|는|이|가)?\s*(?:오름차순|가나다순|알파벳순)",
    re.IGNORECASE,
)
_GENERIC_NUMERIC_THRESHOLD = re.compile(
    r"-?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?\s*"
    r"(?:%|퍼센트)?\s*보다\s*(?:큰|높은|작은|낮은)",
    re.IGNORECASE,
)
_EXPLICIT_NUMERIC_BOUND = re.compile(
    r"(?:최소|최대|적어도|많아야)\s*-?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?|"
    r"-?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?\s*"
    r"(?:%|퍼센트|배|일|년|주|달러|USD|KRW)?\s*"
    r"(?:이상|이하|초과|미만|밑(?:인|이고|이며)?|아래(?:인|이고|이며)?|"
    r"보다\s*(?:낮(?:은|고|거나)|작(?:은|고|거나)|적(?:은|고|거나)|"
    r"높(?:은|고|거나)|큰|크거나|많(?:은|고|거나))|"
    r"보다\s*(?:낮|작|적|높|크|많)지\s*않(?:은|는))",
    re.IGNORECASE,
)
_ANSWER_ORDER_EXPLANATION = re.compile(
    r"(?:첫|선두|맨\s*앞).{0,24}?왜.{0,10}?먼저|왜\s*(?:가장\s*)?먼저(?:인지|인가)",
    re.IGNORECASE,
)
_GLOBAL_RANKING_SCOPE = re.compile(
    r"통합\s*(?:비교|정렬|순위)|한\s*줄\s*순위|(?:한\s*순위에\s*)?합쳐|"
    r"통합\s*(?:높은|낮은)\s*순|함께\s*(?:비교|정렬|순위)|"
    r"한\s*(?:목록|리스트)(?:으로|에)?|따로\s*나누지\s*말고|"
    r"직접\s*비교|통틀어|전체\s*순위",
    re.IGNORECASE,
)
_POTENTIAL_SCHEMA_GAP = re.compile(
    r"(?P<span>[A-Za-z가-힣][A-Za-z0-9가-힣._/-]{1,30}?)"
    r"(?:이|가|은|는|을|를)?\s*(?:높은|낮은|큰|작은|많은|적은)",
    re.IGNORECASE,
)
_DIRECTION_METRIC_PATTERN = (
    r"(?:AUM|순자산(?:총액)?|운용\s*(?:자산|규모)|"
    r"(?:총\s*)?발행\s*(?:잔액|금액|액|규모|물량)|남은\s*발행\s*물량|"
    r"매수\s*가능\s*수량|"
    r"거래대금|외화\s*거래대금|거래량|종가|마감\s*가격|"
    r"총\s*보수(?:율)?|보수율|연간\s*보수(?:율)?|추적\s*오차(?:율)?|괴리율|분배\s*수익률|"
    r"연간\s*분배\s*(?:지급\s*)?횟수|분배\s*(?:지급\s*)?횟수|"
    r"판매(?:\s*회사)?\s*보수|신탁\s*업자\s*보수|수탁보수|"
    r"(?:국내|해외)\s*(?:주식|채권)\s*(?:구성비율|비중|편입\s*(?:비|비중|비율))|"
    r"최근\s*분배율|보전율|"
    r"민평\s*(?:(?:기준|산출)\s*)?(?:수익률|금리)|적용\s*수익률|"
    r"민평에서\s*(?:계산|산출)(?:된|한)?\s*(?:수익률|금리)|"
    r"민간평가\s*금리|평가기관\s*수익률|"
    r"평가일단가|평가\s*단가|클린\s*프라이스|이자\s*제외\s*평가(?:단가|가격)|"
    r"이자부단가|경과이자(?:를)?\s*포함(?:한)?\s*(?:단가|가격)|컨벡시티|가격\s*굴곡|"
    r"(?:20|60|120|252)\s*(?:거래일|일)(?:\s*연환산)?\s*(?:변동성|흔들림)|"
    r"(?:1\s*개월|3\s*개월|6\s*개월|1\s*년)\s*(?:변동성|흔들림)|"
    r"(?:1\s*일|하루|당일|오늘|1D|1\s*주|한\s*주|일주일|7\s*일|1W|"
    r"1\s*개월|한\s*달|30\s*일|1M|3\s*개월|석\s*달|90\s*일|3M|"
    r"6\s*개월|반년|6M|18\s*개월|18M|1\s*년|일\s*년|12\s*개월|1Y|"
    r"2\s*년|2Y|3\s*년|3Y|5\s*년|5Y|YTD|올해\s*들어|"
    r"연초\s*이후)\s*(?:수익률|수익|성과|return)|"
    r"매수\s*수익률|세후\s*수익률|표면\s*(?:이자율|이율|금리)|"
    r"쿠폰(?:\s*금리)?|액면\s*쿠폰|액면에?\s*붙은\s*이자율|"
    r"잔존\s*(?:일수|일|기일|기간)|잔여\s*일|"
    r"(?:상환|만기)(?:까지)?\s*남은\s*(?:일수|날짜|날|기간)|"
    r"(?:상환|만기)(?:일|일자)?|"
    r"듀레이션|duration|금리\s*민감도|동적\s*(?:지표\s*)?기준일|정적\s*(?:지표\s*)?기준일|"
    r"상품명|종목명|펀드명|ETF\s*명|ETN\s*명|채권명|짧은\s*이름|이름|"
    r"티커|종목\s*코드|"
    r"규모|유동성|리스크|위험(?:\s*등급|도|성)?|"
    r"가격|비용|변동성|테마|섹터|산업|ESG|친환경|금리|이율|기간|수량)"
)
_DIRECTION_METRIC = re.compile(_DIRECTION_METRIC_PATTERN, re.IGNORECASE)
_EXPLICIT_DIRECTIONAL_METRIC = re.compile(
    rf"(?P<metric>{_DIRECTION_METRIC_PATTERN})"
    r"(?:이|가|은|는|을|를|의|\s|기준|으로)*"
    r"(?P<direction>높(?:은|게)?|낮(?:은|게)?|큰|크(?:거나|게)?|"
    r"작(?:은|게)?|많(?:은|게)?|적(?:은|게)?|긴|길(?:게)?|"
    r"짧(?:은|게)?|비싼|싼)",
    re.IGNORECASE,
)
_EXPLICIT_TWO_STAGE_TRANSITION = re.compile(
    r"(?:상위|하위|큰|작은|높은|낮은|많은|적은).{0,24}?"
    r"(?:\d+|한|두|세|셋|네|넷|다섯|여섯|일곱|여덟|아홉|열)\s*"
    r"(?:개|종목|상품)(?:를|을)?\s*"
    r"(?:중(?:에서)?|가운데서|안에서|그\s*중|그\s*안에서|"
    r"먼저|우선|1\s*(?:차|단계)|예선|후보|"
    r"추린\s*(?:뒤|후|다음)|고른\s*(?:뒤|후|다음)|"
    r"남긴\s*(?:뒤|후|다음))",
    re.IGNORECASE,
)
_FILTER_THEN_RANK_TRANSITION = re.compile(
    r"(?:먼저|우선)\s*(?:후보(?:를|로)?\s*)?"
    r"(?:추린|추리고|추려|고른|고르고|골라|선별한|선별하고|"
    r"남긴|남기고|남겨|거른|거르고|걸러|좁힌|좁히고|"
    r"한정한|한정하고|뽑은|뽑고)\s*(?:뒤|후|다음)",
    re.IGNORECASE,
)
_EXPLICIT_RESULT_COUNT = re.compile(
    r"(?:\d+|한|두|세|셋|네|넷|다섯|여섯|일곱|여덟|아홉|열)\s*"
    r"(?:개|종목|상품)",
    re.IGNORECASE,
)
_DIRECTION_METRIC_PREFIX = re.compile(
    _DIRECTION_METRIC_PATTERN + r"(?:이|가|은|는|을|를|의|\s|기준|순|순서|으로)*$",
    re.IGNORECASE,
)
_EXCLUSION = re.compile(
    r"제외(?:하고|한|해)?|이외(?:의|에는|로)?|말고|빼고|"
    r"(?<!국내)(?<!해외)\s외(?:의|에는|로|\s|$)",
    re.IGNORECASE,
)
_NO_EXTRA_CONDITION_DISCLAIMER = re.compile(
    r"(?:다른|추가(?:적인)?)\s*조건(?:은|을|이|가)?\s*"
    r"(?:걸지|적용하지)\s*말고|"
    r"(?:해당|그|이)?\s*조건(?:은|을|이|가)?\s*"
    r"(?:빠뜨리|누락하|빼먹|잊)지\s*말고",
    re.IGNORECASE,
)
_SAFE_EXCLUSION = re.compile(
    r"(?:상품|종목)\s*(?:목록|리스트)(?:은|는|을|를)?\s*"
    r"(?:말고|빼고|제외(?:하고|해)?)|"
    r"(?:ETF|ETN)(?:가|이|은|는|을|를|\s)*"
    r"(?:아닌|제외(?:하고|한|해)?|이외(?:의|에는|로)?|말고|빼고|"
    r"외(?:의|에는|로)?)|"
    r"거래\s*(?:중지|정지)(?:된|인)?\s*(?:상품|것)?"
    r"(?:은|는|을|를)?\s*(?:제외(?:하고|한|해)?|말고|빼고)|"
    r"미국(?:을)?\s*제외(?:한)?\s*글로벌(?:\s*지역)?(?:\s*\(Global\s*Ex\s*US\))?|"
    r"따로\s*나누지\s*말고|"
    r"(?:합쳐|합해서|통합해).{0,24}?(?:줄\s*세우|정렬|순위).{0,8}?"
    r"(?:지\s*말고|말고)|"
    r"이자(?:를)?\s*(?:제외|뺀)\s*평가(?:단가|가격)|"
    r"연금\s*거래\s*불가.{0,16}?(?:제외(?:하고|한|해)?|말고|빼고)|"
    r"잔존\s*(?:일수|일).{0,20}?초과.{0,16}?(?:제외|말고|빼고)|"
    r"환헤지(?:를)?\s*하지\s*않는.{0,16}?(?:제외|말고|빼고)|"
    r"사모.{0,12}?(?:제외|말고|빼고).{0,12}?공모",
    re.IGNORECASE,
)
_MISSING_VALUE_EXCLUSION = re.compile(
    r"(?:값|수치)(?:이|가)?\s*(?:없|비어\s*있)(?:는|은)\s*"
    r"(?:상품|종목|행)(?:은|는|을|를)?\s*"
    r"(?:제외(?:하고|한|해)?|빼고|말고)",
    re.IGNORECASE,
)
_RANKED_ZERO_OR_MISSING_EXCLUSION = re.compile(
    r"(?:0|영)(?:이나|과|와|또는|혹은|·|/|,|\s)+(?:결측(?:값)?|빈\s*값|누락(?:값)?)"
    r"(?:은|는|을|를)?\s*(?:제외(?:하고|한|해)?|빼고|말고)|"
    r"(?:결측(?:값)?|빈\s*값|누락(?:값)?)(?:이나|과|와|또는|혹은|·|/|,|\s)+"
    r"(?:0|영)(?:은|는|을|를)?\s*(?:제외(?:하고|한|해)?|빼고|말고)",
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
    r"(?:(?:상품명|정식\s*상품명|짧은\s*이름|약어명)"
    r".{0,40}?(?:포함|들어간|contains)|"
    r"정확(?:한)?\s*(?:상품명|이름|명칭)(?:이|은|가)?\s*[:：]?)",
    re.IGNORECASE,
)
_PER_FAMILY_IDENTITY_BATCH = re.compile(
    r"각각|나란히|상품군별로|모두\s*포함한\s*목록|한\s*답변에서|한\s*번에",
    re.IGNORECASE,
)
_LATEST_DB_VALUE = re.compile(r"최신\s*(?:DB|데이터베이스|제공\s*데이터)\s*값", re.IGNORECASE)

_NUMERIC_SORT_METRIC_PATTERN = (
    r"(?:AUM|순자산(?:총액)?|운용\s*자산|(?:총\s*)?발행\s*(?:잔액|금액|규모|물량)|"
    r"남은\s*발행\s*물량|"
    r"매수\s*가능\s*수량|"
    r"거래대금|외화\s*거래대금|거래량|종가|마감\s*가격|"
    r"총\s*보수(?:율)?|보수율|연간\s*보수(?:율)?|추적\s*오차(?:율)?|괴리율|분배\s*수익률|"
    r"연간\s*분배\s*(?:지급\s*)?횟수|분배\s*(?:지급\s*)?횟수|"
    r"판매(?:\s*회사)?\s*보수|신탁\s*업자\s*보수|수탁보수|"
    r"(?:국내|해외)\s*(?:주식|채권)\s*(?:구성비율|비중)|최근\s*분배율|보전율|"
    r"민평\s*(?:(?:기준\s*)?수익률|금리)|민평에서\s*(?:계산|산출)(?:된|한)?\s*(?:수익률|금리)|"
    r"민간평가\s*금리|평가기관\s*수익률|"
    r"평가일단가|평가\s*단가|클린\s*프라이스|이자\s*제외\s*평가(?:단가|가격)|"
    r"이자부단가|경과이자(?:를)?\s*포함(?:한)?\s*(?:단가|가격)|컨벡시티|가격\s*굴곡|"
    r"(?:20|60|120|252)\s*(?:거래일|일)(?:\s*연환산)?\s*(?:변동성|흔들림)|"
    r"(?:1\s*개월|3\s*개월|6\s*개월|1\s*년)\s*(?:변동성|흔들림)|"
    r"(?:1\s*일|하루|당일|오늘|1D|1\s*주|한\s*주|1W|1\s*개월|한\s*달|1M|"
    r"3\s*개월|석\s*달|3M|6\s*개월|반년|6M|18\s*개월|18M|"
    r"1\s*년|일\s*년|1Y|2\s*년|2Y|3\s*년|3Y|5\s*년|5Y|YTD|"
    r"연초\s*이후)\s*(?:수익률|수익|성과|return)|"
    r"매수\s*수익률|세후\s*수익률|표면\s*(?:이자율|이율)|"
    r"쿠폰(?:\s*금리)?|액면\s*쿠폰|액면에?\s*붙은\s*이자율|"
    r"잔존\s*(?:일수|일|기일|기간)|잔여\s*일|"
    r"(?:상환|만기)(?:까지)?\s*남은\s*(?:일수|날짜|날|기간)|"
    r"만기|상환|듀레이션|duration|금리\s*민감도)"
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
_ADDITIONAL_CRITERION = re.compile(
    r"(?P<span>[A-Za-z가-힣][A-Za-z0-9가-힣._/-]{1,30}?)(?:도|까지)?\s*"
    r"(?:반영|고려|감안)(?:해|해서|하여|하고|해줘|해\s*주세요|하되|할)?",
    re.IGNORECASE,
)
_KNOWN_EXTERNAL_CRITERION = re.compile(
    r"위험\s*요인|리스크|구성\s*종목|편입|보유|테마|섹터|동향|근거|출처|기준일",
    re.IGNORECASE,
)
_UNDERSPECIFIED_DECISION_RULE = re.compile(
    r"[0-9A-Za-z가-힣&·+._/-]{2,40}\s*(?:테마|섹터|산업|기업)?"
    r".{0,16}?(?:와|과)?\s*(?:연결|연관)된\s*상품|"
    r"(?:보수|비용).{0,16}?(?:낮|적).{0,28}?(?:거래|유동성).{0,16}?"
    r"(?:활발|높).{0,28}?(?:종합|통합)\s*1\s*위|"
    r"변동(?:성|폭)?(?:이|이\s*)?\s*줄어든|"
    r"너무\s*쏠리지\s*않은|"
    r"배당(?:이|금이|수익이)?\s*나은|"
    r"순자산(?:총액)?(?:이|이\s*)?\s*(?:빠르게\s*)?(?:늘어난|증가한)"
    r".{0,24}?증가율|"
    r"환율\s*효과.{0,24}?(?:맞춰|반영).{0,24}?(?:순위|비교)|"
    r"최근에\s*(?:새로\s*)?편입된|"
    r"충분히\s*(?:담|편입|보유)|"
    r"(?:제|내)\s*계좌(?:에|에서)?.{0,28}?(?:들어갈|가입할|거래할)\s*수\s*있는"
    r"(?:\s*공모\s*펀드)?\s*클래스|"
    r"비용(?:이|이\s*)?\s*덜\s*드는|"
    r"거래\s*시장.{0,32}?(?:값|수치).{0,24}?(?:다르면|다른).{0,24}?"
    r"(?:하나로|한\s*값으로)\s*정리|"
    r"(?:개인용|개인).{0,24}?(?:법인용|법인).{0,24}?(?:값|자료).{0,16}?다르면|"
    r"채권.{0,32}?ETF.{0,32}?(?:같은\s*)?만기\s*기준|"
    r"환율(?:에|의)?\s*덜\s*흔들리는|"
    r"지수.{0,24}?반대로\s*움직(?:이는|인).{0,32}?(?:정도|순위)|"
    r"(?:처음\s*내는|가입\s*시)\s*비용.{0,32}?(?:보유|운용).{0,24}?비용"
    r".{0,24}?(?:모두|합쳐|합산)",
    re.IGNORECASE,
)
_VAGUE_RELATION_SCOPE = re.compile(
    r"[0-9A-Za-z가-힣&·+._-]{2,40}(?:\s*(?:계열|테마|섹터|산업))?"
    r".{0,24}?(?:담은|관련된)\s*상품",
    re.IGNORECASE,
)
_FUND_AUM = re.compile(r"공모\s*펀드|공모펀드", re.IGNORECASE)
_AUM_TERM = re.compile(r"AUM|순자산(?:총액)?|운용\s*자산", re.IGNORECASE)
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
_ALIAS_FAMILY_PATTERNS: dict[ProductFamily, re.Pattern[str]] = {
    ProductFamily.BOND: re.compile(r"국내\s*채권|채권"),
    ProductFamily.DOMESTIC_ETP: re.compile(r"국내\s*(?:ETF|ETN|ETP)", re.IGNORECASE),
    ProductFamily.OVERSEAS_ETP: re.compile(r"해외\s*(?:ETF|ETN|ETP)", re.IGNORECASE),
    ProductFamily.FUND: re.compile(r"공모\s*펀드|펀드"),
}


def _metrics_have_distinct_family_owners(
    question: str,
    metrics: list[re.Match[str]],
) -> bool:
    """Return whether each directed metric belongs to a different family clause.

    Cross-family requests legitimately use different metrics, for example
    ``국내 ETF는 1개월 수익률 높은 순, 해외 ETF는 AUM 높은 순``.
    This is not the same as asking one product list to optimize two metrics.
    Ownership is admitted only when every metric has a nearby explicit family
    noun and at least two distinct families own the metrics; the downstream
    family compiler still has to preserve every clause independently.
    """

    family_mentions = [
        (family, match.start(), match.end())
        for family, pattern in _ALIAS_FAMILY_PATTERNS.items()
        for match in pattern.finditer(question)
    ]
    owners: list[ProductFamily] = []
    for metric in metrics:
        candidates: list[tuple[int, ProductFamily]] = []
        for family, start, end in family_mentions:
            if end <= metric.start():
                distance = metric.start() - end
            elif metric.end() <= start:
                distance = start - metric.end()
            else:
                distance = 0
            if distance <= 48:
                candidates.append((distance, family))
        if not candidates:
            return False
        minimum = min(distance for distance, _family in candidates)
        nearest = {family for distance, family in candidates if distance == minimum}
        if len(nearest) != 1:
            return False
        owners.append(next(iter(nearest)))
    return len(set(owners)) >= 2


def _unique(values: list[str]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(value.strip() for value in values if value.strip()))


def _metric_has_explicit_mapping(question: str, match: re.Match[str]) -> bool:
    field_surface = question[max(0, match.start() - 40) : match.end()]
    if _known_field_surface(field_surface):
        return True
    # "가격 괴리율이 높은" names a registry field even though it opens with the
    # vague token "가격". Drop the trailing direction word and accept only when
    # what remains says more than the vague token by itself, so a bare
    # "가격이 높은" stays ambiguous.
    trimmed = _TRAILING_DIRECTION.sub("", field_surface).strip()
    if len(trimmed) > len(match.group("metric")) and _known_field_surface(trimmed):
        return True
    prefix = question[max(0, match.start() - 24) : match.start()]
    metric = match.group("metric").casefold()
    known_prefixes = {
        "가격": r"(?:마감|평가|이자\s*제외\s*평가|경과이자\s*포함)\s*$",
        "금리": r"(?:쿠폰|표면|민평|민간평가)\s*$",
        "이율": r"표면\s*$",
        "기간": r"(?:잔존|만기까지\s*남은)\s*$",
        "수량": r"매수\s*가능\s*$",
        "변동성": (
            r"(?<!\d)(?:20|60|120|252)\s*(?:거래일|일)(?:\s*연환산)?\s*$|"
            r"(?:1\s*개월|3\s*개월|6\s*개월|1\s*년)(?:\s*연환산)?\s*$"
        ),
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
    if (
        re.search(
            _DIRECTION_METRIC_PATTERN + r"(?:이|가|은|는|을|를)?\s*"
            r"(?:높(?:은)?|낮(?:은)?|크(?:거나)?|큰|작(?:은)?|많(?:은)?|"
            r"적(?:은)?|길(?:거나)?|긴|짧(?:은)?|센)\S*\s*$",
            prefix,
            flags=re.IGNORECASE,
        )
        is not None
    ):
        return True
    if _EXPLICIT_SIZE_METRIC.search(prefix) is not None:
        return True
    if _DIRECTION_METRIC_PREFIX.search(prefix) is not None:
        return True
    # Relative filters retain the explicit metric before an aggregate
    # reference, e.g. ``듀레이션이 전체 중앙값보다 짧은``.  The aggregate
    # phrase does not create a second metric; it only supplies the comparison
    # threshold, so the trailing direction is fully covered.
    if (
        re.search(
            _DIRECTION_METRIC_PATTERN
            + r"(?:이|가|은|는)?\s*(?:전체\s*)?"
            + r"(?:평균|산술\s*평균|중앙값|최댓값|최솟값)\s*보다\s*$",
            prefix,
            flags=re.IGNORECASE,
        )
        is not None
    ):
        return True
    # The frozen registry is the control layer, so a label or alias it declares
    # grounds the direction even when no hand written pattern spells it out.
    # Without this, adding an alias to the registry silently fails to work.
    if _known_field_surface(prefix):
        return True
    # A trailing "높은 순" can refer back to one and only one explicit metric
    # used earlier as a threshold (for example, "1주 수익률 5% 이상 ... 높은
    # 순"). Multiple possible metric antecedents remain ambiguous.
    return len(list(_DIRECTION_METRIC.finditer(question))) == 1


def _is_earlier_maturity_direction(question: str, match: re.Match[str]) -> bool:
    """Return whether ``먼저`` belongs directly to a maturity expression.

    ``먼저`` is also workflow syntax (``먼저 추린 뒤``), so using the
    sentence-wide presence of any metric is unsafe.  Only the immediately
    preceding maturity/repayment noun may turn it into an ascending sort cue.
    """

    prefix = question[max(0, match.start() - 24) : match.start()]
    suffix = question[match.end() : match.end() + 24]
    return (
        re.search(
            r"(?:만기(?:일|시점|도래)?|상환(?:일|시점|도래)?)"
            r"(?:이|가|은|는)?\s*$",
            prefix,
            flags=re.IGNORECASE,
        )
        is not None
        or re.match(
            r"\s*(?:만기(?:가|되는|도래하는)?|상환(?:되|되는|되는지|될|하는))",
            suffix,
            flags=re.IGNORECASE,
        )
        is not None
    )


def _opens_known_field_surface(value: str) -> bool:
    """Return whether the text starts with a registry label or alias.

    "최신" is a direction word on its own but also opens the declared surface
    "최신 분배율". Reading forward keeps the metric from being mistaken for a
    bare direction with nothing to sort by.
    """

    normalized = re.sub(r"\s+", "", value).casefold()
    if not normalized:
        return False
    from finance_agent_core.config import load_field_registry

    for definition in load_field_registry().fields.values():
        for phrase in (definition.label, *definition.aliases):
            candidate = re.sub(r"\s+", "", phrase).casefold()
            if len(candidate) >= 3 and normalized.startswith(candidate):
                return True
    return False


def _known_field_surface(value: str) -> bool:
    """Return whether a directional metric is already mapped by the frozen registry."""

    normalized = re.sub(r"\s+", "", value).casefold()
    if not normalized:
        return False
    from finance_agent_core.config import load_field_registry

    for definition in load_field_registry().fields.values():
        for phrase in (definition.label, *definition.aliases):
            candidate = re.sub(r"\s+", "", phrase).casefold()
            if len(candidate) >= 2 and (
                normalized == candidate
                or normalized in candidate
                or normalized.endswith(candidate)
                or re.search(
                    rf"{re.escape(candidate)}(?:이|가|은|는|을|를|으로|로|기준)?$",
                    normalized,
                )
                is not None
            ):
                return True
    return False


def _overlaps_any(start: int, end: int, ranges: tuple[tuple[int, int], ...]) -> bool:
    return any(start < right and left < end for left, right in ranges)


def _covered_by_resolved_alias(
    start: int,
    end: int,
    ranges: tuple[tuple[int, int], ...],
) -> bool:
    return _overlaps_any(start, end, ranges) or any(0 <= start - right <= 8 for _, right in ranges)


def _compatible_trailing_alias_direction(
    question: str,
    match: re.Match[str],
    alias_matches: tuple[TypedAliasMatch, ...],
) -> bool:
    """Accept a redundant trailing sort cue for one nearest resolved alias.

    Natural requests often state the direction twice around the product noun,
    for example ``올해 들어 성적이 좋은 국내 ETF 상위 4개``.  ``상위``
    carries no new metric there: it agrees with the one field already locked by
    the typed alias.  Standalone adjectives still require ``순`` so a vague
    second condition cannot be swallowed as a redundant direction.
    """

    nearby = [item for item in alias_matches if 0 <= match.start() - item.end <= 48]
    if not nearby:
        return False
    nearest_gap = min(match.start() - item.end for item in nearby)
    nearby = [item for item in nearby if match.start() - item.end == nearest_gap]
    if len({item.field_id for item in nearby}) != 1:
        return False
    surface = match.group(0).strip()
    has_sort_suffix = re.match(r"\s*순", question[match.end() :]) is not None
    is_standalone_ordinal = re.fullmatch(
        r"상위|하위|오름차순|내림차순|ascending|descending",
        surface,
        re.IGNORECASE,
    ) is not None
    # Some typed aliases deliberately end at the metric noun so the same
    # meaning can support both filtering and ranking grammar.  A direction at
    # most one Korean particle/space away is its descriptor (``매수 보상이
    # 큰``), not a new condition.  Longer gaps need an explicit sort suffix or
    # ordinal and are still checked for directional agreement below.
    is_adjacent_descriptor = nearest_gap <= 2
    if not has_sort_suffix and not is_standalone_ordinal and not is_adjacent_descriptor:
        return False
    ascending = re.search(r"낮|작|적|짧|빠른|하위|오름차순", surface) is not None
    descending = (
        re.search(r"높|크|큰|많|긴|센|좋|비싼|상위|내림차순", surface) is not None
    )
    if ascending == descending:
        return False
    expected = "asc" if ascending else "desc"
    return all(item.direction.value == expected for item in nearby)


def _is_filter_then_rank_sequence(
    question: str,
    direction_match: re.Match[str],
    aliases: tuple[TypedAliasMatch, ...],
) -> bool:
    """Prove that ``먼저`` is sequencing explicit filters and a final rank.

    The word ``먼저`` is normally a direction-like residual and must not be
    discarded.  A common, fully typed request shape is nevertheless
    ``[hard filters] 먼저 추린 뒤 [metric + direction + N]``.  It is safe to
    treat ``먼저`` as workflow syntax only when both sides are independently
    evidenced in the user's text.  No metric, direction, or threshold is
    inferred here.
    """

    transition = next(
        (
            item
            for item in _FILTER_THEN_RANK_TRANSITION.finditer(question)
            if item.start() <= direction_match.start() < item.end()
        ),
        None,
    )
    if transition is None:
        return False
    before = question[: transition.start()]
    after = question[transition.end() :]
    has_typed_filter = any(
        item.operation == "filter" and item.end <= transition.start()
        for item in aliases
    )
    has_numeric_filter = (
        _EXPLICIT_NUMERIC_BOUND.search(before) is not None
        and _DIRECTION_METRIC.search(before) is not None
    )
    has_rank_alias = any(
        item.operation == "rank" and item.start >= transition.end()
        for item in aliases
    )
    has_explicit_rank = (
        _EXPLICIT_DIRECTIONAL_METRIC.search(after) is not None
        and _DIRECTION_TERM.search(after) is not None
    )
    return (
        (has_typed_filter or has_numeric_filter)
        and (has_rank_alias or has_explicit_rank)
        and _EXPLICIT_RESULT_COUNT.search(after) is not None
    )


def _schema_link_gap_matches(
    question: str,
    *,
    resolved_ranges: tuple[tuple[int, int], ...] = (),
) -> list[re.Match[str]]:
    matches = []
    blocked_tokens = {
        "상품",
        "종목",
        "것",
        "개",
        "건",
        "etf",
        "etn",
        "etp",
        "채권",
        "펀드",
        "국내",
        "해외",
        "가장",
        "제일",
        "높은",
        "낮은",
        "큰",
        "작은",
        "많은",
        "적은",
        "상위",
        "하위",
        "기준",
        "기준으로",
        "좋은",
        "비교해",
        "비교하여",
        "비교하려고",
        "단위로",
        "수익률",
        "보다",
        "대상으로",
        "골라",
        "남겨",
        "추려",
        "추린",
        "뽑아",
        "뽑고",
        "선택하고",
        "통합",
        "주세요",
        "알려",
        "보여",
        "제시",
        "정리",
    }
    exact_risk_ranges = [match.span() for match in _EXACT_RISK.finditer(question)]
    for match in _POTENTIAL_SCHEMA_GAP.finditer(question):
        span = match.group("span").strip()
        normalized_span = span.casefold().strip("._/-")
        contextual_raw_qualifier = span in {"원천값", "수치"} and _known_field_surface(
            question[max(0, match.start() - 40) : match.start()]
        )
        if (
            _overlaps_any(match.start(), match.end(), resolved_ranges)
            or any(
                match.start() < right and match.end() > left for left, right in exact_risk_ranges
            )
            or normalized_span in blocked_tokens
            or contextual_raw_qualifier
            or re.fullmatch(
                r"(?:상품|종목|것|개|건)(?:을|를|은|는|이|가)?",
                span,
                flags=re.IGNORECASE,
            )
            is not None
            or _DIRECTION_METRIC.fullmatch(span) is not None
            or _known_field_surface(span)
            # The span stops at the first word, so "2Y 수익률" arrives as "수익률".
            # Re-read it with the text in front so a registry surface whose
            # leading token is a number or a code still counts as mapped.
            or _known_field_surface(
                question[max(0, match.start() - 40) : match.start() + len(span)]
            )
        ):
            continue
        matches.append(match)
    return matches


class SemanticCoverageGate:
    """Reject semantic downgrades from a constrained request to a generic search.

    This gate is intentionally lexical and conservative. It does not invent a
    field mapping: when a phrase can name more than one metric or requires an
    action outside the read-only Oracle, it returns a control issue instead.
    """

    def __init__(self, *, schema_link_gaps_enabled: bool = False) -> None:
        if type(schema_link_gaps_enabled) is not bool:
            raise TypeError("schema_link_gaps_enabled must be a boolean")
        self.schema_link_gaps_enabled = schema_link_gaps_enabled

    def evaluate(
        self,
        question: str,
        *,
        interaction_intent: str | None = None,
        check_exclusions: bool = True,
    ) -> SemanticCoverageDecision:
        # Explicit refusals such as ``추천 말고 데이터 순위만`` describe what
        # the caller does *not* want. Reuse the SafetyEnvelope's bounded
        # negation normalizer so a negated policy noun cannot become a fake
        # residual condition.
        normalized = safety_policy_surface(
            mask_response_presentation(normalize_user_question(question))
        ).strip()
        scope_surface = data_scope_policy_surface(normalized)
        if interaction_intent in {"clarify", "unsupported"}:
            return SemanticCoverageDecision()

        unsupported: list[str] = []
        ambiguities: list[str] = []
        schema_link_gaps: list[str] = []
        alias_families = tuple(
            family
            for family, pattern in _ALIAS_FAMILY_PATTERNS.items()
            if pattern.search(normalized)
        )
        if not alias_families:
            typed_aliases = typed_alias_matches(normalized)
        else:
            typed_aliases = tuple(
                {
                    (item.rule_id, item.start, item.end): item
                    for family in alias_families
                    for item in typed_alias_matches(normalized, family)
                }.values()
            )
        rank_aliases = tuple(item for item in typed_aliases if item.operation == "rank")
        alias_ranges = tuple((item.start, item.end) for item in typed_aliases)

        if match := _TRANSACTIONAL_ACTION.search(normalized):
            unsupported.append(match.group(0))
        if match := _EXTERNAL_SOURCE_ACTION.search(scope_surface):
            unsupported.append(match.group(0))
        if match := _CREATIVE_ACTION.search(normalized):
            unsupported.append(match.group(0))
        if match := _UNSUPPORTED_DATA_ACTION.search(normalized):
            unsupported.append(match.group(0))
        if match := _UNSUPPORTED_PERCENTILE_FILTER.search(normalized):
            unsupported.append(match.group(0))
        if match := _UNSUPPORTED_ROW_DATE_ARITHMETIC.search(normalized):
            unsupported.append(match.group(0))
        if match := _UNSUPPORTED_DERIVED_AGGREGATE_THRESHOLD.search(normalized):
            unsupported.append(match.group(0))
        if match := _INVALID_BUYABLE_QUANTITY_METRIC.search(normalized):
            unsupported.append(match.group(0))
        if match := _NEGATED_RANKING.search(normalized):
            # Negating an ordering does not identify its opposite order.  For
            # example, ``AUM 큰 순이 아닌`` could mean ascending, no sort, or
            # a different metric.  Never silently execute the positive cue.
            ambiguities.append(match.group(0))
        # An explicit but absent credit-rating value is a safe empty-set lookup.
        # The Field Registry marks only credit_rating for this policy; it is
        # executed through parameterized SQL and returns NOT_FOUND rather than
        # being misreported as an unsupported action.
        if match := _UNAVAILABLE_SNAPSHOT_FIELD.search(normalized):
            unsupported.append(match.group(0))
        if (
            (match := _OVERSEAS_ONE_YEAR_FIELD.search(normalized))
            and set(alias_families) == {ProductFamily.OVERSEAS_ETP}
        ):
            # The official overseas table has only a one-day return.  A mixed
            # family request may still run for the eligible domestic/fund
            # branches and explicitly report the overseas exclusion; only an
            # overseas-only request is wholly unsupported here.
            unsupported.append(match.group(0))
        if match := _UNAPPROVED_RELATION_FILTER.search(normalized):
            allocation_ratio_rank = any(
                item.field_id
                in {
                    "domestic_stock_ratio_pct",
                    "domestic_bond_ratio_pct",
                    "overseas_stock_ratio_pct",
                    "overseas_bond_ratio_pct",
                }
                for item in rank_aliases
            )
            if allocation_ratio_rank and "비중" in match.group(0):
                # A trailing ``비중`` can refer back to one approved public-fund
                # allocation field; it is not a company/constituent relation.
                pass
            elif (
                re.search(r"최근에\s*(?:새로\s*)?편입된", normalized, re.IGNORECASE)
                or _VAGUE_RELATION_SCOPE.search(normalized) is not None
            ):
                ambiguities.append(match.group(0))
            else:
                unsupported.append(match.group(0))
        if match := _UNAPPROVED_DOCUMENT_KNOWLEDGE.search(normalized):
            unsupported.append(match.group(0))
        # A comparison between two fields of every row is a different
        # operation from filtering one field by a literal or by its cohort
        # average/median.  Until a typed cross-field expression exists, never
        # execute one field while silently dropping the other.
        directional_metrics = list(_DIRECTION_METRIC.finditer(normalized))
        explicit_directional_metrics = list(
            _EXPLICIT_DIRECTIONAL_METRIC.finditer(normalized)
        )
        numeric_referent = re.search(
            r"(?:그|이|해당)\s*(?:값|수치)(?:이|가|을|를|은|는)?"
            r".{0,12}?(?:큰|높은|많은|작은|낮은|적은|상위|하위|"
            r"오름차순|내림차순)",
            normalized,
            re.IGNORECASE,
        )
        if numeric_referent is not None and len(alias_families) == 1:
            # A pronoun can inherit a ranking field only when the preceding
            # clause exposes exactly one numeric owner.  This is deliberately
            # re-checked at the coverage boundary rather than trusting the
            # compiler: silently choosing between ``표면이율`` and
            # ``매수수익률`` would execute a different question.
            family = alias_families[0]
            numeric_owners = {
                mention.field_id
                for capability in ("queryable", "sortable")
                for mention in registry_field_mentions(
                    normalized,
                    family.value,
                    capability,
                    value_types=frozenset({ValueType.NUMBER}),
                )
                if mention.start < numeric_referent.start()
            }
            if len(numeric_owners) > 1:
                ambiguities.append(numeric_referent.group(0))
        direct_named_anchor_relative = re.search(
            r"(?<![A-Z0-9])(?:A?\d{6}|KR[A-Z0-9]{10}|"
            r"(?!(?:ETF|ETN)\b)[A-Z][A-Z0-9.]{1,19})"
            r"(?:\)|\s)*(?:보다|대비)",
            normalized,
            re.IGNORECASE,
        ) is not None or re.search(
            r"기준\s*(?:상품|종목|채권|펀드|ETF)\s*\([A-Z0-9.]{2,20}\)"
            r".{0,120}?(?:이|그|해당)\s*(?:기준\s*)?(?:상품|종목|채권|펀드|ETF)"
            r"\s*(?:보다|대비)",
            normalized,
            re.IGNORECASE,
        ) is not None
        # Natural Korean often declares an exact reference product first and
        # places the metric before the anaphor later: ``A(ID)를 기준으로 ...
        # 종가가 이 상품보다 큰``.  This only prevents a shallow false
        # UNSUPPORTED; DB identity and every executable clause are still
        # proven by the Compound compiler and authorization ledger.
        declared_anchor = re.search(
            r"(?<![A-Z0-9])(?:A?\d{6}|KR[A-Z0-9]{10}|"
            r"(?!(?:ETF|ETN)\b)[A-Z][A-Z0-9.]{1,19})"
            r"(?:\)|\]|[’”'\"])?\s*(?:을|를)?\s*"
            r"(?:비교\s*)?기준(?:점|\s*상품|\s*종목)?(?:으로)?",
            normalized,
            re.IGNORECASE,
        )
        anaphoric_relative = re.search(
            r"(?:"
            r"(?:이|그|해당)\s*(?:기준\s*)?"
            r"(?:상품|종목|펀드|ETF|ETN|ETP|채권)\s*"
            r"(?:(?:이상|이하|초과|미만)인|(?:보다|대비))|"
            r"[^,.;!?]{1,64}?(?:이|가|은|는)\s*"
            r"(?:이|그|해당)\s*(?:기준\s*)?"
            r"(?:상품|종목|펀드|ETF|ETN|ETP|채권)\s*"
            r"(?:(?:이상|이하|초과|미만)인|(?:보다|대비)\s*"
            r"(?:더\s*)?(?:큰|작은|높은|낮은|많은|적은|긴|짧은))"
            r")",
            normalized,
            re.IGNORECASE,
        )
        named_anchor_relative = direct_named_anchor_relative or (
            declared_anchor is not None and anaphoric_relative is not None
        )
        repeated_metric_surfaces = {
            re.sub(r"\s+", "", item.group(0)).casefold() for item in directional_metrics
        }
        if (
            len(directional_metrics) >= 2
            and len(repeated_metric_surfaces) >= 2
            and "보다" in normalized
            and re.search(r"평균|중앙\s*값|중위수|메디안", normalized) is None
            and not named_anchor_relative
        ):
            left, right = directional_metrics[0], directional_metrics[1]
            comparison_surface = normalized[left.start() : right.end()]
            unsupported.append(comparison_surface)
        # Import locally to keep the lexical gate usable during module
        # initialization; compound_query only imports linker lazily.  Compute
        # this independently of the directional-regex count: a valid stage can
        # state ``상위`` rather than an adjective such as ``높은``.
        from finance_agent_core.agent.compound_query import (
            requires_typed_two_stage_plan,
        )

        typed_two_stage_request = requires_typed_two_stage_plan(normalized)
        independent_family_metrics = _metrics_have_distinct_family_owners(
            normalized,
            explicit_directional_metrics,
        )
        if (
            len(explicit_directional_metrics) >= 2
            and not typed_two_stage_request
            and not independent_family_metrics
            and not named_anchor_relative
        ):
            # Two independently directed metrics do not define a deterministic
            # ordering by themselves.  ``종가가 높은 ... 비용이 낮은`` could
            # mean a threshold, a lexicographic sort, a weighted score, or a
            # two-stage shortlist.  A numeric bound/explicit shortlist is
            # compiled elsewhere; without one, never discard either metric.
            left, right = explicit_directional_metrics[0], explicit_directional_metrics[1]
            ambiguities.append(normalized[left.start() : right.end()])
        for match in _UNDERSPECIFIED_DECISION_RULE.finditer(normalized):
            if not _overlaps_any(match.start(), match.end(), alias_ranges):
                ambiguities.append(match.group(0))

        # Subjective preferences are ambiguous for every read intent, not only
        # SEARCH.  Keeping this check below the DETAIL/COMPARE early return let
        # an extra criterion such as ``인기도도 함께 반영`` disappear from a
        # closed comparison while the named products and numeric metric still
        # executed.  A Registry-backed alias may still own the same literal.
        for match in _GENERIC_PREFERENCE.finditer(normalized):
            popularity = match.group(0).casefold().startswith("인기")
            if (
                popularity
                and not _overlaps_any(match.start(), match.end(), alias_ranges)
            ) or (
                not popularity
                and not _covered_by_resolved_alias(match.start(), match.end(), alias_ranges)
            ):
                ambiguities.append(match.group(0))

        # Routed DETAIL/COMPARE/EXPLAIN requests have dedicated fail-closed parsers
        # with more specific control messages.  The legacy agent calls this
        # gate without an interaction intent, so it still cannot bypass the
        # checks below with a bare comparison metric.
        if interaction_intent in {"detail", "compare", "explain"}:
            return SemanticCoverageDecision(
                ambiguity_spans=_unique(ambiguities),
                unsupported_spans=_unique(unsupported),
            )

        if match := _CROSS_FAMILY_MIXED_RANKING.search(normalized):
            ambiguities.append(match.group(0))
        if match := _VAGUE_PERIOD_COMPARISON.search(normalized):
            ambiguities.append(match.group(0))
        for match in _ADDITIONAL_CRITERION.finditer(normalized):
            span = match.group("span")
            if (
                _known_field_surface(span)
                or _KNOWN_EXTERNAL_CRITERION.search(span) is not None
                or _overlaps_any(match.start("span"), match.end("span"), alias_ranges)
            ):
                continue
            ambiguities.append(span)
        if match := _PERSONAL_SUITABILITY.search(normalized):
            ambiguities.append(match.group(0))
        if match := _UNSUPPORTED_INVERSE_EXCLUSION.search(normalized):
            ambiguities.append(match.group(0))
        for match in _DIRECTIONLESS_SORT.finditer(normalized):
            if _DIRECTION_TERM.search(match.group(0)) is None:
                if (
                    (
                        len({item.field_id for item in rank_aliases}) == 1
                        or len(list(_EXPLICIT_PERIOD_RETURN.finditer(normalized))) == 1
                    )
                    and _GLOBAL_RANKING_SCOPE.search(normalized) is not None
                    and _DIRECTION_TERM.search(normalized[match.end() :]) is not None
                ):
                    continue
                ambiguities.append(match.group(0))

        return_surface = _EXPLICIT_RETURN.sub(" ", normalized)
        resolved_return_fields = {
            item.field_id
            for item in rank_aliases
            if "return_pct" in item.field_id or "yield_pct" in item.field_id
        }
        explicit_period_returns = list(_EXPLICIT_PERIOD_RETURN.finditer(normalized))
        if match := _BARE_RETURN.search(return_surface):
            # A repeated bare "수익률" in a numeric threshold refers to the
            # one explicit period metric already locked in the same question.
            # Multiple resolved return fields remain ambiguous and fail closed.
            if (
                not _overlaps_any(match.start(), match.end(), alias_ranges)
                and len(resolved_return_fields) != 1
                and len(explicit_period_returns) != 1
            ):
                ambiguities.append(match.group(0))

        if (match := _CHEAP.search(normalized)) and _EXPLICIT_CHEAP_METRIC.search(
            normalized
        ) is None:
            if not _overlaps_any(match.start(), match.end(), alias_ranges):
                ambiguities.append(match.group(0))
        if (match := _LIQUIDITY.search(normalized)) and _EXPLICIT_LIQUIDITY_METRIC.search(
            normalized
        ) is None:
            if not _overlaps_any(match.start(), match.end(), alias_ranges):
                ambiguities.append(match.group(0))
        if (match := _SIZE.search(normalized)) and _EXPLICIT_SIZE_METRIC.search(normalized) is None:
            if not _overlaps_any(match.start(), match.end(), alias_ranges):
                ambiguities.append(match.group(0))
        if (
            (match := _RISK.search(normalized))
            and _EXACT_RISK.search(normalized) is None
            and _RISK_GROUP.search(normalized) is None
            and _RISK_PROJECTION.search(normalized) is None
            and not _overlaps_any(match.start(), match.end(), alias_ranges)
        ):
            ambiguities.append(match.group(0))
        if match := _VAGUE_STABILITY.search(normalized):
            ambiguities.append(match.group(0))
        for match in _UNMAPPED_METRIC.finditer(normalized):
            if not _metric_has_explicit_mapping(normalized, match) and not _overlaps_any(
                match.start(), match.end(), alias_ranges
            ):
                ambiguities.append(match.group(0))

        explicit_period_ranges = tuple(
            match.span() for match in _EXPLICIT_PERIOD_RETURN.finditer(normalized)
        )
        schema_gap_matches = _schema_link_gap_matches(
            normalized,
            resolved_ranges=(*alias_ranges, *explicit_period_ranges),
        )
        detected_schema_gaps = [match.group("span") for match in schema_gap_matches]
        if (
            len(directional_metrics) == 1
            and _EXPLICIT_NUMERIC_BOUND.search(normalized) is not None
            and _DIRECTION_TERM.search(normalized) is not None
        ):
            # ``그 값``/``그것부터`` is a deterministic reference only when
            # one explicit metric owns both a numeric bound and a direction.
            # This resolution intentionally stays disabled when two metrics
            # appear, preserving the fail-closed ambiguity boundary.
            detected_schema_gaps = [
                span
                for span in detected_schema_gaps
                if re.fullmatch(
                    r"(?:(?:그|이|해당)\s*)?"
                    r"(?:값(?:이|을|은|는)?|것(?:부터|을|이|은|는)?)",
                    span.strip(),
                    flags=re.IGNORECASE,
                )
                is None
            ]
        if self.schema_link_gaps_enabled:
            schema_link_gaps.extend(detected_schema_gaps)
        else:
            ambiguities.extend(detected_schema_gaps)

        explanation_ranges = tuple(
            item.span() for item in _ANSWER_ORDER_EXPLANATION.finditer(normalized)
        )
        generic_threshold_ranges = tuple(
            item.span() for item in _GENERIC_NUMERIC_THRESHOLD.finditer(normalized)
        )
        explicit_numeric_bound_ranges = tuple(
            item.span() for item in _EXPLICIT_NUMERIC_BOUND.finditer(normalized)
        )
        aggregate_group_order_ranges = (
            tuple(item.span() for item in _AGGREGATE_GROUP_LABEL_ORDER.finditer(normalized))
            if interaction_intent == "aggregate"
            else ()
        )
        exact_risk_ranges = tuple(item.span() for item in _EXACT_RISK.finditer(normalized))
        for match in _DIRECTION_TERM.finditer(normalized):
            if _overlaps_any(match.start(), match.end(), exact_risk_ranges):
                continue
            if _overlaps_any(match.start(), match.end(), aggregate_group_order_ranges):
                continue
            # Comparative adjectives inside an explicit numeric predicate
            # (``보수 0.25%보다 낮은``, ``수익률 4% 밑인``) describe the
            # filter boundary, not a second unlabelled ranking instruction.
            if _overlaps_any(
                match.start(), match.end(), explicit_numeric_bound_ranges
            ):
                continue
            if match.group(0).strip() == "먼저":
                if _overlaps_any(match.start(), match.end(), explanation_ranges):
                    continue
                if _is_earlier_maturity_direction(normalized, match):
                    # ``만기가 먼저 오는`` and ``상환이 먼저 되는`` have an
                    # explicit time-to-maturity owner.  This is an ordinary
                    # ASC ranking, not an unbound workflow instruction.
                    continue
                if typed_two_stage_request:
                    continue
                if _is_filter_then_rank_sequence(
                    normalized,
                    match,
                    typed_aliases,
                ):
                    continue
                # Never attach a procedural ``먼저`` to an unrelated metric
                # elsewhere in the sentence.  Unless both workflow stages are
                # explicit, it remains a genuine unresolved instruction.
                ambiguities.append(match.group(0))
                continue
            if len(explicit_period_returns) == 1 and _overlaps_any(
                match.start(), match.end(), generic_threshold_ranges
            ):
                continue
            # A direction is covered only when it is part of the typed alias
            # itself.  The wider ``start - alias_end <= 8`` tolerance used for
            # descriptive preference words is unsafe here: a product noun can
            # fit in that gap (``비율이 높은 공모펀드 하위``), causing the
            # contradictory trailing ordinal to disappear.  Compatible
            # repeated directions are handled explicitly below.
            if _overlaps_any(match.start(), match.end(), alias_ranges):
                continue
            if (
                len(explicit_period_returns) == 1
                and _GLOBAL_RANKING_SCOPE.search(normalized) is not None
            ):
                continue
            if _compatible_trailing_alias_direction(normalized, match, rank_aliases):
                continue
            if any(
                candidate.start() <= match.start() <= candidate.end() + 8
                for candidate in schema_gap_matches
            ):
                continue
            if _opens_known_field_surface(normalized[match.start() : match.start() + 40]):
                continue
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
                missing_value = _MISSING_VALUE_EXCLUSION.search(exclusion_surface)
                if (
                    not is_safe
                    and missing_value is not None
                    and missing_value.start() <= match.start() <= missing_value.end()
                    and len(directional_metrics) == 1
                    and _DIRECTION_TERM.search(normalized) is not None
                ):
                    # Official numeric ordering already excludes 0/NULL. A
                    # missing-value exclusion adds no second filter when one
                    # ranked metric owns the request. Without that one owner,
                    # the wording remains fail-closed.
                    is_safe = True
                ranked_missing = _RANKED_ZERO_OR_MISSING_EXCLUSION.search(
                    exclusion_surface
                )
                if (
                    not is_safe
                    and ranked_missing is not None
                    and ranked_missing.start() <= match.start() <= ranked_missing.end()
                    and len({item.field_id for item in rank_aliases}) == 1
                ):
                    # The organizer-defined numeric ranking policy already
                    # excludes zero/NULL.  A clause-level reminder is safe only
                    # when exactly one typed ranking field owns the request.
                    is_safe = True
                if not is_safe:
                    ambiguities.append(match.group(0))

        if interaction_intent in {None, "search"}:
            explicit_name_filter = _EXPLICIT_NAME_FILTER.search(normalized) is not None
            batch_families = set(alias_families)
            document_references = extract_dated_official_document_references(normalized)
            branded_product_matches = tuple(
                match
                for match in _BRANDED_PRODUCT.finditer(normalized)
                if not overlaps_dated_document_reference(
                    match.start(),
                    match.end(),
                    document_references,
                )
            )
            if branded_product_matches:
                batch_families.add(ProductFamily.DOMESTIC_ETP)
            family_identity_batch = (
                _PER_FAMILY_IDENTITY_BATCH.search(normalized) is not None
                and len(batch_families) > 1
            )
            if not explicit_name_filter:
                for match in _QUOTED_PRODUCT.finditer(normalized):
                    if match.group(1).strip().upper() not in {"ETF", "ETN", "ETP"}:
                        ambiguities.append(match.group(0))
                if branded_product_matches and not family_identity_batch:
                    ambiguities.append(branded_product_matches[0].group(0))
                if match := _UNLABELED_PRODUCT_NAME.search(normalized):
                    ambiguities.append(match.group(0))

        if _LATEST_DB_VALUE.search(normalized) is not None:
            ambiguities = [item for item in ambiguities if item.strip() != "최신"]

        return SemanticCoverageDecision(
            ambiguity_spans=_unique(ambiguities),
            unsupported_spans=_unique(unsupported),
            schema_link_gap_spans=_unique(schema_link_gaps),
        )


def is_bare_direction_span(span: str) -> bool:
    """Report whether an ambiguity span is only a ranking direction word."""

    return _DIRECTION_TERM.fullmatch(span.strip()) is not None


def is_bare_risk_span(span: str) -> bool:
    """Report whether an ambiguity span is only a risk mention without a grade."""

    return _RISK.fullmatch(span.strip()) is not None


def is_branded_product_span(span: str) -> bool:
    """Report whether an ambiguity span is only a branded ETP product name."""

    return _BRANDED_PRODUCT.fullmatch(span.strip()) is not None
