from __future__ import annotations

import re
from collections.abc import Iterable

from finance_agent_core.config.capability import CapabilityMatrix, load_capability_matrix
from finance_agent_core.contracts.queryplan import Intent, ProductFamily
from finance_agent_core.contracts.routing import (
    InteractionIntent,
    MinimalQueryDraft,
    RouteDecision,
    RouteDisposition,
)

_COMPARE = re.compile(r"비교|대조|차이|나란히|versus|\bvs\b", re.IGNORECASE)
_AGGREGATE = re.compile(
    r"몇\s*개|개수|건수|평균|합계|총합|집계|분포|비중|"
    r"최댓값|최대값|최솟값|최소값|최고값|최저값|"
    r"(?:AUM|보수율|수익률|이율|잔존일수|듀레이션)\s*(?:최대|최소)",
    re.IGNORECASE,
)
_EXPLAIN = re.compile(r"설명|무슨\s*뜻|뭐(?:야|고)|의미|장점|요소|왜\s|알려")
_DEFINITION = re.compile(r"무슨\s*뜻|뭐(?:야|고)|의미")
_DETAIL = re.compile(r"상세|세부|정보\s*조회|상품번호|종목코드|티커")
_AMBIGUOUS = re.compile(
    r"적당한|괜찮은|안전한|좋은\s*상품|추천(?:해|하|받|할\s*만한)|"
    r"좀\s*낮아도|많이\s*주는|제일\s*수익률|리스크.*신경\s*안|"
    r"뭐가\s*더\s*좋|어느.*더\s*좋|"
    r"(?:ETF|ETN).*(?:ETF|ETN).*(?:보수|수수료|비용)|"
    r"(?:가격.*왜|왜.*가격)",
    re.IGNORECASE,
)
_UNSUPPORTED = re.compile(
    r"전망|예측|예상\s*수익|수익\s*보장|원금\s*보장|"
    r"매수\s*추천|투자\s*추천|사야\s*할|사면\s*좋|가장\s*좋은|"
    r"기대되는\s*(?:고)?수익|오를\s*것|오를까|호재|악재|"
    r"유상증자|유상감자|사모\s*CB|"
    r"기관.*(?:구매|순매수|매매량)|"
    r"오늘.*가장.*(?:오른|상승)|"
    r"국내주식.*해외주식|해외주식.*국내주식",
    re.IGNORECASE,
)
_EXTERNAL_POLICY = re.compile(r"세율|세금|과세|거래\s*수수료", re.IGNORECASE)
_UNAVAILABLE_MARKET_DATA = re.compile(
    r"거래량|괴리율|배당.*(?:가장|최대)|"
    r"최근\s*[2-9]\s*년.*수익률|[2-9]\s*년간.*수익률|"
    r"우주항공.*수익률|하락장.*잘\s*버티|"
    r"변동성.*(?:낮|높|제일|가장)",
    re.IGNORECASE,
)
_MIXED_ETP_COST = re.compile(
    r"(?=.*ETF)(?=.*ETN)(?=.*(?:보수|수수료|비용))",
    re.IGNORECASE,
)
_OVERSEAS_UNAVAILABLE_METRIC = re.compile(
    r"수익률|변동성|하락장|오른|S&P\s*500",
    re.IGNORECASE,
)
_FUND_UNAVAILABLE_DETAIL = re.compile(
    r"최근\s*3\s*년.*수익률|언제든.*(?:꺼내|환매)",
    re.IGNORECASE,
)
_QUOTED = (
    re.compile(r'"([^"\n]+)"'),
    re.compile(r"'([^'\n]+)'"),
    re.compile(r"“([^”\n]+)”"),
    re.compile(r"‘([^’\n]+)’"),
)
_KNOWN_ID = re.compile(
    r"(?<![A-Z0-9])(?:KR[A-Z0-9]{10}|[A-Z]{2,5}:[A-Z0-9._-]+)(?![A-Z0-9])",
    re.IGNORECASE,
)
_LABELED_ID = re.compile(
    r"(?:상품번호|종목코드|티커)\s*[:：]?\s*([A-Z0-9._:-]{2,30})",
    re.IGNORECASE,
)


