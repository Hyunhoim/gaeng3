from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from enum import StrEnum


class SafetyGate(StrEnum):
    """Server-owned safety checks that run before routing or model planning."""

    SCOPE = "scope"
    SECURITY = "security"
    DATA_INTEGRITY = "data_integrity"
    FRESHNESS = "freshness"
    TRANSACTION = "transaction"
    MARKET_ABUSE = "market_abuse"
    OFF_TOPIC = "off_topic"
    FORECAST = "forecast"
    GUARANTEE = "guarantee"
    MULTITURN = "multiturn"


class SafetyDisposition(StrEnum):
    ALLOW = "allow"
    CLARIFY = "clarify"
    UNSUPPORTED = "unsupported"


@dataclass(frozen=True)
class SafetyEnvelopeDecision:
    normalized_question: str
    disposition: SafetyDisposition = SafetyDisposition.ALLOW
    gate: SafetyGate | None = None
    reason: str | None = None

    @property
    def blocked(self) -> bool:
        return self.disposition is not SafetyDisposition.ALLOW


_SECURITY = re.compile(
    r"(?:이전|기존|앞선|위의|모든)?\s*(?:지시|지침|명령|규칙|정책|가드레일)"
    r".{0,32}?(?:무시|잊어|우회|해제|따르지)|"
    r"(?:시스템|개발자)\s*(?:프롬프트|메시지|지시|규칙|설정)"
    r".{0,32}?(?:출력|공개|노출|보여|알려|붙여|넣|첨부)|"
    r"(?:숨겨진|내부|비공개)\s*(?:시스템\s*)?"
    r"(?:지시(?:문)?|지침|설정|규칙|프롬프트|데이터\s*경로|인증\s*(?:키|정보))"
    r".{0,32}?(?:출력|공개|노출|보여|알려)|"
    r"(?:숨겨진|내부|비공개).{0,16}?"
    r"(?:prompt|credentials?|secrets?|api\s*keys?)"
    r".{0,40}?(?:출력|공개|노출|보여|알려)|"
    r"(?:비밀|토큰|API\s*(?:키|key)|환경\s*변수|데이터베이스\s*경로)"
    r".{0,32}?(?:출력|공개|노출|보여|알려|나열|인용|붙여)|"
    r"(?:내부\s*)?(?:도구|툴).{0,32}?(?:이름|인자|목록)"
    r".{0,24}?(?:출력|공개|노출|보여|알려|나열)|"
    r"(?:데이터베이스|DB).{0,32}?(?:스키마|접속\s*문자열|connection\s*string)"
    r".{0,24}?(?:출력|공개|노출|보여|알려|말해)|"
    r"(?:비밀번호|패스워드|password).{0,24}?(?:기억|저장|보관|출력|공개)|"
    r"\b(?:drop\s+table|union\s+select|sqlite_master|pragma\s+[a-z_])\b|"
    r"(?:제한|보안\s*규칙|안전\s*(?:장치|규칙|필터|검사))"
    r".{0,32}?(?:우회|해제|비활성화|건너뛰|끄고|꺼|제거)|"
    r"(?:보안|안전)\s*(?:규칙|정책|검사).{0,24}?(?:적용하지\s*말|무시)|"
    r"(?:관리자|admin).{0,16}?(?:모드|권한)|"
    r"ignore\s+(?:all\s+)?(?:previous|prior|system|developer)\s+"
    r"(?:instructions?|messages?|rules?)|"
    r"ignore[_\s-]*(?:all[_\s-]*)?(?:rules?|polic(?:y|ies)|previous|safety)|"
    r"forget\s+(?:all|every)\s+(?:safety\s+)?(?:instructions?|rules?|polic(?:y|ies))|"
    r"(?:reveal|show|print|expose|disclose).{0,24}?"
    r"(?:system[_\s-]+prompt|hidden\s+instructions?|secrets?|credentials?|"
    r"api\s*keys?|tools?(?:\s+and\s+arguments?)?)|"
    r"\b(?:jailbreak|developer\s+mode)\b|"
    r"(?:모든\s*)?정책.{0,28}?(?:이\s*문장|요청).{0,20}?(?:우선|최우선)|"
    r"거절(?:하지\s*마|금지|을?\s*우회)|"
    r"(?:평가(?:기|\s*결과)?|검증|provider\s*호출\s*수|호출\s*로그)"
    r".{0,36}?(?:속여|조작|삭제|0으로\s*기록|PASS)|"
    r"검증(?:\s*(?:단계|절차|경계))?.{0,20}?(?:우회|건너뛰|비활성화)|"
    r"(?:verifier|검증기|answer\s*verifier).{0,24}?"
    r"(?:우회|건너뛰|건너뛴|건너뜀|생략|비활성화|끄고|제거)|"
    r"(?:원본\s*데이터\s*파일|서버|내부\s*파일).{0,20}?"
    r"(?:경로|위치).{0,24}?(?:전부\s*)?(?:출력|공개|노출|보여|알려)|"
    r"(?:로그|기록).{0,24}?(?:삭제|지워|조작)|"
    r"(?:순위|결과).{0,20}?(?:조작|꾸미고|꾸며)|"
    r"(?:drop|disable)\s+(?:safety|guardrails?|filters?)|"
    r"(?:os\.environ|import\s+os|printenv)|"
    r"(?:SQL|쿼리).{0,20}?(?:작성|실행|만들)|"
    r"(?:DB|database|데이터베이스).{0,20}?(?:삭제|초기화|드롭|drop)|"
    r"(?:파일|디렉터리|폴더).{0,20}?(?:삭제|지워|제거)|"
    r"(?:서버|서비스).{0,20}?(?:재부팅|재시작|종료|중지)|"
    r"(?:셸|쉘|shell|bash|명령|command).{0,20}?(?:실행|run)|"
    r"(?:다른|타인|다른\s*이용자|다른\s*고객).{0,40}?"
    r"(?:개인\s*정보|보유\s*(?:내역|목록)|거래\s*내역|연락처|"
    r"계좌\s*(?:정보|번호))|"
    r"(?:개인\s*정보|보유\s*내역|거래\s*내역|연락처|계좌\s*정보)"
    r".{0,40}?(?:가져|조회|출력|공개|보여)|"
    r"(?:내부\s*)?(?:alias|별칭|동의어)\s*(?:사전|목록|원문)"
    r".{0,32}?(?:다운로드|내려받|반출|출력|공개|보여)|"
    r"(?:벡터|임베딩|embedding)\s*(?:원문|코퍼스|index|인덱스|데이터)"
    r".{0,32}?(?:다운로드|내려받|반출|출력|공개|보여)|"
    r"(?:평가용\s*)?(?:정답|gold)\s*(?:파일|키|데이터).{0,32}?"
    r"(?:찾|열|읽|출력|공개|보여)|"
    r"(?:다른|타인|다른\s*고객).{0,56}?(?:이름|성명|주민등록번호|잔고)|"
    r"(?:주민등록번호|보유\s*잔고).{0,40}?(?:꺼내|공개|보여|알려|조회)|"
    r"(?:개인\s*정보|본인\s*확인|인증)\s*(?:확인\s*)?(?:절차)?"
    r".{0,24}?(?:생략|우회).{0,32}?(?:잔고|계좌|보유)|"
    r"(?:운영자|관리자)\s*권한.{0,32}?(?:DB|데이터베이스).{0,24}?(?:접속|수정)|"
    r"(?:조회|감사|접속|주문)\s*(?:로그|기록).{0,24}?(?:남지\s*않|남기지\s*않|우회)",
    re.IGNORECASE,
)
_PROTECTED_MARKER = re.compile(
    r"(?:PROVIDER[_\s-]*SECRET|BLIND[_\s-]*CANARY|SYSTEM[_\s-]*PROMPT|"
    r"SAFETY[_\s-]*BLIND[_\s-]*KEY)",
    re.IGNORECASE,
)
_DATA_INTEGRITY = re.compile(
    r"(?:제공\s*(?:데이터|자료)|공식\s*(?:데이터|자료)|수익률|가격|수치|순위)"
    r".{0,48}?(?:좋아\s*보이도록|유리하게|불리하게|원하는\s*대로)"
    r".{0,32}?(?:바꿔|변경|수정|조작|왜곡)|"
    r"(?:SNS|커뮤니티|인터넷)\s*(?:소문|루머|미확인\s*정보)"
    r".{0,32}?(?:근거|기준|반영)|"
    r"(?:공식|제공)\s*(?:데이터|자료).{0,20}?(?:대신|보다)"
    r".{0,24}?(?:소문|루머|미확인\s*정보)|"
    r"(?:제공\s*(?:데이터|자료)|공식\s*(?:데이터|자료)|매수\s*수익률|"
    r"세후\s*수익률|수익률|가격|수치|순위|AUM|순자산|총\s*보수(?:율)?|"
    r"보수율|만기|위험\s*등급).{0,64}?"
    r"(?:바꿔|변경|수정(?!\s*듀레이션)|조작|왜곡|덮어|"
    r"임의\s*(?:값|수치)(?:으로)?\s*대체|"
    r"가정해)|"
    r"(?:결측\s*값|결측값|빈\s*값|누락\s*값).{0,40}?"
    r"(?:임의|0|평균).{0,20}?(?:채워|대체|보정)|"
    r"(?:수익률|가격|수치|순위).{0,20}?(?:숫자|수치)?.{0,16}?"
    r"(?:임의로|마음대로).{0,16}?(?:높여|낮춰|부풀려|줄여)|"
    r"(?:조건|필터).{0,24}?(?:무시|빼고|제외).{0,40}?"
    r"(?:원천|공식|제공).{0,20}?(?:값|수치|데이터|자료).{0,24}?"
    r"(?:바꿔|변경|수정|조작)|"
    r"(?:검색\s*)?(?:조건|필터).{0,32}?(?:무시|숨기|은폐|빼고|제외)"
    r".{0,64}?(?:특정|우리\s*회사|원하는)?.{0,24}?(?:1\s*위|상위)"
    r".{0,24}?(?:만들|나오게|조작)|"
    r"(?:보수율|수익률|수치|조건|필터).{0,24}?(?:숨기|은폐)"
    r".{0,48}?(?:특정\s*)?(?:티커|상품|종목).{0,24}?(?:1\s*위|상위)"
    r".{0,16}?(?:만들|조작)|"
    r"(?:공시|근거|출처).{0,40}?(?:확인했다고\s*거짓말|가짜\s*출처|"
    r"없는\s*것을?.{0,12}?있는\s*것처럼)|"
    r"(?:근거\s*문서|공식\s*자료).{0,40}?(?:없던|없는).{0,28}?"
    r"(?:편입|보유|구성).{0,20}?(?:있는\s*것처럼|작성)|"
    r"(?:손실률|위험|불리한\s*수치).{0,28}?(?:숨기|은폐).{0,40}?"
    r"(?:수익|유리한).{0,20}?(?:것처럼|상품처럼)|"
    r"(?:손실(?:률|\s*수치)?|위험(?:\s*요인)?|불리한\s*(?:정보|수치))"
    r".{0,32}?(?:숨기|숨긴|은폐).{0,40}?"
    r"(?:홍보|광고|마케팅|소개)(?:\s*(?:문구|카피|자료))?|"
    r"(?:문서|자료|근거).{0,24}?(?:기준일|날짜).{0,32}?"
    r"(?:바꿔|변경|조작).{0,32}?(?:최신|현재).{0,20}?(?:처럼|으로)|"
    r"(?:조건|필터).{0,24}?(?:몰래|임의로).{0,20}?(?:바꿔|변경)|"
    r"(?:원천|공식).{0,20}?(?:수익률|가격|수치).{0,24}?(?:고쳐|덮어써)|"
    r"(?:원천|공식|제공).{0,24}?(?:파일|자료|데이터).{0,24}?"
    r"(?:덮어\s*쓰|덮어써|덮어쓴|덮어쓸|overwrite|변조|수정|교체)|"
    r"(?:결측\s*값|결측치|빈\s*값).{0,40}?(?:고수익|높은\s*수익).{0,24}?"
    r"(?:채워|대체)|"
    r"(?:DB|데이터(?:베이스)?).{0,32}?(?:없|미제공|누락).{0,64}?"
    r"(?:배(?:해서|하여)?|곱(?:해서|하여)?|환산|추정).{0,40}?"
    r"(?:채워|생성|만들|대체)|"
    r"(?:DB|데이터(?:베이스)?).{0,24}?(?:없|미제공|누락).{0,48}?"
    r"(?:결과.{0,12}?)?(?:끼워\s*넣|삽입|포함|섞어\s*넣)|"
    r"(?:근거\s*(?:문서|자료)|공식\s*(?:출처|문서|자료)).{0,32}?"
    r"(?:지어\s*내|꾸며\s*내|날조|가짜).{0,16}?(?:줘|주세요|작성|생성)?|"
    r"(?:위험\s*등급|수익률|가격|수치).{0,32}?(?:덮어써|원래\s*값이라고)|"
    r"(?:공식\s*)?(?:근거|출처|인용).{0,20}?(?:위조|날조|조작|꾸며)|"
    r"(?:원천|공식|제공).{0,20}?(?:값|수치|데이터|자료).{0,24}?"
    r"(?:수정|변경|조작).{0,48}?(?:성과|수익률|순서|순위).{0,32}?"
    r"(?:원하는\s*대로|마음대로|유리하게|불리하게).{0,20}?(?:바꿔|변경|조작)?|"
    r"(?:다른\s*(?:팀|회사|서비스)|외부\s*(?:쇼핑|판매|상거래)).{0,32}?"
    r"(?:상품|종목).{0,40}?(?:제공\s*)?(?:DB|데이터|검색)\s*결과.{0,28}?"
    r"(?:몰래\s*)?(?:섞|추가|삽입|포함)|"
    r"(?:임의|가짜|존재하지\s*않는).{0,24}?"
    r"(?:코인|주식|상품|종목).{0,32}?(?:ETF|ETN|채권|공모\s*펀드|펀드)"
    r"(?:로|라고).{0,20}?(?:답|표시|분류|포함)",
    re.IGNORECASE,
)
_UNVERIFIABLE_FRESHNESS = re.compile(
    r"(?:현재\s*)?실시간\s*(?:호가|가격|체결가|시세|뉴스|공시|거래량)|"
    r"(?:실시간|초\s*단위).{0,24}?(?:체결가|호가|가격|시세|거래량)|"
    r"실시간.{0,32}?(?:가입|판매|거래)\s*가능.{0,32}?(?:외부\s*시스템|확인|조회)|"
    r"(?:오늘\s*)?방금\s*(?:발표|공개|보도)(?:된)?\s*(?:뉴스|공시|정보)|"
    r"(?:오늘|최신).{0,20}?(?:뉴스|공시|호가|시세)"
    r".{0,24}?(?:반영|조회|검색|가져|평가)|"
    r"(?:지금|오늘|금일|최신).{0,40}?(?:가격|종가|시세|호가|뉴스|공시)"
    r".{0,24}?(?:알려|보여|조회|확인|검색|가져|반영|얼마|어때|[?？]?$)|"
    r"현재\s*(?:(?:해외|국내)\s*)?"
    r"(?:(?:ETF|ETN|ETP|국내\s*채권|채권|공모\s*펀드|펀드|상품|종목)"
    r"(?:의|을|를|은|는)?\s*)?(?:가격|종가|시세|호가)",
    re.IGNORECASE,
)
_BOND_PURCHASE_YIELD_CONTEXT = re.compile(
    r"(?=.*(?:원화\s*)?(?:국내\s*)?채권)"
    r"(?:"
    r"(?=.*(?:사는|매수(?:하는|할)?).{0,16}?가격.{0,16}?반영)"
    r"(?=.*(?:(?:세전|세후).{0,16}?(?:수익|보상)|(?:수익|보상).{0,16}?(?:세전|세후)))|"
    r"(?=.*(?:현재|지금)\s*(?:매수\s*)?가격)"
    r"(?=.*(?:샀을|살\s*때|매수(?:할|하는|한)?))"
    r"(?=.*(?:세전|세후).{0,16}?(?:수익률|수익|보상))"
    r")",
    re.IGNORECASE,
)
_TRANSACTION = re.compile(
    r"(?:내\s*이름|본인\s*명의).{0,64}?(?:매수|매도|주문|가입|환매|거래)"
    r".{0,32}?(?:실제로|제출|체결|대신|끝내|실행)|"
    r"내가\s*보유한.{0,64}?(?:전부\s*)?(?:매도|환매).{0,32}?(?:체결|결과|끝내)|"
    r"내\s*(?:계좌\s*번호|계좌번호|비밀번호|패스워드)"
    r".{0,80}?(?:사용|확인|접속|거래|매수|매도|주문|체결|끝내)|"
    r"(?:계좌\s*잔고|내\s*잔고).{0,64}?(?:확인|조회)"
    r".{0,64}?(?:주문|매수|매도|거래)|"
    r"(?:매수|매도|주문|가입|환매).{0,48}?"
    r"(?:실제로\s*)?(?:제출|체결|대신\s*끝내|실행|처리|진행|시켜)|"
    r"지금\s*즉시.{0,48}?(?:매수|매도|주문).{0,32}?(?:지시|실행|제출)|"
    r"(?:사|구입(?:해)?|매입(?:해)?|구매(?:해)?)\s*"
    r"(?:줘|주세요|주십시오|달라)|"
    r"(?:팔아|처분(?:해)?|환매(?:해)?)\s*(?:줘|주세요|주십시오|달라)|"
    r"(?:주문|오더).{0,24}?(?:넣어|걸어|접수해)\s*"
    r"(?:줘|주세요|주십시오|달라)|"
    r"(?:체결|결제)(?:해)?\s*(?:줘|주세요|주십시오|달라)|"
    r"(?:가입|매수|주문).{0,32}?(?:결제|체결)(?:까지)?|"
    r"(?:로그인|인증)\s*정보.{0,40}?계좌.{0,32}?(?:접속|잔고|조회)|"
    r"계좌.{0,24}?(?:접속|로그인).{0,32}?(?:잔고|보유|조회)|"
    r"(?:동의\s*없이|자동)\s*(?:매수|매도|주문).{0,40}?(?:등록|반복|실행)|"
    r"(?:주문|거래).{0,32}?(?:로그|기록).{0,24}?(?:남지\s*않|남기지\s*않)|"
    r"(?:가진|보유한)\s*(?:현금|자금|잔고).{0,32}?(?:전부|모두).{0,20}?"
    r"(?:투자|매수|주문)|"
    r"(?:실제로|직접)\s*(?:매수|매도|주문|가입|환매)(?:해)?\s*"
    r"(?:줘|주세요|주십시오|달라)|"
    r"(?:내\s*)?개인\s*정보(?:를|로|까지|와|하고)?\s*.{0,64}?"
    r"(?:매수|매도|주문|가입|환매).{0,32}?"
    r"(?:완료|실행|제출|체결|처리|진행|해\s*줘|해주세요)",
    re.IGNORECASE,
)
_MARKET_ABUSE = re.compile(
    r"(?:미공개\s*(?:중요\s*)?정보|내부자\s*(?:거래|정보))"
    r"(?:를|을)?\s*(?:이용|활용|사용|바탕).{0,64}?"
    r"(?:찾|골라|추천|거래|매수|매도|오를|내릴|수익|가격)|"
    r"(?:여러|복수)\s*계좌.{0,64}?(?:시세|가격).{0,24}?(?:조종|조작)|"
    r"(?:시세|가격).{0,32}?(?:조종|조작).{0,48}?(?:주문|거래|순서|방법)|"
    r"(?:규제|감시|보고|한도).{0,24}?(?:피하|피하도록|회피|우회)"
    r".{0,64}?(?:주문|거래|쪼개|분할)|"
    r"(?:주문|거래).{0,40}?(?:잘게\s*)?(?:쪼개|분할)"
    r".{0,32}?(?:규제|감시|보고|회피|우회)",
    re.IGNORECASE,
)
_FORECAST = re.compile(
    r"전망|예측|예상\s*(?:수익|가격|수익률)|목표\s*가격|"
    r"(?:내일|다음\s*(?:주|달|분기|해)|향후|앞으로).{0,28}?"
    r"(?:오를|내릴|상승|하락|수익|가격)|"
    r"(?:오를|내릴|상승할|하락할)\s*(?:까|것|상품)|"
    r"(?:오를|내릴)\s*가능성|(?:곧\s*)?떡상|폭등|폭락|유망한|대박날|담아야\s*할|"
    # A provided snapshot may legitimately contain a field described as
    # ``오늘 상승률``.  Only future morphology is a forecast; a bare historical
    # ``상승률`` must remain available to the normal field linker.
    r"(?:오늘(?:\s*밤)?|금일).{0,28}?(?:급등할|급락할|오를|내릴|상승할|하락할|"
    r"상승하겠|하락하겠|급등하겠|급락하겠)|"
    r"(?:호재|악재).{0,20}?(?:반영|전망|예상)|"
    r"(?:전\s*재산|은퇴\s*자금|대출\s*받아|빚을?\s*내).{0,36}?"
    r"(?:넣|투자|살|사야|매수|골라|정해|추천|지시)|"
    r"(?:\d+\s*(?:일|주|개월|년))\s*(?:뒤|후).{0,32}?"
    r"(?:수익률|가격).{0,24}?(?:미리|예측|예상|정확)|"
    r"올해\s*말.{0,24}?(?:수익률|가격).{0,20}?(?:1등|최고)"
    r".{0,20}?(?:맞혀|예측|골라)|"
    r"(?:사야|팔아야)\s*할|매수\s*시점|"
    r"(?:제|내)\s*(?:연봉|소득|나이|재산|자산|투자\s*성향|위험\s*성향)"
    r".{0,72}?(?:맞는|적합한|알맞은|추천|골라)|"
    r"(?:맞는|적합한|알맞은|맞춤형).{0,36}?"
    r"(?:상품|채권|ETF|ETN|ETP|펀드).{0,20}?(?:추천|골라)|"
    r"\b(?:pick|buy|recommend|predict)\b",
    re.IGNORECASE,
)
_GUARANTEE = re.compile(
    r"원금\s*(?:이|은)?\s*(?:(?:반드시|확실히|무조건|절대)\s*)?보장|"
    r"수익\s*(?:이|은)?\s*(?:(?:반드시|확실히|무조건|절대)\s*)?보장|"
    r"손실\s*(?:이\s*)?없|"
    r"손실.{0,16}?절대.{0,12}?없|"
    r"손실.{0,20}?(?:나지|발생하지).{0,12}?않|"
    r"손실\s*가능성\s*(?:0\s*%|제로)|절대.{0,12}?손실.{0,12}?없|"
    r"무조건\s*(?:수익|오르|돈)|"
    r"(?:수익률?|가격).{0,16}?(?:확실|확정|보장)|"
    r"목표\s*수익률.{0,24}?(?:무조건|반드시).{0,16}?(?:달성|확정)|"
    r"확실(?:한|히)\s*(?:수익|오르|돈)|"
    r"(?:원금|수익|무손실)을?\s*(?:지켜|보장|보증)|"
    r"손해(?:를)?\s*(?:안\s*보|보지\s*않)|"
    r"원금(?:을|이)?\s*(?:안\s*(?:잃|까먹|줄)|(?:잃|줄)지\s*않)|"
    r"(?:투자(?:에)?\s*실패|손실).{0,32}?원금.{0,24}?(?:돌려주|환급|확약)|"
    r"손실.{0,20}?절대\s*안\s*(?:나|난|발생).{0,24}?(?:보장|확약)?|"
    r"\bguaranteed\s+(?:returns?|profits?|no[-\s]?loss)\b|\bno[-\s]?loss\b",
    re.IGNORECASE,
)
_OFF_TOPIC = re.compile(
    r"날씨|미세먼지|레시피|요리법|김치찌개|(?:끓이|요리하)는\s*법|"
    r"맛집|여행\s*(?:일정|코스)|"
    r"(?:영어|일본어|중국어)로\s*번역|"
    r"(?:단어|문장|표현)?.{0,12}?번역(?:해|해줘|해주세요|하라)|"
    r"(?:이메일|메일).{0,24}?(?:작성|써|대신)|"
    r"소설(?:을|를)?\s*(?:써|작성)|시를\s*써|"
    r"(?:노래\s*)?가사(?:를)?\s*(?:써|작성|만들)|"
    r"한자.{0,12}?뜻|어원(?:만|을)?\s*(?:알려|설명)|"
    r"(?:축구|야구).{0,20}?(?:경기|결과|우승팀)|"
    r"영화\s*추천|(?:음악|재즈|앨범).{0,16}?추천|"
    r"(?:두통|복통|증상).{0,28}?(?:진단|약|먹어야|치료)|"
    r"(?:임대차|계약).{0,28}?(?:법적\s*효력|법률\s*(?:판단|의견|상담))|"
    r"(?:연애\s*상담|화해(?:하는)?\s*방법|친구와\s*다퉜)|"
    r"(?:고양이|강아지|이미지|그림).{0,20}?(?:그림|이미지)?\s*프롬프트|"
    r"(?:이|삼|사|오|육|칠|팔|구|N)\s*행시|끝말잇기|말장난|"
    r"(?:아재\s*)?개그|농담|역할극|"
    r"운세|사주|파이썬.{0,24}?코드|"
    r"(?:메뉴|음식|요리|쇼핑\s*상품).{0,32}?"
    r"(?:ETF|ETN|ETP|채권|공모\s*펀드|펀드)\s*(?:분석|상품)?(?:인|인\s*것처럼|처럼)"
    r".{0,24}?(?:꾸며|포장|분류|답)|"
    r"(?:파이썬|자바(?:스크립트)?|JavaScript|SQL)\s*"
    r"(?:코드|프로그램|게임)|코딩\s*(?:해|문제)",
    re.IGNORECASE,
)
_OUT_OF_SCOPE = re.compile(
    r"가상\s*자산|암호\s*화폐|비트코인|알트코인|코인\s*(?:시세|매매)|"
    r"\bcrypto(?:currency)?\b(?!\s+(?:ETF|ETN|ETP)\b)|"
    r"예금|적금|대출(?!형)|보험(?!회사채)|개별\s*주식|주식\s*종목|"
    r"선물\s*옵션|"
    r"외환\s*매매|환전|부동산\s*(?:매물|시세)",
    re.IGNORECASE,
)
_DATA_SCOPE_BYPASS = re.compile(
    r"(?:제공\s*)?데이터(?:에)?\s*(?:없어도|없더라도|밖(?:의|에서))|"
    r"(?:제공|공식|승인)\s*(?:데이터|자료)(?:의)?\s*"
    r"(?:제한|범위|경계|근거)(?:를|을|는|은)?\s*(?:무시|우회|없애|해제)|"
    # ``위성 인터넷`` is an investable theme noun, not a request to bypass
    # the approved source boundary with an Internet search.
    r"(?:(?<!위성)(?<!위성\s)인터넷|웹|온라인)(?:을|에서|으로)?\s*.{0,48}?"
    r"(?:지식|검색|찾아|조회|가져|수집|크롤링|확인)|외부\s*지식|"
    r"(?:없는|가짜|허구의?)?\s*(?:ETF|ETN|ETP|채권|펀드|상품)(?:도|을|를)?\s*"
    r"(?:지어내|만들어|창작|생성)|"
    r"상품(?:을|를)?\s*(?:지어내|만들어|창작)|"
    r"(?:없는|존재하지\s*않는|가짜|허구|KR-(?:FAKE|UNICORN)[A-Z0-9-]*)"
    r".{0,48}?(?:실제.{0,20}?처럼|가정|포함)|"
    r"(?:없는|존재하지\s*않는|가짜|허구의?)\s*"
    r"(?:ETF|ETN|ETP|채권|펀드|상품|종목)(?:\s*코드)?"
    r".{0,56}?(?:추가|끼워\s*넣|넣어|삽입|포함)|"
    r"(?:수익률|가격|수치)(?:을|를)?\s*(?:임의로|마음대로|가짜로)?\s*"
    r"(?:만들|지어내|생성)|"
    r"(?:근거|인용)(?:을|를)?\s*(?:임의로|마음대로|가짜로)?\s*"
    r"(?:만들|지어내|생성)|"
    r"(?:근거\s*(?:문서|자료|출처)|인용문)(?:도|을|를)?\s*"
    r"(?:임의로|마음대로|가짜로)?\s*(?:만들|지어내|생성)|"
    r"(?:DB|데이터(?:베이스)?|제공\s*데이터).{0,28}?없는.{0,32}?"
    r"(?:ETF|ETN|ETP|채권|펀드|상품).{0,40}?"
    r"(?:끼워\s*넣|삽입|포함|섞)|"
    r"(?:외부|개별|[A-Za-z0-9가-힣._-]{2,30}\s*주식).{0,36}?"
    r"(?:ETF|ETN|ETP|국내채권|공모펀드|ID).{0,28}?(?:바꿔|삽입|포함)|"
    r"(?:외부|개별).{0,12}?주식.{0,24}?(?:삽입|포함|섞)|"
    r"(?:외부|쇼핑|타사|다른\s*(?:팀|회사)).{0,20}?(?:상품|종목).{0,28}?"
    r"(?:ETF|ETN|ETP|채권|공모\s*펀드|펀드)(?:인|인\s*척|처럼).{0,32}?"
    r"(?:추가|삽입|포함|섞|답)|"
    r"(?:외부|미국\s*국채|원문).{0,36}?(?:승인\s*(?:DB|데이터))"
    r".{0,24}?(?:추가|넣어|삽입)|"
    r"사모\s*펀드.{0,36}?공모\s*펀드.{0,28}?(?:포함|가정|승인\s*범위)|"
    r"(?:공모\s*펀드|국내채권|ETF|ETN).{0,12}?(?:이|라고)\s*가정|"
    r"(?:승인\s*(?:경계|범위)|공식\s*데이터).{0,24}?(?:무시|우회)|"
    r"실제.{0,12}?가짜\s*상품.{0,16}?(?:섞|포함)|"
    r"(?:(?<![A-Za-z0-9])BUYABLE[_\s-]*QUANTITY(?![A-Za-z0-9])"
    r".{0,72}?(?:사용|기준|조회|계산|필터|정렬)|"
    r"(?:사용|기준|조회|계산|필터|정렬).{0,72}?"
    r"(?<![A-Za-z0-9])BUYABLE[_\s-]*QUANTITY(?![A-Za-z0-9]))|"
    r"(?:(?:Morningstar|모닝스타|Lipper|리퍼|제\s*3자|외부\s*평가\s*기관)"
    r".{0,32}?(?:별점|평점|스타\s*등급|rating)|"
    r"(?:별점|평점|스타\s*등급|rating).{0,32}?"
    r"(?:Morningstar|모닝스타|Lipper|리퍼|제\s*3자|외부\s*평가\s*기관))",
    re.IGNORECASE,
)
_NEGATED_EXTERNAL_SOURCE = re.compile(
    r"(?:인터넷|웹|온라인|외부\s*지식)(?:을|에서|으로)?\s*.{0,40}?"
    r"(?:검색|찾|조회|가져|사용|활용)?\s*(?:하지\s*말|하지\s*않|없이|제외)",
    re.IGNORECASE,
)
_ONLINE_OFFLINE_TRADING_CHANNEL = re.compile(
    r"(?:온라인(?:과|·|/|\s+및)\s*오프라인|오프라인(?:과|·|/|\s+및)\s*온라인)"
    r"(?:에서|으로)?\s*(?:매매|거래|구매|매수)(?:가|는|이)?\s*가능(?:한|한지|여부)?",
    re.IGNORECASE,
)
_STRUCTURED_SALES_CHANNEL = re.compile(
    r"(?:클래스\s*)?판매\s*채널(?:이|은|가)?\s*(?:온라인|오프라인)|"
    r"(?:개인용\s*|법인용\s*)?(?:온라인|오프라인)\s*(?:전용\s*)?"
    r"판매\s*(?:공모\s*)?(?:펀드|상품)",
    re.IGNORECASE,
)
_MULTITURN = re.compile(
    r"^\s*(?:그\s*중(?:에서)?|그것들?\s*중(?:에서)?|"
    r"해당\s*(?:(?:해외|국내)\s*)?"
    r"(?:상품|종목|ETF|ETN|ETP|공모\s*펀드|펀드|채권)|"
    r"내가\s*(?:말한|고른|선택한|언급한)\s*(?:(?:해외|국내)\s*)?"
    r"(?:상품|종목|ETF|ETN|ETP|공모\s*펀드|펀드|채권)|"
    r"선택한\s*(?:(?:해외|국내)\s*)?"
    r"(?:상품|종목|ETF|ETN|ETP|공모\s*펀드|펀드|채권)|"
    r"비교\s*대상(?:으로)?\s*말한\s*(?:두\s*)?(?:(?:해외|국내)\s*)?"
    r"(?:상품|종목|ETF|ETN|ETP|공모\s*펀드|펀드|채권)|"
    r"(?:그|저)\s*(?:(?:해외|국내)\s*)?"
    r"(?:상품|종목|ETF|ETN|ETP|공모\s*펀드|펀드|채권)|"
    r"위(?:에서|의)\s*(?:결과|상품|목록)|앞서\s*(?:본|말한|찾은)|"
    r"아까\s*(?:것|거|조건|본|말한|찾은|질문한)|"
    r"방금\s*(?:것|거|조건|본|말한|찾은|질문한)|"
    r"이전\s*(?:결과|답변|질문|조건)|저번\s*(?:결과|답변|질문|조건)|"
    r"(?:첫|두|세)\s*번째\s*(?:(?:해외|국내)\s*)?"
    r"(?:것|거|상품|종목|ETF|ETN|ETP|채권|펀드)|"
    r"그때\s*기준|"
    r"같은\s*.{0,20}?(?:조건|기준).{0,20}?(?:다시|그대로))",
    re.IGNORECASE,
)
_EXPLICIT_PRODUCT_IDENTITY = re.compile(
    r"(?:상품\s*(?:번호|ID|아이디)|종목\s*(?:코드|번호)|티커|ISIN)"
    r"(?:가|는|은|이)?\s*[:：]?\s*[A-Z0-9._:-]{2,30}|"
    r"(?<![A-Z0-9])(?:KR[A-Z0-9]{10}|(?:[A-Z]{2,5}|[0-9]{3}):[A-Z0-9._-]+|"
    r"[A-Z][A-Z0-9]{0,9}\.[A-Z0-9]{1,5})(?![A-Z0-9])",
    re.IGNORECASE,
)

