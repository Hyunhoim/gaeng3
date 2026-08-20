from __future__ import annotations

import argparse
import json
import sys
import time
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from finance_agent_core.evaluation.official_acceptance import (
    build_official_acceptance_transport_cases,
)

EXPECTED_FAMILIES = ["bond", "domestic_etp", "overseas_etp", "fund"]


@dataclass(frozen=True)
class SmokeCase:
    case_id: str
    question: str | None
    expected_http_status: int
    expected_status: str
    expected_intent: str
    expected_families: tuple[str, ...] = ()
    expected_answer_mode: str = "control"
    expected_product_count: int = 0
    expected_dataset: str | None = None
    expected_query_plan_kind: str | None = None
    expected_citation_kind: str | None = None
    uses_answer_provider: bool = True
    expected_clarification_code: str | None = None
    expected_error_code: str | None = None

    def payload(self) -> dict[str, str]:
        if self.question is None:
            return {}
        return {
            "schema_version": "1.0",
            "request_id": self.case_id,
            "question": self.question,
            "locale": "ko-KR",
        }


@dataclass(frozen=True)
class OfficialSmokeCase:
    case_id: str
    query: tuple[tuple[str, str], ...]
    expected_question_id: str
    expected_question: str
    expected_control_code: str | None = None
    expected_trace_status: str | None = None
    expected_intent: str | None = None
    expected_families: tuple[str, ...] = ()
    expected_evidence: bool = False
    expected_empty_context: bool = False
    expected_execution_step: str | None = None
    expected_citation_kind: str | None = None
    expected_relation_type: str | None = None
    expected_relation_query: str | None = None
    expected_relation_top_k: int | None = None
    expected_product_count: int | None = None
    expected_relation_field: str | None = None
    expected_relation_value: str | None = None
    expected_candidate_count: int | None = None
    require_safe_control_response: bool = False
    forbidden_execution_steps: tuple[str, ...] = ()
    forbidden_output_fragments: tuple[str, ...] = ()

    def query_string(self) -> str:
        return urlencode(self.query)


CASES = (
    SmokeCase(
        case_id="docker-smoke-bond-001",
        question="매수 가능한 국내채권을 매수수익률 높은 순으로 3개 보여줘.",
        expected_http_status=200,
        expected_status="success",
        expected_intent="search",
        expected_families=("bond",),
        expected_answer_mode="deterministic",
        expected_product_count=3,
        expected_dataset="bond",
    ),
    SmokeCase(
        case_id="docker-smoke-domestic-etp-001",
        question="미국 주식형 국내 ETF를 1개월 수익률 높은 순으로 5개 보여줘.",
        expected_http_status=200,
        expected_status="success",
        expected_intent="search",
        expected_families=("domestic_etp",),
        expected_answer_mode="deterministic",
        expected_product_count=5,
        expected_dataset="domestic_etp",
    ),
    SmokeCase(
        case_id="docker-smoke-overseas-etp-001",
        question=(
            "미국 채권형 해외 ETF 중 현재 거래 가능한 상품에서 총보수 0.20% 이하인 "
            "상품을 AUM 순으로 5개 보여줘."
        ),
        expected_http_status=200,
        expected_status="success",
        expected_intent="search",
        expected_families=("overseas_etp",),
        expected_answer_mode="deterministic",
        expected_product_count=5,
        expected_dataset="overseas_etp",
    ),
    SmokeCase(
        case_id="docker-smoke-relation-issued-by-001",
        question="한국주택금융공사가 발행한 국내채권 3개 보여줘.",
        expected_http_status=200,
        expected_status="success",
        expected_intent="search",
        expected_families=("bond",),
        expected_answer_mode="deterministic",
        expected_product_count=3,
        expected_query_plan_kind="relation_search",
        expected_citation_kind="relation_field",
        uses_answer_provider=False,
    ),
    SmokeCase(
        case_id="docker-smoke-fund-locked-001",
        question=(
            "당사에서 판매 중인 해외 주식형 공모펀드 중 3개월 수익률이 높은 상품 5개 보여줘."
        ),
        expected_http_status=200,
        expected_status="unsupported",
        expected_intent="unsupported",
        expected_families=("fund",),
    ),
    SmokeCase(
        case_id="docker-smoke-clarification-001",
        question="안전한 상품을 추천해 주세요.",
        expected_http_status=200,
        expected_status="clarification",
        expected_intent="clarify",
        expected_clarification_code="subjective_condition",
    ),
    SmokeCase(
        case_id="docker-smoke-unsupported-001",
        question="내일 가장 오를 해외 ETF를 예측해서 매수 추천해줘.",
        expected_http_status=200,
        expected_status="unsupported",
        expected_intent="unsupported",
        expected_families=("overseas_etp",),
    ),
    SmokeCase(
        case_id="docker-smoke-invalid-001",
        question=None,
        expected_http_status=422,
        expected_status="error",
        expected_intent="unsupported",
        expected_error_code="invalid_request",
    ),
)