def _ordered_unique(values: Iterable[str]) -> list[str]:
    return list(dict.fromkeys(value.strip() for value in values if value.strip()))


def _product_mentions(question: str) -> list[str]:
    mentions: list[tuple[int, str]] = []
    for pattern in _QUOTED:
        mentions.extend((match.start(), match.group(1)) for match in pattern.finditer(question))
    mentions.extend((match.start(), match.group(0)) for match in _KNOWN_ID.finditer(question))
    mentions.extend((match.start(), match.group(1)) for match in _LABELED_ID.finditer(question))
    return _ordered_unique(value for _, value in sorted(mentions))


def _product_families(question: str) -> list[ProductFamily]:
    mentions: list[tuple[int, ProductFamily]] = []

    def add_matches(pattern: str, family: ProductFamily, *, flags: int = 0) -> None:
        mentions.extend((match.start(), family) for match in re.finditer(pattern, question, flags))

    add_matches(r"공모\s*펀드|공모펀드", ProductFamily.FUND, flags=re.IGNORECASE)
    add_matches(
        r"국내\s*채권|국내채권|회사채|국채|국공채|국고채|특수채|금융채|"
        r"지역개발채|도시철도공채|채권\s*상품|채권(?!형)",
        ProductFamily.BOND,
    )

    etp_token = r"(?<![A-Z])(?:ETF|ETN|ETP)(?![A-Z])"
    domestic_pattern = (
        rf"(?:국내|한국|코스피|코스닥)(?!\s*채권)[^와과,\n]{{0,20}}?{etp_token}|"
        rf"{etp_token}\s*(?:국내|한국)"
    )
    overseas_pattern = (
        rf"(?:해외|글로벌)[^와과,\n]{{0,20}}?{etp_token}|"
        rf"{etp_token}\s*해외"
    )
    domestic_matches = list(re.finditer(domestic_pattern, question, re.IGNORECASE))
    overseas_matches = list(re.finditer(overseas_pattern, question, re.IGNORECASE))
    mentions.extend((match.start(), ProductFamily.DOMESTIC_ETP) for match in domestic_matches)
    mentions.extend((match.start(), ProductFamily.OVERSEAS_ETP) for match in overseas_matches)

    if re.search(etp_token, question, re.IGNORECASE):
        explicit_etp_family = domestic_matches or overseas_matches
        if not explicit_etp_family:
            if re.search(r"NYSE|NASDAQ|AMEX", question, re.IGNORECASE):
                match = re.search(r"NYSE|NASDAQ|AMEX", question, re.IGNORECASE)
                assert match is not None
                mentions.append((match.start(), ProductFamily.OVERSEAS_ETP))
            elif re.search(r"미국", question):
                match = re.search(r"미국", question)
                assert match is not None
                mentions.append((match.start(), ProductFamily.OVERSEAS_ETP))

    domestic_brand = re.search(r"\b(?:TIGER|ACE)\b", question, re.IGNORECASE)
    if domestic_brand is not None:
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

    fund = re.search(r"(?<!사모)\s*펀드", question, re.IGNORECASE)
    if fund is not None:
        mentions.append((fund.start(), ProductFamily.FUND))

    ordered = [family for _, family in sorted(mentions, key=lambda item: item[0])]
    return list(dict.fromkeys(ordered))


def _requested_limit(question: str) -> int | None:
    matches = re.findall(r"(\d+)\s*(?:개|건)(?!월)", question)
    if not matches:
        return None
    value = int(matches[-1])
    return value if 1 <= value <= 100 else None


def _intent(question: str, families: list[ProductFamily]) -> InteractionIntent:
    if (
        _UNSUPPORTED.search(question)
        or _EXTERNAL_POLICY.search(question)
        or _UNAVAILABLE_MARKET_DATA.search(question)
        or (
            families == [ProductFamily.OVERSEAS_ETP]
            and _OVERSEAS_UNAVAILABLE_METRIC.search(question)
            and not _COMPARE.search(question)
        )
        or (ProductFamily.FUND in families and _FUND_UNAVAILABLE_DETAIL.search(question))
    ):
        return InteractionIntent.UNSUPPORTED
    if _AMBIGUOUS.search(question) or _MIXED_ETP_COST.search(question):
        return InteractionIntent.CLARIFY
    if _DEFINITION.search(question):
        return InteractionIntent.EXPLAIN
    if _COMPARE.search(question):
        return InteractionIntent.COMPARE
    if _AGGREGATE.search(question):
        return InteractionIntent.AGGREGATE
    if _EXPLAIN.search(question):
        return InteractionIntent.EXPLAIN
    if _DETAIL.search(question):
        return InteractionIntent.DETAIL
    return InteractionIntent.SEARCH