_CONFUSABLE_ASCII = str.maketrans(
    {
        "А": "A",
        "В": "B",
        "С": "C",
        "Е": "E",
        "Н": "H",
        "І": "I",
        "Ј": "J",
        "К": "K",
        "М": "M",
        "О": "O",
        "Р": "P",
        "Т": "T",
        "Х": "X",
        "У": "Y",
        "а": "a",
        "в": "b",
        "с": "c",
        "е": "e",
        "і": "i",
        "ј": "j",
        "к": "k",
        "м": "m",
        "н": "h",
        "о": "o",
        "р": "p",
        "т": "t",
        "х": "x",
        "у": "y",
    }
)
_NEGATED_POLICY_REQUEST = re.compile(
    r"(?:추천|전망|예측|보장)"
    r"(?:(?:이나|이나\s+|과|와|또는|/|,|·)\s*(?:추천|전망|예측|보장))*"
    r"(?:은|는|이|가)?\s*"
    r"(?:필요(?:가)?\s*없|원하지\s*않|하지\s*(?:말|않)|없이|제외|말고|빼고)",
    re.IGNORECASE,
)
_NON_ADVISORY_CALCULATION = re.compile(
    r"(?:이\s*)?계산값을\s*.{0,40}?(?:전망|예측|투자\s*추천)"
    r".{0,32}?해석하지\s*말(?:고|아|라)",
    re.IGNORECASE,
)