OFFICIAL_CASES = tuple(
    OfficialSmokeCase(
        case_id=case.id,
        query=case.query,
        expected_question_id=case.expected_question_id,
        expected_question=case.expected_question,
        expected_control_code=case.expected_control_code,
        expected_trace_status=(
            case.expected_trace_statuses[0] if len(case.expected_trace_statuses) == 1 else None
        ),
        expected_intent=(case.expected_intents[0] if len(case.expected_intents) == 1 else None),
        expected_families=tuple(family.value for family in case.expected_families),
        expected_evidence=case.require_evidence,
        expected_empty_context=case.require_no_execution,
        forbidden_output_fragments=case.forbidden_output_fragments,
    )
    for case in build_official_acceptance_transport_cases()
)

RELATION_OFFICIAL_CASES = (
    OfficialSmokeCase(
        case_id="official-relation-exact",
        query=(
            ("question_id", "docker-smoke-official-relation-exact-001"),
            ("question", "한국주택금융공사가 발행한 국내채권 3개 보여줘."),
        ),
        expected_question_id="docker-smoke-official-relation-exact-001",
        expected_question="한국주택금융공사가 발행한 국내채권 3개 보여줘.",
        expected_trace_status="success",
        expected_intent="search",
        expected_families=("bond",),
        expected_evidence=True,
        expected_execution_step="relation_retrieval",
        expected_citation_kind="relation_field",
        expected_relation_type="issued_by",
        expected_relation_query="한국주택금융공사",
        expected_relation_top_k=3,
        expected_product_count=3,
        expected_relation_field="issuer",
        expected_relation_value="한국주택금융공사",
        expected_candidate_count=3,
    ),
    OfficialSmokeCase(
        case_id="official-relation-partial-not-found",
        query=(
            ("question_id", "docker-smoke-official-relation-not-found-001"),
            ("question", "한국주택금융이 발행한 국내채권 3개 보여줘."),
        ),
        expected_question_id="docker-smoke-official-relation-not-found-001",
        expected_question="한국주택금융이 발행한 국내채권 3개 보여줘.",
        expected_trace_status="not_found",
        expected_intent="search",
        expected_families=("bond",),
        expected_empty_context=True,
        expected_execution_step="relation_retrieval",
        expected_relation_type="issued_by",
        expected_relation_query="한국주택금융",
        expected_relation_top_k=3,
        expected_product_count=0,
        expected_candidate_count=0,
    ),
    OfficialSmokeCase(
        case_id="official-relation-clarify",
        query=(
            ("question_id", "docker-smoke-official-relation-clarify-001"),
            ("question", "미래에셋이 운용하는 ETF를 보여줘."),
        ),
        expected_question_id="docker-smoke-official-relation-clarify-001",
        expected_question="미래에셋이 운용하는 ETF를 보여줘.",
        expected_control_code="ambiguous_product_family",
        expected_trace_status="clarification",
        expected_intent="clarify",
        expected_empty_context=True,
        require_safe_control_response=True,
        forbidden_execution_steps=("relation_retrieval", "sql", "deterministic_oracle"),
    ),
    OfficialSmokeCase(
        case_id="official-relation-unsupported",
        query=(
            ("question_id", "docker-smoke-official-relation-unsupported-001"),
            ("question", "미래에셋이 운용하는 공모펀드를 보여줘."),
        ),
        expected_question_id="docker-smoke-official-relation-unsupported-001",
        expected_question="미래에셋이 운용하는 공모펀드를 보여줘.",
        expected_trace_status="unsupported",
        expected_intent="unsupported",
        expected_empty_context=True,
        require_safe_control_response=True,
        forbidden_execution_steps=("relation_retrieval", "sql", "deterministic_oracle"),
    ),
)


