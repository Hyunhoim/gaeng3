from __future__ import annotations

import json
from copy import deepcopy
from typing import Any, Literal

from finance_agent_core.agent.providers.local_test import (
    LocalProviderError,
    LocalTestProvider,
    LocalTestSettings,
)
from finance_agent_core.answering.context import required_evidence_fields
from finance_agent_core.answering.models import (
    GroundedAnswerContext,
    GroundedAnswerDraft,
    ProductAnswerDraft,
)
from finance_agent_core.config import QualityStatus, load_field_registry


def _generation_payload(context: GroundedAnswerContext) -> dict[str, Any]:
    registry = load_field_registry()
    required = required_evidence_fields(context)
    products: list[dict[str, Any]] = []
    for rank, product in enumerate(context.products, start=1):
        fields = [
            {
                "canonical_field": field.canonical_field,
                "label": registry.require_field(
                    field.canonical_field,
                    [context.source_manifest.dataset],
                ).label,
                "unit": field.unit,
                "quality": field.quality.value,
            }
            for field in product.fields
            if field.normalized_value is not None
            and field.quality in {QualityStatus.VALID, QualityStatus.PARTIAL}
        ]
        products.append(
            {
                "result_ref": f"result_{rank}",
                "required_evidence_fields": [
                    name
                    for name in required
                    if any(field["canonical_field"] == name for field in fields)
                ],
                "available_evidence": fields,
            }
        )
    return {
        "ranking": [ranking.model_dump(mode="json") for ranking in context.query_plan.ranking],
        "products": products,
        "required_warning_codes": [warning.code for warning in context.warnings],
    }


