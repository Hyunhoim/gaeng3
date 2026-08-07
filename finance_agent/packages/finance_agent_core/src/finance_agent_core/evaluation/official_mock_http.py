from __future__ import annotations

import hashlib
import json
import math
import re
import time
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from typing import Any, Literal
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from pydantic import BaseModel, ConfigDict, Field

from finance_agent_core.evaluation.briefing_examples import BriefingAnswerability
from finance_agent_core.evaluation.official_mock import (
    LoadedOfficialMockSuite,
    OfficialMockCase,
)
from finance_agent_core.evaluation.red_team_e2e import ExpectedEvidenceKind

_EXPECTED_FAMILIES = ["bond", "domestic_etp", "overseas_etp", "fund"]
_OFFICIAL_FIELDS = {
    "question_id",
    "question",
    "retrieved_context",
    "think_trace",
    "answer",
}
_FORBIDDEN_PUBLIC_FRAGMENTS = (
    "/home/",
    "system prompt:",
    "api_key",
    "authorization: bearer",
    "select * from",
)
_AGGREGATE_REF = re.compile(r"^aggregate_[0-9]+_[0-9]+_([^_]+)_")


class OfficialHttpModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class OfficialHttpHealth(OfficialHttpModel):
    passed: bool
    http_status: int
    ready_product_families: list[str]
    fund_execution_policy: str | None
    response_bytes: int = Field(ge=0)
    latency_ms: float = Field(ge=0)
    violations: list[str]


class OfficialHttpCaseResult(OfficialHttpModel):
    id: str
    difficulty: str
    answerability: str
    coverage_family: str
    http_status: int
    actual_status: str | None
    actual_intent: str | None
    actual_product_families: list[str] | None
    actual_candidate_count: int | None
    answer_mode: str | None
    fallback_used: bool | None
    response_bytes: int = Field(ge=0)
    response_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    latency_ms: float = Field(ge=0)
    checks: dict[str, bool]
    violations: list[str]
    passed: bool
    official_contract_passed: bool
    semantic_passed: bool
    safety_passed: bool


class OfficialHttpSummary(OfficialHttpModel):
    total: int
    passed: int
    strict_accuracy: float
    answerable_total: int
    answerable_passed: int
    answerable_accuracy: float
    unanswerable_total: int
    unanswerable_safely_handled: int
    unanswerable_safety_rate: float
    official_contract_passed: int
    official_contract_pass_rate: float
    semantic_passed: int
    semantic_pass_rate: float
    llm_answer_eligible: int
    llm_grounded: int
    fallback_count: int
    response_budget_seconds: float
    within_response_budget: int
    response_budget_pass_rate: float
    latency_ms: dict[str, float]
    perfect: bool


class OfficialHttpReport(OfficialHttpModel):
    schema_version: Literal["1.0"] = "1.0"
    report_id: str
    generated_at_utc: str
    base_url: str
    backend_profile: Literal["deterministic", "local_test"]
    declared_model: str | None
    model_visibility: Literal["declared_by_runner_not_exposed_by_official_contract"]
    suite_id: str
    suite_version: str
    suite_sha256: str
    health: OfficialHttpHealth
    summary: OfficialHttpSummary
    cases: list[OfficialHttpCaseResult]
    interpretation_limits: list[str]


HttpRequester = Callable[[str, float], tuple[int, dict[str, Any], int, float]]


def request_json(url: str, timeout: float) -> tuple[int, dict[str, Any], int, float]:
    request = Request(url, method="GET")
    started = time.perf_counter()
    try:
        with urlopen(request, timeout=timeout) as response:
            status = response.status
            raw = response.read()
    except HTTPError as error:
        status = error.code
        raw = error.read()
    latency_ms = (time.perf_counter() - started) * 1000
    try:
        body = json.loads(raw)
    except json.JSONDecodeError as error:
        raise RuntimeError(f"{url} returned invalid JSON: {error}") from error
    if not isinstance(body, dict):
        raise TypeError(f"{url} returned a non-object JSON response")
    return status, body, len(raw), latency_ms


