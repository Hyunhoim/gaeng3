from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from finance_agent_core.config import ValueType, load_field_registry
from finance_agent_core.contracts.queryplan import (
    AggregateFunction,
    ProductFamily,
    QueryPlan,
)


class AggregatePlanParseError(ValueError):
    """Raised when an aggregate request cannot be compiled without guessing."""


@dataclass(frozen=True)
class _Mention:
    field: str
    start: int
    end: int
    phrase: str


_COUNT_CONTEXT = re.compile(
    r"(?:상품|ETF|ETN|ETP|채권|펀드)(?:의)?\s*수(?:를|가|는)?\s*"
    r"(?:계산|집계|총합|알려)"
)
_FUNCTION_PATTERNS = {
    AggregateFunction.COUNT: re.compile(
        rf"몇\s*(?:개|건)|개수|건수|{_COUNT_CONTEXT.pattern}"
    ),
    AggregateFunction.AVG: re.compile(r"평균"),
    AggregateFunction.SUM: re.compile(r"합계|총합"),
    AggregateFunction.MIN: re.compile(r"최솟값|최소값|최저값|최소(?!\s*\d+\s*개)"),
    AggregateFunction.MAX: re.compile(r"최댓값|최대값|최고값|최대(?!\s*\d+\s*개)"),
}
_DISTRIBUTION = re.compile(r"분포|비중")
_IDENTITY_GROUP_FIELDS = {
    "product_id",
    "product_name",
    "short_name",
    "ticker",
    "isin",
}
_CURATED_GROUP_PHRASES = {
    "product_type": (
        "상품유형",
        "상품 유형",
        "상품의 유형",
        "ETF·ETN",
        "ETF/ETN",
        "ETP 유형",
    ),
    "asset_type": ("자산유형", "자산 유형", "자산군", "투자 자산 유형"),
    "investment_region": ("투자지역", "투자 지역", "지역"),
    "exchange_code": ("거래소",),
    "trading_currency": ("통화", "거래 통화"),
    "risk_level": ("위험등급", "위험 등급"),
    "bond_market": ("채권시장", "채권 시장"),
    "bond_major_class": ("채권대분류", "채권 대분류"),
    "bond_type": ("채권종류", "채권 종류", "채종"),
    "manager": ("운용사",),
    "sellable": ("판매여부", "판매 여부", "판매 상태"),
    "fund_geography_scope": ("국내외구분", "국내외 구분"),
    "fund_management_attribute": ("펀드운용속성", "펀드 운용속성", "운용속성"),
    "investor_type": ("투자자유형", "투자자 유형"),
    "currency_hedged": ("환헤지여부", "환헤지 여부"),
}


def _field_phrases(field_name: str, definition: Any) -> list[str]:
    values = [
        definition.label,
        *definition.aliases,
        field_name,
        field_name.replace("_", " "),
    ]
    return sorted(
        {value.strip() for value in values if value.strip()},
        key=lambda value: (-len(value), value),
    )


def _numeric_mentions(question: str, family: ProductFamily) -> list[_Mention]:
    registry = load_field_registry()
    mentions: list[_Mention] = []
    for field_name, base_definition in registry.fields.items():
        if family.value not in base_definition.datasets:
            continue
        definition = base_definition.resolve(family.value)
        if not definition.aggregatable or definition.value_type is not ValueType.NUMBER:
            continue
        best_by_span: dict[tuple[int, int], _Mention] = {}
        for phrase in _field_phrases(field_name, definition):
            for match in re.finditer(re.escape(phrase), question, flags=re.IGNORECASE):
                mention = _Mention(field_name, match.start(), match.end(), match.group(0))
                span = (mention.start, mention.end)
                current = best_by_span.get(span)
                if current is None or len(mention.phrase) > len(current.phrase):
                    best_by_span[span] = mention
        mentions.extend(best_by_span.values())
    return sorted(mentions, key=lambda item: (item.start, -len(item.phrase), item.field))


def _distance(function_match: re.Match[str], mention: _Mention) -> int:
    if mention.end <= function_match.start():
        return function_match.start() - mention.end
    if function_match.end() <= mention.start:
        return mention.start - function_match.end()
    return 0


def _target_field(
    function: AggregateFunction,
    match: re.Match[str],
    mentions: list[_Mention],
) -> str:
    if function is AggregateFunction.COUNT:
        return "product_id"
    if not mentions:
        raise AggregatePlanParseError(f"{function.value} 집계에 사용할 수치 필드를 확인할 수 없음")
    distances = [(_distance(match, mention), mention) for mention in mentions]
    shortest = min(distance for distance, _ in distances)
    closest_fields = {mention.field for distance, mention in distances if distance == shortest}
    if len(closest_fields) != 1:
        raise AggregatePlanParseError(
            f"{function.value} 대상 수치 필드가 둘 이상으로 해석됨: {sorted(closest_fields)}"
        )
    return closest_fields.pop()


