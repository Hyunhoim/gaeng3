from __future__ import annotations

import json
import math
import os
import re
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, model_validator

from finance_agent_core.agent.fund_comparison_parser import (
    SUPPORTED_FUND_COMPARISON_FIELDS,
    FundComparisonDraft,
)
from finance_agent_core.agent.linker import (
    canonicalize_linked_query_plan,
    canonicalize_query_plan_payload,
)
from finance_agent_core.agent.providers.local_test import (
    build_fund_comparison_draft_system_prompt,
    build_query_plan_system_prompt,
)
from finance_agent_core.contracts import QueryPlan, load_hcx_queryplan_schema
from finance_agent_core.contracts.hcx_schema import validate_hcx_schema
from finance_agent_core.deadline import (
    RequestDeadlineExceeded,
    remaining_request_timeout,
)

type HyperClovaXOperation = Literal[
    "query_plan",
    "fund_comparison_draft",
    "grounded_answer",
]
type HyperClovaXOutcome = Literal[
    "success",
    "authentication_error",
    "rate_limited",
    "service_error",
    "timeout",
    "transport_error",
    "response_error",
]


class HyperClovaXProviderError(RuntimeError):
    """Base error for the official-provider boundary."""


class HyperClovaXConfigurationError(HyperClovaXProviderError):
    """Raised before a non-HCX provider can enter the official path."""


class HyperClovaXAuthenticationError(HyperClovaXProviderError):
    """Raised for normalized 401 and 403 transport responses."""


class HyperClovaXRateLimitError(HyperClovaXProviderError):
    """Raised for normalized 429 transport responses."""


class HyperClovaXServiceError(HyperClovaXProviderError):
    """Raised for normalized non-success service responses."""


class HyperClovaXTimeoutError(HyperClovaXProviderError):
    """Raised when the injected transport exceeds the request timeout."""


class HyperClovaXTransportError(HyperClovaXProviderError):
    """Raised when the injected transport cannot complete a request."""


class HyperClovaXResponseError(HyperClovaXProviderError):
    """Raised when a successful transport response violates the contract."""


@dataclass(frozen=True, slots=True)
class HyperClovaXSettings:
    model: str
    timeout_seconds: float = 60.0

    def __post_init__(self) -> None:
        if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,199}", self.model) is None:
            raise HyperClovaXConfigurationError(
                "HCX_MODEL must be a non-empty safe model identifier"
            )
        if not self.model.upper().startswith("HCX-"):
            raise HyperClovaXConfigurationError(
                "official provider model must use an HCX- identifier"
            )
        if (
            not math.isfinite(self.timeout_seconds)
            or self.timeout_seconds <= 0
            or self.timeout_seconds > 300
        ):
            raise HyperClovaXConfigurationError("HyperCLOVA X timeout must be in (0, 300]")

    @classmethod
    def from_environment(
        cls,
        environment: Mapping[str, str] | None = None,
    ) -> HyperClovaXSettings:
        values = os.environ if environment is None else environment
        mode = values.get("FINANCE_AGENT_LLM_MODE")
        provider = values.get("LLM_PROVIDER")
        if mode not in {"evaluation", "production"} or provider != "hyperclova":
            raise HyperClovaXConfigurationError(
                "official execution requires FINANCE_AGENT_LLM_MODE="
                "evaluation|production and LLM_PROVIDER=hyperclova"
            )
        model = values.get("HCX_MODEL", "").strip()
        try:
            timeout = float(values.get("HCX_TIMEOUT_SECONDS", "60"))
        except ValueError as error:
            raise HyperClovaXConfigurationError("HCX_TIMEOUT_SECONDS must be numeric") from error
        return cls(model=model, timeout_seconds=timeout)


class HyperClovaXContractModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class HyperClovaXTokenUsage(HyperClovaXContractModel):
    input_tokens: int = Field(ge=0)
    output_tokens: int = Field(ge=0)
    total_tokens: int = Field(ge=0)

    @model_validator(mode="after")
    def validate_total(self) -> HyperClovaXTokenUsage:
        if self.total_tokens != self.input_tokens + self.output_tokens:
            raise ValueError("total_tokens must equal input_tokens + output_tokens")
        return self