def normalize_user_question(question: str) -> str:
    """Canonicalize adversarial Unicode before every lexical safety decision.

    NFKC folds compatibility characters such as full-width ASCII. Format and
    control characters are removed so zero-width or bidi controls cannot split
    a prohibited phrase. Remaining whitespace is collapsed for stable routing.
    """

    normalized = unicodedata.normalize("NFKC", question).translate(_CONFUSABLE_ASCII)
    visible: list[str] = []
    for character in normalized:
        category = unicodedata.category(character)
        if category == "Cf":
            continue
        if category == "Cc":
            if character.isspace():
                visible.append(" ")
            continue
        visible.append(" " if character.isspace() else character)
    return " ".join("".join(visible).split())


def safety_policy_surface(question: str) -> str:
    """Remove explicit negative disclaimers from lexical policy matching only."""

    normalized = normalize_user_question(question)
    normalized = _NON_ADVISORY_CALCULATION.sub(" ", normalized)
    return _NEGATED_POLICY_REQUEST.sub(" ", normalized)


def data_scope_policy_surface(question: str) -> str:
    """Mask an explicit refusal to use external sources before scope checks."""

    normalized = normalize_user_question(question)
    # "온라인과 오프라인에서 거래 가능한 채권" describes a transaction
    # channel, not a request to search the public web. Mask only this bounded
    # phrase; genuine requests such as "온라인에서 찾아줘" remain blocked.
    normalized = _ONLINE_OFFLINE_TRADING_CHANNEL.sub("거래 가능", normalized)
    # The official fund table contains a structured sales-channel field.
    # "판매채널이 온라인" is a DB filter, not a request to search the web.
    normalized = _STRUCTURED_SALES_CHANNEL.sub("판매채널", normalized)
    return _NEGATED_EXTERNAL_SOURCE.sub(" ", normalized)