def _answer_system_prompt(context: GroundedAnswerContext) -> str:
    payload = json.dumps(
        _generation_payload(context),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return f"""
당신은 검증된 금융상품 검색 결과를 설명하는 grounded answer planner다.
반드시 JSON 하나만 출력하고 제공된 근거 밖의 사실을 만들지 않는다.

출력 규칙:
- products는 입력 products의 result_ref와 순서를 하나도 바꾸지 않고 복사한다.
- 각 evidence_fields에는 해당 상품의 required_evidence_fields를 모두 포함한다.
- evidence_fields에는 available_evidence에 있는 canonical_field만 넣는다.
- lead와 explanation은 자연스러운 한국어로 쓰되 실제 값, 날짜, 개수, 퍼센트·
  통화 금액, 상품명, 티커, 상품 식별자를 쓰지 않는다. 단, available_evidence의
  label 자체에 포함된 기간 표현은 그대로 쓸 수 있다. 정확한 값과 식별자는
  서버가 검증된 근거로 별도 컴파일한다.
- 과거 성과를 미래 성과처럼 표현하거나 매수·매도·수익 보장 표현을 쓰지 않는다.
- 상품별 explanation은 선택한 evidence_fields가 정렬 또는 식별 근거로
  사용됐다는 사실만 짧게 설명한다. 좋음·나쁨·유리함·수익성·전망·예측·추천
  같은 평가나 투자 해석을 추가하지 않는다.
- acknowledged_warning_codes는 required_warning_codes를 같은 순서로 정확히 복사한다.
- 입력에 없는 경고 코드나 evidence field를 추가하지 않는다.

검증된 입력:
{payload}
""".strip()


_SAFE_LEADS = [
    "검증된 조건과 데이터에 따라 결과를 정리했습니다.",
    "검증된 검색 결과와 근거를 바탕으로 상품을 정리했습니다.",
    "적용된 검색 조건을 통과한 결과를 근거와 함께 정리했습니다.",
]


def _answer_schema(context: GroundedAnswerContext) -> dict[str, Any]:
    schema = GroundedAnswerDraft.model_json_schema()
    schema["properties"]["lead"]["enum"] = _SAFE_LEADS

    base_product = ProductAnswerDraft.model_json_schema()
    prefix_items: list[dict[str, Any]] = []
    for index, product in enumerate(context.products, start=1):
        item = deepcopy(base_product)
        usable_fields = [
            field.canonical_field
            for field in product.fields
            if field.normalized_value is not None
            and field.quality in {QualityStatus.VALID, QualityStatus.PARTIAL}
        ]
        item["properties"]["result_ref"] = {
            "type": "string",
            "const": f"result_{index}",
        }
        item["properties"]["evidence_fields"]["items"] = {
            "type": "string",
            "enum": usable_fields,
        }
        prefix_items.append(item)
    schema["properties"]["products"] = {
        "type": "array",
        "prefixItems": prefix_items,
        "items": False,
        "minItems": len(prefix_items),
        "maxItems": len(prefix_items),
    }

    warning_codes = [warning.code for warning in context.warnings]
    warning_schema: dict[str, Any] = {
        "type": "array",
        "minItems": len(warning_codes),
        "maxItems": len(warning_codes),
    }
    if warning_codes:
        warning_schema["prefixItems"] = [
            {"type": "string", "const": code} for code in warning_codes
        ]
        warning_schema["items"] = False
    else:
        warning_schema["items"] = {"type": "string"}
    schema["properties"]["acknowledged_warning_codes"] = warning_schema
    return schema


class ExpectedGroundedAnswerProvider:
    @property
    def provider_name(self) -> Literal["expected"]:
        return "expected"

    @property
    def model_name(self) -> None:
        return None

    def generate_grounded_answer(
        self,
        context: GroundedAnswerContext,
    ) -> GroundedAnswerDraft:
        required = required_evidence_fields(context)
        products: list[ProductAnswerDraft] = []
        for index, product in enumerate(context.products, start=1):
            usable = {
                field.canonical_field
                for field in product.fields
                if field.normalized_value is not None
                and field.quality in {QualityStatus.VALID, QualityStatus.PARTIAL}
            }
            selected = [field for field in required if field in usable]
            if not selected:
                selected = [
                    next(
                        field.canonical_field
                        for field in product.fields
                        if field.canonical_field in usable
                    )
                ]
            products.append(
                ProductAnswerDraft(
                    result_ref=f"result_{index}",
                    evidence_fields=selected,
                    explanation="검증된 검색 조건과 근거에 따라 포함된 결과입니다.",
                )
            )
        return GroundedAnswerDraft(
            lead=_SAFE_LEADS[0],
            products=products,
            acknowledged_warning_codes=[warning.code for warning in context.warnings],
        )


class LocalGroundedAnswerProvider:
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

    def generate_grounded_answer(
        self,
        context: GroundedAnswerContext,
    ) -> GroundedAnswerDraft:
        schema = _answer_schema(context)
        request_payload = {
            "model": self.settings.model,
            "messages": [
                {"role": "system", "content": _answer_system_prompt(context)},
                {
                    "role": "user",
                    "content": "검증된 입력만 사용해 grounded answer JSON을 작성해줘.",
                },
            ],
            "temperature": 0,
            "seed": 42,
            "max_tokens": 2048,
            "response_format": {
                "type": "json_schema",
                "json_schema": {
                    "name": "grounded_finance_answer",
                    "strict": True,
                    "schema": schema,
                },
            },
        }
        response = self._client._request_json("chat/completions", request_payload)
        try:
            content = response["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as error:
            raise LocalProviderError(
                "local grounded answer response has an unexpected shape"
            ) from error
        if not isinstance(content, str):
            raise LocalProviderError("local grounded answer content is not text")
        try:
            payload = json.loads(content)
        except json.JSONDecodeError as error:
            raise LocalProviderError("local model did not return grounded answer JSON") from error
        if not isinstance(payload, dict):
            raise LocalProviderError("local model did not return a JSON object")
        try:
            return GroundedAnswerDraft.model_validate(payload)
        except ValueError as error:
            raise LocalProviderError(
                f"local model returned an invalid grounded answer: {error}"
            ) from error