class HyperClovaXStructuredRequest(HyperClovaXContractModel):
    schema_version: Literal["1.0"] = "1.0"
    operation: HyperClovaXOperation
    model: str = Field(min_length=1, max_length=200)
    system_prompt: str = Field(min_length=1, max_length=200_000)
    user_prompt: str = Field(min_length=1, max_length=10_000)
    schema_name: str = Field(pattern=r"^[a-z][a-z0-9_]{2,63}$")
    response_schema: dict[str, Any]
    max_output_tokens: int = Field(ge=1, le=16_384)
    timeout_seconds: float = Field(gt=0, le=300)


class HyperClovaXTransportResponse(HyperClovaXContractModel):
    status_code: int = Field(ge=100, le=599)
    content: str | None = Field(default=None, max_length=2_000_000)
    request_id: str | None = Field(
        default=None,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$",
    )
    usage: HyperClovaXTokenUsage | None = None


class HyperClovaXCallRecord(HyperClovaXContractModel):
    operation: HyperClovaXOperation
    model: str
    outcome: HyperClovaXOutcome
    status_code: int | None
    latency_ms: float = Field(ge=0)
    request_id: str | None
    usage: HyperClovaXTokenUsage | None


class HyperClovaXTransport(Protocol):
    """Translate one semantic structured request to the official HTTP API later."""

    def complete(self, request: HyperClovaXStructuredRequest) -> object: ...


class HyperClovaXClient:
    def __init__(
        self,
        settings: HyperClovaXSettings,
        transport: HyperClovaXTransport,
        *,
        on_call: Callable[[HyperClovaXCallRecord], None] | None = None,
    ) -> None:
        self.settings = settings
        self.transport = transport
        self.on_call = on_call

    def _emit(
        self,
        *,
        operation: HyperClovaXOperation,
        outcome: HyperClovaXOutcome,
        started: float,
        response: HyperClovaXTransportResponse | None = None,
    ) -> None:
        if self.on_call is None:
            return
        record = HyperClovaXCallRecord(
            operation=operation,
            model=self.settings.model,
            outcome=outcome,
            status_code=None if response is None else response.status_code,
            latency_ms=round((time.perf_counter() - started) * 1000, 3),
            request_id=None if response is None else response.request_id,
            usage=None if response is None else response.usage,
        )
        try:
            self.on_call(record)
        except Exception:  # noqa: BLE001 - telemetry must not change provider behavior
            return

    def complete(
        self,
        *,
        operation: HyperClovaXOperation,
        system_prompt: str,
        user_prompt: str,
        schema_name: str,
        response_schema: dict[str, Any],
        max_output_tokens: int,
    ) -> str:
        try:
            validate_hcx_schema(response_schema)
        except ValueError as error:
            raise HyperClovaXConfigurationError(
                "response schema is outside the documented HyperCLOVA X subset"
            ) from error
        started = time.perf_counter()
        try:
            timeout_seconds = remaining_request_timeout(self.settings.timeout_seconds)
        except RequestDeadlineExceeded:
            self._emit(operation=operation, outcome="timeout", started=started)
            raise HyperClovaXTimeoutError("HyperCLOVA X request timed out") from None
        request = HyperClovaXStructuredRequest(
            operation=operation,
            model=self.settings.model,
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            schema_name=schema_name,
            response_schema=response_schema,
            max_output_tokens=max_output_tokens,
            timeout_seconds=timeout_seconds,
        )
        try:
            raw_response = self.transport.complete(request)
        except TimeoutError:
            self._emit(operation=operation, outcome="timeout", started=started)
            raise HyperClovaXTimeoutError("HyperCLOVA X request timed out") from None
        except (ConnectionError, OSError):
            self._emit(operation=operation, outcome="transport_error", started=started)
            raise HyperClovaXTransportError("HyperCLOVA X transport failed") from None
        try:
            response = HyperClovaXTransportResponse.model_validate(raw_response)
        except ValueError:
            self._emit(operation=operation, outcome="response_error", started=started)
            raise HyperClovaXResponseError(
                "HyperCLOVA X transport returned an invalid response contract"
            ) from None

        if response.status_code in {401, 403}:
            self._emit(
                operation=operation,
                outcome="authentication_error",
                started=started,
                response=response,
            )
            raise HyperClovaXAuthenticationError("HyperCLOVA X authentication failed")
        if response.status_code == 429:
            self._emit(
                operation=operation,
                outcome="rate_limited",
                started=started,
                response=response,
            )
            raise HyperClovaXRateLimitError("HyperCLOVA X request was rate limited")
        if not 200 <= response.status_code < 300:
            self._emit(
                operation=operation,
                outcome="service_error",
                started=started,
                response=response,
            )
            raise HyperClovaXServiceError(
                f"HyperCLOVA X service returned HTTP {response.status_code}"
            )
        if response.content is None:
            self._emit(
                operation=operation,
                outcome="response_error",
                started=started,
                response=response,
            )
            raise HyperClovaXResponseError("HyperCLOVA X response content is missing")
        self._emit(
            operation=operation,
            outcome="success",
            started=started,
            response=response,
        )
        return response.content


