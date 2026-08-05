from __future__ import annotations

import re
from collections.abc import Iterable

from finance_agent_core.config.capability import CapabilityMatrix, load_capability_matrix
from finance_agent_core.contracts.queryplan import ProductFamily
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
_EXPLAIN = re.compile(r"설명|무슨\s*뜻|의미|왜\s|알려")
_DETAIL = re.compile(r"상세|세부|정보\s*조회|상품번호|종목코드|티커")
_AMBIGUOUS = re.compile(r"적당한|괜찮은|안전한|좋은\s*상품|추천할\s*만한")
_UNSUPPORTED = re.compile(
    r"전망|예측|예상\s*수익|수익\s*보장|원금\s*보장|"
    r"매수\s*추천|투자\s*추천|사야\s*할|가장\s*좋은|오를\s*것"
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
    families: list[ProductFamily] = []
    if re.search(r"공모\s*펀드|공모펀드", question, re.IGNORECASE):
        families.append(ProductFamily.FUND)

    etp_mentioned = (
        re.search(r"(?<![A-Z])(?:ETF|ETN|ETP)(?![A-Z])", question, re.IGNORECASE) is not None
    )
    if etp_mentioned:
        if re.search(r"국내|한국|코스피|코스닥", question):
            families.append(ProductFamily.DOMESTIC_ETP)
        if re.search(r"해외|미국|글로벌|NYSE|NASDAQ|AMEX", question, re.IGNORECASE):
            families.append(ProductFamily.OVERSEAS_ETP)
    elif re.search(
        r"국내\s*채권|국내채권|회사채|국채|국공채|국고채|특수채|금융채|"
        r"지역개발채|도시철도공채|채권\s*상품",
        question,
    ):
        families.append(ProductFamily.BOND)
    return list(dict.fromkeys(families))


def _requested_limit(question: str) -> int | None:
    matches = re.findall(r"(\d+)\s*(?:개|건)(?!월)", question)
    if not matches:
        return None
    value = int(matches[-1])
    return value if 1 <= value <= 100 else None


def _intent(question: str) -> InteractionIntent:
    if _UNSUPPORTED.search(question):
        return InteractionIntent.UNSUPPORTED
    if _AMBIGUOUS.search(question):
        return InteractionIntent.CLARIFY
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

        intent = _intent(stripped)
        families = _product_families(stripped)
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
        if len(families) != 1:
            return self._control(
                draft,
                RouteDisposition.CLARIFY,
                "ambiguous_product_family",
                "실행할 상품군을 하나로 확정할 수 없음",
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