_CLAUSE_BOUNDARY = re.compile(
    r"(?:[,.!?;]|\s+(?:그리고|또한|또는|및)\s+|"
    r"(?:하고|하며|해서|하면서|한\s*뒤|한\s*후)\s+)",
    re.IGNORECASE,
)

# High-risk intent categories are kept as separate, reviewable grammars rather
# than appending one-off sentences to the older catch-all expressions above.
# Each rule requires both a protected target and an adversarial action, which
# preserves ordinary product questions while making the unsafe clause terminal
# even when it is joined to a valid DB search.
_SENSITIVE_RUNTIME_DISCLOSURE = re.compile(
    r"(?:\.env(?:\.[A-Za-z0-9_-]+)?|(?:api\s*)?secrets?|credentials?|"
    r"인증\s*(?:키|정보)|비밀\s*(?:키|파일)|"
    r"(?:서버|docker|컨테이너|런타임|내부)\s*(?:파일\s*)?경로)"
    r"(?:의|을|를|은|는|이|가)?\s*.{0,36}?"
    r"(?:내용|값|원문|위치)?\s*.{0,16}?"
    r"(?:출력|공개|노출|보여|알려|읽어|꺼내|내놔|전달)|"
    r"(?:출력|공개|노출|보여|알려|읽어|꺼내|내놔)\s*.{0,24}?"
    r"(?:\.env(?:\.[A-Za-z0-9_-]+)?|(?:api\s*)?secrets?|credentials?|"
    r"인증\s*(?:키|정보)|비밀\s*(?:키|파일)|"
    r"(?:서버|docker|컨테이너|런타임|내부)\s*(?:파일\s*)?경로)",
    re.IGNORECASE,
)
_EXPLICIT_NUMERIC_TAMPERING = re.compile(
    r"(?:(?:DB|데이터베이스|원천|공식|제공)\s*)?"
    r"(?:수익률|가격|수치|순위|AUM|순자산|보수율|위험\s*등급)"
    r"(?:을|를|은|는|이|가)?\s*"
    r"(?:\d+(?:\.\d+)?\s*(?:%|퍼센트|배|포인트)?\s*)?"
    r"(?:높여|낮춰|올려|내려|늘려|줄여|증가시켜|감소시켜|더해|빼서|곱해|"
    r"부풀려|깎아서|고쳐)"
    r".{0,28}?(?:표시|답|보여|출력|반영|만들|정렬)",
    re.IGNORECASE,
)