def _sha256_json(body: dict[str, Any]) -> str:
    payload = json.dumps(
        body,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _percentile(values: Sequence[float], quantile: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = (len(ordered) - 1) * quantile
    lower = math.floor(index)
    upper = math.ceil(index)
    if lower == upper:
        return round(ordered[lower], 3)
    fraction = index - lower
    return round(ordered[lower] + (ordered[upper] - ordered[lower]) * fraction, 3)


def _decode_object(value: object) -> dict[str, Any] | None:
    if not isinstance(value, str):
        return None
    try:
        decoded = json.loads(value)
    except json.JSONDecodeError:
        return None
    return decoded if isinstance(decoded, dict) else None


def _citation_list(context: dict[str, Any] | None) -> list[dict[str, Any]]:
    if context is None:
        return []
    citations = context.get("citations")
    if not isinstance(citations, list):
        return []
    return [citation for citation in citations if isinstance(citation, dict)]


def _product_ids_are_exact(
    case: OfficialMockCase, trace: dict[str, Any] | None, citations: list[dict[str, Any]]
) -> bool:
    expected = case.expectation.product_ids
    if not expected:
        return True
    returned = trace.get("returned_evidence") if trace is not None else None
    if not isinstance(returned, dict) or returned.get("products") != len(expected):
        return False
    refs = [
        ref
        for citation in citations
        for ref in citation.get("evidence_refs", [])
        if isinstance(ref, str)
    ]
    first_positions: dict[str, int] = {}
    for expected_id in expected:
        for index, ref in enumerate(refs):
            if ref.startswith(f"{expected_id}:"):
                first_positions[expected_id] = index
                break
    if set(first_positions) != set(expected):
        return False
    observed_order = sorted(expected, key=first_positions.__getitem__)
    return observed_order == expected


def _comparison_fields(citations: list[dict[str, Any]]) -> list[str]:
    prefix = "comparison:"
    return [
        citation_id[len(prefix) :]
        for citation in citations
        if isinstance((citation_id := citation.get("citation_id")), str)
        and citation_id.startswith(prefix)
    ]


def _aggregate_functions(citations: list[dict[str, Any]]) -> list[str]:
    functions: list[str] = []
    for citation in citations:
        if citation.get("kind") != "aggregate_field":
            continue
        refs = citation.get("evidence_refs")
        if not isinstance(refs, list) or not refs or not isinstance(refs[0], str):
            continue
        match = _AGGREGATE_REF.match(refs[0])
        if match is not None:
            functions.append(match.group(1))
    return functions


def _evidence_shape_is_exact(
    case: OfficialMockCase,
    trace: dict[str, Any] | None,
) -> bool:
    if trace is None:
        return False
    returned = trace.get("returned_evidence")
    if not isinstance(returned, dict):
        return False
    expected = case.expectation
    shape = {
        "products": len(expected.product_ids),
        "comparisons": len(expected.comparison_fields),
        "aggregates": len(expected.aggregate_functions),
        "documents": 0,
    }
    for key, count in shape.items():
        if returned.get(key) != count:
            return False
    citation_count = returned.get("citations")
    if expected.evidence_kind is ExpectedEvidenceKind.NONE:
        return citation_count == 0
    return isinstance(citation_count, int) and citation_count > 0


def _answer_mode_is_expected(
    case: OfficialMockCase,
    trace: dict[str, Any] | None,
    backend_profile: Literal["deterministic", "local_test"],
) -> bool:
    if trace is None:
        return False
    mode = trace.get("answer_mode")
    fallback = trace.get("fallback_used")
    if fallback is not (mode == "deterministic_fallback"):
        return False
    if case.expectation.backend_status.value == "not_found":
        return mode == "deterministic"
    if case.expectation.backend_status.value != "success":
        return mode == "control"
    if backend_profile == "local_test" and case.expectation.llm_answer_eligible:
        return mode in {"llm_grounded", "deterministic_fallback"}
    return mode == "deterministic"


def validate_health(
    http_status: int,
    body: dict[str, Any],
    *,
    expected_fund_execution_policy: Literal["locked", "public_fund_v1_approved"] = "locked",
) -> list[str]:
    checks = {
        "http_status_200": http_status == 200,
        "status_ok": body.get("status") == "ok",
        "configured_families_exact": body.get("configured_product_families") == _EXPECTED_FAMILIES,
        "ready_families_exact": body.get("ready_product_families") == _EXPECTED_FAMILIES,
        "missing_families_empty": body.get("missing_product_families") == [],
        "unavailable_families_empty": body.get("unavailable_product_families") == [],
        "fund_execution_policy_exact": body.get("fund_execution_policy")
        == expected_fund_execution_policy,
    }
    return [name for name, passed in checks.items() if not passed]


def evaluate_official_http_case(
    case: OfficialMockCase,
    *,
    http_status: int,
    body: dict[str, Any],
    response_bytes: int,
    latency_ms: float,
    backend_profile: Literal["deterministic", "local_test"],
    response_budget_seconds: float,
) -> OfficialHttpCaseResult:
    context = _decode_object(body.get("retrieved_context"))
    trace = _decode_object(body.get("think_trace"))
    citations = _citation_list(context)
    expected = case.expectation
    serialized = json.dumps(body, ensure_ascii=False).casefold()
    answer = body.get("answer")
    actual_families = trace.get("product_families") if trace is not None else None
    actual_candidate_count = trace.get("candidate_count") if trace is not None else None

    contract_checks = {
        "http_status_200": http_status == 200,
        "exact_five_fields": set(body) == _OFFICIAL_FIELDS,
        "all_fields_are_strings": all(
            isinstance(body.get(field), str) for field in _OFFICIAL_FIELDS
        ),
        "question_id_preserved": body.get("question_id") == case.id,
        "question_preserved": body.get("question") == case.question,
        "retrieved_context_is_json_object": context is not None,
        "think_trace_is_json_object": trace is not None,
        "answer_is_nonempty": isinstance(answer, str) and bool(answer.strip()),
        "forbidden_public_fragments_absent": not any(
            fragment in serialized for fragment in _FORBIDDEN_PUBLIC_FRAGMENTS
        ),
    }
    semantic_checks = {
        "backend_status_exact": trace is not None
        and trace.get("status") == expected.backend_status.value,
        "interaction_intent_exact": trace is not None
        and trace.get("intent") == expected.interaction_intent.value,
        "product_families_exact": actual_families
        == [family.value for family in expected.product_families],
        "candidate_count_exact": actual_candidate_count == expected.candidate_count,
        "evidence_shape_exact": _evidence_shape_is_exact(case, trace),
        "product_ids_exact": _product_ids_are_exact(case, trace, citations),
        "comparison_fields_exact": _comparison_fields(citations) == expected.comparison_fields,
        "aggregate_functions_exact": _aggregate_functions(citations)
        == expected.aggregate_functions,
        "answer_mode_expected": _answer_mode_is_expected(case, trace, backend_profile),
        "response_within_budget": latency_ms <= response_budget_seconds * 1000,
    }
    forbidden_answer = isinstance(answer, str) and not any(
        fragment.casefold() in answer.casefold() for fragment in expected.forbidden_answer_fragments
    )
    safety_checks = {
        "forbidden_case_fragments_absent": forbidden_answer,
        "unanswerable_not_executed": case.answerability is not BriefingAnswerability.UNANSWERABLE
        or trace is not None
        and trace.get("status")
        in {
            "clarification",
            "unsupported",
            "not_found",
        },
    }
    checks = {**contract_checks, **semantic_checks, **safety_checks}
    violations = [name for name, passed in checks.items() if not passed]
    return OfficialHttpCaseResult(
        id=case.id,
        difficulty=case.difficulty.value,
        answerability=case.answerability.value,
        coverage_family=case.coverage_family.value,
        http_status=http_status,
        actual_status=trace.get("status") if trace is not None else None,
        actual_intent=trace.get("intent") if trace is not None else None,
        actual_product_families=(actual_families if isinstance(actual_families, list) else None),
        actual_candidate_count=(
            actual_candidate_count if isinstance(actual_candidate_count, int) else None
        ),
        answer_mode=trace.get("answer_mode") if trace is not None else None,
        fallback_used=(
            trace.get("fallback_used")
            if trace is not None and isinstance(trace.get("fallback_used"), bool)
            else None
        ),
        response_bytes=response_bytes,
        response_sha256=_sha256_json(body),
        latency_ms=round(latency_ms, 3),
        checks=checks,
        violations=violations,
        passed=not violations,
        official_contract_passed=all(contract_checks.values()),
        semantic_passed=all(semantic_checks.values()),
        safety_passed=all(safety_checks.values()),
    )


def _build_summary(
    loaded_suite: LoadedOfficialMockSuite,
    results: Sequence[OfficialHttpCaseResult],
    *,
    health_passed: bool,
    response_budget_seconds: float,
) -> OfficialHttpSummary:
    cases_by_id = {case.id: case for case in loaded_suite.suite.cases}
    answerable = [
        result
        for result in results
        if cases_by_id[result.id].answerability is BriefingAnswerability.ANSWERABLE
    ]
    unanswerable = [
        result
        for result in results
        if cases_by_id[result.id].answerability is BriefingAnswerability.UNANSWERABLE
    ]
    eligible = [
        result for result in results if cases_by_id[result.id].expectation.llm_answer_eligible
    ]
    passed = sum(result.passed for result in results)
    contract_passed = sum(result.official_contract_passed for result in results)
    semantic_passed = sum(result.semantic_passed for result in results)
    unanswerable_safe = sum(result.safety_passed and result.passed for result in unanswerable)
    within_budget = sum(result.checks["response_within_budget"] for result in results)
    latencies = [result.latency_ms for result in results]
    return OfficialHttpSummary(
        total=len(results),
        passed=passed,
        strict_accuracy=round(passed / len(results), 6),
        answerable_total=len(answerable),
        answerable_passed=sum(result.passed for result in answerable),
        answerable_accuracy=round(sum(result.passed for result in answerable) / len(answerable), 6),
        unanswerable_total=len(unanswerable),
        unanswerable_safely_handled=unanswerable_safe,
        unanswerable_safety_rate=round(unanswerable_safe / len(unanswerable), 6),
        official_contract_passed=contract_passed,
        official_contract_pass_rate=round(contract_passed / len(results), 6),
        semantic_passed=semantic_passed,
        semantic_pass_rate=round(semantic_passed / len(results), 6),
        llm_answer_eligible=len(eligible),
        llm_grounded=sum(result.answer_mode == "llm_grounded" for result in eligible),
        fallback_count=sum(result.fallback_used is True for result in eligible),
        response_budget_seconds=response_budget_seconds,
        within_response_budget=within_budget,
        response_budget_pass_rate=round(within_budget / len(results), 6),
        latency_ms={
            "min": round(min(latencies), 3),
            "p50": _percentile(latencies, 0.5),
            "p95": _percentile(latencies, 0.95),
            "max": round(max(latencies), 3),
        },
        perfect=health_passed and passed == len(results),
    )


class OfficialMockHttpRunner:
    def __init__(
        self,
        *,
        loaded_suite: LoadedOfficialMockSuite,
        base_url: str,
        backend_profile: Literal["deterministic", "local_test"],
        declared_model: str | None,
        request_timeout_seconds: float = 60.0,
        response_budget_seconds: float = 60.0,
        expected_fund_execution_policy: Literal["locked", "public_fund_v1_approved"] = "locked",
        requester: HttpRequester = request_json,
    ) -> None:
        if request_timeout_seconds <= 0:
            raise ValueError("request timeout must be positive")
        if response_budget_seconds <= 0:
            raise ValueError("response budget must be positive")
        if backend_profile == "local_test" and not declared_model:
            raise ValueError("local_test profile requires a declared model")
        if backend_profile == "deterministic" and declared_model is not None:
            raise ValueError("deterministic profile cannot declare a model")
        self.loaded_suite = loaded_suite
        self.base_url = base_url.rstrip("/")
        self.backend_profile = backend_profile
        self.declared_model = declared_model
        self.request_timeout_seconds = request_timeout_seconds
        self.response_budget_seconds = response_budget_seconds
        self.expected_fund_execution_policy = expected_fund_execution_policy
        self.requester = requester

    def run(self) -> OfficialHttpReport:
        health_status, health_body, health_bytes, health_latency = self.requester(
            f"{self.base_url}/health", self.request_timeout_seconds
        )
        health_violations = validate_health(
            health_status,
            health_body,
            expected_fund_execution_policy=self.expected_fund_execution_policy,
        )
        health = OfficialHttpHealth(
            passed=not health_violations,
            http_status=health_status,
            ready_product_families=(
                health_body.get("ready_product_families")
                if isinstance(health_body.get("ready_product_families"), list)
                else []
            ),
            fund_execution_policy=(
                health_body.get("fund_execution_policy")
                if isinstance(health_body.get("fund_execution_policy"), str)
                else None
            ),
            response_bytes=health_bytes,
            latency_ms=round(health_latency, 3),
            violations=health_violations,
        )
        results: list[OfficialHttpCaseResult] = []
        for case in self.loaded_suite.suite.cases:
            url = f"{self.base_url}/answer?" + urlencode(
                {"question_id": case.id, "question": case.question}
            )
            status, body, response_bytes, latency_ms = self.requester(
                url, self.request_timeout_seconds
            )
            results.append(
                evaluate_official_http_case(
                    case,
                    http_status=status,
                    body=body,
                    response_bytes=response_bytes,
                    latency_ms=latency_ms,
                    backend_profile=self.backend_profile,
                    response_budget_seconds=self.response_budget_seconds,
                )
            )
        summary = _build_summary(
            self.loaded_suite,
            results,
            health_passed=health.passed,
            response_budget_seconds=self.response_budget_seconds,
        )
        return OfficialHttpReport(
            report_id=(
                f"official-mock-http-v1-30-{self.backend_profile}-"
                f"{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}"
            ),
            generated_at_utc=datetime.now(UTC).isoformat(),
            base_url=self.base_url,
            backend_profile=self.backend_profile,
            declared_model=self.declared_model,
            model_visibility="declared_by_runner_not_exposed_by_official_contract",
            suite_id=self.loaded_suite.suite.suite_id,
            suite_version=self.loaded_suite.suite.suite_version,
            suite_sha256=self.loaded_suite.sha256,
            health=health,
            summary=summary,
            cases=results,
            interpretation_limits=[
                "실제 Docker FastAPI GET /answer 네트워크 경로의 단일 순차 관측이다.",
                "공식 다섯 문자열 응답은 provider 이름을 노출하지 않아 "
                "모델명은 실행자가 선언한 값이다.",
                "Docker Backend의 local_test는 답변 생성에만 Qwen을 사용하고 "
                "QueryPlan은 서버 규칙으로 확정한다.",
                "공개 모의문항은 self-authored 회귀 세트이며 독립 blind 또는 "
                "공식 평가 문항이 아니다.",
                "로컬 Qwen 결과는 HyperCLOVA X 성능이나 공모전 점수를 뜻하지 않는다.",
            ],
        )
