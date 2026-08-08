from __future__ import annotations

import json
import re
import unicodedata
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, model_validator

from finance_agent_core.agent.linker import canonicalize_query_plan_payload
from finance_agent_core.agent.product_comparison import comparison_projection
from finance_agent_core.config import FieldDefinition, load_field_registry
from finance_agent_core.contracts.queryplan import (
    SEARCH_PROJECTION_BY_FAMILY,
    AggregateFunction,
    Aggregation,
    Constraint,
    ConstraintOperator,
    ConstraintStrength,
    Intent,
    IntentPayload,
    NullPlacement,
    ProductFamily,
    QueryPlan,
    Ranking,
    SortDirection,
    Unit,
)
from finance_agent_core.contracts.routing import (
    InteractionIntent,
    RouteDecision,
    RouteDisposition,
)
from finance_agent_core.storage import ProductIdentitySnapshotCache

type ProposalScalar = bool | int | float | str
type ProposalValue = ProposalScalar | list[ProposalScalar]

_IDENTITY_FIELDS = {"product_id", "ticker", "isin"}
_ALLOWED_RESCUE_CONTROL_REASONS = {"ambiguous_product_family", "missing_product_identity"}
_FIELD_LEXEMES: dict[str, tuple[str, ...]] = {
    "product_id": ("상품 ID", "상품ID", "상품번호", "상품 식별자"),
    "ticker": ("티커", "종목코드", "종목 번호"),
    "isin": ("ISIN",),
    "product_type": ("ETF", "ETN", "ETP 유형", "상품 유형"),
    "sellable": (
        "판매 가능",
        "판매 중",
        "살 수 있",
        "팔 수 있",
        "거래 가능",
        "거래할 수 있",
    ),
    "trading_suspended": (
        "거래 중지",
        "거래 정지",
        "거래 가능",
        "거래할 수 있",
    ),
    "pension_eligible": ("연금", "연금계좌", "연금 거래"),
    "currently_buyable": ("매수 가능", "구매 가능", "살 수 있"),
    "public_offering": ("공모", "공모펀드"),
    "investment_region": ("투자 지역", "지역", "국가", "미국", "해외", "국내"),
    "asset_type": ("자산 유형", "자산군", "기초 자산", "주식형", "채권형"),
    "fund_geography_scope": ("국내외", "해외", "국내"),
    "fund_management_attribute": ("펀드 유형", "운용 속성", "주식형", "채권형"),
    "total_expense_ratio_pct": ("총보수율", "총보수", "보수율"),
    "aum": ("AUM", "순자산", "운용 자산"),
    "one_month_return_pct": (
        "1개월 수익률",
        "1M 수익률",
        "월간 수익률",
        "한 달 수익률",
    ),
    "three_month_return_pct": ("3개월 수익률", "3M 수익률", "석 달 수익률"),
    "remaining_days": ("잔존일수", "잔존일", "만기까지 남은"),
    "buy_yield_pct": ("매수수익률", "매수 수익률", "세전 매수수익률"),
    "coupon_rate_pct": ("표면이율", "표면금리", "쿠폰금리"),
    "close_price": ("종가", "마감 가격"),
    "risk_level": ("위험등급", "위험도"),
    "trading_currency": ("거래 통화", "통화", "원화", "KRW", "달러", "USD"),
}
_VALUE_ALIASES: dict[str, tuple[str, ...]] = {
    "United States of America": ("미국",),
    "Bond": ("채권", "채권형"),
    "Equity": ("주식", "주식형"),
    "KRW": ("원화", "KRW"),
    "USD": ("달러", "USD"),
}
_FAMILY_ENUM_ALIASES: dict[ProductFamily, dict[str, str]] = {
    ProductFamily.DOMESTIC_ETP: {
        "Equity": "주식",
        "Bond": "채권",
        "Commodity": "원자재",
        "Mixed Assets": "혼합자산",
        "Money Market": "단기자금",
        "United States of America": "미국",
    },
}
_OPERATOR_PATTERNS = {
    ConstraintOperator.NEQ: re.compile(r"아닌|아니|제외|말고|빼고"),
    ConstraintOperator.NOT_IN: re.compile(r"제외|말고|빼고|아닌|아니"),
    ConstraintOperator.LTE: re.compile(r"이하|넘지\s*않|≤"),
    ConstraintOperator.LT: re.compile(r"미만|<"),
    ConstraintOperator.GTE: re.compile(r"이상|적어도|≥"),
    ConstraintOperator.GT: re.compile(r"초과|>"),
    ConstraintOperator.BETWEEN: re.compile(r"사이|범위|에서.+까지"),
}
_DIRECTION_PATTERNS = {
    SortDirection.ASC: re.compile(r"낮|작|적|짧|빠|오름"),
    SortDirection.DESC: re.compile(r"높|큰|많|상위|내림"),
}
_IDENTITY_CANDIDATE = re.compile(
    r"(?<![A-Z0-9])(?:KR[A-Z0-9]{10}|(?:[A-Z]{2,5}|[0-9]{3}):[A-Z0-9._-]+|"
    r"[A-Z0-9][A-Z0-9._-]{0,29})(?![A-Z0-9])",
    re.IGNORECASE,
)
_IDENTITY_CUE = re.compile(
    r"티커|종목\s*코드|상품\s*(?:ID|번호|식별자)|ISIN|"
    r"코드\s*(?:가|는|를|이|인|:|：)",
    re.IGNORECASE,
)
_RESERVED_UNCUED_IDENTITIES = {
    "AUM",
    "ETF",
    "ETN",
    "ETP",
    "ISIN",
    "KRW",
    "USD",
}
_FUNCTION_PATTERNS = {
    AggregateFunction.COUNT: re.compile(r"몇|개수|건수|상품\s*수|분포|집계"),
    AggregateFunction.AVG: re.compile(r"평균"),
    AggregateFunction.MAX: re.compile(r"최대|최댓|최고"),
    AggregateFunction.MIN: re.compile(r"최소|최솟|최저"),
    AggregateFunction.SUM: re.compile(r"합계|총합"),
}


class GroundedPlanningModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class GroundedConstraintProposal(GroundedPlanningModel):
    field: str
    operator: ConstraintOperator
    value: ProposalValue
    evidence_span: str = Field(min_length=1, max_length=300)

    @model_validator(mode="after")
    def validate_value_shape(self) -> GroundedConstraintProposal:
        is_list = isinstance(self.value, list)
        if self.operator in {ConstraintOperator.IN, ConstraintOperator.NOT_IN}:
            if not is_list or not self.value:
                raise ValueError(f"{self.operator.value} requires a non-empty list")
        elif self.operator is ConstraintOperator.BETWEEN:
            if not is_list or len(self.value) != 2:
                raise ValueError("between requires a two-item list")
        elif is_list:
            raise ValueError(f"{self.operator.value} requires a scalar")
        return self


class GroundedRankingProposal(GroundedPlanningModel):
    field: str
    direction: SortDirection
    evidence_span: str = Field(min_length=1, max_length=300)


class GroundedFieldProposal(GroundedPlanningModel):
    field: str
    evidence_span: str = Field(min_length=1, max_length=300)


class GroundedAggregationProposal(GroundedPlanningModel):
    function: AggregateFunction
    field: str
    evidence_span: str = Field(min_length=1, max_length=300)


class GroundedIssueProposal(GroundedPlanningModel):
    span: str = Field(min_length=1, max_length=300)
    reason: str = Field(min_length=1, max_length=500)


class GroundedPlanProposal(GroundedPlanningModel):
    schema_version: Literal["1.0"]
    question_id: str = Field(min_length=1, max_length=128)
    intent: Intent
    product_family: ProductFamily
    family_evidence_span: str = Field(min_length=1, max_length=300)
    constraints: list[GroundedConstraintProposal] = Field(max_length=20)
    ranking: list[GroundedRankingProposal] = Field(max_length=5)
    limit: int = Field(ge=1, le=100)
    limit_evidence_span: str = Field(max_length=100)
    comparison_fields: list[GroundedFieldProposal] = Field(max_length=20)
    group_by: list[GroundedFieldProposal] = Field(max_length=10)
    aggregations: list[GroundedAggregationProposal] = Field(max_length=10)
    ambiguities: list[GroundedIssueProposal] = Field(max_length=10)
    unsupported_conditions: list[GroundedIssueProposal] = Field(max_length=10)

    @model_validator(mode="after")
    def validate_intent_shape(self) -> GroundedPlanProposal:
        if self.intent is Intent.SEARCH:
            if self.comparison_fields or self.group_by or self.aggregations:
                raise ValueError("search proposal contains another intent payload")
        elif self.intent is Intent.COMPARE:
            if not self.comparison_fields:
                raise ValueError("compare proposal requires comparison_fields")
            if self.ranking or self.group_by or self.aggregations:
                raise ValueError("compare proposal contains another intent payload")
        elif self.intent is Intent.AGGREGATE:
            if not self.aggregations:
                raise ValueError("aggregate proposal requires aggregations")
            if self.ranking or self.comparison_fields:
                raise ValueError("aggregate proposal contains another intent payload")
        else:
            raise ValueError("grounded planning lowers detail to search and forbids explain")
        return self


class GroundedPlanProvider(Protocol):
    @property
    def provider_name(self) -> str: ...

    @property
    def model_name(self) -> str: ...

    def generate_grounded_plan(
        self,
        question: str,
        question_id: str,
        product_family_hint: ProductFamily | None = None,
    ) -> GroundedPlanProposal: ...


class GroundedPlanRejectedError(ValueError):
    """Raised when a model proposal cannot be proven from the user question."""


def grounded_plan_is_eligible(decision: RouteDecision) -> bool:
    """Keep model planning outside hard safety and subjective-control routes."""

    if decision.disposition is RouteDisposition.EXECUTE:
        return True
    return (
        decision.disposition is RouteDisposition.CLARIFY
        and decision.reason_code in _ALLOWED_RESCUE_CONTROL_REASONS
    )


def grounded_plan_proposal_schema(
    product_families: Sequence[ProductFamily],
) -> dict[str, Any]:
    if not product_families:
        raise ValueError("grounded proposal schema requires at least one product family")
    schema = GroundedPlanProposal.model_json_schema()
    definitions = schema["$defs"]
    definitions["Intent"]["enum"] = [
        Intent.SEARCH.value,
        Intent.COMPARE.value,
        Intent.AGGREGATE.value,
    ]
    definitions["ProductFamily"]["enum"] = [family.value for family in product_families]
    registry = load_field_registry()

    def capable(capability: str, *, include_product_id: bool = False) -> list[str]:
        names = {
            name
            for name, definition in registry.fields.items()
            if name != "product_family"
            if any(
                family.value in definition.datasets
                and getattr(definition.resolve(family.value), capability)
                for family in product_families
            )
        }
        if include_product_id:
            names.add("product_id")
        return sorted(names)

    definitions["GroundedConstraintProposal"]["properties"]["field"]["enum"] = capable("queryable")
    definitions["GroundedRankingProposal"]["properties"]["field"]["enum"] = capable("sortable")
    definitions["GroundedFieldProposal"]["properties"]["field"]["enum"] = capable("selectable")
    definitions["GroundedAggregationProposal"]["properties"]["field"]["enum"] = capable(
        "aggregatable", include_product_id=True
    )
    return schema