def official_smoke_cases(relation_retrieval_status: str) -> tuple[OfficialSmokeCase, ...]:
    """Add public relation coverage only when the signed relation release is active."""

    if relation_retrieval_status == "ready":
        return OFFICIAL_CASES + RELATION_OFFICIAL_CASES
    if relation_retrieval_status == "disabled":
        return OFFICIAL_CASES
    raise ValueError(f"unsupported relation retrieval status: {relation_retrieval_status}")


def smoke_cases(
    fund_execution_policy: str,
    relation_retrieval_status: str = "ready",
) -> tuple[SmokeCase, ...]:
    if fund_execution_policy not in {"locked", "public_fund_v1_approved"}:
        raise ValueError(f"unsupported fund execution policy: {fund_execution_policy}")
    if relation_retrieval_status not in {"disabled", "ready"}:
        raise ValueError(f"unsupported relation retrieval status: {relation_retrieval_status}")
    cases = tuple(
        replace(
            case,
            case_id="docker-smoke-fund-approved-001",
            expected_status="success",
            expected_intent="search",
            expected_answer_mode="deterministic",
            expected_product_count=5,
            expected_dataset="fund",
            expected_clarification_code=None,
        )
        if fund_execution_policy == "public_fund_v1_approved"
        and case.case_id == "docker-smoke-fund-locked-001"
        else case
        for case in CASES
    )
    if relation_retrieval_status == "ready":
        return cases
    return tuple(
        replace(
            case,
            expected_status="unsupported",
            expected_intent="unsupported",
            expected_families=(),
            expected_answer_mode="control",
            expected_product_count=0,
            expected_query_plan_kind=None,
            expected_citation_kind=None,
        )
        if case.case_id == "docker-smoke-relation-issued-by-001"
        else case
        for case in cases
    )


def _request_json(
    url: str,
    *,
    timeout: float,
    payload: dict[str, str] | None = None,
) -> tuple[int, dict[str, Any], int, float, str]:
    data = None
    headers: dict[str, str] = {}
    method = "GET"
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"
        method = "POST"
    request = Request(url, data=data, headers=headers, method=method)
    started = time.perf_counter()
    try:
        with urlopen(request, timeout=timeout) as response:
            status = response.status
            raw = response.read()
            content_type = response.headers.get("Content-Type", "")
    except HTTPError as error:
        status = error.code
        raw = error.read()
        content_type = error.headers.get("Content-Type", "")
    duration_ms = (time.perf_counter() - started) * 1000
    try:
        body = json.loads(raw)
    except json.JSONDecodeError as error:
        raise RuntimeError(f"{url} returned invalid JSON: {error}") from error
    if not isinstance(body, dict):
        raise TypeError(f"{url} returned a non-object JSON response")
    return status, body, len(raw), duration_ms, content_type


def _expect(errors: list[str], condition: bool, message: str) -> None:
    if not condition:
        errors.append(message)


def validate_health(
    http_status: int,
    body: dict[str, Any],
    *,
    expected_fund_execution_policy: str | None = None,
    expected_relation_retrieval_status: str | None = None,
) -> list[str]:
    errors: list[str] = []
    _expect(errors, http_status == 200, f"expected HTTP 200, got {http_status}")
    _expect(errors, body.get("status") == "ok", "health status must be ok")
    _expect(
        errors,
        body.get("configured_product_families") == EXPECTED_FAMILIES,
        "configured product families differ",
    )
    _expect(
        errors,
        body.get("ready_product_families") == EXPECTED_FAMILIES,
        "ready product families differ",
    )
    _expect(errors, body.get("missing_product_families") == [], "missing families are present")
    _expect(
        errors,
        body.get("unavailable_product_families") == [],
        "unavailable families are present",
    )
    if expected_fund_execution_policy is not None:
        _expect(
            errors,
            body.get("fund_execution_policy") == expected_fund_execution_policy,
            "fund execution policy differs",
        )
    if expected_relation_retrieval_status is not None:
        _expect(
            errors,
            body.get("relation_retrieval_status") == expected_relation_retrieval_status,
            "relation retrieval status differs",
        )
    return errors


