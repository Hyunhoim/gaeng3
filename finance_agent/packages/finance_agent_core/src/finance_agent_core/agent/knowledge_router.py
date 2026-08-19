from __future__ import annotations

import re
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from finance_agent_core.contracts.knowledge import (
    KnowledgeQueryPlan,
    RelationKnowledgeOperation,
)
from finance_agent_core.contracts.queryplan import ProductFamily
from finance_agent_core.retrieval.relations import RelationType


class KnowledgeRouteDisposition(StrEnum):
    """Public routing outcome before any knowledge retrieval is attempted."""

    EXECUTE = "execute"
    NOT_APPLICABLE = "not_applicable"
    CLARIFY = "clarify"
    UNSUPPORTED = "unsupported"


class KnowledgeRouteDecision(BaseModel):
    """Strict seam between the legacy product router and relation retrieval."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["1.0"] = "1.0"
    disposition: KnowledgeRouteDisposition
    reason_code: str = Field(min_length=1, max_length=100)
    reason: str = Field(min_length=1, max_length=500)
    plan: KnowledgeQueryPlan | None = None

    @model_validator(mode="after")
    def validate_execution_contract(self) -> KnowledgeRouteDecision:
        if (self.disposition is KnowledgeRouteDisposition.EXECUTE) != (self.plan is not None):
            raise ValueError("only executable knowledge routes may contain a plan")
        return self


class KnowledgeRoutedExecutionError(RuntimeError):
    """Preserve a trusted knowledge route when execution fails downstream."""

    def __init__(self, decision: KnowledgeRouteDecision, cause: Exception) -> None:
        if type(decision) is not KnowledgeRouteDecision:
            raise TypeError("decision must be a KnowledgeRouteDecision")
        if decision.disposition is not KnowledgeRouteDisposition.EXECUTE:
            raise ValueError("only an executable knowledge route can fail downstream")
        self.decision = decision
        self.cause = cause
        super().__init__("trusted knowledge route execution failed")


_FAMILY_PATTERNS: dict[ProductFamily, re.Pattern[str]] = {
    ProductFamily.BOND: re.compile(
        r"국내\s*채권(?!\s*형\s*(?:ETF|ETN|ETP))|"
        r"채권\s*(?:상품|종목)",
        re.IGNORECASE,
    ),
    ProductFamily.DOMESTIC_ETP: re.compile(
        r"국내\s*(?:ETF|ETN|ETP)",
        re.IGNORECASE,
    ),
    ProductFamily.OVERSEAS_ETP: re.compile(
        r"해외\s*(?:ETF|ETN|ETP)",
        re.IGNORECASE,
    ),
    ProductFamily.FUND: re.compile(r"(?:공모\s*)?펀드", re.IGNORECASE),
}
_BARE_ETP = re.compile(r"(?<![A-Z])(?:ETF|ETN|ETP)(?![A-Z])", re.IGNORECASE)

_RELATION_SIGNALS: dict[RelationType, re.Pattern[str]] = {
    RelationType.ISSUED_BY: re.compile(r"발행\s*사|발행\s*(?:하|한|된)", re.IGNORECASE),
    RelationType.MANAGED_BY: re.compile(
        r"(?:자산\s*)?운용\s*사|운용\s*(?:하|한|되)",
        re.IGNORECASE,
    ),
    RelationType.TRACKS_INDEX: re.compile(
        r"기초\s*지수|추종\s*(?:하|한)|따르\s*는|연동\s*(?:하|한|된)",
        re.IGNORECASE,
    ),
    RelationType.INVESTS_IN_REGION: re.compile(
        r"투자\s*지역|(?:에|로)\s*투자\s*(?:하|한|되)",
        re.IGNORECASE,
    ),
    RelationType.CLASSIFIED_AS_ASSET: re.compile(r"자산\s*(?:유형|군)", re.IGNORECASE),
}

_ENTITY = r"[0-9A-Za-z가-힣&().·+_\-/ ]{1,100}?"
_ENTITY_PATTERNS: dict[RelationType, tuple[re.Pattern[str], ...]] = {
    RelationType.ISSUED_BY: (
        re.compile(
            rf"^\s*(?:현재\s+)?(?P<entity>{_ENTITY})(?:이|가|에서)\s*발행\s*(?:하는|한)",
            re.IGNORECASE,
        ),
        re.compile(
            rf"발행\s*사(?:가|는|은|이)?\s*[:：=]?\s*(?P<entity>{_ENTITY})"
            rf"인(?=\s*(?:국내|채권|상품))",
            re.IGNORECASE,
        ),
    ),
    RelationType.MANAGED_BY: (
        re.compile(
            rf"^\s*(?:현재\s+)?(?P<entity>{_ENTITY})(?:이|가|에서)\s*운용\s*(?:하는|한)",
            re.IGNORECASE,
        ),
        re.compile(
            rf"(?:자산\s*)?운용\s*사(?:가|는|은|이)?\s*[:：=]?\s*(?P<entity>{_ENTITY})"
            rf"인(?=\s*(?:국내|해외|ETF|ETN|ETP|상품))",
            re.IGNORECASE,
        ),
    ),
    RelationType.TRACKS_INDEX: (
        re.compile(
            rf"^\s*(?:현재\s+)?(?P<entity>{_ENTITY})(?:을|를)\s*"
            rf"(?:추종\s*(?:하는|한)|따르는|연동\s*(?:하는|한|된))",
            re.IGNORECASE,
        ),
        re.compile(
            rf"기초\s*지수(?:가|는|은|이)?\s*[:：=]?\s*(?P<entity>{_ENTITY})"
            rf"인(?=\s*(?:국내|해외|ETF|ETN|ETP|상품))",
            re.IGNORECASE,
        ),
    ),
    RelationType.INVESTS_IN_REGION: (
        re.compile(
            rf"^\s*(?:현재\s+)?(?P<entity>{_ENTITY})(?:에|로)\s*투자\s*(?:하는|한)",
            re.IGNORECASE,
        ),
        re.compile(
            rf"투자\s*지역(?:가|는|은|이)?\s*[:：=]?\s*(?P<entity>{_ENTITY})"
            rf"인(?=\s*(?:국내|해외|ETF|ETN|ETP|상품))",
            re.IGNORECASE,
        ),
    ),
    RelationType.CLASSIFIED_AS_ASSET: (
        re.compile(
            rf"자산\s*(?:유형|군)(?:이|가|는|은)?\s*[:：=]?\s*(?P<entity>{_ENTITY})"
            rf"인(?=\s*(?:국내|해외|ETF|ETN|ETP|상품))",
            re.IGNORECASE,
        ),
    ),
}

# This mirrors the P0-6 approved relation index. Overseas manager and base-index
# fields are intentionally absent until their source contract is approved.
_SUPPORTED_FAMILIES: dict[RelationType, frozenset[ProductFamily]] = {
    RelationType.ISSUED_BY: frozenset({ProductFamily.BOND}),
    RelationType.MANAGED_BY: frozenset({ProductFamily.DOMESTIC_ETP}),
    RelationType.TRACKS_INDEX: frozenset({ProductFamily.DOMESTIC_ETP}),
    RelationType.INVESTS_IN_REGION: frozenset(
        {ProductFamily.DOMESTIC_ETP, ProductFamily.OVERSEAS_ETP}
    ),
    RelationType.CLASSIFIED_AS_ASSET: frozenset(
        {ProductFamily.DOMESTIC_ETP, ProductFamily.OVERSEAS_ETP}
    ),
}

_UNSUPPORTED_RELATION_SCOPE = re.compile(
    r"테마|편입\s*(?:종목|자산)|보유\s*(?:종목|자산)|구성\s*종목|"
    r"약관|설명서|문서|공시",
    re.IGNORECASE,
)
_PROHIBITED_ACTION = re.compile(
    r"전망|예측|추천|사야|매수|수익\s*보장|CSV|엑셀|다운로드|내보내|출력\s*파일|"
    r"이전\s*지시\s*무시|시스템\s*프롬프트|ignore\s+(?:all\s+)?previous|"
    r"drop\s+table|<script|```",
    re.IGNORECASE,
)
_ADDITIONAL_PRODUCT_CONDITION = re.compile(
    r"AUM|총\s*보수|보수율|수익률|이율|금리|만기|신용\s*등급|거래\s*통화|"
    r"가격|시가\s*총액|듀레이션|잔존\s*일수|오름차순|내림차순|상위|하위|"
    r"높은\s*순|낮은\s*순|큰\s*순|작은\s*순",
    re.IGNORECASE,
)
_COORDINATED_ENTITY = re.compile(r"(?:또는|혹은|및|와|과)\s+|[,/;]", re.IGNORECASE)
_INVALID_ENTITY = re.compile(
    r"(?:ETF|ETN|ETP|펀드|상품|어떤|어느|해당|모든|전체|추천|전망)",
    re.IGNORECASE,
)
_LIMIT = re.compile(r"(\d+)\s*(?:개|건)(?!월)")


def _decision(
    disposition: KnowledgeRouteDisposition,
    reason_code: str,
    reason: str,
    *,
    plan: KnowledgeQueryPlan | None = None,
) -> KnowledgeRouteDecision:
    return KnowledgeRouteDecision(
        disposition=disposition,
        reason_code=reason_code,
        reason=reason,
        plan=plan,
    )


def _mentioned_families(question: str) -> tuple[ProductFamily, ...]:
    matches: list[tuple[int, ProductFamily]] = []
    for family, pattern in _FAMILY_PATTERNS.items():
        matches.extend((item.start(), family) for item in pattern.finditer(question))
    return tuple(dict.fromkeys(family for _, family in sorted(matches)))


def _relation_signals(question: str) -> tuple[RelationType, ...]:
    return tuple(
        relation_type
        for relation_type, pattern in _RELATION_SIGNALS.items()
        if pattern.search(question)
    )


def _entity_candidates(question: str, relation_type: RelationType) -> tuple[str, ...]:
    candidates: list[str] = []
    for pattern in _ENTITY_PATTERNS[relation_type]:
        for match in pattern.finditer(question):
            entity = match.group("entity").strip()
            if entity and entity not in candidates:
                candidates.append(entity)
    return tuple(candidates)


class DeterministicKnowledgeRouter:
    """Compile explicit relation questions after the caller's safety gate.

    ``not_applicable`` means the caller must continue through the established
    product router. This class never calls an LLM and never guesses a family,
    predicate, entity alias, or unsupported relation source.
    """

    def route_after_safety_gate(
        self,
        question: str,
        question_id: str,
        *,
        safety_gate_passed: bool,
        default_top_k: int = 5,
    ) -> KnowledgeRouteDecision:
        if type(safety_gate_passed) is not bool:
            raise TypeError("safety_gate_passed must be a boolean")
        if not question.strip():
            raise ValueError("question cannot be blank")
        if not question_id.strip():
            raise ValueError("question_id cannot be blank")
        if type(default_top_k) is not int or not 1 <= default_top_k <= 20:
            raise ValueError("default_top_k must be an integer from 1 to 20")
        if not safety_gate_passed:
            return _decision(
                KnowledgeRouteDisposition.UNSUPPORTED,
                "safety_gate_required",
                "관계 검색은 호출자의 안전 검사가 통과된 뒤에만 실행할 수 있음",
            )

        signals = _relation_signals(question)
        if not signals:
            return _decision(
                KnowledgeRouteDisposition.NOT_APPLICABLE,
                "not_relation_question",
                "명시적인 관계 질문이 아니므로 기존 상품 라우터로 전달",
            )

        if _PROHIBITED_ACTION.search(question):
            return _decision(
                KnowledgeRouteDisposition.UNSUPPORTED,
                "prohibited_relation_request",
                "관계 검색에 예측·추천·파일 반출 또는 지시 변조 요청을 함께 실행할 수 없음",
            )
        if _UNSUPPORTED_RELATION_SCOPE.search(question):
            return _decision(
                KnowledgeRouteDisposition.UNSUPPORTED,
                "relation_source_not_approved",
                "테마·편입종목·외부 문서 관계는 현재 승인된 P0-6 관계 색인에 없음",
            )
        if _ADDITIONAL_PRODUCT_CONDITION.search(question):
            return _decision(
                KnowledgeRouteDisposition.CLARIFY,
                "additional_relation_conditions",
                "관계 검색과 다른 수치·정렬 조건을 한 요청에서 함께 실행할 수 없음",
            )
        if len(signals) != 1:
            return _decision(
                KnowledgeRouteDisposition.CLARIFY,
                "multiple_relation_predicates",
                "한 번에 하나의 관계 조건만 검색할 수 있음",
            )

        relation_type = signals[0]
        families = _mentioned_families(question)
        if ProductFamily.FUND in families:
            return _decision(
                KnowledgeRouteDisposition.UNSUPPORTED,
                "fund_relation_source_unavailable",
                "공모펀드 관계 출처 계약이 아직 승인되지 않음",
            )
        if len(families) > 1:
            return _decision(
                KnowledgeRouteDisposition.CLARIFY,
                "ambiguous_product_family",
                "관계 검색은 한 번에 하나의 상품군을 명시해야 함",
            )
        if not families:
            reason = (
                "국내 ETF·ETN 또는 해외 ETF·ETN 중 하나를 명시해야 함"
                if _BARE_ETP.search(question)
                else "관계 검색을 실행할 상품군을 명시해야 함"
            )
            return _decision(
                KnowledgeRouteDisposition.CLARIFY,
                "ambiguous_product_family",
                reason,
            )

        family = families[0]
        if family not in _SUPPORTED_FAMILIES[relation_type]:
            return _decision(
                KnowledgeRouteDisposition.UNSUPPORTED,
                "relation_family_unavailable",
                "요청한 관계와 상품군 조합은 승인된 P0-6 관계 색인에 없음",
            )

        entities = _entity_candidates(question, relation_type)
        if not entities:
            return _decision(
                KnowledgeRouteDisposition.CLARIFY,
                "missing_relation_entity",
                "검색할 발행사·운용사·기초지수·지역·자산유형을 정확히 명시해야 함",
            )
        if len(entities) != 1 or _COORDINATED_ENTITY.search(entities[0]):
            return _decision(
                KnowledgeRouteDisposition.CLARIFY,
                "multiple_relation_entities",
                "한 번에 하나의 관계 대상만 검색할 수 있음",
            )
        entity = entities[0]
        if _INVALID_ENTITY.search(entity) or (
            entity == "채권" and relation_type is not RelationType.CLASSIFIED_AS_ASSET
        ):
            return _decision(
                KnowledgeRouteDisposition.CLARIFY,
                "invalid_relation_entity",
                "관계 대상을 하나의 명시적인 값으로 확정할 수 없음",
            )

        requested_limits = tuple(dict.fromkeys(int(item) for item in _LIMIT.findall(question)))
        if len(requested_limits) > 1:
            return _decision(
                KnowledgeRouteDisposition.CLARIFY,
                "ambiguous_result_limit",
                "결과 개수를 하나로 확정해야 함",
            )
        top_k = requested_limits[0] if requested_limits else default_top_k
        if not 1 <= top_k <= 20:
            return _decision(
                KnowledgeRouteDisposition.UNSUPPORTED,
                "relation_limit_out_of_range",
                "관계 검색은 한 번에 1건에서 20건까지만 실행할 수 있음",
            )

        plan = KnowledgeQueryPlan(
            question_id=question_id,
            question=question,
            operation=RelationKnowledgeOperation(
                query=entity,
                relation_types=(relation_type,),
                product_families=(family,),
                top_k=top_k,
            ),
        )
        return _decision(
            KnowledgeRouteDisposition.EXECUTE,
            "knowledge_relation_executable",
            "승인된 단일 관계 검색 계획을 결정론적으로 확정",
            plan=plan,
        )


__all__ = [
    "DeterministicKnowledgeRouter",
    "KnowledgeRouteDecision",
    "KnowledgeRouteDisposition",
    "KnowledgeRoutedExecutionError",
]
