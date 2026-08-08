from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from collections import Counter
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from difflib import SequenceMatcher
from typing import Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, model_validator

from finance_agent_core.agent.providers import (
    LocalProviderError,
    LocalTestProvider,
    LocalTestSettings,
)
from finance_agent_core.config import load_field_registry
from finance_agent_core.contracts.queryplan import (
    AggregateFunction,
    Constraint,
    ConstraintOperator,
    Intent,
    ProductFamily,
    QueryPlan,
    SortDirection,
    Unit,
)
from finance_agent_core.evaluation.metamorphic import (
    SEMANTIC_ROUNDTRIP_AXES,
    GeneratedMutation,
    GeneratedMutationSet,
    MutationAxis,
    MutationBatch,
    MutationCandidate,
    MutationValidation,
    _product_family_present,
)
from finance_agent_core.evaluation.official_mock import (
    OfficialMockCase,
    load_official_mock_suite,
)

_RESOURCE_NAME = "semantic_roundtrip_v1.json"
_SCREEN_VERSION = "semantic-screen-v3"
_CODE_FENCE_OR_JSON = re.compile(r"```|^\s*[\[{]", re.MULTILINE)
_INTERNAL_JARGON = re.compile(
    r"QueryPlan|query[_ ]?plan|question_id|product_famil(?:y|ies)|"
    r"comparison_fields|group_by|aggregations|(?:^|\W)(?:locked|lte|gte|neq)(?:$|\W)|"
    r"[a-z]+_[a-z_]+",
    re.IGNORECASE,
)
_UNSAFE_ADDED_INTENT = re.compile(r"예측|전망|추천|수익\s*보장|무조건\s*(?:사|매수)")
_NUMBER = re.compile(r"(?<![A-Za-z0-9])\d+(?:\.\d+)?")
_IDENTIFIER = re.compile(r"^(?:KR[A-Z0-9]{10}|[0-9]{3}:[A-Z0-9._:-]+|[A-Z]+[.:][A-Z0-9._:-]+)$")

_FAMILY_LABELS = {
    ProductFamily.BOND: "국내채권",
    ProductFamily.DOMESTIC_ETP: "국내 ETF·ETN",
    ProductFamily.OVERSEAS_ETP: "해외 ETF·ETN",
    ProductFamily.FUND: "공모펀드",
}
_OPERATOR_LABELS = {
    ConstraintOperator.EQ: "같음",
    ConstraintOperator.NEQ: "같지 않음",
    ConstraintOperator.IN: "목록에 포함",
    ConstraintOperator.NOT_IN: "목록에서 제외",
    ConstraintOperator.LT: "미만",
    ConstraintOperator.LTE: "이하",
    ConstraintOperator.GT: "초과",
    ConstraintOperator.GTE: "이상",
    ConstraintOperator.BETWEEN: "범위 안",
    ConstraintOperator.CONTAINS: "포함",
}
_FUNCTION_LABELS = {
    AggregateFunction.COUNT: "개수",
    AggregateFunction.MIN: "최솟값",
    AggregateFunction.MAX: "최댓값",
    AggregateFunction.AVG: "평균",
    AggregateFunction.SUM: "합계",
}
_DIRECTION_LABELS = {
    SortDirection.ASC: "낮거나 작은 값부터",
    SortDirection.DESC: "높거나 큰 값부터",
}
_VALUE_LABELS: dict[object, str] = {
    "United States of America": "미국",
    "Bond": "채권",
    "KRW": "원화(KRW)",
}
_FIELD_CONCEPTS: dict[str, re.Pattern[str]] = {
    "product_type": re.compile(r"ETF|ETN|ETP", re.IGNORECASE),
    "sellable": re.compile(
        r"(?:(?:판매|거래).{0,12}(?:가능|중|할\s*수\s*있)|살\s*수\s*있)",
        re.IGNORECASE,
    ),
    "pension_eligible": re.compile(r"연금", re.IGNORECASE),
    "currently_buyable": re.compile(r"(?:매수|구매|살).{0,12}(?:가능|중|수\s*있)", re.IGNORECASE),
    "investment_region": re.compile(r"미국|지역|국가", re.IGNORECASE),
    "asset_type": re.compile(r"주식\s*형?|채권\s*형?|자산", re.IGNORECASE),
    "total_expense_ratio_pct": re.compile(r"총\s*보수(?:\s*율)?|보수\s*율|expense", re.IGNORECASE),
    "aum": re.compile(r"AUM|순자산|운용\s*자산", re.IGNORECASE),
    "one_month_return_pct": re.compile(
        r"1\s*개월\s*수익률|한\s*달.{0,8}수익률|월간\s*수익률|1M", re.IGNORECASE
    ),
    "remaining_days": re.compile(r"잔존\s*일수?|만기까지\s*남은", re.IGNORECASE),
    "fund_geography_scope": re.compile(r"해외|국내외", re.IGNORECASE),
    "fund_management_attribute": re.compile(r"주식\s*형|펀드\s*유형|운용\s*속성", re.IGNORECASE),
    "three_month_return_pct": re.compile(r"3\s*개월\s*수익률|3M", re.IGNORECASE),
    "trading_currency": re.compile(r"원화|KRW|통화", re.IGNORECASE),
    "trading_suspended": re.compile(
        r"거래.{0,12}(?:가능|중지|정지|중단|할\s*수\s*있)", re.IGNORECASE
    ),
    "buy_yield_pct": re.compile(r"매수\s*수익률", re.IGNORECASE),
    "coupon_rate_pct": re.compile(r"표면\s*이율|표면\s*금리|쿠폰\s*금리", re.IGNORECASE),
    "close_price": re.compile(r"종가|마감\s*가격", re.IGNORECASE),
    "risk_level": re.compile(r"위험\s*등급|위험도", re.IGNORECASE),
}


class SemanticRoundtripModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class SemanticRoundtripProtocol(SemanticRoundtripModel):
    schema_version: Literal["1.0"]
    protocol_id: Literal["semantic-roundtrip-v1"]
    protocol_version: Literal["1.0"]
    status: Literal["internal_development_not_blind"]
    source_suite_id: Literal["official-mock-v1-30"]
    source_suite_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_question_hidden_from_generator: Literal[True]
    source_case_ids: list[str] = Field(min_length=1, max_length=30)
    excluded_case_ids: dict[str, str] = Field(min_length=1, max_length=30)
    axes: list[MutationAxis] = Field(min_length=3, max_length=3)
    interpretation_limits: list[str] = Field(min_length=1, max_length=20)

    @model_validator(mode="after")
    def validate_protocol(self) -> SemanticRoundtripProtocol:
        if self.axes != list(SEMANTIC_ROUNDTRIP_AXES):
            raise ValueError("semantic roundtrip axes differ from the frozen protocol")
        if len(self.source_case_ids) != len(set(self.source_case_ids)):
            raise ValueError("source_case_ids must be unique")
        if set(self.source_case_ids) & set(self.excluded_case_ids):
            raise ValueError("included and excluded source case IDs overlap")
        return self


class LoadedSemanticRoundtripProtocol(SemanticRoundtripModel):
    protocol: SemanticRoundtripProtocol
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    resource_name: str


class SemanticConditionSpec(SemanticRoundtripModel):
    field_name: str
    label: str
    aliases: list[str]
    operator: str
    value: str | list[str]


class SemanticRankingSpec(SemanticRoundtripModel):
    field_name: str
    label: str
    aliases: list[str]
    direction: str


class SemanticAggregationSpec(SemanticRoundtripModel):
    function: AggregateFunction
    function_label: str
    field_name: str
    field_label: str


class SemanticPlanSpec(SemanticRoundtripModel):
    product_family: ProductFamily
    product_family_label: str
    intent: Intent
    request_kind: str
    conditions: list[SemanticConditionSpec]
    ranking: list[SemanticRankingSpec]
    result_limit: int | None
    comparison_fields: list[SemanticRankingSpec]
    group_by: list[SemanticRankingSpec]
    aggregations: list[SemanticAggregationSpec]


