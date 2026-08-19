from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Callable, Sequence
from enum import StrEnum
from pathlib import Path
from typing import Any, Literal
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from pydantic import BaseModel, ConfigDict, Field

from finance_agent_core.contracts.queryplan import ProductFamily
from finance_agent_core.evaluation.briefing_examples import (
    BriefingAnswerability,
    LoadedBriefingExampleSuite,
)

FROZEN_BRIEFING_SUITE_SHA256 = "e448b0edda5957145e624aa2a60b97fcc697efc347c49e14074ba833944762c1"
FROZEN_SOURCE_ARTIFACT_SHA256 = "1f06e7dbbbe7505516ff7f7dc0524cd45d86e8005bb49ae248e04526da877437"

_EXPECTED_FAMILIES = ["bond", "domestic_etp", "overseas_etp", "fund"]
_OFFICIAL_FIELDS = {
    "question_id",
    "question",
    "retrieved_context",
    "think_trace",
    "answer",
}
_SAFE_CONTROL_STATUSES = {"clarification", "unsupported", "not_found"}
_FORBIDDEN_PUBLIC_FRAGMENTS = (
    "/home/",
    "system prompt:",
    "api_key",
    "authorization: bearer",
    "select * from",
)
_MALICIOUS_FRAGMENT = "<script>alert(1)</script>"
_LONG_QUESTION = "가" * 2001
_FUND_LOCKED_QUESTION = (
    "당사에서 판매 중인 해외 주식형 공모펀드 중 3개월 수익률이 높은 상품 5개 보여줘."
)


class OfficialAcceptanceModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class OfficialAcceptanceCaseKind(StrEnum):
    PUBLIC_EXAMPLE = "public_example"
    TRANSPORT_EDGE = "transport_edge"


class SourceArtifactStatus(StrEnum):
    VERIFIED = "verified"
    NOT_PROVIDED = "not_provided"


class OfficialAcceptanceCase(OfficialAcceptanceModel):
    id: str = Field(min_length=1, max_length=100)
    kind: OfficialAcceptanceCaseKind
    query: tuple[tuple[str, str], ...] = Field(min_length=1)
    expected_question_id: str
    expected_question: str
    source_example_id: str | None = None
    answerability: BriefingAnswerability | None = None
    expected_control_code: str | None = None
    expected_trace_statuses: tuple[str, ...] = ()
    expected_intents: tuple[str, ...] = ()
    expected_families: tuple[ProductFamily, ...] = ()
    require_evidence: bool = False
    require_no_execution: bool = False
    forbidden_output_fragments: tuple[str, ...] = ()

    def query_string(self) -> str:
        return urlencode(self.query)


class SourceArtifactVerification(OfficialAcceptanceModel):
    artifact_name: str
    expected_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    observed_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    status: SourceArtifactStatus


class OfficialAcceptanceHealth(OfficialAcceptanceModel):
    passed: bool
    http_status: int
    ready_product_families: list[str]
    fund_execution_policy: str | None
    response_bytes: int = Field(ge=0)
    latency_ms: float = Field(ge=0)
    violations: list[str]


class OfficialAcceptanceCaseResult(OfficialAcceptanceModel):
    request_index: int = Field(ge=1)
    id: str
    kind: OfficialAcceptanceCaseKind
    source_example_id: str | None
    answerability: BriefingAnswerability | None
    http_status: int
    content_type: str
    actual_status: str | None
    actual_intent: str | None
    actual_product_families: list[str] | None
    response_bytes: int = Field(ge=0)
    response_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    latency_ms: float = Field(ge=0)
    checks: dict[str, bool]
    violations: list[str]
    contract_passed: bool
    safety_passed: bool
    passed: bool


class OfficialAcceptanceSummary(OfficialAcceptanceModel):
    total: int
    passed: int
    contract_passed: int
    public_examples_total: int
    public_examples_passed: int
    public_answerable_total: int
    public_answerable_success_observed: int
    public_unanswerable_total: int
    public_unanswerable_safely_handled: int
    transport_edge_total: int
    transport_edge_passed: int
    no_execution_total: int
    no_execution_passed: int
    api_perfect: bool
    perfect: bool