def validate_answer(
    case: SmokeCase,
    http_status: int,
    body: dict[str, Any],
    *,
    success_answer_mode: str = "deterministic",
    provider_model: str | None = None,
) -> list[str]:
    errors: list[str] = []
    _expect(
        errors,
        http_status == case.expected_http_status,
        f"expected HTTP {case.expected_http_status}, got {http_status}",
    )
    expected_request_id = "invalid-request" if case.question is None else case.case_id
    _expect(errors, body.get("request_id") == expected_request_id, "request_id differs")
    _expect(errors, body.get("status") == case.expected_status, "status differs")
    _expect(errors, body.get("intent") == case.expected_intent, "intent differs")
    _expect(
        errors,
        body.get("product_families") == list(case.expected_families),
        "product families differ",
    )
    success_expected = case.expected_status == "success"
    provider_expected = success_expected and case.uses_answer_provider
    expected_answer_mode = success_answer_mode if provider_expected else case.expected_answer_mode
    expected_fallback = provider_expected and success_answer_mode == "deterministic_fallback"
    expected_provider_model = provider_model if provider_expected else None
    _expect(errors, body.get("answer_mode") == expected_answer_mode, "answer mode differs")
    _expect(
        errors,
        body.get("fallback_used") is expected_fallback,
        "fallback flag differs",
    )
    _expect(
        errors,
        body.get("provider_model") == expected_provider_model,
        "model provider differs",
    )

    products = body.get("products")
    citations = body.get("citations")
    if case.expected_status == "success":
        _expect(errors, isinstance(products, list), "products must be an array")
        if isinstance(products, list):
            _expect(
                errors,
                len(products) == case.expected_product_count,
                f"expected {case.expected_product_count} products, got {len(products)}",
            )
        candidate_count = body.get("candidate_count")
        _expect(errors, isinstance(candidate_count, int), "candidate_count must be an integer")
        if isinstance(candidate_count, int):
            _expect(
                errors,
                candidate_count >= case.expected_product_count,
                "candidate_count is smaller than returned products",
            )
        _expect(errors, isinstance(citations, list) and bool(citations), "citations are missing")
        _expect(errors, bool(body.get("as_of_dates")), "as_of_dates are missing")
        manifest = body.get("source_manifest")
        if case.expected_dataset is not None:
            _expect(errors, isinstance(manifest, dict), "source_manifest is missing")
            if isinstance(manifest, dict):
                _expect(
                    errors,
                    manifest.get("dataset") == case.expected_dataset,
                    "dataset differs",
                )
        if case.expected_query_plan_kind is not None:
            query_plan = body.get("query_plan")
            operation = query_plan.get("operation") if isinstance(query_plan, dict) else None
            _expect(
                errors,
                isinstance(operation, dict)
                and operation.get("kind") == case.expected_query_plan_kind,
                "query plan kind differs",
            )
        if case.expected_citation_kind is not None and isinstance(citations, list):
            _expect(
                errors,
                bool(citations)
                and all(
                    isinstance(citation, dict)
                    and citation.get("kind") == case.expected_citation_kind
                    for citation in citations
                ),
                "citation kind differs",
            )
        _expect(errors, body.get("error") is None, "success response contains an error")
        _expect(
            errors,
            body.get("clarification") is None,
            "success response contains clarification",
        )
    else:
        _expect(errors, products == [], "control response contains products")
        _expect(errors, citations == [], "control response contains citations")
        _expect(errors, body.get("candidate_count") is None, "control response was executed")

    clarification = body.get("clarification")
    if case.expected_clarification_code is not None:
        _expect(errors, isinstance(clarification, dict), "clarification details are missing")
        if isinstance(clarification, dict):
            _expect(
                errors,
                clarification.get("code") == case.expected_clarification_code,
                "clarification code differs",
            )
    else:
        _expect(errors, clarification is None, "unexpected clarification details")

    error = body.get("error")
    if case.expected_error_code is not None:
        _expect(errors, isinstance(error, dict), "error details are missing")
        if isinstance(error, dict):
            _expect(errors, error.get("code") == case.expected_error_code, "error code differs")
            _expect(errors, error.get("retryable") is False, "invalid input cannot be retryable")
    else:
        _expect(errors, error is None, "unexpected error details")
    return errors