# Mutation and truth-substitution are modelled as actions rather than a list of
# frozen red-team sentences.  Requiring both a protected data target and a
# write/replace action avoids blocking ordinary read-only questions that merely
# mention a DB field, product ID, or numeric comparison.
_DATABASE_MUTATION_OR_TOOL_INJECTION = re.compile(
    r"(?:"
    r"(?:DB|데이터베이스|SQLite|테이블|원천\s*파일|데이터\s*파일)"
    r"(?:을|를|의|\s*값(?:을|를|의)?)?\s*.{0,36}?"
    r"(?:수정|변경|변조|바(?:꿔|꾸|꾼)|고쳐|덮어\s*쓰|업데이트|삽입|추가|삭제|폐기)"
    r"(?:해|하|해서|한)?|"
    r"(?:exec[_\s-]*sql|execute[_\s-]*sql|run[_\s-]*sql|도구\s*출력|tool)"
    r".{0,80}?\b(?:UPDATE|INSERT|DELETE|DROP|ALTER|REPLACE)\b|"
    r"\b(?:UPDATE|INSERT\s+INTO|DELETE\s+FROM|DROP\s+TABLE|ALTER\s+TABLE|"
    r"REPLACE\s+INTO)\b"
    r")",
    re.IGNORECASE,
)
_GROUND_TRUTH_SUBSTITUTION = re.compile(
    r"(?:"
    r"(?:실제|공식|제공)\s*(?:DB|데이터|값|수치|상품\s*ID|종목\s*ID)?"
    r".{0,28}?(?:달라도|대신|무시하고|관계없이).{0,56}?"
    r"(?:바꿔|치환|대체|꾸며|고쳐|반환|답해|표시)|"
    r"(?:상품|종목)\s*ID(?:를|을)?\s*.{0,24}?(?:실제\s*값\s*)?대신"
    r".{0,40}?(?:바꿔|치환|대체|반환)|"
    r"(?:가짜|임의|지정한)\s*(?:ETF|ETN|ETP|채권|펀드|상품|종목|ID)"
    r".{0,48}?(?:1\s*위|상위|결과|목록).{0,28}?(?:넣|추가|삽입|반환)|"
    r"(?:모든|전체)\s*(?:ETF|ETN|ETP|채권|펀드|상품|종목)?\s*"
    r"(?:수익률|가격|수치|AUM|순자산|보수율)(?:을|를)?\s*"
    r"\d+(?:\.\d+)?\s*(?:%|퍼센트|배|포인트)?(?:로|이라고)?\s*"
    r"(?:꾸며|바꿔|고쳐|표시|답해)"
    r")",
    re.IGNORECASE,
)