class SemanticQuestionProvider(Protocol):
    @property
    def provider_name(self) -> Literal["expected", "local_test"]: ...

    @property
    def model_name(self) -> str | None: ...

    def generate_questions(
        self,
        spec: SemanticPlanSpec,
        axes: Sequence[MutationAxis],
    ) -> list[GeneratedMutation]: ...


class RoutedPlanService(Protocol):
    def answer(self, question: str, request_id: str): ...


def _sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _normalized_text(value: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", value).split()).casefold()


def _field_words(field_name: str, family: ProductFamily) -> tuple[str, list[str]]:
    definition = load_field_registry().require_field(field_name, [family.value])
    aliases = [alias for alias in definition.aliases if re.search(r"[가-힣]", alias)]
    return definition.label, list(dict.fromkeys(aliases))[:4]


def _render_value(constraint: Constraint) -> str | list[str]:
    values = constraint.value if isinstance(constraint.value, list) else [constraint.value]
    rendered: list[str] = []
    for value in values:
        if isinstance(value, bool):
            if constraint.field == "sellable":
                rendered.append("판매 가능" if value else "판매 불가")
            elif constraint.field == "pension_eligible":
                rendered.append("연금 거래 가능" if value else "연금 거래 불가")
            elif constraint.field == "currently_buyable":
                rendered.append("현재 매수 가능" if value else "현재 매수 불가")
            elif constraint.field == "trading_suspended":
                rendered.append("거래 중지" if value else "거래 중지 아님")
            elif constraint.field == "public_offering":
                rendered.append("공모" if value else "사모")
            else:
                rendered.append("예" if value else "아니오")
        elif value in _VALUE_LABELS:
            rendered.append(_VALUE_LABELS[value])
        elif constraint.unit is Unit.PCT_POINT:
            rendered.append(f"{value}%")
        else:
            rendered.append(str(value))
    return rendered if isinstance(constraint.value, list) else rendered[0]


def _field_spec(field_name: str, family: ProductFamily) -> SemanticRankingSpec:
    label, aliases = _field_words(field_name, family)
    return SemanticRankingSpec(
        field_name=field_name,
        label=label,
        aliases=aliases,
        direction="",
    )


def build_semantic_plan_spec(plan: QueryPlan) -> SemanticPlanSpec:
    if len(plan.product_families) != 1:
        raise ValueError("semantic roundtrip requires one product family per QueryPlan")
    family = plan.product_families[0]
    conditions: list[SemanticConditionSpec] = []
    identifier_lookup = False
    for constraint in plan.constraints:
        label, aliases = _field_words(constraint.field, family)
        conditions.append(
            SemanticConditionSpec(
                field_name=constraint.field,
                label=label,
                aliases=aliases,
                operator=_OPERATOR_LABELS[constraint.operator],
                value=_render_value(constraint),
            )
        )
        identifier_lookup = identifier_lookup or (
            constraint.field in {"product_id", "ticker"}
            and constraint.operator in {ConstraintOperator.EQ, ConstraintOperator.IN}
        )

    rankings: list[SemanticRankingSpec] = []
    for ranking in plan.ranking:
        label, aliases = _field_words(ranking.field, family)
        rankings.append(
            SemanticRankingSpec(
                field_name=ranking.field,
                label=label,
                aliases=aliases,
                direction=_DIRECTION_LABELS[ranking.direction],
            )
        )

    comparison_fields = [
        _field_spec(field_name, family) for field_name in plan.intent_payload.comparison_fields
    ]
    group_by = [_field_spec(field_name, family) for field_name in plan.intent_payload.group_by]
    aggregations = []
    for aggregation in plan.intent_payload.aggregations:
        label, _ = _field_words(aggregation.field, family)
        aggregations.append(
            SemanticAggregationSpec(
                function=aggregation.function,
                function_label=_FUNCTION_LABELS[aggregation.function],
                field_name=aggregation.field,
                field_label=label,
            )
        )

    if plan.intent is Intent.COMPARE:
        request_kind = "두 상품 비교"
    elif plan.intent is Intent.AGGREGATE:
        request_kind = "정확한 집계와 계산"
    elif identifier_lookup:
        request_kind = "식별자가 정확히 일치하는 상품 상세 조회"
    else:
        request_kind = "조건 검색"
    result_limit = plan.limit if plan.intent is Intent.SEARCH and not identifier_lookup else None
    return SemanticPlanSpec(
        product_family=family,
        product_family_label=_FAMILY_LABELS[family],
        intent=plan.intent,
        request_kind=request_kind,
        conditions=conditions,
        ranking=rankings,
        result_limit=result_limit,
        comparison_fields=comparison_fields,
        group_by=group_by,
        aggregations=aggregations,
    )


def _prompt_payload(spec: SemanticPlanSpec) -> dict[str, Any]:
    def field_payload(field: SemanticRankingSpec) -> dict[str, Any]:
        return {
            "표현할_항목": field.label,
            "자연어_표현_힌트": field.aliases,
            **({"정렬": field.direction} if field.direction else {}),
        }

    return {
        "상품군": spec.product_family_label,
        "요청_종류": spec.request_kind,
        "조건": [
            {
                "표현할_항목": item.label,
                "자연어_표현_힌트": item.aliases,
                "관계": item.operator,
                "값": item.value,
            }
            for item in spec.conditions
        ],
        "정렬": [field_payload(item) for item in spec.ranking],
        "결과_개수": spec.result_limit,
        "비교할_항목": [field_payload(item) for item in spec.comparison_fields],
        "묶을_기준": [field_payload(item) for item in spec.group_by],
        "계산": [
            {"함수": item.function_label, "대상": item.field_label} for item in spec.aggregations
        ],
    }


def build_semantic_roundtrip_system_prompt(spec: SemanticPlanSpec) -> str:
    semantic_payload = json.dumps(_prompt_payload(spec), ensure_ascii=False, indent=2)
    return f"""
당신은 금융상품 Agent의 개발 전용 질문 작성자다.
아래 의미 명세만 사용해 서로 다른 한국어 사용자 질문 3개를 만든다.
원래 질문은 제공되지 않았으며, 검색이나 답변은 하지 않는다.

의미 명세:
{semantic_payload}

반드시 지킬 규칙:
- 조건, 값, 비교 대상, 정렬 방향, 결과 개수, 집계 함수를 빠짐없이 정확히 표현한다.
- 상품 코드·숫자·퍼센트는 명세와 정확히 같은 값을 쓴다.
- 예측, 전망, 투자 추천, 새 조건, 새 답변 형식을 추가하지 않는다.
- QueryPlan, 영문 내부 필드명, JSON 구조 같은 구현 용어를 질문에 쓰지 않는다.
- 각 질문은 한 개의 사용자 요청이며 답이나 해설을 포함하지 않는다.

스타일:
- semantic_formal: 정중하고 완전한 문장의 실무형 질문
- semantic_colloquial: 일반 사용자가 대화하듯 묻는 자연스러운 질문
- semantic_telegraphic: 모바일 검색창에 쓰듯 짧지만 모든 조건이 남은 질문

각 스타일을 정확히 한 번 사용하고 axis는 지정된 영문 값을 그대로 쓴다.
Markdown이나 부가 설명 없이 지정된 JSON만 출력한다.
""".strip()


def _question_schema(axes: Sequence[MutationAxis]) -> dict[str, Any]:
    values = [axis.value for axis in axes]
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["variants"],
        "properties": {
            "variants": {
                "type": "array",
                "minItems": len(values),
                "maxItems": len(values),
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["axis", "question"],
                    "properties": {
                        "axis": {"type": "string", "enum": values},
                        "question": {"type": "string", "minLength": 1, "maxLength": 3000},
                    },
                },
            }
        },
    }