def validate_official_answer(
    http_status: int,
    body: dict[str, Any],
    *,
    content_type: str = "application/json; charset=utf-8",
    question_id: str,
    question: str,
    expected_control_code: str | None = None,
    expected_trace_status: str | None = None,
    expected_intent: str | None = None,
    expected_families: tuple[str, ...] = (),
    expected_evidence: bool = False,
    expected_empty_context: bool = False,
    expected_execution_step: str | None = None,
    expected_citation_kind: str | None = None,
    expected_relation_type: str | None = None,
    expected_relation_query: str | None = None,
    expected_relation_top_k: int | None = None,
    expected_product_count: int | None = None,
    expected_relation_field: str | None = None,
    expected_relation_value: str | None = None,
    expected_candidate_count: int | None = None,
    require_safe_control_response: bool = False,
    forbidden_execution_steps: tuple[str, ...] = (),
    forbidden_output_fragments: tuple[str, ...] = (),
) -> list[str]:
    errors: list[str] = []
    expected_keys = {
        "question_id",
        "question",
        "retrieved_context",
        "think_trace",
        "answer",
    }
    _expect(errors, http_status == 200, f"official answer expected HTTP 200, got {http_status}")
    _expect(
        errors,
        content_type.casefold().replace(" ", "") == "application/json;charset=utf-8",
        f"official Content-Type differs: {content_type!r}",
    )
    _expect(errors, set(body) == expected_keys, "official answer fields differ")
    _expect(errors, body.get("question_id") == question_id, "official question_id differs")
    _expect(errors, body.get("question") == question, "official question differs")
    _expect(
        errors,
        all(isinstance(body.get(key), str) for key in expected_keys),
        "official answer fields must all be strings",
    )
    decoded_fields: dict[str, dict[str, Any]] = {}
    for key in ("retrieved_context", "think_trace"):
        value = body.get(key)
        if not isinstance(value, str):
            continue
        try:
            decoded = json.loads(value)
        except json.JSONDecodeError:
            errors.append(f"official {key} is not valid JSON text")
        else:
            _expect(errors, isinstance(decoded, dict), f"official {key} must encode an object")
            if isinstance(decoded, dict):
                decoded_fields[key] = decoded
    if expected_control_code is not None:
        trace = decoded_fields.get("think_trace", {})
        if expected_trace_status is None:
            _expect(errors, trace.get("status") == "error", "official control status differs")
        _expect(
            errors,
            trace.get("control_code") == expected_control_code,
            "official control code differs",
        )
    trace = decoded_fields.get("think_trace", {})
    if expected_trace_status is not None:
        _expect(
            errors,
            trace.get("status") == expected_trace_status,
            "official trace status differs",
        )
    if expected_intent is not None:
        _expect(errors, trace.get("intent") == expected_intent, "official intent differs")
    if expected_families:
        _expect(
            errors,
            trace.get("product_families") == list(expected_families),
            "official product families differ",
        )
    if expected_execution_step is not None:
        execution_steps = trace.get("execution_steps")
        _expect(
            errors,
            isinstance(execution_steps, list) and expected_execution_step in execution_steps,
            "official execution step differs",
        )
    execution_steps = trace.get("execution_steps")
    if require_safe_control_response:
        _expect(
            errors,
            isinstance(execution_steps, list) and "safe_control_response" in execution_steps,
            "official safe control step is missing",
        )
    if forbidden_execution_steps:
        _expect(
            errors,
            isinstance(execution_steps, list)
            and not any(step in execution_steps for step in forbidden_execution_steps),
            "official control response contains a forbidden execution step",
        )
    if any(
        value is not None
        for value in (
            expected_relation_type,
            expected_relation_query,
            expected_relation_top_k,
        )
    ):
        filters = trace.get("filters")
        expected_filter = {
            "relation_type": expected_relation_type,
            "query": expected_relation_query,
            "top_k": expected_relation_top_k,
        }
        _expect(
            errors,
            filters == [expected_filter],
            "official relation filter differs",
        )
    if expected_candidate_count is not None:
        _expect(
            errors,
            trace.get("candidate_count") == expected_candidate_count,
            "official candidate count differs",
        )
    context = decoded_fields.get("retrieved_context", {})
    if expected_evidence:
        evidence = context.get("evidence")
        _expect(errors, isinstance(evidence, dict), "official evidence is missing")
        if isinstance(evidence, dict):
            _expect(
                errors,
                any(
                    evidence.get(key)
                    for key in ("products", "comparisons", "aggregates", "documents")
                ),
                "official evidence is empty",
            )
        _expect(errors, bool(context.get("citations")), "official citations are empty")
    if expected_citation_kind is not None:
        citations = context.get("citations")
        _expect(
            errors,
            isinstance(citations, list)
            and bool(citations)
            and all(
                isinstance(citation, dict) and citation.get("kind") == expected_citation_kind
                for citation in citations
            ),
            "official citation kind differs",
        )
    if expected_product_count is not None:
        evidence = context.get("evidence")
        products = evidence.get("products") if isinstance(evidence, dict) else None
        if expected_product_count == 0 and expected_empty_context:
            _expect(
                errors,
                products is None,
                "official not-found context unexpectedly contains products",
            )
        else:
            _expect(
                errors,
                isinstance(products, list) and len(products) == expected_product_count,
                "official relation product count differs",
            )
    if expected_relation_field is not None or expected_relation_value is not None:
        evidence = context.get("evidence")
        products = evidence.get("products") if isinstance(evidence, dict) else None
        relation_fields = []
        if isinstance(products, list):
            for product in products:
                if not isinstance(product, dict):
                    continue
                fields = product.get("fields")
                if isinstance(fields, list):
                    relation_fields.extend(field for field in fields if isinstance(field, dict))
        _expect(
            errors,
            bool(relation_fields)
            and all(
                field.get("field") == expected_relation_field
                and field.get("value") == expected_relation_value
                for field in relation_fields
            ),
            "official relation field evidence differs",
        )
    if expected_empty_context:
        _expect(errors, context.get("citations") == [], "official control context has citations")
        _expect(errors, "evidence" not in context, "official control context has evidence")
    for fragment in forbidden_output_fragments:
        for key in ("retrieved_context", "think_trace", "answer"):
            value = body.get(key)
            if isinstance(value, str):
                _expect(
                    errors,
                    fragment not in value,
                    f"official {key} reflected a forbidden input fragment",
                )
    return errors


