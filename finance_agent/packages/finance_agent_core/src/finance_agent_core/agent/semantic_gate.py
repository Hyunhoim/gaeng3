from __future__ import annotations

import re
from dataclasses import dataclass

from finance_agent_core.agent.presentation import mask_response_presentation
from finance_agent_core.agent.safety import data_scope_policy_surface, normalize_user_question
from finance_agent_core.agent.semantic_aliases import (
    TypedAliasMatch,
    typed_alias_matches,
)
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
_INVALID_BUYABLE_QUANTITY_METRIC = re.compile(
    r"(?:매수\s*가능\s*수량|매수가능수량|주문\s*가능\s*수량)"
    r"(?:이|가|은|는|을|를)?\s*"
    r"(?:\d|이상|이하|초과|미만|많|적|높|낮|순|정렬|상위|하위)",
    re.IGNORECASE,
)
_UNAVAILABLE_SNAPSHOT_FIELD = re.compile(
    r"(?=.*(?:공모\s*펀드|공모펀드))"
    r"(?=.*(?:1\s*주|일주일|한\s*주).{0,16}?(?:수익률|수익|성과|성적))",
    re.IGNORECASE,
)
_UNAPPROVED_RELATION_FILTER = re.compile(
    r"(?:섹터|테마|산업)(?:에|로|를|을)?\s*(?:투자|편입|포함)|"
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
    r"[0-9A-Za-z가-힣&·+._/-]{2,60}\s*관련\s*(?:된\s*)?"
    r"(?:(?:국내|해외)\s*)?(?:투자\s*)?"
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
    r"1\s*개월|한\s*달|월간|1M|3\s*개월|석\s*달|3M|6\s*개월|반년|6M|"
    r"18\s*개월|1\s*년|연간|1Y|2\s*년|3\s*년|5\s*년|YTD|"
    r"연초\s*(?:이후|대비))\s*(?:수익률|수익|성과|return)|"
    r"(?:매수|세후|세전)\s*수익률|민평\s*(?:(?:기준\s*)?수익률|금리)|"
    r"민평에서\s*(?:계산|산출)(?:된|한)?\s*(?:수익률|금리)|"
    r"(?:민간평가\s*금리|평가기관\s*수익률)|(?:연환산\s*)?분배\s*수익률|"
    r"(?:표면\s*(?:이율|금리)|쿠폰\s*금리)",
    re.IGNORECASE,
)
_EXPLICIT_PERIOD_RETURN = re.compile(
    r"(?:1\s*일|하루|당일|오늘|일간|1D|1\s*주|일주일|주간|1W|"
    r"1\s*개월|한\s*달|월간|1M|3\s*개월|석\s*달|3M|6\s*개월|반년|6M|"
    r"18\s*개월|1\s*년|연간|1Y|2\s*년|3\s*년|5\s*년|YTD|"
    r"연초\s*(?:이후|대비))\s*(?:수익률|수익|성과|return)",
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
    r"(?:AUM|순자산(?:총액)?|운용\s*자산|발행\s*(?:잔액|금액))"
    r"\s*(?:규모|기준|으로|을\s*기준)|운용\s*규모|총\s*발행\s*규모",
    re.IGNORECASE,
)
_RISK = re.compile(r"리스크|위험(?:\s*등급|도|성)?", re.IGNORECASE)
_EXACT_RISK = re.compile(
    r"(?:매우높은|높은|다소높은|보통|낮은|매우낮은)위험\s*\([1-6]등급\)|"
    r"(?:매우높은|높은|다소높은|보통|낮은|매우낮은)위험\s*[1-6]등급|"
    r"(?:매우\s*높은|높은|다소\s*높은|보통|낮은|매우\s*낮은)\s*위험",
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
    r"대표적(?:인|이다)?|인기\s*(?:있는|많은)|효율(?:이|가)?\s*높(?:은|다)?|"
    r"부담(?:이|가)?\s*(?:(?:적|작)(?:은|다)?|없(?:는|다)?|되지\s*않(?:는|다)?)|"
    r"무난(?:한|하다)?|유망(?:한|하다)?|알짜(?:인|다)?|"
    r"조건(?:이|가)?\s*(?:괜찮|좋)|비용\s*대비\s*괜찮(?:은|다)?|"
    r"추천할\s*만한|(?<!않은\s)좋은|괜찮은|싼",
    re.IGNORECASE,
)
_UNMAPPED_METRIC = re.compile(
    r"(?P<metric>가격|비용|변동성|테마|섹터|산업|ESG|친환경|금리|이율|기간|수량)"
    r".{0,16}?(?:높|낮|크|작|많|적|길|짧|저렴|비싸)",
    re.IGNORECASE,
)
_DIRECTION_TERM = re.compile(
    r"(?:가장\s*|제일\s*)?"
    r"(?:높은|낮은|큰|작은|많은|적은|긴|짧은|빠른|오래된|센|"
    r"상위|하위|오름차순|내림차순|최신|좋은|빨리|먼저|가까운)|"
    r"highest\s*[- ]to\s*[- ]lowest|lowest\s*[- ]to\s*[- ]highest|"
    r"ascending|descending",
    re.IGNORECASE,
)
_GENERIC_NUMERIC_THRESHOLD = re.compile(
    r"-?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?\s*"
    r"(?:%|퍼센트)?\s*보다\s*(?:큰|높은|작은|낮은)",
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
    r"(?:총\s*)?발행\s*(?:잔액|금액|규모|물량)|남은\s*발행\s*물량|"
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
    r"3\s*개월|석\s*달|3M|6\s*개월|반년|6M|18\s*개월|"
    r"1\s*년|일\s*년|1Y|2\s*년|3\s*년|5\s*년|YTD|"
    r"연초\s*이후)\s*(?:수익률|수익|성과|return)|"
    r"매수\s*수익률|세후\s*수익률|표면\s*(?:이자율|이율|금리)|"
    r"쿠폰(?:\s*금리)?|액면\s*쿠폰|액면에?\s*붙은\s*이자율|"
    r"잔존\s*(?:일수|일|기일|기간)|잔여\s*일|"
    r"(?:상환|만기)(?:까지)?\s*남은\s*(?:일수|날짜|날|기간)|"
    r"(?:상환|만기)(?:일|일자)?|"
    r"듀레이션|duration|금리\s*민감도|동적\s*(?:지표\s*)?기준일|정적\s*(?:지표\s*)?기준일|"
    r"상품명|짧은\s*이름|이름|티커|종목\s*코드|"
    r"규모|유동성|리스크|위험(?:\s*등급|도|성)?|"
    r"가격|비용|변동성|테마|섹터|산업|ESG|친환경|금리|이율|기간|수량)"
)
_DIRECTION_METRIC = re.compile(_DIRECTION_METRIC_PATTERN, re.IGNORECASE)
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
    r"(?:걸지|적용하지)\s*말고",
    re.IGNORECASE,
)
_SAFE_EXCLUSION = re.compile(
    r"(?:ETF|ETN)(?:가|이|은|는|을|를|\s)*"
    r"(?:아닌|제외(?:하고|한|해)?|이외(?:의|에는|로)?|말고|빼고|"
    r"외(?:의|에는|로)?)|"
    r"거래\s*(?:중지|정지)(?:된|인)?\s*(?:상품|것)?"
    r"(?:은|는|을|를)?\s*(?:제외(?:하고|한|해)?|말고|빼고)|"
    r"미국(?:을)?\s*제외(?:한)?\s*글로벌(?:\s*지역)?(?:\s*\(Global\s*Ex\s*US\))?|"
    r"따로\s*나누지\s*말고|"
    r"이자(?:를)?\s*(?:제외|뺀)\s*평가(?:단가|가격)|"
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
    r"3\s*개월|석\s*달|3M|6\s*개월|반년|6M|18\s*개월|"
    r"1\s*년|일\s*년|1Y|2\s*년|3\s*년|5\s*년|YTD|"
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


def _unique(values: list[str]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(value.strip() for value in values if value.strip()))


def _metric_has_explicit_mapping(question: str, match: re.Match[str]) -> bool:
    field_surface = question[max(0, match.start() - 40) : match.end()]
    if _known_field_surface(field_surface):
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
    # A trailing "높은 순" can refer back to one and only one explicit metric
    # used earlier as a threshold (for example, "1주 수익률 5% 이상 ... 높은
    # 순"). Multiple possible metric antecedents remain ambiguous.
    return len(list(_DIRECTION_METRIC.finditer(question))) == 1


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
                or re.search(rf"{re.escape(candidate)}(?:이|가|은|는|을|를)?$", normalized)
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
    """Accept a redundant trailing sort cue for one nearest resolved alias."""

    if re.match(r"\s*순", question[match.end() :]) is None:
        return False
    nearby = [item for item in alias_matches if 0 <= match.start() - item.end <= 48]
    if not nearby:
        return False
    nearest_gap = min(match.start() - item.end for item in nearby)
    nearby = [item for item in nearby if match.start() - item.end == nearest_gap]
    if len({item.field_id for item in nearby}) != 1:
        return False
    surface = match.group(0)
    ascending = re.search(r"낮|작|적|짧|빠른|하위|오름차순", surface) is not None
    descending = re.search(r"높|크|많|긴|센|상위|내림차순", surface) is not None
    if ascending == descending:
        return False
    expected = "asc" if ascending else "desc"
    return all(item.direction.value == expected for item in nearby)


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
        normalized = mask_response_presentation(normalize_user_question(question)).strip()
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
        if match := _INVALID_BUYABLE_QUANTITY_METRIC.search(normalized):
            unsupported.append(match.group(0))
        # An explicit but absent credit-rating value is a safe empty-set lookup.
        # The Field Registry marks only credit_rating for this policy; it is
        # executed through parameterized SQL and returns NOT_FOUND rather than
        # being misreported as an unsupported action.
        if match := _UNAVAILABLE_SNAPSHOT_FIELD.search(normalized):
            unsupported.append(match.group(0))
        if match := _UNAPPROVED_RELATION_FILTER.search(normalized):
            unsupported.append(match.group(0))
        if match := _UNAPPROVED_DOCUMENT_KNOWLEDGE.search(normalized):
            unsupported.append(match.group(0))

        # Routed DETAIL/COMPARE/EXPLAIN requests have dedicated fail-closed parsers
        # with more specific control messages.  The legacy agent calls this
        # gate without an interaction intent, so it still cannot bypass the
        # checks below with a bare comparison metric.
        if interaction_intent in {"detail", "compare", "explain"}:
            return SemanticCoverageDecision(unsupported_spans=_unique(unsupported))

        if match := _CROSS_FAMILY_MIXED_RANKING.search(normalized):
            ambiguities.append(match.group(0))
        if match := _VAGUE_PERIOD_COMPARISON.search(normalized):
            ambiguities.append(match.group(0))
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
        ):
            ambiguities.append(match.group(0))
        if match := _VAGUE_STABILITY.search(normalized):
            ambiguities.append(match.group(0))
        for match in _GENERIC_PREFERENCE.finditer(normalized):
            if not _covered_by_resolved_alias(match.start(), match.end(), alias_ranges):
                ambiguities.append(match.group(0))

        for match in _UNMAPPED_METRIC.finditer(normalized):
            if not _metric_has_explicit_mapping(normalized, match) and not _overlaps_any(
                match.start(), match.end(), alias_ranges
            ):
                ambiguities.append(match.group(0))

        schema_gap_matches = _schema_link_gap_matches(
            normalized,
            resolved_ranges=alias_ranges,
        )
        detected_schema_gaps = [match.group("span") for match in schema_gap_matches]
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
        exact_risk_ranges = tuple(item.span() for item in _EXACT_RISK.finditer(normalized))
        for match in _DIRECTION_TERM.finditer(normalized):
            if _overlaps_any(match.start(), match.end(), exact_risk_ranges):
                continue
            if match.group(0).strip() == "먼저" and _overlaps_any(
                match.start(), match.end(), explanation_ranges
            ):
                continue
            if len(explicit_period_returns) == 1 and _overlaps_any(
                match.start(), match.end(), generic_threshold_ranges
            ):
                continue
            if _covered_by_resolved_alias(match.start(), match.end(), alias_ranges):
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
            batch_families = set(alias_families)
            if _BRANDED_PRODUCT.search(normalized) is not None:
                batch_families.add(ProductFamily.DOMESTIC_ETP)
            family_identity_batch = (
                _PER_FAMILY_IDENTITY_BATCH.search(normalized) is not None
                and len(batch_families) > 1
            )
            if not explicit_name_filter:
                for match in _QUOTED_PRODUCT.finditer(normalized):
                    if match.group(1).strip().upper() not in {"ETF", "ETN", "ETP"}:
                        ambiguities.append(match.group(0))
                if (match := _BRANDED_PRODUCT.search(normalized)) and not family_identity_batch:
                    ambiguities.append(match.group(0))
                if match := _UNLABELED_PRODUCT_NAME.search(normalized):
                    ambiguities.append(match.group(0))

        if _LATEST_DB_VALUE.search(normalized) is not None:
            ambiguities = [item for item in ambiguities if item.strip() != "최신"]

        return SemanticCoverageDecision(
            ambiguity_spans=_unique(ambiguities),
            unsupported_spans=_unique(unsupported),
            schema_link_gap_spans=_unique(schema_link_gaps),
        )
