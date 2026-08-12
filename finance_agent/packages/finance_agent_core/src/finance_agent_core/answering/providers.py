from __future__ import annotations

import json
from collections.abc import Callable
from copy import deepcopy
from typing import Any, Literal

from finance_agent_core.agent.providers.hyperclova import (
    HyperClovaXCallRecord,
    HyperClovaXClient,
    HyperClovaXResponseError,
    HyperClovaXSettings,
    HyperClovaXTransport,
    parse_hcx_json_object,
)
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
from finance_agent_core.contracts.hcx_schema import validate_hcx_payload


def _safe_explanation(context: GroundedAnswerContext) -> str:
    intent = context.query_plan.intent.value
    if intent == "compare":
        return "선택한 근거 항목이 요청한 상품 비교 근거로 사용됐습니다."
    if intent == "explain":
        return "선택한 근거 항목이 요청한 상품 설명 근거로 사용됐습니다."
    if context.query_plan.ranking:
        return "선택한 근거 항목이 요청한 정렬 근거로 사용됐습니다."
    return "선택한 근거 항목이 요청한 상품 조회 근거로 사용됐습니다."


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
        "intent": context.query_plan.intent.value,
        "ranking": [ranking.model_dump(mode="json") for ranking in context.query_plan.ranking],
        "comparison_fields": context.query_plan.intent_payload.comparison_fields,
        "products": products,
        "required_warning_codes": [warning.code for warning in context.warnings],
        "safe_explanation": _safe_explanation(context),
    }


def build_grounded_answer_system_prompt(context: GroundedAnswerContext) -> str:
    payload = json.dumps(
        _generation_payload(context),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return f"""
당신은 검증된 금융상품 검색·비교 결과를 설명하는 grounded answer planner다.
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
- 상품별 explanation은 선택한 evidence_fields가 정렬·식별 또는 비교 근거로
  사용됐다는 사실만 짧게 설명한다. 각 상품에 입력 safe_explanation을 글자 하나
  바꾸지 않고 그대로 복사한다. 비교 우열이나 추천을 판단하지 않는다.
  좋음·나쁨·유리함·수익성·전망·예측 같은 평가나 투자 해석을 추가하지 않는다.
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
        item["properties"]["explanation"] = {
            "type": "string",
            "const": _safe_explanation(context),
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
                    explanation=_safe_explanation(context),
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
                {"role": "system", "content": build_grounded_answer_system_prompt(context)},
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


def _hcx_grounded_answer_schema(
    context: GroundedAnswerContext,
) -> dict[str, Any]:
    result_refs = [f"result_{index}" for index in range(1, len(context.products) + 1)]
    usable_fields = sorted(
        {
            field.canonical_field
            for product in context.products
            for field in product.fields
            if field.normalized_value is not None
            and field.quality in {QualityStatus.VALID, QualityStatus.PARTIAL}
        }
    )
    warning_codes = [warning.code for warning in context.warnings]
    warning_items: dict[str, Any] = {"type": "string"}
    if warning_codes:
        warning_items["enum"] = warning_codes
    product_schema = {
        "type": "object",
        "properties": {
            "result_ref": {
                "type": "string",
                "enum": result_refs,
            },
            "evidence_fields": {
                "type": "array",
                "items": {
                    "type": "string",
                    "enum": usable_fields,
                },
                "minItems": 1,
                "maxItems": 20,
            },
            "explanation": {
                "type": "string",
                "enum": [_safe_explanation(context)],
                "description": "서버가 제공한 안전한 근거 설명문",
            },
        },
        "required": ["result_ref", "evidence_fields", "explanation"],
    }
    return {
        "type": "object",
        "properties": {
            "schema_version": {
                "type": "string",
                "enum": ["1.0"],
            },
            "lead": {
                "type": "string",
                "enum": _SAFE_LEADS,
            },
            "products": {
                "type": "array",
                "items": product_schema,
                "minItems": len(context.products),
                "maxItems": len(context.products),
            },
            "acknowledged_warning_codes": {
                "type": "array",
                "items": warning_items,
                "minItems": len(warning_codes),
                "maxItems": len(warning_codes),
            },
        },
        "required": [
            "schema_version",
            "lead",
            "products",
            "acknowledged_warning_codes",
        ],
    }


class HyperClovaXGroundedAnswerProvider:
    def __init__(
        self,
        settings: HyperClovaXSettings,
        transport: HyperClovaXTransport,
        *,
        on_call: Callable[[HyperClovaXCallRecord], None] | None = None,
    ) -> None:
        self.settings = settings
        self._client = HyperClovaXClient(settings, transport, on_call=on_call)

    @property
    def provider_name(self) -> Literal["hyperclova"]:
        return "hyperclova"

    @property
    def model_name(self) -> str:
        return self.settings.model

    def generate_grounded_answer(
        self,
        context: GroundedAnswerContext,
    ) -> GroundedAnswerDraft:
        response_schema = _hcx_grounded_answer_schema(context)
        content = self._client.complete(
            operation="grounded_answer",
            system_prompt=build_grounded_answer_system_prompt(context),
            user_prompt="검증된 입력만 사용해 grounded answer JSON을 작성해줘.",
            schema_name="grounded_finance_answer",
            response_schema=response_schema,
            max_output_tokens=2048,
        )
        payload = parse_hcx_json_object(content, "grounded answer")
        try:
            validate_hcx_payload(response_schema, payload)
            return GroundedAnswerDraft.model_validate(payload)
        except ValueError:
            raise HyperClovaXResponseError(
                "HyperCLOVA X returned an invalid grounded answer"
            ) from None