def _group_fields(question: str, family: ProductFamily) -> list[str]:
    registry = load_field_registry()
    candidates: list[tuple[int, str]] = []
    distribution_requested = _DISTRIBUTION.search(question) is not None
    for field_name, base_definition in registry.fields.items():
        if family.value not in base_definition.datasets:
            continue
        definition = base_definition.resolve(family.value)
        if (
            not definition.selectable
            or definition.value_type in {ValueType.NUMBER, ValueType.DATE}
            or field_name in _IDENTITY_GROUP_FIELDS
        ):
            continue
        phrases = {
            *_field_phrases(field_name, definition),
            *_CURATED_GROUP_PHRASES.get(field_name, ()),
        }
        for phrase in sorted(phrases, key=lambda value: (-len(value), value)):
            by_match = re.search(
                rf"{re.escape(phrase)}\s*별",
                question,
                flags=re.IGNORECASE,
            )
            if by_match:
                candidates.append((by_match.start(), field_name))
                break
            if distribution_requested:
                distribution_match = re.search(
                    rf"{re.escape(phrase)}\s*(?:에\s*따라|기준(?:으로)?)?\s*"
                    r"(?:분포|비중)",
                    question,
                    flags=re.IGNORECASE,
                )
                if distribution_match:
                    candidates.append((distribution_match.start(), field_name))
                    break
    ordered = list(dict.fromkeys(field for _, field in sorted(candidates)))
    if distribution_requested and not ordered:
        raise AggregatePlanParseError("분포·비중 집계에 사용할 그룹 기준을 확인할 수 없음")
    if len(ordered) > 2:
        raise AggregatePlanParseError("그룹 기준은 한 번에 최대 두 개까지 지원")
    return ordered


def compile_aggregate_plan(
    *,
    question: str,
    question_id: str,
    family: ProductFamily,
    base_payload: dict[str, Any],
    requested_limit: int | None,
) -> QueryPlan:
    mentions = _numeric_mentions(question, family)
    requested: list[tuple[int, AggregateFunction, re.Match[str]]] = []
    for function, pattern in _FUNCTION_PATTERNS.items():
        for match in pattern.finditer(question):
            if function is AggregateFunction.SUM and any(
                context.start() <= match.start() < context.end()
                for context in _COUNT_CONTEXT.finditer(question)
            ):
                continue
            requested.append((match.start(), function, match))
    group_by = _group_fields(question, family)
    if _DISTRIBUTION.search(question) and not any(
        function is AggregateFunction.COUNT for _, function, _ in requested
    ):
        synthetic = _DISTRIBUTION.search(question)
        assert synthetic is not None
        requested.append((synthetic.start(), AggregateFunction.COUNT, synthetic))
    if not requested:
        raise AggregatePlanParseError(
            "집계 함수가 명확하지 않음; 개수·평균·합계·최솟값·최댓값 중 하나가 필요"
        )

    aggregation_items: list[dict[str, str]] = []
    for _, function, match in sorted(requested, key=lambda item: item[0]):
        field = _target_field(function, match, mentions)
        item = {"function": function.value, "field": field}
        if item not in aggregation_items:
            aggregation_items.append(item)

    projection = list(
        dict.fromkeys(
            [
                "product_id",
                *group_by,
                *(item["field"] for item in aggregation_items),
            ]
        )
    )
    amount_requested = any(
        item["function"] != AggregateFunction.COUNT.value
        and load_field_registry().require_field(item["field"], [family.value]).unit
        == "source_currency_amount"
        for item in aggregation_items
    )
    constraints = list(base_payload["constraints"])
    has_currency_scope = any(item["field"] == "trading_currency" for item in constraints)
    if (
        amount_requested
        and "trading_currency" not in group_by
        and not has_currency_scope
        and family in {ProductFamily.OVERSEAS_ETP, ProductFamily.DOMESTIC_ETP}
    ):
        constraints.append(
            {
                "field": "trading_currency",
                "operator": "eq",
                "value": "USD" if family is ProductFamily.OVERSEAS_ETP else "KRW",
                "unit": "code",
                "strength": "locked",
            }
        )

    payload = {
        **base_payload,
        "question_id": question_id,
        "intent": "aggregate",
        "product_families": [family.value],
        "constraints": constraints,
        "ranking": [],
        "projection": projection,
        "limit": requested_limit or (100 if group_by else 1),
        "intent_payload": {
            "comparison_fields": [],
            "group_by": group_by,
            "aggregations": aggregation_items,
            "explain_product_ids": [],
        },
    }
    if "trading_currency" in group_by:
        payload["ambiguities"] = [
            item for item in base_payload["ambiguities"] if item["span"] != "AUM 비교 통화"
        ]
    return QueryPlan.model_validate(payload)