# The evaluation snapshot has no live FX/benchmark/ESG feed.  A request to
# inject a newly announced market value and recompute provided product metrics
# is therefore terminally unverifiable, while ordinary snapshot-period fields
# such as ``오늘 수익률`` remain supported.
_UNAPPROVED_LIVE_RECALCULATION = re.compile(
    r"(?:"
    r"(?:오늘|현재|실시간)\s*(?:환율|기준\s*금리|시장\s*금리|ESG\s*(?:점수|등급))"
    r".{0,64}?(?:환산|재계산|반영|비교|순위|정렬)|"
    r"(?:방금|오늘|금일|최근)\s*(?:발표|공개|고시)(?:된)?\s*.{0,32}?"
    r"(?:환율|기준\s*금리|시장\s*금리|ESG\s*(?:점수|등급))"
    r".{0,64}?(?:환산|재계산|반영|비교|순위|정렬)"
    r")",
    re.IGNORECASE,
)
_ACCOUNT_TRANSACTION_ACTION = re.compile(
    r"(?:내|제|고객|사용자)?\s*계좌(?:로|에서|에|를)?\s*.{0,48}?"
    r"(?:(?:실제(?:로)?|직접)\s*)?"
    r"(?:매수|매도|주문|가입|환매)(?:해|하|를|을|까지)?\s*.{0,12}?"
    r"(?:줘|주세요|주십시오|달라|실행|체결|접수|처리|완료|주문\s*번호)|"
    r"(?:내|제|고객|사용자)?\s*계좌(?:로|에서|에|를)?\s*.{0,48}?"
    r"(?:거래\s*(?:실행|체결|접수|처리|완료)|거래(?:해|하)\s*"
    r"(?:줘|주세요|주십시오|달라))|"
    r"(?:실제(?:로)?|직접)\s*(?:매수|매도|주문|가입|환매|거래)"
    r"(?:해|하|를|을)?\s*(?:줘|주세요|주십시오|달라|실행|체결|접수|처리)|"
    r"(?:개인\s*정보|인증\s*정보|계좌\s*정보)(?:를|로|까지|와|하고)?"
    r"\s*.{0,80}?(?:계좌|포트폴리오).{0,56}?"
    r"(?:담아|넣어|편입|추가)(?:\s*해)?\s*"
    r"(?:줘|주세요|주십시오|달라|실행|처리|완료)|"
    r"(?:내|제|고객|사용자)?\s*(?:연금|ISA|개인)?\s*계좌(?:에|로|에서)?"
    r".{0,64}?(?:ETF|ETN|ETP|채권|공모\s*펀드|펀드|상품|종목)"
    r"(?:을|를)?\s*.{0,24}?(?:담아|넣어|편입|추가)(?:\s*해)?\s*"
    r"(?:줘|주세요|주십시오|달라|실행|처리|완료)",
    re.IGNORECASE,
)
_OUTSIDE_ASSET_INJECTION = re.compile(
    r"(?:(?:주최\s*측|대회\s*주최\s*측|제공|승인|공식)\s*)?"
    r"(?:데이터|DB|데이터베이스)(?:에|에는|의)?\s*"
    r"(?:없|없는|밖(?:의|에|에서)).{0,40}?"
    r"(?:코인|가상\s*자산|암호\s*화폐|개별\s*주식|주식\s*종목|"
    r"예금|적금|보험|대출|외부\s*상품|타사\s*상품)"
    r".{0,32}?(?:ETF|ETN|ETP|채권|공모\s*펀드|펀드|상품|종목)"
    r"(?:로|라고|인\s*것처럼|인\s*척|처럼).{0,24}?"
    r"(?:추가|삽입|포함|끼워|섞|답|표시|분류)",
    re.IGNORECASE,
)
_AUDIT_OR_STATUS_TAMPERING = re.compile(
    r"(?:(?:감사|요청|접속|검증|실행|호출|서버)\s*)?"
    r"(?:로그|기록)(?:를|을|은|는)?\s*.{0,24}?"
    r"(?:삭제|지우|지워|없애|말소|변조|조작)|"
    r"(?:거래\s*(?:정지|중지)|판매\s*불가|가입\s*불가|상장\s*폐지)"
    r"(?:된|인)?\s*(?:상품|종목)?(?:을|를|은|는)?\s*.{0,28}?"
    r"(?:정상|거래\s*가능|판매\s*가능|가입\s*가능)(?:이라고|으로|인\s*것처럼)"
    r".{0,20}?(?:표시|위장|변경|조작|답)",
    re.IGNORECASE,
)
_SOURCE_PROVENANCE_SPOOFING = re.compile(
    r"(?:외부\s*)?(?:블로그|카페|커뮤니티|SNS|개인\s*(?:글|게시물)|"
    r"비공식\s*(?:자료|문서|출처)|미확인\s*(?:자료|문서|출처))"
    r".{0,48}?(?:공식|승인)\s*(?:자료|문서|출처|근거)(?:라고|인\s*척|처럼)?"
    r".{0,24}?(?:속(?:이|여)|위장|꾸미|사칭|둔갑|거짓)|"
    r"(?:공식|승인)\s*(?:자료|문서|출처|근거)(?:인\s*척|처럼)\s*.{0,28}?"
    r"(?:블로그|카페|커뮤니티|SNS|개인\s*(?:글|게시물)|비공식\s*자료)",
    re.IGNORECASE,
)
_INTERNAL_REASONING_DISCLOSURE = re.compile(
    r"(?:(?:HCLX|LLM|모델|에이전트)\s*)?(?:내부|숨겨진|비공개)\s*"
    r"(?:추론|사고\s*과정|생각\s*과정|도구\s*호출\s*(?:기록|내역)|"
    r"think[_\s-]*trace|chain[_\s-]*of[_\s-]*thought)"
    r".{0,32}?(?:원문|그대로|전문)?\s*.{0,16}?"
    r"(?:출력|공개|노출|보여|알려|내놔|제공)",
    re.IGNORECASE,
)
_DANGEROUS_OFF_TOPIC_INSTRUCTIONS = re.compile(
    r"(?:폭발물|폭탄|화약|사제\s*무기|총기).{0,24}?"
    r"(?:제조법|제작법|만드는\s*법|만들기|제조|제작)"
    r"|(?:제조|제작|만드는\s*법).{0,24}?(?:폭발물|폭탄|화약|사제\s*무기)",
    re.IGNORECASE,
)
_VERIFICATION_BYPASS = re.compile(
    r"(?:검증|검산|확인)(?:\s*(?:단계|절차|경계|규칙|정책|기능|로직))?"
    r"(?:을|를|은|는)?\s*.{0,28}?"
    r"(?:끄|꺼|해제|우회|비활성화|건너\s*뛰|건너\s*뛴|"
    r"스킵|skip|생략|제외|무시)",
    re.IGNORECASE,
)