class LocalQwenSemanticQuestionProvider:
    """Generate new questions from semantics without exposing the source wording."""

    def __init__(self, settings: LocalTestSettings) -> None:
        self.settings = settings
        self._client = LocalTestProvider(settings)

    @property
    def provider_name(self) -> Literal["local_test"]:
        return "local_test"

    @property
    def model_name(self) -> str:
        return self.settings.model

    def healthcheck(self) -> dict[str, Any]:
        return self._client.healthcheck()

    def generate_questions(
        self,
        spec: SemanticPlanSpec,
        axes: Sequence[MutationAxis],
    ) -> list[GeneratedMutation]:
        if tuple(axes) != SEMANTIC_ROUNDTRIP_AXES:
            raise ValueError("semantic roundtrip requires the three frozen axes")
        payload = {
            "model": self.settings.model,
            "messages": [
                {"role": "system", "content": build_semantic_roundtrip_system_prompt(spec)},
                {
                    "role": "user",
                    "content": "의미 명세를 보존한 세 가지 새 질문을 작성해 주세요.",
                },
            ],
            "temperature": 0.8,
            "seed": 71,
            "max_tokens": 2048,
            "response_format": {
                "type": "json_schema",
                "json_schema": {
                    "name": "semantic_roundtrip_questions",
                    "strict": True,
                    "schema": _question_schema(axes),
                },
            },
        }
        response = self._client._request_json("chat/completions", payload)
        try:
            content = response["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as error:
            raise LocalProviderError("local semantic response has an unexpected shape") from error
        if not isinstance(content, str):
            raise LocalProviderError("local semantic response content is not text")
        try:
            generated = GeneratedMutationSet.model_validate_json(content)
        except ValueError as error:
            raise LocalProviderError("local model returned invalid semantic questions") from error
        expected_axes = Counter(axes)
        observed_axes = Counter(item.axis for item in generated.variants)
        if observed_axes != expected_axes:
            raise LocalProviderError(
                f"local semantic axes differ: expected {expected_axes}, got {observed_axes}"
            )
        return generated.variants


def _identifier_literals(spec: SemanticPlanSpec) -> list[str]:
    literals: list[str] = []
    for condition in spec.conditions:
        values = condition.value if isinstance(condition.value, list) else [condition.value]
        for value in values:
            if _IDENTIFIER.fullmatch(value):
                literals.append(value)
    return list(dict.fromkeys(literals))


def _required_numbers(spec: SemanticPlanSpec) -> list[float]:
    values: list[float] = []
    for condition in spec.conditions:
        rendered = condition.value if isinstance(condition.value, list) else [condition.value]
        for value in rendered:
            match = re.fullmatch(r"(\d+(?:\.\d+)?)%", value)
            if match:
                values.append(float(match.group(1)))
    return values


def _numbers_present(question: str, required: Sequence[float]) -> bool:
    observed = [float(value) for value in _NUMBER.findall(question)]
    return all(any(abs(actual - expected) < 1e-9 for actual in observed) for expected in required)


def _limit_present(question: str, limit: int | None) -> bool:
    if limit is None:
        return True
    return (
        re.search(
            rf"(?<!\d){limit}(?!\d)\s*(?:개|건|종목|상품|개만|건만)",
            question,
            re.IGNORECASE,
        )
        is not None
    )


def _required_field_names(spec: SemanticPlanSpec) -> set[str]:
    names = {
        item.field_name
        for item in [*spec.conditions, *spec.ranking, *spec.comparison_fields, *spec.group_by]
    }
    names.update(
        item.field_name
        for item in spec.aggregations
        if not (item.function is AggregateFunction.COUNT and item.field_name == "product_id")
    )
    names.difference_update({"product_id", "ticker", "public_offering"})
    return names


def _concepts_present(question: str, spec: SemanticPlanSpec) -> bool:
    return all(
        (pattern := _FIELD_CONCEPTS.get(field_name)) is None or pattern.search(question)
        for field_name in _required_field_names(spec)
    )


def _semantic_family_present(question: str, family: ProductFamily) -> bool:
    if family is ProductFamily.FUND:
        return re.search(r"공모.{0,20}펀드", question, re.IGNORECASE) is not None
    return _product_family_present(question, family)


def _intent_present(question: str, spec: SemanticPlanSpec) -> bool:
    if spec.intent is Intent.COMPARE:
        explicit = re.search(r"비교|차이|대조", question, re.IGNORECASE) is not None
        implicit = (
            len(_identifier_literals(spec)) >= 2
            and re.search(r"둘\s*중|어느|어떤\s*게|각각|\bvs\b|\b대\b", question, re.IGNORECASE)
            is not None
        )
        return explicit or implicit
    if spec.intent is Intent.AGGREGATE:
        required = []
        functions = {item.function for item in spec.aggregations}
        if AggregateFunction.COUNT in functions:
            required.append(
                re.compile(
                    r"몇|개수|수량|총\s*개|분포|집계|계산|"
                    r"(?:상품|펀드|채권|ETF|ETP).{0,4}수(?:를|가|는)?",
                    re.IGNORECASE,
                )
            )
        if AggregateFunction.AVG in functions:
            required.append(re.compile(r"평균"))
        if AggregateFunction.MAX in functions:
            required.append(re.compile(r"최대|최댓|최고"))
        if AggregateFunction.MIN in functions:
            required.append(re.compile(r"최소|최솟"))
        if AggregateFunction.SUM in functions:
            required.append(re.compile(r"합계|총합"))
        if spec.group_by:
            required.append(re.compile(r"별|기준|나눠|분포"))
        return all(pattern.search(question) for pattern in required)
    explicit = re.search(r"조회|찾|검색|보여|알려|확인", question) is not None
    implicit_detail = "상세" in question
    implicit_ranked_list = spec.result_limit is not None and bool(spec.ranking)
    return explicit or implicit_detail or implicit_ranked_list


def _operators_present(question: str, spec: SemanticPlanSpec) -> bool:
    for condition in spec.conditions:
        if condition.operator == "이하" and re.search(r"이하|넘지\s*않|≤", question) is None:
            return False
        if condition.operator == "미만" and "미만" not in question:
            return False
        if condition.operator == "이상" and re.search(r"이상|적어도", question) is None:
            return False
        if condition.operator == "초과" and "초과" not in question:
            return False
    return True


def _ranking_present(question: str, spec: SemanticPlanSpec) -> bool:
    for ranking in spec.ranking:
        if ranking.direction == _DIRECTION_LABELS[SortDirection.ASC]:
            if re.search(r"낮|작|적|짧|빠|오름", question) is None:
                return False
        elif re.search(r"높|큰|많|내림", question) is None:
            return False
    return True


def _max_similarity(question: str, references: Sequence[str]) -> float:
    normalized = _normalized_text(question)
    return max(
        SequenceMatcher(None, normalized, _normalized_text(reference)).ratio()
        for reference in references
    )


def validate_semantic_question(
    question: str,
    spec: SemanticPlanSpec,
    *,
    source_questions: Sequence[str],
    sibling_questions: Sequence[str] = (),
) -> MutationValidation:
    normalized = _normalized_text(question)
    sibling_normalized = {_normalized_text(value) for value in sibling_questions}
    identifiers = _identifier_literals(spec)
    family_is_fixed_by_identifiers = bool(identifiers) and spec.request_kind in {
        "두 상품 비교",
        "식별자가 정확히 일치하는 상품 상세 조회",
    }
    checks = {
        "not_public_source_copy": normalized
        not in {_normalized_text(value) for value in source_questions},
        "lexically_novel": _max_similarity(question, source_questions) <= 0.88,
        "not_duplicate": normalized not in sibling_normalized,
        "product_family_present": (
            _semantic_family_present(question, spec.product_family)
            or family_is_fixed_by_identifiers
        ),
        "identifier_literals_present": all(
            literal.casefold() in normalized for literal in identifiers
        ),
        "numeric_constraints_present": _numbers_present(question, _required_numbers(spec)),
        "result_limit_present": _limit_present(question, spec.result_limit),
        "required_concepts_present": _concepts_present(question, spec),
        "intent_present": _intent_present(question, spec),
        "operators_present": _operators_present(question, spec),
        "ranking_direction_present": _ranking_present(question, spec),
        "no_internal_jargon": _INTERNAL_JARGON.search(question) is None,
        "no_added_unsafe_intent": _UNSAFE_ADDED_INTENT.search(question) is None,
        "single_question": question.count("\n") <= 2,
        "length_safe": 10 <= len(normalized) <= 500,
        "no_code_or_json_wrapper": _CODE_FENCE_OR_JSON.search(question) is None,
        "no_nul": "\x00" not in question,
    }
    violations = [name for name, passed in checks.items() if not passed]
    return MutationValidation(checks=checks, violations=violations, passed=not violations)


def load_semantic_roundtrip_protocol() -> LoadedSemanticRoundtripProtocol:
    from importlib.resources import files

    resource = files("finance_agent_core.evaluation.suites").joinpath(_RESOURCE_NAME)
    raw = resource.read_bytes()
    protocol = SemanticRoundtripProtocol.model_validate_json(raw)
    source = load_official_mock_suite()
    if protocol.source_suite_sha256 != source.sha256:
        raise ValueError("semantic roundtrip source suite SHA-256 differs")
    source_ids = {case.id for case in source.suite.cases}
    covered_ids = {*protocol.source_case_ids, *protocol.excluded_case_ids}
    if covered_ids != source_ids:
        raise ValueError("semantic roundtrip included/excluded IDs do not cover the source suite")
    return LoadedSemanticRoundtripProtocol(
        protocol=protocol,
        sha256=_sha256_bytes(raw),
        resource_name=_RESOURCE_NAME,
    )


def _derive_plan(case: OfficialMockCase, service: RoutedPlanService) -> QueryPlan:
    result = service.answer(case.question, f"semantic-source-{case.id}")
    if result.status != "executed" or result.query_plan is None or result.family_searches:
        raise ValueError(f"source case does not have one executable QueryPlan: {case.id}")
    return result.query_plan


def _candidate_hard_literals(spec: SemanticPlanSpec) -> list[str]:
    return [
        *_identifier_literals(spec),
        *(str(value) for value in _required_numbers(spec)),
        *([] if spec.result_limit is None else [str(spec.result_limit)]),
    ]


def rescreen_semantic_roundtrip_batch(
    batch: MutationBatch,
    *,
    services: Mapping[ProductFamily, RoutedPlanService],
    loaded_protocol: LoadedSemanticRoundtripProtocol | None = None,
) -> MutationBatch:
    """Reapply improved mechanical checks without regenerating any Qwen text."""

    loaded = loaded_protocol or load_semantic_roundtrip_protocol()
    protocol = loaded.protocol
    if batch.protocol_id != protocol.protocol_id:
        raise ValueError("batch is not a semantic-roundtrip-v1 batch")
    if batch.protocol_sha256 != loaded.sha256:
        raise ValueError("semantic roundtrip protocol SHA-256 differs")
    if batch.source_suite_sha256 != protocol.source_suite_sha256:
        raise ValueError("semantic roundtrip source suite SHA-256 differs")

    source = load_official_mock_suite()
    cases = {case.id: case for case in source.suite.cases}
    source_questions = [case.question for case in source.suite.cases]
    specs: dict[str, SemanticPlanSpec] = {}
    siblings_by_source: dict[str, list[str]] = {}
    screened: list[MutationCandidate] = []
    for candidate in batch.candidates:
        if candidate.source_case_id not in protocol.source_case_ids:
            raise ValueError(
                f"semantic batch references an excluded source case: {candidate.source_case_id}"
            )
        case = cases[candidate.source_case_id]
        if candidate.coverage_family is not case.coverage_family:
            raise ValueError("semantic candidate family differs from its source case")
        if candidate.source_question != case.question:
            raise ValueError("semantic candidate source question differs from frozen suite")
        if candidate.source_case_id not in specs:
            plan = _derive_plan(case, services[case.coverage_family])
            specs[candidate.source_case_id] = build_semantic_plan_spec(plan)
        spec = specs[candidate.source_case_id]
        siblings = siblings_by_source.setdefault(candidate.source_case_id, [])
        validation = validate_semantic_question(
            candidate.question,
            spec,
            source_questions=source_questions,
            sibling_questions=siblings,
        )
        siblings.append(candidate.question)
        screened.append(
            candidate.model_copy(
                update={
                    "validation": validation,
                    "hard_literals": _candidate_hard_literals(spec),
                }
            )
        )
    accepted = sum(candidate.validation.passed for candidate in screened)
    limits = [
        item
        for item in batch.interpretation_limits
        if not item.startswith("기계 검사 재적용 버전:")
    ]
    payload = batch.model_dump(mode="json")
    payload.update(
        accepted_count=accepted,
        rejected_count=len(screened) - accepted,
        candidates=[candidate.model_dump(mode="json") for candidate in screened],
        interpretation_limits=[*limits, f"기계 검사 재적용 버전: {_SCREEN_VERSION}"],
    )
    return MutationBatch.model_validate(payload)


def generate_semantic_roundtrip_batch(
    provider: SemanticQuestionProvider,
    *,
    services: Mapping[ProductFamily, RoutedPlanService],
    loaded_protocol: LoadedSemanticRoundtripProtocol | None = None,
    generated_at_utc: str | None = None,
) -> MutationBatch:
    loaded = loaded_protocol or load_semantic_roundtrip_protocol()
    protocol = loaded.protocol
    source = load_official_mock_suite()
    cases = {case.id: case for case in source.suite.cases}
    source_questions = [case.question for case in source.suite.cases]
    candidates: list[MutationCandidate] = []
    for source_index, source_case_id in enumerate(protocol.source_case_ids, start=1):
        case = cases[source_case_id]
        plan = _derive_plan(case, services[case.coverage_family])
        spec = build_semantic_plan_spec(plan)
        generated = provider.generate_questions(spec, protocol.axes)
        siblings: list[str] = []
        for item in generated:
            validation = validate_semantic_question(
                item.question,
                spec,
                source_questions=source_questions,
                sibling_questions=siblings,
            )
            siblings.append(item.question)
            candidates.append(
                MutationCandidate(
                    id=f"{protocol.protocol_id}-{source_index:03d}-{item.axis.value}",
                    source_case_id=case.id,
                    axis=item.axis,
                    coverage_family=case.coverage_family,
                    source_question=case.question,
                    question=item.question,
                    hard_literals=_candidate_hard_literals(spec),
                    validation=validation,
                )
            )
    accepted = sum(item.validation.passed for item in candidates)
    generated_at = generated_at_utc or datetime.now(UTC).isoformat()
    return MutationBatch(
        batch_id=f"{protocol.protocol_id}-{provider.provider_name}",
        generated_at_utc=generated_at,
        protocol_id=protocol.protocol_id,
        protocol_sha256=loaded.sha256,
        source_suite_id=protocol.source_suite_id,
        source_suite_sha256=protocol.source_suite_sha256,
        generator=provider.provider_name,
        model=provider.model_name,
        requested_count=len(protocol.source_case_ids) * len(protocol.axes),
        generated_count=len(candidates),
        accepted_count=accepted,
        rejected_count=len(candidates) - accepted,
        candidates=candidates,
        interpretation_limits=[
            *protocol.interpretation_limits,
            "각 질문의 원문 최대 문자 유사도 0.88 이하만 실행 후보로 인정함",
        ],
    )