class IntentRouter:
    """Deterministic, fail-closed router in front of any model-generated plan."""

    def __init__(self, matrix: CapabilityMatrix | None = None) -> None:
        self.matrix = matrix or load_capability_matrix()

    def route(self, question: str, request_id: str) -> RouteDecision:
        stripped = question.strip()
        if not stripped:
            raise ValueError("question cannot be blank")
        if not request_id.strip():
            raise ValueError("request_id cannot be blank")

        families = _product_families(stripped)
        intent = _intent(stripped, families)
        mentions = _product_mentions(stripped)
        draft = MinimalQueryDraft(
            request_id=request_id,
            question=stripped,
            intent=intent,
            product_families=families,
            product_mentions=mentions,
            requested_limit=_requested_limit(stripped),
        )

        if intent is InteractionIntent.UNSUPPORTED:
            return self._control(
                draft,
                RouteDisposition.UNSUPPORTED,
                "prohibited_financial_request",
                "제공 데이터로 검증할 수 없는 예측·보장·단정적 추천 요청",
            )
        if intent is InteractionIntent.CLARIFY:
            return self._control(
                draft,
                RouteDisposition.CLARIFY,
                "subjective_condition",
                "판단 기준이나 임계값이 명시되지 않은 주관적 조건",
            )
        if not families:
            return self._control(
                draft,
                RouteDisposition.CLARIFY,
                "ambiguous_product_family",
                "실행할 상품군을 확정할 수 없음",
            )
        if len(families) > 1:
            if intent is not InteractionIntent.SEARCH:
                return self._control(
                    draft,
                    RouteDisposition.CLARIFY,
                    "ambiguous_product_family",
                    "복수 상품군은 현재 상품군별 독립 검색만 지원",
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
                return self._control(
                    draft,
                    RouteDisposition.UNSUPPORTED,
                    "capability_not_implemented",
                    blocked.reason,
                )
            return RouteDecision(
                draft=draft,
                disposition=RouteDisposition.EXECUTE,
                reason_code="cross_family_search_executable",
                reason="복수 상품군을 각각 독립적으로 검색하고 검증",
                query_plan_intent=Intent.SEARCH,
                capability_matrix_version=self.matrix.matrix_version,
            )
        if intent in {InteractionIntent.DETAIL, InteractionIntent.EXPLAIN} and not mentions:
            return self._control(
                draft,
                RouteDisposition.CLARIFY,
                "missing_product_identity",
                "상세 조회·설명에는 정확한 상품번호나 종목코드가 필요",
            )
        if intent is InteractionIntent.COMPARE and len(mentions) != 2:
            return self._control(
                draft,
                RouteDisposition.CLARIFY,
                "missing_product_identity",
                "비교에는 서로 다른 두 상품의 정확한 식별자가 필요",
            )

        capability = self.matrix.require(families[0], intent)
        if capability.status == "unsupported":
            return self._control(
                draft,
                RouteDisposition.UNSUPPORTED,
                "capability_not_implemented",
                capability.reason,
            )
        if capability.status == "control":
            disposition = (
                RouteDisposition.CLARIFY
                if intent is InteractionIntent.CLARIFY
                else RouteDisposition.UNSUPPORTED
            )
            return self._control(
                draft,
                disposition,
                f"{intent.value}_control",
                capability.reason,
            )
        return RouteDecision(
            draft=draft,
            disposition=RouteDisposition.EXECUTE,
            reason_code="capability_executable",
            reason=capability.reason,
            query_plan_intent=capability.query_plan_intent,
            capability_matrix_version=self.matrix.matrix_version,
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