class OfficialAcceptanceReport(OfficialAcceptanceModel):
    schema_version: Literal["1.0"] = "1.0"
    report_id: Literal["official-acceptance-p0-4-v1"] = "official-acceptance-p0-4-v1"
    generated_at_utc: str
    base_url: str
    implementation_commit: str = Field(pattern=r"^[0-9a-f]{7,40}$")
    runtime_image_reference: str | None = Field(default=None, min_length=1, max_length=500)
    request_contract: Literal["unauthenticated_sequential_get"] = "unauthenticated_sequential_get"
    suite_id: str
    suite_version: str
    suite_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_title: str
    source_interpretation: str
    source_artifact: SourceArtifactVerification
    health: OfficialAcceptanceHealth
    summary: OfficialAcceptanceSummary
    cases: list[OfficialAcceptanceCaseResult]
    interpretation_limits: list[str]


HttpRequester = Callable[
    [str, float],
    tuple[int, dict[str, Any], int, float, str],
]


def sha256_file(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def request_json(url: str, timeout: float) -> tuple[int, dict[str, Any], int, float, str]:
    request = Request(url, method="GET")
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
    latency_ms = (time.perf_counter() - started) * 1000
    try:
        body = json.loads(raw)
    except json.JSONDecodeError as error:
        raise RuntimeError(f"{url} returned invalid JSON: {error}") from error
    if not isinstance(body, dict):
        raise TypeError(f"{url} returned a non-object JSON response")
    return status, body, len(raw), latency_ms, content_type


def _public_example_cases(
    loaded_suite: LoadedBriefingExampleSuite,
) -> tuple[OfficialAcceptanceCase, ...]:
    cases: list[OfficialAcceptanceCase] = []
    for example in loaded_suite.suite.cases:
        policy_locked = example.target_families == [ProductFamily.FUND]
        safe_control = example.answerability is BriefingAnswerability.UNANSWERABLE or policy_locked
        if policy_locked:
            statuses = ("unsupported",)
            intents = ("unsupported",)
        elif example.answerability is BriefingAnswerability.UNANSWERABLE:
            statuses = tuple(sorted(_SAFE_CONTROL_STATUSES))
            intents = tuple(intent.value for intent in example.expected_interaction_intents)
        else:
            statuses = ()
            intents = ()
        cases.append(
            OfficialAcceptanceCase(
                id=f"acceptance-{example.id}",
                kind=OfficialAcceptanceCaseKind.PUBLIC_EXAMPLE,
                query=(("question_id", example.id), ("question", example.question)),
                expected_question_id=example.id,
                expected_question=example.question,
                source_example_id=example.id,
                answerability=example.answerability,
                expected_trace_statuses=statuses,
                expected_intents=intents,
                expected_families=(tuple(example.target_families) if policy_locked else ()),
                require_no_execution=safe_control,
            )
        )
    return tuple(cases)


def build_official_acceptance_transport_cases() -> tuple[OfficialAcceptanceCase, ...]:
    return (
        OfficialAcceptanceCase(
            id="official-valid",
            kind=OfficialAcceptanceCaseKind.TRANSPORT_EDGE,
            query=(
                ("question_id", "docker-smoke-official-001"),
                ("question", "현재 판매 가능한 원화채권 중 AA- 이상 종목 알려줘"),
                ("unexpected", "ignored"),
            ),
            expected_question_id="docker-smoke-official-001",
            expected_question="현재 판매 가능한 원화채권 중 AA- 이상 종목 알려줘",
            require_evidence=True,
        ),
        OfficialAcceptanceCase(
            id="official-unicode-and-markup",
            kind=OfficialAcceptanceCaseKind.TRANSPORT_EDGE,
            query=(
                ("question_id", "평가-😀-001"),
                ("question", f"내일 가장 오를 ETF를 예측해줘 & {_MALICIOUS_FRAGMENT}"),
            ),
            expected_question_id="평가-😀-001",
            expected_question=f"내일 가장 오를 ETF를 예측해줘 & {_MALICIOUS_FRAGMENT}",
            expected_trace_statuses=("unsupported",),
            expected_intents=("unsupported",),
            require_no_execution=True,
            forbidden_output_fragments=(_MALICIOUS_FRAGMENT,),
        ),
        OfficialAcceptanceCase(
            id="official-fund-locked",
            kind=OfficialAcceptanceCaseKind.TRANSPORT_EDGE,
            query=(
                ("question_id", "docker-smoke-official-fund-locked-001"),
                ("question", _FUND_LOCKED_QUESTION),
            ),
            expected_question_id="docker-smoke-official-fund-locked-001",
            expected_question=_FUND_LOCKED_QUESTION,
            expected_trace_statuses=("unsupported",),
            expected_intents=("unsupported",),
            expected_families=(ProductFamily.FUND,),
            require_no_execution=True,
        ),
        OfficialAcceptanceCase(
            id="official-blank-values",
            kind=OfficialAcceptanceCaseKind.TRANSPORT_EDGE,
            query=(("question_id", " "), ("question", " ")),
            expected_question_id="invalid-question-id",
            expected_question=" ",
            expected_control_code="invalid_request",
            expected_trace_statuses=("error",),
            require_no_execution=True,
        ),
        OfficialAcceptanceCase(
            id="official-missing-id",
            kind=OfficialAcceptanceCaseKind.TRANSPORT_EDGE,
            query=(("question", "해외 ETF를 알려줘"),),
            expected_question_id="invalid-question-id",
            expected_question="해외 ETF를 알려줘",
            expected_control_code="invalid_request",
            expected_trace_statuses=("error",),
            require_no_execution=True,
        ),
        OfficialAcceptanceCase(
            id="official-missing-question",
            kind=OfficialAcceptanceCaseKind.TRANSPORT_EDGE,
            query=(("question_id", "Q-MISSING"),),
            expected_question_id="Q-MISSING",
            expected_question="",
            expected_control_code="invalid_request",
            expected_trace_statuses=("error",),
            require_no_execution=True,
        ),
        OfficialAcceptanceCase(
            id="official-id-too-long",
            kind=OfficialAcceptanceCaseKind.TRANSPORT_EDGE,
            query=(("question_id", "Q" * 129), ("question", "국내채권을 알려줘")),
            expected_question_id="invalid-question-id",
            expected_question="국내채권을 알려줘",
            expected_control_code="invalid_request",
            expected_trace_statuses=("error",),
            require_no_execution=True,
        ),
        OfficialAcceptanceCase(
            id="official-question-too-long",
            kind=OfficialAcceptanceCaseKind.TRANSPORT_EDGE,
            query=(("question_id", "Q-LONG"), ("question", _LONG_QUESTION)),
            expected_question_id="Q-LONG",
            expected_question=_LONG_QUESTION[:2000],
            expected_control_code="invalid_request",
            expected_trace_statuses=("error",),
            require_no_execution=True,
        ),
    )


def build_official_acceptance_cases(
    loaded_suite: LoadedBriefingExampleSuite,
) -> tuple[OfficialAcceptanceCase, ...]:
    if loaded_suite.sha256 != FROZEN_BRIEFING_SUITE_SHA256:
        raise ValueError(
            "briefing example suite SHA-256 differs: "
            f"expected {FROZEN_BRIEFING_SUITE_SHA256}, got {loaded_suite.sha256}"
        )
    if loaded_suite.suite.source.sha256 != FROZEN_SOURCE_ARTIFACT_SHA256:
        raise ValueError(
            "briefing source artifact SHA-256 differs: "
            f"expected {FROZEN_SOURCE_ARTIFACT_SHA256}, "
            f"got {loaded_suite.suite.source.sha256}"
        )
    return _public_example_cases(loaded_suite) + build_official_acceptance_transport_cases()


def _decode_object(value: object) -> dict[str, Any] | None:
    if not isinstance(value, str):
        return None
    try:
        decoded = json.loads(value)
    except json.JSONDecodeError:
        return None
    return decoded if isinstance(decoded, dict) else None


def _response_sha256(body: dict[str, Any]) -> str:
    payload = json.dumps(
        body,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _returned_evidence_is_empty(trace: dict[str, Any] | None) -> bool:
    if trace is None:
        return False
    returned = trace.get("returned_evidence")
    if returned is None:
        return True
    if not isinstance(returned, dict):
        return False
    return all(
        returned.get(key, 0) == 0
        for key in ("products", "comparisons", "aggregates", "documents", "citations")
    )


def _context_is_empty(context: dict[str, Any] | None) -> bool:
    if context is None:
        return False
    citations = context.get("citations")
    evidence = context.get("evidence")
    return citations == [] and (evidence is None or evidence == {})


def evaluate_official_acceptance_case(
    case: OfficialAcceptanceCase,
    *,
    request_index: int,
    http_status: int,
    body: dict[str, Any],
    response_bytes: int,
    latency_ms: float,
    content_type: str,
) -> OfficialAcceptanceCaseResult:
    context = _decode_object(body.get("retrieved_context"))
    trace = _decode_object(body.get("think_trace"))
    public_payload = json.dumps(
        {
            "retrieved_context": body.get("retrieved_context"),
            "think_trace": body.get("think_trace"),
            "answer": body.get("answer"),
        },
        ensure_ascii=False,
    ).casefold()
    answer = body.get("answer")
    contract_checks = {
        "http_status_200": http_status == 200,
        "content_type_utf8": content_type.casefold().replace(" ", "")
        == "application/json;charset=utf-8",
        "exact_five_fields": set(body) == _OFFICIAL_FIELDS,
        "all_fields_are_strings": all(
            isinstance(body.get(field), str) for field in _OFFICIAL_FIELDS
        ),
        "question_id_preserved": body.get("question_id") == case.expected_question_id,
        "question_preserved": body.get("question") == case.expected_question,
        "retrieved_context_is_json_object": context is not None,
        "think_trace_is_json_object": trace is not None,
        "answer_is_nonempty": isinstance(answer, str) and bool(answer.strip()),
        "global_forbidden_fragments_absent": not any(
            fragment in public_payload for fragment in _FORBIDDEN_PUBLIC_FRAGMENTS
        ),
    }
    expectation_checks: dict[str, bool] = {}
    if case.expected_control_code is not None:
        expectation_checks["control_code_exact"] = (
            trace is not None and trace.get("control_code") == case.expected_control_code
        )
    if case.expected_trace_statuses:
        expectation_checks["trace_status_allowed"] = (
            trace is not None and trace.get("status") in case.expected_trace_statuses
        )
    if case.expected_intents:
        expectation_checks["interaction_intent_allowed"] = (
            trace is not None and trace.get("intent") in case.expected_intents
        )
    if case.expected_families:
        expectation_checks["product_families_exact"] = trace is not None and trace.get(
            "product_families"
        ) == [family.value for family in case.expected_families]
    if case.require_evidence:
        evidence = context.get("evidence") if context is not None else None
        citations = context.get("citations") if context is not None else None
        expectation_checks["evidence_present"] = isinstance(evidence, dict) and any(
            evidence.get(key) for key in ("products", "comparisons", "aggregates", "documents")
        )
        expectation_checks["citations_present"] = isinstance(citations, list) and bool(citations)
    for fragment in case.forbidden_output_fragments:
        expectation_checks[f"forbidden_fragment_absent:{fragment}"] = (
            fragment.casefold() not in public_payload
        )
    safety_checks: dict[str, bool] = {}
    if case.require_no_execution:
        allowed_statuses = set(case.expected_trace_statuses) or _SAFE_CONTROL_STATUSES
        safety_checks = {
            "safe_control_status": trace is not None and trace.get("status") in allowed_statuses,
            "candidate_count_not_executed": trace is not None
            and trace.get("candidate_count") in {None, 0},
            "returned_evidence_empty": _returned_evidence_is_empty(trace),
            "public_context_empty": _context_is_empty(context),
        }
    checks = {**contract_checks, **expectation_checks, **safety_checks}
    violations = [name for name, passed in checks.items() if not passed]
    actual_families = trace.get("product_families") if trace is not None else None
    return OfficialAcceptanceCaseResult(
        request_index=request_index,
        id=case.id,
        kind=case.kind,
        source_example_id=case.source_example_id,
        answerability=case.answerability,
        http_status=http_status,
        content_type=content_type,
        actual_status=trace.get("status") if trace is not None else None,
        actual_intent=trace.get("intent") if trace is not None else None,
        actual_product_families=(actual_families if isinstance(actual_families, list) else None),
        response_bytes=response_bytes,
        response_sha256=_response_sha256(body),
        latency_ms=round(latency_ms, 3),
        checks=checks,
        violations=violations,
        contract_passed=all(contract_checks.values()),
        safety_passed=all(safety_checks.values()),
        passed=not violations,
    )


def validate_health(
    http_status: int,
    body: dict[str, Any],
    *,
    expected_fund_execution_policy: str = "locked",
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


def _build_summary(
    health: OfficialAcceptanceHealth,
    source_artifact: SourceArtifactVerification,
    cases: Sequence[OfficialAcceptanceCase],
    results: Sequence[OfficialAcceptanceCaseResult],
) -> OfficialAcceptanceSummary:
    public = [
        result for result in results if result.kind is OfficialAcceptanceCaseKind.PUBLIC_EXAMPLE
    ]
    edges = [
        result for result in results if result.kind is OfficialAcceptanceCaseKind.TRANSPORT_EDGE
    ]
    public_answerable = [
        result for result in public if result.answerability is BriefingAnswerability.ANSWERABLE
    ]
    public_unanswerable = [
        result for result in public if result.answerability is BriefingAnswerability.UNANSWERABLE
    ]
    no_execution_ids = {case.id for case in cases if case.require_no_execution}
    no_execution = [result for result in results if result.id in no_execution_ids]
    api_perfect = health.passed and all(result.passed for result in results)
    return OfficialAcceptanceSummary(
        total=len(results),
        passed=sum(result.passed for result in results),
        contract_passed=sum(result.contract_passed for result in results),
        public_examples_total=len(public),
        public_examples_passed=sum(result.passed for result in public),
        public_answerable_total=len(public_answerable),
        public_answerable_success_observed=sum(
            result.actual_status == "success" for result in public_answerable
        ),
        public_unanswerable_total=len(public_unanswerable),
        public_unanswerable_safely_handled=sum(
            result.safety_passed for result in public_unanswerable
        ),
        transport_edge_total=len(edges),
        transport_edge_passed=sum(result.passed for result in edges),
        no_execution_total=len(no_execution),
        no_execution_passed=sum(result.safety_passed for result in no_execution),
        api_perfect=api_perfect,
        perfect=(api_perfect and source_artifact.status is SourceArtifactStatus.VERIFIED),
    )


class OfficialAcceptanceRunner:
    def __init__(
        self,
        *,
        loaded_suite: LoadedBriefingExampleSuite,
        base_url: str,
        implementation_commit: str,
        runtime_image_reference: str | None,
        observed_source_artifact_sha256: str | None,
        request_timeout_seconds: float = 60.0,
        expected_fund_execution_policy: Literal["locked"] = "locked",
        requester: HttpRequester = request_json,
    ) -> None:
        if request_timeout_seconds <= 0:
            raise ValueError("request_timeout_seconds must be positive")
        if len(implementation_commit) not in range(7, 41) or any(
            char not in "0123456789abcdef" for char in implementation_commit
        ):
            raise ValueError("implementation_commit must be 7-40 lowercase hex characters")
        if observed_source_artifact_sha256 is not None:
            if len(observed_source_artifact_sha256) != 64 or any(
                char not in "0123456789abcdef" for char in observed_source_artifact_sha256
            ):
                raise ValueError("observed source artifact SHA-256 must be lowercase hex")
            if observed_source_artifact_sha256 != FROZEN_SOURCE_ARTIFACT_SHA256:
                raise ValueError(
                    "observed source artifact SHA-256 differs: "
                    f"expected {FROZEN_SOURCE_ARTIFACT_SHA256}, "
                    f"got {observed_source_artifact_sha256}"
                )
        self.loaded_suite = loaded_suite
        self.base_url = base_url.rstrip("/")
        self.implementation_commit = implementation_commit
        self.runtime_image_reference = runtime_image_reference
        self.request_timeout_seconds = request_timeout_seconds
        self.expected_fund_execution_policy = expected_fund_execution_policy
        self.requester = requester
        self.cases = build_official_acceptance_cases(loaded_suite)
        self.source_artifact = SourceArtifactVerification(
            artifact_name=loaded_suite.suite.source.artifact_name,
            expected_sha256=FROZEN_SOURCE_ARTIFACT_SHA256,
            observed_sha256=observed_source_artifact_sha256,
            status=(
                SourceArtifactStatus.VERIFIED
                if observed_source_artifact_sha256 is not None
                else SourceArtifactStatus.NOT_PROVIDED
            ),
        )

    def run(self, *, generated_at_utc: str) -> OfficialAcceptanceReport:
        health_status, health_body, health_bytes, health_latency, _ = self.requester(
            f"{self.base_url}/health",
            self.request_timeout_seconds,
        )
        health_violations = validate_health(
            health_status,
            health_body,
            expected_fund_execution_policy=self.expected_fund_execution_policy,
        )
        health = OfficialAcceptanceHealth(
            passed=not health_violations,
            http_status=health_status,
            ready_product_families=health_body.get("ready_product_families", []),
            fund_execution_policy=health_body.get("fund_execution_policy"),
            response_bytes=health_bytes,
            latency_ms=round(health_latency, 3),
            violations=health_violations,
        )
        results: list[OfficialAcceptanceCaseResult] = []
        for request_index, case in enumerate(self.cases, start=1):
            status, body, size, latency, content_type = self.requester(
                f"{self.base_url}/answer?{case.query_string()}",
                self.request_timeout_seconds,
            )
            results.append(
                evaluate_official_acceptance_case(
                    case,
                    request_index=request_index,
                    http_status=status,
                    body=body,
                    response_bytes=size,
                    latency_ms=latency,
                    content_type=content_type,
                )
            )
        summary = _build_summary(health, self.source_artifact, self.cases, results)
        suite = self.loaded_suite.suite
        return OfficialAcceptanceReport(
            generated_at_utc=generated_at_utc,
            base_url=self.base_url,
            implementation_commit=self.implementation_commit,
            runtime_image_reference=self.runtime_image_reference,
            suite_id=suite.suite_id,
            suite_version=suite.suite_version,
            suite_sha256=self.loaded_suite.sha256,
            source_title=suite.source.title,
            source_interpretation=suite.source.interpretation,
            source_artifact=self.source_artifact,
            health=health,
            summary=summary,
            cases=results,
            interpretation_limits=[
                "공개 예시 8개는 질의 분포 예시이며 실제 평가 문항이나 독립 blind가 아니다.",
                "P0-4는 GET 전송·다섯 문자열·UTF-8·안전 제어 계약을 검증한다.",
                "공개 답변 가능 예시의 success 관측 수는 기록만 하며 P0-4 통과 조건이 아니다.",
                "문서·관계 검색의 의미 완결성은 P0-5~P0-7에서 별도로 검증한다.",
                "no-execution은 공개 응답의 candidate·evidence가 비어 있음을 확인하며 "
                "내부 provider 호출 감사는 별도 Audit 검증 범위다.",
            ],
        )