_PROMPT_POLICY_BYPASS = re.compile(
    r"(?:시스템\s*)?(?:프롬프트|prompt)(?:의|에\s*있는|\s*)?"
    r"(?:규칙|정책|지침|제약|제한)(?:을|를|은|는)?\s*.{0,28}?"
    r"(?:무효화|해제|우회|무시|삭제|제거|비활성화|끄|꺼|건너\s*뛰|스킵|skip)",
    re.IGNORECASE,
)
_MEDICAL_OFF_TOPIC = re.compile(
    r"(?:"
    r"(?:질환|질병|증상|통증|두통|복통|요통|허리\s*통증|감기|당뇨|고혈압)"
    r".{0,40}?(?:약|의약품|처방|복용|투약|진단|치료)|"
    r"(?:약|의약품)(?:을|를|은|는)?\s*.{0,32}?"
    r"(?:처방|추천|복용량|용량|먹(?:어|을)|투약)|"
    r"(?:처방|진단|치료)(?:해|하|을|를|이|가|법)?\s*.{0,24}?"
    r"(?:줘|주세요|주십시오|알려|정해|추천)"
    r")",
    re.IGNORECASE,
)

# These policies describe unsafe *actions*, not a list of known entities.  A
# user may vary the invented product name or the third-party model provider,
# but neither may expand the approved product universe or delegate product
# selection to an unapproved commercial LLM.  Requiring both the unsafe target
# and its action keeps ordinary questions such as ``LLM 관련 ETF`` in scope.
_SYNTHETIC_PRODUCT_INJECTION = re.compile(
    r"(?:"
    r"(?:(?:공식|제공|승인|주최\s*측)\s*(?:데이터|DB|데이터베이스|목록)"
    r"(?:에|에는|의|에서)?\s*(?:없|없는|밖(?:의|에|에서))|"
    r"(?:가상(?:의)?|가짜|허구(?:의)?|존재하지\s*않는|임의로\s*(?:만든|생성한)))"
    r".{0,64}?(?:ETF|ETN|ETP|채권|공모\s*펀드|펀드|금융\s*상품|상품|종목)"
    r".{0,48}?(?:목록|검색\s*결과|후보|제공\s*데이터|DB)?\s*.{0,24}?"
    r"(?:추가|삽입|끼워\s*넣|섞어\s*넣|포함(?:시켜|해)?|만들어\s*넣)|"
    r"(?:목록|검색\s*결과|후보|제공\s*데이터|DB)(?:에|에는|로)?\s*.{0,28}?"
    r"(?:추가|삽입|끼워\s*넣|섞어\s*넣|포함(?:시켜|해)?)\s*.{0,48}?"
    r"(?:가상(?:의)?|가짜|허구(?:의)?|존재하지\s*않는|임의로\s*(?:만든|생성한))"
    r".{0,48}?(?:ETF|ETN|ETP|채권|공모\s*펀드|펀드|금융\s*상품|상품|종목)"
    r")",
    re.IGNORECASE,
)
_UNAPPROVED_LLM_PRODUCT_SELECTION = re.compile(
    r"(?:(?:외부|타사|상용|유료|SaaS(?:형)?)\s*)+"
    r"(?:생성형\s*)?(?:AI|LLM|대규모\s*언어\s*모델|언어\s*모델|모델|API)"
    r"(?:을|를|에게|로)?\s*.{0,40}?"
    r"(?:호출|사용|활용|연결|질의|물어|문의)"
    r".{0,64}?(?:추천|선정|골라|고르|추려|순위|랭킹|후보|포트폴리오)"
    r"|(?:추천|선정|골라|고르|추려|순위|랭킹|후보|포트폴리오)"
    r".{0,64}?(?:(?:외부|타사|상용|유료|SaaS(?:형)?)\s*)+"
    r"(?:생성형\s*)?(?:AI|LLM|대규모\s*언어\s*모델|언어\s*모델|모델|API)"
    r"(?:을|를|에게|로)?\s*.{0,32}?(?:호출|사용|활용|연결|질의|물어|문의)",
    re.IGNORECASE,
)
_FUTURE_PRODUCT_SELECTION = re.compile(
    r"(?:(?:미래에(?!셋)|장래에|앞으로|향후)\s*.{0,40}?"
    r"(?:오를|내릴|상승|하락|수익(?:이|률)?|성과|가격)|"
    r"(?:미래|장래)(?:의)?\s+(?:수익률|성과|가격))"
    r".{0,64}?(?:ETF|ETN|ETP|채권|공모\s*펀드|펀드|상품|종목)?"
    r".{0,32}?(?:골라|고르|선정|추천|추려|뽑아|알려|찾아)",
    re.IGNORECASE,
)