def parse_hcx_json_object(content: str, label: str) -> dict[str, Any]:
    try:
        payload = json.loads(content)
    except json.JSONDecodeError:
        raise HyperClovaXResponseError(f"HyperCLOVA X did not return {label} JSON") from None
    if not isinstance(payload, dict):
        raise HyperClovaXResponseError(f"HyperCLOVA X did not return a {label} object")
    return payload


def _fund_comparison_hcx_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "target_mentions": {
                "type": "array",
                "items": {"type": "string"},
                "minItems": 0,
                "maxItems": 4,
            },
            "comparison_fields": {
                "type": "array",
                "items": {
                    "type": "string",
                    "enum": list(SUPPORTED_FUND_COMPARISON_FIELDS),
                },
                "minItems": 0,
                "maxItems": 16,
            },
        },
        "required": ["target_mentions", "comparison_fields"],
    }


class HyperClovaXQueryPlanProvider:
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

    def generate_query_plan(self, question: str, question_id: str) -> QueryPlan:
        if not question.strip():
            raise ValueError("question cannot be blank")
        if not question_id.strip():
            raise ValueError("question_id cannot be blank")
        content = self._client.complete(
            operation="query_plan",
            system_prompt=build_query_plan_system_prompt(question_id, question),
            user_prompt=question,
            schema_name="finance_query_plan",
            response_schema=load_hcx_queryplan_schema(),
            max_output_tokens=4096,
        )
        payload = parse_hcx_json_object(content, "QueryPlan")
        payload["question_id"] = question_id
        try:
            payload = canonicalize_query_plan_payload(question, payload)
            payload["question_id"] = question_id
            plan = QueryPlan.model_validate(payload)
            return canonicalize_linked_query_plan(question, plan)
        except (KeyError, TypeError, ValueError):
            raise HyperClovaXResponseError("HyperCLOVA X returned an invalid QueryPlan") from None


class HyperClovaXFundComparisonDraftProvider:
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

    def generate_comparison_draft(
        self,
        question: str,
        question_id: str,
    ) -> FundComparisonDraft:
        if not question.strip():
            raise ValueError("question cannot be blank")
        if not question_id.strip():
            raise ValueError("question_id cannot be blank")
        content = self._client.complete(
            operation="fund_comparison_draft",
            system_prompt=build_fund_comparison_draft_system_prompt(question),
            user_prompt="질문에 실제로 적힌 비교 대상과 비교 항목만 추출해줘.",
            schema_name="fund_comparison_draft",
            response_schema=_fund_comparison_hcx_schema(),
            max_output_tokens=1024,
        )
        payload = parse_hcx_json_object(content, "fund comparison draft")
        try:
            return FundComparisonDraft.model_validate(payload)
        except ValueError:
            raise HyperClovaXResponseError(
                "HyperCLOVA X returned an invalid fund comparison draft"
            ) from None
