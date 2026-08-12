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
    r"(?:시스템|개발자)\s*(?:프롬프트|메시지|지시|규칙)"
    r".{0,32}?(?:출력|공개|노출|보여|알려|붙여|넣|첨부)|"
    r"(?:숨겨진|내부)\s*(?:지침|설정|규칙|프롬프트|데이터\s*경로)"
    r".{0,32}?(?:출력|공개|노출|보여|알려)|"
    r"(?:비밀|토큰|API\s*키|환경\s*변수|데이터베이스\s*경로)"
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
    r"(?:로그|기록).{0,24}?(?:삭제|지워|조작)|"
    r"(?:순위|결과).{0,20}?(?:조작|꾸미고|꾸며)|"
    r"(?:drop|disable)\s+(?:safety|guardrails?|filters?)|"
    r"(?:os\.environ|import\s+os|printenv)|"
    r"(?:SQL|쿼리).{0,20}?(?:작성|실행|만들)|"
    r"(?:DB|database|데이터베이스).{0,20}?(?:삭제|초기화|드롭|drop)|"
    r"(?:파일|디렉터리|폴더).{0,20}?(?:삭제|지워|제거)|"
    r"(?:서버|서비스).{0,20}?(?:재부팅|재시작|종료|중지)|"
    r"(?:셸|쉘|shell|bash|명령|command).{0,20}?(?:실행|run)",
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
    r"(?:임의|0|평균).{0,20}?(?:채워|대체|보정)",
    re.IGNORECASE,
)
_UNVERIFIABLE_FRESHNESS = re.compile(
    r"(?:현재\s*)?실시간\s*(?:호가|가격|시세|뉴스|공시|거래량)|"
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
_TRANSACTION = re.compile(
    r"(?:내\s*이름|본인\s*명의).{0,64}?(?:매수|매도|주문|가입|환매|거래)"
    r".{0,32}?(?:실제로|제출|체결|대신|끝내|실행)|"
    r"내가\s*보유한.{0,64}?(?:전부\s*)?(?:매도|환매).{0,32}?(?:체결|결과|끝내)|"
    r"내\s*(?:계좌\s*번호|계좌번호|비밀번호|패스워드)"
    r".{0,80}?(?:사용|확인|접속|거래|매수|매도|주문|체결|끝내)|"
    r"(?:계좌\s*잔고|내\s*잔고).{0,64}?(?:확인|조회)"
    r".{0,64}?(?:주문|매수|매도|거래)|"
    r"(?:매수|매도|주문|가입|환매).{0,48}?"
    r"(?:실제로\s*)?(?:제출|체결|대신\s*끝내|실행|처리|진행)|"
    r"지금\s*즉시.{0,48}?(?:매수|매도|주문).{0,32}?(?:지시|실행|제출)|"
    r"(?:사|구입(?:해)?|매입(?:해)?|구매(?:해)?)\s*"
    r"(?:줘|주세요|주십시오|달라)|"
    r"(?:팔아|처분(?:해)?|환매(?:해)?)\s*(?:줘|주세요|주십시오|달라)|"
    r"(?:주문|오더).{0,24}?(?:넣어|걸어|접수해)\s*"
    r"(?:줘|주세요|주십시오|달라)|"
    r"(?:체결|결제)(?:해)?\s*(?:줘|주세요|주십시오|달라)",
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
    r"(?:호재|악재).{0,20}?(?:반영|전망|예상)|"
    r"(?:전\s*재산|은퇴\s*자금|대출\s*받아|빚을?\s*내).{0,36}?"
    r"(?:넣|투자|살|사야|매수|골라|정해|추천|지시)|"
    r"(?:\d+\s*(?:일|주|개월|년))\s*(?:뒤|후).{0,32}?"
    r"(?:수익률|가격).{0,24}?(?:미리|예측|예상|정확)|"
    r"올해\s*말.{0,24}?(?:수익률|가격).{0,20}?(?:1등|최고)"
    r".{0,20}?(?:맞혀|예측|골라)|"
    r"(?:사야|팔아야)\s*할|매수\s*시점|"
    r"\b(?:pick|buy|recommend|predict)\b",
    re.IGNORECASE,
)
_GUARANTEE = re.compile(
    r"원금\s*(?:이\s*)?보장|수익\s*(?:이\s*)?보장|손실\s*(?:이\s*)?없|"
    r"손실.{0,16}?절대.{0,12}?없|"
    r"손실\s*가능성\s*(?:0\s*%|제로)|절대.{0,12}?손실.{0,12}?없|"
    r"무조건\s*(?:수익|오르|돈)|"
    r"(?:수익률?|가격).{0,16}?(?:확실|확정|보장)|"
    r"목표\s*수익률.{0,24}?(?:무조건|반드시).{0,16}?(?:달성|확정)|"
    r"확실(?:한|히)\s*(?:수익|오르|돈)|"
    r"(?:원금|수익|무손실)을?\s*(?:지켜|보장|보증)|"
    r"손해(?:를)?\s*(?:안\s*보|보지\s*않)|"
    r"원금(?:을|이)?\s*(?:안\s*(?:잃|까먹|줄)|(?:잃|줄)지\s*않)|"
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
    r"(?:파이썬|자바(?:스크립트)?|JavaScript|SQL)\s*"
    r"(?:코드|프로그램|게임)|코딩\s*(?:해|문제)",
    re.IGNORECASE,
)
_OUT_OF_SCOPE = re.compile(
    r"가상\s*자산|암호\s*화폐|비트코인|알트코인|코인\s*(?:시세|매매)|"
    r"\bcrypto(?:currency)?\b|"
    r"예금|적금|대출(?!형)|보험(?!회사채)|개별\s*주식|주식\s*종목|"
    r"선물\s*옵션|"
    r"외환\s*매매|환전|부동산\s*(?:매물|시세)",
    re.IGNORECASE,
)
_DATA_SCOPE_BYPASS = re.compile(
    r"(?:제공\s*)?데이터(?:에)?\s*(?:없어도|없더라도|밖(?:의|에서))|"
    r"(?:인터넷|웹|온라인)(?:을|에서|으로)?\s*.{0,48}?"
    r"(?:지식|검색|찾아|조회|가져|수집|크롤링|확인)|외부\s*지식|"
    r"상품(?:을|를)?\s*(?:지어내|만들어|창작)|가상(?:의)?\s*상품|"
    r"(?:없는|존재하지\s*않는|가짜|허구|KR-(?:FAKE|UNICORN)[A-Z0-9-]*)"
    r".{0,48}?(?:실제.{0,20}?처럼|가정|포함)|"
    r"(?:수익률|가격|수치|근거|인용).{0,20}?(?:만들|지어내|생성)|"
    r"(?:외부|개별|[A-Za-z0-9가-힣._-]{2,30}\s*주식).{0,36}?"
    r"(?:ETF|ETN|ETP|국내채권|공모펀드|ID).{0,28}?(?:바꿔|삽입|포함)|"
    r"(?:외부|개별).{0,12}?주식.{0,24}?(?:삽입|포함|섞)|"
    r"(?:외부|미국\s*국채|원문).{0,36}?(?:승인\s*(?:DB|데이터))"
    r".{0,24}?(?:추가|넣어|삽입)|"
    r"사모\s*펀드.{0,36}?공모\s*펀드.{0,28}?(?:포함|가정|승인\s*범위)|"
    r"(?:공모\s*펀드|국내채권|ETF|ETN).{0,12}?(?:이|라고)\s*가정|"
    r"(?:승인\s*(?:경계|범위)|공식\s*데이터).{0,24}?(?:무시|우회)|"
    r"실제.{0,12}?가짜\s*상품.{0,16}?(?:섞|포함)",
    re.IGNORECASE,
)
_NEGATED_EXTERNAL_SOURCE = re.compile(
    r"(?:인터넷|웹|온라인|외부\s*지식)(?:을|에서|으로)?\s*.{0,40}?"
    r"(?:검색|찾|조회|가져|사용|활용)?\s*(?:하지\s*말|하지\s*않|없이|제외)",
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
    r"(?:필요(?:가)?\s*없|원하지\s*않|하지\s*(?:말|않)|없이|제외)",
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

    return _NEGATED_POLICY_REQUEST.sub(" ", normalize_user_question(question))


def data_scope_policy_surface(question: str) -> str:
    """Mask an explicit refusal to use external sources before scope checks."""

    return _NEGATED_EXTERNAL_SOURCE.sub(" ", normalize_user_question(question))


class SafetyEnvelope:
    """Fail-closed pre-router envelope with explicit, auditable gates."""

    def evaluate(self, question: str) -> SafetyEnvelopeDecision:
        normalized = normalize_user_question(question)
        policy_surface = safety_policy_surface(normalized)
        scope_surface = data_scope_policy_surface(normalized)
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
            surface = (
                policy_surface
                if gate in {SafetyGate.FORECAST, SafetyGate.GUARANTEE}
                else normalized
            )
            matched = pattern.search(surface)
            if gate is SafetyGate.SECURITY:
                matched = (
                    matched
                    or _PROTECTED_MARKER.search(normalized)
                    or pattern.search(normalized[::-1])
                    or _PROTECTED_MARKER.search(normalized[::-1])
                )
            if matched:
                return SafetyEnvelopeDecision(normalized, disposition, gate, reason)

        if _DATA_SCOPE_BYPASS.search(scope_surface) or _OUT_OF_SCOPE.search(normalized):
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