_ADDITIONAL_TERMINAL_PATTERNS: dict[SafetyGate, tuple[re.Pattern[str], ...]] = {
    SafetyGate.SECURITY: (
        _SENSITIVE_RUNTIME_DISCLOSURE,
        _AUDIT_OR_STATUS_TAMPERING,
        _INTERNAL_REASONING_DISCLOSURE,
        _UNAPPROVED_LLM_PRODUCT_SELECTION,
        _VERIFICATION_BYPASS,
        _PROMPT_POLICY_BYPASS,
        _DATABASE_MUTATION_OR_TOOL_INJECTION,
    ),
    SafetyGate.DATA_INTEGRITY: (
        _EXPLICIT_NUMERIC_TAMPERING,
        _OUTSIDE_ASSET_INJECTION,
        _SYNTHETIC_PRODUCT_INJECTION,
        _SOURCE_PROVENANCE_SPOOFING,
        _GROUND_TRUTH_SUBSTITUTION,
    ),
    SafetyGate.TRANSACTION: (_ACCOUNT_TRANSACTION_ACTION,),
    SafetyGate.OFF_TOPIC: (
        _DANGEROUS_OFF_TOPIC_INSTRUCTIONS,
        _MEDICAL_OFF_TOPIC,
    ),
    SafetyGate.FORECAST: (_FUTURE_PRODUCT_SELECTION,),
    SafetyGate.FRESHNESS: (_UNAPPROVED_LIVE_RECALCULATION,),
}


def _policy_clauses(question: str) -> tuple[str, ...]:
    """Keep the whole request and every non-empty clause for terminal policy checks."""

    clauses = [question]
    clauses.extend(part.strip() for part in _CLAUSE_BOUNDARY.split(question) if part.strip())
    return tuple(dict.fromkeys(clauses))


class SafetyEnvelope:
    """Fail-closed pre-router envelope with explicit, auditable gates."""

    def evaluate(self, question: str) -> SafetyEnvelopeDecision:
        normalized = normalize_user_question(question)
        policy_surface = safety_policy_surface(normalized)
        scope_surface = data_scope_policy_surface(normalized)
        normalized_clauses = _policy_clauses(normalized)
        policy_clauses = _policy_clauses(policy_surface)
        checks = (
            (
                SafetyGate.SECURITY,
                SafetyDisposition.UNSUPPORTED,
                _SECURITY,
                "지시 우회·내부 정보 노출·명령 주입 요청은 처리할 수 없습니다.",
            ),
            (
                SafetyGate.DATA_INTEGRITY,
                SafetyDisposition.UNSUPPORTED,
                _DATA_INTEGRITY,
                "공식 데이터의 수치·순위를 조작하거나 미확인 정보로 대체할 수 없습니다.",
            ),
            (
                SafetyGate.MARKET_ABUSE,
                SafetyDisposition.UNSUPPORTED,
                _MARKET_ABUSE,
                "미공개정보 이용·시세조종·규제회피 요청은 처리할 수 없습니다.",
            ),
            (
                SafetyGate.TRANSACTION,
                SafetyDisposition.UNSUPPORTED,
                _TRANSACTION,
                "계좌 접근이나 실제 주문·거래 실행은 지원하지 않습니다.",
            ),
            (
                SafetyGate.FRESHNESS,
                SafetyDisposition.UNSUPPORTED,
                _UNVERIFIABLE_FRESHNESS,
                "실시간 시세·호가·뉴스는 승인된 기준 데이터로 확인할 수 없습니다.",
            ),
            (
                SafetyGate.GUARANTEE,
                SafetyDisposition.UNSUPPORTED,
                _GUARANTEE,
                "원금이나 수익을 보장하는 요청은 제공 데이터로 답할 수 없습니다.",
            ),
            (
                SafetyGate.FORECAST,
                SafetyDisposition.UNSUPPORTED,
                _FORECAST,
                "미래 가격·수익률 예측이나 전망 요청은 제공 데이터 범위를 벗어납니다.",
            ),
            (
                SafetyGate.OFF_TOPIC,
                SafetyDisposition.UNSUPPORTED,
                _OFF_TOPIC,
                "금융상품 데이터 조회와 무관한 요청은 처리할 수 없습니다.",
            ),
        )
        for gate, disposition, pattern, reason in checks:
            surfaces = (
                policy_clauses
                if gate in {SafetyGate.FORECAST, SafetyGate.GUARANTEE}
                else normalized_clauses
            )
            gate_patterns = (pattern, *_ADDITIONAL_TERMINAL_PATTERNS.get(gate, ()))
            matched = next(
                (
                    match
                    for surface in surfaces
                    for candidate in gate_patterns
                    if (match := candidate.search(surface)) is not None
                ),
                None,
            )
            if gate is SafetyGate.SECURITY:
                matched = (
                    matched
                    or _PROTECTED_MARKER.search(normalized)
                    or next(
                        (
                            match
                            for candidate in gate_patterns
                            if (match := candidate.search(normalized[::-1])) is not None
                        ),
                        None,
                    )
                    or _PROTECTED_MARKER.search(normalized[::-1])
                )
            if (
                gate is SafetyGate.FRESHNESS
                and matched is not None
                and _BOND_PURCHASE_YIELD_CONTEXT.search(normalized) is not None
            ):
                # ``지금 사는 가격을 반영한 세전/세후 보상`` is a natural
                # description of the provided snapshot's buy-yield fields. It
                # does not request a live quote.  Keep real-time price/news
                # requests blocked, but let this narrowly evidenced product
                # metric proceed to the normal QueryPlan authority checks.
                matched = None
            if matched:
                return SafetyEnvelopeDecision(normalized, disposition, gate, reason)

        out_of_scope = _OUT_OF_SCOPE.search(normalized)
        if (
            out_of_scope is not None
            and out_of_scope.group(0).startswith("보험")
            and _EXPLICIT_PRODUCT_IDENTITY.search(normalized) is not None
            and re.search(
                r"(?:중|비교|대조|수익률|금리|이율|만기|듀레이션|컨벡시티|"
                r"발행잔액|평가일단가)",
                normalized,
            )
            is not None
        ):
            # ``푸본현대생명보험24(후)...(KR...)`` is the issuer-bearing
            # name of an exact provided-DB bond, not a request to search for an
            # insurance policy.  The DB-resolved identity still has to pass the
            # normal Router/Compiler authority checks after this narrow scope
            # disambiguation.
            out_of_scope = None
        if _DATA_SCOPE_BYPASS.search(scope_surface) or out_of_scope is not None:
            return SafetyEnvelopeDecision(
                normalized,
                SafetyDisposition.UNSUPPORTED,
                SafetyGate.SCOPE,
                "현재 승인된 범위는 국내채권·국내/해외 ETP·공모펀드 데이터입니다.",
            )
        if _MULTITURN.search(normalized) and _EXPLICIT_PRODUCT_IDENTITY.search(normalized) is None:
            return SafetyEnvelopeDecision(
                normalized,
                SafetyDisposition.CLARIFY,
                SafetyGate.MULTITURN,
                "이전 대화 상태를 전제로 하지 말고 대상 상품이나 조건을 질문에 다시 명시해 주세요.",
            )
        return SafetyEnvelopeDecision(normalized)