def canonicalize_grounded_plan_proposal_payload(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Remove structurally powerless fields before validating a model proposal.

    Structured-output grammars cannot express all cross-field Pydantic invariants.
    These reductions only remove authority: they never add a condition, ranking,
    identity, or field that the model did not return.
    """

    normalized = json.loads(json.dumps(payload, ensure_ascii=False))
    constraints = normalized.get("constraints")
    if isinstance(constraints, list):
        normalized["constraints"] = [
            item
            for item in constraints
            if not isinstance(item, dict) or item.get("field") != "product_family"
        ]
    intent = normalized.get("intent")
    if intent == Intent.SEARCH.value:
        normalized["comparison_fields"] = []
        normalized["group_by"] = []
        normalized["aggregations"] = []
        if any(
            isinstance(item, dict) and item.get("field") in _IDENTITY_FIELDS
            for item in normalized.get("constraints", [])
        ):
            normalized["ranking"] = []
    elif intent == Intent.COMPARE.value:
        normalized["ranking"] = []
        normalized["group_by"] = []
        normalized["aggregations"] = []
    elif intent == Intent.AGGREGATE.value:
        normalized["ranking"] = []
        normalized["comparison_fields"] = []
    return normalized


def build_grounded_plan_system_prompt(
    question_id: str,
    field_catalog: Mapping[str, Any],
    product_families: Sequence[ProductFamily],
) -> str:
    families = ", ".join(family.value for family in product_families)
    catalog = json.dumps(
        field_catalog,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return f"""
당신은 금융상품 질문을 근거 첨부 실행 계획으로 바꾸는 parser다.
검색하거나 답변하지 말고 지정된 JSON 하나만 출력한다.
question_id는 {question_id!r}를 정확히 사용한다.
선택 가능한 상품군은 {families}다.

핵심 안전 규칙:
- 조건·정렬·비교 항목·집계마다 사용자의 원문에서 그대로 복사한 evidence_span을 붙인다.
- evidence_span은 질문에 실제로 연속해서 존재하는 문자열이어야 한다.
- 질문에 없는 조건, 숫자, 상품 코드, 정렬, 추천 의도를 추가하지 않는다.
- 숫자·퍼센트·상품 코드는 질문과 같은 값을 보존한다.
- 모호하거나 지원하지 않는 조건은 실행 조건으로 추정하지 말고 해당 원문 span과 이유를 기록한다.
- 예측·전망·수익 보장·단정적 매수 추천은 unsupported_conditions로 기록한다.

계획 규칙:
- 상세 조회는 intent=search로 낮추고 정확한 식별자 constraint를 만든다.
- 두 상품 비교는 intent=compare, 두 식별자와 명시된 comparison_fields를 모두 기록한다.
- 개수·평균·최댓값·분포 계산은 intent=aggregate와 aggregations/group_by로 기록한다.
- 상품군 표현을 family_evidence_span에 그대로 복사한다. 상품군이 생략되고 정확한 코드만
  있으면 그 코드 전체를 family_evidence_span으로 사용한다.
- ETF와 ETN은 product_type 값으로 구분한다. 공모펀드는 public_offering=true를 기록한다.
- 해외·국내 ETP의 현재 거래 가능은 sellable=true와 trading_suspended=false를 모두
  기록한다. 채권의 매수 가능만 currently_buyable=true를 사용한다.
- 공모펀드의 판매 가능·판매 중은 sellable=true를 사용한다.
- "상세 조회", "정보 조회", "조회해 줘"는 판매·거래 가능 조건이 아니다.
  판매/거래/매수 가능이 원문에 직접 쓰이지 않으면 해당 boolean을 절대 추가하지 않는다.
- 모든 constraint는 질문에 명시된 의미만 사용한다. 단위와 locked 여부는 서버가 확정한다.
- product_family는 top-level product_family로만 기록하고 constraint에는 절대 넣지 않는다.
- 장내·장외가 직접 쓰인 경우에만 bond_market을 기록한다. 국내채권은 bond_market이 아니다.
- 정렬 필드의 결측·UNKNOWN·0 제외를 constraint로 추가하지 않는다. "큰/높은 순"은
  ranking일 뿐이며 aum>0, 수익률>UNKNOWN 같은 품질 조건을 만들지 않는다.
- "판매 가능"만 쓰였으면 sellable=true만 기록하고 거래 중지 여부를 추정하지 않는다.
- "국내에서 거래되는 ETF"는 국내 상품군 표현일 뿐 sellable이나 trading_suspended 조건이 아니다.
- 비교에서는 ranking/group_by/aggregations를, 집계에서는 ranking/comparison_fields를 비워 둔다.
- exact ID 상세 조회에서는 ranking을 비워 둔다.
- 정렬 방향은 큰·높은·상위=desc, 낮은·작은·짧은=asc로 바꾼다.
- 결과 개수가 쓰였으면 그 숫자를 limit와 limit_evidence_span에 기록한다.
  쓰이지 않았으면 상세=1, 비교=2, 일반 검색=5, 집계=1을 쓰고 span은 빈 문자열로 둔다.
- 비교·집계 필드에도 각각 그 항목이 드러난 정확한 evidence_span을 붙인다.
- 내부 영문 field는 아래 catalog의 값만 사용하고 enum·operator 범위를 지킨다.

field catalog:
{catalog}

Markdown, 설명, 정답을 출력하지 않는다.
""".strip()


def _nfkc(value: str) -> str:
    return unicodedata.normalize("NFKC", value)


def _compact(value: str) -> str:
    return re.sub(r"[\s·_,:：()\[\]{}]+", "", _nfkc(value)).casefold()


def _span_occurs(question: str, span: str) -> bool:
    return _nfkc(span) in _nfkc(question)


def _evidence_is_negated(question: str, span: str) -> bool:
    """Reject a positive reading when its cited occurrence is locally negated.

    A free-form model can cite only the convenient noun inside a phrase such as
    ``ETF가 아닌``.  Verbatim presence alone therefore is not enough.  If the
    same short span occurs in both positive and negative contexts, the string
    evidence is ambiguous and the proposal fails closed.
    """

    normalized_question = _nfkc(question)
    normalized_span = _nfkc(span)
    if not normalized_span:
        return True
    internal_negation = re.compile(
        r"아닌|아니|아님|하지\s*않|지\s*않|없|"
        r"제외|말고|빼고"
    )
    if internal_negation.search(normalized_span) is not None:
        return True
    suffix_negation = re.compile(
        r"^\s*(?:가|이|은|는|을|를|이나|나)?\s*"
        r"(?:아닌|아니|아님|지\s*않|하지\s*않|"
        r"없|제외|말고|빼고|\b외\b)"
    )
    start = 0
    observed = False
    while True:
        index = normalized_question.find(normalized_span, start)
        if index < 0:
            break
        observed = True
        suffix = normalized_question[index + len(normalized_span) :][:20]
        if suffix_negation.search(suffix) is not None:
            return True
        start = index + max(len(normalized_span), 1)
    return not observed


def _values(value: ProposalValue) -> list[ProposalScalar]:
    return value if isinstance(value, list) else [value]


def _field_lexemes(field: str, definition: FieldDefinition) -> tuple[str, ...]:
    values = [definition.label, *definition.aliases, *_FIELD_LEXEMES.get(field, ())]
    return tuple(dict.fromkeys(item for item in values if item))


def _field_is_grounded(
    field: str,
    definition: FieldDefinition,
    span: str,
    value: ProposalValue | None = None,
) -> bool:
    compact_span = _compact(span)
    if field in _IDENTITY_FIELDS and value is not None:
        return all(_compact(str(item)) in compact_span for item in _values(value))
    return any(_compact(lexeme) in compact_span for lexeme in _field_lexemes(field, definition))


def _number_is_grounded(value: int | float, span: str) -> bool:
    observed = [float(item) for item in re.findall(r"(?<![A-Za-z0-9])\d+(?:\.\d+)?", span)]
    return any(abs(item - float(value)) < 1e-9 for item in observed)


def _boolean_is_grounded(field: str, value: bool, span: str) -> bool:
    # A positive keyword inside a negated phrase must not grant execution
    # authority.  For example, ``거래 가능하지 않은`` contains the
    # substring ``거래 가능`` but means the opposite.  The model may still
    # report the phrase as an ambiguity; it may not turn it into a boolean.
    contradictory_patterns: dict[tuple[str, bool], re.Pattern[str]] = {
        ("sellable", True): re.compile(
            r"(?:판매|거래).{0,10}가능.{0,8}(?:하지\s*않|아닌|없)|"
            r"(?:살|팔)\s*수\s*없"
        ),
        ("sellable", False): re.compile(r"판매.{0,8}(?:불가|완료).{0,8}(?:아님|아닌|아니)"),
        ("trading_suspended", True): re.compile(
            r"거래\s*(?:중지|정지|중단).{0,8}(?:아님|아닌|아니|되지\s*않)"
        ),
        ("trading_suspended", False): re.compile(r"거래.{0,10}가능.{0,8}(?:하지\s*않|아닌|없)"),
        ("pension_eligible", True): re.compile(r"연금.{0,12}(?:불가|할\s*수\s*없|가능하지\s*않)"),
        ("pension_eligible", False): re.compile(r"연금.{0,12}불가.{0,8}(?:아님|아닌|아니)"),
        ("currently_buyable", True): re.compile(
            r"(?:매수|구매).{0,10}(?:불가|가능하지\s*않)|"
            r"살\s*수\s*없"
        ),
        ("currently_buyable", False): re.compile(
            r"(?:매수|구매).{0,10}불가.{0,8}(?:아님|아닌|아니)"
        ),
        ("public_offering", True): re.compile(r"공모.{0,8}(?:아님|아닌|아니)"),
        ("public_offering", False): re.compile(r"사모.{0,8}(?:아님|아닌|아니)"),
    }
    contradictory = contradictory_patterns.get((field, value))
    if contradictory is not None and contradictory.search(span) is not None:
        return False
    patterns: dict[tuple[str, bool], re.Pattern[str]] = {
        ("sellable", True): re.compile(
            r"판매(?:가|이)?\s*가능|판매\s*중|살\s*수\s*있|팔\s*수\s*있|"
            r"거래.{0,8}(?:가능|할\s*수\s*있)"
        ),
        ("sellable", False): re.compile(r"판매\s*불가|판매\s*완료|팔\s*수\s*없"),
        ("trading_suspended", True): re.compile(r"거래\s*(?:중지|정지|중단)"),
        ("trading_suspended", False): re.compile(
            r"거래.{0,8}(?:가능|할\s*수\s*있)|"
            r"거래\s*(?:중지|정지).{0,8}(?:아님|아닌|아니)"
        ),
        ("pension_eligible", True): re.compile(r"연금.{0,12}(?:가능|살\s*수\s*있|거래)"),
        ("pension_eligible", False): re.compile(r"연금.{0,12}(?:불가|할\s*수\s*없)"),
        ("currently_buyable", True): re.compile(r"(?:매수|구매).{0,10}가능|살\s*수\s*있"),
        ("currently_buyable", False): re.compile(r"(?:매수|구매).{0,10}불가"),
        ("public_offering", True): re.compile(r"공모"),
        ("public_offering", False): re.compile(r"사모"),
    }
    pattern = patterns.get((field, value))
    return pattern is not None and pattern.search(span) is not None


def _value_is_grounded(field: str, value: ProposalValue, span: str) -> bool:
    for item in _values(value):
        if isinstance(item, bool):
            if not _boolean_is_grounded(field, item, span):
                return False
        elif isinstance(item, (int, float)):
            if not _number_is_grounded(item, span):
                return False
        else:
            aliases = (item, *_VALUE_ALIASES.get(item, ()))
            if not any(_compact(alias) in _compact(span) for alias in aliases):
                return False
    return True


def _constraint_is_grounded(
    question: str,
    proposal: GroundedConstraintProposal,
    definition: FieldDefinition,
) -> bool:
    if not _field_is_grounded(proposal.field, definition, proposal.evidence_span, proposal.value):
        return False
    if not _value_is_grounded(proposal.field, proposal.value, proposal.evidence_span):
        return False
    boolean_values = [item for item in _values(proposal.value) if isinstance(item, bool)]
    if proposal.operator in {ConstraintOperator.EQ, ConstraintOperator.IN}:
        positive_boolean = any(boolean_values)
        trading_available = (
            proposal.field == "trading_suspended"
            and boolean_values == [False]
            and re.search(r"거래.{0,8}가능", proposal.evidence_span) is not None
        )
        non_boolean = not boolean_values
        if (positive_boolean or trading_available or non_boolean) and _evidence_is_negated(
            question,
            proposal.evidence_span,
        ):
            return False
    pattern = _OPERATOR_PATTERNS.get(proposal.operator)
    return pattern is None or pattern.search(proposal.evidence_span) is not None


def _canonical_value(value: ProposalValue) -> object:
    if isinstance(value, list):
        return [_canonical_value(item) for item in value]
    return value


def _canonical_enum_value(
    family: ProductFamily,
    definition: FieldDefinition,
    value: ProposalValue,
) -> object:
    def canonicalize(item: ProposalScalar) -> ProposalScalar:
        if not isinstance(item, str) or not definition.enum_values:
            return item
        candidate = _FAMILY_ENUM_ALIASES.get(family, {}).get(item, item)
        if candidate not in definition.enum_values:
            raise GroundedPlanRejectedError(
                f"proposal value is outside the field enum: {definition.label}"
            )
        return candidate

    if isinstance(value, list):
        return [canonicalize(item) for item in value]
    return canonicalize(value)


def _constraint_key(field: str, operator: str, value: object) -> str:
    return json.dumps(
        {"field": field, "operator": operator, "value": value},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


class GroundedPlanGate:
    """Compile only model proposals whose semantics are provable from source spans."""

    def __init__(
        self,
        database_paths: Mapping[ProductFamily | str, str | Path],
        *,
        identity_cache: ProductIdentitySnapshotCache | None = None,
    ) -> None:
        self.database_paths = {
            ProductFamily(family): Path(path) for family, path in database_paths.items()
        }
        self.identity_cache = identity_cache or ProductIdentitySnapshotCache(max_entries=4)

    def compile(
        self,
        question: str,
        decision: RouteDecision,
        proposal: GroundedPlanProposal,
        *,
        trusted_plan: QueryPlan | None = None,
    ) -> QueryPlan:
        if proposal.question_id != decision.draft.request_id:
            raise GroundedPlanRejectedError("proposal question_id differs")
        if decision.disposition is RouteDisposition.UNSUPPORTED:
            raise GroundedPlanRejectedError("unsupported routes cannot be rescued")
        if (
            decision.disposition is RouteDisposition.CLARIFY
            and decision.reason_code not in _ALLOWED_RESCUE_CONTROL_REASONS
        ):
            raise GroundedPlanRejectedError("subjective or policy controls cannot be rescued")
        if proposal.ambiguities or proposal.unsupported_conditions:
            raise GroundedPlanRejectedError("proposal reports unresolved conditions")
        identity_values = self._identity_values(question, proposal)
        self._require_spans(
            question,
            proposal,
            allow_identity_family_override=bool(identity_values),
        )
        family, canonical_ids = self._resolve_family_and_identities(
            decision,
            proposal.product_family,
            identity_values,
        )
        self._require_intent(decision, proposal.intent, len(canonical_ids))
        constraints = self._compile_constraints(question, family, proposal, canonical_ids)
        rankings = self._compile_rankings(question, family, proposal)
        limit = self._compile_limit(question, decision, proposal, canonical_ids)
        comparison_fields = self._compile_fields(
            question,
            family,
            proposal.comparison_fields,
            capability="comparable",
        )
        group_by = self._compile_fields(
            question,
            family,
            proposal.group_by,
            capability="selectable",
        )
        aggregations = self._compile_aggregations(
            question,
            family,
            proposal.aggregations,
        )
        projection = self._projection(
            family,
            proposal.intent,
            comparison_fields,
            group_by,
            aggregations,
        )
        plan = QueryPlan(
            schema_version="1.0",
            question_id=decision.draft.request_id,
            intent=proposal.intent,
            product_families=[family],
            constraints=constraints,
            ranking=rankings,
            projection=projection,
            limit=limit,
            intent_payload=IntentPayload(
                comparison_fields=comparison_fields,
                group_by=group_by,
                aggregations=aggregations,
                explain_product_ids=[],
            ),
            ambiguities=[],
            unsupported_conditions=[],
        )
        self._require_trusted_anchors(question, plan)
        self._require_server_plan_anchors(question, trusted_plan, plan)
        return plan

    @staticmethod
    def _is_default_name_ranking(question: str, field: str) -> bool:
        return (
            field == "product_name"
            and re.search(
                r"(?:상품명|이름).{0,20}(?:순서|순으로|오름차순|내림차순|이름순)",
                question,
            )
            is None
        )

    def _require_server_plan_anchors(
        self,
        question: str,
        trusted_plan: QueryPlan | None,
        model_plan: QueryPlan,
    ) -> None:
        if trusted_plan is None:
            return
        if trusted_plan.intent is not model_plan.intent:
            raise GroundedPlanRejectedError("proposal changed the server plan intent")
        if trusted_plan.product_families != model_plan.product_families:
            raise GroundedPlanRejectedError("proposal changed the server plan family")
        model_identity_ids = {
            str(value)
            for item in model_plan.constraints
            if item.field == "product_id"
            for value in (item.value if isinstance(item.value, list) else [item.value])
        }
        trusted_constraints: set[str] = set()
        for item in trusted_plan.constraints:
            if item.field in _IDENTITY_FIELDS:
                raw_values = item.value if isinstance(item.value, list) else [item.value]
                resolved_ids = {
                    matches[0][1]
                    for value in raw_values
                    if isinstance(value, str) and len(matches := self._identity_matches(value)) == 1
                }
                if len(resolved_ids) == len(raw_values) and resolved_ids.issubset(
                    model_identity_ids
                ):
                    continue
            trusted_constraints.add(_constraint_key(item.field, item.operator.value, item.value))
        model_constraints = {
            _constraint_key(item.field, item.operator.value, item.value)
            for item in model_plan.constraints
        }
        if not trusted_constraints.issubset(model_constraints):
            raise GroundedPlanRejectedError("proposal omitted a server-compiled constraint")
        trusted_rankings = {
            (item.field, item.direction.value)
            for item in trusted_plan.ranking
            if not self._is_default_name_ranking(question, item.field)
        }
        model_rankings = {(item.field, item.direction.value) for item in model_plan.ranking}
        if not trusted_rankings.issubset(model_rankings):
            raise GroundedPlanRejectedError("proposal omitted a server-compiled ranking")
        trusted_payload = trusted_plan.intent_payload
        model_payload = model_plan.intent_payload
        if not set(trusted_payload.comparison_fields).issubset(model_payload.comparison_fields):
            raise GroundedPlanRejectedError("proposal omitted a server comparison field")
        if not set(trusted_payload.group_by).issubset(model_payload.group_by):
            raise GroundedPlanRejectedError("proposal omitted a server group-by field")
        trusted_aggregations = {
            (item.function.value, item.field) for item in trusted_payload.aggregations
        }
        model_aggregations = {
            (item.function.value, item.field) for item in model_payload.aggregations
        }
        if not trusted_aggregations.issubset(model_aggregations):
            raise GroundedPlanRejectedError("proposal omitted a server aggregation")

    @staticmethod
    def _require_spans(
        question: str,
        proposal: GroundedPlanProposal,
        *,
        allow_identity_family_override: bool,
    ) -> None:
        spans = [
            *(item.evidence_span for item in proposal.constraints),
            *(item.evidence_span for item in proposal.ranking),
            *(item.evidence_span for item in proposal.comparison_fields),
            *(item.evidence_span for item in proposal.group_by),
            *(item.evidence_span for item in proposal.aggregations),
            *(item.span for item in proposal.ambiguities),
            *(item.span for item in proposal.unsupported_conditions),
        ]
        if not allow_identity_family_override:
            spans.insert(0, proposal.family_evidence_span)
        if proposal.limit_evidence_span:
            spans.append(proposal.limit_evidence_span)
        missing = [span for span in spans if not _span_occurs(question, span)]
        if missing:
            raise GroundedPlanRejectedError(f"proposal evidence span is not verbatim: {missing}")

    def _identity_values(
        self,
        question: str,
        proposal: GroundedPlanProposal,
    ) -> list[str]:
        values: list[str] = []
        for constraint in proposal.constraints:
            if constraint.field not in _IDENTITY_FIELDS:
                continue
            if constraint.operator not in {
                ConstraintOperator.EQ,
                ConstraintOperator.IN,
            }:
                raise GroundedPlanRejectedError("identity proposal operator must be eq or in")
            if _evidence_is_negated(question, constraint.evidence_span):
                raise GroundedPlanRejectedError("identity evidence is negated or ambiguous")
            proposed_values = _values(constraint.value)
            if not all(isinstance(value, str) for value in proposed_values):
                raise GroundedPlanRejectedError("identity constraints require text values")

            # The dataset may repair a truncated or mislabeled model value, but
            # it must never supply an identifier that the user did not type.
            # Only exact tokens copied into the evidence span can acquire
            # authority.  This blocks a model from selecting any real product
            # merely because that product happens to exist in the database.
            grounded_values = list(
                dict.fromkeys(
                    token
                    for token in _IDENTITY_CANDIDATE.findall(constraint.evidence_span)
                    if len(self._identity_matches(token)) == 1
                )
            )
            if _IDENTITY_CUE.search(constraint.evidence_span) is None:
                proposed_casefold = {str(value).casefold() for value in proposed_values}
                grounded_values = [
                    token
                    for token in grounded_values
                    if token.upper() not in _RESERVED_UNCUED_IDENTITIES
                    and token.casefold() in proposed_casefold
                ]
            expected_count = len(proposed_values)
            if len(grounded_values) != expected_count:
                raise GroundedPlanRejectedError(
                    "identity evidence does not contain the proposed number of "
                    "unique dataset identifiers"
                )
            for value in grounded_values:
                if value.casefold() not in {item.casefold() for item in values}:
                    values.append(value)
        return values

    def _identity_matches(self, value: str) -> list[tuple[ProductFamily, str]]:
        normalized = value.casefold()
        matches: list[tuple[ProductFamily, str]] = []
        for family, path in self.database_paths.items():
            for record in self.identity_cache.get(path).records:
                fields = (record.product_id, record.ticker, record.isin)
                if any(item is not None and item.casefold() == normalized for item in fields):
                    match = (family, record.product_id)
                    if match not in matches:
                        matches.append(match)
        return matches

    def _resolve_family_and_identities(
        self,
        decision: RouteDecision,
        proposed_family: ProductFamily,
        identity_values: Sequence[str],
    ) -> tuple[ProductFamily, list[str]]:
        routed_families = decision.draft.product_families
        if len(routed_families) > 1:
            raise GroundedPlanRejectedError("multi-family proposals are not supported")
        canonical_ids: list[str] = []
        identity_families: set[ProductFamily] = set()
        for value in identity_values:
            matches = self._identity_matches(value)
            if len(matches) != 1:
                raise GroundedPlanRejectedError(
                    f"identity does not resolve uniquely across datasets: {value}"
                )
            family, product_id = matches[0]
            identity_families.add(family)
            if product_id not in canonical_ids:
                canonical_ids.append(product_id)
        if len(identity_families) > 1:
            raise GroundedPlanRejectedError("identities resolve to different product families")
        identity_family = next(iter(identity_families), None)
        if routed_families:
            family = routed_families[0]
            if family is not proposed_family:
                raise GroundedPlanRejectedError("model family differs from explicit route family")
        elif identity_family is not None:
            family = identity_family
        else:
            raise GroundedPlanRejectedError("family lacks explicit or exact-identity grounding")
        if identity_family is not None and identity_family is not family:
            raise GroundedPlanRejectedError("identity family differs from routed family")
        return family, canonical_ids

    @staticmethod
    def _require_intent(
        decision: RouteDecision,
        proposed_intent: Intent,
        identity_count: int,
    ) -> None:
        if proposed_intent is Intent.COMPARE and identity_count != 2:
            raise GroundedPlanRejectedError("compare requires two exact resolved identities")
        if (
            decision.draft.intent in {InteractionIntent.DETAIL, InteractionIntent.EXPLAIN}
            and identity_count != 1
        ):
            raise GroundedPlanRejectedError("detail or explain requires one exact identity")
        expected = {
            InteractionIntent.SEARCH: Intent.SEARCH,
            InteractionIntent.DETAIL: Intent.SEARCH,
            InteractionIntent.EXPLAIN: Intent.SEARCH,
            InteractionIntent.COMPARE: Intent.COMPARE,
            InteractionIntent.AGGREGATE: Intent.AGGREGATE,
        }.get(decision.draft.intent)
        implicit_compare = (
            proposed_intent is Intent.COMPARE
            and identity_count == 2
            and re.search(
                r"비교|차이|대조|둘\s*중|어느|어떤\s*게|\bvs\b",
                decision.draft.question,
                re.IGNORECASE,
            )
            is not None
        )
        if proposed_intent is not expected and not implicit_compare:
            raise GroundedPlanRejectedError("model intent differs from grounded route intent")

    def _compile_constraints(
        self,
        question: str,
        family: ProductFamily,
        proposal: GroundedPlanProposal,
        canonical_ids: Sequence[str],
    ) -> list[Constraint]:
        registry = load_field_registry()
        constraints: list[Constraint] = []
        for item in proposal.constraints:
            if item.field in _IDENTITY_FIELDS:
                continue
            if (
                family is ProductFamily.FUND
                and item.field == "public_offering"
                and not (item.operator is ConstraintOperator.EQ and item.value is True)
            ):
                raise GroundedPlanRejectedError(
                    "fund execution scope requires public_offering=true"
                )
            definition = registry.require_field(item.field, [family.value])
            if not definition.queryable or item.operator.value not in definition.allowed_operators:
                raise GroundedPlanRejectedError(f"unsupported proposal constraint: {item.field}")
            if not _constraint_is_grounded(question, item, definition):
                raise GroundedPlanRejectedError(f"constraint lacks lexical grounding: {item.field}")
            constraints.append(
                Constraint(
                    field=item.field,
                    operator=item.operator,
                    value=_canonical_enum_value(family, definition, item.value),  # type: ignore[arg-type]
                    unit=Unit(definition.unit),
                    strength=ConstraintStrength.LOCKED,
                )
            )

        if canonical_ids:
            operator = ConstraintOperator.EQ if len(canonical_ids) == 1 else ConstraintOperator.IN
            value: str | list[str] = (
                canonical_ids[0] if len(canonical_ids) == 1 else list(canonical_ids)
            )
            constraints.append(
                Constraint(
                    field="product_id",
                    operator=operator,
                    value=value,
                    unit=Unit.CODE,
                    strength=ConstraintStrength.LOCKED,
                )
            )
        if family is ProductFamily.FUND and not any(
            item.field == "public_offering" for item in constraints
        ):
            if "공모" not in question or _evidence_is_negated(question, "공모"):
                raise GroundedPlanRejectedError("fund proposal lacks public-offering grounding")
            constraints.insert(
                0,
                Constraint(
                    field="public_offering",
                    operator=ConstraintOperator.EQ,
                    value=True,
                    unit=Unit.BOOLEAN,
                    strength=ConstraintStrength.LOCKED,
                ),
            )
        return self._dedupe_constraints(constraints)

    @staticmethod
    def _dedupe_constraints(constraints: Sequence[Constraint]) -> list[Constraint]:
        observed: dict[str, Constraint] = {}
        fields: dict[str, set[str]] = {}
        for constraint in constraints:
            key = _constraint_key(
                constraint.field,
                constraint.operator.value,
                constraint.value,
            )
            observed.setdefault(key, constraint)
            fields.setdefault(constraint.field, set()).add(key)
        for field, keys in fields.items():
            equality_keys = [
                key
                for key in keys
                if observed[key].operator in {ConstraintOperator.EQ, ConstraintOperator.IN}
            ]
            if len(equality_keys) > 1:
                raise GroundedPlanRejectedError(f"conflicting grounded constraints: {field}")
        return list(observed.values())

    def _compile_rankings(
        self,
        question: str,
        family: ProductFamily,
        proposal: GroundedPlanProposal,
    ) -> list[Ranking]:
        registry = load_field_registry()
        rankings: list[Ranking] = []
        for item in proposal.ranking:
            definition = registry.require_field(item.field, [family.value])
            if not definition.sortable:
                raise GroundedPlanRejectedError(f"field is not sortable: {item.field}")
            if _evidence_is_negated(question, item.evidence_span):
                raise GroundedPlanRejectedError(
                    f"ranking evidence is negated or ambiguous: {item.field}"
                )
            if not _field_is_grounded(item.field, definition, item.evidence_span):
                raise GroundedPlanRejectedError(f"ranking field lacks grounding: {item.field}")
            if _DIRECTION_PATTERNS[item.direction].search(item.evidence_span) is None:
                raise GroundedPlanRejectedError(f"ranking direction lacks grounding: {item.field}")
            rankings.append(
                Ranking(field=item.field, direction=item.direction, nulls=NullPlacement.LAST)
            )
        if len({item.field for item in rankings}) != len(rankings):
            raise GroundedPlanRejectedError("duplicate grounded ranking field")
        return rankings

    @staticmethod
    def _compile_limit(
        question: str,
        decision: RouteDecision,
        proposal: GroundedPlanProposal,
        canonical_ids: Sequence[str],
    ) -> int:
        if proposal.limit_evidence_span:
            if _evidence_is_negated(question, proposal.limit_evidence_span):
                raise GroundedPlanRejectedError("limit evidence is negated or ambiguous")
            if not _number_is_grounded(proposal.limit, proposal.limit_evidence_span):
                raise GroundedPlanRejectedError("limit evidence does not contain the limit")
            if decision.draft.requested_limit is not None and (
                proposal.limit != decision.draft.requested_limit
            ):
                raise GroundedPlanRejectedError("model limit differs from routed explicit limit")
            return proposal.limit
        if decision.draft.requested_limit is not None:
            raise GroundedPlanRejectedError("explicit route limit lacks proposal evidence")
        if proposal.intent is Intent.COMPARE:
            return max(len(canonical_ids), 2)
        if proposal.intent is Intent.AGGREGATE:
            return 100 if proposal.group_by else 1
        if canonical_ids:
            return 1
        return 5

    @staticmethod
    def _compile_fields(
        question: str,
        family: ProductFamily,
        proposals: Sequence[GroundedFieldProposal],
        *,
        capability: Literal["comparable", "selectable"],
    ) -> list[str]:
        registry = load_field_registry()
        fields: list[str] = []
        for item in proposals:
            definition = registry.require_field(item.field, [family.value])
            if not getattr(definition, capability):
                raise GroundedPlanRejectedError(
                    f"field lacks {capability} capability: {item.field}"
                )
            if _evidence_is_negated(question, item.evidence_span):
                raise GroundedPlanRejectedError(
                    f"payload field evidence is negated or ambiguous: {item.field}"
                )
            if not _field_is_grounded(item.field, definition, item.evidence_span):
                raise GroundedPlanRejectedError(f"payload field lacks grounding: {item.field}")
            if item.field not in fields:
                fields.append(item.field)
        return fields

    @staticmethod
    def _compile_aggregations(
        question: str,
        family: ProductFamily,
        proposals: Sequence[GroundedAggregationProposal],
    ) -> list[Aggregation]:
        registry = load_field_registry()
        aggregations: list[Aggregation] = []
        for item in proposals:
            definition = registry.require_field(item.field, [family.value])
            if item.function is not AggregateFunction.COUNT and not definition.aggregatable:
                raise GroundedPlanRejectedError(f"field is not aggregatable: {item.field}")
            if item.function is AggregateFunction.COUNT and not definition.selectable:
                raise GroundedPlanRejectedError(f"field cannot be counted: {item.field}")
            if _evidence_is_negated(question, item.evidence_span):
                raise GroundedPlanRejectedError(
                    "aggregation evidence is negated or ambiguous: "
                    f"{item.function.value}:{item.field}"
                )
            field_grounded = item.field == "product_id" and item.function is AggregateFunction.COUNT
            if not field_grounded and not _field_is_grounded(
                item.field,
                definition,
                item.evidence_span,
            ):
                raise GroundedPlanRejectedError(f"aggregation field lacks grounding: {item.field}")
            if _FUNCTION_PATTERNS[item.function].search(item.evidence_span) is None:
                raise GroundedPlanRejectedError(
                    f"aggregation function lacks grounding: {item.function.value}"
                )
            aggregation = Aggregation(function=item.function, field=item.field)
            if aggregation not in aggregations:
                aggregations.append(aggregation)
        return aggregations

    @staticmethod
    def _projection(
        family: ProductFamily,
        intent: Intent,
        comparison_fields: Sequence[str],
        group_by: Sequence[str],
        aggregations: Sequence[Aggregation],
    ) -> list[str]:
        if intent is Intent.SEARCH:
            return list(SEARCH_PROJECTION_BY_FAMILY[family.value])
        if intent is Intent.COMPARE:
            return comparison_projection(family, comparison_fields)
        return list(
            dict.fromkeys(
                [
                    "product_id",
                    *group_by,
                    *(aggregation.field for aggregation in aggregations),
                ]
            )
        )

    def _require_trusted_anchors(self, question: str, plan: QueryPlan) -> None:
        payload = canonicalize_query_plan_payload(
            question,
            {
                "question_id": plan.question_id,
                "product_families": [plan.product_families[0].value],
            },
            force_product_family_hint=True,
        )
        if payload["unsupported_conditions"] or payload["ambiguities"]:
            raise GroundedPlanRejectedError("trusted linker found a blocked condition")
        trusted_keys: set[str] = set()
        plan_identity_ids = {
            str(value)
            for constraint in plan.constraints
            if constraint.field == "product_id"
            for value in (
                constraint.value if isinstance(constraint.value, list) else [constraint.value]
            )
        }
        for item in payload["constraints"]:
            if item["field"] in _IDENTITY_FIELDS:
                raw_values = item["value"] if isinstance(item["value"], list) else [item["value"]]
                resolved_ids = {
                    matches[0][1]
                    for value in raw_values
                    if isinstance(value, str) and len(matches := self._identity_matches(value)) == 1
                }
                if len(resolved_ids) == len(raw_values) and resolved_ids.issubset(
                    plan_identity_ids
                ):
                    continue
            trusted_keys.add(_constraint_key(item["field"], item["operator"], item["value"]))
        plan_keys = {
            _constraint_key(item.field, item.operator.value, item.value)
            for item in plan.constraints
        }
        missing = trusted_keys - plan_keys
        if missing:
            raise GroundedPlanRejectedError(
                f"proposal omitted trusted constraints: {sorted(missing)}"
            )
        if plan.intent is Intent.SEARCH and payload["ranking"]:
            trusted_rankings = {
                (item["field"], item["direction"])
                for item in payload["ranking"]
                if not self._is_default_name_ranking(question, item["field"])
            }
            plan_rankings = {(item.field, item.direction.value) for item in plan.ranking}
            if not trusted_rankings.issubset(plan_rankings):
                raise GroundedPlanRejectedError("proposal omitted a trusted ranking")