def _case_summary(
    case: SmokeCase,
    *,
    http_status: int,
    body: dict[str, Any],
    response_bytes: int,
    duration_ms: float,
    errors: list[str],
) -> dict[str, Any]:
    products = body.get("products")
    citations = body.get("citations")
    return {
        "case_id": case.case_id,
        "passed": not errors,
        "http_status": http_status,
        "status": body.get("status"),
        "intent": body.get("intent"),
        "product_families": body.get("product_families"),
        "candidate_count": body.get("candidate_count"),
        "product_count": len(products) if isinstance(products, list) else None,
        "citation_count": len(citations) if isinstance(citations, list) else None,
        "answer_mode": body.get("answer_mode"),
        "fallback_used": body.get("fallback_used"),
        "provider_model": body.get("provider_model"),
        "response_bytes": response_bytes,
        "duration_ms": round(duration_ms, 3),
        "errors": errors,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run the real Docker HTTP smoke contract.")
    parser.add_argument("--base-url", default="http://127.0.0.1:18001")
    parser.add_argument("--timeout", type=float, default=30.0)
    parser.add_argument(
        "--success-answer-mode",
        choices=("deterministic", "llm_grounded", "deterministic_fallback"),
        default="deterministic",
    )
    parser.add_argument("--provider-model")
    parser.add_argument(
        "--expected-fund-execution-policy",
        choices=("locked", "public_fund_v1_approved"),
        default="locked",
    )
    parser.add_argument(
        "--expected-relation-retrieval-status",
        choices=("disabled", "ready"),
        default="ready",
    )
    parser.add_argument("--output", type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    arguments = parser.parse_args(argv)
    if arguments.success_answer_mode == "deterministic" and arguments.provider_model is not None:
        parser.error("--provider-model is not valid for deterministic answers")
    if arguments.success_answer_mode != "deterministic" and not arguments.provider_model:
        parser.error("LLM answer modes require --provider-model")
    base_url = arguments.base_url.rstrip("/")
    cases = smoke_cases(
        arguments.expected_fund_execution_policy,
        arguments.expected_relation_retrieval_status,
    )
    started_at = datetime.now(UTC)
    try:
        health_status, health_body, health_bytes, health_duration, _ = _request_json(
            f"{base_url}/health",
            timeout=arguments.timeout,
        )
        health_errors = validate_health(
            health_status,
            health_body,
            expected_fund_execution_policy=arguments.expected_fund_execution_policy,
            expected_relation_retrieval_status=(arguments.expected_relation_retrieval_status),
        )
        results = []
        for case in cases:
            http_status, body, response_bytes, duration_ms, _ = _request_json(
                f"{base_url}/answer",
                timeout=arguments.timeout,
                payload=case.payload(),
            )
            results.append(
                _case_summary(
                    case,
                    http_status=http_status,
                    body=body,
                    response_bytes=response_bytes,
                    duration_ms=duration_ms,
                    errors=validate_answer(
                        case,
                        http_status,
                        body,
                        success_answer_mode=arguments.success_answer_mode,
                        provider_model=arguments.provider_model,
                    ),
                )
            )
        official_results = []
        for case in official_smoke_cases(arguments.expected_relation_retrieval_status):
            (
                official_status,
                official_body,
                official_bytes,
                official_duration,
                official_content_type,
            ) = _request_json(
                f"{base_url}/answer?{case.query_string()}",
                timeout=arguments.timeout,
            )
            official_errors = validate_official_answer(
                official_status,
                official_body,
                content_type=official_content_type,
                question_id=case.expected_question_id,
                question=case.expected_question,
                expected_control_code=case.expected_control_code,
                expected_trace_status=case.expected_trace_status,
                expected_intent=case.expected_intent,
                expected_families=case.expected_families,
                expected_evidence=case.expected_evidence,
                expected_empty_context=case.expected_empty_context,
                expected_execution_step=case.expected_execution_step,
                expected_citation_kind=case.expected_citation_kind,
                expected_relation_type=case.expected_relation_type,
                expected_relation_query=case.expected_relation_query,
                expected_relation_top_k=case.expected_relation_top_k,
                expected_product_count=case.expected_product_count,
                expected_relation_field=case.expected_relation_field,
                expected_relation_value=case.expected_relation_value,
                expected_candidate_count=case.expected_candidate_count,
                require_safe_control_response=case.require_safe_control_response,
                forbidden_execution_steps=case.forbidden_execution_steps,
                forbidden_output_fragments=case.forbidden_output_fragments,
            )
            official_results.append(
                {
                    "case_id": case.case_id,
                    "passed": not official_errors,
                    "http_status": official_status,
                    "content_type": official_content_type,
                    "response_bytes": official_bytes,
                    "duration_ms": round(official_duration, 3),
                    "errors": official_errors,
                }
            )
    except (OSError, RuntimeError, TypeError, URLError) as error:
        print(f"Docker HTTP smoke failed before completion: {error}", file=sys.stderr)
        return 2

    passed_cases = sum(item["passed"] for item in results)
    report = {
        "schema_version": "1.1",
        "suite_id": "docker-http-smoke-v3",
        "started_at": started_at.isoformat(),
        "base_url": base_url,
        "llm_provider_expected": arguments.provider_model is not None,
        "expected_success_answer_mode": arguments.success_answer_mode,
        "expected_provider_model": arguments.provider_model,
        "expected_fund_execution_policy": arguments.expected_fund_execution_policy,
        "health": {
            "passed": not health_errors,
            "http_status": health_status,
            "ready_product_families": health_body.get("ready_product_families"),
            "response_bytes": health_bytes,
            "duration_ms": round(health_duration, 3),
            "errors": health_errors,
        },
        "official_answers": official_results,
        "metrics": {
            "backend_passed": passed_cases,
            "backend_failed": len(cases) - passed_cases,
            "backend_total": len(cases),
            "official_passed": sum(item["passed"] for item in official_results),
            "official_failed": sum(not item["passed"] for item in official_results),
            "official_total": len(official_results),
        },
        "cases": results,
        "passed": (
            not health_errors
            and passed_cases == len(cases)
            and all(item["passed"] for item in official_results)
        ),
    }
    rendered = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    if arguments.output is not None:
        arguments.output.parent.mkdir(parents=True, exist_ok=True)
        arguments.output.write_text(rendered, encoding="utf-8")
    print(rendered, end="")
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
